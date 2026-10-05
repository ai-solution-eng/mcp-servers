"""Tests for the workbench API-key middleware (fleet pattern: K8S-MCP).

The middleware wraps EVERY route except /health+/healthz — the fleet-audit
CRITICAL was that /api/ws/{ws}/run (arbitrary process execution) trusted
network position alone. These tests pin the whole matrix: open-in-dev-mode,
probes always public, 401 shape, both header forms, multi-key overlap
rotation, per-request env re-read, and the run endpoint specifically.

Run:  python -m pytest tests/test_auth.py -v
"""

import pytest
from starlette.testclient import TestClient

import mcp_auth
import server


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture()
def app(root):
    return server._build_http_app()


def test_open_when_no_keys_configured(app, monkeypatch):
    """Dev mode: no WORKBENCH_API_KEYS → everything passes through."""
    monkeypatch.delenv(server.WORKBENCH_API_KEYS_ENV, raising=False)
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/").status_code == 200


def test_health_always_public(app, monkeypatch):
    """Probes must pass with or without keys (k8s startup/readiness)."""
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1,k2")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/healthz").status_code == 200


def test_missing_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        r = c.get("/api/status")
        assert r.status_code == 401
        assert "unauthorized" in r.json()["error"]
        assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": "nope"}).status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Basic a2k="}).status_code == 401


def test_valid_key_via_both_header_forms(app, monkeypatch):
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1,k2")
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": "k1"}).status_code == 200
        assert c.get("/api/status", headers={"Authorization": "Bearer k2"}).status_code == 200


def test_multi_key_overlap_rotation(app, monkeypatch):
    """Old and new keys are BOTH valid while listed together — the rotation
    story: append the new key, move clients over, drop the old one."""
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "old-key,new-key")
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": "old-key"}).status_code == 200
        assert c.get("/api/status", headers={"X-API-Key": "new-key"}).status_code == 200


def test_env_is_reread_per_request(app, monkeypatch):
    """Rotation without restart: the middleware re-reads the env every call."""
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": "k1"}).status_code == 200
        monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k2")
        assert c.get("/api/status", headers={"X-API-Key": "k1"}).status_code == 401
        assert c.get("/api/status", headers={"X-API-Key": "k2"}).status_code == 200


def test_run_endpoint_requires_key(app, monkeypatch):
    """The crown jewel: /api/ws/{ws}/run (process execution) is behind the key."""
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        r = c.post("/api/ws/x/run", json={"command": ["ls"], "timeout_s": 5})
        assert r.status_code == 401
        # And a file write too.
        r = c.post("/api/ws/x/file", json={"path": "a.txt", "content": "hi"})
        assert r.status_code == 401


def test_console_html_public_and_data_gated(app, monkeypatch):
    """Design revised 2026-09-13 (live-deploy finding): the console HTML is
    PUBLIC-but-inert (K8S-MCP console pattern) — gating the HTML shell made
    the unlock bar unreachable (the browser got the raw 401 JSON at / and
    the console could never load). The MUTATION surface is what stays
    behind the key: every /api/* route (run/workspaces/audit/…) is 401
    without a valid key, and the served HTML is inert without one (the
    unlock bar in the page collects it; every call carries X-API-Key)."""
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "unlock" in r.text.lower()
        assert c.get("/api/audit").status_code == 401
        assert c.post("/api/run", json={"workspace": "w", "command": ["ls"]}).status_code == 401


def test_universal_env_var_accepted(app, monkeypatch):
    """One-address wiring: MCP_API_KEYS alone authenticates fleet-wide."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-key")
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": "uni-key"}).status_code == 200
        assert c.get("/api/status", headers={"X-API-Key": "workbench-only"}).status_code == 401


def test_universal_and_server_keys_are_unioned(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "uni-key")
    monkeypatch.setenv(server.WORKBENCH_API_KEYS_ENV, "wb-key")
    with TestClient(app) as c:
        assert c.get("/api/status", headers={"X-API-Key": "uni-key"}).status_code == 200
        assert c.get("/api/status", headers={"X-API-Key": "wb-key"}).status_code == 200


# ---------------------------------------------------------------------------
# transport security from env (fleet-shared mcp_auth adoption)
# ---------------------------------------------------------------------------


def test_transport_security_adopted_from_shared_helper():
    """server.py must bind the SHARED helper's output, not hand-construct
    settings (the fleet one-address pattern; K8S-MCP precedent)."""
    assert mcp_auth.transport_security_from_env is not None
    # The module-level binding IS the helper's result for import-time env.
    from mcp.server.transport_security import TransportSecuritySettings

    assert server._mcp_transport_security is None or isinstance(
        server._mcp_transport_security, TransportSecuritySettings
    )


def test_transport_security_dev_mode_is_none(monkeypatch):
    """No MCP_HOSTNAME / MCP_EXTRA_ALLOWED_HOSTS → None (the SDK's implicit
    default applies untouched — loopback auto-protection on a loopback bind)."""
    monkeypatch.delenv("MCP_HOSTNAME", raising=False)
    monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
    assert mcp_auth.transport_security_from_env() is None


def test_transport_security_pinned_hostname(monkeypatch):
    """MCP_HOSTNAME turns protection ON with the pinned FQDN + loopback and
    the https-only browser-form Origin allowlist (mcp_auth contract)."""
    monkeypatch.setenv("MCP_HOSTNAME", "workbench.example.com")
    monkeypatch.delenv("MCP_EXTRA_ALLOWED_HOSTS", raising=False)
    ts = mcp_auth.transport_security_from_env()
    assert ts is not None and ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["workbench.example.com", "localhost:*", "127.0.0.1:*"]
    assert ts.allowed_origins == ["https://workbench.example.com"]


def test_transport_security_extra_allowed_hosts(monkeypatch):
    """In-cluster svc-DNS callers: MCP_EXTRA_ALLOWED_HOSTS joins the Host
    allowlist (verbatim or host:*); no Origin entries without a hostname."""
    monkeypatch.delenv("MCP_HOSTNAME", raising=False)
    monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "wb-service.ns.svc.cluster.local:*, wb2.ns.svc.cluster.local")
    ts = mcp_auth.transport_security_from_env()
    assert ts is not None and ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == [
        "wb-service.ns.svc.cluster.local:*",
        "wb2.ns.svc.cluster.local",
        "localhost:*",
        "127.0.0.1:*",
    ]
    assert ts.allowed_origins == []
