"""Sandboxed check of the coach-delete data-loss regression tests (Candidate A, validation only).

Runs tests/test_coach_delete_guard.py through the Bubblewrap entry point against one backend tree
(the clean baseline, or later a repaired tree) and records every test's outcome.

Modes:
  baseline  master tree + new tests: the 8 blocked-record tests must FAIL (204 instead of 409);
            the 2 unblocked tests must PASS; the whole backend tests/ directory must fail
            only those 8 tests.
  repaired  the fix branch tree: all 10 coach-delete tests and the whole directory pass.

The runner only reads the tree and never edits it. Results go to a NEW evidence file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.real_bugs import unresolved_eval as ue
from benchmarks.real_bugs import vimj_sandbox_repair_check as sc
from benchmarks.real_bugs.vimj_ai_repair_check import run_pytest

TEST_FILE = "tests/test_coach_delete_guard.py"
CLASS = "CoachDeleteGuardTests"
BLOCKED_TESTS = {
    "test_coach_with_sessions_and_attendance_cannot_be_deleted",
    "test_coach_with_sessions_only_cannot_be_deleted",
    "test_coach_with_salary_only_cannot_be_deleted",
    "test_coach_with_fee_receipt_only_cannot_be_deleted",
    "test_coach_with_reminder_draft_only_cannot_be_deleted",
    "test_coach_with_facility_attendance_only_cannot_be_deleted",
    "test_coach_with_leave_only_cannot_be_deleted",
    "test_coach_with_activity_assignment_only_cannot_be_deleted",
}
UNBLOCKED_TESTS = {
    "test_coach_without_associated_records_can_still_be_deleted",
    "test_deactivated_coach_keeps_their_records",
}
# mode -> (tests expected to FAIL, tests expected to PASS)
EXPECTED = {
    "baseline": (BLOCKED_TESTS, UNBLOCKED_TESTS),
    "repaired": (set(), BLOCKED_TESTS | UNBLOCKED_TESTS),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend", type=Path, required=True, help="backend tree to check (read-only)"
    )
    parser.add_argument("--mode", choices=["baseline", "repaired"], required=True)
    parser.add_argument(
        "--repo", type=Path, required=True, help="clean clone that must stay unchanged"
    )
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)

    python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
    record: dict[str, Any] = {
        "claim_level": "coach_delete_data_loss_regression_check",
        "backend_tree": str(args.backend),
        "test_file_sha256": _sha(args.backend / TEST_FILE),
        "provider_calls": 0,
        "live": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence_path = ue._ensure_private_evidence_path(args.evidence)
    ue._write_evidence(evidence_path, {**record, "status": "started"}, create=True)
    try:
        before = sc.git_status(args.repo), sc.tree_digest(args.repo)
        whole_file = run_pytest(args.backend, python, TEST_FILE)
        per_test: dict[str, Any] = {}
        expect_fail, expect_pass = EXPECTED[args.mode]
        whole_dir = run_pytest(args.backend, python, "tests")
        for name in sorted(expect_fail | expect_pass):
            node = f"{TEST_FILE}::{CLASS}::{name}"
            result = run_pytest(args.backend, python, node)
            per_test[name] = {
                "passed": bool(result["passed"]),
                "summary": result["summary"],
                "sandbox": result["sandbox"],
                "sandbox_unavailable": result["sandbox_unavailable"],
                "output_tail": result["output_tail"][-1200:],
            }
        after = sc.git_status(args.repo), sc.tree_digest(args.repo)

        failed = {n for n, r in per_test.items() if not r["passed"]}
        passed = {n for n, r in per_test.items() if r["passed"]}
        record["mode"] = args.mode
        record["whole_tests_dir"] = {k: v for k, v in whole_dir.items() if k != "output_tail"}
        record["per_test"] = per_test
        record["whole_test_file"] = {k: v for k, v in whole_file.items() if k != "output_tail"}
        record["criteria"] = {
            "per_test_outcomes_match_expectation": failed == expect_fail and passed == expect_pass,
            "whole_tests_dir_matches_expectation": (
                # pytest reports node ids; compare the bare test names the expectation uses.
                {node.rsplit("::", 1)[-1] for node in whole_dir["failed_tests"]} == expect_fail
                if args.mode == "baseline"
                else bool(whole_dir["passed"])
            ),
            "ran_inside_bubblewrap": all(
                r["sandbox"] == "bubblewrap" and not r["sandbox_unavailable"]
                for r in per_test.values()
            ),
            "repo_checkout_unchanged": before == after,
        }
        record["overall_matches_expectation"] = all(record["criteria"].values())
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        print(json.dumps(record["criteria"], indent=1))
        for name, result in per_test.items():
            print(f"{'PASS' if result['passed'] else 'FAIL'}  {name}  ({result['summary']})")
        print(f"evidence: {evidence_path}")
        return 0 if record["overall_matches_expectation"] else 1
    except Exception as exc:  # fail closed, but record why
        from sentinel.scrubber import scrub_text

        record["status"] = "stopped"
        record["stop_reason"] = scrub_text(f"{type(exc).__name__}: {exc}")
        ue._write_evidence(evidence_path, record)
        print(f"stopped: {record['stop_reason']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
