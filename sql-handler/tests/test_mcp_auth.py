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


# --- D19 explicit-over-ambient on /mcp (the gateway per-key override story) -
# The pcai-llm-gateway relays /mcp calls carrying BOTH its server-level
# credential (Authorization: Bearer <server key>) AND the calling key's
# stored override (X-API-Key: <tenant key>) — resolve_mcp_headers merges
# per-key overrides last-wins BY HEADER NAME, so both headers ride the wire.
# The gate must resolve the EXPLICIT claim, never whichever header the relay
# happened to emit first.


def test_override_key_beats_invalid_ambient_bearer(app, monkeypatch):
    """The exact pcai-llm-gateway shape with a wrong/stale server-level
    Bearer: the pre-D19 wire-order gate read the Bearer first and 401'd
    here; the explicit X-API-Key claim must win."""
    monkeypatch.setenv("MCP_API_KEYS", "tenant-key")
    headers = {"Authorization": "Bearer not-the-tenant-key", "X-API-Key": "tenant-key"}
    assert _ping(app, headers).status_code == 200


def test_unrecognized_explicit_claim_is_hard_401(app, monkeypatch):
    """A presented-but-unrecognized X-API-Key is a hard refusal even when the
    ambient Bearer is a perfectly valid key — a wrong key claim is never
    redeemed as the credential behind it (the /api admin posture)."""
    monkeypatch.setenv("MCP_API_KEYS", "ambient-svc-key")
    r = _ping(app, {"Authorization": "Bearer ambient-svc-key", "X-API-Key": "wrong-override"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_x_api_token_is_an_explicit_claim_too(app, monkeypatch):
    """X-API-Token rides the same explicit class as X-API-Key (the /api
    surface and saved.py already accept it; /mcp now agrees)."""
    monkeypatch.setenv("MCP_API_KEYS", "tok-key")
    assert _ping(app, {"Authorization": "Bearer nope", "X-API-Token": "tok-key"}).status_code == 200


def test_both_valid_explicit_claim_wins(app, monkeypatch):
    """Both credentials valid: resolution is DETERMINISTIC — the recorded
    identity feed is the X-API-Key's fingerprint, not the Bearer's."""
    import asyncio
    import hashlib

    from sqlhandler.server import _McpApiKeyMiddleware

    monkeypatch.setenv("MCP_API_KEYS", "ambient-key,tenant-key")
    captured: dict = {}

    async def inner(scope, receive, send):
        captured.update(scope.get("state") or {})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [
            (b"authorization", b"Bearer ambient-key"),
            (b"x-api-key", b"tenant-key"),
        ],
    }
    asyncio.run(_McpApiKeyMiddleware(inner)(scope, receive, send))
    expected = "sha256:" + hashlib.sha256(b"tenant-key").hexdigest()[:12]
    assert captured.get("sqlhandler.key_fp") == expected


def test_later_explicit_claim_still_rescues_earlier_wrong_one(app, monkeypatch):
    """Multiple explicit claims: the first RECOGNIZED one wins — an
    unrecognized claim among several does not hard-refuse the request
    (same semantics as _admin_resolve_caller's candidate loop). Duplicate
    headers need the list form, so this posts directly instead of _ping."""
    monkeypatch.setenv("MCP_API_KEYS", "tenant-key")
    r = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers=[
            ("Accept", "application/json, text/event-stream"),
            ("X-API-Key", "wrong"),
            ("X-API-Key", "tenant-key"),
        ],
    )
    assert r.status_code == 200
