"""Validate the password-reset repair against a moving VIMJ master tip (validation only).

The frozen case is pinned to the old base commit. `master` has since changed the same test file,
so the frozen overlay no longer applies there. This runner checks the repair on the current
master snapshot with a test overlay that is ported from the frozen one (same added test body)
and labelled as such. It never modifies master, the clean checkouts or any pin.

Inside Bubblewrap it runs, for the baseline and for the repaired copy:
  - the ported regression test by node id, and
  - the whole tests/ directory.
It requires the baseline to fail only the regression test, for the TypeError, and the repaired
copy to pass everything. Results are written to a NEW evidence file (never overwritten).
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

from benchmarks.real_bugs import unresolved_eval as ue
from benchmarks.real_bugs import vimj_sandbox_repair_check as sc
from benchmarks.real_bugs.vimj_ai_repair_check import run_pytest

TEST_FILE = "tests/test_auth_login.py"
TEST_ID = (
    "tests/test_auth_login.py::AuthLoginTests::test_password_reset_accepts_a_valid_database_token"
)
SOURCE = "app/routers/auth.py"
FAILURE_TOKENS = ("TypeError", "offset-naive and offset-aware")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git() -> str:
    git = shutil.which("git")
    if git is None:
        raise ue.EvaluationSafetyError("Git executable is required")
    return git


def _stage(backend: Path, test_file: Path, repair: Path | None, scratch: Path) -> Path:
    tree = scratch / "backend"
    shutil.copytree(backend, tree, symlinks=False)
    (tree / TEST_FILE).write_bytes(test_file.read_bytes())
    if repair is not None:
        subprocess.run(  # noqa: S603 - fixed args, scratch copy only
            [_git(), "apply", "--recount", "--ignore-whitespace", str(repair)],
            cwd=tree,
            check=True,
            timeout=60,
        )
    if (tree / ".git").exists():
        raise ue.EvaluationSafetyError("staged tree must not contain Git history")
    return tree


def _run_both(tree: Path, python: Path) -> dict[str, Any]:
    frozen = run_pytest(tree, python, TEST_ID)
    whole = run_pytest(tree, python, "tests")
    return {"frozen": frozen, "whole": whole}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-backend", type=Path, required=True)
    parser.add_argument("--test-file", type=Path, required=True)
    parser.add_argument("--repair", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True, help="VIMJ clone to check is untouched")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)

    python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
    record: dict[str, Any] = {
        "claim_level": "master_tip_validation_ported_test",
        "provenance": (
            "validation only; frozen pin not used on master; overlay ported from the frozen one"
        ),
        "test_id": TEST_ID,
        "master_source_sha256": _sha(args.master_backend / SOURCE),
        "ported_test_sha256": _sha(args.test_file),
        "repair_patch_sha256": _sha(args.repair),
        "provider_calls": 0,
        "live": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence_path = ue._ensure_private_evidence_path(args.evidence)
    ue._write_evidence(evidence_path, {**record, "status": "started"}, create=True)
    try:
        before = sc.git_status(args.repo), sc.tree_digest(args.repo)
        ue.PREPARED_ROOT.mkdir(parents=True, exist_ok=True)
        scratch_root = Path(tempfile.mkdtemp(prefix="vimj-master-tip-", dir=ue.PREPARED_ROOT))
        try:
            base_tree = _stage(args.master_backend, args.test_file, None, scratch_root / "base")
            base = _run_both(base_tree, python)
            fixed_tree = _stage(
                args.master_backend, args.test_file, args.repair, scratch_root / "fixed"
            )
            fixed = _run_both(fixed_tree, python)
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)
        after = sc.git_status(args.repo), sc.tree_digest(args.repo)

        base_fail_set = set(base["whole"]["failed_tests"])
        out_b = base["frozen"]["output_tail"]
        record["baseline"] = {
            k: {x: v[x] for x in v if x != "output_tail"} for k, v in base.items()
        }
        record["baseline_frozen_output_tail"] = out_b[-2500:]
        record["repaired"] = {
            k: {x: v[x] for x in v if x != "output_tail"} for k, v in fixed.items()
        }
        record["criteria"] = {
            "baseline_frozen_fails_for_pinned_reason": (
                not base["frozen"]["passed"]
                and all(token in out_b for token in FAILURE_TOKENS)
                and base["frozen"]["sandbox"] == "bubblewrap"
            ),
            "baseline_only_regression_test_fails": base_fail_set == {TEST_ID},
            "repaired_frozen_test_passes": bool(fixed["frozen"]["passed"]),
            "repaired_whole_tests_dir_passes": bool(fixed["whole"]["passed"]),
            "ran_inside_bubblewrap": all(
                r["sandbox"] == "bubblewrap" and not r["sandbox_unavailable"]
                for r in (*base.values(), *fixed.values())
            ),
            "repo_checkout_unchanged": before == after,
        }
        record["overall_pass"] = all(record["criteria"].values())
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        print(json.dumps({"overall_pass": record["overall_pass"], **record["criteria"]}, indent=1))
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
