"""Tests for the logsearch-mcp /mcp API-key gate (MANDATORY-auth fleet decision).

Scope matches applygate: ONLY /mcp is enforced (the console's /api/* is a
read-only search front-end; probes stay public for k8s). No keys configured →
open (dev mode, loud startup warning); MCP_API_KEYS (fleet-universal) or
LOGSEARCH_API_KEYS turns it on. Bearer + X-API-Key accepted; comma-separated
keys = the rotation story.

NOTE: never drive /mcp with a bare GET — the streamable-http transport treats
GET as an SSE stream request and blocks the TestClient. Use the JSON-RPC ping
POST (also proves the transport answers behind the gate).

The TestClients below drive http://127.0.0.1:9101 (not the httpx default
http://testserver): the fleet-shared transport security
(mcp_auth.transport_security_from_env) leaves the SDK's DNS-rebinding
protection ON in dev mode — loopback-only Host allowlist — and 'testserver'
is not a loopback Host (421). A loopback base_url is the honest dev posture.

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
    for var in ("MCP_API_KEYS", server.LOGSEARCH_API_KEYS_ENV):
        monkeypatch.delenv(var, raising=False)
    return server._build_http_app()


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json=PING,
        headers={**MCP_ACCEPT, **(headers or {})},
    )


def _client(app):
    """Loopback TestClient — see the module note on the Host allowlist."""
    return TestClient(app, base_url="http://127.0.0.1:9101")


def test_open_when_no_keys(app):
    with _client(app) as c:
        assert _ping(c).status_code == 200


def test_health_and_console_public_even_with_keys(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1")
    with _client(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 200  # console shell stays public
        assert c.get("/api/status").status_code == 200  # read-only API stays public


def test_mcp_missing_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1")
    with _client(app) as c:
        r = _ping(c)
        assert r.status_code == 401
        assert "unauthorized" in r.json()["error"]
        assert r.headers["www-authenticate"] == "Bearer"


def test_mcp_wrong_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1")
    with _client(app) as c:
        assert _ping(c, {"X-API-Key": "nope"}).status_code == 401
        assert _ping(c, {"Authorization": "Bearer nope"}).status_code == 401
        assert _ping(c, {"Authorization": "Basic a2k="}).status_code == 401


def test_mcp_valid_key_via_both_header_forms(app, monkeypatch):
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "k1,k2")
    with _client(app) as c:
        r = _ping(c, {"X-API-Key": "k1"})
        assert r.status_code == 200
        assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert _ping(c, {"Authorization": "Bearer k2"}).status_code == 200


def test_universal_env_var_accepted(app, monkeypatch):
    """One-address wiring: MCP_API_KEYS alone authenticates fleet-wide."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-k")
    with _client(app) as c:
        assert _ping(c, {"X-API-Key": "uni-k"}).status_code == 200
        assert _ping(c, {"X-API-Key": "logsearch-only"}).status_code == 401


def test_union_and_rotation(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "old-key")
    monkeypatch.setenv(server.LOGSEARCH_API_KEYS_ENV, "new-key")
    with _client(app) as c:
        assert _ping(c, {"X-API-Key": "old-key"}).status_code == 200
        assert _ping(c, {"X-API-Key": "new-key"}).status_code == 200
        monkeypatch.setenv("MCP_API_KEYS", "")
        assert _ping(c, {"X-API-Key": "old-key"}).status_code == 401
        assert _ping(c, {"X-API-Key": "new-key"}).status_code == 200


# ---------------------------------------------------------------------------
# transport security (fleet-shared mcp_auth.transport_security_from_env)
# ---------------------------------------------------------------------------
# The DNS-rebinding Host check lives INSIDE /mcp (the SDK transport), so the
# auth tests above now need the loopback Host the SDK's allowlist accepts.


@pytest.fixture()
def no_transport_env(monkeypatch):
    """Dev-mode transport posture: neither MCP_HOSTNAME nor
    MCP_EXTRA_ALLOWED_HOSTS set → transport_security_from_env() is None → the
    SDK's implicit loopback-only protection, identical to pre-adoption."""
    monkeypatch.delenv(mcp_auth.HOSTNAME_ENV, raising=False)
    monkeypatch.delenv(mcp_auth.EXTRA_ALLOWED_HOSTS_ENV, raising=False)


def test_transport_security_none_when_envs_unset(no_transport_env):
    assert server._mcp_transport_security is None  # dev mode: SDK default
    # _build_http_app re-reads lazily per app build, so a fresh app also
    # passes None through:
    app = server._build_http_app()
    with _client(app) as c:
        assert _ping(c).status_code == 200  # Host: testserver on port 80


def test_transport_security_pinned_hostname_allows_pinned_and_loopback(monkeypatch):
    monkeypatch.setenv(mcp_auth.HOSTNAME_ENV, "logsearch.example.com")
    ts = mcp_auth.transport_security_from_env()
    assert ts is not None and ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["logsearch.example.com", "localhost:*", "127.0.0.1:*"]
    assert ts.allowed_origins == ["https://logsearch.example.com"]
    # End-to-end through the app: a loopback Host passes; the pinned FQDN
    # passes; an unknown host is rejected by the SDK with 421 (AFTER auth
    # middleware — same order as K8S-MCP).
    monkeypatch.setattr(server, "_mcp_transport_security", ts)
    app = server._build_http_app()
    with _client(app) as c:
        assert _ping(c, {"Host": "logsearch.example.com"}).status_code == 200
        assert _ping(c, {"Host": "127.0.0.1:9101"}).status_code == 200
        assert _ping(c, {"Host": "evil.example.com"}).status_code == 421


def test_transport_security_extra_hosts_add_svc_dns(monkeypatch):
    """MCP_EXTRA_ALLOWED_HOSTS adds in-cluster svc-DNS Hosts (the chart's
    extraAllowedHosts values) so local callers reach the server directly."""
    monkeypatch.setenv(mcp_auth.EXTRA_ALLOWED_HOSTS_ENV, "logsearch-mcp-svc.ops.svc.cluster.local:*")
    ts = mcp_auth.transport_security_from_env()
    assert ts.allowed_hosts == [
        "logsearch-mcp-svc.ops.svc.cluster.local:*",
        "localhost:*",
        "127.0.0.1:*",
    ]
    assert ts.allowed_origins == []
    monkeypatch.setattr(server, "_mcp_transport_security", ts)
    app = server._build_http_app()
    with _client(app) as c:
        assert _ping(c, {"Host": "logsearch-mcp-svc.ops.svc.cluster.local:9101"}).status_code == 200
        assert _ping(c, {"Host": "evil.example.com"}).status_code == 421
