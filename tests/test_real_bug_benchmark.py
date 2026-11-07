"""Offline tests for the real historical bug benchmark pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from benchmarks.real_bugs import mine as mine_module
from benchmarks.real_bugs import prepare as prepare_module
from benchmarks.real_bugs import run as run_module
from benchmarks.real_bugs import score as score_module
from benchmarks.real_bugs.common import DATA_ROOT, PROJECT_ROOT, case_id
from benchmarks.real_bugs.run import TracingMCPClient
from mcp_server.sandbox import SandboxViolation, check_readable
from mcp_server.tools.code import _is_noise_path


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _historical_repo(root: Path) -> tuple[Path, str, str]:
    repository = root / "upstream"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Fixture Author")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "app.py").write_text(
        "def triple(value):\n    return value * 2\n", encoding="utf-8"
    )
    _git(repository, "add", "app.py")
    _git(repository, "commit", "--quiet", "-m", "Initial implementation")
    parent = _git(repository, "rev-parse", "HEAD")

    (repository / "app.py").write_text(
        "def triple(value):\n    return value * 3\n", encoding="utf-8"
    )
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "from app import triple\n\n\ndef test_triple():\n    assert triple(2) == 6\n",
        encoding="utf-8",
    )
    _git(repository, "add", "app.py", "tests/test_app.py")
    _git(repository, "commit", "--quiet", "-m", "Fix triple result and add regression test")
    fix = _git(repository, "rev-parse", "HEAD")
    return repository, parent, fix


def test_mine_keeps_test_and_human_diffs_separate(tmp_path: Path) -> None:
    upstream, parent, fix = _historical_repo(tmp_path)
    config = tmp_path / "repos.yaml"
    config.write_text(
        f"repositories:\n  - name: fixture\n    url: {upstream.as_posix()}\n",
        encoding="utf-8",
    )
    output = tmp_path / "cases"

    identifiers = mine_module.mine(
        repos_file=config,
        cache=tmp_path / "cache",
        output=output,
    )

    assert len(identifiers) == 1
    case_dir = output / identifiers[0]
    metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    test_diff = (case_dir / "test.diff").read_text(encoding="utf-8")
    human_diff = (case_dir / "scoring_only" / "human_fix.diff").read_text(encoding="utf-8")
    assert metadata["parent_sha"] == parent
    assert metadata["fix_sha"] == fix
    assert identifiers[0] == case_id("fixture", fix)
    assert fix[:12] not in identifiers[0]
    assert "Fix triple result" not in json.dumps(metadata)
    assert "tests/test_app.py" in test_diff
    assert "a/app.py" not in test_diff
    assert "b/app.py" not in test_diff
    assert "app.py" in human_diff
    assert "tests/test_app.py" not in human_diff
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["case_ids"] == [
        identifiers[0]
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("tests/test.py", True),
        ("tests/app_test.py", True),
        ("tests/test_helpers.py", True),
        ("tests/helpers.py", False),
        ("tests/conftest.py", False),
        ("tests/utils_test_data.py", False),
    ],
)
def test_miner_identifies_test_modules_without_helpers(path: str, expected: bool) -> None:
    assert mine_module._is_test(path) is expected


def test_prepare_rejects_checkouts_with_missing_historical_blobs(tmp_path: Path) -> None:
    repository, _parent, _fix = _historical_repo(tmp_path)
    (repository / "app.py").unlink()

    with pytest.raises(RuntimeError, match="missing 1 tracked files"):
        prepare_module._assert_complete_checkout(repository)


def test_prepare_discards_flaky_cases_and_keeps_only_reproducing_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, parent, fix = _historical_repo(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    _git(tmp_path, "clone", "--quiet", str(upstream), str(cache / "fixture"))
    identifier = case_id("fixture", fix)
    case = tmp_path / identifier
    scoring = case / "scoring_only"
    scoring.mkdir(parents=True)
    (case / "metadata.json").write_text(
        json.dumps(
            {
                "case_id": identifier,
                "repository": "fixture",
                "parent_sha": parent,
                "fix_sha": fix,
                "test_paths": ["tests/test_app.py"],
                "source_paths": ["app.py"],
                "test_dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    case_repo = tmp_path / "case_checkout"
    _git(tmp_path, "clone", "--quiet", str(upstream), str(case_repo))
    test_patch = subprocess.run(
        ["git", "diff", "--binary", parent, fix, "--", "tests/test_app.py"],
        cwd=case_repo,
        check=True,
        capture_output=True,
    ).stdout
    human_patch = subprocess.run(
        ["git", "diff", "--binary", parent, fix, "--", "app.py"],
        cwd=case_repo,
        check=True,
        capture_output=True,
    ).stdout
    (case / "test.diff").write_bytes(test_patch)
    (scoring / "human_fix.diff").write_bytes(human_patch)

    def fake_make_venv(_: dict[str, Any], __: Path, venv: Path) -> tuple[Path, Path]:
        venv.mkdir(parents=True)
        return venv, venv / "python"

    calls = 0

    async def fake_pytest_repeated(
        workspace: Path,
        _venv: Path,
        _python: Path,
        _paths: list[str],
        _source_paths: list[str],
        _timeout: float,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        before_source = (workspace / "baseline" / "app.py").read_text(encoding="utf-8")
        after_source = (workspace / "fixed" / "app.py").read_text(encoding="utf-8")
        return {
            "passed": True,
            "sandbox": "fixture",
            "before_runs": [{"passed": "return value * 3" in before_source}] * 3,
            "after_runs": [{"passed": "return value * 3" in after_source}] * 3,
            "full_suite": {"passed": True, "output": "fixture full suite"},
        }

    monkeypatch.setattr(prepare_module, "_make_venv", fake_make_venv)
    monkeypatch.setattr(prepare_module, "_pytest_repeated", fake_pytest_repeated)
    prepared_root = tmp_path / "prepared"
    venv_root = tmp_path / "venvs"
    row = prepare_module.prepare_case(
        case,
        cache=cache,
        prepared_root=prepared_root,
        venv_root=venv_root,
    )

    assert row["status"] == "kept"
    assert row["preparation_version"] == prepare_module.PREPARATION_SCHEMA_VERSION
    assert row["test_passes_before"] == [False, False, False]
    assert row["test_passes_with_human_fix"] == [True, True, True]
    assert row["human_full_suite_pass"] is True
    assert row["sandbox"] == "fixture"
    assert calls == 1
    prepared_repo = prepared_root / identifier / "repo"
    assert not (prepared_repo / ".git").exists()
    assert "return value * 2" in (prepared_repo / "app.py").read_text(encoding="utf-8")
    assert (prepared_repo / "tests" / "test_app.py").is_file()
    assert not (prepared_repo / "scoring_only").exists()


def test_preparation_harness_runs_three_before_and_after_checks(tmp_path: Path) -> None:
    workspace = tmp_path / "sandbox_fixture"
    baseline = workspace / "baseline"
    fixed = workspace / "fixed"
    (baseline / "src").mkdir(parents=True)
    (fixed / "src").mkdir(parents=True)
    for repository, value in ((baseline, 2), (fixed, 3)):
        (repository / "src" / "app.py").write_text(
            f"def triple(value):\n    return value * {value}\n", encoding="utf-8"
        )
        tests = repository / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "strategies.py").write_text("EXPECTED = 6\n", encoding="utf-8")
        (tests / "test_app.py").write_text(
            "from .strategies import EXPECTED\nfrom app import triple\n\n"
            "def test_triple():\n    assert triple(2) == EXPECTED\n",
            encoding="utf-8",
        )
    harness = workspace / "_selfheal_prepare_harness.py"
    harness.write_text(prepare_module._test_harness_source(), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(harness),
            sys.executable,
            "20",
            '["tests/test_app.py"]',
            '["src/app.py"]',
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    summary = json.loads(result.stdout)

    assert [item["passed"] for item in summary["before_runs"]] == [False, False, False]
    assert [item["passed"] for item in summary["after_runs"]] == [True, True, True]
    assert summary["full_suite"]["passed"] is True


def test_git_utils_pytest_bootstrap_imports_checkout_test_helpers(tmp_path: Path) -> None:
    from mcp_server import git_utils

    repository = tmp_path / "repository"
    (repository / "src" / "example").mkdir(parents=True)
    tests = repository / "tests"
    tests.mkdir()
    (repository / "src" / "example" / "__init__.py").write_text(
        "VALUE = 'checkout'\n", encoding="utf-8"
    )
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "helpers.py").write_text("EXPECTED = 'checkout'\n", encoding="utf-8")
    (tests / "test_imports.py").write_text(
        "from .helpers import EXPECTED\nfrom example import VALUE\n\n"
        "def test_checkout_imports():\n    assert EXPECTED == VALUE\n",
        encoding="utf-8",
    )

    result = asyncio.run(git_utils.run_pytest("tests/test_imports.py", cwd=repository))

    assert result["sandbox"] in {"windows-appcontainer-job", "bubblewrap"}
    assert result["passed"] is True, result
    assert "1 passed" in result["output"]


def test_preparation_harness_is_passed_as_trusted_inline_code(tmp_path: Path, monkeypatch) -> None:
    import mcp_server.execution_sandbox as execution_sandbox

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    venv = tmp_path / "venv"
    venv.mkdir()
    python = tmp_path / "python.exe"
    commands: list[list[str]] = []

    async def fake_run_untrusted_command(command: list[str], **_kwargs: Any) -> dict[str, Any]:
        commands.append(command)
        return {
            "passed": True,
            "output": json.dumps({"before_runs": [], "after_runs": [], "full_suite": None}),
        }

    monkeypatch.setattr(execution_sandbox, "run_untrusted_command", fake_run_untrusted_command)
    result = asyncio.run(
        prepare_module._pytest_repeated(workspace, venv, python, ["test_app.py"], ["app.py"], 10)
    )

    assert commands[0][1] == "-c"
    assert "before = [run_tests" in commands[0][2]
    assert result["passed"] is True


def test_score_reports_measured_rates_and_unknown_costs(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts.jsonl"
    attempts.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "run_id": "fixture-run",
                    "case_id": "one",
                    "arm": "with_validation",
                    "provider": "fixture",
                    "model": "offline",
                    "target_tests_pass": True,
                    "full_suite_pass": True,
                    "extra_checks_available": True,
                    "human_full_suite_pass": True,
                    "duration_seconds": 12,
                    "cost_usd": None,
                },
                {
                    "run_id": "fixture-run",
                    "case_id": "two",
                    "arm": "with_validation",
                    "provider": "fixture",
                    "model": "offline",
                    "target_tests_pass": True,
                    "full_suite_pass": False,
                    "extra_checks_available": True,
                    "human_full_suite_pass": True,
                    "duration_seconds": 20,
                    "cost_usd": "0.02",
                    "failure_category": "tests_still_failing",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    json_path, markdown_path, report = score_module.score(
        [attempts], results_root=tmp_path / "results"
    )

    arm = report["runs"][0]["arms"]["with_validation"]
    assert arm["attempted"] == 2
    assert arm["resolve_rate"] == 1.0
    assert arm["resolve_rate_with_full_suite_pass"] == 0.5
    assert arm["false_positive_rate"] == 0.5
    assert arm["median_cost_usd"] == "0.02"
    assert arm["cost_unknown_cases"] == 1
    assert json_path.is_file() and markdown_path.is_file()
    assert "does not mean zero" in markdown_path.read_text(encoding="utf-8")


def test_score_compares_resolve_rates_for_paired_repeat_runs(tmp_path: Path) -> None:
    attempts = tmp_path / "repeats.jsonl"
    rows = [
        {
            "run_id": f"2026-01-01-repeat-{repeat}",
            "case_id": case_id,
            "arm": arm,
            "provider": "fixture",
            "model": "offline",
            "target_tests_pass": passed,
        }
        for repeat, outcomes in (
            ("a", {"one": True, "two": False}),
            ("b", {"one": True, "two": True}),
        )
        for arm in ("with_validation", "without_validation")
        for case_id, passed in outcomes.items()
    ]
    attempts.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    _json_path, markdown_path, report = score_module.score(
        [attempts], results_root=tmp_path / "results"
    )

    comparison = report["repeat_comparisons"][0]
    for arm in ("with_validation", "without_validation"):
        assert comparison["arms"][arm]["paired_cases"] == 2
        assert comparison["arms"][arm]["absolute_rate_difference"] == 0.5
    assert "absolute rate difference 50.0%" in markdown_path.read_text(encoding="utf-8")


def test_benchmark_artifacts_are_outside_mcp_reads() -> None:
    assert DATA_ROOT != PROJECT_ROOT
    assert PROJECT_ROOT not in DATA_ROOT.parents
    with pytest.raises(SandboxViolation, match="benchmark result data"):
        check_readable("benchmarks/real_bugs/results/2026-11-07.json")
    assert _is_noise_path("benchmarks/real_bugs/results/2026-11-07.json")


def test_runner_uses_only_current_manifest_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_root = tmp_path / "cases"
    prepared_root = tmp_path / "prepared"

    def add_case(identifier: str, fix_sha: str, status: str) -> None:
        case_dir = case_root / identifier
        (case_dir / "scoring_only").mkdir(parents=True)
        (case_dir / "metadata.json").write_text(
            json.dumps({"case_id": identifier, "repository": "fixture", "fix_sha": fix_sha}),
            encoding="utf-8",
        )
        prepared_case = prepared_root / identifier
        (prepared_case / "repo").mkdir(parents=True)
        (prepared_case / "prepared.json").write_text(
            json.dumps({"case_id": identifier, "status": status}), encoding="utf-8"
        )

    kept_sha = "a" * 40
    discarded_sha = "b" * 40
    stale_sha = "c" * 40
    kept_id = case_id("fixture", kept_sha)
    discarded_id = case_id("fixture", discarded_sha)
    add_case(kept_id, kept_sha, "kept")
    add_case(discarded_id, discarded_sha, "discarded")
    add_case(case_id("fixture", stale_sha), stale_sha, "kept")
    (case_root / "manifest.json").write_text(
        json.dumps({"case_ids": [kept_id, discarded_id]}), encoding="utf-8"
    )
    monkeypatch.setattr(run_module, "CASE_ROOT", case_root)

    cases = run_module._prepared_cases(prepared_root)

    assert [metadata["case_id"] for _path, metadata, _record in cases] == [kept_id]


def test_rate_limit_backoff_honors_retry_after() -> None:
    record = {
        "rate_limit_attempt": 1,
        "retry_after_seconds": 120,
        "recorded_at": datetime.now(UTC).isoformat(),
    }

    remaining = run_module._rate_limit_backoff_remaining(record)

    assert 119 <= remaining <= 120


def test_gemini_provider_requires_recent_model_quota_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import settings
    from healer import api_adapters

    marker = object()
    monkeypatch.setattr(api_adapters, "GeminiApiClient", lambda: marker)
    monkeypatch.setattr(settings, "gemini_api_model", "gemini-3.8-flash")
    monkeypatch.setenv("REAL_BUG_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("REAL_BUG_BILLING_DISABLED", "true")
    monkeypatch.setenv("GEMINI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("GEMINI_38_FLASH_FREE_QUOTA", "true")
    monkeypatch.setenv("GEMINI_QUOTA_CHECKED_UTC", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    client, model, attested_free = run_module._provider("gemini_api")

    assert client is marker
    assert model == "gemini-3.8-flash"
    assert attested_free is True

    monkeypatch.setenv("GEMINI_QUOTA_CHECKED_UTC", "2000-01-01T00:00:00Z")
    with pytest.raises(RuntimeError, match="no more than 15 minutes old"):
        run_module._provider("gemini_api")


def test_runner_stops_after_recording_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def rate_limited(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["arm"])
        return {
            "case_id": "fixture-case",
            "arm": kwargs["arm"],
            "failure_category": "rate_limited",
            "rate_limited": True,
            "retry_after_seconds": 60,
            "retryable": True,
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(run_module, "_run_arm", rate_limited)
    run_file = tmp_path / "partial.jsonl"
    metadata = {
        "case_id": "fixture-case",
        "parent_sha": "a" * 40,
        "repository": "fixture",
    }

    rows = asyncio.run(
        run_module._run_cases(
            cases=[(tmp_path, metadata, {})],
            provider_name="groq_api",
            run_file=run_file,
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            timeout_seconds=10,
            resume=False,
            provider_client=object(),
            model="fixture-model",
            attested_free=True,
        )
    )

    assert calls == ["with_validation"]
    assert len(rows) == 1
    assert json.loads(run_file.read_text(encoding="utf-8"))["failure_category"] == "rate_limited"


def test_model_tool_wrapper_rejects_git_history_tools() -> None:
    class FakeClient:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise AssertionError("unauthorized MCP tool reached the server")

    trace = TracingMCPClient(FakeClient(), validated=True, error_id=42)
    with pytest.raises(ValueError, match="not authorized"):
        asyncio.run(trace.call_tool("get_recent_commits", {}))


def test_model_context_excludes_human_fix_and_post_parent_history(tmp_path: Path) -> None:
    from healer.prompts import build_initial_messages

    fix_sha = "fixing-commit-sha-never-model-facing"
    fix_message = "Fix private historical regression"
    human_diff = "return 999  # reference solution"
    later_history = "later-commit-secret-message"
    source = tmp_path / "prepared-parent"
    (source / ".git").mkdir(parents=True)
    (source / "scoring_only").mkdir()
    (source / ".git" / "history.txt").write_text(
        f"{fix_sha}\n{fix_message}\n{later_history}\n", encoding="utf-8"
    )
    (source / "scoring_only" / "human_fix.diff").write_text(human_diff, encoding="utf-8")
    (source / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    snapshot = tmp_path / "connected" / "repo"

    run_module._git_init_snapshot(source, snapshot)
    messages = build_initial_messages(
        source_kind="error",
        source={
            "id": 7,
            "exception_type": "AssertionError",
            "message": "The selected regression assertion failed.",
            "traceback": "assert value() == 2",
            "file_path": "app.py",
            "line_number": 2,
        },
        attempt_number=1,
        max_attempts=1,
        heal_job_id=9,
        worktree="realbug-fixture-with_validation",
        previous_attempts_summary=None,
    )
    model_context = json.dumps(
        {
            "messages": messages,
            "system": run_module._benchmark_prompt(),
            "files": [
                path.relative_to(snapshot).as_posix()
                for path in snapshot.rglob("*")
                if path.is_file() and ".git" not in path.parts
            ],
        },
        sort_keys=True,
    )

    # The Selfheal workspace gets its own baseline commit. The source checkout's
    # .git directory and its fixing/later history must never be copied in.
    assert not (snapshot / "scoring_only").exists()
    for withheld in (fix_sha, fix_message, human_diff, later_history):
        assert withheld not in model_context

    history = subprocess.run(
        ["git", "log", "--all", "--format=%H %s"],
        cwd=snapshot,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert len(history.splitlines()) == 1
    assert "Benchmark parent snapshot" in history
    assert fix_sha not in history
    assert fix_message not in history
    assert later_history not in history


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("RUN_REAL_BUG_NETWORK") != "1", reason="opt-in network mining")
def test_configured_public_repository_mining_is_opt_in(tmp_path: Path) -> None:
    identifiers = mine_module.mine(
        cache=tmp_path / "cache",
        output=tmp_path / "cases",
        limit_per_repo=1,
    )
    assert identifiers
