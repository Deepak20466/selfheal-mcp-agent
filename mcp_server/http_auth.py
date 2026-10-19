"""Small shared-secret guard for the private MCP streamable-HTTP endpoint."""

from __future__ import annotations

import hmac
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.db import session_scope
from core.models import ProviderTokenUse
from core.provider_auth import (
    MAX_PROVIDER_MCP_BODY_BYTES,
    provider_request_allowed,
    provider_request_uses_tool,
    verify_provider_token,
)

logger = logging.getLogger(__name__)


async def claim_provider_rerun_token(claims: dict[str, object]) -> bool:
    """Atomically claim a provider rerun token in PostgreSQL across restarts."""
    nonce = claims.get("nonce")
    heal_job_id = claims.get("heal_job_id")
    run_id = claims.get("run_id")
    expires = claims.get("exp")
    if (
        not isinstance(nonce, str)
        or isinstance(heal_job_id, bool)
        or not isinstance(heal_job_id, int)
        or heal_job_id < 1
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id < 1
        or isinstance(expires, bool)
        or not isinstance(expires, int)
    ):
        raise ValueError("Provider rerun claims are incomplete")
    expires_at = datetime.fromtimestamp(expires, tz=UTC)
    now = datetime.now(UTC)

    async with session_scope() as session:
        await session.execute(delete(ProviderTokenUse).where(ProviderTokenUse.expires_at <= now))
        statement = (
            postgresql_insert(ProviderTokenUse)
            .values(
                nonce=nonce,
                heal_job_id=heal_job_id,
                run_id=run_id,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=[ProviderTokenUse.nonce])
            .returning(ProviderTokenUse.nonce)
        )
        result = await session.execute(statement)
        return result.scalar_one_or_none() is not None


class MCPBearerAuthMiddleware:
    """Authenticate healer requests and constrain short-lived provider sessions."""

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
            try:
                claimed = await claim_provider_rerun_token(provider_claims)
            except Exception:
                logger.exception("Provider rerun capability could not be claimed")
                await self._reject(send, 503, "provider rerun capability is unavailable")
                return
            if not claimed:
                await self._reject(send, 403, "provider rerun capability has already been used")
                return

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
