"""Validate independent FastAPI failure fixtures and evaluate real attempts.

`validate` checks that all intentional regressions fail and all reference
repairs pass in a temporary local Git repository. `evaluate` scores a JSON
file produced by actual repair attempts; it never manufactures attempt data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from core.process_env import sanitized_subprocess_env
from mcp_server.execution_sandbox import SandboxUnavailableError, run_untrusted_command
from sentinel.scrubber import scrub_text

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "benchmarks" / "fastapi_failure_repo"
SCENARIOS_PATH = FIXTURE / "scenarios.json"
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"


def load_scenarios() -> list[dict[str, str]]:
    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    if len(scenarios) < 20:
        raise ValueError("benchmark requires at least 20 scenarios")
    return scenarios


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=sanitized_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _test_counts(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for count, status in re.findall(r"(\d+)\s+(passed|failed|errors?|skipped)\b", output):
        key = "failed" if status in {"failed", "error", "errors"} else status
        counts[key] += int(count)
    return counts


async def _run_regressions(cwd: Path) -> tuple[dict[str, int], int]:
    result = await run_untrusted_command(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--disable-warnings",
            "test_scenarios.py",
        ],
        cwd=cwd,
        timeout=120,
    )
    if result.get("sandbox_unavailable"):
        raise SandboxUnavailableError(
            "benchmark fixture validation requires the OS sandbox: "
            + scrub_text(str(result.get("output", ""))[-1500:])
        )
    return _test_counts(str(result.get("output", ""))), int(result.get("return_code", 125))


def _select_recorded_run(
    result_documents: list[tuple[Path, dict[str, Any]]], scenario_count: int
) -> tuple[list[dict[str, Any]], str]:
    """Select one run document; partial runs must never be merged together."""
    if not result_documents:
        return [], "no actual AI run found"

    def rank(item: tuple[Path, dict[str, Any]]) -> tuple[bool, int, int, str, float]:
        path, document = item
        attempts = document.get("attempts", [])
        terminal = [
            attempt
            for attempt in attempts
            if isinstance(attempt, dict) and attempt.get("status") in {"PASS", "FAIL"}
        ]
        scenario_ids = {attempt.get("scenario_id") for attempt in terminal}
        coverage = len(scenario_ids)
        complete = coverage == scenario_count and len(terminal) == scenario_count
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (
            complete,
            coverage,
            len(terminal),
            str(document.get("finished_at", "")),
            modified,
        )

    selected_path, selected_document = max(result_documents, key=rank)
    attempts = selected_document.get("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
    selected_rank = rank((selected_path, selected_document))
    if selected_rank[0]:
        selection = "latest complete actual AI run"
    else:
        selection = "best-covered single partial actual AI run"
    return attempts, selection


async def validate_fixtures() -> dict[str, Any]:
    scenarios = load_scenarios()
    with tempfile.TemporaryDirectory(prefix="selfheal-fastapi-benchmark-") as temp:
        target = Path(temp) / "target"
        shutil.copytree(FIXTURE, target)

        # Local-only baseline commit gives the fixture a real independent Git
        # worktree; no remote is configured and no push can occur.
        commands = [
            ["git", "init", "--quiet"],
            ["git", "config", "user.name", "Selfheal Benchmark"],
            ["git", "config", "user.email", "benchmark@example.invalid"],
            ["git", "add", "-A"],
            ["git", "commit", "--quiet", "-m", "broken benchmark baseline"],
        ]
        for command in commands:
            result = _run(command, cwd=target)
            if result.returncode:
                raise RuntimeError(f"local git setup failed: {scrub_text(result.stderr[-1000:])}")

        try:
            broken, broken_exit = await _run_regressions(target)
            shutil.copy2(target / "app_fixed.py", target / "app.py")
            changed = _run(["git", "status", "--porcelain"], cwd=target)
            changed_paths = [
                line[3:].strip() for line in changed.stdout.splitlines() if line.strip()
            ]
            fixed, fixed_exit = await _run_regressions(target)
        except SandboxUnavailableError as exc:
            return {
                "benchmark": "independent-fastapi-fixtures",
                "scenario_count": len(scenarios),
                "fixture_validation": "SKIPPED",
                "skip_reason": scrub_text(str(exc)),
            }

    count = len(scenarios)
    valid = (
        broken["failed"] == count
        and broken["passed"] == 0
        and fixed["passed"] == count
        and fixed["failed"] == 0
        and fixed_exit == 0
        and set(changed_paths) == {"app.py"}
    )
    result_documents = [
        (result_path, json.loads(result_path.read_text(encoding="utf-8")))
        for result_path in RESULTS_DIR.glob("*.json")
    ]
    recorded_attempts, run_selection = _select_recorded_run(result_documents, count)
    terminal_attempts = [
        attempt
        for attempt in recorded_attempts
        if isinstance(attempt, dict) and attempt.get("status") in {"PASS", "FAIL"}
    ]
    if terminal_attempts:
        measured = evaluate_attempts({"attempts": recorded_attempts})
        ai_metrics = measured["metrics"]
        covered = sorted({attempt["scenario_id"] for attempt in terminal_attempts})
        ai_metrics_status = "MEASURED"
    else:
        ai_metrics = {
            "diagnosis_accuracy": None,
            "successful_verified_fixes": None,
            "average_repair_seconds": None,
            "false_fixes": None,
            "manual_interventions": None,
        }
        covered = []
        ai_metrics_status = "NOT_RUN"
    return {
        "benchmark": "independent-fastapi-fixtures",
        "scenario_count": count,
        "fixture_validation": "PASS" if valid else "FAIL",
        "broken_baseline": {**broken, "pytest_exit_code": broken_exit},
        "reference_repair": {**fixed, "pytest_exit_code": fixed_exit},
        "reference_changed_paths": changed_paths,
        "ai_attempts": len(terminal_attempts),
        "ai_scenario_coverage": covered,
        "ai_metrics_status": ai_metrics_status,
        "ai_run_selection": run_selection,
        "metrics": ai_metrics,
        "note": (
            "20-case fixture validation uses reference repairs. AI metrics use only recorded "
            "real CLI attempts and are not extrapolated to unattempted scenarios."
        ),
    }


def evaluate_attempts(document: dict[str, Any]) -> dict[str, Any]:
    scenarios = {scenario["id"]: scenario for scenario in load_scenarios()}
    attempts = document.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("results JSON must contain an 'attempts' array")

    seen: set[str] = set()
    diagnosis_correct = verified = false_fixes = manual = 0
    repair_times: list[float] = []
    skipped = 0
    reported_costs: list[float] = []
    token_totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    token_reported_attempts = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("each attempt must be a JSON object")
        status = attempt.get("status")
        if status in {"SKIPPED", "PENDING", "PENDING_VERIFICATION"}:
            skipped += 1
            continue
        scenario_id = attempt.get("scenario_id")
        if scenario_id not in scenarios:
            raise ValueError(f"unknown scenario_id: {scenario_id!r}")
        if scenario_id in seen:
            raise ValueError(f"duplicate attempt for scenario_id: {scenario_id}")
        seen.add(scenario_id)
        scenario = scenarios[scenario_id]
        diagnosis_correct += attempt.get("diagnosis") == scenario["root_cause"]
        verified += bool(attempt.get("regression_tests_passed")) and bool(
            attempt.get("scope_guard_passed")
        )
        false_fixes += bool(attempt.get("false_fix"))
        manual += bool(
            attempt.get("manual_intervention_required", attempt.get("manual_intervention", False))
        )
        seconds = attempt.get("end_to_end_seconds")
        if seconds is None:
            repair = attempt.get("repair_seconds")
            verification = attempt.get("verification_seconds") or 0
            if isinstance(repair, (int, float)) and not isinstance(repair, bool):
                seconds = repair + verification
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError(f"repair duration must be a non-negative number for {scenario_id}")
        repair_times.append(float(seconds))
        cost = attempt.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            reported_costs.append(float(cost))
        has_tokens = False
        for key in token_totals:
            value = attempt.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                token_totals[key] += value
                has_tokens = True
        token_reported_attempts += has_tokens

    total = len(repair_times)
    if total == 0:
        raise ValueError("at least one real attempt is required to calculate metrics")
    return {
        "attempted_scenarios": total,
        "skipped_scenarios": skipped,
        "metrics": {
            "diagnosis_accuracy": diagnosis_correct / total,
            "successful_verified_fixes": verified,
            "verified_fix_rate": verified / total,
            "average_repair_seconds": statistics.mean(repair_times),
            "false_fixes": false_fixes,
            "false_fix_rate": false_fixes / total,
            "manual_interventions": manual,
            "manual_intervention_rate": manual / total,
            "average_end_to_end_seconds": statistics.mean(repair_times),
            "reported_cost_usd": sum(reported_costs) if reported_costs else None,
            "cost_reported_attempts": len(reported_costs),
            **token_totals,
            "token_reported_attempts": token_reported_attempts,
        },
        "metric_definitions": {
            "diagnosis_accuracy": "Exact root_cause label matches divided by attempted scenarios.",
            "successful_verified_fixes": "Attempts passing regression tests and the scope guard.",
            "false_fix_rate": "Attempts explicitly marked false_fix divided by attempts.",
            "manual_intervention_rate": (
                "Attempts marked as requiring human follow-up divided by terminal attempts."
            ),
            "average_repair_seconds": (
                "Mean repair plus verification duration, where verification duration is recorded."
            ),
            "average_end_to_end_seconds": (
                "Mean elapsed repair and regression verification duration."
            ),
            "reported_cost_usd": "Sum of cost fields explicitly reported by the CLI; null if none.",
            "input_tokens": "Sum of input_tokens fields explicitly reported by the CLI.",
            "output_tokens": "Sum of output_tokens fields explicitly reported by the CLI.",
            "cached_input_tokens": (
                "Sum of cached_input_tokens fields explicitly reported by the CLI."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="run broken and reference-fixed fixture suites")
    evaluate = subparsers.add_parser("evaluate", help="score actual AI attempt results from JSON")
    evaluate.add_argument("results_json", type=Path)
    args = parser.parse_args()

    if args.command == "validate":
        report = asyncio.run(validate_fixtures())
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["fixture_validation"] in {"PASS", "SKIPPED"} else 1
    report = evaluate_attempts(json.loads(args.results_json.read_text(encoding="utf-8")))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
