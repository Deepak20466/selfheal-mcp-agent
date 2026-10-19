from __future__ import annotations

from typing import Any

import pytest

from mcp_server.http_auth import MCPBearerAuthMiddleware
from mcp_server.server import validate_http_auth


async def _request(
    headers: list[tuple[bytes, bytes]], *, path: str = "/mcp"
) -> tuple[list[dict[str, Any]], list[bool]]:
    called: list[bool] = []
    messages: list[dict[str, Any]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        called.append(True)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = MCPBearerAuthMiddleware(app, token="unit-test-token-0123456789abcdef")
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await middleware(scope, receive, send)
    return messages, called


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
        messages, called = await _request(headers)
        assert messages[0]["status"] == 401
        assert (b"www-authenticate", b"Bearer") in messages[0]["headers"]
        assert not called


@pytest.mark.asyncio
async def test_mcp_bearer_auth_accepts_exact_token() -> None:
    messages, called = await _request(
        [(b"authorization", b"Bearer unit-test-token-0123456789abcdef")]
    )
    assert messages[0]["status"] == 204
    assert called == [True]


@pytest.mark.asyncio
async def test_mcp_bearer_auth_does_not_intercept_other_routes() -> None:
    messages, called = await _request([], path="/healthz")
    assert messages[0]["status"] == 204
    assert called == [True]


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
