"""Sandboxed check of the password-reset guard tests, with mutation checks (validation only).

Runs the three guard tests in tests/test_password_reset_guards.py through the real Bubblewrap
entry point against:
  - the baseline (current master behaviour, with the naive-timestamp bug),
  - the repaired copy (upstream repair applied to a scratch copy),
  - two mutants of the repaired copy, each with ONE guard removed in scratch:
      M1 used-token check removed,
      M2 missing-user check removed.
A guard test is only meaningful if it fails on its mutant. All edits happen in scratch copies.
No provider calls, no master change, no push. Results go to a NEW evidence file.
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

GUARD_FILE = "tests/test_password_reset_guards.py"
GUARD_TESTS = (
    "tests/test_password_reset_guards.py::PasswordResetGuardTests::test_used_reset_token_is_rejected_and_password_unchanged",
    "tests/test_password_reset_guards.py::PasswordResetGuardTests::test_reset_token_cannot_be_reused_after_success",
    "tests/test_password_reset_guards.py::PasswordResetGuardTests::test_reset_token_for_missing_user_is_rejected",
)
USED_CHECK_OLD = "    if not entry or entry.used:\n"
USED_CHECK_NEW = "    if not entry:\n"
USER_CHECK_OLD = (
    "    user = db.query(User).filter(User.id == entry.user_id).first()\n    if not user:\n"
)
USER_CHECK_NEW = (
    "    user = db.query(User).filter(User.id == entry.user_id).first()\n    if False:\n"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mutate(src: Path, dst: Path, old: str, new: str) -> None:
    shutil.copytree(src, dst, symlinks=False)
    target = dst / "app" / "routers" / "auth.py"
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ue.EvaluationSafetyError(f"mutation anchor not found exactly once: {old!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def _apply(src: Path, dst: Path, patch: Path) -> None:
    shutil.copytree(src, dst, symlinks=False)
    git = shutil.which("git")
    if git is None:
        raise ue.EvaluationSafetyError("Git executable is required")
    subprocess.run(  # noqa: S603 - fixed args, scratch copy only
        [git, "apply", "--recount", "--ignore-whitespace", str(patch)],
        cwd=dst,
        check=True,
        timeout=60,
    )


def _variant(tree: Path, python: Path) -> dict[str, Any]:
    guard = run_pytest(tree, python, GUARD_FILE)
    whole = run_pytest(tree, python, "tests")
    per_test = {}
    for node in GUARD_TESTS:
        result = run_pytest(tree, python, node)
        per_test[node.rsplit("::", 1)[1]] = {
            "passed": result["passed"],
            "sandbox": result["sandbox"],
            "sandbox_unavailable": result["sandbox_unavailable"],
            "summary": result["summary"],
        }
    return {
        "guard_file": {k: v for k, v in guard.items() if k != "output_tail"},
        "whole_tests_dir": {k: v for k, v in whole.items() if k != "output_tail"},
        "per_test": per_test,
        "guard_output_tail": guard["output_tail"][-2000:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="master + guard tests, scratch")
    parser.add_argument("--repair", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True, help="clone that must stay unchanged")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)

    python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
    record: dict[str, Any] = {
        "claim_level": "guard_tests_sandboxed_with_mutation_checks",
        "guard_file": GUARD_FILE,
        "backend_sha256_guard_test": _sha(args.backend / GUARD_FILE),
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
        work = Path(tempfile.mkdtemp(prefix="vimj-guard-", dir=ue.PREPARED_ROOT))
        try:
            base = work / "baseline"
            shutil.copytree(args.backend, base, symlinks=False)
            repaired = work / "repaired"
            _apply(args.backend, repaired, args.repair)
            m1 = work / "m1_no_used_check"
            _mutate(repaired, m1, USED_CHECK_OLD, USED_CHECK_NEW)
            m2 = work / "m2_no_user_check"
            _mutate(repaired, m2, USER_CHECK_OLD, USER_CHECK_NEW)
            variants = {
                "baseline": _variant(base, python),
                "repaired": _variant(repaired, python),
                "mutant_m1_no_used_check": _variant(m1, python),
                "mutant_m2_no_user_check": _variant(m2, python),
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)
        after = sc.git_status(args.repo), sc.tree_digest(args.repo)

        def passed(name: str) -> dict[str, bool]:
            return {k: v["passed"] for k, v in variants[name]["per_test"].items()}

        rep = passed("repaired")
        m1r = passed("mutant_m1_no_used_check")
        m2r = passed("mutant_m2_no_user_check")
        ran_sandbox = all(
            v["guard_file"]["sandbox"] == "bubblewrap"
            and not v["guard_file"]["sandbox_unavailable"]
            for v in variants.values()
        )
        record["variants"] = variants
        record["criteria"] = {
            "repaired_all_guard_tests_pass": all(rep.values()),
            "repaired_whole_tests_dir_passes": bool(
                variants["repaired"]["whole_tests_dir"]["passed"]
            ),
            "m1_used_check_removed_fails_used_and_reuse_tests": (
                not m1r["test_used_reset_token_is_rejected_and_password_unchanged"]
                and not m1r["test_reset_token_cannot_be_reused_after_success"]
            ),
            "m2_user_check_removed_fails_missing_user_test": (
                not m2r["test_reset_token_for_missing_user_is_rejected"]
            ),
            "ran_inside_bubblewrap": ran_sandbox,
            "repo_checkout_unchanged": before == after,
        }
        record["overall_pass"] = all(record["criteria"].values())
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        print(json.dumps({"overall_pass": record["overall_pass"], **record["criteria"]}, indent=1))
        for name, result in variants.items():
            print(
                f"{name}: "
                + ", ".join(
                    f"{k}={'PASS' if v['passed'] else 'FAIL'}"
                    for k, v in result["per_test"].items()
                )
            )
            print(f"    whole tests dir: {result['whole_tests_dir']['summary']}")
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
