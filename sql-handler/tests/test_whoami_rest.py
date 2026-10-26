"""GET /api/whoami — the header identity widget's probe (D-UI, 2026-10).

The REST twin of the MCP whoami tool, PREVIEW shape only (never a grant):
the payload is the caller's OWN audit-safe identity — class/subject/fp,
which ladder rung resolved it (``via``), ``authenticated`` — plus the
identity gate's current posture. Contracts pinned here:

* Resolution mirrors the ADMIN surface (_admin_resolve_caller): an
  explicitly presented X-API-Key / Bearer is authenticated by the route
  itself (the /api surface has no key middleware); a key that matches
  NOTHING stays anonymous — a wrong key is NEVER redeemed as the browser
  user behind it (the same wrong-key contract require_admin pins).
* A keyless request resolves through the full ladder: an SSO bearer JWT
  (verified against the test JWKS) → jwt; trusted browser headers (only
  under SQLHANDLER_TRUST_BROWSER_HEADERS) → browser; nothing → anonymous.
* The response never leaks admin designation or key material, and the
  route is un-gated even under SQLHANDLER_REQUIRE_IDENTITY (the UI shell
  is un-gated; this only PREVIEWS what a gated call would resolve to).
* The UI asset carries the widget wired to this endpoint: chip ids, the
  use-key dialog feeding the Access-control panel's single credential
  (adminCredential — never localStorage), and the sign-out clearing it.

Run:  python -m pytest tests/test_whoami_rest.py -v
"""

from __future__ import annotations

import base64
import json
import re
import time

import pytest
from starlette.testclient import TestClient

from sqlhandler import policy as _policy
from sqlhandler import server as server_module
from sqlhandler.mcp_fleet_common.audit import key_fingerprint

ADMIN_KEY = "admin-key-0123456789abcdef"
USER_KEY = "user-key-0123456789abcdef"
ADMIN_FP = key_fingerprint(ADMIN_KEY)
USER_FP = key_fingerprint(USER_KEY)

ISSUER = "https://whoami.test/realms/ezaf"
AUDIENCE = "ua"
KID = "whoami-test-key"

POLICY_DOC = {
    "datasets": {
        "global": ["workorder/*"],
        "assignments": {USER_FP: ["reports/*"]},
        "blocked": ["scratch/*"],
    },
    "admins": [ADMIN_FP],
}


# --- minimal local JWT machinery (mirrors test_selfservice_keys) ------------


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


def _claims(sub="alice", preferred="alice", exp=None) -> dict:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": exp if exp is not None else int(time.time()) + 600,
        "iat": int(time.time()) - 5,
        "sub": sub,
        "preferred_username": preferred,
    }


class _JwksServer:
    """A minimal local JWKS endpoint (mirrors test_selfservice_keys)."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        holder = self
        self._doc: dict = {"keys": []}
        # The minted key material the fixtures stash for _mint(); declared so
        # `server._rsa = _rsa` is a normal attribute write, not a new attr.
        self._rsa: dict | None = None

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
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


@pytest.fixture()
def identity_scope_fwd(jwks_server, monkeypatch):
    """A raw ASGI scope carrying the forwarded-token envelope (for the
    ladder-level unit test) with the OIDC resolver pointed at the JWKS."""
    import sqlhandler.oidc_identity as oidc

    monkeypatch.setenv("SQLHANDLER_OIDC_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_URL", jwks_server.url)
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    token = _mint(
        jwks_server._rsa,
        _claims(sub="1d2fee26-12d7-4167-bfc2-e781ff52fb54", preferred="andrew-bydlon"),
    )
    scope = {
        "type": "http",
        "headers": [
            (b"x-auth-request-access-token", token.encode("latin-1")),
            (b"x-auth-request-user", b"1d2fee26-12d7-4167-bfc2-e781ff52fb54"),
        ],
        "state": {},
    }
    yield scope
    monkeypatch.delenv("SQLHANDLER_OIDC_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("SQLHANDLER_OIDC_JWKS_URL", raising=False)
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()


@pytest.fixture()
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY_DOC, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(path))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    yield path
    _policy.reset_policy_store()


@pytest.fixture()
def app(monkeypatch, policy_file, jwks_server):
    """Fresh app per test; the OIDC resolver points at the local JWKS."""
    monkeypatch.setenv("MCP_API_KEYS", f"{ADMIN_KEY},{USER_KEY}")
    monkeypatch.setenv("SQLHANDLER_OIDC_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_URL", jwks_server.url)
    monkeypatch.delenv("SQLHANDLER_TRUST_BROWSER_HEADERS", raising=False)
    monkeypatch.delenv("SQLHANDLER_REQUIRE_IDENTITY", raising=False)
    import sqlhandler.oidc_identity as oidc

    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    with TestClient(server_module._build_http_app()) as c:
        yield c
    monkeypatch.delenv("SQLHANDLER_OIDC_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("SQLHANDLER_OIDC_JWKS_URL", raising=False)


# ---------------------------------------------------------------------------
# resolution: key rung, wrong-key anonymity, SSO bearer, browser rung
# ---------------------------------------------------------------------------


def test_key_header_resolves_key_rung(app):
    """A valid X-API-Key resolves via='key' with its fingerprint — never
    the raw key anywhere in the body."""
    r = app.get("/api/whoami", headers={"X-API-Key": USER_KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authenticated"] is True
    assert body["via"] == "key"
    assert body["caller"]["class"] == "key"
    assert body["caller"]["key_fp"] == USER_FP
    assert body["caller"]["subject"] is None
    assert USER_KEY not in r.text  # no key material, ever


def test_sso_bearer_resolves_jwt_rung_with_subject(app, jwks_server):
    """A verified SSO bearer resolves via='jwt', subject from the VERIFIED
    claims (preferred_username) — the answer the mint card needs."""
    token = _mint(jwks_server._rsa, _claims(sub="alice", preferred="andrew-bylon"))
    r = app.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authenticated"] is True
    assert body["via"] == "jwt"
    assert body["caller"]["class"] == "user"
    assert body["caller"]["subject"] == "andrew-bylon"
    assert body["caller"]["key_fp"] is None


def test_expired_bearer_stays_anonymous(app, jwks_server):
    """An expired token declines silently → the ANONYMOUS shape (not an
    error, not a 500) — fail-closed, honest preview."""
    token = _mint(jwks_server._rsa, _claims(exp=int(time.time()) - 60))
    r = app.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["via"] == "anonymous"


def test_wrong_key_stays_anonymous_never_browser_user(app, jwks_server):
    """A key-shaped credential that matches nothing must NOT fall through
    to the ladder — even with a valid SSO bearer in the same request. The
    wrong-key contract (require_admin's) applies here identically: a wrong
    key is never redeemed as the browser user behind it."""
    token = _mint(jwks_server._rsa, _claims(sub="alice", preferred="alice"))
    r = app.get(
        "/api/whoami",
        headers={"X-API-Key": "not-a-real-key", "Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["via"] == "anonymous"


def test_browser_headers_only_under_trust_flag(app, monkeypatch):
    """oauth2-proxy headers resolve the browser rung ONLY under
    SQLHANDLER_TRUST_BROWSER_HEADERS; without the flag → anonymous."""
    headers = {"X-Auth-Request-User": "gateway-user"}
    r = app.get("/api/whoami", headers=headers)
    assert r.status_code == 200
    assert r.json()["authenticated"] is False

    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    r2 = app.get("/api/whoami", headers=headers)
    assert r2.status_code == 200
    body = r2.json()
    assert body["authenticated"] is True
    assert body["via"] == "browser"
    assert body["caller"]["subject"] == "gateway-user"


# ---------------------------------------------------------------------------
# the D21 forwarded-token envelope (X-Auth-Request-Access-Token)
# ---------------------------------------------------------------------------


def test_forwarded_access_token_resolves_jwt_with_username(app, jwks_server, monkeypatch):
    """The oauth2-proxy forwarded token (pass-access-token) verifies through
    the JWT rung → subject = preferred_username (andrew-bydlon-style), NOT
    the X-Auth-Request-User sub-UUID the header rung carries. The envelope
    works WITHOUT the browser trust flag — it is verified, not trusted."""
    token = _mint(jwks_server._rsa, _claims(sub="1d2fee26-12d7-4167-bfc2-e781ff52fb54", preferred="andrew-bydlon"))
    headers = {
        "X-Auth-Request-Access-Token": token,
        "X-Auth-Request-User": "1d2fee26-12d7-4167-bfc2-e781ff52fb54",
    }
    r = app.get("/api/whoami", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["via"] == "jwt"
    assert body["caller"]["class"] == "user"
    assert body["caller"]["subject"] == "andrew-bydlon"


def test_forwarded_token_garbage_declines_not_browser(app, monkeypatch):
    """A garbage value in the envelope is an invalid token — it declines
    silently (fail-closed); with no trust flag the request stays anonymous.
    The envelope NEVER bypasses verification."""
    headers = {
        "X-Auth-Request-Access-Token": "not-a-token",
        "X-Auth-Request-User": "spoofed-user",
    }
    r = app.get("/api/whoami", headers=headers)
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def test_forwarded_token_wrong_signature_stays_anonymous(app, jwks_server, monkeypatch):
    """A token signed by a DIFFERENT key (kid matches, signature doesn't)
    fails RS256 verification → anonymous. Spoofing the envelope header
    gains nothing — same contract as a forged Bearer."""
    rogue = _generate_rsa(KID)  # same kid, different key pair
    forged = _mint(rogue, _claims(sub="x", preferred="victim"))
    headers = {
        "X-Auth-Request-Access-Token": forged,
        "X-Auth-Request-User": "victim",
    }
    r = app.get("/api/whoami", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["via"] == "anonymous"


def test_explicit_bearer_outranks_forwarded_envelope(app, jwks_server):
    """D19 explicit-over-ambient: a request carrying BOTH its own Bearer and
    the ambient forwarded token resolves as the EXPLICIT one (both verified;
    the caller's own credential governs)."""
    mine = _mint(jwks_server._rsa, _claims(sub="me", preferred="me-the-caller"))
    ambient = _mint(jwks_server._rsa, _claims(sub="ambient", preferred="ambient-user"))
    r = app.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {mine}", "X-Auth-Request-Access-Token": ambient},
    )
    assert r.status_code == 200
    assert r.json()["caller"]["subject"] == "me-the-caller"


def test_static_key_in_envelope_stays_key_rung(app):
    """A static key value in the envelope is the key rung's credential —
    never parsed as a JWT (disjoint credential spaces, RAG D21/D19)."""
    r = app.get(
        "/api/whoami",
        headers={"X-Auth-Request-Access-Token": f"Bearer {USER_KEY}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["via"] == "key"
    assert body["caller"]["key_fp"] == USER_FP


def test_ladder_forwarded_envelope_direct(identity_scope_fwd):
    """The identity-ladder unit path: caller_from_scope resolves the
    verified envelope to via='jwt'/preferred_username, above the
    trust-gated browser rung."""
    from sqlhandler.identity import caller_from_scope

    caller = caller_from_scope(identity_scope_fwd)
    assert caller.via == "jwt"
    assert caller.cls == "user"
    assert caller.subject == "andrew-bydlon"


def test_anonymous_shape_exact(app):
    """The anonymous payload's exact shape (the chip + tests render from
    it) — including the D22 sign-in hint (False when the flow is inert)."""
    r = app.get("/api/whoami")
    assert r.status_code == 200
    assert r.json() == {
        "caller": {"class": "anonymous", "subject": None, "key_fp": None},
        "via": "anonymous",
        "authenticated": False,
        "require_identity": False,
        "sso_login_available": False,
    }


def test_whoami_stays_ungated_under_require_identity(app, monkeypatch):
    """The /api gate skips nothing else — but whoami previews, it does not
    admit: with requireIdentity on and no credential it still answers 200
    anonymous (the shell is un-gated and this is its identity probe)."""
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    for path in ("/api/status", "/api/tables"):
        assert app.get(path).status_code == 401, path
    r = app.get("/api/whoami")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


# ---------------------------------------------------------------------------
# the UI asset: the widget exists, is wired to /api/whoami, and shares the
# Access-control panel's single page-memory credential
# ---------------------------------------------------------------------------


def test_ui_carries_header_identity_widget():
    from sqlhandler.webui import _HTML

    for marker in (
        'id="id-chip"',
        'id="id-key-link"',
        'id="id-signout"',
        'id="id-key-modal"',
        'id="id-key-input"',
        "/api/whoami",
        "/oauth2/sign_out",
    ):
        assert marker in _HTML, marker


def test_ui_widget_uses_the_panel_credential_not_storage():
    """Single-credential contract: the widget assigns the PANEL's variable
    (adminCredential) and the panel field; it NEVER persists the key (no
    localStorage/sessionStorage anywhere in the widget block — the same
    posture test_admin_ui pins for the Access-control block)."""
    from sqlhandler.webui import _HTML

    marker = "// ---- header identity widget"
    end_marker = "// ---- header status"
    assert marker in _HTML and end_marker in _HTML
    widget_js = _HTML[_HTML.index(marker) : _HTML.index(end_marker)]
    assert "adminCredential" in widget_js
    assert '$("adm-key")' in widget_js
    assert not re.search(r"(local|session)Storage\s*\.", widget_js)
    # The sign-out clears the credential before the edge redirect.
    signout_idx = widget_js.index("function idSignOut")
    assert "adminCredential" in widget_js[signout_idx : signout_idx + 400]
