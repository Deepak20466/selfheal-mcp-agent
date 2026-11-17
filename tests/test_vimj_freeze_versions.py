"""Offline tests for the VIMJ freeze versions (V1 historical default, V2 explicit).

Nothing here runs VIMJ code, the OS sandbox, a database, or a model. The tests read
checked-in configuration files and compare full SHA-256 values. Run with:

    python -m pytest --noconftest -p no:cacheprovider tests/test_vimj_freeze_versions.py
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from benchmarks.real_bugs import offline_scripted_eval as harness
from benchmarks.real_bugs import unresolved_eval as ue

UNRESOLVED = ue.ROOT / "unresolved"
COMMITTED_FREEZE_SHA256 = "921fe894efc6a20f85185c76d4bfae77d01c65f273699564f2ece151a3ab3efc"

# V1 files as they were before the V2 change (full SHA-256 values from the pre-change manifest).
V1_CHALLENGE_SHA256 = "f586130a223d2ba4df0dd3762c10894327277a344701b90e92030308da8ab1c2"
V1_OVERLAY_SHA256 = "c77b210cef093b15370493e96daf76219f5f16c3f13224a26b955a40e70c61d4"
V1_BUDGET_SHA256 = "9d40775aeefad2f694f3412fed56db1ea1ca6fb12fc4f2e3a9c1d8a402dc1a3a"
V1_CONTEXT_SHA256 = "e4156ca62c14c76f17f6ca37aece111ae4dd01b4ee6272b18ca1d41e400e6a06"
V1_MODEL_VISIBLE_PIN = "9c9f1e6e71ce96925c0c716dd452ccec2e1254241e523632ec4226701b7555f6"
V1_FILE_SHA256 = {
    "vimj-fee-balance-v1.json": V1_CHALLENGE_SHA256,
    "vimj-fee-balance-v1-model-visible-freeze.json": COMMITTED_FREEZE_SHA256,
    "vimj-fee-balance-v1-test.patch": V1_OVERLAY_SHA256,
    "vimj-gemini-2.5-flash-budget-v1.json": V1_BUDGET_SHA256,
    "vimj-gemini-2.5-flash-context-v1.json": V1_CONTEXT_SHA256,
}
# Historical V1 pins and identities. These must never change.
V1_CONSTANTS = {
    "EXPECTED_FREEZE_SHA256": V1_CHALLENGE_SHA256,
    "EXPECTED_MODEL_VISIBLE_FREEZE_SHA256": V1_MODEL_VISIBLE_PIN,
    "EXPECTED_BUDGET_CONFIG_SHA256": V1_BUDGET_SHA256,
    "EXPECTED_CONTEXT_POLICY_SHA256": V1_CONTEXT_SHA256,
    "CHALLENGE_ID": "vimj-offline-candidate-2026-11-09",
    "BUDGET_EVALUATION_ID": "vimj-gemini-2.5-flash-budget-v1-2026-11-09",
    "EVALUATION_ID": "vimj-gemini-2.5-flash-context-v1-2026-11-09",
}
# V2 files, pinned in full.
V2_CHALLENGE_SHA256 = "b67eebaea953c1393d82961c6b8376456ed149c34852b7e35ba8ce4b000ca24f"
V2_BUDGET_SHA256 = "786952c696095481fddc40b08a1d47c80f939e1ebf4051f2ecc65895fad62818"
V2_CONTEXT_SHA256 = "7eaa23bcc4a3e7540cb89573b08e262dfc61977d2cbfc72ddbe33c3c60c5eb66"
V2_FILE_SHA256 = {
    "vimj-fee-balance-v2.json": V2_CHALLENGE_SHA256,
    "vimj-fee-balance-v2-model-visible-freeze.json": COMMITTED_FREEZE_SHA256,
    "vimj-gemini-2.5-flash-budget-v2.json": V2_BUDGET_SHA256,
    "vimj-gemini-2.5-flash-context-v2.json": V2_CONTEXT_SHA256,
}
V2_CHALLENGE_ID = "vimj-offline-candidate-2026-11-17"
V2_BUDGET_EVALUATION_ID = "vimj-gemini-2.5-flash-budget-v2-2026-11-17"
V2_CONTEXT_EVALUATION_ID = "vimj-gemini-2.5-flash-context-v2-2026-11-17"
V2_SOURCE_COMMIT = "066bcf8a2393646857d8d801f08a3770b8038d58"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(name: str) -> dict[str, Any]:
    return json.loads((UNRESOLVED / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ V1 immutability


@pytest.mark.parametrize("name", sorted(V1_FILE_SHA256))
def test_v1_files_keep_their_pre_change_sha256(name: str) -> None:
    assert _sha(UNRESOLVED / name) == V1_FILE_SHA256[name]


@pytest.mark.parametrize("constant", sorted(V1_CONSTANTS))
def test_v1_evaluator_constants_are_unchanged(constant: str) -> None:
    assert getattr(ue, constant) == V1_CONSTANTS[constant]


def test_v1_remains_the_default_and_v2_is_never_implicit() -> None:
    assert ue.DEFAULT_FREEZE_VERSION == "v1"
    assert (
        ue.build_parser().parse_args(["--candidate-root", "c", "--evidence", "e"]).freeze_version
        == "v1"
    )
    default_version = harness.check_frozen_inputs.__kwdefaults__["version"]
    assert default_version is ue.FREEZE_V1


def test_v2_requires_an_explicit_selection() -> None:
    args = ue.build_parser().parse_args(
        ["--candidate-root", "c", "--evidence", "e", "--freeze-version", "v2"]
    )
    assert args.freeze_version == "v2"
    assert ue.select_freeze_version(args.freeze_version) is ue.FREEZE_V2


def test_unknown_freeze_version_fails_closed() -> None:
    with pytest.raises(ue.EvaluationSafetyError, match="unknown freeze version"):
        ue.select_freeze_version("v3")
    with pytest.raises(SystemExit):
        ue.build_parser().parse_args(
            ["--candidate-root", "c", "--evidence", "e", "--freeze-version", "v3"]
        )


def test_registry_matches_the_choices() -> None:
    assert set(ue.FREEZE_VERSIONS) == set(ue.FREEZE_VERSION_CHOICES) == {"v1", "v2"}


def test_v1_documents_still_validate_under_v1() -> None:
    ue.validate_model_config(_load("vimj-fee-balance-v1.json"), ue.FREEZE_V1)
    ue.validate_budget_config(_load("vimj-gemini-2.5-flash-budget-v1.json"), ue.FREEZE_V1)
    ue.validate_context_policy(_load("vimj-gemini-2.5-flash-context-v1.json"), ue.FREEZE_V1)
    # The default argument is V1 as well.
    ue.validate_budget_config(_load("vimj-gemini-2.5-flash-budget-v1.json"))


def test_v1_freeze_pin_mismatch_is_preserved_not_silently_fixed() -> None:
    """The historical V1 pin does not match its file. V1 must still refuse without override."""
    assert (
        _sha(UNRESOLVED / "vimj-fee-balance-v1-model-visible-freeze.json")
        == COMMITTED_FREEZE_SHA256
    )
    assert ue.FREEZE_V1.model_visible_freeze_sha256 != COMMITTED_FREEZE_SHA256
    with pytest.raises(ue.EvaluationSafetyError, match="model-visible freeze hash mismatch"):
        harness.check_frozen_inputs(allow_freeze_file_mismatch=False, version=ue.FREEZE_V1)


# ------------------------------------------------------------------ V2 pins and cross-references


@pytest.mark.parametrize("name", sorted(V2_FILE_SHA256))
def test_v2_files_match_full_sha256_pins(name: str) -> None:
    assert _sha(UNRESOLVED / name) == V2_FILE_SHA256[name]


def test_v2_constants_match_the_files_they_pin() -> None:
    assert ue.EXPECTED_V2_CHALLENGE_SHA256 == V2_FILE_SHA256["vimj-fee-balance-v2.json"]
    assert ue.EXPECTED_V2_MODEL_VISIBLE_FREEZE_SHA256 == COMMITTED_FREEZE_SHA256
    assert (
        ue.EXPECTED_V2_BUDGET_CONFIG_SHA256
        == V2_FILE_SHA256["vimj-gemini-2.5-flash-budget-v2.json"]
    )
    assert (
        ue.EXPECTED_V2_CONTEXT_POLICY_SHA256
        == V2_FILE_SHA256["vimj-gemini-2.5-flash-context-v2.json"]
    )
    assert ue.FREEZE_V2.challenge_config_sha256 == ue.EXPECTED_V2_CHALLENGE_SHA256
    assert ue.FREEZE_V2.model_visible_freeze_sha256 == ue.EXPECTED_V2_MODEL_VISIBLE_FREEZE_SHA256
    assert ue.FREEZE_V2.budget_config_sha256 == ue.EXPECTED_V2_BUDGET_CONFIG_SHA256
    assert ue.FREEZE_V2.context_policy_sha256 == ue.EXPECTED_V2_CONTEXT_POLICY_SHA256


def test_v2_freeze_bytes_are_identical_to_v1_and_the_committed_file() -> None:
    v1 = (UNRESOLVED / "vimj-fee-balance-v1-model-visible-freeze.json").read_bytes()
    v2 = (UNRESOLVED / "vimj-fee-balance-v2-model-visible-freeze.json").read_bytes()
    assert v1 == v2
    assert hashlib.sha256(v2).hexdigest() == COMMITTED_FREEZE_SHA256


def test_v2_cross_references_chain_every_hash_and_identity() -> None:
    challenge = _load("vimj-fee-balance-v2.json")
    budget = _load("vimj-gemini-2.5-flash-budget-v2.json")
    context = _load("vimj-gemini-2.5-flash-context-v2.json")
    freeze_doc = json.loads(
        (UNRESOLVED / "vimj-fee-balance-v2-model-visible-freeze.json").read_text()
    )

    # Freeze bytes -> challenge config
    assert challenge["model_visible_freeze_path"] == "vimj-fee-balance-v2-model-visible-freeze.json"
    assert challenge["model_visible_freeze_sha256"] == _sha(
        UNRESOLVED / "vimj-fee-balance-v2-model-visible-freeze.json"
    )
    assert challenge["model_visible_freeze_source_commit"] == V2_SOURCE_COMMIT
    assert challenge["model_visible_freeze_source_evaluation_id"] == ue.CHALLENGE_ID
    assert freeze_doc["evaluation_id"] == ue.CHALLENGE_ID  # the committed bytes name their source
    # Challenge config -> budget
    assert budget["challenge_freeze_sha256"] == _sha(UNRESOLVED / "vimj-fee-balance-v2.json")
    assert budget["model_visible_freeze_sha256"] == _sha(
        UNRESOLVED / "vimj-fee-balance-v2-model-visible-freeze.json"
    )
    # Budget -> context
    assert context["budget_config_sha256"] == _sha(
        UNRESOLVED / "vimj-gemini-2.5-flash-budget-v2.json"
    )
    # Identities
    assert (
        challenge["evaluation_id"]
        == budget["challenge_id"]
        == context["challenge_id"]
        == V2_CHALLENGE_ID
    )
    assert budget["evaluation_id"] == V2_BUDGET_EVALUATION_ID
    assert context["evaluation_id"] == V2_CONTEXT_EVALUATION_ID
    # Same baseline, same caps and provider as the configs that run the benchmark
    assert challenge["baseline_commit"] == budget["baseline_commit"] == ue.EXPECTED_BASELINE
    assert challenge["test_overlay_sha256"] == ue.EXPECTED_PATCH_SHA256
    assert budget["provider"] == "gemini_api" and budget["model"] == ue.GEMINI_MODEL
    assert context["model"] == ue.GEMINI_MODEL


def test_v2_identities_are_distinct_from_v1() -> None:
    assert ue.FREEZE_V2.challenge_id != ue.FREEZE_V1.challenge_id
    assert ue.FREEZE_V2.budget_evaluation_id != ue.FREEZE_V1.budget_evaluation_id
    assert ue.FREEZE_V2.context_evaluation_id != ue.FREEZE_V1.context_evaluation_id
    assert ue.FREEZE_V2.challenge_config_path != ue.FREEZE_V1.challenge_config_path
    assert ue.FREEZE_V2.challenge_config_sha256 != ue.FREEZE_V1.challenge_config_sha256
    assert ue.FREEZE_V2.budget_config_sha256 != ue.FREEZE_V1.budget_config_sha256


def test_v2_documents_validate_under_v2() -> None:
    ue.validate_model_config(_load("vimj-fee-balance-v2.json"), ue.FREEZE_V2)
    ue.validate_budget_config(_load("vimj-gemini-2.5-flash-budget-v2.json"), ue.FREEZE_V2)
    ue.validate_context_policy(_load("vimj-gemini-2.5-flash-context-v2.json"), ue.FREEZE_V2)


def test_v2_freeze_gate_passes_without_override() -> None:
    result = harness.check_frozen_inputs(allow_freeze_file_mismatch=False, version=ue.FREEZE_V2)
    assert result["freeze_version"] == "v2"
    assert result["model_visible_freeze_file_status"] == "match"
    assert result["model_visible_freeze_file_actual_sha256"] == COMMITTED_FREEZE_SHA256


def test_v2_wrong_freeze_bytes_fail_closed(tmp_path: Path) -> None:
    altered = tmp_path / "altered-freeze.json"
    altered.write_bytes(
        (UNRESOLVED / "vimj-fee-balance-v2-model-visible-freeze.json").read_bytes() + b" "
    )
    version = dataclasses.replace(ue.FREEZE_V2, model_visible_freeze_path=altered)
    with pytest.raises(ue.EvaluationSafetyError, match="model-visible freeze hash mismatch"):
        harness.check_frozen_inputs(allow_freeze_file_mismatch=False, version=version)


def test_v2_wrong_challenge_config_fails_closed(tmp_path: Path) -> None:
    altered = tmp_path / "altered-challenge.json"
    altered.write_bytes((UNRESOLVED / "vimj-fee-balance-v2.json").read_bytes() + b"\n")
    version = dataclasses.replace(ue.FREEZE_V2, challenge_config_path=altered)
    with pytest.raises(ue.EvaluationSafetyError, match="frozen challenge config hash mismatch"):
        harness.check_frozen_inputs(allow_freeze_file_mismatch=False, version=version)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.__setitem__("baseline_commit", "0" * 40), "frozen baseline commit mismatch"),
        (
            lambda d: d.__setitem__("evaluation_id", "vimj-offline-candidate-2026-11-09"),
            "frozen challenge identity mismatch",
        ),
        (lambda d: d.__setitem__("provider", "groq_api"), "frozen provider/model mismatch"),
        (
            lambda d: d.__setitem__(
                "model_visible_freeze_sha256",
                "9c9f1e6e71ce96925c0c716dd452ccec2e1254241e523632ec4226701b7555f6",
            ),
            "model-visible freeze hash mismatch",
        ),
        (
            lambda d: d.__setitem__("model_visible_freeze_source_commit", "0" * 40),
            "model-visible freeze source commit mismatch",
        ),
        (
            lambda d: d.__setitem__("permitted_source_paths", ["app/routers/other.py"]),
            "frozen source allowlist mismatch",
        ),
    ],
)
def test_v2_challenge_mutations_are_rejected(mutation: Any, message: str) -> None:
    document = copy.deepcopy(_load("vimj-fee-balance-v2.json"))
    mutation(document)
    with pytest.raises(ue.EvaluationSafetyError, match=message):
        ue.validate_model_config(document, ue.FREEZE_V2)


def test_v2_budget_and_context_mutations_are_rejected() -> None:
    budget = copy.deepcopy(_load("vimj-gemini-2.5-flash-budget-v2.json"))
    budget["per_case_token_cap"] = 99_999
    with pytest.raises(ue.EvaluationSafetyError, match="budget differs"):
        ue.validate_budget_config(budget, ue.FREEZE_V2)
    context = copy.deepcopy(_load("vimj-gemini-2.5-flash-context-v2.json"))
    context["budget_config_sha256"] = ue.EXPECTED_BUDGET_CONFIG_SHA256  # a V1 budget hash
    with pytest.raises(ue.EvaluationSafetyError, match="context policy differs"):
        ue.validate_context_policy(context, ue.FREEZE_V2)


# ------------------------------------------------------------------ cross-version isolation


def test_v1_and_v2_documents_do_not_validate_under_the_other_version() -> None:
    with pytest.raises(ue.EvaluationSafetyError):
        ue.validate_model_config(_load("vimj-fee-balance-v2.json"), ue.FREEZE_V1)
    with pytest.raises(ue.EvaluationSafetyError):
        ue.validate_model_config(_load("vimj-fee-balance-v1.json"), ue.FREEZE_V2)
    with pytest.raises(ue.EvaluationSafetyError):
        ue.validate_budget_config(_load("vimj-gemini-2.5-flash-budget-v2.json"), ue.FREEZE_V1)
    with pytest.raises(ue.EvaluationSafetyError):
        ue.validate_budget_config(_load("vimj-gemini-2.5-flash-budget-v1.json"), ue.FREEZE_V2)
    with pytest.raises(ue.EvaluationSafetyError):
        ue.validate_context_policy(_load("vimj-gemini-2.5-flash-context-v2.json"), ue.FREEZE_V1)
    with pytest.raises(ue.EvaluationSafetyError):
        ue.validate_context_policy(_load("vimj-gemini-2.5-flash-context-v1.json"), ue.FREEZE_V2)


def test_no_v2_identity_leaks_into_v1_files() -> None:
    for name in (
        "vimj-fee-balance-v1.json",
        "vimj-gemini-2.5-flash-budget-v1.json",
        "vimj-gemini-2.5-flash-context-v1.json",
    ):
        text = (UNRESOLVED / name).read_text(encoding="utf-8")
        assert V2_CHALLENGE_ID not in text
        assert V2_BUDGET_EVALUATION_ID not in text
        assert V2_CONTEXT_EVALUATION_ID not in text
        assert "b67eebae" not in text and "786952c6" not in text and "7eaa23bc" not in text
