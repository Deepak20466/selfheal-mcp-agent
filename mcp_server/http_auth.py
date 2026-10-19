"""Small shared-secret guard for the private MCP streamable-HTTP endpoint."""

from __future__ import annotations

import asyncio
import hmac
import json
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.provider_auth import (
    MAX_PROVIDER_MCP_BODY_BYTES,
    provider_request_allowed,
    provider_request_uses_tool,
    verify_provider_token,
)


class MCPBearerAuthMiddleware:
    """Authenticate healer requests and constrain short-lived provider sessions."""

    _rerun_lock: asyncio.Lock
    _used_rerun_tokens: dict[str, int]

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") != self._path:
            await self.app(scope, receive, send)
            return

        authorization = [
            value for name, value in scope.get("headers", []) if name.lower() == b"authorization"
        ]
        if len(authorization) != 1:
            await self._reject(send, 401, "unauthorized", authenticate=True)
            return

        if hmac.compare_digest(authorization[0], self._expected):
            await self.app(scope, receive, send)
            return

        prefix = b"Bearer "
        value = authorization[0]
        if not value.startswith(prefix):
            await self._reject(send, 401, "unauthorized", authenticate=True)
            return
        try:
            provider_token = value[len(prefix) :].decode("ascii")
        except UnicodeDecodeError:
            await self._reject(send, 401, "unauthorized", authenticate=True)
            return
        provider_claims = verify_provider_token(provider_token, self._token)
        if provider_claims is None:
            await self._reject(send, 401, "unauthorized", authenticate=True)
            return

        method = scope.get("method")
        if method == "GET":
            # Streamable HTTP may use GET for server-to-client event streams.
            # It cannot invoke tools, so it carries no provider capability.
            await self.app(scope, receive, send)
            return
        if method == "DELETE":
            # MCP uses DELETE to close this client's streamable-HTTP session.
            # It is a session lifecycle action and must not carry a request body.
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request" or message.get("body", b""):
                    await self._reject(send, 403, "provider DELETE body is not allowed")
                    return
                if not message.get("more_body", False):
                    break

            async def replay_empty_receive() -> Message:
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_empty_receive, send)
            return
        if method != "POST":
            await self._reject(send, 403, "provider method is not allowed")
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            body.extend(chunk)
            if len(body) > MAX_PROVIDER_MCP_BODY_BYTES:
                await self._reject(send, 413, "request body is too large")
                return
            if not message.get("more_body", False):
                break

        request_body = bytes(body)
        if not provider_request_allowed(request_body, provider_claims):
            await self._reject(send, 403, "provider tool capability denied")
            return
        if provider_request_uses_tool(request_body, "rerun_workflow"):
            nonce = str(provider_claims["nonce"])
            async with self._rerun_lock:
                now = int(time.time())
                self._used_rerun_tokens = {
                    prior_nonce: expires_at
                    for prior_nonce, expires_at in self._used_rerun_tokens.items()
                    if expires_at > now
                }
                if nonce in self._used_rerun_tokens:
                    await self._reject(send, 403, "provider rerun capability has already been used")
                    return
                self._used_rerun_tokens[nonce] = int(provider_claims["exp"])

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": request_body, "more_body": False}

        await self.app(scope, replay_receive, send)

    def __init__(self, app: ASGIApp, *, token: str, path: str = "/mcp") -> None:
        if len(token) < 32:
            raise ValueError("MCP_AUTH_TOKEN must be at least 32 characters")
        self.app = app
        self._token = token
        self._expected = f"Bearer {token}".encode()
        self._path = path
        self._rerun_lock = asyncio.Lock()
        self._used_rerun_tokens: dict[str, int] = {}

    @staticmethod
    async def _reject(
        send: Send,
        status: int,
        message: str,
        *,
        authenticate: bool = False,
    ) -> None:
        body = json.dumps({"error": message}, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        if authenticate:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
