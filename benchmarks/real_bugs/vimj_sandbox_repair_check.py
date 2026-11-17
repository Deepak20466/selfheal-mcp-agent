"""Sandboxed baseline-fail and patched-pass check for the frozen VIMJ fee-balance case (V2).

What this proves, and what it does not:
- It runs the frozen regression through the evaluator's real Bubblewrap entry point, once
  against the clean baseline and once against a scratch copy with an operator-authored patch.
- The patch is a SCRIPTED FIXTURE written by the operator. It is NOT AI-generated and is not
  evidence that any model can repair the defect.
- No provider or model call is made. --live is not accepted. No database is used.
- The clean VIMJ checkout is only read. Patch application happens only in a scratch copy.

Exit status is 0 only if the baseline fails for the fee-balance assertion, the patch applies
cleanly, and the identical regression passes after the patch. Every step is recorded in a new
JSONL evidence file under benchmarks/real_bugs/results/unresolved/ (never overwritten).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.real_bugs import offline_scripted_eval as harness
from benchmarks.real_bugs import unresolved_eval as ue
from mcp_server.execution_sandbox import run_untrusted_command

VERSION = ue.FREEZE_V2
PATCH_PATH = harness.SCRIPTED_PATCH_PATH
PATCH_SHA256 = harness.EXPECTED_SCRIPTED_PATCH_SHA256
POST_PATCH_FEES_SHA256 = "068c76e3f80e6d81608b3889d4a4d47465a931c0cc432790c773d713e779b88a"
BASELINE_FAILURE_TOKENS = ("125.00", "175", "1 failed")
ENVIRONMENT_ERROR_MARKERS = (
    "ModuleNotFoundError",
    "ImportError",
    "ERROR collecting",
    "SandboxViolation",
)


def tree_digest(root: Path) -> str:
    """Digest every file under root (excluding .git) by relative path and content."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def git_status(repo: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise ue.EvaluationSafetyError("Git executable is required")
    completed = subprocess.run(  # noqa: S603 - fixed git arguments on the clean checkout
        [git, "-C", str(repo), "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout


def tail(text: str, limit: int = 8_000) -> str:
    return text[-limit:]


def run_regression(directory: Path, python: Path) -> dict[str, Any]:
    """Run the frozen regression through the real Bubblewrap entry point."""
    outcome = _run(
        run_untrusted_command(
            [str(python), "-m", "pytest", "-q", ue.TEST_PATH],
            cwd=directory,
            timeout=120,
            read_only_paths=[python.parents[1], Path(sys.base_prefix)],
        )
    )
    return {
        "passed": bool(outcome.get("passed")),
        "sandbox_unavailable": bool(outcome.get("sandbox_unavailable")),
        "output_tail": tail(str(outcome.get("output", ""))),
        "raw_keys": sorted(outcome.keys()),
    }


def _run(awaitable: Any) -> dict[str, Any]:
    import asyncio

    return asyncio.run(awaitable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True, help="clean VIMJ checkout")
    parser.add_argument(
        "--evidence", type=Path, required=True, help="new .jsonl under results/unresolved"
    )
    args = parser.parse_args(argv)

    record: dict[str, Any] = {
        "evaluation_id": VERSION.challenge_id,
        "freeze_version": VERSION.name,
        "claim_level": "sandboxed_baseline_and_operator_patch_check",
        "patch_provenance": "operator-authored scripted fixture; NOT AI-generated",
        "patch_path": str(PATCH_PATH.relative_to(ue.ROOT.parent.parent)),
        "patch_sha256_expected": PATCH_SHA256,
        "post_patch_fees_sha256_expected": POST_PATCH_FEES_SHA256,
        "provider_calls": 0,
        "live": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "criteria": {},
    }
    evidence_path: Path | None = None
    try:
        evidence_path = ue._ensure_private_evidence_path(args.evidence)
        ue._write_evidence(evidence_path, {**record, "status": "started"}, create=True)

        # 1. Frozen inputs, including the V2 freeze chain and the scripted fixture.
        record["freeze_gate"] = harness.check_frozen_inputs(
            allow_freeze_file_mismatch=False, version=VERSION
        )

        # 2. Clean checkout: read-only, pinned commit, clean status, source hash.
        repo = args.candidate_root.expanduser().resolve()
        candidate_backend = ue._validate_candidate(repo)
        record["clean_checkout"] = {
            "root": str(repo),
            "git_status_before": git_status(repo),
            "tree_digest_before": tree_digest(repo),
        }

        # 3. Baseline inside Bubblewrap via the evaluator's own preparation path.
        #    It refuses unless the failure is the expected fee-balance assertion.
        # Diagnostic capture only: record the sandbox's raw result so a stopped run keeps the
        # real cause. The wrapper delegates to the real Bubblewrap entry point and returns its
        # result unchanged; no evaluator gate or sandbox setting is altered.
        import mcp_server.execution_sandbox as sandbox_module

        real_entry_point = sandbox_module.run_untrusted_command
        record["baseline_sandbox_capture"] = {"calls": 0}

        async def capturing_entry_point(*call_args: Any, **call_kwargs: Any) -> dict[str, Any]:
            result = await real_entry_point(*call_args, **call_kwargs)
            capture = record["baseline_sandbox_capture"]
            capture["calls"] += 1
            capture["command"] = [str(part) for part in call_args[0]]
            capture["return_code"] = result.get("return_code")
            capture["passed"] = result.get("passed")
            capture["sandbox"] = result.get("sandbox")
            capture["sandbox_unavailable"] = result.get("sandbox_unavailable", False)
            capture["output_tail"] = tail(str(result.get("output", "")))
            return result

        sandbox_module.run_untrusted_command = capturing_entry_point
        try:
            staged, python, baseline_output = ue._prepare_model_workspace(
                candidate_backend, VERSION
            )
        finally:
            sandbox_module.run_untrusted_command = real_entry_point
        record["baseline"] = {
            "staged_source_sha256": ue.sha256_text_file(staged / ue.SOURCE_PATH),
            "staged_test_canonical_sha256": ue.sha256_text_file(
                staged / "tests" / "test_requested_changes.py"
            ),
            "python": str(python),
            "sandbox": "Bubblewrap via mcp_server.execution_sandbox.run_untrusted_command",
            "output_tail": tail(baseline_output),
        }
        baseline_ok = all(token in baseline_output for token in BASELINE_FAILURE_TOKENS)
        baseline_env_error = any(marker in baseline_output for marker in ENVIRONMENT_ERROR_MARKERS)
        record["criteria"]["baseline_fails_for_fee_balance_assertion"] = (
            baseline_ok and not baseline_env_error
        )

        # 4. Apply the operator-authored patch only to a scratch copy of the staged baseline.
        ue.PREPARED_ROOT.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="vimj-repair-check-", dir=ue.PREPARED_ROOT))
        patched = work / "repo"
        shutil.copytree(staged, patched, symlinks=False)
        if (patched / ".git").exists():
            raise ue.EvaluationSafetyError("scratch copy must not contain Git history")
        git = shutil.which("git")
        if git is None:
            raise ue.EvaluationSafetyError("Git executable is required")
        check = subprocess.run(  # noqa: S603 - fixed arguments, operator patch in scratch only
            [git, "apply", "--check", str(PATCH_PATH)],
            cwd=patched,
            capture_output=True,
            text=True,
            timeout=60,
        )
        applied = False
        if check.returncode == 0:
            subprocess.run(  # noqa: S603 - fixed arguments, scratch copy only
                [git, "apply", str(PATCH_PATH)], cwd=patched, check=True, timeout=60
            )
            applied = True
        fees_sha = ue.sha256_text_file(patched / ue.SOURCE_PATH)
        record["patch"] = {
            "scratch_root": str(work),
            "git_apply_check_returncode": check.returncode,
            "git_apply_check_stderr": check.stderr[-500:],
            "applied": applied,
            "changed_paths": sorted(ue.extract_paths(PATCH_PATH.read_text(encoding="utf-8"))),
            "patched_fees_sha256": fees_sha,
            "patched_test_canonical_sha256": ue.sha256_text_file(
                patched / "tests" / "test_requested_changes.py"
            ),
        }
        record["criteria"]["patch_applies_in_isolated_scratch_copy"] = applied and (
            fees_sha == POST_PATCH_FEES_SHA256
        )
        record["criteria"]["frozen_test_unchanged_by_patch"] = (
            record["patch"]["patched_test_canonical_sha256"] == ue.EXPECTED_TEST_SHA256
        )

        # 5. Identical frozen regression inside Bubblewrap against the patched scratch copy.
        if applied:
            post = run_regression(patched, python)
            record["post_patch"] = {**post, "output_tail": post["output_tail"]}
            record["criteria"]["post_patch_regression_passes_in_sandbox"] = (
                post["passed"] and not post["sandbox_unavailable"]
            )
        else:
            record["post_patch"] = {"skipped": "patch did not apply"}
            record["criteria"]["post_patch_regression_passes_in_sandbox"] = False

        # 6. The clean checkout must be unchanged.
        record["clean_checkout"]["git_status_after"] = git_status(repo)
        record["clean_checkout"]["tree_digest_after"] = tree_digest(repo)
        record["criteria"]["clean_checkout_unchanged"] = (
            record["clean_checkout"]["git_status_before"]
            == record["clean_checkout"]["git_status_after"]
            and record["clean_checkout"]["tree_digest_before"]
            == record["clean_checkout"]["tree_digest_after"]
        )

        criteria = record["criteria"]
        record["overall_pass"] = all(
            criteria.get(name, False)
            for name in (
                "baseline_fails_for_fee_balance_assertion",
                "patch_applies_in_isolated_scratch_copy",
                "post_patch_regression_passes_in_sandbox",
                "clean_checkout_unchanged",
            )
        )
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        print(json.dumps({"overall_pass": record["overall_pass"], "criteria": criteria}, indent=2))
        print(f"evidence: {evidence_path}")
        return 0 if record["overall_pass"] else 1
    except Exception as exc:  # fail closed, but record why
        from sentinel.scrubber import scrub_text

        record["status"] = "stopped"
        record["overall_pass"] = False
        record["stop_reason"] = scrub_text(f"{type(exc).__name__}: {exc}")
        record["stopped_at_utc"] = datetime.now(UTC).isoformat()
        if evidence_path is not None and evidence_path.exists():
            try:
                ue._write_evidence(evidence_path, record)
            except OSError:
                pass
        print(f"stopped: {record['stop_reason']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
