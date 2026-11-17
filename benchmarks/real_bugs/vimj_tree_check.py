"""Sandboxed whole-suite check of one VIMJ backend tree (read-only validation).

Runs the backend tests/ directory through the Bubblewrap entry point against an exact tree
(for example a merged master tip), records the counts, and requires every test to pass. Nothing is
modified; the clone is checked for no change. Results go to a NEW evidence file.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.real_bugs import unresolved_eval as ue
from benchmarks.real_bugs import vimj_sandbox_repair_check as sc
from benchmarks.real_bugs.vimj_ai_repair_check import run_pytest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--expected-passed", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
    record: dict[str, Any] = {
        "claim_level": "tree_whole_suite_sandboxed_check",
        "tree": str(args.backend),
        "expected_passed": args.expected_passed,
        "provider_calls": 0,
        "live": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence_path = ue._ensure_private_evidence_path(args.evidence)
    ue._write_evidence(evidence_path, {**record, "status": "started"}, create=True)
    try:
        before = sc.git_status(args.repo), sc.tree_digest(args.repo)
        result = run_pytest(args.backend, python, "tests")
        after = sc.git_status(args.repo), sc.tree_digest(args.repo)
        record["result"] = {k: v for k, v in result.items() if k != "output_tail"}
        record["criteria"] = {
            "all_tests_pass": bool(result["passed"]),
            "count_matches_expected": result["summary"].startswith(
                f"{args.expected_passed} passed"
            ),
            "ran_inside_bubblewrap": result["sandbox"] == "bubblewrap"
            and not result["sandbox_unavailable"],
            "repo_checkout_unchanged": before == after,
        }
        record["overall_pass"] = all(record["criteria"].values())
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        print(json.dumps({"overall_pass": record["overall_pass"], **record["criteria"]}, indent=1))
        print(f"summary: {result['summary']}")
        print(f"evidence: {evidence_path}")
        return 0 if record["overall_pass"] else 1
    except Exception as exc:  # fail closed, but record why
        from sentinel.scrubber import scrub_text

        record["status"] = "stopped"
        record["overall_pass"] = False
        record["stop_reason"] = scrub_text(f"{type(exc).__name__}: {exc}")
        ue._write_evidence(evidence_path, record)
        print(f"stopped: {record['stop_reason']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
