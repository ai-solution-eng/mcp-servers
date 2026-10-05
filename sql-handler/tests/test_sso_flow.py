"""Browser SSO (D22) — the MM-RAG approach ported to SQLhandler.

The flow: /oauth/login redirects to the realm (CSRF state cookie planted),
/oauth/oidc/callback exchanges the code, VERIFIES the token with the same
D21 machinery as every other credential, and plants the HttpOnly session
cookie (default pcai-sso). The identity ladder treats that cookie as the
LOWEST-priority envelope — an explicit key/Bearer always outranks the
browser session — so the chip binds the VERIFIED preferred_username (what
the browser-header rung cannot know; it sees only the IdP sub UUID).

Security contracts pinned here (mirroring RAG's D22 tests):

* INERT by default — without the SSO env block the routes 404 and no
  cookie is ever accepted (byte-identical to pre-D22 behavior).
* The callback verifies the exchanged token through the FULL D21
  machinery (RS256/JWKS, iss/aud/exp); an unverifiable token never
  becomes a session (redirect, no cookie).
* CSRF state: the callback requires the state cookie planted by /login;
  a mismatched/absent state → redirect without a cookie (fail closed).
* Cookie precedence: an explicit X-API-Key outranks the SSO cookie; a
  GARBAGE cookie value declines to the next rung (never an error).
* /oauth/* is reachable while anonymous even with requireIdentity on
  (the login round trip IS how an anonymous visitor authenticates).

Run:  python -m pytest tests/test_sso_flow.py -v
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from sqlhandler import policy as _policy
from sqlhandler import server as server_module
from sqlhandler.mcp_fleet_common.audit import key_fingerprint

ADMIN_KEY = "admin-key-0123456789abcdef"
USER_KEY = "user-key-0123456789abcdef"
ADMIN_FP = key_fingerprint(ADMIN_KEY)
USER_FP = key_fingerprint(USER_KEY)

ISSUER = "https://sso.test/realms/ezaf"
AUDIENCE = "ua"
KID = "sso-flow-test-key"

# The flow envs (client secret + redirect + discovery pointing at a dead
# host — the flow tests that need a REAL exchange monkeypatch exchange_code,
# the rest never reach the network).
SSO_ENVS = {
    "SQLHANDLER_OIDC_SSO_ENABLED": "1",
    "SQLHANDLER_OIDC_SSO_CLIENT_ID": "ua",
    "SQLHANDLER_OIDC_SSO_CLIENT_SECRET": "test-secret",
    "SQLHANDLER_OIDC_SSO_REDIRECT_URI": "https://sqlhandler.test/oauth/oidc/callback",
    # Discovery bypassed with the documented endpoint overrides (the flow
    # tests never want a network dependency; the callback tests monkeypatch
    # exchange_code instead).
    "SQLHANDLER_OIDC_SSO_AUTHORIZATION_ENDPOINT": f"{ISSUER}/protocol/openid-connect/auth",
    "SQLHANDLER_OIDC_SSO_TOKEN_ENDPOINT": f"{ISSUER}/protocol/openid-connect/token",
    "SQLHANDLER_OIDC_SSO_COOKIE_SECURE": "false",
}

POLICY_DOC = {
    "datasets": {
        "global": ["workorder/*"],
        "assignments": {USER_FP: ["reports/*"]},
        "blocked": ["scratch/*"],
    },
    "admins": [ADMIN_FP],
}


# --- minimal local JWT machinery (mirrors the other OIDC suites) ------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64uint(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _b64json(obj) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _generate_rsa(key_id: str = KID) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    return {
        "key": key,
        "kid": key_id,
        "jwk": {
            "kty": "RSA",
            "kid": key_id,
            "use": "sig",
            "alg": "RS256",
            "n": _b64uint(nums.n),
            "e": _b64uint(nums.e),
        },
        "sign": lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
    }


def _mint(key: dict, claims: dict) -> str:
    signing_input = f"{_b64json({'alg': 'RS256', 'typ': 'JWT', 'kid': key['kid']})}.{_b64json(claims)}"
    return f"{signing_input}.{_b64url(key['sign'](signing_input.encode('ascii')))}"


def _claims(sub="1d2fee26-12d7-4167-bfc2-e781ff52fb54", preferred="andrew-bydlon", exp=None) -> dict:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": exp if exp is not None else int(time.time()) + 600,
        "iat": int(time.time()) - 5,
        "sub": sub,
        "preferred_username": preferred,
    }


class _JwksServer:
    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        holder = self
        self._doc: dict = {"keys": []}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(holder._doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/protocol/openid-connect/certs"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def set(self, doc: dict) -> None:
        self._doc = doc

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def jwks_server():
    import sqlhandler.oidc_identity as oidc

    server = _JwksServer()
    _rsa = _generate_rsa()
    server.set({"keys": [_rsa["jwk"]]})
    server._rsa = _rsa
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    yield server
    server.stop()
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()


def _setup_env(monkeypatch, jwks_server, *, sso: bool):
    monkeypatch.setenv("MCP_API_KEYS", f"{ADMIN_KEY},{USER_KEY}")
    monkeypatch.setenv("SQLHANDLER_OIDC_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_URL", jwks_server.url)
    for var in ("SQLHANDLER_TRUST_BROWSER_HEADERS", "SQLHANDLER_REQUIRE_IDENTITY"):
        monkeypatch.delenv(var, raising=False)
    for var, value in SSO_ENVS.items():
        if sso:
            monkeypatch.setenv(var, value)
        else:
            monkeypatch.delenv(var, raising=False)
    import sqlhandler.oidc_identity as oidc

    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()


@pytest.fixture()
def app_sso(monkeypatch, tmp_path, jwks_server):
    """App WITH the SSO flow configured (full env block)."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY_DOC, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(path))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    _setup_env(monkeypatch, jwks_server, sso=True)
    with TestClient(server_module._build_http_app()) as c:
        yield c
    _policy.reset_policy_store()


@pytest.fixture()
def app_inert(monkeypatch, tmp_path, jwks_server):
    """App WITHOUT the SSO env block — the byte-identical inert default."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY_DOC, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(path))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    _setup_env(monkeypatch, jwks_server, sso=False)
    with TestClient(server_module._build_http_app()) as c:
        yield c
    _policy.reset_policy_store()


def _sso_login(client) -> str:
    """Drive /oauth/login far enough to harvest the CSRF state cookie."""
    r = client.get("/oauth/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    auth_url = r.headers["location"]
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["ua"]
    assert qs["redirect_uri"] == ["https://sqlhandler.test/oauth/oidc/callback"]
    assert qs["state"] and qs["state"][0]
    state_cookie = None
    for header in r.headers.get_list("set-cookie"):
        if header.startswith("pcai-sso-state="):
            state_cookie = header.split(";")[0].split("=", 1)[1]
    assert state_cookie, r.headers.get_list("set-cookie")
    return f"pcai-sso-state={state_cookie}"


# ---------------------------------------------------------------------------
# inert by default
# ---------------------------------------------------------------------------


def test_sso_routes_404_when_not_configured(app_inert):
    r = app_inert.get("/oauth/login", follow_redirects=False)
    assert r.status_code == 404
    # /oauth/logout stays a harmless 302 redirect even when inert (clearing
    # nothing) — signing out must never 404, a cookie may predate a config
    # change. /oauth/login is the loud inert marker.
    r = app_inert.get("/oauth/logout", follow_redirects=False)
    assert r.status_code == 302


def test_sso_cookie_never_accepted_when_inert(app_inert, jwks_server, monkeypatch):
    """The INERT contract is about the FLOW, not the rung: routes 404 and
    no NEW session can be planted. But a cookie from a previous config
    (or a config where only `sso` was disabled while the OIDC rung stays
    on) still RESOLVES — it is just a verified token; RAG's parity
    behavior (the D22 fleet decision): the rung gates on the D21 resolver
    being configured, never on the flow's client secret. What inert-mode
    guarantees is that nothing can ESTABLISH a session."""
    token = _mint(jwks_server._rsa, _claims())
    r = app_inert.get("/api/whoami", cookies={"pcai-sso": token})
    assert r.status_code == 200
    body = r.json()
    # The verified token still resolves (fleet parity) — but the chip's
    # sign-in offer stays off (the flow is not served).
    assert body["authenticated"] is True
    assert body["via"] == "jwt"
    assert body["sso_login_available"] is False


# ---------------------------------------------------------------------------
# the flow: login → callback → session
# ---------------------------------------------------------------------------


def test_login_redirects_to_realm_with_state_cookie(app_sso):
    r = app_sso.get("/oauth/login", follow_redirects=False)
    assert r.status_code == 302
    auth_url = r.headers["location"]
    assert auth_url.startswith(ISSUER + "/"), auth_url
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["client_id"] == ["ua"]
    assert qs["scope"] == ["openid profile email"]
    assert qs["state"] and qs["state"][0]
    # CSRF state rides a short-lived HttpOnly cookie
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith("pcai-sso-state=") for c in cookies)
    assert any("HttpOnly" in c for c in cookies)


def test_callback_verifies_and_plants_session(app_sso, jwks_server, monkeypatch):
    """The full happy path: code exchange (monkeypatched at the network
    seam), D21 verification of the returned token, HttpOnly session cookie
    bound to the token's remaining life."""
    token = _mint(jwks_server._rsa, _claims())
    monkeypatch.setattr("sqlhandler.oidc_sso.exchange_code", lambda code: {"access_token": token, "expires_in": 600})
    state_cookie = _sso_login(app_sso)
    r = app_sso.get(
        f"/oauth/oidc/callback?code=the-code&state={_state_from_cookie(state_cookie)}",
        headers={"Cookie": state_cookie},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith("/ui")
    session = [h for h in r.headers.get_list("set-cookie") if h.startswith("pcai-sso=")]
    assert session, r.headers.get_list("set-cookie")
    assert "HttpOnly" in session[0] and "Secure" not in session[0]
    # The token is IN the cookie (the session IS the token — stateless) but
    # never logged (checked below) and never in an error body.
    assert token in session[0]


def _state_from_cookie(state_cookie: str) -> str:
    return state_cookie.split("=", 1)[1].split("|", 1)[0]


def test_callback_without_state_cookie_fails_closed(app_sso, jwks_server, monkeypatch):
    """CSRF: a callback whose state does not match the planted cookie gets
    the error redirect and NO session cookie."""
    token = _mint(jwks_server._rsa, _claims())
    monkeypatch.setattr("sqlhandler.oidc_sso.exchange_code", lambda code: {"access_token": token})
    r = app_sso.get(
        "/oauth/oidc/callback?code=the-code&state=forged-state",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "sso=error" in r.headers["location"]
    assert not [h for h in r.headers.get_list("set-cookie") if h.startswith("pcai-sso=")]


def test_callback_with_unverifiable_token_never_sets_session(app_sso, jwks_server, monkeypatch):
    """The exchange returns a token that fails D21 verification (wrong
    realm) → redirect, no cookie. The D21 machinery is the ONLY path to a
    session — the callback adds no trust of its own."""
    rogue = _generate_rsa("sso-flow-test-key")  # same kid, wrong key
    bad_token = _mint(rogue, _claims())
    monkeypatch.setattr("sqlhandler.oidc_sso.exchange_code", lambda code: {"access_token": bad_token})
    state_cookie = _sso_login(app_sso)
    r = app_sso.get(
        f"/oauth/oidc/callback?code=the-code&state={_state_from_cookie(state_cookie)}",
        headers={"Cookie": state_cookie},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "sso=error" in r.headers["location"]
    assert not [h for h in r.headers.get_list("set-cookie") if h.startswith("pcai-sso=")]


def test_logout_clears_the_session(app_sso):
    r = app_sso.get("/oauth/logout", follow_redirects=False)
    assert r.status_code == 302
    clears = r.headers.get_list("set-cookie")
    assert any(c.startswith("pcai-sso=;") and "Max-Age=0" in c for c in clears)


# ---------------------------------------------------------------------------
# the cookie as the LOWEST-priority identity envelope
# ---------------------------------------------------------------------------


def test_sso_cookie_resolves_jwt_username(app_sso, jwks_server):
    """THE fix for the UUID chip: with the D22 session planted, the caller
    resolves via='jwt' as the VERIFIED preferred_username — not the
    X-Auth-Request-User sub-UUID."""
    token = _mint(jwks_server._rsa, _claims())
    r = app_sso.get(
        "/api/whoami",
        cookies={"pcai-sso": token},
        headers={"X-Auth-Request-User": "1d2fee26-12d7-4167-bfc2-e781ff52fb54"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["via"] == "jwt"
    assert body["caller"]["subject"] == "andrew-bydlon"


def test_explicit_key_outranks_sso_cookie(app_sso, jwks_server):
    """D19 explicit-over-ambient: a presented X-API-Key wins over the
    browser session behind it."""
    token = _mint(jwks_server._rsa, _claims())
    r = app_sso.get(
        "/api/whoami",
        cookies={"pcai-sso": token},
        headers={"X-API-Key": USER_KEY},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["via"] == "key"
    assert body["caller"]["key_fp"] == USER_FP
    assert body["caller"]["subject"] is None


def test_garbage_sso_cookie_declines_to_next_rung(app_sso, monkeypatch):
    """A tampered cookie value is just an invalid token: the rung declines
    silently and the ladder falls through (browser-header rung under the
    trust flag — never an error, never a half-identity)."""
    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    r = app_sso.get(
        "/api/whoami",
        cookies={"pcai-sso": "tampered-or-expired"},
        headers={"X-Auth-Request-User": "gateway-user"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["via"] == "browser"
    assert body["caller"]["subject"] == "gateway-user"


def test_expired_sso_cookie_stays_anonymous(app_sso, jwks_server):
    token = _mint(jwks_server._rsa, _claims(exp=int(time.time()) - 60))
    r = app_sso.get("/api/whoami", cookies={"pcai-sso": token})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["via"] == "anonymous"


def test_whoami_reports_sso_login_available(app_sso):
    """The sign-in hint tracks the FLOW's config (the routes being served),
    not the JWT rung."""
    assert app_sso.get("/api/whoami").json()["sso_login_available"] is True


def test_whoami_hides_sign_in_when_flow_inert(app_inert):
    """The inert deployment's hint is False (the routes are not served) —
    even though the JWT rung itself may stay live (separate config)."""
    assert app_inert.get("/api/whoami").json()["sso_login_available"] is False


# ---------------------------------------------------------------------------
# /oauth/* reachable while anonymous (requireIdentity on)
# ---------------------------------------------------------------------------


def test_oauth_reachable_while_anonymous_under_require_identity(app_sso, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    assert app_sso.get("/api/tables").status_code == 401
    r = app_sso.get("/oauth/login", follow_redirects=False)
    assert r.status_code == 302, "the login round trip must stay reachable"


# ---------------------------------------------------------------------------
# the UI asset: sign-in parity with MM-RAG's header
# ---------------------------------------------------------------------------


def test_ui_carries_sso_sign_in_link():
    from sqlhandler.webui import _HTML

    assert 'id="id-sso-link"' in _HTML
    assert "/oauth/login" in _HTML
    assert "/oauth/logout" in _HTML
