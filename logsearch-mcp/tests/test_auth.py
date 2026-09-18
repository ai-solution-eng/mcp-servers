"""Tests for the logsearch-mcp /mcp API-key gate (MANDATORY-auth fleet decision).

Scope matches applygate: ONLY /mcp is enforced (the console's /api/* is a
read-only search front-end; probes stay public for k8s). No keys configured →
open (dev mode, loud startup warning); MCP_API_KEYS (fleet-universal) or
LOGSEARCH_API_KEYS turns it on. Bearer + X-API-Key accepted; comma-separated
keys = the rotation story.

NOTE: never drive /mcp with a bare GET — the streamable-http transport treats
GET as an SSE stream request and blocks the TestClient. Use the JSON-RPC ping
POST (also proves the transport answers behind the gate).

Run:  python -m pytest tests/test_auth.py -v
"""

import pytest
from starlette.testclient import TestClient

import server

PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}
MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture()
def app(monkeypatch):
    for var in ("MCP_API_KEYS", server.LOGSEARCH_API_KEYS_ENV):
        monkeypatch.delenv(var, raising=False)
    return server._build_http_app()


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json=PING,
        headers={**MCP_ACCEPT, **(headers or {})},
    )


def test_open_when_no_keys(app):
    with TestClient(app) as c:
        assert _ping(c).status_code == 200


def test_health_and_console_public_even_with_keys(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 200  # console shell stays public
        assert c.get("/api/status").status_code == 200  # read-only API stays public


def test_mcp_missing_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        r = _ping(c)
        assert r.status_code == 401
        assert "unauthorized" in r.json()["error"]
        assert r.headers["www-authenticate"] == "Bearer"


def test_mcp_wrong_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert _ping(c, {"X-API-Key": "nope"}).status_code == 401
        assert _ping(c, {"Authorization": "Bearer nope"}).status_code == 401
        assert _ping(c, {"Authorization": "Basic a2k="}).status_code == 401


def test_mcp_valid_key_via_both_header_forms(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1,k2")
    with TestClient(app) as c:
        r = _ping(c, {"X-API-Key": "k1"})
        assert r.status_code == 200
        assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert _ping(c, {"Authorization": "Bearer k2"}).status_code == 200


def test_universal_env_var_accepted(app, monkeypatch):
    """One-address wiring: MCP_API_KEYS alone authenticates fleet-wide."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    with TestClient(app) as c:
        assert _ping(c, {"X-API-Key": "uni-k"}).status_code == 200
        assert _ping(c, {"X-API-Key": "logsearch-only"}).status_code == 401


def test_union_and_rotation(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "old-key")
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "new-key")
    with TestClient(app) as c:
        assert _ping(c, {"X-API-Key": "old-key"}).status_code == 200
        assert _ping(c, {"X-API-Key": "new-key"}).status_code == 200
        monkeypatch.setenv("MCP_API_KEYS", "")
        assert _ping(c, {"X-API-Key": "old-key"}).status_code == 401
        assert _ping(c, {"X-API-Key": "new-key"}).status_code == 200
