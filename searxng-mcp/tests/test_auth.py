"""Tests for the searxng-mcp /mcp API-key gate (OPTIONAL-auth fleet decision).

The middleware (shared module pcai_utils/mcp_auth.py) protects only the MCP
endpoints — there is no console here. No keys configured → open exactly as
before; MCP_API_KEYS (fleet-universal) or SEARXNG_API_KEYS turns it on.

Run:  python -m pytest tests/test_auth.py -v
"""

import pytest
from starlette.testclient import TestClient

import mcp_auth
import server

PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}
MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture()
def app(monkeypatch):
    for var in ("MCP_API_KEYS", server.SEARXNG_API_KEYS_ENV):
        monkeypatch.delenv(var, raising=False)
    # Build exactly what main() assembles: streamable-http only, then wrap
    # with the same middleware call main() uses.
    from starlette.applications import Starlette

    http_app = server.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=server._mcp_transport_security,
    )
    app = Starlette(routes=list(http_app.routes), lifespan=http_app.router.lifespan_context)
    return mcp_auth.ApiKeyAuthMiddleware(
        app,
        env_names=server.AUTH_ENV_NAMES,
        protected=lambda p: p.startswith("/mcp"),
    )


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json=PING,
        headers={**MCP_ACCEPT, **(headers or {})},
    )


def test_open_when_no_keys(app):
    with TestClient(app) as c:
        assert _ping(c).status_code == 200


def test_universal_key_gates_mcp(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    with TestClient(app) as c:
        r = _ping(c)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert _ping(c, {"X-API-Key": "uni-k"}).status_code == 200


def test_server_alias_key(app, monkeypatch):
    monkeypatch.setenv(server.SEARXNG_API_KEYS_ENV, "sx-k")
    with TestClient(app) as c:
        assert _ping(c, {"Authorization": "Bearer sx-k"}).status_code == 200
        assert _ping(c, {"X-API-Key": "nope"}).status_code == 401


def test_union_and_rotation(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "old-key")
    monkeypatch.setenv(server.SEARXNG_API_KEYS_ENV, "new-key")
    with TestClient(app) as c:
        assert _ping(c, {"X-API-Key": "old-key"}).status_code == 200
        assert _ping(c, {"X-API-Key": "new-key"}).status_code == 200
        monkeypatch.setenv("MCP_API_KEYS", "")
        assert _ping(c, {"X-API-Key": "old-key"}).status_code == 401
