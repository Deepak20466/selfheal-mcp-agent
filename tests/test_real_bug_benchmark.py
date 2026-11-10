"""Offline tests for the real historical bug benchmark pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
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


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows workspace collisions")
def test_windows_attempt_workspace_preserves_legacy_directory_and_clones_to_reserved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.worktree as worktree_module

    repository_root = tmp_path / "repository"
    worktrees_root = repository_root / "worktrees"
    monkeypatch.setattr(worktree_module, "WORKTREES_ROOT", worktrees_root)
    case_id_value = "fixture-case"
    arm = "with_validation"
    legacy = repository_root / "connected_apps" / "real_bugs" / case_id_value / arm
    legacy.mkdir(parents=True)
    user_file = legacy / "keep.txt"
    user_file.write_text("preserve existing workspace", encoding="utf-8")

    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "add", "module.py")
    _git(source, "commit", "--quiet", "-m", "fixture")

    workspace = run_module._allocate_attempt_workspace(
        case_id_value=case_id_value,
        arm=arm,
        repository_root=repository_root,
        worktrees_root=worktrees_root,
    )
    try:
        run_module._git_init_snapshot(source, workspace.snapshot)
        assert workspace.root.parent != legacy
        cloned = asyncio.run(
            worktree_module.create_worktree_for_connected_app(
                workspace.worktree_name,
                "benchmark/fixture-case-with-validation",
                source_dir=workspace.snapshot,
            )
        )
        assert cloned == workspace.worktree_path
        assert (cloned / "module.py").read_text(encoding="utf-8") == "value = 1\n"
        assert user_file.read_text(encoding="utf-8") == "preserve existing workspace"
    finally:
        run_module._cleanup_owned_path(
            workspace.worktree_path, workspace.worktree_marker, workspace.owner_token
        )
        run_module._cleanup_owned_path(workspace.root, workspace.root_marker, workspace.owner_token)


@pytest.mark.skipif(os.name != "nt", reason="exercises interrupted Windows workspace recovery")
def test_windows_interrupted_workspace_is_not_reused_or_deleted(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    worktrees_root = repository_root / "worktrees"
    kwargs = {
        "case_id_value": "fixture-case",
        "arm": "without_validation",
        "repository_root": repository_root,
        "worktrees_root": worktrees_root,
    }
    interrupted = run_module._allocate_attempt_workspace(**kwargs)
    interrupted.snapshot.mkdir()
    stale_patch = interrupted.snapshot / "stale-patch.txt"
    stale_patch.write_text("interrupted state", encoding="utf-8")

    resumed = run_module._allocate_attempt_workspace(**kwargs)
    try:
        assert resumed.root != interrupted.root
        assert resumed.worktree_path != interrupted.worktree_path
        assert not (resumed.snapshot / "stale-patch.txt").exists()
        assert stale_patch.read_text(encoding="utf-8") == "interrupted state"
    finally:
        run_module._cleanup_owned_path(
            resumed.worktree_path, resumed.worktree_marker, resumed.owner_token
        )
        run_module._cleanup_owned_path(resumed.root, resumed.root_marker, resumed.owner_token)
        run_module._cleanup_owned_path(
            interrupted.root, interrupted.root_marker, interrupted.owner_token
        )


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows benchmark resume workspaces")
def test_windows_resume_skips_attempted_arm_and_allocates_fresh_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.worktree as worktree_module
    import mcp_server.sandbox as sandbox_module

    repository_root = tmp_path / "repository"
    worktrees_root = repository_root / "worktrees"
    monkeypatch.setattr(sandbox_module, "REPO_ROOT", repository_root)
    monkeypatch.setattr(sandbox_module, "WORKTREES_ROOT", worktrees_root)
    monkeypatch.setattr(worktree_module, "WORKTREES_ROOT", worktrees_root)
    case_id_value = "fixture-case"
    legacy = repository_root / "connected_apps" / "real_bugs" / case_id_value / "without_validation"
    legacy.mkdir(parents=True)
    preserved = legacy / "user-owned.txt"
    preserved.write_text("do not remove", encoding="utf-8")

    run_file = tmp_path / "resume.jsonl"
    run_file.write_text(
        json.dumps(
            {
                "case_id": case_id_value,
                "arm": "with_validation",
                "failure_category": "provider_error",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata = {
        "case_id": case_id_value,
        "parent_sha": "a" * 40,
        "repository": "fixture",
    }
    allocated: list[run_module.AttemptWorkspace] = []

    async def run_inner(*, workspace: run_module.AttemptWorkspace, **kwargs: Any) -> dict[str, Any]:
        allocated.append(workspace)
        return {
            "case_id": kwargs["metadata"]["case_id"],
            "arm": kwargs["arm"],
            "status": "failed",
            "failure_category": "repair_error",
        }

    monkeypatch.setattr(run_module, "_run_arm_inner", run_inner)
    rows = asyncio.run(
        run_module._run_cases(
            cases=[(tmp_path, metadata, {})],
            provider_name="gemini_api",
            run_file=run_file,
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            timeout_seconds=10,
            resume=True,
            provider_client=object(),
            model="fixture-model",
            attested_free=True,
        )
    )

    assert [row["arm"] for row in rows] == ["without_validation"]
    assert len(allocated) == 1
    assert allocated[0].root.parent != legacy
    assert allocated[0].root.is_relative_to(repository_root / "connected_apps" / "real_bugs")
    assert allocated[0].snapshot != legacy
    assert preserved.read_text(encoding="utf-8") == "do not remove"
    assert not allocated[0].root.exists()


def test_tracing_baseline_returns_patch_apply_failure_as_recoverable_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from healer.mcp_client import MCPToolError

    async def fail_patch(_self: Any, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("git apply failed: malformed unified diff")

    monkeypatch.setattr(run_module.TracingMCPClient, "_apply_without_policy", fail_patch)
    tracer = run_module.TracingMCPClient(object(), validated=False, error_id=1)

    with pytest.raises(MCPToolError, match="malformed unified diff"):
        asyncio.run(
            tracer.call_tool(
                "propose_patch",
                {"unified_diff": "not a patch", "worktree": "fixture", "heal_job_id": 1},
            )
        )

    assert tracer.patch_error == "git apply failed: malformed unified diff"
    assert tracer.events[-1]["ok"] is False


def test_exception_group_summary_records_sanitized_nested_cause() -> None:
    nested = TimeoutError("provider request timed out; token=do-not-record-this")
    error = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [ExceptionGroup("server task failed", [nested])],
    )

    summary = run_module._exception_chain_summary(error)

    assert "ExceptionGroup: unhandled errors in a TaskGroup" in summary
    assert "TimeoutError: provider request timed out" in summary
    assert "[REDACTED]" in summary
    assert "do-not-record-this" not in summary


async def test_metered_client_propagates_remaining_case_deadline() -> None:
    from types import SimpleNamespace

    class FakeClient:
        messages = None

        def __init__(self) -> None:
            self.messages = self
            self.request_kwargs: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> Any:
            self.request_kwargs = kwargs
            return SimpleNamespace(
                content=[],
                usage=SimpleNamespace(input_tokens=0, output_tokens=0, cost_usd=0),
            )

    fake = FakeClient()
    metered = run_module.MeteredClient(
        fake,
        run_module.UsageMeter(
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            allow_attested_free_cost=True,
        ),
    )
    deadline = asyncio.get_running_loop().time() + 15
    metered.set_request_deadline(deadline)

    await metered.create(model="offline")

    assert fake.request_kwargs is not None
    remaining = fake.request_kwargs["request_timeout_seconds"]
    assert 0 < remaining <= 15


async def test_zero_cost_provider_requires_cost_on_every_response() -> None:
    from types import SimpleNamespace

    class MissingCostClient:
        messages = None

        def __init__(self) -> None:
            self.messages = self

        async def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, cost_usd=None),
            )

    metered = run_module.MeteredClient(
        MissingCostClient(),
        run_module.UsageMeter(
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            allow_attested_free_cost=False,
        ),
    )
    with pytest.raises(run_module.BenchmarkStop, match="no free-tier attestation"):
        await metered.create(model="pinned-free-model")


async def test_provider_request_cap_prevents_a_second_call() -> None:
    from types import SimpleNamespace

    class CountingClient:
        messages = None

        def __init__(self) -> None:
            self.messages = self
            self.calls = 0

        async def create(self, **_kwargs: Any) -> Any:
            self.calls += 1
            return SimpleNamespace(
                content=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, cost_usd=0),
            )

    client = CountingClient()
    metered = run_module.MeteredClient(
        client,
        run_module.UsageMeter(
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            allow_attested_free_cost=False,
            request_limit=1,
        ),
    )
    await metered.create(model="pinned-free-model")
    with pytest.raises(run_module.BenchmarkStop, match="request cap"):
        await metered.create(model="pinned-free-model")
    assert client.calls == 1


async def test_case_token_meter_sums_usage_across_provider_responses() -> None:
    from types import SimpleNamespace

    class SequenceClient:
        messages = None

        def __init__(self) -> None:
            self.messages = self
            self.responses = iter(((3_000, 100), (2_500, 100), (2_485, 223)))

        async def create(self, **_kwargs: Any) -> Any:
            input_tokens, output_tokens = next(self.responses)
            return SimpleNamespace(
                content=[],
                usage=SimpleNamespace(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=0,
                ),
            )

    old_budget_meter = run_module.MeteredClient(
        SequenceClient(),
        run_module.UsageMeter(
            token_limit=8_000,
            cost_limit=run_module.Decimal("0"),
            allow_attested_free_cost=False,
        ),
    )
    await old_budget_meter.create(model="mock-free")
    await old_budget_meter.create(model="mock-free")
    with pytest.raises(run_module.BenchmarkStop, match="per-case token cap"):
        await old_budget_meter.create(model="mock-free")
    assert old_budget_meter._meter.input_tokens + old_budget_meter._meter.output_tokens == 8_408
    assert old_budget_meter._meter.request_usage == [
        {"request": 1, "input_tokens": 3_000, "output_tokens": 100, "cost_usd": "0"},
        {"request": 2, "input_tokens": 2_500, "output_tokens": 100, "cost_usd": "0"},
        {"request": 3, "input_tokens": 2_485, "output_tokens": 223, "cost_usd": "0"},
    ]

    proposed_budget_meter = run_module.MeteredClient(
        SequenceClient(),
        run_module.UsageMeter(
            token_limit=32_768,
            cost_limit=run_module.Decimal("0"),
            allow_attested_free_cost=False,
        ),
    )
    await proposed_budget_meter.create(model="mock-free")
    await proposed_budget_meter.create(model="mock-free")
    await proposed_budget_meter.create(model="mock-free")
    assert proposed_budget_meter._meter.input_tokens == 7_985
    assert proposed_budget_meter._meter.output_tokens == 423


async def test_metered_client_does_not_start_provider_call_after_case_deadline() -> None:
    class FakeClient:
        messages = None

        def __init__(self) -> None:
            self.messages = self
            self.called = False

        async def create(self, **_kwargs: Any) -> Any:
            self.called = True
            raise AssertionError("expired case must not reach the provider client")

    fake = FakeClient()
    metered = run_module.MeteredClient(
        fake,
        run_module.UsageMeter(
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            allow_attested_free_cost=True,
        ),
    )
    metered.set_request_deadline(asyncio.get_running_loop().time() - 1)

    with pytest.raises(TimeoutError, match="deadline expired before provider request"):
        await metered.create(model="offline")
    assert fake.called is False


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
    assert arm["target_tests_passed"] == 2
    assert arm["target_test_pass_rate"] == 1.0
    assert arm["resolved"] == 1
    assert arm["resolve_rate"] == 0.5
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
            "full_suite_pass": passed,
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
    frozen_cases = run_module._prepared_cases(
        prepared_root, case_ids=[kept_id, case_id("fixture", stale_sha)]
    )
    assert [metadata["case_id"] for _path, metadata, _record in frozen_cases] == [
        kept_id,
        case_id("fixture", stale_sha),
    ]
    with pytest.raises(ValueError, match="no longer prepared as kept"):
        run_module._prepared_cases(prepared_root, case_ids=[discarded_id])


def test_runner_configures_database_before_importing_cached_settings(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os
        import sys
        import tempfile
        from pathlib import Path

        from sqlalchemy.engine import make_url

        os.environ.pop("DATABASE_URL", None)
        os.environ["REAL_BUG_DATABASE_URL"] = (
            "postgresql+asyncpg://benchmark@127.0.0.1:5434/selfheal_real_bug_test"
        )
        from benchmarks.real_bugs import run as runner

        assert "core.config" not in sys.modules
        assert "healer.backend_chain" not in sys.modules

        project = Path(tempfile.mkdtemp())
        (project / "alembic").mkdir()
        (project / "alembic.ini").write_text("[alembic]\\nscript_location = alembic\\n")
        runner.ROOT = project / "benchmarks" / "real_bugs"
        runner._prepared_cases = lambda *_args, **_kwargs: []

        from alembic import command

        observed = []

        def verify_database(database_url):
            url = make_url(database_url)
            observed.append(("verify", url.host, url.port, url.database))

        runner._verify_database_connection = verify_database

        def verify_settings(_config, _revision):
            from core.config import settings

            url = make_url(settings.database_url)
            observed.append(("alembic", url.host, url.port, url.database))

        command.upgrade = verify_settings

        def verify_provider(_provider_name):
            from core.config import settings

            url = make_url(settings.database_url)
            observed.append(("runner", url.host, url.port, url.database))
            return object(), "fixture-model", True

        runner._provider = verify_provider
        rows = runner.run_benchmark(
            provider_name="gemini_api",
            run_file=project / "attempts.jsonl",
            token_limit=100,
            cost_limit=runner.Decimal("0"),
            timeout=1,
            resume=True,
        )
        expected = ("127.0.0.1", 5434, "selfheal_real_bug_test")
        assert rows == []
        assert observed == [
            ("runner", *expected),
            ("verify", *expected),
            ("alembic", *expected),
        ]
        assert os.environ["DATABASE_URL"] == os.environ["REAL_BUG_DATABASE_URL"]

        os.environ.pop("DATABASE_URL")
        runner._verify_database_connection = lambda _url: (_ for _ in ()).throw(
            RuntimeError("database identity unavailable")
        )
        try:
            runner.run_benchmark(
                provider_name="gemini_api",
                run_file=project / "blocked.jsonl",
                token_limit=100,
                cost_limit=runner.Decimal("0"),
                timeout=1,
                resume=True,
            )
        except RuntimeError as exc:
            assert "database identity unavailable" in str(exc)
        else:
            raise AssertionError("runner did not fail closed on database verification error")
        assert observed == [
            ("runner", *expected),
            ("verify", *expected),
            ("alembic", *expected),
            ("runner", *expected),
        ]
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_provider_attestation_precedes_database_verification_and_migrations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(run_module, "_prepared_cases", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run_module, "_configure_database", lambda: "isolated-db-url")

    def reject_unattested_provider(_provider_name: str) -> Any:
        calls.append("provider-check")
        raise RuntimeError("operator billing attestation missing")

    monkeypatch.setattr(run_module, "_provider", reject_unattested_provider)
    monkeypatch.setattr(
        run_module,
        "_verify_database_connection",
        lambda _url: calls.append("database-verification"),
    )
    monkeypatch.setattr(run_module, "_migrate_database", lambda: calls.append("migration"))

    with pytest.raises(RuntimeError, match="billing attestation missing"):
        run_module.run_benchmark(
            provider_name="gemini_api",
            run_file=tmp_path / "attempts.jsonl",
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            timeout=1,
            resume=True,
        )

    assert calls == ["provider-check"]


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


def test_groq_provider_fails_closed_on_unconfirmed_or_changed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import settings
    from healer import api_adapters

    marker = object()
    monkeypatch.setattr(api_adapters, "GroqClient", lambda: marker)
    monkeypatch.setattr(settings, "groq_api_model", "openai/gpt-oss-120b")
    monkeypatch.setenv("REAL_BUG_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("REAL_BUG_BILLING_DISABLED", "true")
    monkeypatch.setenv("GROQ_FREE_PLAN_CONFIRMED", "true")
    monkeypatch.setenv("GROQ_GPT_OSS_120B_FREE_QUOTA", "true")

    client, model, attested_free = run_module._provider("groq_api")

    assert client is marker
    assert model == "openai/gpt-oss-120b"
    assert attested_free is True

    monkeypatch.setattr(settings, "groq_api_model", "another-model")
    with pytest.raises(RuntimeError, match="frozen Groq benchmark requires"):
        run_module._provider("groq_api")


def test_openrouter_vimj_provider_is_exactly_allowlisted_and_preflighted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import settings
    from healer import api_adapters
    from scripts import openrouter_repair

    expected_model = "cohere/north-mini-code:free"
    marker = object()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(settings, "anthropic_model", expected_model)
    monkeypatch.setattr(api_adapters, "OpenRouterClient", lambda *, api_key: marker)
    monkeypatch.setattr(
        openrouter_repair,
        "verify_free_tier_key",
        lambda key: calls.append(("key", key)),
    )
    monkeypatch.setattr(
        openrouter_repair,
        "verify_free_model",
        lambda model, _key, **limits: calls.append(
            (
                "model",
                f"{model}:{limits['minimum_context_tokens']}:{limits['minimum_completion_tokens']}",
            )
        ),
    )

    def verify_endpoint(
        model: str,
        _key: str,
        *,
        required_parameter: str,
        minimum_context_tokens: int,
        minimum_completion_tokens: int,
    ) -> None:
        calls.append(
            (
                "endpoint",
                f"{model}:{required_parameter}:{minimum_context_tokens}:{minimum_completion_tokens}",
            )
        )

    monkeypatch.setattr(openrouter_repair, "verify_free_endpoint", verify_endpoint)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "x" * 32)
    monkeypatch.setenv("REAL_BUG_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("REAL_BUG_BILLING_DISABLED", "true")
    monkeypatch.setenv("OPENROUTER_FREE_TIER_CONFIRMED", "true")

    client, model, free_attested = run_module._provider("openrouter_api")

    assert client is marker
    assert model == expected_model
    assert free_attested is True
    assert calls == [
        ("key", "sk-or-v1-" + "x" * 32),
        ("model", f"{expected_model}:32768:4096"),
        ("endpoint", f"{expected_model}:tools:32768:4096"),
    ]

    for disallowed in (
        "openrouter/free",
        "google/gemma-4-26b-a4b-it:free",
        "google/gemma-4-26b-a4b-it",
        "vendor/paid-model",
    ):
        monkeypatch.setattr(settings, "anthropic_model", disallowed)
        with pytest.raises(RuntimeError, match="only allows"):
            run_module._provider("openrouter_api")


def test_daily_provider_quota_is_not_marked_resumable() -> None:
    from healer.api_adapters import GroqDailyQuotaError
    from healer.backend_chain import BackendCooldownError

    assert run_module._cooldown_failure_category(GroqDailyQuotaError()) == (
        False,
        "provider_error",
    )
    assert run_module._cooldown_failure_category(
        BackendCooldownError("temporary TPM", status_code=429)
    ) == (True, "rate_limited")


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


def test_resume_skips_attempted_case_arm_and_limit_counts_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_arm(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["arm"])
        return {
            "case_id": "fixture-case",
            "arm": kwargs["arm"],
            "failure_category": "tests_still_failing",
            "retryable": False,
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(run_module, "_run_arm", fake_arm)
    run_file = tmp_path / "partial.jsonl"
    run_file.write_text(
        json.dumps(
            {
                "case_id": "fixture-case",
                "arm": "with_validation",
                "failure_category": "rate_limited",
                "rate_limited": True,
                "retryable": True,
                "rate_limit_attempt": 1,
                "recorded_at": "2000-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata = {"case_id": "fixture-case", "parent_sha": "a" * 40, "repository": "fixture"}

    rows = asyncio.run(
        run_module._run_cases(
            cases=[(tmp_path, metadata, {})],
            provider_name="gemini_api",
            run_file=run_file,
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            timeout_seconds=10,
            resume=True,
            provider_client=object(),
            model="fixture-model",
            attested_free=True,
            limit=2,
        )
    )

    assert calls == ["without_validation"]
    assert len(rows) == 1
    assert len(run_file.read_text(encoding="utf-8").splitlines()) == 2


def test_score_reader_preserves_every_attempt_row(tmp_path: Path) -> None:
    run_file = tmp_path / "attempts.jsonl"
    first = {"case_id": "same-case", "arm": "with_validation", "status": "failed"}
    second = {"case_id": "same-case", "arm": "with_validation", "status": "resolved"}
    run_file.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")

    rows = score_module._read_rows([run_file])

    assert [row["status"] for row in rows] == ["failed", "resolved"]


def test_openrouter_daily_budget_counts_every_same_day_record(tmp_path: Path) -> None:
    run_file = tmp_path / "attempts.jsonl"
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    records = [
        {
            "case_id": "same-case",
            "arm": "with_validation",
            "provider_request_count": 10,
            "recorded_at": "2026-11-10T10:00:00+00:00",
        },
        {
            "case_id": "same-case",
            "arm": "with_validation",
            "provider_request_count": 4,
            "recorded_at": "2026-11-10T11:00:00+00:00",
        },
        {
            "case_id": "older-case",
            "arm": "with_validation",
            "provider_request_count": 20,
            "recorded_at": "2026-11-09T23:59:00+00:00",
        },
    ]
    run_file.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    (tmp_path / "second-run.jsonl").write_text(
        json.dumps(
            {
                "case_id": "another-case",
                "arm": "without_validation",
                "provider_request_count": 2,
                "recorded_at": "2026-11-10T11:30:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert run_module._openrouter_requests_used_today(run_file, now=now) == 14
    assert run_module._openrouter_requests_used_today(tmp_path, now=now) == 16


def test_openrouter_resumed_run_passes_only_remaining_daily_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_limits: list[int | None] = []

    async def fake_arm(**kwargs: Any) -> dict[str, Any]:
        seen_limits.append(kwargs["request_limit"])
        return {
            "case_id": kwargs["metadata"]["case_id"],
            "arm": kwargs["arm"],
            "provider_request_count": int(kwargs["request_limit"] or 0),
            "failure_category": "request_cap",
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(run_module, "_run_arm", fake_arm)
    run_file = tmp_path / "partial.jsonl"
    prior = {
        "case_id": "fixture-case",
        "arm": "with_validation",
        "provider_request_count": 10,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    run_file.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    metadata = {"case_id": "fixture-case", "parent_sha": "a" * 40, "repository": "fixture"}

    rows = asyncio.run(
        run_module._run_cases(
            cases=[(tmp_path, metadata, {})],
            provider_name="openrouter_api",
            run_file=run_file,
            token_limit=100,
            cost_limit=run_module.Decimal("0"),
            timeout_seconds=10,
            resume=True,
            provider_client=object(),
            model="fixture-model",
            attested_free=True,
        )
    )

    assert seen_limits == [run_module.OPENROUTER_BENCHMARK_DAILY_REQUEST_BUDGET - 10]
    assert len(rows) == 1


def test_model_tool_wrapper_rejects_git_history_tools() -> None:
    class FakeClient:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise AssertionError("unauthorized MCP tool reached the server")

    trace = TracingMCPClient(FakeClient(), validated=True, error_id=42)
    with pytest.raises(ValueError, match="not authorized"):
        asyncio.run(trace.call_tool("get_recent_commits", {}))


def test_model_tool_wrapper_maps_only_the_observed_repo_browser_alias() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def list_tool_schemas(self) -> list[dict[str, Any]]:
            return [
                {"name": "list_files", "input_schema": {"type": "object"}},
                {"name": "get_recent_commits", "input_schema": {"type": "object"}},
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            return {"paths": ["src/example.py"]}

    client = FakeClient()
    trace = TracingMCPClient(client, validated=True, error_id=42)
    schemas = asyncio.run(trace.list_tool_schemas())
    arguments = {"glob": "src/**/*.py"}
    result = asyncio.run(trace.call_tool("repo_browser.list_files", arguments))

    assert [schema["name"] for schema in schemas] == ["list_files"]
    assert result == {"paths": ["src/example.py"]}
    assert client.calls == [("list_files", arguments)]
    assert trace.events == [
        {
            "tool": "list_files",
            "requested_tool": "repo_browser.list_files",
            "ok": True,
        }
    ]


@pytest.mark.parametrize(
    "tool_name",
    ["other_namespace.list_files", "repo_browser.run_tests", "repo_browser.delete_file"],
)
def test_model_tool_wrapper_rejects_unknown_or_malicious_namespaces(tool_name: str) -> None:
    class FakeClient:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise AssertionError("unauthorized MCP tool reached the server")

    trace = TracingMCPClient(FakeClient(), validated=True, error_id=42)
    with pytest.raises(ValueError, match="not authorized"):
        asyncio.run(trace.call_tool(tool_name, {}))


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
