"""Short lived, least privilege authentication for provider MCP sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

PROVIDER_TOKEN_TTL_SECONDS = 900
MAX_PROVIDER_TOKEN_TTL_SECONDS = PROVIDER_TOKEN_TTL_SECONDS
MAX_PROVIDER_MCP_BODY_BYTES = 2 * 1024 * 1024

# Provider sessions can inspect source and evidence, run the existing sandboxed
# test tool, and propose a patch. GitHub mutations and deployment actions stay
# on the trusted healer/MCP side of the boundary.
PROVIDER_ALLOWED_TOOLS = frozenset(
    {
        "get_error",
        "list_open_errors",
        "get_contract_violation",
        "read_file",
        "search_code",
        "list_files",
        "get_git_blame",
        "get_recent_commits",
        "run_tests",
        "propose_patch",
        "list_workflow_runs",
        "get_workflow_run",
        "get_job_logs",
        "get_pr_status",
        "get_deployment_status",
        "get_health",
        "get_metrics",
    }
)
_SCOPED_PROVIDER_TOOLS = frozenset({"rerun_workflow"})
_ALL_PROVIDER_TOOLS = PROVIDER_ALLOWED_TOOLS | _SCOPED_PROVIDER_TOOLS
_WORKTREE_WRITE_TOOLS = frozenset({"propose_patch", "run_tests"})

_TOKEN_PREFIX = "selfheal-provider-v1"  # noqa: S105 - public token format marker, not a secret
_SAFE_SCOPE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_PROVIDER_PROTOCOL_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "ping",
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "tools/list",
        "tools/call",
    }
)


def issue_provider_token(
    secret: str,
    *,
    worktree: str,
    tools: frozenset[str] = PROVIDER_ALLOWED_TOOLS,
    heal_job_id: int | None = None,
    run_id: int | None = None,
    ttl_seconds: int = PROVIDER_TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint a per-invocation token with tool and worktree scope."""
    if len(secret) < 32:
        raise ValueError("MCP_AUTH_TOKEN must be at least 32 characters")
    if not _SAFE_SCOPE_NAME.fullmatch(worktree):
        raise ValueError("Provider MCP worktree scope is invalid")
    if not tools or not tools.issubset(_ALL_PROVIDER_TOOLS):
        raise ValueError("Provider MCP tool scope contains an unauthorized tool")
    if heal_job_id is not None and (
        isinstance(heal_job_id, bool) or not isinstance(heal_job_id, int) or heal_job_id < 1
    ):
        raise ValueError("Provider MCP heal job scope must be a positive integer")
    if heal_job_id is None:
        # Read-only chat sessions have no repair job. They must never be able
        # to use a caller-selected job id to acquire a patch or test capability.
        tools = tools - _WORKTREE_WRITE_TOOLS
    if "rerun_workflow" in tools and (
        heal_job_id is None or isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1
    ):
        raise ValueError("Provider rerun capability requires one positive workflow run id")
    if not 1 <= ttl_seconds <= MAX_PROVIDER_TOKEN_TTL_SECONDS:
        raise ValueError("Provider MCP token lifetime is outside the permitted range")

    issued_at = int(time.time()) if now is None else now
    claims = {
        "aud": "selfheal-mcp",
        "exp": issued_at + ttl_seconds,
        "heal_job_id": heal_job_id,
        "iat": issued_at,
        "nonce": secrets.token_urlsafe(24),
        "role": "provider",
        "run_id": run_id,
        "tools": sorted(tools),
        "worktree": worktree,
    }
    payload = _encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{_TOKEN_PREFIX}.{payload}"
    signature = _encode(hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest())
    return f"{signed}.{signature}"


def verify_provider_token(
    token: str,
    secret: str,
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Return verified claims, or ``None`` for any malformed/expired token."""
    if len(secret) < 32:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return None
    signed = f"{parts[0]}.{parts[1]}"
    expected = _encode(hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(parts[2], expected):
        return None
    try:
        claims = json.loads(_decode(parts[1]))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    current_time = int(time.time()) if now is None else now
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    nonce = claims.get("nonce")
    worktree = claims.get("worktree")
    tools = claims.get("tools")
    run_id = claims.get("run_id")
    heal_job_id = claims.get("heal_job_id")
    if (
        claims.get("aud") != "selfheal-mcp"
        or claims.get("role") != "provider"
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or issued_at > current_time + 30
        or expires_at <= current_time
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_PROVIDER_TOKEN_TTL_SECONDS
        or not isinstance(nonce, str)
        or len(nonce) < 24
        or not isinstance(worktree, str)
        or not _SAFE_SCOPE_NAME.fullmatch(worktree)
        or not isinstance(tools, list)
        or not tools
        or any(not isinstance(name, str) or name not in _ALL_PROVIDER_TOOLS for name in tools)
        or (
            bool(_WORKTREE_WRITE_TOOLS.intersection(tools))
            and (
                isinstance(heal_job_id, bool) or not isinstance(heal_job_id, int) or heal_job_id < 1
            )
        )
        or (
            heal_job_id is not None
            and (
                isinstance(heal_job_id, bool) or not isinstance(heal_job_id, int) or heal_job_id < 1
            )
        )
        or (
            "rerun_workflow" in tools
            and (
                isinstance(heal_job_id, bool)
                or not isinstance(heal_job_id, int)
                or heal_job_id < 1
                or isinstance(run_id, bool)
                or not isinstance(run_id, int)
                or run_id < 1
            )
        )
        or ("rerun_workflow" not in tools and run_id is not None)
    ):
        return None
    return claims


def provider_request_allowed(body: bytes, claims: dict[str, Any]) -> bool:
    """Allow only scoped MCP protocol requests and provider-safe tools."""
    if len(body) > MAX_PROVIDER_MCP_BODY_BYTES:
        return False
    try:
        request = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    requests = request if isinstance(request, list) else [request]
    if not requests:
        return False
    rerun_call_count = 0
    for item in requests:
        if not isinstance(item, dict):
            return False
        method = item.get("method")
        if method not in _PROVIDER_PROTOCOL_METHODS:
            return False
        if method != "tools/call":
            continue
        params = item.get("params")
        if not isinstance(params, dict):
            return False
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool_name, str) or tool_name not in claims["tools"]:
            return False
        if not isinstance(arguments, dict):
            return False
        if tool_name in {"propose_patch", "run_tests"}:
            # The worker can only change or test the worktree it was started for.
            scoped_job_id = claims.get("heal_job_id")
            supplied_job_id = arguments.get("heal_job_id")
            if (
                arguments.get("worktree") != claims["worktree"]
                or isinstance(supplied_job_id, bool)
                or not isinstance(supplied_job_id, int)
                or supplied_job_id != scoped_job_id
            ):
                return False
        if tool_name == "rerun_workflow":
            supplied_run_id = arguments.get("run_id")
            if (
                isinstance(supplied_run_id, bool)
                or not isinstance(supplied_run_id, int)
                or supplied_run_id != claims.get("run_id")
            ):
                return False
        if tool_name == "rerun_workflow":
            rerun_call_count += 1
            if rerun_call_count > 1:
                return False
    return True


def provider_request_uses_tool(body: bytes, tool_name: str) -> bool:
    """Whether a validated MCP request or batch invokes ``tool_name``."""
    try:
        request = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    requests = request if isinstance(request, list) else [request]
    return any(
        isinstance(item, dict)
        and item.get("method") == "tools/call"
        and isinstance(item.get("params"), dict)
        and item["params"].get("name") == tool_name
        for item in requests
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
