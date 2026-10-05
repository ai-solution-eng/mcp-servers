"""Self-service SSO key minting + the admin Users view (2026-10-02).

The feature: users authenticate with their SSO bearer (JWT rung) and mint a
long-lived key BOUND TO THEIR OWN VERIFIED SUBJECT (the X-API-KEY for /mcp);
admins see every known subject with grants/keys/last-seen and grant BY NAME.

Security contracts pinned here:

* JWT rung only — a relay/key/browser caller can NEVER mint (the relay rung
  is the spoofing class; the self-mint must never mint another's identity).
* The subject comes from the VERIFIED token, never request input (no
  parameter names a user).
* NO POLICY WRITE (live 2026-10-05, G2: the policy file is a read-only
  ConfigMap mount — "[Errno 30] Read-only file system"): the minted key is
  SUBJECT-BOUND, and the identity ladder resolves subject-bound keys to a
  subject-carrying Caller, so grants BY NAME apply to the human AND to the
  key — inherited LIVE through the subject binding, same hot-reload. The
  mint is pure keys-store work: create entry, cap, audit. Zero policy
  writes; explicit 'assign' is refused (custom-scoped keys = admin mint).
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


def _jwks_doc(*keys) -> dict:
    return {"keys": [k["jwk"] for k in keys]}


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
    """Pre-grant a subject BY NAME in the policy file."""
    doc = _read_policy(policy_file)
    doc["datasets"]["assignments"][f"subject:{subject}"] = globs
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()


# ---------------------------------------------------------------------------
# the mint: happy path + the security contracts
# ---------------------------------------------------------------------------


def test_sso_user_mints_key_bound_to_own_subject(app, jwks_server):
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"] == "alice"
    assert body["key"].startswith("http") is False and len(body["key"]) >= 32
    # The store entry carries the binding; the raw key is in the response only.
    entry = next(e for e in _admin_keys.list_keys() if e["fp"] == body["fp"])
    assert entry["subject"] == "alice"
    assert body["key"] not in json.dumps(_admin_keys.list_keys())


def test_mint_writes_nothing_to_the_policy_file(app, jwks_server, policy_file):
    """THE no-write contract (live 2026-10-05: the policy file is a
    read-only ConfigMap mount on real deployments). The mint is pure
    keys-store work — the policy file is byte-identical after minting, and
    the fp gets NO assignment row: grants ride the SUBJECT binding live."""
    before = _read_policy(policy_file)
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    assert _read_policy(policy_file) == before
    assert r.json()["fp"] not in (_read_policy(policy_file)["datasets"].get("assignments") or {})


def test_minted_key_carries_subject_policy_assignment(app, jwks_server, policy_file):
    """Pre-grant alice BY NAME, then mint: NO policy write — the key
    inherits her grants LIVE through its subject binding (groups_for falls
    through the absent fp row to the subject row). Verified here via the
    compiled policy: the minted fp is absent, the subject row is intact."""
    _grant(policy_file, "alice", ["reports/*"])
    before = _read_policy(policy_file)
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    assert _read_policy(policy_file) == before
    # And the resolution the key will get at request time: fp absent →
    # subject binding → alice's groups.
    pol = _policy.policy_store().get()
    fp = r.json()["fp"]
    assert fp not in pol.key_fps
    assert pol.groups_for("alice", fp) == pol.groups_for("alice", None)


def test_mint_carries_global_default_when_no_assignment(app, jwks_server, policy_file):
    """G2's live shape (global=["*"], EMPTY assignments): the subject HAS
    access through the default group — the mint succeeds with ZERO policy
    writes; the subject-bound key inherits the default LIVE."""
    before = _read_policy(policy_file)
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"] == "alice"
    assert _read_policy(policy_file) == before
    assert body["fp"] not in (_read_policy(policy_file)["datasets"].get("assignments") or {})


def test_mint_with_matches_nothing_global_succeeds_uselessly(app, jwks_server, policy_file):
    """A global that matches NO table: the subject HAS the grant (the
    default group's visible_tables), so the mint succeeds — and the key is
    exactly as useless as the live identity. NO policy write."""
    doc = _read_policy(policy_file)
    doc["datasets"]["global"] = ["nonexistent_table/*"]
    policy_file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _policy.reset_policy_store()
    before = _read_policy(policy_file)
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 201, r.text
    assert _read_policy(policy_file) == before


def test_mint_rejects_explicit_assign_403(app, jwks_server):
    """The self-mint cannot accept custom 'assign' globs — a mint that
    wrote ANY policy row would fail on the read-only ConfigMap mount, and
    custom-scoped keys are the ADMIN mint's job."""
    r = app.post(
        "/api/admin/keys/self",
        headers=_sso_headers(jwks_server),
        json={"assign": ["*"]},
    )
    assert r.status_code == 403, r.text
    assert "assign" in r.json()["error"]
    assert _admin_keys.list_keys() == []


def test_mint_subject_never_from_request_input(app, jwks_server):
    # There is no subject parameter; an attempt to sneak one changes nothing.
    r = app.post(
        "/api/admin/keys/self",
        headers=_sso_headers(jwks_server, sub="alice"),
        json={"subject": "bob", "label": "evil"},
    )
    assert r.status_code == 201
    assert r.json()["subject"] == "alice"  # the VERIFIED subject, not the input


def test_mint_requires_sso_403_for_key_callers(app):
    r = app.post("/api/admin/keys/self", headers=_admin_headers(), json={})
    assert r.status_code == 403 and "SSO" in r.json()["error"]


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


def test_mint_without_keys_store_503(app, monkeypatch, jwks_server):
    monkeypatch.delenv("SQLHANDLER_ADMIN_KEYS_FILE", raising=False)
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r.status_code == 503
    assert "keys store" in r.json()["error"] or "not configured" in r.json()["error"]


# ---------------------------------------------------------------------------
# revoke-to-rotate + self-revoke ownership
# ---------------------------------------------------------------------------


def test_revoke_to_rotate_one_key_per_subject(app, jwks_server):
    r1 = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    r2 = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    assert r1.status_code == 201 and r2.status_code == 201
    mine = [e for e in _admin_keys.list_keys() if e.get("subject") == "alice"]
    assert len(mine) == 1, "the cap rotates the oldest out"


def test_self_list_and_revoke_own_keys(app, jwks_server):
    r = app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    fp = r.json()["fp"]
    listed = app.get("/api/admin/keys/self", headers=_sso_headers(jwks_server))
    assert listed.status_code == 200
    assert [k["fp"] for k in listed.json()["keys"]] == [fp]
    # A foreign/unknown fp is 404 (store-verified ownership).
    miss = app.delete("/api/admin/keys/self/sha256:000000000000", headers=_sso_headers(jwks_server))
    assert miss.status_code == 404
    gone = app.delete(f"/api/admin/keys/self/{fp}", headers=_sso_headers(jwks_server))
    assert gone.status_code == 200
    assert all(k["fp"] != fp for k in _admin_keys.list_keys())


# ---------------------------------------------------------------------------
# the Users view (admin-gated)
# ---------------------------------------------------------------------------


def test_users_view_lists_sso_subjects_admin_gated(app, jwks_server, keys_file):
    app.post("/api/admin/keys/self", headers=_sso_headers(jwks_server), json={})
    # Admin-gated: anon/key callers refused.
    assert app.get("/api/admin/users").status_code == 401
    assert app.get("/api/admin/users", headers={"X-API-Key": "wrong"}).status_code == 401
    ok = app.get("/api/admin/users", headers=_admin_headers())
    assert ok.status_code == 200, ok.text
    users = ok.json()["users"]
    assert any(u["subject"] == "alice" for u in users)


def test_grant_by_name_writes_assignments(app, jwks_server, policy_file, keys_file):
    _grant(policy_file, "bob", ["reports/*"])
    r = app.put(
        "/api/admin/users/bob/grants",
        headers=_admin_headers(),
        json={"globs": ["reports/*", "workorder/*"]},
    )
    assert r.status_code == 200, r.text
    doc = _read_policy(policy_file)
    assert doc["datasets"]["assignments"]["subject:bob"] == ["reports/*", "workorder/*"]
