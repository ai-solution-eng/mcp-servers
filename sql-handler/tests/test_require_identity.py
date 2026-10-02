"""The identity-required gate (_IdentityRequiredMiddleware, task D1).

Env ``SQLHANDLER_REQUIRE_IDENTITY`` (1/true/yes/on, case-insensitive, re-read
per request): requests to /mcp AND /api/* whose resolved Caller is anonymous
get 401 {"error": "identity required: ..."} + WWW-Authenticate: Bearer.
NEVER gated: /health, /ready, /metrics (kubelet probes cannot carry secrets
— 2026-09-18), the /ui shell (static bytes; its data rides gated /api), and
"/" (the shell mount). An UNSET env is byte-identical behavior: same codes
as the pre-gate server.

Run:  python -m pytest tests/test_require_identity.py -v
"""

import pytest
from starlette.testclient import TestClient

from sqlhandler.server import _build_http_app


@pytest.fixture()
def app(monkeypatch):
    """Fresh app per test; every gate env is read per request."""
    for var in (
        "MCP_API_KEYS",
        "SQLHANDLER_API_KEYS",
        "SQLHANDLER_REQUIRE_IDENTITY",
        "SQLHANDLER_POLICY_ENABLED",
        "SQLHANDLER_POLICY_FILE",
    ):
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


# ---------------------------------------------------------------------------
# Gated surfaces: anonymous → 401
# ---------------------------------------------------------------------------


def test_anonymous_mcp_401_when_required(app, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = _ping(app)
    assert r.status_code == 401
    body = r.json()
    assert "identity required" in body["error"]
    assert "X-API-Key" in body["error"] or "key" in body["error"]
    assert r.headers["www-authenticate"] == "Bearer"


def test_anonymous_api_401_when_required(app, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "true")
    for path in ("/api/status", "/api/tables"):
        r = app.get(path)
        assert r.status_code == 401, path
        assert "identity required" in r.json()["error"]
        assert r.headers["www-authenticate"] == "Bearer"


def test_require_identity_flag_case_insensitive_variants(app, monkeypatch):
    for value in ("1", "true", "TRUE", "Yes", "on", "ON"):
        monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", value)
        assert _ping(app).status_code == 401, value
    for value in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", value)
        assert _ping(app).status_code == 200, repr(value)


# ---------------------------------------------------------------------------
# Never gated: probes + the UI shell
# ---------------------------------------------------------------------------


def test_probes_stay_open_when_required(app, monkeypatch):
    """Kubelet probes cannot carry secrets — /health /ready /metrics must
    stay 200 with the gate ON (live-learned 2026-09-18; do not regress)."""
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    assert app.get("/health").status_code == 200
    # /ready hits the backend check; either the healthy 200 or the
    # backend-failure 503 proves the GATE did not reject it (a gated path
    # would be 401 with the identity-required body).
    r = app.get("/ready")
    assert r.status_code in (200, 503)
    assert "identity required" not in r.text
    assert app.get("/metrics").status_code == 200


def test_ui_shell_stays_open_when_required(app, monkeypatch):
    """The /ui shell (and its static bytes) stay ungated; its DATA comes
    through the gated /api routes."""
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    assert app.get("/ui").status_code == 200
    assert app.get("/").status_code == 200
    # ...while the shell's data endpoints are gated:
    assert app.get("/api/tables").status_code == 401


# ---------------------------------------------------------------------------
# Authenticated callers pass
# ---------------------------------------------------------------------------


def test_key_holding_request_passes(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "svc-key")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    assert _ping(app).status_code == 401  # gate + key gate both refuse anon
    assert _ping(app, {"X-API-Key": "svc-key"}).status_code == 200
    assert _ping(app, {"Authorization": "Bearer svc-key"}).status_code == 200


def test_key_holding_api_request_passes(app, monkeypatch):
    """The /api surface has its own token middleware; with requireIdentity
    ON and the API token presented, the call must pass the identity gate.
    Layering note: _ApiTokenMiddleware was registered BEFORE the identity
    middlewares, so LIFO puts it INSIDE them — its token presentation alone
    does not populate a Caller. The shared API token is the single-user
    local mode (an intentionally unattributed credential), so the identity
    gate still requires an attributable identity here — the deployment that
    wants attributed /api callers sets MCP_API_KEYS/SQLHANDLER_API_KEYS and
    presents a key, or OIDC. This test pins the gate's honest refusal; the
    key-attributed /api pass is covered by the MCP-surface test above."""
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "api-tok")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = app.get("/api/status", headers={"X-API-Token": "api-tok"})
    assert r.status_code == 401
    assert "identity required" in r.json()["error"]


def test_key_attributed_api_request_via_gateway_headers(app, monkeypatch):
    """The gateway topology on /api: a key-valid caller is a /mcp concept —
    the key middleware only guards /mcp, so an /api caller's attribution
    comes from the GATEWAY relay headers over... a path where no key gate
    ran. Honest outcome: the relay rung is key-valid-GATED (attribution-
    never-authorization), the caller stays anonymous, and the identity gate
    refuses. /api callers authenticate via SQLHANDLER_API_TOKEN (single-user
    local mode — pinned above as honestly refused when requireIdentity is
    on) or ride the gateway's own identity enforcement. This pins that the
    gate does NOT let bare relay headers self-attribute on /api."""
    monkeypatch.setenv("MCP_API_KEYS", "svc-key")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = app.get("/api/status", headers={"X-API-Key": "svc-key", "X-MCP-Caller-Subject": "alice"})
    assert r.status_code == 401
    assert "identity required" in r.json()["error"]


# ---------------------------------------------------------------------------
# Env unset → byte-identical
# ---------------------------------------------------------------------------


def test_unset_env_byte_identical(app):
    """No SQLHANDLER_REQUIRE_IDENTITY: the anonymous POST to /mcp and the
    GET to /api behave exactly as the pre-gate server (200 pass-through —
    the gate middleware is registered but inert)."""
    assert _ping(app).status_code == 200
    assert app.get("/api/status").status_code == 200
    assert app.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Layering: key middleware still runs OUTSIDE the identity gate
# ---------------------------------------------------------------------------


def test_key_gate_401_shape_unchanged_with_require_on(app, monkeypatch):
    """With BOTH the key gate and requireIdentity on, a keyless request is
    refused by the KEY middleware (outermost) with ITS message — the
    identity gate never masks the key gate's contract."""
    monkeypatch.setenv("MCP_API_KEYS", "svc-key")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = _ping(app)
    assert r.status_code == 401
    assert "unauthorized: missing or invalid API key" in r.json()["error"]


def test_gate_reads_resolved_caller_not_raw_headers(app, monkeypatch):
    """Spoofing refusal: a relay subject header WITHOUT a valid key resolves
    to anonymous (relay rung is key-valid-gated) — the identity gate must
    still 401 it. Only a real key (or verified JWT / trusted browser rung)
    passes."""
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = _ping(app, {"X-MCP-Caller-Subject": "alice"})
    assert r.status_code == 401
    assert "identity required" in r.json()["error"]
