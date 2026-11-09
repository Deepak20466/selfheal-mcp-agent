from __future__ import annotations

# Ruff versions disagree about the pytest import grouping in this split test module.
# ruff: noqa: I001

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

# isort: split

from benchmarks.real_bugs import unresolved_eval as evaluation
from healer.mcp_client import MCPToolClient
from healer.prompts import build_initial_messages
from healer.runtime_agent import _run_one_attempt
from mcp_server.sandbox import SandboxViolation


def _diff(path: str = "app/routers/fees.py") -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old_value = 1\n+new_value = 2\n"


@pytest.mark.asyncio
async def test_attempt_worktree_and_branch_fit_deep_windows_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise clone and checkout beneath a path that stresses MAX_PATH."""
    from benchmarks.real_bugs import run as benchmark_runner
    from benchmarks.real_bugs.run import _allocate_attempt_workspace, _benchmark_attempt_id
    from healer.worktree import create_worktree_for_connected_app, remove_plain_clone

    project = tmp_path.joinpath(*(f"nested-project-{index:02d}" for index in range(3)))
    project.mkdir(parents=True)
    worktrees_root = project / "worktrees"
    import mcp_server.sandbox as sandbox

    monkeypatch.setattr(sandbox, "WORKTREES_ROOT", worktrees_root)
    workspace = _allocate_attempt_workspace(
        case_id_value=evaluation.CASE_ID,
        arm=evaluation.ARM,
        repository_root=project,
        worktrees_root=worktrees_root,
    )
    source = tmp_path / "source-repository"
    source.mkdir()

    async def git(*args: str, cwd: Path) -> None:
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode(errors="replace")

    await git("init", "-b", "main", cwd=source)
    await git("config", "user.email", "test@example.com", cwd=source)
    await git("config", "user.name", "Test", cwd=source)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    await git("add", "-A", cwd=source)
    await git("commit", "-m", "initial", cwd=source)

    branch = f"benchmark/{_benchmark_attempt_id(evaluation.CASE_ID, evaluation.ARM)}"
    try:
        clone = await create_worktree_for_connected_app(
            workspace.worktree_name, branch, source_dir=source
        )
        assert clone.is_dir()
        assert (clone / "app.py").is_file()
        assert (
            clone / ".git" / "refs" / "heads" / "benchmark" / branch.rsplit("/", 1)[1]
        ).is_file()
        assert (
            len(
                str(
                    clone
                    / ".git"
                    / "refs"
                    / "heads"
                    / "benchmark"
                    / f"{branch.rsplit('/', 1)[1]}.lock"
                )
            )
            < 260
        )
        assert len(str(clone)) < 240
        assert len(workspace.worktree_name) <= 52
        assert len(branch) <= 27
    finally:
        await remove_plain_clone(workspace.worktree_name)
        benchmark_runner._cleanup_owned_path(
            workspace.root, workspace.root_marker, workspace.owner_token
        )


def test_frozen_config_and_model_prompt_have_no_evaluator_finding() -> None:
    config = json.loads(evaluation.FREEZE_PATH.read_text(encoding="utf-8"))
    evaluation.validate_model_config(config)
    model_freeze = json.loads(evaluation.MODEL_VISIBLE_FREEZE_PATH.read_text(encoding="utf-8"))
    prompt = evaluation.build_model_prompt()
    assert "invoice update leaves balance stale" not in prompt.lower()
    assert "expected fix" not in prompt.lower()
    assert "root_cause" not in json.dumps(config).lower()
    assert "human_fix" not in json.dumps(config).lower()
    assert "root_cause" not in json.dumps(model_freeze).lower()
    assert "human_fix" not in json.dumps(model_freeze).lower()


def test_new_budget_is_versioned_and_keeps_the_frozen_challenge_unchanged() -> None:
    from healer.api_adapters import OPENROUTER_TOOL_RESULT_MAX_BYTES

    budget = json.loads(evaluation.BUDGET_CONFIG_PATH.read_text(encoding="utf-8"))
    evaluation.validate_budget_config(budget)
    context_policy = json.loads(evaluation.CONTEXT_POLICY_PATH.read_text(encoding="utf-8"))
    evaluation.validate_context_policy(context_policy)
    from benchmarks.real_bugs import run as benchmark_runner

    challenge = json.loads(evaluation.FREEZE_PATH.read_text(encoding="utf-8"))
    evaluation.validate_model_config(challenge)
    assert evaluation.EVALUATION_ID != evaluation.CHALLENGE_ID
    assert context_policy["evaluation_id"] == evaluation.EVALUATION_ID
    assert context_policy["tool_result_max_bytes"] == OPENROUTER_TOOL_RESULT_MAX_BYTES
    assert budget["per_case_token_cap"] == 32_768
    assert budget["per_case_request_cap"] == 10
    assert budget["per_case_request_cap"] == benchmark_runner.OPENROUTER_BENCHMARK_REQUEST_LIMIT
    assert budget["per_case_cost_cap_usd"] == "0"
    assert (
        evaluation.sha256_file(evaluation.BUDGET_CONFIG_PATH)
        == evaluation.EXPECTED_BUDGET_CONFIG_SHA256
    )
    assert (
        evaluation.sha256_file(evaluation.CONTEXT_POLICY_PATH)
        == evaluation.EXPECTED_CONTEXT_POLICY_SHA256
    )


@pytest.mark.asyncio
async def test_exact_scope_normalizes_error_paths_and_deduplicates_snapshot_prefix() -> None:
    from healer.mcp_client import MCPToolError

    prefix = "connected_apps/mock/s"

    class EchoMCP:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def list_tool_schemas(self) -> list[dict[str, Any]]:
            return []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            if name == "get_error":
                return {
                    "file_path": f"{prefix}/{evaluation.SOURCE_PATH}",
                    "traceback": f"failed in {prefix}/{evaluation.SOURCE_PATH}\nassertion",
                }
            if name == "read_file" and arguments["path"].endswith("missing.py"):
                raise MCPToolError(f"Not a file: {arguments['path']!r}")
            return {"path": arguments["path"], "content": "repair source"}

    client = EchoMCP()
    tracer = evaluation.ExactScopeTracingMCPClient(
        client,
        validated=True,
        error_id=3,
        allowed_paths={evaluation.SOURCE_PATH},
        workspace_prefix=prefix,
        worktree_name="mock-run",
    )
    error = await tracer.call_tool("get_error", {"error_id": 3})
    assert error["file_path"] == evaluation.SOURCE_PATH
    assert prefix not in error["traceback"]
    assert len(error["traceback"]) <= evaluation.MODEL_VISIBLE_TRACEBACK_CHARS

    await tracer.call_tool("read_file", {"path": f"{prefix}/{evaluation.SOURCE_PATH}"})
    assert client.calls[-1][1]["path"] == f"{prefix}/{evaluation.SOURCE_PATH}"
    assert tracer.events[-1]["request"]["path"] == evaluation.SOURCE_PATH

    with pytest.raises(MCPToolError, match="repository-relative path") as read_error:
        await tracer.call_tool("read_file", {"path": "app/routers/missing.py"})
    assert prefix not in str(read_error.value)
    assert prefix not in tracer.events[-1]["error"]

    before = len(client.calls)
    with pytest.raises(evaluation.EvaluationSafetyError, match="repeated the workspace prefix"):
        await tracer.call_tool("read_file", {"path": f"{prefix}/{prefix}/{evaluation.SOURCE_PATH}"})
    assert len(client.calls) == before


def test_exact_source_path_is_accepted(tmp_path: Path) -> None:
    from mcp_server.sandbox import check_diff_paths_writable

    worktree = tmp_path / "worktree"
    target = worktree / "app" / "routers" / "fees.py"
    target.parent.mkdir(parents=True)
    target.write_text("old_value = 1\n", encoding="utf-8")
    changed = evaluation.validate_exact_patch(
        _diff(), allowed_paths={"app/routers/fees.py"}, worktree=worktree
    )
    assert changed == {"app/routers/fees.py"}
    assert check_diff_paths_writable(_diff(), workspace_root=worktree) == changed


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_requested_changes.py",
        "app/routers/fees.pyx",
        "../../outside.py",
        "C:/outside.py",
        ".env",
        "benchmarks/real_bugs/unresolved/vimj-fee-balance-v1.json",
    ],
)
def test_exact_scope_rejects_tests_traversal_secrets_and_metadata(
    tmp_path: Path, path: str
) -> None:
    with pytest.raises((evaluation.EvaluationSafetyError, SandboxViolation)):
        evaluation.validate_exact_patch(
            _diff(path), allowed_paths={"app/routers/fees.py"}, worktree=tmp_path
        )


def test_patch_to_symlink_is_rejected(tmp_path: Path) -> None:
    from mcp_server.sandbox import check_diff_paths_writable

    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    (outside / "fees.py").write_text("old_value = 1\n", encoding="utf-8")
    (worktree / "app").mkdir()
    (worktree / "app" / "routers").mkdir()
    link = worktree / "app" / "routers" / "fees.py"
    try:
        link.symlink_to(outside / "fees.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this host: {exc}")
    with pytest.raises(SandboxViolation, match="symlink"):
        check_diff_paths_writable(
            _diff(), allowed_prefixes=["app/routers/fees.py"], workspace_root=worktree
        )


def test_model_workspace_rejects_secret_and_evaluator_material(tmp_path: Path) -> None:
    workspace = tmp_path / "model"
    workspace.mkdir()
    (workspace / "app.py").write_text("safe = True\n", encoding="utf-8")
    evaluation.audit_model_workspace(workspace)
    (workspace / "evaluator-only-findings.txt").write_text("private", encoding="utf-8")
    with pytest.raises(evaluation.EvaluationSafetyError, match="evaluation-only"):
        evaluation.audit_model_workspace(workspace)


def test_copy_filter_excludes_git_secrets_and_credentials(tmp_path: Path) -> None:
    from benchmarks.real_bugs.unresolved_eval import _copy_ignore

    names = [".git", ".env", ".env.example", ".npmrc", "secrets", "app.py"]
    ignored = _copy_ignore(str(tmp_path), names)
    assert {".git", ".env", ".npmrc", "secrets"}.issubset(ignored)
    assert ".env.example" not in ignored
    assert "app.py" not in ignored


def test_candidate_must_be_outside_selfheal_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_server.sandbox as sandbox

    repository = tmp_path / "selfheal"
    repository.mkdir()
    candidate = repository / "candidate"
    candidate.mkdir()
    monkeypatch.setattr(sandbox, "REPO_ROOT", repository)
    with pytest.raises(evaluation.EvaluationSafetyError, match="outside Selfheal"):
        evaluation._validate_candidate(candidate)


def test_evidence_integrity_is_verifiable_and_path_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = tmp_path / "results" / "unresolved"
    private_root.mkdir(parents=True)
    monkeypatch.setattr(evaluation, "RESULT_ROOT", tmp_path / "results")
    path = evaluation._ensure_private_evidence_path(private_root / "one.jsonl")
    evaluation._write_evidence(path, {"status": "started", "provider_calls": 0}, create=True)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    integrity = row.pop("integrity_sha256")
    serialized = json.dumps(row, sort_keys=True, separators=(",", ":"))
    assert integrity == hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    row["integrity_sha256"] = integrity
    assert evaluation.verify_evidence_record(row)
    row["status"] = "tampered"
    assert not evaluation.verify_evidence_record(row)
    with pytest.raises(evaluation.EvaluationSafetyError, match="protected results"):
        evaluation._ensure_private_evidence_path(tmp_path / "elsewhere.jsonl")


def test_mock_execution_environment_contains_no_provider_or_database_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_server.execution_sandbox import _safe_child_environment

    monkeypatch.setenv("GROQ_API_KEY", "mock-only-value")
    monkeypatch.setenv("OPENROUTER_API_KEY", "mock-only-openrouter-value")
    monkeypatch.setenv("REAL_BUG_DATABASE_URL", "mock-only-database-url")
    monkeypatch.setenv("PGPASSWORD", "mock-only-password")
    monkeypatch.setenv("GROQ_QUOTA_CHECKED_UTC", "operator-only-timestamp")
    child = _safe_child_environment(tmp_path, "python -m pytest")
    assert not {
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "REAL_BUG_DATABASE_URL",
        "PGPASSWORD",
    }.intersection(child)
    assert "GROQ_QUOTA_CHECKED_UTC" not in child


@pytest.mark.asyncio
async def test_actual_selfheal_loop_runs_with_mock_provider_and_exact_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respx_mock: Any
) -> None:
    import structlog

    import mcp_server.sandbox as sandbox
    from benchmarks.real_bugs import run as runner
    from core.config import settings
    from healer.api_adapters import OPENROUTER_API_BASE, OpenRouterClient
    from healer.mcp_client import MCPToolError

    worktrees = tmp_path / "worktrees"
    worktree = worktrees / "mock-run"
    target = worktree / "app" / "routers" / "fees.py"
    target.parent.mkdir(parents=True)
    target.write_text("old_value = 1\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "WORKTREES_ROOT", worktrees)

    class FakeMCP:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.tests = 0

        async def list_tool_schemas(self) -> list[dict[str, Any]]:
            return [
                {"name": "read_file"},
                {"name": "list_files"},
                {"name": "run_tests"},
                {"name": "propose_patch"},
                {"name": "deploy"},
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            if name == "run_tests":
                self.tests += 1
                return {"passed": self.tests > 1, "output": "mock test evidence"}
            if name == "read_file":
                return {"content": "safe candidate source"}
            if name == "list_files":
                return ["connected_apps/mock/s/app.py"]
            return {"applied": True, "diff_stat": "1 file changed"}

    def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def provider_response(message: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": evaluation.OPENROUTER_MODEL,
            "choices": [{"message": message}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "cost": 0,
                "is_byok": False,
            },
        }

    monkeypatch.setattr(settings, "anthropic_model", evaluation.OPENROUTER_MODEL)
    response_queue = [
        provider_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call(
                        "r1",
                        "read_file",
                        {"path": "app/routers/fees.py", "worktree": "mock-run"},
                    ),
                    tool_call(
                        "t1",
                        "run_tests",
                        {"test_path": evaluation.TEST_PATH, "worktree": "mock-run"},
                    ),
                ],
            }
        ),
        provider_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call(
                        "p1",
                        "propose_patch",
                        {"unified_diff": _diff(), "worktree": "mock-run"},
                    ),
                    tool_call(
                        "t2",
                        "run_tests",
                        {"test_path": evaluation.TEST_PATH, "worktree": "mock-run"},
                    ),
                ],
            }
        ),
        provider_response({"role": "assistant", "content": "Mock repair complete."}),
    ]
    provider_routes = respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        side_effect=lambda _request: httpx.Response(200, json=response_queue.pop(0))
    )

    fake_mcp = FakeMCP()
    original_tracer_factory = runner.TracingMCPClient

    def scoped_tracer(client: Any, *, validated: bool, error_id: int) -> Any:
        return evaluation.ExactScopeTracingMCPClient(
            client,
            validated=validated,
            error_id=error_id,
            allowed_paths={"app/routers/fees.py"},
            workspace_prefix="connected_apps/mock/s",
            worktree_name="mock-run",
            tracer_factory=original_tracer_factory,
        )

    # Match unresolved_eval._execute's dynamic class replacement. The exact
    # scope wrapper must retain the original tracer factory after this patch.
    monkeypatch.setattr(runner, "TracingMCPClient", scoped_tracer)
    tracer = runner.TracingMCPClient(fake_mcp, validated=True, error_id=7)
    schemas = await tracer.list_tool_schemas()
    assert [schema["name"] for schema in schemas] == [
        "read_file",
        "list_files",
        "run_tests",
        "propose_patch",
    ]
    fake_provider = OpenRouterClient(api_key="sk-or-fake")
    with structlog.testing.capture_logs() as diagnostic_logs:
        result = await _run_one_attempt(
            anthropic_client=fake_provider,
            mcp=cast(MCPToolClient, tracer),
            job_id=7,
            worktree_name="mock-run",
            messages=build_initial_messages(
                source_kind="error",
                source={"message": "frozen regression test fails"},
                attempt_number=1,
                max_attempts=1,
                heal_job_id=7,
                worktree="mock-run",
                previous_attempts_summary=None,
            ),
            tool_schemas=schemas,
            system_prompt=evaluation.build_model_prompt(),
            max_total_tokens=100,
            model_name="mock-only",
        )
    assert result.success is True
    assert isinstance(tracer._tracer, original_tracer_factory)
    assert provider_routes.call_count == 3
    tool_diagnostics = [
        entry
        for entry in diagnostic_logs
        if entry.get("event") == "provider.tool_result_diagnostic"
    ]
    assert {entry["tool"] for entry in tool_diagnostics} == {
        "read_file",
        "run_tests",
        "propose_patch",
    }
    assert all(entry["failure_category"] is None for entry in tool_diagnostics)
    assert all(entry["payload_bytes"] > 0 for entry in tool_diagnostics)
    called_tools = [name for name, _ in fake_mcp.calls]
    assert fake_mcp.tests >= 2
    assert called_tools.count("read_file") >= 1
    assert called_tools.count("propose_patch") == 1
    assert called_tools.count("run_tests") >= 2
    assert set(called_tools).issubset({"read_file", "list_files", "run_tests", "propose_patch"})
    assert (await tracer.call_tool("read_file", {"path": "app.py"}))["content"] == (
        "safe candidate source"
    )
    assert await tracer.call_tool("list_files", {"glob": "**/*.py"}) == ["app.py"]
    assert fake_mcp.calls[-2][1]["path"] == "connected_apps/mock/s/app.py"
    assert fake_mcp.calls[-1][1]["glob"] == "connected_apps/mock/s/**/*.py"
    before_rejected_calls = len(fake_mcp.calls)
    for tool_name, arguments in (
        ("read_file", {"path": "../../benchmarks/real_bugs/unresolved/vimj-fee-balance-v1.json"}),
        ("read_file", {"path": "../tests/test_unresolved_real_bug_eval.py"}),
        ("list_files", {"glob": "../**/*.py"}),
        ("list_files", {"glob": "app/../**/*.py"}),
    ):
        with pytest.raises(evaluation.EvaluationSafetyError):
            await tracer.call_tool(tool_name, arguments)
    with pytest.raises(MCPToolError, match="frozen regression and full-suite"):
        await tracer.call_tool(
            "run_tests",
            {"worktree": "mock-run", "test_path": "python -m pytest -q tests/test_secret.py"},
        )
    assert len(fake_mcp.calls) == before_rejected_calls
    rejected_event = tracer.events[-1]
    assert rejected_event["tool"] == "run_tests"
    assert rejected_event["ok"] is False
    assert rejected_event["request"]["test_path"] == "python -m pytest -q tests/test_secret.py"
    with pytest.raises(evaluation.EvaluationSafetyError, match="exact source allowlist"):
        await tracer.call_tool(
            "propose_patch",
            {"unified_diff": _diff("tests/test_fee.py"), "worktree": "mock-run"},
        )
    with pytest.raises(ValueError, match="not authorized"):
        await tracer.call_tool(
            "attacker.propose_patch",
            {"unified_diff": _diff(), "worktree": "mock-run"},
        )
    assert len(fake_mcp.calls) == before_rejected_calls
    first_payload = json.loads(provider_routes.calls[0].request.content or b"{}")
    system_text = " ".join(
        message["content"] for message in first_payload["messages"] if message["role"] == "system"
    )
    user_text = repr(first_payload["messages"])
    model_context = system_text + user_text
    assert "invoice update leaves balance stale" not in model_context.lower()
    assert "expected fix" not in model_context.lower()


@pytest.mark.asyncio
async def test_offline_agent_executes_real_patch_tool_and_independent_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respx_mock: Any
) -> None:
    import shutil
    import subprocess
    import sys
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import mcp_server.audit as audit
    import mcp_server.sandbox as sandbox
    import mcp_server.tools.code as code_tools
    from benchmarks.real_bugs import run as runner
    from core.config import settings
    from core.models import HealJob, HealJobType, MonitoredApp
    from healer.api_adapters import OPENROUTER_API_BASE, OpenRouterClient
    from healer.mcp_client import connect_in_memory
    from mcp_server.instance import mcp

    worktrees = tmp_path / "worktrees"
    worktree = worktrees / "mock-repair"
    source = worktree / "app" / "routers" / "fees.py"
    test_file = worktree / "tests" / "test_requested_changes.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def balance(amount, paid):\n    return amount + paid\n", encoding="utf-8")
    test_file.write_text(
        "from app.routers.fees import balance\n\n"
        "class TestSyntheticFullSuite:\n"
        "    def test_balance_uses_paid_amount(self):\n"
        "        assert balance(100, 25) == 75\n\n"
        "class RequestedChanges:\n"
        "    def test_updating_unpaid_fee_amount_keeps_outstanding_balance_in_sync(self):\n"
        "        assert balance(100, 25) == 75\n",
        encoding="utf-8",
    )
    (worktree / "pytest.ini").write_text(
        "[pytest]\npython_classes = Test* RequestedChanges\n", encoding="utf-8"
    )
    (worktree / "app" / "__init__.py").write_text("", encoding="utf-8")
    (worktree / "app" / "routers" / "__init__.py").write_text("", encoding="utf-8")
    for args in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.name", "Offline fixture"],
        ["git", "config", "user.email", "offline@example.invalid"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "synthetic failing baseline"],
    ):
        await asyncio.to_thread(
            subprocess.run, args, cwd=worktree, check=True, capture_output=True, text=True
        )

    repo_root = tmp_path / "mcp-root"
    workspace_prefix = "connected_apps/mock/s"
    snapshot = repo_root / workspace_prefix
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(worktree, snapshot, ignore=shutil.ignore_patterns(".git"))
    monkeypatch.setattr(sandbox, "REPO_ROOT", repo_root)
    monkeypatch.setattr(sandbox, "WORKTREES_ROOT", worktrees)
    monkeypatch.setattr(code_tools, "REPO_ROOT", repo_root)

    app = SimpleNamespace(
        allowed_write_paths=["app/routers/fees.py"],
        language="python",
        test_command=f'"{sys.executable}" -m pytest -q',
    )
    job = SimpleNamespace(type=HealJobType.RUNTIME_ERROR, app_id=42)

    class FakeSession:
        def add(self, _record: Any) -> None:
            return None

        async def get(self, model: Any, _identity: int) -> Any:
            if model is HealJob:
                return job
            if model is MonitoredApp:
                return app
            return None

    @asynccontextmanager
    async def fake_session_scope() -> Any:
        yield FakeSession()

    monkeypatch.setattr(code_tools, "session_scope", fake_session_scope)
    monkeypatch.setattr(audit, "session_scope", fake_session_scope)

    def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def response(message: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": evaluation.OPENROUTER_MODEL,
                "choices": [{"message": message}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "cost": 0,
                    "is_byok": False,
                },
            },
        )

    diff = (
        "--- a/app/routers/fees.py\n+++ b/app/routers/fees.py\n"
        "@@ -1,2 +1,2 @@\n def balance(amount, paid):\n"
        "-    return amount + paid\n+    return amount - paid\n"
    )
    provider_responses = [
        response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call(
                        "read-1",
                        "read_file",
                        {"path": "app/routers/fees.py", "start_line": 1, "end_line": 10},
                    ),
                    tool_call(
                        "test-before",
                        "run_tests",
                        {
                            "worktree": "mock-repair",
                            "test_path": "python -m pytest -q tests/test_requested_changes.py",
                        },
                    ),
                    tool_call(
                        "test-baseline",
                        "run_tests",
                        {"worktree": "mock-repair", "test_path": evaluation.TEST_PATH},
                    ),
                ],
            }
        ),
        response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call(
                        "patch-1",
                        "propose_patch",
                        {"worktree": "mock-repair", "unified_diff": diff},
                    ),
                    tool_call(
                        "test-after",
                        "run_tests",
                        {"worktree": "mock-repair", "test_path": evaluation.TEST_PATH},
                    ),
                ],
            }
        ),
        response({"role": "assistant", "content": "Synthetic repair verified."}),
    ]
    provider_route = respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        side_effect=lambda _request: provider_responses.pop(0)
    )
    monkeypatch.setattr(settings, "anthropic_model", evaluation.OPENROUTER_MODEL)

    async with connect_in_memory(mcp) as mcp_client:
        original_tracer = runner.TracingMCPClient
        scoped = evaluation.ExactScopeTracingMCPClient(
            mcp_client,
            validated=True,
            error_id=7,
            allowed_paths={evaluation.SOURCE_PATH},
            workspace_prefix=workspace_prefix,
            worktree_name="mock-repair",
            tracer_factory=original_tracer,
        )
        agent_result = await _run_one_attempt(
            anthropic_client=OpenRouterClient(api_key="sk-or-fake"),
            mcp=cast(MCPToolClient, scoped),
            job_id=7,
            worktree_name="mock-repair",
            messages=build_initial_messages(
                source_kind="error",
                source={
                    "file_path": evaluation.SOURCE_PATH,
                    "message": "synthetic regression fails",
                },
                attempt_number=1,
                max_attempts=1,
                heal_job_id=7,
                worktree="mock-repair",
                previous_attempts_summary=None,
            ),
            tool_schemas=await scoped.list_tool_schemas(),
            system_prompt=evaluation.build_model_prompt(),
            max_total_tokens=10_000,
            model_name=evaluation.OPENROUTER_MODEL,
        )

        assert agent_result.success is True
        assert provider_route.call_count == 3
        assert source.read_text(encoding="utf-8").endswith("return amount - paid\n")
        assert any(
            event.get("tool") == "propose_patch" and event.get("ok") for event in scoped.events
        )
        rejected_tests = [
            event
            for event in scoped.events
            if event.get("tool") == "run_tests" and event.get("ok") is False
        ]
        assert len(rejected_tests) == 1
        assert rejected_tests[0]["request"]["test_path"] == (
            "python -m pytest -q tests/test_requested_changes.py"
        )
        assert any(event.get("tool") == "run_tests" and event.get("ok") for event in scoped.events)

    independent = await code_tools.run_tests(worktree="mock-repair", test_path=evaluation.TEST_PATH)
    assert independent["passed"] is True
