from __future__ import annotations

import json
from typing import Any

import pytest

from core.provider_auth import PROVIDER_ALLOWED_TOOLS, issue_provider_token
from mcp_server.http_auth import MCPBearerAuthMiddleware
from mcp_server.server import validate_http_auth


async def _request(
    headers: list[tuple[bytes, bytes]],
    *,
    path: str = "/mcp",
    body: bytes = b"",
    method: str = "POST",
) -> tuple[list[dict[str, Any]], list[bool], list[bytes]]:
    called: list[bool] = []
    messages: list[dict[str, Any]] = []
    forwarded_body: list[bytes] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        called.append(True)
        request = await receive()
        forwarded_body.append(request.get("body", b""))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = MCPBearerAuthMiddleware(app, token="unit-test-token-0123456789abcdef")
    scope = {"type": "http", "method": method, "path": path, "headers": headers}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await middleware(scope, receive, send)
    return messages, called, forwarded_body


@pytest.mark.asyncio
async def test_mcp_bearer_auth_rejects_missing_or_invalid_tokens() -> None:
    for headers in (
        [],
        [(b"authorization", b"Bearer wrong-token")],
        [
            (b"authorization", b"Bearer unit-test-token-0123456789abcdef"),
            (b"authorization", b"Bearer wrong-token"),
        ],
    ):
        messages, called, _ = await _request(headers)
        assert messages[0]["status"] == 401
        assert (b"www-authenticate", b"Bearer") in messages[0]["headers"]
        assert not called


@pytest.mark.asyncio
async def test_mcp_bearer_auth_accepts_exact_token() -> None:
    messages, called, forwarded = await _request(
        [(b"authorization", b"Bearer unit-test-token-0123456789abcdef")]
    )
    assert messages[0]["status"] == 204
    assert called == [True]
    assert forwarded == [b""]


@pytest.mark.asyncio
async def test_mcp_bearer_auth_does_not_intercept_other_routes() -> None:
    messages, called, _ = await _request([], path="/healthz")
    assert messages[0]["status"] == 204
    assert called == [True]


@pytest.mark.asyncio
async def test_provider_token_forwards_a_scoped_read_request() -> None:
    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "apps/example.py"}},
        }
    ).encode()
    messages, called, forwarded = await _request(
        [(b"authorization", f"Bearer {token}".encode())], body=body
    )
    assert messages[0]["status"] == 204
    assert called == [True]
    assert forwarded == [body]


@pytest.mark.asyncio
async def test_provider_token_allows_read_only_mcp_resource_requests() -> None:
    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
    )
    for method, params in (
        ("resources/list", {}),
        ("resources/read", {"uri": "repo://tree"}),
    ):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        messages, called, forwarded = await _request(
            [(b"authorization", f"Bearer {token}".encode())], body=body
        )
        assert messages[0]["status"] == 204
        assert called == [True]
        assert forwarded == [body]


@pytest.mark.asyncio
async def test_provider_token_allows_empty_session_close_and_rejects_delete_body() -> None:
    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
    )
    authorization = [(b"authorization", f"Bearer {token}".encode())]

    messages, called, forwarded = await _request(authorization, method="DELETE")
    assert messages[0]["status"] == 204
    assert called == [True]
    assert forwarded == [b""]

    messages, called, _ = await _request(authorization, method="DELETE", body=b"unexpected")
    assert messages[0]["status"] == 403
    assert not called


@pytest.mark.asyncio
async def test_provider_token_cannot_call_mutating_github_or_deployment_tools() -> None:
    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
    )
    for tool_name in ("rerun_workflow", "cancel_workflow", "trigger_rollback"):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": {"run_id": 12}},
            }
        ).encode()
        messages, called, _ = await _request(
            [(b"authorization", f"Bearer {token}".encode())], body=body
        )
        assert messages[0]["status"] == 403
        assert not called


@pytest.mark.asyncio
async def test_provider_rerun_capability_is_bound_to_one_ci_run() -> None:
    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="ci-heal-27-1",
        heal_job_id=27,
        tools=PROVIDER_ALLOWED_TOOLS | {"rerun_workflow"},
        run_id=54,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "rerun_workflow", "arguments": {"run_id": 54}},
    }
    messages, called, _ = await _request(
        [(b"authorization", f"Bearer {token}".encode())], body=json.dumps(request).encode()
    )
    assert messages[0]["status"] == 204
    assert called == [True]

    request["params"]["arguments"]["run_id"] = 55
    messages, called, _ = await _request(
        [(b"authorization", f"Bearer {token}".encode())], body=json.dumps(request).encode()
    )
    assert messages[0]["status"] == 403
    assert not called


@pytest.mark.asyncio
async def test_provider_rerun_capability_is_single_use() -> None:
    secret = "unit-test-token-0123456789abcdef"
    token = issue_provider_token(
        secret,
        worktree="ci-heal-27-1",
        heal_job_id=27,
        tools=PROVIDER_ALLOWED_TOOLS | {"rerun_workflow"},
        run_id=54,
    )
    calls: list[bool] = []
    statuses: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        calls.append(True)
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = MCPBearerAuthMiddleware(app, token=secret)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "rerun_workflow", "arguments": {"run_id": 54}},
        }
    ).encode()

    async def send_request() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                statuses.append(int(message["status"]))

        await middleware(scope, receive, send)

    await send_request()
    await send_request()
    assert statuses == [204, 403]
    assert calls == [True]


@pytest.mark.asyncio
async def test_provider_token_binds_patch_and_test_calls_to_its_worktree() -> None:
    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
        heal_job_id=27,
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "propose_patch",
                "arguments": {"worktree": "heal-28", "heal_job_id": 27, "unified_diff": ""},
            },
        }
    ).encode()
    messages, called, _ = await _request(
        [(b"authorization", f"Bearer {token}".encode())], body=body
    )
    assert messages[0]["status"] == 403
    assert not called

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "propose_patch",
                "arguments": {"worktree": "heal-27", "heal_job_id": 28, "unified_diff": ""},
            },
        }
    ).encode()
    messages, called, _ = await _request(
        [(b"authorization", f"Bearer {token}".encode())], body=body
    )
    assert messages[0]["status"] == 403
    assert not called


@pytest.mark.asyncio
async def test_provider_token_rejects_expired_or_oversized_requests() -> None:
    expired = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
        now=1,
        ttl_seconds=60,
    )
    messages, called, _ = await _request(
        [(b"authorization", f"Bearer {expired}".encode())],
        body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert messages[0]["status"] == 401
    assert not called

    token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
    )
    messages, called, _ = await _request(
        [(b"authorization", f"Bearer {token}".encode())], body=b"x" * (2 * 1024 * 1024 + 1)
    )
    assert messages[0]["status"] == 413
    assert not called


@pytest.mark.asyncio
async def test_provider_token_rejects_invalid_encoding_and_signature() -> None:
    valid_token = issue_provider_token(
        "unit-test-token-0123456789abcdef",
        worktree="heal-27",
    )
    tokens = [
        "selfheal-provider-v1.!!!!.not-a-signature",
        issue_provider_token(
            "another-unit-test-secret-0123456789",
            worktree="heal-27",
        ),
    ]
    for token in tokens:
        messages, called, _ = await _request(
            [(b"authorization", f"Bearer {token}".encode())],
            body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        )
        assert messages[0]["status"] == 401
        assert not called

    messages, called, _ = await _request(
        [(b"authorization", b"Bearer " + valid_token.encode() + b"\xff")],
        body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    assert messages[0]["status"] == 401
    assert not called


def test_mcp_bearer_auth_rejects_short_secrets() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        MCPBearerAuthMiddleware(lambda *_: None, token="too-short")


def test_http_mcp_requires_token_on_non_loopback_bind() -> None:
    with pytest.raises(RuntimeError, match="required when MCP_HOST"):
        validate_http_auth("192.0.2.7", None)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_http_mcp_allows_tokenless_loopback(host: str) -> None:
    validate_http_auth(host, None)


def test_http_mcp_rejects_weak_token_even_on_loopback() -> None:
    with pytest.raises(RuntimeError, match="at least 32"):
        validate_http_auth("127.0.0.1", "short")
