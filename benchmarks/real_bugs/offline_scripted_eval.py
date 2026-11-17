"""Offline, scripted-provider run of the frozen VIMJ fee-balance case.

This is a DIAGNOSTIC pipeline check. It is not a model-generated result and not an
official evaluation record.

- The provider is a fixed script (ScriptedProvider). Its repair text is the operator-authored
  fixture unresolved/vimj-offline-scripted-fee-balance-v1.patch, written after reading
  backend/app/routers/fees.py. No language model produces it.
- It runs the real evaluator path: candidate validation, sandboxed baseline failure, isolated
  staging, the MCP read/propose/test loop in a private worktree, and the full-backend gate.
- The model-visible freeze FILE hash pinned in the evaluator (9c9f1e6e...) does not match the
  file on disk. Every structural check still runs. The mismatch is recorded as an explicit
  override and marks the record official=false.
- An independent check re-applies the recorded patch to a fresh copy of the candidate and
  reruns the frozen regression, outside the arm's own worktree.
- No provider or network model call is made. The candidate checkout is only read.
"""

from __future__ import annotations

import argparse
import asyncio
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
from benchmarks.real_bugs.claude_cli_provider import Block, Response, Usage

PROVIDER_LABEL = "scripted_offline_mock"
MODEL_LABEL = "scripted-fixture-v1"
EVALUATION_ID = "vimj-offline-scripted-fee-balance-v1-2026-11-17"
SCRIPTED_PATCH_PATH = ue.ROOT / "unresolved" / "vimj-offline-scripted-fee-balance-v1.patch"
EXPECTED_SCRIPTED_PATCH_SHA256 = "33882294faf5d508e1ff00dc1acbb637487774c2ba25f92656a5df07a760d7ee"
EXPECTED_MODEL_VISIBLE_FREEZE_FILE_SHA256 = ue.EXPECTED_MODEL_VISIBLE_FREEZE_SHA256
FREEZE_FILE_OVERRIDE_FLAG = "--diagnostic-allow-freeze-file-hash-mismatch"


class ScriptedProvider:
    """Replays a fixed sequence of replies. Every reply is labelled as scripted."""

    def __init__(self, replies: list[list[Block]]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    @property
    def messages(self) -> ScriptedProvider:
        return self

    async def create(self, **kwargs: Any) -> Response:
        offered = [str(tool["name"]) for tool in kwargs.get("tools", [])]
        approx_input = len(json.dumps(kwargs.get("messages", []), default=str)) // 4
        if not self._replies:
            raise RuntimeError("scripted replies exhausted")
        content = self._replies.pop(0)
        approx_output = len(json.dumps([vars(block) for block in content], default=str)) // 4
        self.requests.append(
            {
                "index": len(self.requests) + 1,
                "offered_tools": offered,
                "reply_blocks": [
                    {"type": block.type, "name": block.name, "id": block.id} for block in content
                ],
                "approx_input_tokens": approx_input,
                "approx_output_tokens": approx_output,
            }
        )
        return Response(
            content=content,
            usage=Usage(input_tokens=approx_input, output_tokens=approx_output, cost_usd=None),
        )


def scripted_replies(patch_text: str) -> list[list[Block]]:
    return [
        [Block(type="tool_use", id="scripted_1", name="read_file", input={"path": ue.SOURCE_PATH})],
        [
            Block(
                type="tool_use",
                id="scripted_2",
                name="run_tests",
                input={"test_path": ue.TEST_PATH},
            )
        ],
        [
            Block(
                type="tool_use",
                id="scripted_3",
                name="propose_patch",
                input={"unified_diff": patch_text},
            )
        ],
        [
            Block(
                type="tool_use",
                id="scripted_4",
                name="run_tests",
                input={"test_path": ue.TEST_PATH},
            )
        ],
        [
            Block(
                type="text",
                text=(
                    "Scripted fixture reply: applied the balance-sync fixture "
                    "and reran the frozen regression."
                ),
            )
        ],
    ]


def check_frozen_inputs(
    *, allow_freeze_file_mismatch: bool, version: ue.FreezeVersion = ue.FREEZE_V1
) -> dict[str, Any]:
    """Run every evaluator gate except the (overridable) model-visible freeze file hash."""
    document = json.loads(version.challenge_config_path.read_text(encoding="utf-8"))
    ue.validate_model_config(document, version)
    budget = json.loads(version.budget_config_path.read_text(encoding="utf-8"))
    ue.validate_budget_config(budget, version)
    if ue.sha256_file(version.budget_config_path) != version.budget_config_sha256:
        raise ue.EvaluationSafetyError("versioned benchmark budget hash mismatch")
    context = json.loads(version.context_policy_path.read_text(encoding="utf-8"))
    ue.validate_context_policy(context, version)
    if ue.sha256_file(version.context_policy_path) != version.context_policy_sha256:
        raise ue.EvaluationSafetyError("versioned context policy hash mismatch")

    actual_freeze = ue.sha256_file(version.model_visible_freeze_path)
    freeze_matches = actual_freeze == version.model_visible_freeze_sha256
    if not freeze_matches and not allow_freeze_file_mismatch:
        raise ue.EvaluationSafetyError(
            "operator-provided model-visible freeze hash mismatch; "
            f"pass {FREEZE_FILE_OVERRIDE_FLAG} for a non-official diagnostic run"
        )
    if ue.sha256_file(version.challenge_config_path) != version.challenge_config_sha256:
        raise ue.EvaluationSafetyError("frozen challenge config hash mismatch")
    if ue.sha256_file(ue.PATCH_PATH) != ue.EXPECTED_PATCH_SHA256:
        raise ue.EvaluationSafetyError("regression overlay hash check failed")
    if ue.sha256_file(SCRIPTED_PATCH_PATH) != EXPECTED_SCRIPTED_PATCH_SHA256:
        raise ue.EvaluationSafetyError("scripted fixture patch hash mismatch")
    return {
        "freeze_version": version.name,
        "structural_checks": "passed",
        "model_visible_freeze_file_expected_sha256": version.model_visible_freeze_sha256,
        "model_visible_freeze_file_actual_sha256": actual_freeze,
        "model_visible_freeze_file_status": "match" if freeze_matches else "mismatch_overridden",
    }


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise ue.EvaluationSafetyError("Git executable is required")
    return git


def independently_verify_patch(
    candidate_backend: Path, patch_text: str, python: Path
) -> dict[str, Any]:
    """Re-apply the recorded patch to a fresh copy and rerun the frozen regression."""
    from mcp_server.execution_sandbox import run_untrusted_command

    ue.PREPARED_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="independent-verify-", dir=ue.PREPARED_ROOT))
    try:
        repo = work / "repo"
        shutil.copytree(candidate_backend, repo, ignore=ue._copy_ignore, symlinks=False)
        overlay = work / "overlay.patch"
        overlay.write_bytes(ue.PATCH_PATH.read_bytes())
        model_patch = work / "model.patch"
        model_patch.write_text(patch_text, encoding="utf-8", newline="\n")
        git = _git_executable()
        apply_args = [git, "apply", "--recount", "--ignore-whitespace", "--whitespace=nowarn"]
        # Fixed git arguments operating on the local scratch copy only; no repo code runs here.
        subprocess.run([*apply_args, str(overlay)], cwd=repo, check=True, timeout=60)  # noqa: S603
        canonical = ue.sha256_text_file(repo / "tests" / "test_requested_changes.py")
        if canonical != ue.EXPECTED_TEST_SHA256:
            raise ue.EvaluationSafetyError("independent copy: frozen test hash mismatch")
        check = subprocess.run(  # noqa: S603
            [git, "apply", "--check", str(model_patch)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        applied = False
        if check.returncode == 0:
            subprocess.run(  # noqa: S603
                [git, "apply", str(model_patch)], cwd=repo, check=True, timeout=60
            )
            applied = True
        changed = sorted(ue.extract_paths(patch_text))
        result: dict[str, Any] = {
            "patch_applies_cleanly": applied,
            "git_apply_check_stderr_tail": check.stderr[-500:],
            "changed_paths": changed,
        }
        if applied:
            outcome = asyncio.run(
                run_untrusted_command(
                    [str(python), "-m", "pytest", "-q", ue.TEST_PATH],
                    cwd=repo,
                    timeout=120,
                    read_only_paths=[python.parents[1], Path(sys.base_prefix)],
                )
            )
            if outcome.get("sandbox_unavailable"):
                raise ue.EvaluationSafetyError("OS test sandbox unavailable for independent check")
            result["post_patch_regression_passed"] = bool(outcome.get("passed"))
            result["post_patch_regression_output_tail"] = str(outcome.get("output", ""))[-1500:]
            result["post_patch_fees_py_sha256"] = ue.sha256_text_file(repo / ue.SOURCE_PATH)
        else:
            result["post_patch_regression_passed"] = None
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(FREEZE_FILE_OVERRIDE_FLAG, dest="allow_freeze", action="store_true")
    parser.add_argument(
        "--freeze-version",
        choices=ue.FREEZE_VERSION_CHOICES,
        default=ue.DEFAULT_FREEZE_VERSION,
        help="freeze to validate against; v1 is the historical default, v2 is explicit",
    )
    args = parser.parse_args(argv)

    # Database identity must be fixed before any module that reads settings is imported.
    from benchmarks.real_bugs import run as runner

    evidence_path: Path | None = None
    base: dict[str, Any] = {}
    try:
        version = ue.select_freeze_version(args.freeze_version)
        gates = check_frozen_inputs(allow_freeze_file_mismatch=args.allow_freeze, version=version)
        candidate_backend = ue._validate_candidate(args.candidate_root)
        evidence_path = ue._ensure_private_evidence_path(args.evidence)
        patch_text = SCRIPTED_PATCH_PATH.read_text(encoding="utf-8")
        base = {
            "freeze_version": version.name,
            "evaluation_id": EVALUATION_ID,
            "challenge_id": version.challenge_id,
            "official": False,
            "claim_level": "offline_pipeline_diagnostic",
            "output_source": "scripted_fixture_not_model_generated",
            "provider": PROVIDER_LABEL,
            "model": MODEL_LABEL,
            "provider_calls": 0,
            "baseline_commit": ue.EXPECTED_BASELINE,
            "freeze_sha256": version.challenge_config_sha256,
            "regression_test_command": f"python -m pytest -q {ue.TEST_PATH}",
            "regression_test_sha256": ue.EXPECTED_FROZEN_TEST_RAW_SHA256,
            "regression_test_canonical_sha256": ue.EXPECTED_TEST_SHA256,
            "permitted_source_paths": [ue.SOURCE_PATH],
            "scripted_patch_sha256": EXPECTED_SCRIPTED_PATCH_SHA256,
            "freeze_gate": gates,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "status": "baseline_verification",
        }
        ue._write_evidence(evidence_path, base, create=True)
        staged, python, failure_output = ue._prepare_model_workspace(candidate_backend, version)
        base.update(
            status="baseline_failed_as_expected",
            baseline_failure_output=failure_output[-4000:],
            staged_source_sha256=ue.sha256_text_file(staged / ue.SOURCE_PATH),
            failing_test_canonical_sha256=ue.sha256_text_file(
                staged / "tests" / "test_requested_changes.py"
            ),
        )
        ue._write_evidence(evidence_path, base)

        # Database identity is checked before any migration or arm execution.
        database_url = runner._configure_database()
        runner._verify_database_connection(database_url)
        runner._migrate_database()

        provider = ScriptedProvider(scripted_replies(patch_text))
        base.update(status="scripted_attempt_start")
        ue._write_evidence(evidence_path, base)
        result = asyncio.run(
            ue._execute(
                staged,
                python,
                provider,
                MODEL_LABEL,
                True,
                provider_name=PROVIDER_LABEL,
            )
        )
        base.update(
            status="completed",
            scripted_requests=provider.requests,
            arm_result=result,
        )
        model_patch = result.get("model_patch")
        if isinstance(model_patch, str) and model_patch.strip():
            base["recorded_patch_sha256"] = hashlib.sha256(model_patch.encode("utf-8")).hexdigest()
            base["independent_verification"] = independently_verify_patch(
                candidate_backend, model_patch, python
            )
        else:
            base["independent_verification"] = {"skipped": "arm recorded no patch"}
        base["completed_at_utc"] = datetime.now(UTC).isoformat()
        ue._write_evidence(evidence_path, base)
        print(f"Recorded diagnostic run: {evidence_path}")
        return 0
    except Exception as exc:
        from sentinel.scrubber import scrub_text

        if evidence_path is not None and evidence_path.exists():
            base.update(
                status="stopped",
                stop_reason=scrub_text(f"{type(exc).__name__}: {exc}"),
                stopped_at_utc=datetime.now(UTC).isoformat(),
            )
            try:
                ue._write_evidence(evidence_path, base)
            except OSError:
                pass
        print(f"Evaluation stopped safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
