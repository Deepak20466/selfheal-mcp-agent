from __future__ import annotations

import asyncio

import pytest

from mcp_server.execution_sandbox import SandboxUnavailableError
from scripts import benchmark_fastapi, run_ai_benchmark
from scripts.benchmark_fastapi import _select_recorded_run, evaluate_attempts, load_scenarios
from scripts.run_ai_benchmark import _scrub_patch_text, _validate_and_apply_patch


def test_fixture_validation_reports_unavailable_sandbox_as_skip(monkeypatch) -> None:
    async def unavailable(_cwd):
        raise SandboxUnavailableError("host denied Bubblewrap namespace creation")

    monkeypatch.setattr(benchmark_fastapi, "_run_regressions", unavailable)
    report = asyncio.run(benchmark_fastapi.validate_fixtures())

    assert report["fixture_validation"] == "SKIPPED"
    assert "host denied" in report["skip_reason"]


def test_benchmark_contains_twenty_unique_contract_scenarios() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 20
    assert len({scenario["id"] for scenario in scenarios}) == 20
    assert len({scenario["root_cause"] for scenario in scenarios}) == 20


def test_evaluator_calculates_metrics_from_explicit_attempt_records() -> None:
    scenario = load_scenarios()[0]
    report = evaluate_attempts(
        {
            "attempts": [
                {
                    "scenario_id": scenario["id"],
                    "diagnosis": scenario["root_cause"],
                    "repair_seconds": 2.5,
                    "regression_tests_passed": True,
                    "scope_guard_passed": True,
                    "false_fix": False,
                    "manual_intervention": False,
                }
            ]
        }
    )
    assert report["metrics"] == {
        "diagnosis_accuracy": 1.0,
        "successful_verified_fixes": 1,
        "verified_fix_rate": 1.0,
        "average_repair_seconds": 2.5,
        "false_fixes": 0,
        "false_fix_rate": 0.0,
        "manual_interventions": 0,
        "manual_intervention_rate": 0.0,
        "average_end_to_end_seconds": 2.5,
        "reported_cost_usd": None,
        "cost_reported_attempts": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "token_reported_attempts": 0,
    }


def test_evaluator_rejects_duplicate_scenario_attempts() -> None:
    attempt = {
        "scenario_id": load_scenarios()[0]["id"],
        "diagnosis": "wrong",
        "repair_seconds": 1,
        "regression_tests_passed": False,
        "scope_guard_passed": True,
        "false_fix": True,
        "manual_intervention": True,
    }
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_attempts({"attempts": [attempt, attempt]})


def test_run_selection_never_combines_overlapping_partial_runs(tmp_path) -> None:
    scenarios = load_scenarios()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.touch()
    second.touch()
    first_attempts = [
        {"scenario_id": scenarios[index]["id"], "status": "FAIL"} for index in range(4)
    ]
    second_attempts = [
        {"scenario_id": scenarios[index]["id"], "status": "FAIL"} for index in range(3)
    ]
    second_attempts.append({"scenario_id": scenarios[3]["id"], "status": "SKIPPED"})

    attempts, selection = _select_recorded_run(
        [
            (first, {"attempts": first_attempts}),
            (second, {"attempts": second_attempts}),
        ],
        scenario_count=len(scenarios),
    )

    assert selection == "best-covered single partial actual AI run"
    assert attempts == first_attempts
    assert len({attempt["scenario_id"] for attempt in attempts}) == 4


def test_run_selection_prefers_complete_run_and_keeps_skipped_rows(tmp_path) -> None:
    scenarios = load_scenarios()
    partial = tmp_path / "partial.json"
    complete = tmp_path / "complete.json"
    partial.touch()
    complete.touch()
    complete_attempts = [
        {"scenario_id": scenario["id"], "status": "PASS"} for scenario in scenarios
    ]
    complete_attempts.append({"scenario_id": "S99", "status": "SKIPPED"})

    attempts, selection = _select_recorded_run(
        [
            (partial, {"attempts": [{"scenario_id": "S01", "status": "FAIL"}]}),
            (complete, {"attempts": complete_attempts}),
        ],
        scenario_count=len(scenarios),
    )

    assert selection == "latest complete actual AI run"
    assert attempts == complete_attempts


def test_evaluator_sums_reported_token_usage() -> None:
    scenario = load_scenarios()[0]
    report = evaluate_attempts(
        {
            "attempts": [
                {
                    "scenario_id": scenario["id"],
                    "diagnosis": scenario["root_cause"],
                    "repair_seconds": 2,
                    "regression_tests_passed": True,
                    "scope_guard_passed": True,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cached_input_tokens": 80,
                    "cost_usd": None,
                }
            ]
        }
    )

    assert report["metrics"]["input_tokens"] == 120
    assert report["metrics"]["output_tokens"] == 30
    assert report["metrics"]["cached_input_tokens"] == 80
    assert report["metrics"]["token_reported_attempts"] == 1


def test_patch_application_preserves_unified_diff_line_endings(tmp_path) -> None:
    (tmp_path / "app.py").write_bytes(b"def value():\n    return 1\n")
    patch = (
        "diff --git a/app.py b/app.py\r\n"
        "--- a/app.py\r\n"
        "+++ b/app.py\r\n"
        "@@ -1,2 +1,2 @@\r\n"
        " def value():\r\n"
        "-    return 1\r\n"
        "+    return 2\r\n"
    )

    applied, paths, message = _validate_and_apply_patch(tmp_path, patch)

    assert applied, message
    assert paths == ["app.py"]
    assert (tmp_path / "app.py").read_bytes() == b"def value():\n    return 2\n"


def test_patch_application_recounts_hunk_lines_and_commits_atomically(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_bytes(b"def value():\n    return 1\n")
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,8 +1,9 @@\n"
        " def value():\n"
        "-    return 1\n"
        "+    return 2\n"
    )

    applied, paths, message = _validate_and_apply_patch(tmp_path, patch)

    assert applied, message
    assert paths == ["app.py"]
    assert target.read_bytes() == b"def value():\n    return 2\n"


def test_patch_application_rejects_non_target_without_changing_source(tmp_path) -> None:
    target = tmp_path / "app.py"
    original = "def value():\n    return 1\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        "diff --git a/../outside.py b/../outside.py\n"
        "--- a/../outside.py\n"
        "+++ b/../outside.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    applied, paths, message = _validate_and_apply_patch(tmp_path, patch)

    assert not applied
    assert "app.py only" in message
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == original
    assert paths == ["../outside.py"]


def test_patch_application_rejects_zero_context_hunks_without_changing_source(tmp_path) -> None:
    target = tmp_path / "app.py"
    original = b"def value():\n    return 1\n"
    target.write_bytes(original)
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -2 +2 @@\n"
        "-    return 1\n"
        "+    return 2\n"
    )

    applied, _, message = _validate_and_apply_patch(tmp_path, patch)

    assert not applied
    assert "must include context lines" in message
    assert target.read_bytes() == original


def test_patch_scrubbing_preserves_python_diff_markers_and_redacts_email() -> None:
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-@app.post('/old')\n"
        "+@app.post('/new')\n"
        "+OWNER = 'person@example.invalid'\n"
    )

    scrubbed = _scrub_patch_text(patch)

    assert "-@app.post('/old')" in scrubbed
    assert "+@app.post('/new')" in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "person@example.invalid" not in scrubbed


def test_report_writer_retries_transient_windows_replace_lock(tmp_path, monkeypatch) -> None:
    output = tmp_path / "benchmark.json"
    real_replace = run_ai_benchmark.os.replace
    replace_calls = 0

    def replace_with_one_transient_lock(source, target) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise PermissionError("temporary sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(run_ai_benchmark.os, "replace", replace_with_one_transient_lock)

    run_ai_benchmark._write_results(output, {"status": "complete"})

    assert replace_calls == 2
    assert output.read_text(encoding="utf-8") == '{\n  "status": "complete"\n}\n'


def test_retry_usage_accumulates_tokens_without_treating_unknown_cost_as_zero() -> None:
    attempt = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_input_tokens": 10,
        "turn_count": 1,
        "cost_usd": None,
        "cost_status": "not_reported_by_codex_cli",
    }

    run_ai_benchmark._accumulate_retry_usage(
        attempt,
        {
            "input_tokens": 50,
            "output_tokens": 10,
            "cached_input_tokens": 5,
            "turn_count": 1,
            "cost_usd": 0.002,
        },
    )

    assert attempt["input_tokens"] == 150
    assert attempt["output_tokens"] == 30
    assert attempt["cached_input_tokens"] == 15
    assert attempt["turn_count"] == 2
    assert attempt["cost_usd"] is None
    assert attempt["cost_status"] == "not_reported_by_codex_cli"
