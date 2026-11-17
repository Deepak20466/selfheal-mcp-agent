"""Sandboxed multi-case, multi-patch verifier for the frozen VIMJ repair cases.

For one case it stages the clean baseline plus the frozen test overlay, proves the baseline fails
for the pinned reason, and then evaluates each candidate patch from a manifest in its own scratch
copy: allowlist check, `git apply`, frozen test, and the WHOLE tests/ directory compared with the
baseline's failing set. Every command runs through the real Bubblewrap entry point; nothing is
executed natively. No provider or model call is made here, and the clean checkout is only read.

Manifest: a JSON list of objects with keys label (str), patch (path), recount (bool),
provenance (str), claim_level (str) and optional generation_record (path).
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
from benchmarks.real_bugs.vimj_cases import BASELINE_COMMIT, Case, get_case

PLACEHOLDER = "PIN_AFTER_BASELINE_OBSERVATION"


def _git() -> str:
    git = shutil.which("git")
    if git is None:
        raise ue.EvaluationSafetyError("Git executable is required")
    return git


def validate_checkout(repo: Path, case: Case) -> Path:
    """Read-only: pinned commit, clean status, pinned source hash. Returns backend/."""
    git = _git()
    head = subprocess.run(  # noqa: S603 - fixed args, read-only
        [git, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != BASELINE_COMMIT:
        raise ue.EvaluationSafetyError(f"checkout is not the pinned baseline: {head}")
    if sc.git_status(repo).strip():
        raise ue.EvaluationSafetyError("candidate checkout must be clean")
    backend = repo / "backend"
    source = backend / case.source_path
    if not source.is_file() or ue.sha256_text_file(source) != case.source_sha256:
        raise ue.EvaluationSafetyError("pinned source file hash mismatch")
    return backend


def stage_baseline(backend: Path, case: Case, destination: Path) -> Path:
    """Copy the clean backend and add only the frozen regression test overlay."""
    if ue.sha256_file(case.overlay_path) != case.overlay_sha256:
        raise ue.EvaluationSafetyError("test overlay hash mismatch")
    shutil.copytree(backend, destination, ignore=ue._copy_ignore, symlinks=False)
    subprocess.run(  # noqa: S603 - fixed args, scratch copy only
        [
            _git(),
            "apply",
            "--recount",
            "--ignore-whitespace",
            "--whitespace=nowarn",
            str(case.overlay_path),
        ],
        cwd=destination,
        check=True,
        timeout=30,
    )
    if (destination / ".git").exists():
        raise ue.EvaluationSafetyError("staged workspace must not contain Git history")
    return destination


def _record(label: str, **fields: Any) -> dict[str, Any]:
    return {"label": label, **fields}


def evaluate_patch(
    entry: dict[str, Any], staged: Path, python: Path, case: Case, baseline_failed: set[str]
) -> dict[str, Any]:
    patch_path = Path(entry["patch"]).resolve()
    flags = ["--recount", "--ignore-whitespace"] if entry.get("recount") else []
    result: dict[str, Any] = {
        "label": entry["label"],
        "provenance": entry.get("provenance", ""),
        "claim_level": entry.get("claim_level", ""),
        "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        "apply_flags": flags,
        "criteria": {},
    }
    if entry.get("generation_record"):
        generation = json.loads(Path(entry["generation_record"]).read_text(encoding="utf-8"))
        result["generation_record"] = generation
    patch_text = patch_path.read_text(encoding="utf-8")
    try:
        paths = ue.extract_paths(patch_text) if patch_text.strip() else set()
    except Exception as exc:  # malformed model output is a failed attempt, not a harness error
        paths = set()
        result["path_parse_error"] = f"{type(exc).__name__}: {exc}"
    result["patch_paths"] = sorted(paths)
    result["criteria"]["patch_touches_only_pinned_source"] = paths == {case.source_path}
    if not result["criteria"]["patch_touches_only_pinned_source"]:
        result["overall_pass"] = False
        return result

    work = Path(tempfile.mkdtemp(prefix="vimj-bench-", dir=ue.PREPARED_ROOT))
    scratch = work / "repo"
    shutil.copytree(staged, scratch, symlinks=False)
    check = subprocess.run(  # noqa: S603 - fixed args, scratch copy only
        [_git(), "apply", "--check", *flags, str(patch_path)],
        cwd=scratch,
        capture_output=True,
        text=True,
        timeout=60,
    )
    applied = False
    if check.returncode == 0:
        subprocess.run(  # noqa: S603 - fixed args, scratch copy only
            [_git(), "apply", *flags, str(patch_path)], cwd=scratch, check=True, timeout=60
        )
        applied = True
    result["apply"] = {
        "check_returncode": check.returncode,
        "check_stderr": check.stderr[-400:],
        "applied": applied,
    }
    result["criteria"]["patch_applies_in_isolated_scratch_copy"] = applied
    if not applied:
        result["overall_pass"] = False
        shutil.rmtree(work, ignore_errors=True)
        return result

    result["patched_source_sha256"] = ue.sha256_text_file(scratch / case.source_path)
    result["criteria"]["frozen_test_unchanged_by_patch"] = (
        ue.sha256_text_file(scratch / case.test_file) == case.test_canonical_sha256
    )
    frozen = run_pytest(scratch, python, case.test_id)
    whole = run_pytest(scratch, python, "tests")
    result["post_patch_frozen_test"] = frozen
    result["post_patch_whole_tests_dir"] = whole
    sandboxed = not (frozen["sandbox_unavailable"] or whole["sandbox_unavailable"])
    result["criteria"]["ran_inside_bubblewrap"] = sandboxed and frozen["sandbox"] == "bubblewrap"
    result["criteria"]["post_patch_frozen_test_passes"] = bool(frozen["passed"]) and sandboxed
    # The baseline's only failure is the frozen test, so after a correct fix the whole directory
    # must pass outright; this also catches collection errors, which have no FAILED line.
    result["criteria"]["whole_tests_dir_passes"] = (
        bool(whole["passed"]) and set(whole["failed_tests"]).issubset(baseline_failed) and sandboxed
    )
    result["overall_pass"] = all(result["criteria"].values())
    shutil.rmtree(work, ignore_errors=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args(argv)
    case = get_case(args.case)

    record: dict[str, Any] = {
        "case_id": case.case_id,
        "claim_level": "sandboxed_multi_patch_verification",
        "baseline_commit": BASELINE_COMMIT,
        "provider_calls_in_verifier": 0,
        "live": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence_path: Path | None = None
    try:
        python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
        ue.PREPARED_ROOT.mkdir(parents=True, exist_ok=True)
        repo = args.candidate_root.expanduser().resolve()
        if not args.observe_only:
            if args.evidence is None or args.manifest is None:
                raise ue.EvaluationSafetyError("--evidence and --manifest are required")
            if PLACEHOLDER in (case.test_canonical_sha256, case.failure_text):
                raise ue.EvaluationSafetyError("case pins are not final")
            evidence_path = ue._ensure_private_evidence_path(args.evidence)
            ue._write_evidence(evidence_path, {**record, "status": "started"}, create=True)

        backend = validate_checkout(repo, case)
        before = {"git_status": sc.git_status(repo), "tree_digest": sc.tree_digest(repo)}
        base_work = Path(tempfile.mkdtemp(prefix="vimj-bench-base-", dir=ue.PREPARED_ROOT))
        staged = stage_baseline(backend, case, base_work / "repo")
        canonical = ue.sha256_text_file(staged / case.test_file)
        record["staged_test_canonical_sha256"] = canonical
        frozen = run_pytest(staged, python, case.test_id)
        whole = run_pytest(staged, python, "tests")
        record["baseline_frozen_test"] = frozen
        record["baseline_whole_tests_dir"] = {k: v for k, v in whole.items() if k != "output_tail"}
        if args.observe_only:
            print(json.dumps({"canonical": canonical, **record}, indent=1, default=str))
            print("--- baseline frozen output tail ---")
            print(frozen["output_tail"][-3500:])
            return 0

        out = frozen["output_tail"]
        env_error = any(marker in out for marker in sc.ENVIRONMENT_ERROR_MARKERS)
        baseline_failed = set(whole["failed_tests"])
        record["criteria"] = {
            "baseline_fails_for_pinned_reason": (
                not frozen["passed"]
                and all(token in out for token in case.failure_tokens)
                and not env_error
                and not frozen["sandbox_unavailable"]
            ),
            "staged_test_matches_pin": canonical == case.test_canonical_sha256,
            "baseline_only_frozen_test_fails": baseline_failed == {case.test_id},
        }
        ue._write_evidence(evidence_path, {**record, "status": "baseline"})

        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        results = []
        for entry in manifest:
            result = evaluate_patch(entry, staged, python, case, baseline_failed)
            results.append(result)
            ue._write_evidence(
                evidence_path, {"case_id": case.case_id, "status": "patch", **result}
            )

        after = {"git_status": sc.git_status(repo), "tree_digest": sc.tree_digest(repo)}
        record["clean_checkout"] = {"before": before, "after": after}
        record["criteria"]["clean_checkout_unchanged"] = before == after
        record["summary"] = [
            {"label": r["label"], "claim_level": r.get("claim_level"), "pass": r["overall_pass"]}
            for r in results
        ]
        record["baseline_valid"] = all(record["criteria"].values())
        record["status"] = "completed"
        record["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, record)
        shutil.rmtree(base_work, ignore_errors=True)
        print(json.dumps({"baseline_valid": record["baseline_valid"], **record["criteria"]}))
        for item in record["summary"]:
            print(f"  {item['label']}: {'PASS' if item['pass'] else 'FAIL'}")
        print(f"evidence: {evidence_path}")
        return 0 if record["baseline_valid"] else 1
    except Exception as exc:  # fail closed, but record why
        from sentinel.scrubber import scrub_text

        record["status"] = "stopped"
        record["stop_reason"] = scrub_text(f"{type(exc).__name__}: {exc}")
        if evidence_path is not None and evidence_path.exists():
            try:
                ue._write_evidence(evidence_path, record)
            except OSError:
                pass
        print(f"stopped: {record['stop_reason']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
