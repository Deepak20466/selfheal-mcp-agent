from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from benchmarks.real_bugs import unresolved_eval as evaluation
from healer.mcp_client import MCPToolClient
from healer.prompts import build_initial_messages
from healer.runtime_agent import _run_one_attempt
from mcp_server.sandbox import SandboxViolation


def _diff(path: str = "app/routers/fees.py") -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old_value = 1\n+new_value = 2\n"


def test_frozen_config_and_model_prompt_have_no_evaluator_finding() -> None:
    config = json.loads(evaluation.FREEZE_PATH.read_text(encoding="utf-8"))
    evaluation.validate_model_config(config)
    prompt = evaluation.build_model_prompt()
    assert "invoice update leaves balance stale" not in prompt.lower()
    assert "expected fix" not in prompt.lower()
    assert "root_cause" not in json.dumps(config).lower()
    assert "human_fix" not in json.dumps(config).lower()


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
    monkeypatch.setenv("REAL_BUG_DATABASE_URL", "mock-only-database-url")
    monkeypatch.setenv("PGPASSWORD", "mock-only-password")
    monkeypatch.setenv("GROQ_QUOTA_CHECKED_UTC", "operator-only-timestamp")
    child = _safe_child_environment(tmp_path, "python -m pytest")
    assert not {"GROQ_API_KEY", "REAL_BUG_DATABASE_URL", "PGPASSWORD"}.intersection(child)
    assert "GROQ_QUOTA_CHECKED_UTC" not in child


@pytest.mark.asyncio
async def test_actual_selfheal_loop_runs_with_mock_provider_and_exact_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_server.sandbox as sandbox

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

    class Block:
        def __init__(self, name: str, arguments: dict[str, Any], call_id: str) -> None:
            self.type = "tool_use"
            self.name = name
            self.input = arguments
            self.id = call_id

    class FakeMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.responses = [
                [
                    Block(
                        "run_tests",
                        {"test_path": evaluation.TEST_PATH, "worktree": "mock-run"},
                        "t1",
                    )
                ],
                [
                    Block("propose_patch", {"unified_diff": _diff(), "worktree": "mock-run"}, "p1"),
                    Block(
                        "run_tests",
                        {"test_path": evaluation.TEST_PATH, "worktree": "mock-run"},
                        "t2",
                    ),
                ],
                [SimpleNamespace(type="text", text="Mock repair complete.")],
            ]

        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            blocks = self.responses.pop(0)
            return SimpleNamespace(
                content=blocks,
                usage=SimpleNamespace(input_tokens=5, output_tokens=3),
            )

    fake_mcp = FakeMCP()
    tracer = evaluation.ExactScopeTracingMCPClient(
        fake_mcp,
        validated=True,
        error_id=7,
        allowed_paths={"app/routers/fees.py"},
        workspace_prefix="connected_apps/mock/s",
        worktree_name="mock-run",
    )
    schemas = await tracer.list_tool_schemas()
    assert [schema["name"] for schema in schemas] == [
        "read_file",
        "list_files",
        "run_tests",
        "propose_patch",
    ]
    fake_provider = SimpleNamespace(messages=FakeMessages())
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
    assert fake_mcp.tests == 3
    assert [name for name, _ in fake_mcp.calls] == [
        "run_tests",
        "propose_patch",
        "run_tests",
        "run_tests",
    ]
    assert all(name != "deploy" for name, _ in fake_mcp.calls)
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
        ("run_tests", {"worktree": "another-worktree", "test_path": evaluation.TEST_PATH}),
        ("run_tests", {"worktree": "mock-run", "test_path": "tests/test_secret.py"}),
    ):
        with pytest.raises(evaluation.EvaluationSafetyError):
            await tracer.call_tool(tool_name, arguments)
    assert len(fake_mcp.calls) == before_rejected_calls
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
    system_text = fake_provider.messages.calls[0]["system"][0]["text"]
    user_text = repr(fake_provider.messages.calls[0]["messages"])
    model_context = system_text + user_text
    assert "invoice update leaves balance stale" not in model_context.lower()
    assert "expected fix" not in model_context.lower()
