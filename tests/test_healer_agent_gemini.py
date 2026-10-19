"""healer.agent_gemini: the (documented-only, not live-verified) Google
Gemini CLI backend.

Mirrors `tests/test_healer_agent_free.py`'s structure: `run_gemini_cli` is
tested against a mocked subprocess (never a real `gemini` call, per
CLAUDE.md), covering success, timeout, an error result, "not logged in",
"usage limit", CLI-not-found, and malformed (non-JSON) output, plus that the
isolated `.gemini/settings.json` restriction file is written outside the
repository and removed after the call.
The higher-level loop (`run_heal_job_gemini`) is tested end to end against a
real MCP server, real git worktrees and a throwaway git remote, with
`run_gemini_cli` itself monkeypatched (same principle as
`test_healer_agent_free.py`/`test_healer_agent_codex.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from core.config import settings as core_settings
from core.db import session_scope
from core.models import AuditLog, Error, HealJob, HealJobStatus, HealJobType, OpenResolvedStatus
from core.provider_auth import verify_provider_token
from healer import agent_gemini
from healer.agent_gemini import (
    CLIMalformedOutputError,
    CLINotFoundError,
    CLINotLoggedInError,
    CLIResult,
    CLITimeoutError,
    CLIUsageLimitError,
    run_gemini_cli,
    run_heal_job_gemini,
)
from healer.mcp_client import connect_in_memory
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient
from mcp_server.sandbox import REPO_ROOT
from mcp_server.server import mcp


@pytest.fixture(autouse=True)
def _deterministic_cli_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("healer.agent_gemini.shutil.which", lambda _name: None)
    monkeypatch.setattr("healer.cli_common.require_provider_cli_sandbox", lambda _provider: None)
    monkeypatch.setattr(core_settings, "mcp_auth_token", "unit-test-mcp-token-0123456789abcdef")


@dataclass
class _FakeProcess:
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
    returncode: int = 0
    pid: int = 6262
    hang: bool = False
    killed: bool = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        self.sent_stdin = input
        if self.hang:
            await asyncio.sleep(9999)
        return self.stdout_bytes, self.stderr_bytes

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@dataclass
class _FakeExec:
    process: _FakeProcess | None
    exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, *args: Any, **kwargs: Any) -> _FakeProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        assert self.process is not None
        return self.process


def _success_stdout(
    *, response: str = "Fixed it.", num_turns: int = 4, total_cost_usd: float | None = None
) -> bytes:
    payload: dict[str, Any] = {
        "response": response,
        "is_error": False,
        "num_turns": num_turns,
        "session_id": "sess-gemini-1",
        "stats": {
            "models": {
                "gemini-pro": {
                    "tokens": {"prompt": 800, "candidates": 120, "cached": 0},
                }
            }
        },
    }
    if total_cost_usd is not None:
        payload["total_cost_usd"] = total_cost_usd
    return json.dumps(payload).encode("utf-8")


async def test_run_gemini_cli_success_parses_result_and_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout(total_cost_usd=0.0)))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    result = await run_gemini_cli("do the thing", cwd=tmp_path, max_turns=5, timeout_s=30)

    assert isinstance(result, CLIResult)
    assert result.result_text == "Fixed it."
    assert result.is_error is False
    assert result.num_turns == 4
    assert result.input_tokens == 800
    assert result.output_tokens == 120
    assert result.total_cost_usd == Decimal("0.0")

    assert len(fake.calls) == 1
    argv = list(fake.calls[0]["args"])
    assert argv[0] == "gemini"
    assert argv[1:3] == ["--output-format", "json"]
    assert argv[3:] == [
        "--max-turns",
        "5",
        "--allowed-mcp-server-names",
        "selfheal",
        "--approval-mode",
        "default",
    ]

    kwargs = fake.calls[0]["kwargs"]
    assert kwargs["cwd"] != str(tmp_path)
    if os.name != "nt":
        assert kwargs["start_new_session"] is True
    assert not (tmp_path / ".gemini").exists()
    assert not await asyncio.to_thread(Path(kwargs["cwd"]).exists)
    assert "do the thing" not in argv
    assert fake.process is not None
    assert fake.process.sent_stdin == b"do the thing"

    env = kwargs["env"]
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        assert key not in env


async def test_run_gemini_cli_writes_isolated_settings_restricted_to_selfheal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(core_settings, "mcp_auth_token", "unit-test-mcp-token-0123456789abcdef")
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))

    async def inspect_isolated_settings(*args: Any, **kwargs: Any) -> _FakeProcess:
        settings_path = Path(kwargs["cwd"]) / ".gemini" / "settings.json"
        assert settings_path.is_file()
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        assert payload["tools"]["core"] == []
        assert payload["mcp"]["allowed"] == ["selfheal"]
        token = payload["mcpServers"]["selfheal"]["headers"]["Authorization"].removeprefix(
            "Bearer "
        )
        assert payload["mcpServers"] == {
            "selfheal": {
                "httpUrl": f"http://127.0.0.1:{core_settings.mcp_port}/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
        claims = verify_provider_token(token, "unit-test-mcp-token-0123456789abcdef")
        assert claims is not None
        assert claims["worktree"] == tmp_path.name
        assert "propose_patch" not in claims["tools"]
        assert "run_tests" not in claims["tools"]
        assert "MCP_AUTH_TOKEN" not in kwargs["env"]
        assert "MCP_PROVIDER_TOKEN" not in kwargs["env"]
        return await fake(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", inspect_isolated_settings)

    await run_gemini_cli("prompt", cwd=tmp_path)

    isolated_cwd = Path(fake.calls[0]["kwargs"]["cwd"])
    assert not await asyncio.to_thread(isolated_cwd.exists)
    assert not (tmp_path / ".gemini").exists()


async def test_run_gemini_cli_strips_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak-either")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-must-not-leak")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "github-app-key-must-not-leak")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "mcp-bearer-must-not-leak")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key-must-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-must-not-leak")
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    await run_gemini_cli("prompt", cwd=tmp_path)

    env = fake.calls[0]["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert "MCP_AUTH_TOKEN" not in env


async def test_run_gemini_cli_is_error_result_still_parses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = json.loads(_success_stdout(response="Could not find a fix."))
    payload["is_error"] = True
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=json.dumps(payload).encode("utf-8")))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    result = await run_gemini_cli("prompt", cwd=tmp_path)
    assert result.is_error is True
    assert result.result_text == "Could not find a fix."


async def test_run_gemini_cli_timeout_kills_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeExec(process=_FakeProcess(hang=True))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    kill_calls: list[int] = []

    async def fake_kill_tree(pid: int) -> None:
        kill_calls.append(pid)

    monkeypatch.setattr(agent_gemini, "kill_process_tree", fake_kill_tree)

    with pytest.raises(CLITimeoutError):
        await run_gemini_cli("prompt", cwd=tmp_path, timeout_s=0.01)

    assert kill_calls == [fake.process.pid]  # type: ignore[union-attr]


async def test_run_gemini_cli_not_found_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeExec(process=None, exc=FileNotFoundError("no such file"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLINotFoundError):
        await run_gemini_cli("prompt", cwd=tmp_path)


async def test_run_gemini_cli_not_logged_in_detected_from_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=b"",
            stderr_bytes=b"Error: not authenticated. Please run `gemini auth login`.",
            returncode=1,
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLINotLoggedInError):
        await run_gemini_cli("prompt", cwd=tmp_path)


async def test_run_gemini_cli_usage_limit_detected_from_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=b"",
            stderr_bytes=b"Quota exceeded. Rate limit exceeded, resets at 5pm.",
            returncode=1,
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLIUsageLimitError):
        await run_gemini_cli("prompt", cwd=tmp_path)


async def test_run_gemini_cli_malformed_json_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=b"not json at all {{{", returncode=1))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLIMalformedOutputError):
        await run_gemini_cli("prompt", cwd=tmp_path)


# --- Higher-level loop: run_gemini_cli monkeypatched, everything else real -


async def _insert_error(
    *, exception_type: str, file_path: str, line_number: int, function_name: str
) -> int:
    async with session_scope() as session:
        error = Error(
            fingerprint=f"test-{uuid.uuid4().hex}",
            exception_type=exception_type,
            message=f"{exception_type} for test",
            traceback="Traceback (most recent call last):\n  ...",
            file_path=file_path,
            line_number=line_number,
            function_name=function_name,
            status=OpenResolvedStatus.OPEN,
        )
        session.add(error)
        await session.flush()
        return error.id


async def _insert_heal_job(*, job_type: HealJobType, source_error_id: int) -> HealJob:
    async with session_scope() as session:
        job = HealJob(
            type=job_type,
            status=HealJobStatus.RUNNING,
            fingerprint=f"test-{uuid.uuid4().hex}",
            source_error_id=source_error_id,
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        return job


def _mock_github_pr_and_labels(respx_mock: Any, *, pr_number: int) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": pr_number, "html_url": "https://example/pr"}
        )
    )
    respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues/{pr_number}/labels").mock(
        return_value=httpx.Response(200, json=[])
    )


def _mock_github_issue(respx_mock: Any, *, issue_number: int) -> None:
    respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues").mock(
        return_value=httpx.Response(201, json={"number": issue_number})
    )


_TEST_FILE = "apps/target_app/test_average_rating_zero_regression_gemini.py"
_TEST_FILE_CONTENT = (
    "from __future__ import annotations\n"
    "\n"
    "from types import SimpleNamespace\n"
    "from apps.target_app import bugs\n"
    "\n"
    "class _FakeSession:\n"
    "    async def get(self, _model: object, _item_id: int):\n"
    "        return SimpleNamespace(rating_sum=0, rating_count=0)\n"
    "\n"
    "\n"
    "async def test_average_rating_zero_reviews_returns_zero() -> None:\n"
    "    result = await bugs.average_rating(_FakeSession(), 999)\n"
    "    assert result == 0.0\n"
)


def _fix_diff() -> str:
    original = (REPO_ROOT / "apps/target_app/bugs.py").read_text(encoding="utf-8")
    old = "    return item.rating_sum / item.rating_count\n"
    assert old in original
    new = (
        "    if item.rating_count == 0:\n"
        "        return 0.0\n"
        "    return item.rating_sum / item.rating_count\n"
    )
    import difflib

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        original.replace(old, new, 1).splitlines(keepends=True),
        fromfile="a/apps/target_app/bugs.py",
        tofile="b/apps/target_app/bugs.py",
    )
    return "".join(diff)


def _new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


async def test_run_heal_job_gemini_fixes_zero_division_error_end_to_end(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    _mock_github_pr_and_labels(respx_mock, pr_number=501)
    _mock_github_issue(respx_mock, issue_number=1)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    async def fake_run_gemini_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        from mcp_server import git_utils

        await git_utils.apply_diff(_new_file_diff(_TEST_FILE, _TEST_FILE_CONTENT), cwd=cwd)
        await git_utils.apply_diff(_fix_diff(), cwd=cwd)
        return CLIResult(
            result_text="Root cause: missing zero guard. Fixed.",
            is_error=False,
            subtype="success",
            num_turns=6,
            session_id="sess-gemini-x",
            input_tokens=1200,
            output_tokens=300,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_gemini, "run_gemini_cli", fake_run_gemini_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_gemini(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PR_OPENED, refreshed.error_message
        assert refreshed.pr_number == 501
        assert refreshed.branch_name is not None

        cli_calls = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "cli_invocation", AuditLog.heal_job_id == job.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cli_calls) == 1
        assert cli_calls[0].details["backend"] == "gemini_cli"


async def test_run_heal_job_gemini_does_not_trust_a_claim_with_no_real_diff(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    _mock_github_issue(respx_mock, issue_number=89)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    async def fake_run_gemini_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        return CLIResult(
            result_text="Fixed it (nothing was actually changed).",
            is_error=False,
            subtype="success",
            num_turns=2,
            session_id="sess-gemini-y",
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_gemini, "run_gemini_cli", fake_run_gemini_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_gemini(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.FAILED
        assert refreshed.pr_number is None


async def test_run_heal_job_gemini_paused_when_cli_call_budget_exhausted(
    isolated_budget_date: None,
    monkeypatch: pytest.MonkeyPatch,
    fake_git_remote: str,
) -> None:
    monkeypatch.setattr(core_settings, "max_cli_calls_per_day", 0)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    called = False

    async def fake_run_gemini_cli(*args: Any, **kwargs: Any) -> CLIResult:
        nonlocal called
        called = True
        raise AssertionError("must not be called once the daily CLI-call cap is hit")

    monkeypatch.setattr(agent_gemini, "run_gemini_cli", fake_run_gemini_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_gemini(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    assert called is False
    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PAUSED_BUDGET
