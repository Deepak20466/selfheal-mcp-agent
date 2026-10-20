"""Validation shared by the OpenRouter repair workflow's trusted jobs.

This module only parses data. It never imports or executes the target repository.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAX_PATCH_BYTES = 64 * 1024
MAX_PATCH_FILES = 3
MAX_CHANGED_LINES = 200
_FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".github",
        "alembic",
        "benchmarks",
        "deploy",
        "docs",
        "migrations",
        "scripts",
        "test",
        "tests",
        "__tests__",
    }
)
_ALLOWED_SOURCE_ROOTS = frozenset(
    {
        "app",
        "apps",
        "cli",
        "core",
        "healer",
        "lib",
        "mcp_server",
        "sentinel",
        "src",
        "service",
        "server",
    }
)
_SECRET_CONTENT = re.compile(
    r"(?i)(?:sk-or-v1-[a-z0-9]{20,}|gh[pousr]_[a-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:OPENROUTER_API_KEY|GITHUB_TOKEN|HEALER_WEBHOOK_SECRET)\s*[:=]|"
    r"\b(?:OPENROUTER_API_KEY|GITHUB_TOKEN|HEALER_WEBHOOK_SECRET)\b|"
    r"\b(?:os\.system|subprocess\.|socket\.|urllib\.|requests\.)|"
    r"\b(?:eval|exec|compile)\s*\()"
)
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


class PatchRejected(ValueError):
    """Raised when a generated or transferred patch violates policy."""


def patch_sha256(patch: bytes) -> str:
    return hashlib.sha256(patch).hexdigest()


def read_patch_file(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise PatchRejected("patch artifact is missing") from exc
    if not stat.S_ISREG(mode):
        raise PatchRejected("patch artifact must be a regular file, not a link")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PatchRejected("patch artifact could not be read") from exc


def _safe_source_path(value: str) -> PurePosixPath:
    if "\\" in value or "\x00" in value:
        raise PatchRejected("patch path contains an unsupported character")
    if not _DIFF_PATH.fullmatch(value):
        raise PatchRejected("patch path uses unsupported characters")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PatchRejected("patch path is not a safe repository-relative path")
    lowered = {part.lower() for part in path.parts}
    if lowered & _FORBIDDEN_PARTS:
        raise PatchRejected("patch path targets a protected directory")
    if path.suffix.lower() != ".py":
        raise PatchRejected("only Python source files are currently allowed")
    filename = path.name.lower()
    if filename.startswith("test") or filename.endswith("_test.py") or filename == "conftest.py":
        raise PatchRejected("test files cannot be changed by a repair patch")
    if len(path.parts) > 1 and path.parts[0].lower() not in _ALLOWED_SOURCE_ROOTS:
        raise PatchRejected("patch path is outside the configured Python implementation roots")
    return path


def _verify_regular_existing_file(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise PatchRejected("patch may change existing source files only") from exc
        if current.is_symlink():
            raise PatchRejected("patch path traverses a symbolic link")
        if current != root / Path(*relative.parts) and not current.is_dir():
            raise PatchRejected("patch path contains a non-directory component")
        if current == root / Path(*relative.parts) and not current.is_file():
            raise PatchRejected("patch target must be an existing regular file")
        if not mode:
            raise PatchRejected("patch target has an invalid file mode")
    return root / Path(*relative.parts)


def _safe_apply_diagnostic(output: bytes, paths: list[PurePosixPath], root: Path) -> str:
    """Expose only a validated source path and numeric Git apply location."""
    allowed = {path.as_posix() for path in paths}
    text = output.decode("utf-8", errors="replace")
    patterns = (
        re.compile(r"^error: patch failed: ([A-Za-z0-9_./-]+):(\d+)$"),
        re.compile(r"^error: ([A-Za-z0-9_./-]+): patch does not apply$"),
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        for pattern in patterns:
            match = pattern.fullmatch(line)
            if match and match.group(1) in allowed:
                if match.lastindex == 2:
                    number = int(match.group(2))
                    source = root / Path(*PurePosixPath(match.group(1)).parts)
                    try:
                        if source.stat().st_size > 2 * 1024 * 1024:
                            path = match.group(1)
                            return f"git apply --check rejected {path} at an unspecified line"
                        with source.open(encoding="utf-8") as handle:
                            line_count = sum(1 for _ in handle)
                    except (OSError, UnicodeError):
                        line_count = 0
                    if 1 <= number <= line_count:
                        return f"git apply --check rejected {match.group(1)} at line {number}"
                    return f"git apply --check rejected {match.group(1)} at an unspecified line"
                return f"git apply --check rejected {match.group(1)}"
        corrupt = re.fullmatch(r"error: corrupt patch at line (\d+)", line)
        if corrupt:
            line_number = int(corrupt.group(1))
            return f"git apply --check rejected a malformed hunk at patch line {line_number}"
    return "git apply --check rejected the hunk; diagnostic omitted"


def validate_patch(patch: bytes, repository: Path, *, check_apply: bool = True) -> str:
    """Validate a small source-only unified diff and optionally run ``git apply --check``."""
    if not patch or len(patch) > MAX_PATCH_BYTES:
        raise PatchRejected("patch is empty or exceeds the 64 KiB limit")
    if b"\x00" in patch:
        raise PatchRejected("binary patches are not allowed")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchRejected("patch must be UTF-8 text") from exc
    if "\r" in text:
        raise PatchRejected("patch must use LF line endings")

    paths: list[PurePosixPath] = []
    changed_lines = 0
    added_lines: list[str] = []
    current_path: PurePosixPath | None = None
    saw_old_header = False
    saw_new_header = False
    hunk_started = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current_path is not None and not (saw_old_header and saw_new_header):
                raise PatchRejected("each patch file must have matching old and new path headers")
            match = _DIFF_HEADER.fullmatch(line)
            if not match:
                raise PatchRejected("patch contains a malformed diff header")
            old_path = _safe_source_path(match.group(1))
            new_path = _safe_source_path(match.group(2))
            if old_path != new_path:
                raise PatchRejected("renames and copies are not allowed")
            paths.append(old_path)
            current_path = old_path
            saw_old_header = False
            saw_new_header = False
            hunk_started = False
        elif line.startswith(
            ("new file mode ", "deleted file mode ", "old mode ", "new mode ", "rename ", "copy ")
        ):
            raise PatchRejected("file creation, deletion, rename, and mode changes are not allowed")
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            raise PatchRejected("binary patches are not allowed")
        elif line.startswith("--- ") and not hunk_started:
            old_header = line[4:].split("\t", 1)[0]
            if current_path is None or saw_old_header or not old_header.startswith("a/"):
                raise PatchRejected("patch has an unsafe or unexpected old file header")
            if _safe_source_path(old_header[2:]) != current_path:
                raise PatchRejected("diff header and old file path do not match")
            saw_old_header = True
        elif line.startswith("+++ ") and not hunk_started:
            new_header = line[4:].split("\t", 1)[0]
            if (
                current_path is None
                or not saw_old_header
                or saw_new_header
                or not new_header.startswith("b/")
            ):
                raise PatchRejected("patch has an unsafe or unexpected new file header")
            if _safe_source_path(new_header[2:]) != current_path:
                raise PatchRejected("diff header and new file path do not match")
            saw_new_header = True
        elif line.startswith("@@"):
            hunk_started = True
        elif hunk_started and line.startswith("+"):
            changed_lines += 1
            added_lines.append(line[1:])
        elif hunk_started and line.startswith("-"):
            changed_lines += 1

    if current_path is not None and not (saw_old_header and saw_new_header):
        raise PatchRejected("each patch file must have matching old and new path headers")
    if not paths or len(paths) > MAX_PATCH_FILES or len(set(paths)) != len(paths):
        raise PatchRejected("patch must modify one to three distinct source files")
    if changed_lines > MAX_CHANGED_LINES:
        raise PatchRejected("patch exceeds the 200 changed-line limit")
    if any(_SECRET_CONTENT.search(line) for line in added_lines):
        raise PatchRejected("patch appears to introduce a credential or secret reference")

    root = repository.resolve(strict=True)
    for relative in paths:
        _verify_regular_existing_file(root, relative)
    if check_apply:
        try:
            result = subprocess.run(
                ["git", "apply", "--check"],
                cwd=root,
                input=patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PatchRejected("git could not validate patch applicability") from exc
        if result.returncode:
            diagnostic = _safe_apply_diagnostic(result.stdout, paths, root)
            raise PatchRejected(
                "patch does not apply cleanly to the selected commit; " + diagnostic
            )
    return patch_sha256(patch)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        patch = read_patch_file(args.patch)
        digest = validate_patch(patch, args.repository)
        if args.expected_sha256 and not hmac.compare_digest(digest, args.expected_sha256):
            raise PatchRejected("transferred patch digest differs from the tested artifact")
        if args.apply:
            subprocess.run(["git", "apply"], cwd=args.repository, input=patch, check=True)
    except (OSError, subprocess.SubprocessError, PatchRejected) as exc:
        print(f"patch rejected: {exc}", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
