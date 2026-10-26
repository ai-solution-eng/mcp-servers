"""Entry point for the statistical-visualization MCP server (v0.1).

MCP 2.0 (protocol 2026-07-28): served STATELESS over streamable-http —
no initialize handshake, no Mcp-Session-Id, every request self-contained,
any replica can serve any request. json_response=True keeps the wire
format simple for the gateway federation.

Env:
    SEABORN_API_KEYS     comma-separated bearer keys gating /mcp (optional;
                         unset = open with a loud warning, fleet convention)
    SQLHANDLER_MCP_URL   sqlhandler MCP endpoint for the `sql` data source
    SEABORN_FETCH_TIMEOUT_S / SQL_MCP_TIMEOUT_S
"""

import os
import sys

import uvicorn

from mcp_instance import mcp
from tools import describe, health_check, plot  # noqa: F401 - registration side effect

SERVER_PORT = int(os.environ.get("SEABORN_MCP_PORT", "9092"))


def _transport_security():
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _auth_wrapper(app):
    """Bearer-key gate on /mcp when SEABORN_API_KEYS is set (fleet
    mcp_auth.py convention). Unset = open mode with a loud warning."""
    raw = os.environ.get("SEABORN_API_KEYS", "").strip()
    if not raw:
        print(
            "[security] SEABORN_API_KEYS unset — /mcp is OPEN. "
            "Set comma-separated bearer keys in production.",
            file=sys.stderr,
        )
        return app

    keys = {k.strip() for k in raw.split(",") if k.strip()}
    from starlette.responses import JSONResponse

    async def gated(scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await app(scope, receive, send)
            return
        auth = ""
        for header, value in scope.get("headers", []):
            if header == b"authorization":
                auth = value.decode("latin-1")
                break
        token = auth.removeprefix("Bearer ").strip()
        if token not in keys:
            resp = JSONResponse(
                {"error": "unauthorized: missing, invalid, or revoked key"},
                status_code=401,
            )
            await resp(scope, receive, send)
            return
        await app(scope, receive, send)

    return gated


def build_app():
    """Stateless MCP 2.0 streamable-http app, auth-wrapped."""
    http_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(),
    )
    return _auth_wrapper(http_app)


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
