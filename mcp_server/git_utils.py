"""Subprocess wrappers around `git` (blame, log, apply) and `pytest`.

Every function takes an explicit `cwd` — callers (tools/code.py) resolve
that path through `sandbox.py` first, so nothing here ever receives an
un-sandboxed path.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from core.git_env import git_subprocess_env, scrub_git_message
from mcp_server.execution_sandbox import run_untrusted_command

_PYTEST_BOOTSTRAP = """from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

repository = Path.cwd()
source_roots = [repository / "src", repository]
sys.path[:0] = [str(path) for path in source_roots if path.is_dir()]
arguments = sys.argv[1:]
test_files = []
for argument in arguments:
    if argument.startswith("-"):
        continue
    candidate = repository / argument
    if candidate.is_file() and candidate.suffix == ".py":
        test_files.append(candidate)
    elif candidate.is_dir():
        test_files.extend(candidate.rglob("test_*.py"))
        test_files.extend(candidate.rglob("*_test.py"))
if not test_files:
    for directory in (repository / "tests", repository / "test"):
        if directory.is_dir():
            test_files.extend(directory.rglob("test_*.py"))
            test_files.extend(directory.rglob("*_test.py"))

module_roots = set()
for test_file in test_files:
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        continue
    for node in ast.walk(tree):
        imported = []
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".", 1)[0])
        for name in imported:
            exists_locally = any(
                (root / name).exists() or (root / f"{name}.py").is_file()
                for root in source_roots
            )
            if exists_locally:
                module_roots.add(name)
if (repository / "tests" / "__init__.py").is_file():
    module_roots.add("tests")
for module in sorted(module_roots):
    imported = importlib.import_module(module)
    if module in {"test", "tests"} and (repository / module / "__init__.py").is_file():
        imported.__path__ = [str(repository / module)]

import pytest

raise SystemExit(pytest.main(arguments))
"""

GIT_TIMEOUT_SECONDS = 30.0
TEST_TIMEOUT_SECONDS = 120.0
# A whole suite (the pre-PR gate) legitimately takes far longer than one test file.
FULL_SUITE_TIMEOUT_SECONDS = 900.0


class GitCommandError(Exception):
    """Raised when a `git` subprocess fails or times out."""


async def _run_git(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    env, sensitive_values = git_subprocess_env()
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise GitCommandError(
            scrub_git_message(f"git {' '.join(args)} timed out after {timeout}s", sensitive_values)
        ) from exc

    if process.returncode != 0:
        message = f"git {' '.join(args)} failed: {stderr.decode(errors='replace')}"
        raise GitCommandError(scrub_git_message(message, sensitive_values))
    return stdout.decode(errors="replace")


async def blame_line(path: str, line: int, *, cwd: Path) -> dict[str, str]:
    """`git blame` for a single line, parsed from `--porcelain` output."""
    output = await _run_git(["blame", "-L", f"{line},{line}", "--porcelain", "--", path], cwd)
    return _parse_blame_porcelain(output)


def _parse_blame_porcelain(output: str) -> dict[str, str]:
    lines = output.splitlines()
    if not lines:
        raise GitCommandError("git blame returned no output")

    info: dict[str, str] = {"commit": lines[0].split()[0]}
    for line in lines[1:]:
        if line.startswith("author "):
            info["author"] = line.removeprefix("author ")
        elif line.startswith("author-time "):
            info["author_time"] = line.removeprefix("author-time ")
        elif line.startswith("summary "):
            info["summary"] = line.removeprefix("summary ")
        elif line.startswith("\t"):
            info["content"] = line[1:]
            break
    return info


_LOG_FIELD_SEPARATOR = "\x1f"


async def recent_commits(n: int, *, cwd: Path) -> list[dict[str, str]]:
    """Last `n` commits as {sha, author, date, subject}."""
    fmt = _LOG_FIELD_SEPARATOR.join(["%H", "%an", "%aI", "%s"])
    output = await _run_git(["log", f"-n{n}", f"--pretty=format:{fmt}"], cwd)

    commits: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        sha, author, date, subject = line.split(_LOG_FIELD_SEPARATOR)
        commits.append({"sha": sha, "author": author, "date": date, "subject": subject})
    return commits


async def apply_diff(
    unified_diff: str, *, cwd: Path, check_only: bool = False, reverse: bool = False
) -> None:
    """`git apply` (or, with `reverse=True`, undo) a unified diff against `cwd`.

    Raises GitCommandError if it doesn't apply (or doesn't reverse-apply).
    """
    # Providers see normalized LF through MCP file reads, while Windows
    # checkouts of connected repositories may use CRLF. Ignore whitespace
    # differences in hunk context so the same safe patch applies portably.
    # Models occasionally emit correct edits with stale unified-diff hunk
    # counts. Recount repairs only that framing metadata; Git still checks
    # every context line and rejects a patch that does not match the file.
    args = ["apply", "--recount", "--ignore-whitespace", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    if reverse:
        args.append("--reverse")

    patch_text = unified_diff.replace("\r\n", "\n").replace("\r", "\n")
    if not patch_text.endswith("\n"):
        patch_text += "\n"
    env, sensitive_values = git_subprocess_env()
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(input=patch_text.encode("utf-8")), timeout=GIT_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise GitCommandError(
            scrub_git_message(f"git apply timed out after {GIT_TIMEOUT_SECONDS}s", sensitive_values)
        ) from exc
    if process.returncode != 0:
        raise GitCommandError(
            scrub_git_message(
                f"git apply failed: {stderr.decode(errors='replace')}", sensitive_values
            )
        )


async def diff_stat(*, cwd: Path) -> str:
    """`git diff --stat` for the worktree's currently unstaged changes."""
    return await _run_git(["diff", "--stat"], cwd)


async def run_pytest(
    test_path: str | None,
    *,
    cwd: Path,
    timeout: float = TEST_TIMEOUT_SECONDS,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    """Run pytest in `cwd` (a worktree) with a timeout. Returns a JSON-able summary."""
    executable = python_executable or Path(sys.executable)
    args = [str(executable), "-c", _PYTEST_BOOTSTRAP, "-q"]
    if test_path:
        args.append(test_path)

    runtime_prefix = _python_runtime_prefix(executable)
    return await run_untrusted_command(
        args,
        cwd=cwd,
        timeout=timeout,
        read_only_paths=[runtime_prefix] if runtime_prefix else [],
    )


def configured_python_executable(command: str) -> Path | None:
    """Return the interpreter from an app's configured ``python -m pytest``.

    This parses server-side app configuration, never a provider tool call.
    Bare ``pytest`` commands continue to use the service's interpreter.
    """
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    if not tokens:
        return None
    executable = Path(tokens[0].strip('"'))
    if executable.name.lower() not in {"python", "python.exe", "python3", "python3.exe"}:
        return None
    if not executable.is_absolute() or not executable.is_file():
        return None
    if tokens[1:3] != ["-m", "pytest"]:
        return None
    return executable.resolve()


def _python_runtime_prefix(executable: Path) -> Path | None:
    resolved = executable.resolve()
    if resolved.parent.name.lower() in {"bin", "scripts"}:
        return resolved.parent.parent
    return None


def pytest_command(command: str) -> str:
    """Make a bare `pytest ...` test_command use this interpreter (no PATH lookup)."""
    if command.split(None, 1)[:1] == ["pytest"]:
        return f'"{sys.executable}" -m {command}'
    return command


async def run_test_command(
    command: str, *, cwd: Path, timeout: float = TEST_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Run a monitored app's own configured `test_command` as a shell command.

    Multi-app/multi-language support: a non-Python app's test suite isn't
    invoked with `python -m pytest` (there is no fixed argv shape across
    npm/go/etc.), so its `MonitoredApp.test_command` (e.g. "npm test",
    "go test ./...") is run verbatim via the shell instead. Same
    timeout/output-capping contract as `run_pytest`, so callers (the healer
    agent) treat both the same way.
    """
    runtime = configured_python_executable(command)
    runtime_prefix = _python_runtime_prefix(runtime) if runtime else None
    if runtime is not None:
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = []
        if tokens[1:3] == ["-m", "pytest"]:
            return await run_untrusted_command(
                [str(runtime), "-c", _PYTEST_BOOTSTRAP, *tokens[3:]],
                cwd=cwd,
                timeout=timeout,
                read_only_paths=[runtime_prefix] if runtime_prefix else [],
            )
    return await run_untrusted_command(
        command,
        cwd=cwd,
        timeout=timeout,
        read_only_paths=[runtime_prefix] if runtime_prefix else [],
    )
