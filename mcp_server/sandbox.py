"""Filesystem sandbox for every mcp_server tool that touches disk.

Two separate policies, per SPEC.md's mcp-pod section:
  - **read** access: block secrets (`.env`, `.env.local`, ... but not the
    non-secret `.env.example`) and raw `.git/` internals.
  - **write** access: block those plus `.git/`, `.github/workflows/`, and
    `alembic/versions/` (SPEC.md's explicit forbidden-write list), and —
    separately — runtime auto-fixes (`propose_patch` for a `runtime_error`/
    `contract_violation` heal_job) may additionally be restricted to
    `apps/target_app/` only, so the healer can never modify its own
    infrastructure. That restriction is looked up from the heal_job's `type`
    in the database (see `tools/code.py`), never trusted from a caller-
    supplied parameter — SPEC.md's "guardrails are enforced in code, not
    only in prompts" applies here too.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREES_ROOT = REPO_ROOT / "worktrees"

MAX_READ_FILE_BYTES = 200_000
MAX_WRITE_FILE_BYTES = 500_000

_FORBIDDEN_WRITE_PREFIXES = (".git/", ".github/workflows/", "alembic/versions/")
_SECRET_DIR_NAMES = frozenset(
    {".aws", ".ssh", ".gnupg", ".kube", ".docker", "secrets", "credentials"}
)
_SECRET_FILE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        ".envrc",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "secret.json",
        "secrets.json",
        "token.json",
        "service-account.json",
    }
)
_SECRET_FILE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
#: Legacy default write scope for a runtime_error/contract_violation job
#: with no app_id (predates multi-app support). A job with an app_id uses
#: that app's own `allowed_write_paths` instead (see tools/code.py).
RUNTIME_FIX_ALLOWED_PREFIX = "apps/target_app/"
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class SandboxViolation(Exception):
    """Raised for any attempted access outside the allowed sandbox."""


def is_secret_path(relative_posix: str) -> bool:
    """Recognize common credential paths for every MCP read and write tool."""
    parts = [part.lower() for part in relative_posix.replace("\\", "/").split("/")]
    name = parts[-1] if parts else ""
    return (
        any(part in _SECRET_DIR_NAMES for part in parts)
        or any(
            part == ".env" or (part.startswith(".env.") and part != ".env.example")
            for part in parts
        )
        or name in _SECRET_FILE_NAMES
        or name.endswith(_SECRET_FILE_SUFFIXES)
    )


def resolve_repo_path(relative_path: str) -> Path:
    """Resolve `relative_path` against the repo root, rejecting any escape."""
    candidate = (REPO_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise SandboxViolation(f"Path escapes the repo root: {relative_path!r}") from exc
    return candidate


def to_repo_relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def check_readable(relative_path: str) -> Path:
    """Resolve and validate a path for reading. Raises SandboxViolation if forbidden."""
    resolved = resolve_repo_path(relative_path)
    rel = to_repo_relative(resolved)
    if is_secret_path(rel):
        raise SandboxViolation(f"Reading {rel!r} is not allowed (secrets file)")
    if rel == ".git" or rel.startswith(".git/"):
        raise SandboxViolation(f"Reading {rel!r} is not allowed (git internals)")
    return resolved


def check_writable(relative_path: str, *, allowed_prefixes: list[str] | None = None) -> Path:
    """Resolve and validate a path for writing. Raises SandboxViolation if forbidden.

    `allowed_prefixes`, when given, additionally restricts writes to paths
    starting with one of them (a monitored app's `allowed_write_paths`, or
    the legacy single-app `apps/target_app/` default for jobs with no
    app_id).
    """
    resolved = resolve_repo_path(relative_path)
    rel = to_repo_relative(resolved)

    if is_secret_path(rel):
        raise SandboxViolation(f"Writing to {rel!r} is not allowed (secrets file)")
    for forbidden in _FORBIDDEN_WRITE_PREFIXES:
        if rel == forbidden.rstrip("/") or rel.startswith(forbidden):
            raise SandboxViolation(f"Writing to {rel!r} is not allowed (forbidden path)")
    if allowed_prefixes is not None and not any(rel.startswith(p) for p in allowed_prefixes):
        raise SandboxViolation(
            f"Writing to {rel!r} is outside the allowed scope {allowed_prefixes!r}"
        )
    return resolved


def resolve_worktree_dir(worktree: str) -> Path:
    """Resolve a worktree directory name under WORKTREES_ROOT, sandboxed."""
    if not worktree or "/" in worktree or "\\" in worktree or worktree in (".", ".."):
        raise SandboxViolation(f"Invalid worktree name: {worktree!r}")
    resolved = (WORKTREES_ROOT / worktree).resolve()
    try:
        resolved.relative_to(WORKTREES_ROOT)
    except ValueError as exc:
        raise SandboxViolation(f"Invalid worktree name: {worktree!r}") from exc
    if not resolved.is_dir():
        raise SandboxViolation(f"Worktree does not exist: {worktree!r}")
    return resolved


def extract_diff_paths(unified_diff: str) -> set[str]:
    """Extract every file path touched by a strict unified-diff header.

    Quoted paths and names containing spaces are rejected because the current
    patch transport does not parse Git's C-quoted path format. A safe refusal
    is preferable to validating a truncated header and applying another path.
    """
    paths: set[str] = set()
    hunk_remaining: tuple[int, int] | None = None
    for line in unified_diff.splitlines():
        if line.startswith(("diff --git ", "diff --cc ")):
            hunk_remaining = None
            continue
        if hunk_remaining is not None:
            old_remaining, new_remaining = hunk_remaining
            if line.startswith(" "):
                old_remaining -= 1
                new_remaining -= 1
            elif line.startswith("-"):
                old_remaining -= 1
            elif line.startswith("+"):
                new_remaining -= 1
            elif not line.startswith("\\"):
                hunk_remaining = None
                continue
            hunk_remaining = (
                (old_remaining, new_remaining) if old_remaining > 0 or new_remaining > 0 else None
            )
            continue
        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match:
            old_remaining = int(hunk_match.group(2) or "1")
            new_remaining = int(hunk_match.group(4) or "1")
            if old_remaining or new_remaining:
                hunk_remaining = (old_remaining, new_remaining)
            continue
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        raw_path = line[4:]
        if raw_path.startswith('"'):
            raise SandboxViolation("Quoted patch paths are not supported")
        path = raw_path.split("\t", 1)[0]
        if " " in path:
            raise SandboxViolation("Patch paths containing spaces are not supported")
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path == "/dev/null":
            continue
        paths.add(path)
    return paths


def check_diff_paths_writable(
    unified_diff: str,
    *,
    allowed_prefixes: list[str] | None = None,
    workspace_root: Path | None = None,
) -> set[str]:
    """Validate every path touched by a diff against the write sandbox.

    Returns the set of touched (repo-relative) paths on success; raises
    SandboxViolation naming the first offending path otherwise.
    """
    touched = extract_diff_paths(unified_diff)
    if not touched:
        raise SandboxViolation("Diff does not touch any files")
    for path in touched:
        _validate_patch_path(path)
        check_writable(path, allowed_prefixes=allowed_prefixes)
        if workspace_root is not None:
            _reject_symlink_components(workspace_root, path)
    return touched


def _validate_patch_path(path: str) -> None:
    """Reject ambiguous, absolute, traversal, and Windows-style diff paths."""
    if not path or "\\" in path or "\x00" in path or path.startswith("/"):
        raise SandboxViolation(f"Invalid patch path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SandboxViolation(f"Patch path contains traversal: {path!r}")
    if any(":" in part or part.endswith((".", " ")) for part in parts):
        raise SandboxViolation(f"Patch path contains a Windows-ambiguous name: {path!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise SandboxViolation(f"Patch path contains control characters: {path!r}")


def _reject_symlink_components(workspace_root: Path, path: str) -> None:
    """Reject existing symlinks in a patch target's path under the worktree."""
    root = workspace_root.absolute()
    if _is_link_or_reparse_point(root):
        raise SandboxViolation("Patch workspace root must not be a symlink or junction")
    root = root.resolve()
    current = root
    for part in PurePosixPath(path).parts:
        current = current / part
        if _is_link_or_reparse_point(current):
            raise SandboxViolation(f"Patch target traverses a symlink: {path!r}")


def _is_link_or_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
