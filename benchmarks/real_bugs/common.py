"""Shared paths and safe process helpers for the real-bug benchmark."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_cache_path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1].resolve()
_configured_data_root = os.environ.get("SELFHEAL_REAL_BUG_DATA_DIR")
DATA_ROOT = (
    Path(
        _configured_data_root
        if _configured_data_root
        else user_cache_path("selfheal") / "real-bugs"
    )
    .expanduser()
    .resolve()
)
if DATA_ROOT == PROJECT_ROOT or PROJECT_ROOT in DATA_ROOT.parents:
    raise RuntimeError("SELFHEAL_REAL_BUG_DATA_DIR must be outside the MCP repository root")
CASE_ROOT = DATA_ROOT / "cases"
PREPARED_ROOT = DATA_ROOT / "prepared"
CACHE_ROOT = DATA_ROOT / "cache"
VENV_ROOT = DATA_ROOT / "venvs"
RESULT_ROOT = ROOT / "results"
MAX_PATCH_BYTES = 1_000_000
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|test|__tests__)(/|$)|(^|/)test_[^/]+\.py$|_test\.py$|\.test\.py$",
    re.IGNORECASE,
)
REPOSITORY_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40,64}")


def load_repositories(path: Path) -> list[dict[str, Any]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    repos = document.get("repositories") if isinstance(document, dict) else None
    if not isinstance(repos, list) or not repos:
        raise ValueError(f"{path}: expected a non-empty 'repositories' list")
    normalized = []
    names: set[str] = set()
    for item in repos:
        if not isinstance(item, dict):
            raise ValueError("each repository entry must be a mapping")
        name, url = item.get("name"), item.get("url")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise ValueError(f"invalid repository name: {name!r}")
        if name in names:
            raise ValueError(f"duplicate repository name: {name}")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"repository {name} has no clone URL")
        deps = item.get("test_dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            raise ValueError(f"repository {name} test_dependencies must be a list of strings")
        command = item.get("test_command", "python -m pytest -q")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"repository {name} has an invalid test_command")
        names.add(name)
        normalized.append({**item, "name": name, "url": url.strip(), "test_command": command})
    return normalized


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command with inherited credentials removed and bounded output."""
    env = _safe_environment()
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell execution.
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {args[0]} {args[1:2]}\n"
            f"{(result.stdout + result.stderr)[-2500:]}"
        )
    return result


def _safe_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if any(term in key.upper() for term in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            env.pop(key, None)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return env


def run_bytes(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
) -> bytes:
    """Run a local command and return bytes without inheriting credentials."""
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell execution.
        args,
        cwd=cwd,
        env=_safe_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def git(*args: str, cwd: Path, timeout: float = 60, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, timeout=timeout, check=check).stdout


def safe_case_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        bool(normalized)
        and not normalized.startswith("/")
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in normalized.split("/"))
        and len(normalized) <= 240
    )


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def case_id(repository: str, fix_sha: str) -> str:
    digest = hashlib.sha256(f"{repository}\0{fix_sha}".encode()).hexdigest()[:12]
    return f"{repository}-{digest}"


def validate_case_metadata(metadata: Any, *, expected_id: str | None = None) -> str:
    """Validate repository identity and require a blinded, deterministic case ID."""
    if not isinstance(metadata, dict):
        raise ValueError("case metadata must be a mapping")
    repository = metadata.get("repository")
    fix_sha = metadata.get("fix_sha")
    identifier = metadata.get("case_id")
    if not isinstance(repository, str) or not REPOSITORY_NAME_RE.fullmatch(repository):
        raise ValueError("case metadata has an invalid repository name")
    if not isinstance(fix_sha, str) or not COMMIT_SHA_RE.fullmatch(fix_sha):
        raise ValueError("case metadata has an invalid fixing commit hash")
    expected = case_id(repository, fix_sha)
    if identifier != expected or (expected_id is not None and expected_id != expected):
        raise ValueError("case ID does not match its blinded repository and commit identity")
    return expected
