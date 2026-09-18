"""Tests for the applygate API-key middleware on /mcp (fleet pattern: K8S-MCP).

Scope matrix pinned here (fleet-audit CRITICAL): /mcp — the cluster's entire
governed write surface — requires a key; the read-only console (/, /ui,
/api/* — plan previews are ALWAYS dry-run, audit tail, policy view) and the
k8s probes stay public, exactly like K8S-MCP's inert console. Open-in-dev-
mode, both header forms, multi-key overlap rotation, and per-request env
re-read are all covered.

NOTE: never drive /mcp with a bare GET — the streamable-http transport treats
GET as an SSE stream request and blocks the TestClient. The positive-path
probe is a minimal JSON-RPC `ping` POST, which doubles as an end-to-end check
(gate AND transport both answer).

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    python -m pytest tests/test_auth.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from starlette.testclient import TestClient

import server

PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}
MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture()
def app():
    return server._build_http_app()


def ping(c, headers):
    return c.post("/mcp", json=PING, headers={**MCP_ACCEPT, **headers})


def test_open_when_no_keys_configured(app, monkeypatch):
    """Dev mode: no APPLYGATE_API_KEYS → /mcp passes through unauthenticated."""
    monkeypatch.delenv(server.APPLYGATE_API_KEYS_ENV, raising=False)
    with TestClient(app) as c:
        assert ping(c, {}).status_code == 200


def test_health_and_console_public_even_with_keys(app, monkeypatch):
    """Probes and the read-only console stay public (no mutation endpoints)."""
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 200
        # Read-only console APIs stay reachable (plan is always dry-run).
        r = c.post("/api/plan", content=b"not json")
        assert r.status_code == 400  # reached the handler, not the gate


def test_mcp_missing_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        r = ping(c, {})
        assert r.status_code == 401
        assert "unauthorized" in r.json()["error"]
        assert r.headers["www-authenticate"] == "Bearer"


def test_mcp_wrong_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "nope"}).status_code == 401
        assert ping(c, {"Authorization": "Bearer nope"}).status_code == 401
        assert ping(c, {"Authorization": "Basic a2k="}).status_code == 401


def test_mcp_valid_key_via_both_header_forms(app, monkeypatch):
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1,k2")
    with TestClient(app) as c:
        r = ping(c, {"X-API-Key": "k1"})
        assert r.status_code == 200
        assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert ping(c, {"Authorization": "Bearer k2"}).status_code == 200


def test_multi_key_overlap_rotation(app, monkeypatch):
    """Old and new keys BOTH valid while listed together — zero-downtime
    rotation: append the new key, move clients over, drop the old one."""
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "old-key,new-key")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "old-key"}).status_code == 200
        assert ping(c, {"X-API-Key": "new-key"}).status_code == 200


def test_env_is_reread_per_request(app, monkeypatch):
    """Rotation without restart: the middleware re-reads the env every call."""
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "k1"}).status_code == 200
        monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k2")
        assert ping(c, {"X-API-Key": "k1"}).status_code == 401
        assert ping(c, {"X-API-Key": "k2"}).status_code == 200


def test_universal_env_var_accepted(app, monkeypatch):
    """One-address wiring: MCP_API_KEYS alone authenticates fleet-wide."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-key")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "uni-key"}).status_code == 200
        assert ping(c, {"X-API-Key": "applygate-only"}).status_code == 401


def test_universal_and_server_keys_are_unioned(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "uni-key")
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "ag-key")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "uni-key"}).status_code == 200
        assert ping(c, {"X-API-Key": "ag-key"}).status_code == 200
