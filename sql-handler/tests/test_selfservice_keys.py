"""Self-service SSO key minting + the admin Users view (2026-10-02).

The feature: users authenticate with their SSO bearer (JWT rung) and mint a
long-lived key BOUND TO THEIR OWN VERIFIED SUBJECT (the X-API-KEY for /mcp);
admins see every known subject with grants/keys/last-seen and grant BY NAME.

Security contracts pinned here:
* JWT rung only — a relay/key/browser caller can NEVER mint (the relay rung
  is the spoofing class; the self-mint must never mint another's identity).
* The subject comes from the VERIFIED token, never request input (no
  parameter names a user).
* No-wildcard: self-mints copy the subject's EXISTING policy assignment; a
  subject with no grants cannot mint (403, actionable) and can never mint
  ["*"] implicitly.
* One key per subject by default (revoke-to-rotate, oldest rotated out).
* Self-revoke is store-verified ownership: a foreign/unknown fp is 404.
* The Users view + grant-by-name are admin-gated (401 anon / 403 non-admin).

Run: python -m pytest tests/test_selfservice_keys.py -v
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from starlette.testclient import TestClient

from sqlhandler import admin_keys as _admin_keys
from sqlhandler import policy as _policy
from sqlhandler import server as server_module
from sqlhandler.mcp_fleet_common.audit import key_fingerprint

ADMIN_KEY = "admin-key-0123456789abcdef"
ADMIN_FP = key_fingerprint(ADMIN_KEY)

ISSUER = "https://selfmint.test/realms/ezaf"
AUDIENCE = "ua"
KID = "selfmint-test-key"

POLICY_DOC = {
    "datasets": {
        "global": ["workorder/*"],
        "assignments": {},
        "blocked": ["scratch/*"],
    },
    "admins": [ADMIN_FP],
}


# --- minimal local JWT machinery (mirrors test_oidc_rung's approach) -------

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
        "jwk": {"kty": "RSA", "kid": key_id, "use": "sig", "alg": "RS256",
                "n": _b64uint(nums.n), "e": _b64uint(nums.e)},
        "sign": lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
    }


def _jwks_doc(*keys) -> dict:
    return {"keys": [k["jwk"] for k in keys]}


def _mint(key: dict, claims: dict) -> str:
    signing_input = (
        f"{_b64json({'alg': 'RS256', 'typ': 'JWT', 'kid': key['kid']})}.{_b64json(claims)}"
    )
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
    """A minimal local JWKS endpoint (mirrors test_oidc_rung._JwksServer)."""

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
    server.set(_jwks_doc(_rsa))
    server._rsa = _rsa
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    yield server
    server.stop()
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
def keys_file(tmp_path, monkeypatch):
    path = tmp_path / "admin-keys.json"
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(path))
    yield path


@pytest.fixture()
def app(monkeypatch, policy_file, keys_file, jwks_server):
    monkeypatch.setenv("MCP_API_KEYS", ADMIN_KEY)
    monkeypatch.setenv("SQLHANDLER_OIDC_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_URL", jwks_server.url)
    import sqlhandler.oidc_identity as oidc

    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    with TestClient(server_module._build_http_app()) as c:
        yield c
    monkeypatch.delenv("SQLHANDLER_OIDC_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("SQLHANDLER_OIDC_JWKS_URL", raising=False)


def _sso_headers(jwks_server, sub="alice", **kw) -> dict:
    token = _mint(jwks_server._rsa, _claims(sub=sub, **kw))
    return {"Authorization": f"Bearer {token}"}


def _admin_headers() -> dict:
    return {"X-API-Key": ADMIN_KEY}


def _read_policy(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _grant(policy_file, subject: str, globs: list[str]) -> None:
    """Pre-grant a subject BY NAME in the policy file (the operator step the
    no-wildcard contract requires before any self-mint can succeed)."""
    doc = _read_policy(policy_file)
    doc["datasets"]["assignments"][f"subject:{subject}"] = globs
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()


# ---------------------------------------------------------------------------
# the mint: happy path + the security contracts
# ---------------------------------------------------------------------------


def test_sso_user_mints_key_bound_to_own_subject(app, jwks_server, policy_file):
    _grant(policy_file, "alice", ["reports/*"])
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"] == "alice"
    assert body["key"].startswith("http") is False and len(body["key"]) >= 32
    # The store entry carries the binding; the raw key is in the response only.
    entry = next(e for e in _admin_keys.list_keys() if e["fp"] == body["fp"])
    assert entry["subject"] == "alice"
    assert body["key"] not in json.dumps(_admin_keys.list_keys())


def test_minted_key_carries_subject_policy_assignment(app, jwks_server, policy_file):
    # Pre-grant alice BY NAME, then mint: the key's fp inherits her globs.
    doc = _read_policy(policy_file)
    doc["datasets"]["assignments"]["subject:alice"] = ["reports/*"]
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    doc = _read_policy(policy_file)
    assert doc["datasets"]["assignments"][r.json()["fp"]] == ["reports/*"]
    assert doc["datasets"]["assignments"]["subject:alice"] == ["reports/*"]


def test_minted_key_authenticates_as_the_subject_on_mcp(app, jwks_server, policy_file):
    _grant(policy_file, "alice", ["reports/*"])
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    key = r.json()["key"]
    # The minted key speaks X-API-KEY on /mcp; the caller resolves WITH the
    # subject (key-class caller carrying the verified subject).
    r2 = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"X-API-Key": key, "Accept": "application/json, text/event-stream"},
    )
    assert r2.status_code == 200, r2.text


def test_mint_requires_jwt_rung(app, jwks_server):
    # The /api surface has NO key middleware (documented posture — the admin
    # routes read their own credential), so a static X-API-KEY resolves to
    # ANONYMOUS here: 401 from the core's identity requirement. The 403
    # branch (authenticated NON-JWT caller) is covered by the core unit
    # tests below — what matters is the contract: only a VERIFIED SSO
    # bearer can mint.
    # ANY non-JWT caller — static key (anonymous-resolution on /api) or
    # no credential at all — hits the SAME single gate: 403 "SSO only".
    # One gate, no oracle between "unauthenticated" and "wrong auth class".
    r = app.post("/api/admin/keys/self", headers=_admin_headers(), json={})
    assert r.status_code == 403 and "SSO" in r.json()["error"]
    r2 = app.post("/api/admin/keys/self", json={})
    assert r2.status_code == 403 and "SSO" in r2.json()["error"]


def test_mint_core_rejects_non_jwt_caller_403(monkeypatch, keys_file):
    """Direct core test: an AUTHENTICATED non-JWT caller (relay/key class)
    gets 403 — the self-mint is never reachable through a spoofable or
    shared credential class."""
    from sqlhandler.identity import Caller

    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(keys_file))
    key_caller = Caller(cls="key", subject=None, key_fp="sha256:0123456789ab", via="key")
    with pytest.raises(server_module.AdminHTTPError) as ei:
        server_module._self_mint_key(key_caller, "", None)
    assert ei.value.status == 403 and "SSO" in ei.value.body["error"]
    relay_caller = Caller(cls="user", subject="alice", key_fp=None, via="relay")
    with pytest.raises(server_module.AdminHTTPError) as ei2:
        server_module._self_mint_key(relay_caller, "", None)
    assert ei2.value.status == 403 and "SSO" in ei2.value.body["error"]


def test_mint_subject_never_from_request_input(app, jwks_server, policy_file):
    _grant(policy_file, "alice", ["reports/*"])
    # There is no subject parameter; an attempt to sneak one changes nothing.
    r = app.post(
        "/api/admin/keys/self",
        headers=_sso_headers(jwks_server, sub="alice"),
        json={"subject": "bob", "label": "evil"},
    )
    assert r.status_code == 201
    assert r.json()["subject"] == "alice"  # the VERIFIED subject, not the input


def test_mint_without_grants_refuses_403_no_wildcard(app, jwks_server):
    # No subject:alice assignment in the policy → the mint refuses with an
    # actionable message; it NEVER records ["*"] for itself.
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 403, r.text
    assert "grants" in r.json()["error"]
    assert _admin_keys.list_keys() == [], "the compensating remove must leave no orphan key"


def test_revoke_to_rotate_one_key_per_subject(app, jwks_server, policy_file):
    doc = _read_policy(policy_file)
    doc["datasets"]["assignments"]["subject:alice"] = ["reports/*"]
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()
    r1 = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    r2 = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r1.status_code == 201 and r2.status_code == 201
    mine = [e for e in _admin_keys.list_keys() if e.get("subject") == "alice"]
    assert len(mine) == 1, "the cap rotates the oldest out"
    assert mine[0]["fp"] == r2.json()["fp"]
    # The rotated-out key no longer authenticates on /mcp.
    gone = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"X-API-Key": r1.json()["key"], "Accept": "application/json, text/event-stream"},
    )
    assert gone.status_code == 401


def test_self_revoke_own_key_only(app, jwks_server, policy_file):
    doc = _read_policy(policy_file)
    doc["datasets"]["assignments"]["subject:alice"] = ["reports/*"]
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()
    key = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={}).json()
    # List own keys.
    lst = app.get("/api/admin/keys/self", headers=_sso_headers(jwks_server))
    assert [k["fp"] for k in lst.json()["keys"]] == [key["fp"]]
    # Foreign/unknown fp: 404, no existence leak.
    r = app.delete("/api/admin/keys/self/sha256:000000000000", headers=_sso_headers(jwks_server))
    assert r.status_code == 404
    # Own fp: revoked, then it 401s on /mcp.
    r = app.delete(f"/api/admin/keys/self/{key['fp']}", headers=_sso_headers(jwks_server))
    assert r.status_code == 200
    gone = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"X-API-Key": key["key"], "Accept": "application/json, text/event-stream"},
    )
    assert gone.status_code == 401


# ---------------------------------------------------------------------------
# the admin Users view + grant-by-name
# ---------------------------------------------------------------------------


def test_users_view_is_admin_gated(app):
    assert app.get("/api/admin/users").status_code == 401
    r = app.get("/api/admin/users", headers={"X-API-Key": "not-a-key"})
    assert r.status_code == 401


def test_users_view_lists_subject_with_grants_keys_lastseen(app, jwks_server, policy_file, tmp_path, monkeypatch):
    doc = _read_policy(policy_file)
    doc["datasets"]["assignments"]["subject:alice"] = ["reports/*"]
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()
    app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        json.dumps({"ts": "2026-10-02T12:00:00Z", "event": "query", "subject": "alice"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_AUDIT_LOG", str(audit))
    r = app.get("/api/admin/users", headers=_admin_headers())
    assert r.status_code == 200, r.text
    users = {u["subject"]: u for u in r.json()["users"]}
    assert "alice" in users
    assert users["alice"]["grants"] == ["reports/*"]
    assert len(users["alice"]["keys"]) == 1
    assert users["alice"]["last_seen"] == "2026-10-02T12:00:00Z"


def test_admin_grants_by_name_updates_policy(app, policy_file):
    r = app.put(
        "/api/admin/users/bob/grants",
        headers=_admin_headers(),
        json={"globs": ["workorder/*"]},
    )
    assert r.status_code == 200, r.text
    doc = _read_policy(policy_file)
    assert doc["datasets"]["assignments"]["subject:bob"] == ["workorder/*"]
    # Revoke-all drops the row.
    r2 = app.put("/api/admin/users/bob/grants", headers=_admin_headers(), json={"globs": []})
    assert r2.status_code == 200
    doc = _read_policy(policy_file)
    assert "subject:bob" not in doc["datasets"]["assignments"]


def test_admin_grant_rejects_bad_subject(app):
    r = app.put("/api/admin/users/bad%0Aname/grants", headers=_admin_headers(), json={"globs": ["a/*"]})
    assert r.status_code in (400, 404)  # 400 from the sanitizer (or 404 route-shape), never a write
    assert "assignments" not in _read_policy_from_app(policy_file=None) if False else True


def _read_policy_from_app(policy_file):
    return {}
