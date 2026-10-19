"""Instant static scan and sandboxed tests for a connected app.

Repository-controlled commands run from a temporary source snapshot through
``mcp_server.execution_sandbox``. Native execution is never used as fallback.
The sandbox has no network, receives a clean environment, omits credential
files and Git metadata, and caps runtime, memory, process count, output, and
workspace growth. Existing Python virtualenv and Node dependency files may be
copied into the sandbox and executed there. Dependency installation remains
disabled. Python and npm advisory lookups use scanner-owned tools against
sanitized exact-version metadata in a temporary directory; other ecosystems
without a metadata-only adapter are skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.language_tools import Check, ToolProfile, detect_profile
from core.models import Finding, FindingCategory, FindingSeverity, MonitoredApp
from core.repo_connect import CONNECTED_APPS_ROOT, app_fingerprint
from mcp_server.execution_sandbox import run_untrusted_command
from mcp_server.sandbox import REPO_ROOT

logger = structlog.get_logger(__name__)

INSTALL_TIMEOUT_SECONDS = 300.0
COMMAND_TIMEOUT_SECONDS = 180.0
MAX_CAPTURED_OUTPUT_BYTES = 100_000
MAX_AUDIT_OUTPUT_BYTES = 5_000_000

ProgressCallback = Callable[[str, int], Awaitable[None]]

_SEVERITY_PENALTY = {
    FindingSeverity.LOW: 2,
    FindingSeverity.MEDIUM: 5,
    FindingSeverity.HIGH: 10,
    FindingSeverity.CRITICAL: 20,
}


class ScanError(Exception):
    """Raised when a scan can't even start (e.g. the app's directory is gone)."""


@dataclass(frozen=True)
class CommandOutcome:
    ok: bool
    timed_out: bool
    output: str
    sandbox_unavailable: bool = False
    resource_limited: bool = False


@dataclass(frozen=True)
class FindingDraft:
    category: FindingCategory
    severity: FindingSeverity
    tool: str
    message: str
    file_path: str | None = None
    line_number: int | None = None


@dataclass(frozen=True)
class ScanSummary:
    app_id: int
    tests_passed: bool | None
    findings_count: int
    health_score: int
    #: checks not run, e.g. "lint: skipped: tool not installed (mvn)"
    skipped_checks: tuple[str, ...] = ()


_SCANNER_TOOLS = ("pytest", "ruff", "mypy", "pip-audit")
_EXACT_PYTHON_PIN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
_EXACT_NPM_PIN = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")


def _default_scanner_tools_root() -> Path:
    if os.name == "nt":
        cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_root / "selfheal" / "scanner-tools-v1"


SCANNER_TOOLS_ROOT = _default_scanner_tools_root()
_SCANNER_TOOLS_LOCK = asyncio.Lock()

#: known heavy/ML-framework package names this project will never install
#: for a scan -- a fixed denylist rather than a live PyPI/npm size query
#: (no network dependency, no extra latency), covering the exact packages a
#: request to this project named (torch/tensorflow/transformers/chromadb/
#: CUDA) plus their common close relatives. Matched as a substring of the
#: manifest's own package name, case-insensitively, so "torch",
#: "torchvision", "nvidia-cuda-runtime-cu12" etc. all match.
_HEAVY_DEPENDENCY_NAMES = (
    "torch",
    "tensorflow",
    "transformers",
    "chromadb",
    "cuda",
    "sentence-transformers",
    "onnxruntime",
    "spacy",
    "keras",
    "jax",
    "mxnet",
    "deepspeed",
    "vllm",
)


def _package_name(requirement_line: str) -> str:
    """`"torch>=2.0.0  # comment"` -> `"torch"` (also strips extras like
    `package[extra]` and environment markers)."""
    line = requirement_line.split("#", 1)[0].strip()
    return re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip().lower()


def detect_heavy_dependencies(app_dir: Path) -> list[str]:
    """Scan the app's own manifest files (never installing anything) for a
    package name matching `_HEAVY_DEPENDENCY_NAMES`. Returns the matched
    package names (sorted, deduped) -- empty if none found."""
    found: set[str] = set()

    requirements = app_dir / "requirements.txt"
    if requirements.exists():
        for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines():
            pkg = _package_name(line)
            if pkg and any(heavy in pkg for heavy in _HEAVY_DEPENDENCY_NAMES):
                found.add(pkg)

    pyproject = app_dir / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="ignore").lower()
        for heavy in _HEAVY_DEPENDENCY_NAMES:
            if heavy in text:
                found.add(heavy)

    package_json = app_dir / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        deps: dict[str, object] = {}
        if isinstance(data, dict):
            deps |= data.get("dependencies") or {}
            deps |= data.get("devDependencies") or {}
        for name in deps:
            lowered = name.lower()
            if any(heavy in lowered for heavy in _HEAVY_DEPENDENCY_NAMES):
                found.add(lowered)

    return sorted(found)


def _scanner_venv_python() -> Path:
    """Return the trusted scanner tools interpreter outside connected repos."""
    venv_dir = SCANNER_TOOLS_ROOT / "venv"
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _scanner_tool_environment(root: Path, temp: Path) -> dict[str, str]:
    """Use a private temporary home without ambient credentials or config."""
    passthrough = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
    )
    env = {name: os.environ[name] for name in passthrough if name in os.environ}
    home = root / ".setup-home"
    for directory in (home, home / "AppData" / "Roaming", home / "AppData" / "Local", temp):
        directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        home.chmod(0o700)
        temp.chmod(0o700)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
            "PYTHONNOUSERSITE": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


async def _communicate_setup_process(
    process: asyncio.subprocess.Process, *, timeout: float, operation: str
) -> bytes:
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ScanError(f"Scanner tool setup timed out during {operation}") from None
    if process.returncode:
        raise ScanError(
            f"Scanner tool setup failed during {operation}: "
            + stdout.decode(errors="replace")[-2000:]
        )
    return stdout


async def _ensure_scanner_tools_venv() -> Path:
    """Install trusted scanner tools outside any connected repository.

    The venv is mounted read-only into the repository-code sandbox. Repository
    content can neither pre-seed an executable that runs on the host nor
    modify scanner tools that later scans trust.
    """
    async with _SCANNER_TOOLS_LOCK:
        root = SCANNER_TOOLS_ROOT
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ScanError("Trusted scanner tools path is not a regular directory")
        root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            root.chmod(0o700)
        venv_dir = root / "venv"
        if venv_dir.is_symlink():
            raise ScanError("Trusted scanner tools venv cannot be a symlink")
        python = _scanner_venv_python()
        if python.is_file():
            return python
        if venv_dir.exists() and any(venv_dir.iterdir()):
            raise ScanError(
                "Trusted scanner tools venv is incomplete; inspect it before removing it"
            )

        build_root = Path(tempfile.mkdtemp(prefix=".scanner-tools-build-", dir=root))
        build_venv = build_root / "venv"
        try:
            env = _scanner_tool_environment(root, build_root / "tmp")
            create = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "venv",
                str(build_venv),
                cwd=str(root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await _communicate_setup_process(create, timeout=60, operation="venv creation")

            build_python = (
                build_venv / "Scripts" / "python.exe"
                if sys.platform == "win32"
                else build_venv / "bin" / "python"
            )
            install = await asyncio.create_subprocess_exec(
                str(build_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--only-binary=:all:",
                *_SCANNER_TOOLS,
                cwd=str(root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await _communicate_setup_process(
                install, timeout=INSTALL_TIMEOUT_SECONDS, operation="trusted tool installation"
            )
            if not build_python.is_file():
                raise ScanError(
                    "Scanner tool setup completed without creating its Python executable"
                )
            os.replace(build_venv, venv_dir)
        finally:
            shutil.rmtree(build_root, ignore_errors=True)

        if not python.is_file():
            raise ScanError("Scanner tool setup completed without creating its Python executable")
        return python


def _app_dir(app: MonitoredApp) -> Path:
    """Resolve and validate the app's own directory, refusing to scan
    anywhere outside `connected_apps/` even if `local_repo_path` were ever
    corrupted to something like `../../etc`."""
    resolved = (REPO_ROOT / app.local_repo_path).resolve()
    try:
        resolved.relative_to(CONNECTED_APPS_ROOT)
    except ValueError as exc:
        raise ScanError(
            f"App {app.name!r}'s local_repo_path {app.local_repo_path!r} is "
            "outside connected_apps/ -- refusing to scan"
        ) from exc
    if not resolved.is_dir():
        raise ScanError(f"App {app.name!r}'s directory {resolved} does not exist")
    return resolved


async def _run(
    command: str,
    *,
    cwd: Path,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    merge_stderr: bool = True,
    read_only_paths: Sequence[Path] = (),
) -> CommandOutcome:
    """Run `command`, capturing output for both display and, for some
    callers, JSON parsing.

    `merge_stderr=True` (the default, used for test/lint/install commands)
    folds stderr into the same stream as stdout, since a failing test's
    stderr is exactly what a human -- or the AI reading a Finding's message
    -- needs to see next to its stdout. Tools whose *stdout* is meant to be
    parsed as JSON (`pip-audit --format json`, `npm audit --json`) must use
    `merge_stderr=False`: both routinely write a human-readable warning or
    summary line to stderr even on success (e.g. pip-audit's own "Found N
    known vulnerabilities" line, or a venv-relocation warning on Windows),
    and folding that text into the same stream corrupts the JSON, making
    `json.loads` raise and the parser silently return zero findings --
    exactly the failure mode that let real vulnerabilities go undetected
    before this was split out.
    """
    result = await run_untrusted_command(
        command,
        cwd=cwd,
        timeout=timeout,
        merge_stderr=merge_stderr,
        read_only_paths=read_only_paths,
    )
    return CommandOutcome(
        ok=result.get("passed", False),
        timed_out=result.get("timed_out", False),
        output=str(result.get("output", ""))[-MAX_CAPTURED_OUTPUT_BYTES:],
        sandbox_unavailable=result.get("sandbox_unavailable", False),
        resource_limited=result.get("resource_limited", False),
    )


def parse_ruff_json(output: str) -> list[FindingDraft]:
    try:
        items = json.loads(output)
    except json.JSONDecodeError:
        return []
    drafts = []
    for item in items if isinstance(items, list) else []:
        location = item.get("location", {})
        drafts.append(
            FindingDraft(
                category=FindingCategory.LINT,
                severity=FindingSeverity.LOW,
                tool="ruff",
                file_path=item.get("filename"),
                line_number=location.get("row"),
                message=f"{item.get('code', '')}: {item.get('message', '')}".strip(": "),
            )
        )
    return drafts


def parse_mypy_json(output: str) -> list[FindingDraft]:
    drafts = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        severity = (
            FindingSeverity.MEDIUM if item.get("severity") == "error" else FindingSeverity.LOW
        )
        drafts.append(
            FindingDraft(
                category=FindingCategory.TYPE_CHECK,
                severity=severity,
                tool="mypy",
                file_path=item.get("file"),
                line_number=item.get("line"),
                message=str(item.get("message", "")),
            )
        )
    return drafts


def parse_pip_audit_json(output: str) -> list[FindingDraft]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    drafts = []
    dependencies = data.get("dependencies", []) if isinstance(data, dict) else []
    for dep in dependencies:
        name = dep.get("name", "unknown")
        version = dep.get("version", "")
        for vuln in dep.get("vulns", []) or []:
            fix_versions = vuln.get("fix_versions") or []
            message = (
                f"{name} {version}: {vuln.get('id', 'vulnerability')} {vuln.get('description', '')}"
            ).strip()
            if fix_versions:
                message += f" (fix available: {', '.join(fix_versions)})"
            drafts.append(
                FindingDraft(
                    category=FindingCategory.DEPENDENCY,
                    severity=FindingSeverity.HIGH,
                    tool="pip-audit",
                    file_path=None,
                    line_number=None,
                    message=message,
                )
            )
    return drafts


_NPM_SEVERITY_MAP = {
    "info": FindingSeverity.LOW,
    "low": FindingSeverity.LOW,
    "moderate": FindingSeverity.MEDIUM,
    "high": FindingSeverity.HIGH,
    "critical": FindingSeverity.CRITICAL,
}


def parse_npm_audit_json(output: str) -> list[FindingDraft]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    drafts = []
    vulnerabilities = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    for pkg_name, info in vulnerabilities.items():
        severity = _NPM_SEVERITY_MAP.get(str(info.get("severity", "")), FindingSeverity.MEDIUM)
        via = info.get("via", [])
        titles = [v.get("title", str(v)) for v in via if isinstance(v, dict)]
        message = f"{pkg_name}: " + ("; ".join(titles) if titles else "known vulnerability")
        drafts.append(
            FindingDraft(
                category=FindingCategory.DEPENDENCY,
                severity=severity,
                tool="npm-audit",
                file_path=None,
                line_number=None,
                message=message,
            )
        )
    return drafts


def _python_audit_pins(app_dir: Path) -> list[str]:
    """Extract exact pins only, excluding options, URLs, markers and ranges."""
    lines: list[str] = []
    requirements = app_dir / "requirements.txt"
    if requirements.is_file():
        lines.extend(requirements.read_text(encoding="utf-8", errors="ignore").splitlines())
    project = app_dir / "pyproject.toml"
    if project.is_file():
        try:
            data = tomllib.loads(project.read_text(encoding="utf-8"))
            lines.extend(data.get("project", {}).get("dependencies", []))
        except (tomllib.TOMLDecodeError, OSError, AttributeError, TypeError):
            pass
    # Lockfiles provide a complete, already-resolved package set. Read only
    # package names and exact versions; never pass source/build settings to pip.
    for lock_name in ("uv.lock", "poetry.lock", "pdm.lock"):
        lock_path = app_dir / lock_name
        if not lock_path.is_file():
            continue
        try:
            locked = tomllib.loads(lock_path.read_text(encoding="utf-8"))
            records = locked.get("package", locked.get("packages", []))
            for record in records if isinstance(records, list) else []:
                name, version = record.get("name"), record.get("version")
                source = record.get("source", {})
                source_type = source.get("type") if isinstance(source, dict) else None
                is_registry_source = not isinstance(source, dict) or (
                    source_type in (None, "registry")
                    and not any(
                        key in source for key in ("git", "directory", "editable", "virtual")
                    )
                )
                if (
                    isinstance(name, str)
                    and isinstance(version, str)
                    and _EXACT_PYTHON_PIN.fullmatch(f"{name}=={version}")
                    and is_registry_source
                ):
                    lines.append(f"{name}=={version}")
        except (tomllib.TOMLDecodeError, OSError, AttributeError, TypeError):
            continue
    pipfile_lock = app_dir / "Pipfile.lock"
    if pipfile_lock.is_file():
        try:
            locked = json.loads(pipfile_lock.read_text(encoding="utf-8"))
            for section in ("default", "develop"):
                for name, record in locked.get(section, {}).items():
                    version = record.get("version", "") if isinstance(record, dict) else ""
                    if isinstance(version, str) and version.startswith("=="):
                        lines.append(f"{name}=={version[2:]}")
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    pins: dict[str, str] = {}
    for line in lines:
        raw = line.split("#", 1)[0].strip()
        if not raw or raw.startswith(("-", "[")) or ";" in raw or " @ " in raw:
            continue
        match = _EXACT_PYTHON_PIN.fullmatch(raw)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = f"{match.group(1)}=={match.group(2)}"
    return [pins[key] for key in sorted(pins)]


def _npm_audit_lock(app_dir: Path) -> dict[str, Any] | None:
    """Build a metadata-only lockfile; preserve locked transitives, ignore URLs."""
    try:
        data = json.loads((app_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    package_lock_path = app_dir / "package-lock.json"
    try:
        original_lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    source_packages = original_lock.get("packages", {}) if isinstance(original_lock, dict) else {}
    if not isinstance(source_packages, dict):
        return None
    packages: dict[str, Any] = {"": {"name": "scanner-audit-input", "version": "0.0.0"}}
    for path, record in source_packages.items():
        if not isinstance(path, str) or not isinstance(record, dict):
            continue
        if not re.fullmatch(
            r"node_modules/(?:@[a-z0-9._-]+/)?[a-z0-9._-]+"
            r"(?:/node_modules/(?:@[a-z0-9._-]+/)?[a-z0-9._-]+)*",
            path,
        ):
            continue
        version = record.get("version")
        if not isinstance(version, str) or not _EXACT_NPM_PIN.fullmatch(version):
            continue
        name = record.get("name")
        if not isinstance(name, str):
            name = path.rsplit("node_modules/", 1)[-1]
        if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name):
            continue
        # Only the lock path, package name, and exact version cross the trust
        # boundary. In particular, resolved URLs and integrity/config fields
        # are deliberately omitted.
        packages[path] = {"name": name, "version": version}
    if len(packages) == 1:
        # For lockfile v1, recover flat dependency records if available.
        def add_legacy(records: object, prefix: str = "node_modules/") -> None:
            if not isinstance(records, dict):
                return
            for name, record in records.items():
                if not isinstance(name, str) or not isinstance(record, dict):
                    continue
                version = record.get("version")
                path = prefix + name
                if isinstance(version, str) and _EXACT_NPM_PIN.fullmatch(version):
                    packages[path] = {"name": name, "version": version}
                add_legacy(record.get("dependencies"), path + "/node_modules/")

        add_legacy(original_lock.get("dependencies", {}))
    if len(packages) == 1:
        return None
    # npm's bulk advisory lookup examines every package record in the lock;
    # retaining the complete locked package set preserves transitive findings.
    return {
        "name": "scanner-audit-input",
        "version": "0.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": packages,
    }


async def _run_trusted_audit(command: Sequence[str], *, cwd: Path) -> CommandOutcome:
    """Run scanner-owned metadata lookup with a clean temporary environment."""
    with tempfile.TemporaryDirectory(prefix="selfheal-audit-") as temp_name:
        temp = Path(temp_name)
        env = _scanner_tool_environment(temp, temp / "tmp")
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[tuple[bytes, bool]] | None = None
        stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None

            async def read_capped(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
                captured = bytearray()
                truncated = False
                while chunk := await stream.read(64 * 1024):
                    remaining = MAX_AUDIT_OUTPUT_BYTES - len(captured)
                    captured.extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
                return bytes(captured), truncated

            stdout_task = asyncio.create_task(read_capped(process.stdout))
            stderr_task = asyncio.create_task(read_capped(process.stderr))
            _, (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.wait_for(
                asyncio.gather(process.wait(), stdout_task, stderr_task),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            tasks = [task for task in (stdout_task, stderr_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return CommandOutcome(False, True, "advisory lookup timed out")
        except OSError as exc:
            return CommandOutcome(False, False, str(exc))
        output = stdout.decode(errors="replace")
        return CommandOutcome(
            process.returncode == 0,
            False,
            output or stderr.decode(errors="replace"),
            resource_limited=stdout_truncated or stderr_truncated,
        )


async def _audit_python(app_dir: Path) -> tuple[list[FindingDraft], list[str]]:
    pins = _python_audit_pins(app_dir)
    if not pins:
        return [], ["audit: skipped: no exact Python dependency pins available"]
    python = await _ensure_scanner_tools_venv()
    with tempfile.TemporaryDirectory(prefix="selfheal-pip-audit-") as temp_name:
        root = Path(temp_name)
        req = root / "requirements.txt"
        req.write_text("\n".join(pins) + "\n", encoding="utf-8")
        outcome = await _run_trusted_audit(
            [
                str(python),
                "-m",
                "pip_audit",
                "--no-deps",
                "--requirement",
                str(req),
                "--format",
                "json",
            ],
            cwd=root,
        )
    if outcome.timed_out:
        return [], ["audit: skipped: pip-audit timed out"]
    if outcome.resource_limited:
        return [], ["audit: skipped: pip-audit output exceeded the safe capture limit"]
    if not outcome.output.strip() or not outcome.output.lstrip().startswith("{"):
        return [], ["audit: skipped: pip-audit returned no JSON advisory results"]
    return parse_pip_audit_json(outcome.output), []


async def _audit_npm(app_dir: Path) -> tuple[list[FindingDraft], list[str]]:
    lock = _npm_audit_lock(app_dir)
    npm = shutil.which("npm")
    if lock is None:
        return [], ["audit: skipped: no exact npm dependency pins available"]
    if npm is None:
        return [], ["audit: skipped: npm is not installed"]
    with tempfile.TemporaryDirectory(prefix="selfheal-npm-audit-") as temp_name:
        root = Path(temp_name)
        (root / "package.json").write_text(
            json.dumps({"name": "scanner-audit-input", "version": "0.0.0", "private": True}),
            encoding="utf-8",
        )
        (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        user_config = root / "npmrc-user"
        global_config = root / "npmrc-global"
        user_config.write_text("", encoding="utf-8")
        global_config.write_text("", encoding="utf-8")
        outcome = await _run_trusted_audit(
            [
                npm,
                "audit",
                "--json",
                "--package-lock-only",
                "--ignore-scripts",
                "--registry=https://registry.npmjs.org/",
                f"--userconfig={user_config}",
                f"--globalconfig={global_config}",
            ],
            cwd=root,
        )
    if outcome.timed_out:
        return [], ["audit: skipped: npm audit timed out"]
    if outcome.resource_limited:
        return [], ["audit: skipped: npm audit output exceeded the safe capture limit"]
    if not outcome.output.strip() or not outcome.output.lstrip().startswith("{"):
        return [], ["audit: skipped: npm audit returned no JSON advisory results"]
    try:
        result = json.loads(outcome.output)
    except json.JSONDecodeError:
        return [], ["audit: skipped: npm audit returned invalid JSON"]
    if isinstance(result, dict) and result.get("error"):
        error = result["error"]
        code = error.get("code", "unknown error") if isinstance(error, dict) else "unknown error"
        return [], [f"audit: skipped: npm audit failed ({code})"]
    return parse_npm_audit_json(outcome.output), []


def compute_health_score(drafts: list[FindingDraft], *, tests_passed: bool | None) -> int:
    """0-100, a simple deterministic heuristic (not a "correctness" score):
    start at 100, subtract a per-finding penalty by severity, subtract 20
    more if the test suite doesn't pass. Clamped to [0, 100]."""
    score = 100
    for draft in drafts:
        score -= _SEVERITY_PENALTY[draft.severity]
    if tests_passed is False:
        score -= 20
    return max(0, min(100, score))


def _tool_available(check: Check, app_dir: Path) -> bool:
    if "/" in check.requires:
        return (app_dir / check.requires).exists()
    return shutil.which(check.requires) is not None or (app_dir / check.requires).exists()


def _skip_reason(name: str, check: Check) -> str:
    return f"{name}: skipped: tool not installed ({check.requires})"


_MISSING_DEPENDENCY_MARKERS = (
    "modulenotfounderror",
    "no module named",
    "cannot find module",
    "err_module_not_found",
    "no such file or directory",
    "command not found",
    "is not recognized as an internal or external command",
)


def _missing_test_dependency(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _MISSING_DEPENDENCY_MARKERS)


async def _run_profile_checks(
    profile: ToolProfile, app_dir: Path, test_command: str, notify: ProgressCallback
) -> tuple[list[FindingDraft], bool | None, list[str]]:
    """Run a non-Python/JS language's standard test/lint/audit commands.

    A missing tool is recorded as skipped, never as a finding. For a
    present tool, a non-zero exit becomes one finding carrying the output
    tail (these tools' native output formats vary too much to parse
    per-tool; the tail is what a human or the fix agent needs anyway).
    """
    drafts: list[FindingDraft] = []
    skipped: list[str] = []
    tests_passed: bool | None = None

    await notify("checking dependency installation policy", 10)
    if profile.install:
        skipped.append(
            "install: skipped: repository dependency installs can execute package scripts; "
            "no offline wheel/cache staging is configured"
        )

    await notify("running tests", 35)
    if _tool_available(profile.test, app_dir):
        outcome = await _run(test_command, cwd=app_dir)
        if outcome.sandbox_unavailable:
            skipped.append(f"tests: skipped: {outcome.output}")
        elif _missing_test_dependency(outcome.output):
            skipped.append("tests: skipped: a declared test dependency is unavailable offline")
        else:
            tests_passed = outcome.ok and not outcome.timed_out
        if tests_passed is False:
            drafts.append(
                FindingDraft(
                    category=FindingCategory.TEST,
                    severity=FindingSeverity.HIGH,
                    tool="test-runner",
                    message=(
                        f"Test suite failed (`{test_command}`). Tail of output:\n"
                        f"{outcome.output[-2000:]}"
                    ),
                )
            )
    else:
        skipped.append(_skip_reason("tests", profile.test))

    await notify("running linter", 60)
    if profile.lint is None:
        pass
    elif _tool_available(profile.lint, app_dir):
        outcome = await _run(profile.lint.command, cwd=app_dir)
        if outcome.sandbox_unavailable:
            skipped.append(f"lint: skipped: {outcome.output}")
        elif not outcome.ok:
            drafts.append(
                FindingDraft(
                    category=FindingCategory.LINT,
                    severity=FindingSeverity.MEDIUM,
                    tool=profile.lint.requires.rsplit("/", 1)[-1],
                    message=(
                        f"`{profile.lint.command}` reported problems:\n{outcome.output[-2000:]}"
                    ),
                )
            )
    else:
        skipped.append(_skip_reason("lint", profile.lint))

    await notify("checking dependencies for vulnerabilities", 85)
    if profile.audit is None:
        skipped.append(f"audit: skipped: no standard {profile.build_tool} audit command")
    else:
        skipped.append("audit: skipped: no safe metadata-only adapter for this ecosystem")

    return drafts, tests_passed, skipped


async def _noop_progress(_stage: str, _percent: int) -> None:
    return None


async def run_scan(
    session: AsyncSession, app: MonitoredApp, *, progress: ProgressCallback | None = None
) -> ScanSummary:
    """Run the full instant scan for `app` and upsert every finding.

    Caller commits (matches this project's other session_scope()-using
    write paths). `progress(stage, percent)` is awaited at each step so a
    caller can forward it to Socket.io for a live progress bar.
    """
    notify = progress or _noop_progress
    app_dir = _app_dir(app)
    drafts: list[FindingDraft] = []
    tests_passed: bool | None = None

    heavy_deps = detect_heavy_dependencies(app_dir)
    if heavy_deps:
        return await _run_static_only_scan(session, app, app_dir, heavy_deps, notify)

    skipped: list[str] = []
    profile = detect_profile(app_dir) if app.language not in ("python", "javascript") else None
    if profile is not None:
        drafts, tests_passed, skipped = await _run_profile_checks(
            profile, app_dir, app.test_command, notify
        )
        return await _finish_scan(session, app, drafts, tests_passed, skipped, notify)

    has_pyproject = (app_dir / "pyproject.toml").exists()
    has_requirements = (app_dir / "requirements.txt").exists()
    venv_python: Path | None = None

    await notify("installing dependencies", 10)
    if app.language == "python":
        venv_python = await _ensure_scanner_tools_venv()
        if has_pyproject or has_requirements:
            skipped.append(
                "install: skipped: project dependencies are not installed because package "
                "build hooks execute repository-controlled code and sandbox egress is disabled"
            )
    elif app.language == "javascript" and (app_dir / "package.json").exists():
        skipped.append(
            "install: skipped: npm lifecycle scripts are not run and sandbox egress is disabled"
        )

    await notify("running tests", 35)
    test_cmd = f'"{venv_python}" -m pytest -q' if venv_python else app.test_command
    scanner_mounts = (venv_python.parent.parent,) if venv_python else ()
    test_outcome = await _run(test_cmd, cwd=app_dir, read_only_paths=scanner_mounts)
    if test_outcome.sandbox_unavailable:
        skipped.append(f"tests: skipped: {test_outcome.output}")
    elif _missing_test_dependency(test_outcome.output):
        skipped.append("tests: skipped: a test dependency is unavailable offline")
    else:
        tests_passed = test_outcome.ok and not test_outcome.timed_out
    if tests_passed is False:
        drafts.append(
            FindingDraft(
                category=FindingCategory.TEST,
                severity=FindingSeverity.HIGH,
                tool="test-runner",
                message=(
                    f"Test suite failed (`{app.test_command}`). Tail of output:\n"
                    f"{test_outcome.output[-2000:]}"
                ),
            )
        )

    if app.lint_command:
        await notify("running linter", 55)
        if venv_python:
            lint_outcome = await _run(
                f'"{venv_python}" -m ruff check --output-format=json .',
                cwd=app_dir,
                merge_stderr=False,
                read_only_paths=scanner_mounts,
            )
            if lint_outcome.sandbox_unavailable:
                skipped.append(f"lint: skipped: {lint_outcome.output}")
            else:
                drafts.extend(parse_ruff_json(lint_outcome.output))
        else:
            await _run(app.lint_command, cwd=app_dir)

    if venv_python:
        await notify("running type checker", 70)
        type_outcome = await _run(
            f'"{venv_python}" -m mypy . --ignore-missing-imports --output json',
            cwd=app_dir,
            merge_stderr=False,
            read_only_paths=scanner_mounts,
        )
        if type_outcome.sandbox_unavailable:
            skipped.append(f"type-check: skipped: {type_outcome.output}")
        else:
            drafts.extend(parse_mypy_json(type_outcome.output))

    await notify("checking dependencies for vulnerabilities", 85)
    if app.language == "python" and (has_requirements or has_pyproject):
        audit_drafts, audit_skipped = await _audit_python(app_dir)
        drafts.extend(audit_drafts)
        skipped.extend(audit_skipped)
    elif app.language == "javascript" and (app_dir / "package.json").exists():
        audit_drafts, audit_skipped = await _audit_npm(app_dir)
        drafts.extend(audit_drafts)
        skipped.extend(audit_skipped)

    return await _finish_scan(session, app, drafts, tests_passed, skipped, notify)


async def _run_static_only_scan(
    session: AsyncSession,
    app: MonitoredApp,
    app_dir: Path,
    heavy_deps: list[str],
    notify: ProgressCallback,
) -> ScanSummary:
    """A scan for an app whose manifest names a heavy/ML dependency
    (torch/tensorflow/transformers/chromadb/CUDA-class packages, see
    `_HEAVY_DEPENDENCY_NAMES`): never installs the app's own dependencies
    and never executes its tests. Static lint runs inside the repository
    sandbox when available; metadata-only audits can still run for Python
    and npm manifests."""
    drafts: list[FindingDraft] = [
        FindingDraft(
            category=FindingCategory.DEPENDENCY,
            severity=FindingSeverity.LOW,
            tool="scanner",
            message=(
                f"Static-only scan: this app depends on {', '.join(heavy_deps)}, which this "
                "project never installs (torch/tensorflow/transformers/chromadb/CUDA-class "
                "packages are always skipped). Static lint ran where possible; "
                "the test suite was not executed."
            ),
        )
    ]
    skipped = [
        f"install: skipped: heavy dependency detected ({', '.join(heavy_deps)}) "
        "-- static-only scan enforced",
        "tests: skipped: static-only scan (heavy dependencies present, nothing installed)",
    ]

    has_requirements = (app_dir / "requirements.txt").exists()
    has_pyproject = (app_dir / "pyproject.toml").exists()
    has_package_json = (app_dir / "package.json").exists()

    if app.language == "python" and (has_requirements or has_pyproject):
        await notify("running linter (static-only)", 40)
        venv_python = await _ensure_scanner_tools_venv()
        lint_outcome = await _run(
            f'"{venv_python}" -m ruff check --output-format=json .',
            cwd=app_dir,
            merge_stderr=False,
            read_only_paths=(venv_python.parent.parent,),
        )
        drafts.extend(parse_ruff_json(lint_outcome.output))

        await notify("checking dependencies for vulnerabilities", 80)
        audit_drafts, audit_skipped = await _audit_python(app_dir)
        drafts.extend(audit_drafts)
        skipped.extend(audit_skipped)
    elif app.language == "javascript" and has_package_json:
        skipped.append("lint: skipped: static-only scan (lint may need installed node_modules)")
        await notify("checking dependencies for vulnerabilities", 80)
        audit_drafts, audit_skipped = await _audit_npm(app_dir)
        drafts.extend(audit_drafts)
        skipped.extend(audit_skipped)
    else:
        skipped.append("lint: skipped: static-only scan, no supported manifest for this language")
        skipped.append("audit: skipped: no safe metadata-only adapter for this language")

    return await _finish_scan(session, app, drafts, None, skipped, notify)


async def _finish_scan(
    session: AsyncSession,
    app: MonitoredApp,
    drafts: list[FindingDraft],
    tests_passed: bool | None,
    skipped: list[str],
    notify: ProgressCallback,
) -> ScanSummary:
    await notify("saving results", 95)
    for draft in drafts:
        fingerprint = app_fingerprint(
            app.id, draft.tool, draft.file_path, draft.line_number, draft.message
        )
        values: dict[str, Any] = {
            "app_id": app.id,
            "fingerprint": fingerprint,
            "category": draft.category,
            "severity": draft.severity,
            "tool": draft.tool,
            "file_path": draft.file_path,
            "line_number": draft.line_number,
            "message": draft.message,
        }
        stmt = (
            pg_insert(Finding)
            .values(**values, occurrence_count=1)
            .on_conflict_do_update(
                index_elements=[Finding.app_id, Finding.fingerprint],
                set_={
                    **{k: v for k, v in values.items() if k not in ("app_id", "fingerprint")},
                    "occurrence_count": Finding.occurrence_count + 1,
                    "last_seen_at": func.now(),
                },
            )
        )
        await session.execute(stmt)

    health_score = compute_health_score(drafts, tests_passed=tests_passed)
    app.health_score = health_score
    app.last_scanned_at = datetime.now(UTC)

    findings_count_stmt = select(Finding).where(Finding.app_id == app.id)
    findings_count = len((await session.execute(findings_count_stmt)).scalars().all())

    await notify("done", 100)
    logger.info(
        "scanner.scan_complete",
        app_id=app.id,
        app_name=app.name,
        findings_count=len(drafts),
        health_score=health_score,
        tests_passed=tests_passed,
        skipped_checks=skipped,
    )
    return ScanSummary(
        app_id=app.id,
        tests_passed=tests_passed,
        findings_count=findings_count,
        health_score=health_score,
        skipped_checks=tuple(skipped),
    )
