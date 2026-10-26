"""Tests for the optional /mcp API-key gate (_McpApiKeyMiddleware).

Fleet decision 2026-09: SQL is OPTIONAL-auth — no keys configured means /mcp
behaves exactly as before (dev/gateway-only deployments); either MCP_API_KEYS
(fleet-universal) or SQLHANDLER_API_KEYS turns the gate on. Bearer and
X-API-Key both accepted; comma-separated keys = the rotation story.

Run:  python -m pytest tests/test_mcp_auth.py -v
"""

import pytest
from starlette.testclient import TestClient

from sqlhandler.server import _build_http_app


@pytest.fixture()
def app(monkeypatch):
    """Fresh app per test; the middleware reads env per request."""
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        monkeypatch.delenv(var, raising=False)
    app = _build_http_app()
    with TestClient(app) as c:
        yield c


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def test_open_when_no_keys(app):
    assert _ping(app).status_code == 200


def test_universal_key_gates_mcp(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    r = _ping(app)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert _ping(app, {"X-API-Key": "uni-k"}).status_code == 200


def test_server_alias_key_gates_mcp(app, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_API_KEYS", "sql-k")
    assert _ping(app, {"Authorization": "Bearer sql-k"}).status_code == 200
    assert _ping(app, {"X-API-Key": "wrong"}).status_code == 401


def test_union_and_rotation(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "old-key")
    monkeypatch.setenv("SQLHANDLER_API_KEYS", "new-key")
    assert _ping(app, {"X-API-Key": "old-key"}).status_code == 200
    assert _ping(app, {"X-API-Key": "new-key"}).status_code == 200
    # Drop the old key from the universal list — rejected immediately.
    monkeypatch.setenv("MCP_API_KEYS", "")
    assert _ping(app, {"X-API-Key": "old-key"}).status_code == 401
    assert _ping(app, {"X-API-Key": "new-key"}).status_code == 200
