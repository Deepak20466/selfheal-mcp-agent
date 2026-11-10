"""mcp_server.audit: every tool call gets logged, scrubbed, and truncated.

Every query here orders by `AuditLog.id.desc()` and takes the newest matching
row: from Phase 4 on, `healer`'s end-to-end tests make real, committed
`propose_patch`/`read_file`/etc. tool calls of their own (via the real
MCP server), which leave earlier "tool_call" rows with the same tool name
(and sometimes the same args) in the shared `selfheal_test` DB. The row this
test just logged is always the newest one, so ordering avoids picking up one
of those instead.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest


async def test_successful_call_is_logged_with_scrubbed_and_truncated_args(db_session) -> None:
    from sqlalchemy import select

    from core.models import AuditLog
    from mcp_server.audit import log_tool_call

    await log_tool_call(
        "read_file",
        {"path": "apps/target_app/bugs.py", "secret_token": "sk-ant-abcdefghijklmnop"},
        success=True,
        duration_ms=12.3,
    )

    stmt = select(AuditLog).where(AuditLog.action == "tool_call").order_by(AuditLog.id.desc())
    row = (await db_session.execute(stmt)).scalars().first()
    assert row is not None
    assert row.actor == "mcp"
    assert row.details["tool"] == "read_file"
    assert row.details["success"] is True
    assert row.details["args"]["secret_token"] == "[REDACTED]"
    assert row.details["args"]["path"] == "apps/target_app/bugs.py"


async def test_failed_call_audit_keeps_error_type_but_not_sensitive_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.models import AuditLog
    from mcp_server.audit import audited_tool

    records: list[AuditLog] = []

    class CaptureSession:
        def add(self, record: AuditLog) -> None:
            records.append(record)

    @asynccontextmanager
    async def capture_session_scope():
        yield CaptureSession()

    monkeypatch.setattr("mcp_server.audit.session_scope", capture_session_scope)

    private_marker = "PRIVATE_EXCEPTION_SOURCE_0d2c71f9"
    credential_marker = "sk-ant-sensitive-fixture-DO-NOT-LOG"

    @audited_tool(_FakeMcp(), "get_error")
    async def fake_error_tool(error_id: int) -> dict[str, Any]:
        raise RuntimeError(f"{private_marker} {credential_marker} error_id={error_id}")

    with pytest.raises(RuntimeError, match=private_marker):
        await fake_error_tool(error_id=999999)

    assert len(records) == 1
    details = records[0].details
    assert details["success"] is False
    assert details["error_type"] == "RuntimeError"
    assert "error" not in details
    assert private_marker not in str(details)
    assert credential_marker not in str(details)


async def test_patch_arguments_are_omitted_from_audit_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.models import AuditLog
    from mcp_server.audit import log_tool_call

    records: list[AuditLog] = []

    class CaptureSession:
        def add(self, record: AuditLog) -> None:
            records.append(record)

    @asynccontextmanager
    async def capture_session_scope():
        yield CaptureSession()

    monkeypatch.setattr("mcp_server.audit.session_scope", capture_session_scope)

    private_source_marker = "PRIVATE_PATCH_SOURCE_MARKER_3f54c7e1"
    long_diff = private_source_marker + "x" * 5000
    await log_tool_call("propose_patch", {"unified_diff": long_diff}, success=True, duration_ms=1.0)

    assert len(records) == 1
    logged_args = records[0].details["args"]
    assert "unified_diff" not in logged_args
    assert logged_args["unified_diff_bytes"] == len(long_diff.encode("utf-8"))
    assert private_source_marker not in str(records[0].details)


class _FakeMcp:
    """Minimal stand-in for MCPServer.tool(): registers nothing, just returns
    the function unwrapped -- audited_tool's own wrapper is what's under test."""

    def tool(self, name: str | None = None, **kwargs: Any):  # noqa: ANN201
        def decorator(func):  # noqa: ANN001, ANN202
            return func

        return decorator


async def test_audited_tool_scrubs_the_returned_result_not_just_logged_args(
    db_session,
) -> None:
    """Privacy guard (terminal-only v1.0 Step 5): a tool's RETURN VALUE is
    what actually reaches whichever AI backend made the call (via
    MCPToolClient.call_tool, or directly for a CLI backend talking to
    mcp-pod) -- log_tool_call only ever scrubbed the logged args, never the
    result handed back to the caller. This is the regression test for that
    gap."""

    from mcp_server.audit import audited_tool

    @audited_tool(_FakeMcp(), "fake_tool_with_a_leaky_result")
    async def fake_tool(error_id: int) -> dict[str, Any]:
        return {
            "error_id": error_id,
            "message": "connection failed: postgresql://svc:hunter2@dbhost:5432/app",
            "github_token": "ghp_" + "a" * 36,
        }

    result = await fake_tool(error_id=1)
    assert "hunter2" not in result["message"]
    assert "svc" not in result["message"]
    assert result["github_token"] == "[REDACTED]"
