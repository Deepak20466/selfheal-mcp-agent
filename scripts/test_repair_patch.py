"""Apply a validated repair and test it in a disposable offline sandbox."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.execution_sandbox import run_untrusted_command  # noqa: E402
from scripts.repair_patch_policy import PatchRejected, read_patch_file, validate_patch  # noqa: E402

_UNITTEST_SUMMARY = re.compile(r"^Ran ([0-9]+) tests? in [0-9.]+s$", re.MULTILINE)


def _unittest_count(output: str) -> int | None:
    """Return the final stdlib unittest summary count, if its runner completed."""
    summaries = _UNITTEST_SUMMARY.findall(output)
    return int(summaries[-1]) if summaries else None


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")


def _append_github_output(path: Path, digest: str, retryable: bool) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"patch_sha256={digest}\n")
        output.write(f"retryable={str(retryable).lower()}\n")


async def test_patch(repository: Path, patch_file: Path, report_path: Path) -> int:
    try:
        patch = read_patch_file(patch_file)
        digest = validate_patch(patch, repository)
    except (OSError, PatchRejected) as exc:
        _write_report(
            report_path,
            {"passed": False, "retryable": False, "reason": "patch_rejected", "output": str(exc)},
        )
        return 1

    try:
        applied = await asyncio.to_thread(
            subprocess.run,
            ["git", "apply"],
            cwd=repository,
            input=patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _write_report(
            report_path,
            {
                "passed": False,
                "retryable": False,
                "reason": "patch_apply_failed",
                "output": type(exc).__name__,
            },
        )
        return 1
    if applied.returncode:
        _write_report(
            report_path,
            {
                "passed": False,
                "retryable": False,
                "reason": "patch_apply_failed",
                "output": applied.stdout.decode("utf-8", errors="replace")[-4000:],
            },
        )
        return 1

    tests = repository / "tests"
    if not tests.is_dir() or tests.is_symlink():
        _write_report(
            report_path,
            {
                "passed": False,
                "retryable": False,
                "reason": "unsupported_test_layout",
                "output": "supported scope requires a regular tests/ directory with unittest tests",
            },
        )
        return 1

    result = await run_untrusted_command(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=repository,
        timeout=120,
        read_only_workspace_paths=("tests",),
    )
    output = str(result.get("output", ""))
    test_count = _unittest_count(output)
    passed = bool(result.get("passed")) and test_count is not None and test_count > 0
    sandbox_available = not bool(result.get("sandbox_unavailable"))
    completed = (
        sandbox_available
        and not bool(result.get("timed_out"))
        and not bool(result.get("resource_limited"))
        and isinstance(result.get("return_code"), int)
        and test_count is not None
        and test_count > 0
    )
    report = {
        "passed": passed,
        "retryable": bool(completed and not passed),
        "reason": "passed"
        if passed
        else "regression_failed"
        if completed
        else "no_tests_discovered"
        if sandbox_available and test_count == 0
        else "sandbox_or_resource_failure",
        "tests_run": test_count,
        "patch_sha256": digest,
        "sandbox": result.get("sandbox"),
        "timed_out": bool(result.get("timed_out")),
        "resource_limited": bool(result.get("resource_limited")),
        "sandbox_unavailable": bool(result.get("sandbox_unavailable")),
        "return_code": result.get("return_code"),
        "output": output[-20_000:],
    }
    _write_report(report_path, report)
    if os.environ.get("GITHUB_OUTPUT"):
        await asyncio.to_thread(
            _append_github_output,
            Path(os.environ["GITHUB_OUTPUT"]),
            digest,
            bool(report["retryable"]),
        )
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(test_patch(args.repository, args.patch, args.report))


if __name__ == "__main__":
    raise SystemExit(main())
