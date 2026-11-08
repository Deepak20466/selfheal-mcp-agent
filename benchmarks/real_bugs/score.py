"""Aggregate real-bug benchmark records without inventing missing costs."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from benchmarks.real_bugs.common import RESULT_ROOT, atomic_json

FAILURE_CATEGORIES = (
    "no_patch",
    "invalid_patch",
    "rejected_by_policy",
    "tests_still_failing",
    "timeout",
    "rate_limited",
    "provider_error",
    "token_cap",
    "cost_cap",
    "cost_unknown",
    "repair_error",
)


def _read_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or not all(
                isinstance(row.get(key), str) for key in ("case_id", "arm")
            ):
                continue
            run_id = str(row.get("run_id", path.stem))
            row.setdefault("run_id", run_id)
            rows.append(row)
    return rows


def _median_cost(rows: list[dict[str, Any]]) -> str | None:
    known: list[Decimal] = []
    for row in rows:
        value = row.get("cost_usd")
        if value is None:
            continue
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            continue
        if amount.is_finite() and amount >= 0:
            known.append(amount)
    if not known:
        return None
    return format(statistics.median(known), "f")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    target_pass = [row for row in rows if row.get("target_tests_pass") is True]
    full_pass = [
        row
        for row in rows
        if row.get("target_tests_pass") is True and row.get("full_suite_pass") is True
    ]
    resolved = full_pass
    eligible = [
        row
        for row in rows
        if row.get("extra_checks_available") is True
        and row.get("human_full_suite_pass") is True
        and row.get("target_tests_pass") is True
    ]
    false_positive = [row for row in eligible if row.get("full_suite_pass") is not True]
    durations = [
        float(row["duration_seconds"]) for row in rows if row.get("duration_seconds") is not None
    ]
    failures = Counter(
        row.get("failure_category") for row in rows if row.get("failure_category") is not None
    )
    costs = sum(row.get("cost_usd") is not None for row in rows)
    return {
        "attempted": len(rows),
        "resolved": len(resolved),
        "resolve_rate": len(resolved) / len(rows) if rows else None,
        "target_tests_passed": len(target_pass),
        "target_test_pass_rate": len(target_pass) / len(rows) if rows else None,
        "resolved_and_full_suite_passed": len(full_pass),
        "resolve_rate_with_full_suite_pass": len(full_pass) / len(rows) if rows else None,
        "false_positive_cases": len(false_positive),
        "false_positive_denominator": len(eligible),
        "false_positive_rate": len(false_positive) / len(eligible) if eligible else None,
        "false_positive_method": (
            "target tests pass but aggregate full suite fails, where the human fix passed "
            "the full suite"
        ),
        "median_time_seconds": statistics.median(durations) if durations else None,
        "median_cost_usd": _median_cost(rows),
        "cost_reported_cases": costs,
        "cost_unknown_cases": len(rows) - costs,
        "failure_taxonomy": {
            category: failures.get(category, 0) for category in FAILURE_CATEGORIES
        },
        "other_failure_categories": {
            category: count
            for category, count in failures.items()
            if category not in FAILURE_CATEGORIES
        },
    }


def score(
    paths: list[Path], *, results_root: Path = RESULT_ROOT
) -> tuple[Path, Path, dict[str, Any]]:
    rows = _read_rows(paths)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("run_id", "unknown")),
            str(row.get("provider", "unknown")),
            str(row.get("model", "unknown")),
        )
        grouped.setdefault(key, []).append(row)

    runs = []
    for (run_id, provider, model), run_rows in sorted(grouped.items()):
        arms = {
            arm: summarize([row for row in run_rows if row.get("arm") == arm])
            for arm in ("with_validation", "without_validation")
        }
        runs.append({"run_id": run_id, "provider": provider, "model": model, "arms": arms})
    repeat_comparisons = _repeat_comparisons(grouped)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "input_files": [str(path) for path in paths],
        "attempt_records": len(rows),
        "runs": runs,
        "repeat_comparisons": repeat_comparisons,
        "limitations": [
            "Cost is reported only when the provider returned a billed-cost value; unknown is "
            "not treated as zero.",
            "False positives use aggregate full-suite pass/fail as an available extra-check "
            "signal.",
            "The no-policy ablation retains OS sandboxing for repository test execution.",
        ],
    }
    results_root.mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).date().isoformat()
    json_path = results_root / f"{date}.json"
    markdown_path = results_root / f"{date}.md"
    atomic_json(json_path, report)
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path, report


def _repeat_comparisons(
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    by_repeat: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = {}
    for (run_id, provider, model), rows in grouped.items():
        if run_id.endswith("-repeat-a"):
            prefix, repeat = run_id[: -len("-repeat-a")], "a"
        elif run_id.endswith("-repeat-b"):
            prefix, repeat = run_id[: -len("-repeat-b")], "b"
        else:
            continue
        by_repeat.setdefault((prefix, provider, model), {})[repeat] = rows

    comparisons = []
    for (prefix, provider, model), repeats in sorted(by_repeat.items()):
        if "a" not in repeats or "b" not in repeats:
            continue
        arms: dict[str, Any] = {}
        for arm in ("with_validation", "without_validation"):
            first = {
                str(row["case_id"]): bool(
                    row.get("target_tests_pass") is True and row.get("full_suite_pass") is True
                )
                for row in repeats["a"]
                if row.get("arm") == arm and row.get("case_id") is not None
            }
            second = {
                str(row["case_id"]): bool(
                    row.get("target_tests_pass") is True and row.get("full_suite_pass") is True
                )
                for row in repeats["b"]
                if row.get("arm") == arm and row.get("case_id") is not None
            }
            paired = sorted(first.keys() & second.keys())
            first_rate = sum(first[item] for item in paired) / len(paired) if paired else None
            second_rate = sum(second[item] for item in paired) / len(paired) if paired else None
            arms[arm] = {
                "paired_cases": len(paired),
                "repeat_a_resolved": sum(first[item] for item in paired),
                "repeat_b_resolved": sum(second[item] for item in paired),
                "repeat_a_resolve_rate": first_rate,
                "repeat_b_resolve_rate": second_rate,
                "absolute_rate_difference": (
                    abs(first_rate - second_rate)
                    if first_rate is not None and second_rate is not None
                    else None
                ),
            }
        comparisons.append({"pair": prefix, "provider": provider, "model": model, "arms": arms})
    return comparisons


def _percent(value: Any) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Real historical bug benchmark results",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "| Run | Provider / model | Arm | Cases | Target pass rate | "
        "Resolve rate (target + suite) | "
        "False positives | Median time | Median reported cost |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        for arm, metrics in run["arms"].items():
            lines.append(
                "| {run} | {provider} / {model} | {arm} | {attempted} | {target} | {resolve} | "
                "{false_positive} | {median_time} | {cost} |".format(
                    run=run["run_id"],
                    provider=run["provider"],
                    model=run["model"],
                    arm=arm,
                    attempted=metrics["attempted"],
                    target=_percent(metrics["target_test_pass_rate"]),
                    resolve=_percent(metrics["resolve_rate"]),
                    false_positive=(
                        f"{metrics['false_positive_cases']}/{metrics['false_positive_denominator']}"
                    ),
                    median_time=(
                        "n/a"
                        if metrics["median_time_seconds"] is None
                        else f"{metrics['median_time_seconds']:.1f}s"
                    ),
                    cost=(
                        "n/a"
                        if metrics["median_cost_usd"] is None
                        else f"${metrics['median_cost_usd']}"
                    ),
                )
            )
    if not report["runs"]:
        lines.append("| No completed run | n/a | n/a | 0 | n/a | n/a | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## Repeat-run variation",
            "",
        ]
    )
    for comparison in report.get("repeat_comparisons", []):
        for arm, metrics in comparison["arms"].items():
            difference = metrics["absolute_rate_difference"]
            lines.append(
                f"- {comparison['pair']} / {arm}: {metrics['paired_cases']} paired cases; "
                f"repeat A {_percent(metrics['repeat_a_resolve_rate'])}, "
                f"repeat B {_percent(metrics['repeat_b_resolve_rate'])}; "
                f"absolute rate difference {_percent(difference)}."
            )
    if not report.get("repeat_comparisons"):
        lines.append("- No complete repeat pair is present; run-to-run variation is not measured.")
    lines.extend(
        [
            "",
            "## Failure taxonomy",
            "",
            "Per-arm counts are in the JSON report. `n/a` means no value was measured; it "
            "does not mean zero.",
            "",
            "## Limits",
            "",
            *[f"- {item}" for item in report["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output-dir", type=Path, default=RESULT_ROOT)
    args = parser.parse_args(argv)
    json_path, markdown_path, report = score(args.inputs, results_root=args.output_dir)
    print(f"Scored {report['attempt_records']} attempt records.")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
