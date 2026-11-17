"""Sandboxed check of the exact password-reset PR tree against the current master (validation only).

Runs the whole backend tests/ directory through the Bubblewrap entry point for:
  - the base: origin/master exactly as it is (bug present), and
  - the PR tree: the PR commit's backend (repair + regression test), copied as-is.
The base must fail only the ported regression test, with the naive-timestamp TypeError. The PR tree
must pass every test. Nothing is applied here: both trees are read from `git archive` output, and
the clone is checked for no change. Results go to a NEW evidence file.
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

PORTED_TEST = (
    "tests/test_auth_login.py::AuthLoginTests::test_password_reset_accepts_a_valid_database_token"
)
TYPE_ERROR = "offset-naive and offset-aware"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(tree: Path, python: Path) -> dict[str, Any]:
    whole = run_pytest(tree, python, "tests")
    ported = run_pytest(tree, python, PORTED_TEST)
    return {
        "whole": {k: v for k, v in whole.items() if k != "output_tail"},
        "ported": {k: v for k, v in ported.items() if k != "output_tail"},
        "ported_output_tail": ported["output_tail"][-1500:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-backend", type=Path, required=True)
    parser.add_argument("--pr-backend", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)

    python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
    record: dict[str, Any] = {
        "claim_level": "pr_tree_sandboxed_check",
        "pr_auth_sha256": _sha(args.pr_backend / "app" / "routers" / "auth.py"),
        "pr_test_auth_login_sha256": _sha(args.pr_backend / "tests" / "test_auth_login.py"),
        "base_auth_sha256": _sha(args.base_backend / "app" / "routers" / "auth.py"),
        "provider_calls": 0,
        "live": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence_path = ue._ensure_private_evidence_path(args.evidence)
    ue._write_evidence(evidence_path, {**record, "status": "started"}, create=True)
    try:
        before = sc.git_status(args.repo), sc.tree_digest(args.repo)
        base = _run(args.base_backend, python)
        pr = _run(args.pr_backend, python)
        after = sc.git_status(args.repo), sc.tree_digest(args.repo)
        record["base"] = base
        record["pr"] = pr
        base_failed = set(base["whole"]["failed_tests"])
        record["criteria"] = {
            "base_only_ported_regression_test_fails": base_failed == {PORTED_TEST},
            "base_failure_is_the_naive_timestamp_typeerror": (
                not base["ported"]["passed"] and TYPE_ERROR in base["ported_output_tail"]
            ),
            "pr_whole_tests_dir_passes": bool(pr["whole"]["passed"]),
            "pr_ported_regression_test_passes": bool(pr["ported"]["passed"]),
            "ran_inside_bubblewrap": all(
                r[k]["sandbox"] == "bubblewrap" and not r[k]["sandbox_unavailable"]
                for r in (base, pr)
                for k in ("whole", "ported")
            ),
            "repo_checkout_unchanged": before == after,
        }
        record["overall_pass"] = all(record["criteria"].values())
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        print(json.dumps({"overall_pass": record["overall_pass"], **record["criteria"]}, indent=1))
        print(f"base whole: {base['whole']['summary']} failed={sorted(base_failed)}")
        print(f"PR whole:   {pr['whole']['summary']}")
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
