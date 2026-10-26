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
    # Dev mode (the helper resolves None when MCP_HOSTNAME /
    # MCP_EXTRA_ALLOWED_HOSTS are unset): the SDK's implicit loopback-only
    # protection applies — so dev clients address the server as
    # localhost:port. TestClient's default Host ("testserver") is NOT
    # loopback and would 421; pin the Host like a real dev client would.
    monkeypatch.delenv("MCP_HOSTNAME", raising=False)
    monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
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
    # Host with an explicit port: the SDK's wildcard-port entries
    # ("localhost:*", "127.0.0.1:*") match "host:port" forms only.
    return client.post(
        "/mcp",
        json=PING,
        headers={"Host": "localhost:8000", **MCP_ACCEPT, **(headers or {})},
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


# ---------------------------------------------------------------------------
# Transport security adoption (mcp_auth.transport_security_from_env)
# ---------------------------------------------------------------------------


def test_dev_mode_resolves_to_none(monkeypatch):
    """Neither env set (local dev) → None = the SDK's implicit loopback-only
    protection, matching the old enable_dns_rebinding_protection=False
    semantics for dev clients (loopback addresses still work)."""
    monkeypatch.delenv("MCP_HOSTNAME", raising=False)
    monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
    assert server._mcp_transport_security is None


def test_pinned_hostname_enables_protection(monkeypatch):
    monkeypatch.setenv("MCP_HOSTNAME", "searxng-mcp.example.com")
    monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
    ts = mcp_auth.transport_security_from_env()
    assert ts is not None and ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["searxng-mcp.example.com", "localhost:*", "127.0.0.1:*"]
    assert ts.allowed_origins == ["https://searxng-mcp.example.com"]


def test_extra_allowed_hosts_widen_and_stay_protected(monkeypatch):
    monkeypatch.delenv("MCP_HOSTNAME", raising=False)
    monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "searxng-mcp-service.ops.svc.cluster.local:*")
    ts = mcp_auth.transport_security_from_env()
    assert ts.allowed_hosts == ["searxng-mcp-service.ops.svc.cluster.local:*", "localhost:*", "127.0.0.1:*"]
    assert ts.allowed_origins == []
    assert ts.enable_dns_rebinding_protection is True


def test_svc_dns_host_passes_unknown_still_421(monkeypatch):
    """End-to-end through the real SDK middleware: an allowlisted svc-DNS
    Host passes; an unknown Host still 421s; auth order is preserved (the
    gate wraps the app, transport security sits inside)."""
    monkeypatch.setenv("MCP_HOSTNAME", "searxng-mcp.example.com")
    monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "searxng-mcp-service.ops.svc.cluster.local:*")
    app = server.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=mcp_auth.transport_security_from_env(),
    )
    with TestClient(app) as c:
        for host, expected in (
            ("searxng-mcp-service.ops.svc.cluster.local:9090", (200, 401)),
            ("searxng-mcp.example.com", (200, 401)),
            ("evil.example.net", (421,)),
        ):
            status = c.post(
                "/mcp",
                json=PING,
                headers={"Host": host, **MCP_ACCEPT},
            ).status_code
            assert status in expected, f"Host {host} -> {status}"
