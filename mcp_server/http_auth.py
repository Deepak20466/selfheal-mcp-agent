"""Small shared-secret guard for the private MCP streamable-HTTP endpoint."""

from __future__ import annotations

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send


class MCPBearerAuthMiddleware:
    """Require one constant-time checked bearer token on the MCP HTTP route."""

    def __init__(self, app: ASGIApp, *, token: str, path: str = "/mcp") -> None:
        if len(token) < 32:
            raise ValueError("MCP_AUTH_TOKEN must be at least 32 characters")
        self.app = app
        self._expected = f"Bearer {token}".encode()
        self._path = path

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
        if len(authorization) != 1 or not hmac.compare_digest(authorization[0], self._expected):
            body = json.dumps({"error": "unauthorized"}, separators=(",", ":")).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
