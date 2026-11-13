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


def test_api_token_request_passes_the_identity_gate(app, monkeypatch):
    """A presented SQLHANDLER_API_TOKEN satisfies requireIdentity.

    It used to NOT: _ApiTokenMiddleware was registered before the identity
    middlewares (LIFO puts it inside them), so the token gate accepted the
    request while the identity gate — which resolves BEFORE it — saw an
    anonymous caller and 401'd. A deployment that configured only
    SQLHANDLER_API_TOKEN was therefore locked out of /api entirely.

    Fixed 2026-11-12: the identity ladder resolves the shared token
    (via="api-token", fingerprint only). The token gate stays as defense in
    depth for deployments that leave requireIdentity off.
    """
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "api-tok")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = app.get("/api/status", headers={"X-API-Token": "api-tok"})
    assert r.status_code == 200, r.text
    # a wrong token is still refused by the token middleware itself
    assert app.get("/api/status", headers={"X-API-Token": "wrong"}).status_code == 401
    # and no token at all is refused by the identity gate
    anon = app.get("/api/status")
    assert anon.status_code == 401
    assert "identity required" in anon.json()["error"]


def test_api_key_authenticates_but_relay_headers_cannot_self_attribute(app, monkeypatch):
    """On /api an explicit key authenticates; a relay SUBJECT header does not.

    audit 2026-11-12 cross-validation (P1): the /api surface had no key rung
    at all, so a caller holding a valid key was anonymous and every /api read
    401'd under the chart's requireIdentity default — while /api/whoami
    reported "authenticated". The explicit-key rung now resolves the key.

    The relay rung stays key-valid-GATED (attribution is an authorization
    input), and the explicit-key rung deliberately carries NO subject, so a
    bare X-MCP-Caller-Subject still cannot self-attribute here.
    """
    monkeypatch.setenv("MCP_API_KEYS", "svc-key")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = app.get("/api/status", headers={"X-API-Key": "svc-key", "X-MCP-Caller-Subject": "alice"})
    assert r.status_code == 200, r.text
    who = app.get("/api/whoami", headers={"X-API-Key": "svc-key", "X-MCP-Caller-Subject": "alice"})
    body = who.json()
    assert body["via"] == "key"
    assert body["caller"]["subject"] is None, "a relay subject must not ride an explicit key on /api"


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
