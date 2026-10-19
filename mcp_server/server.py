"""mcp-pod entrypoints.

- stdio (for `.mcp.json` / Claude Code in VS Code): `python -m mcp_server.server`
- streamable HTTP (the real mcp-pod process; the healer connects to this):
  `python -m mcp_server.http_main`
"""

from __future__ import annotations

import ipaddress

from core.config import settings
from core.logging import configure_logging
from mcp_server import resources  # noqa: F401 - registers resources by import
from mcp_server.http_auth import MCPBearerAuthMiddleware
from mcp_server.instance import mcp
from mcp_server.tools import cicd, code, deploy, errors  # noqa: F401 - registers tools by import

configure_logging(settings.log_level)

__all__ = ["mcp", "run_stdio", "run_http"]


def run_stdio() -> None:
    mcp.run(transport="stdio")


def run_http() -> None:
    host = settings.mcp_host.strip()
    token = settings.mcp_auth_token
    validate_http_auth(host, token)

    app = mcp.streamable_http_app(host=host)
    if token:
        app.add_middleware(MCPBearerAuthMiddleware, token=token)

    import uvicorn

    uvicorn.run(app, host=host, port=settings.mcp_port, log_level=settings.log_level.lower())


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_http_auth(host: str, token: str | None) -> None:
    """Fail closed when an externally reachable MCP listener has no strong token."""
    if not _is_loopback_host(host) and not token:
        raise RuntimeError("MCP_AUTH_TOKEN is required when MCP_HOST is not a loopback address")
    if token and len(token) < 32:
        raise RuntimeError("MCP_AUTH_TOKEN must be at least 32 characters")


if __name__ == "__main__":
    run_stdio()
