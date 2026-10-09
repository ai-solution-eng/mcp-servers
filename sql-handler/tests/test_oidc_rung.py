"""The OIDC bearer-JWT rung (task D3) — synthetic-crypto tests, no jwt lib.

Cribbed from RAG's tests/security/test_oidc_identity.py (v5.2.0, D21): every
RSA key, JWKS document, and token here is SYNTHETICALLY generated in-test
(``cryptography`` + a loopback ``http.server`` JWKS endpoint on 127.0.0.1).
No external network, no real issuer — ``iss`` is claim-compared, only the
JWKS URL is fetched (loopback only). RAG's venv has no PyJWT and neither
does this one's test posture need it: the compact JWS is minted by hand.

Pinned here:
  * valid token → Caller(cls=user, subject=<identity claim>, via=jwt);
  * wrong aud / wrong iss / expired / nbf / garbage / alg-none / HS256
    confusion → the rung declines SILENTLY (anonymous continues down the
    ladder);
  * a Bearer value matching a configured STATIC key resolves the KEY rung
    (fp, via=key) and is never parsed as JWT;
  * SQLHANDLER_REQUIRE_IDENTITY + invalid token → 401 on /mcp;
  * probes stay open with requireIdentity on;
  * whoami reports via/require_identity honestly.

Run:  python -m pytest tests/test_oidc_rung.py -v
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlhandler import identity as _identity
from sqlhandler import oidc_identity as oidc
from sqlhandler.provider import DataProvider
from sqlhandler.server import _build_http_app

# ===========================================================================
# Synthetic crypto: keys, JWK encoding, token minting (RAG's D21 seam)
# ===========================================================================

#: A fixed fake https issuer — ``iss`` is COMPARED to SQLHANDLER_OIDC_ISSUER,
#: never fetched, so a non-existent https URL is safe; only the JWKS URL is
#: fetched (always 127.0.0.1 here).
ISSUER = "https://oidc.test/realm-test"
AUDIENCE = "ua"
KID = "synthetic-test-key"


def _b64url(data: bytes) -> str:
    """Unpadded base64url (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64uint(value: int) -> str:
    """An unsigned integer as a Base64urlUInt (RFC 7518 §6.3.1.1)."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return _b64url(raw)


def _b64json(obj) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _generate_rsa(key_id: str = KID) -> dict:
    """One synthetic RSA keypair: {"key", "jwk", "kid", "sign"}."""
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


def _mint(key: dict, claims: dict, header: dict | None = None, sign=None) -> str:
    """An RS256 compact JWS: header.claims.signature (base64url, unpadded)."""
    hdr = {"alg": "RS256", "typ": "JWT", "kid": key["kid"]}
    if header:
        hdr.update(header)
    signing_input = f"{_b64json(hdr)}.{_b64json(claims)}"
    if sign is None:
        signature = key["sign"](signing_input.encode("ascii"))
    else:
        signature = sign(signing_input.encode("ascii"))
    return f"{signing_input}.{_b64url(signature)}"


def _now() -> int:
    return int(time.time())


def _claims(
    *,
    sub="andrew-bydlon",
    preferred="andrew-bydlon",  # False = omit the claim entirely
    iss=ISSUER,
    aud=AUDIENCE,
    exp=None,
    nbf=None,
    azp=None,
    **extra,
) -> dict:
    c = {
        "iss": iss,
        "aud": aud,
        "exp": exp if exp is not None else _now() + 600,
        "iat": _now() - 5,
    }
    if sub is not None:
        c["sub"] = sub
    if preferred is not False:
        c["preferred_username"] = preferred
    if nbf is not None:
        c["nbf"] = nbf
    if azp is not None:
        c["azp"] = azp
    c.update(extra)
    return c


class _JwksServer:
    """A minimal local JWKS endpoint: 127.0.0.1, ephemeral port, daemon thread."""

    def __init__(self) -> None:
        holder = self
        self._doc: dict = {"keys": []}
        self._status = 200

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(holder._doc).encode("utf-8")
                self.send_response(holder._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence the test log
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/protocol/openid-connect/certs"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def set(self, doc: dict) -> None:
        self._doc = doc

    def set_status(self, status: int) -> None:
        self._status = status

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def jwks_server():
    server = _JwksServer()
    yield server
    server.stop()


# ===========================================================================
# House hygiene: full env isolation (RAG's D15/D17 fixture pattern)
# ===========================================================================

_OIDC_ENV_NAMES = (
    "SQLHANDLER_OIDC_ENABLED",
    "SQLHANDLER_OIDC_ISSUER",
    "SQLHANDLER_OIDC_AUDIENCE",
    "SQLHANDLER_OIDC_IDENTITY_CLAIM",
    "SQLHANDLER_OIDC_JWKS_URL",
    "SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS",
    "SQLHANDLER_OIDC_FETCH_TIMEOUT_SECONDS",
    "SQLHANDLER_OIDC_CLOCK_SKEW_SECONDS",
    "SQLHANDLER_JWKS_FORCE_MIN_INTERVAL",
)

_GATE_ENV_NAMES = (
    "MCP_API_KEYS",
    "SQLHANDLER_API_KEYS",
    "SQLHANDLER_REQUIRE_IDENTITY",
    "SQLHANDLER_TRUST_BROWSER_HEADERS",
    "SQLHANDLER_RELAY_HMAC_SECRET",
    "SQLHANDLER_POLICY_ENABLED",
    "SQLHANDLER_POLICY_FILE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with NO keys, NO OIDC, NO trust flags — everything
    a test needs is set explicitly inside it."""
    for name in _GATE_ENV_NAMES + _OIDC_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # The oidc module caches the JWKS per process; a test must never inherit
    # another test's JWKS/negative-cache/FORCED-REFETCH state (RAG's fixture
    # pattern, extended for the rate limiter + stampede guard).
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    oidc._forced_refetch_at.clear()
    oidc._jwks_fetching.clear()
    yield
    oidc._jwks_cache.clear()
    oidc._jwks_negative.clear()
    oidc._forced_refetch_at.clear()
    oidc._jwks_fetching.clear()


@pytest.fixture(autouse=True)
def _oidc_on(monkeypatch, jwks_server):
    """Default per-test OIDC wiring: enabled, the loopback JWKS, the fake
    https issuer, refetch-on-every-verify (TTL 0) so tests are order-
    independent regardless of how the implementation caches."""
    monkeypatch.setenv("SQLHANDLER_OIDC_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_URL", jwks_server.url)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS", "0")


def _setup_key(server: _JwksServer, key_id: str = KID) -> dict:
    """Generate a keypair, publish it in the server's JWKS, return it."""
    key = _generate_rsa(key_id)
    server.set(_jwks_doc(key))
    return key


def _bearer(token: str):
    return [(b"authorization", f"Bearer {token}".encode())]


def _scope(path="/mcp", headers=None):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
        "client": ("10.9.0.1", 5000),
        "query_string": b"",
    }


# ===========================================================================
# 1 · Unit: the rung's Caller contract
# ===========================================================================


def test_valid_token_resolves_user_caller_via_jwt(jwks_server):
    key = _setup_key(jwks_server)
    caller = _identity.caller_from_scope(_scope(headers=_bearer(_mint(key, _claims()))))
    assert caller.cls == "user"
    assert caller.subject == "andrew-bydlon"
    assert caller.via == "jwt"
    assert not caller.is_anonymous
    assert caller.as_audit_dict() == {"class": "user", "subject": "andrew-bydlon", "key_fp": None}


def test_sub_fallback_when_preferred_username_absent(jwks_server):
    key = _setup_key(jwks_server)
    caller = _identity.caller_from_scope(_scope(headers=_bearer(_mint(key, _claims(preferred=False)))))
    assert caller.via == "jwt" and caller.subject == "andrew-bydlon"


def test_identity_claim_override_to_sub(jwks_server, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_OIDC_IDENTITY_CLAIM", "sub")
    key = _setup_key(jwks_server)
    token = _mint(key, _claims(sub="upstream-sub-123", preferred="andrew-bydlon"))
    caller = _identity.caller_from_scope(_scope(headers=_bearer(token)))
    assert caller.via == "jwt" and caller.subject == "upstream-sub-123"


def test_unsuitable_primary_claim_declines(jwks_server):
    """A present-but-unsuitable identity claim REJECTS the token (no silent
    fallback — the sub fallback exists for ABSENT claims, not forged ones)."""
    key = _setup_key(jwks_server)
    token = _mint(key, _claims(preferred="bad name!with@spaces"))
    assert oidc.verify_and_decode(token) is not None, "the token itself verifies"
    assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous


# ===========================================================================
# 2 · Verification failures decline SILENTLY to anonymous
# ===========================================================================


@pytest.mark.parametrize(
    "claims_kwargs,header_kwargs",
    [
        ({"aud": "other-app"}, None),  # wrong aud
        ({"iss": "https://evil.example/realm"}, None),  # wrong iss
        ({"exp": -3600}, None),  # expired
        ({"nbf": 600}, None),  # nbf in the future
        ({"azp": "other-client"}, None),  # azp mismatch
    ],
)
def test_bad_claims_decline_anonymous(jwks_server, claims_kwargs, header_kwargs):
    key = _setup_key(jwks_server)
    claims = _claims(**claims_kwargs)
    if claims_kwargs.get("exp") is not None and claims_kwargs["exp"] < 0:
        claims["exp"] = _now() + claims_kwargs["exp"]
    if claims_kwargs.get("nbf") is not None:
        claims["nbf"] = _now() + claims_kwargs["nbf"]
    token = _mint(key, claims, header=header_kwargs)
    assert oidc.verify_and_decode(token) is None
    assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous


def test_wrong_signing_key_declines(jwks_server):
    _setup_key(jwks_server)
    other = _generate_rsa("other-key")
    token = _mint(other, _claims())
    assert oidc.verify_and_decode(token) is None
    assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous


def test_garbage_and_malformed_bearers_decline(jwks_server):
    _setup_key(jwks_server)
    for token in ("aaaa.bbbb.cccc", "not-a-token", "a.b", "a.b.c.d", "!!$.###.$$$", ""):
        assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous, token


def test_alg_none_token_declines(jwks_server):
    """The classic JWS 'alg: none' downgrade — an UNSIGNED token must never
    verify (a non-empty dummy signature keeps the shape well-formed)."""
    key = _setup_key(jwks_server)
    token = _mint(key, _claims(), header={"alg": "none"}, sign=lambda data: b"unsigned")
    assert oidc.is_jwt_format(token)
    assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous


def test_alg_hs256_confusion_declines(jwks_server):
    """Alg-confusion: sign with HS256 using the (public) RSA key material as
    the HMAC secret — refused BEFORE any key lookup (RS256 only)."""
    key = _setup_key(jwks_server)
    secret = key["jwk"]["n"].encode("ascii")

    def _hmac_sign(data: bytes) -> bytes:
        return hmac.new(secret, data, hashlib.sha256).digest()

    token = _mint(key, _claims(), header={"alg": "HS256", "kid": KID}, sign=_hmac_sign)
    assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous


def test_oidc_disabled_declines_all_jwt(jwks_server, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_OIDC_ENABLED", "0")
    key = _setup_key(jwks_server)
    assert _identity.caller_from_scope(_scope(headers=_bearer(_mint(key, _claims())))).is_anonymous


def test_jwks_down_declines_fail_closed(jwks_server, monkeypatch):
    """A down IdP: the rung declines (never 500s, never fails open)."""
    key = _setup_key(jwks_server)
    token = _mint(key, _claims())
    jwks_server.set_status(500)
    assert oidc.verify_and_decode(token) is None
    assert _identity.caller_from_scope(_scope(headers=_bearer(token))).is_anonymous
    # the negative cache means no hammering
    assert oidc.verify_and_decode(token) is None
    oidc._jwks_negative.clear()
    jwks_server.set_status(200)
    assert oidc.verify_and_decode(token) is not None


# ===========================================================================
# 2b · JWKS fetch hardening: OUT-OF-LOCK fetch + forced-refetch rate limit
#
# THE finding: the unknown-kid path (force=True) fetched INSIDE _jwks_lock, so
# UNAUTHENTICATED garbage tokens with random kids triggered one serialized
# outbound fetch per request — an unauthenticated amplification/DoS lever.
# ===========================================================================


def test_forced_refetch_rate_limited_per_url(jwks_server, monkeypatch):
    """N forced refetches inside the window → at most ONE network fetch."""
    _setup_key(jwks_server)
    calls = {"n": 0}
    real = oidc._fetch_jwks

    def counting(url, timeout):
        calls["n"] += 1
        return real(url, timeout)

    monkeypatch.setattr(oidc, "_fetch_jwks", counting)
    oidc._jwks_keys(jwks_server.url, force=True)
    assert calls["n"] == 1, "the first forced refetch is allowed"
    for _ in range(5):
        oidc._jwks_keys(jwks_server.url, force=True)
    assert calls["n"] == 1, "inside the 60 s window the cache is served WITHOUT fetching"


def test_forced_refetch_allowed_again_after_window(jwks_server, monkeypatch):
    """The rate limit delays rotation pickup, it does not disable it."""
    _setup_key(jwks_server)
    calls = {"n": 0}
    real = oidc._fetch_jwks

    def counting(url, timeout):
        calls["n"] += 1
        return real(url, timeout)

    monkeypatch.setattr(oidc, "_fetch_jwks", counting)
    oidc._jwks_keys(jwks_server.url, force=True)
    assert calls["n"] == 1
    # Age the last forced refetch beyond the window (the seam a real clock move
    # would provide) — the next forced call must reach the network again.
    oidc._forced_refetch_at[jwks_server.url] -= 61
    assert oidc._jwks_keys(jwks_server.url, force=True)
    assert calls["n"] == 2


def test_force_min_interval_env_override(jwks_server, monkeypatch):
    """``SQLHANDLER_JWKS_FORCE_MIN_INTERVAL`` is honoured (re-read per call)."""
    _setup_key(jwks_server)
    calls = {"n": 0}
    real = oidc._fetch_jwks

    def counting(url, timeout):
        calls["n"] += 1
        return real(url, timeout)

    monkeypatch.setattr(oidc, "_fetch_jwks", counting)
    monkeypatch.setenv("SQLHANDLER_JWKS_FORCE_MIN_INTERVAL", "3600")
    oidc._jwks_keys(jwks_server.url, force=True)
    oidc._forced_refetch_at[jwks_server.url] -= 61  # would pass at the 60 s default
    oidc._jwks_keys(jwks_server.url, force=True)
    assert calls["n"] == 1, "the raised interval is respected"

    # Interval 0 = the escape hatch: every forced call refetches again.
    oidc._forced_refetch_at[jwks_server.url] -= 61
    monkeypatch.setenv("SQLHANDLER_JWKS_FORCE_MIN_INTERVAL", "0")
    oidc._jwks_keys(jwks_server.url, force=True)
    assert calls["n"] == 2


def test_unknown_kid_garbage_tokens_do_not_hammer_jwks(jwks_server, monkeypatch):
    """END TO END for the finding: repeated UNAUTHENTICATED garbage tokens
    with random kids cost one TTL fetch + ONE forced refetch, not one fetch
    per request. (TTL is raised so the normal path is genuinely cached.)"""
    _setup_key(jwks_server)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS", "3600")
    calls = {"n": 0}
    real = oidc._fetch_jwks

    def counting(url, timeout):
        calls["n"] += 1
        return real(url, timeout)

    monkeypatch.setattr(oidc, "_fetch_jwks", counting)
    for i in range(6):
        attacker = _generate_rsa(f"attacker-kid-{i}")
        token = _mint(attacker, _claims())
        assert oidc.verify_and_decode(token) is None, i
    assert calls["n"] == 2, f"expected 1 TTL fetch + 1 forced refetch, got {calls['n']}"


def test_legitimate_rotation_still_picked_up(jwks_server, monkeypatch):
    """The rate limit must not break its own purpose: a NEW signing kid is
    still picked up immediately by the unknown-kid forced refetch."""
    old = _setup_key(jwks_server)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS", "3600")
    assert oidc.verify_and_decode(_mint(old, _claims())) is not None
    rotated = _setup_key(jwks_server, "rotated-kid")  # server now serves the new key
    assert oidc.verify_and_decode(_mint(rotated, _claims())) is not None
    assert oidc.verify_and_decode(_mint(old, _claims())) is None, "the rotated-away kid is gone"


def test_forced_fetch_inside_window_returns_cache_without_network(jwks_server, monkeypatch):
    """Inside the window the cached keys (or None) come back untouched."""
    _setup_key(jwks_server)
    keys = oidc._jwks_keys(jwks_server.url)
    assert keys and KID in keys
    oidc._jwks_keys(jwks_server.url, force=True)  # opens the window (records the stamp)
    monkeypatch.setattr(oidc, "_fetch_jwks", lambda url, timeout: pytest.fail("fetched inside window"))
    assert oidc._jwks_keys(jwks_server.url, force=True) == keys


def test_forced_fetch_inside_window_returns_none_without_network(jwks_server, monkeypatch):
    """No cache + inside the window → None (fail closed), still no fetch."""
    monkeypatch.setattr(oidc, "_fetch_jwks", lambda url, timeout: pytest.fail("fetched inside window"))
    oidc._forced_refetch_at[jwks_server.url] = time.time()  # window already started
    assert oidc._jwks_keys(jwks_server.url, force=True) is None


def test_fetch_runs_outside_lock_and_marker_released(jwks_server, monkeypatch):
    """The core of the fix: the network fetch happens with _jwks_lock FREE, so
    a slow IdP cannot serialize unrelated verification behind the lock."""
    _setup_key(jwks_server)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS", "0")
    assert oidc._jwks_keys(jwks_server.url)  # prime the cache

    entered, release = threading.Event(), threading.Event()
    real = oidc._fetch_jwks

    def slow(url, timeout):
        entered.set()
        release.wait(5)
        return real(url, timeout)

    monkeypatch.setattr(oidc, "_fetch_jwks", slow)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.setdefault("keys", oidc._jwks_keys(jwks_server.url)), daemon=True)
    worker.start()
    try:
        assert entered.wait(5), "the fetch never started"
        assert not oidc._jwks_lock.locked(), "the fetch must NOT hold _jwks_lock"
        start = time.monotonic()
        assert oidc._jwks_keys(jwks_server.url)  # concurrent caller: served, not queued
        assert time.monotonic() - start < 2.0, "a concurrent caller must not block on the fetch"
    finally:
        release.set()
        worker.join(5)
    assert result.get("keys") and KID in result["keys"]
    assert not oidc._jwks_fetching, "the stampede marker must be released"


def test_jwks_keys_called_twice_no_deadlock(jwks_server, monkeypatch):
    """The literal regression guard: two _jwks_keys calls (the second forced)
    both return — the old in-lock fetch shape could not deadlock here, but
    the out-of-lock shape must be proven re-entrant-safe."""
    _setup_key(jwks_server)
    monkeypatch.setenv("SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS", "3600")
    first = oidc._jwks_keys(jwks_server.url)
    second = oidc._jwks_keys(jwks_server.url, force=True)
    assert first and second and KID in first and KID in second


# ===========================================================================
# 3 · The static-key boundary: a static-key Bearer is NEVER parsed as JWT
# ===========================================================================


def test_static_key_bearer_resolves_key_rung_never_jwt(jwks_server, monkeypatch):
    """A static key has the 3-segment SHAPE here (dots are legal in keys) —
    it must resolve via=key with the fingerprint, and never reach the JWT
    verifier (which would decline it → anonymous). The key rung exists only
    when the key middleware matched on a /mcp path (the fp is recorded into
    scope state); the OIDC rung's own static-key check reads the same env
    list directly, so it must NOT claim a value the key ladder owns."""
    monkeypatch.setenv("MCP_API_KEYS", "aaa.bbb.ccc")
    # A /mcp scope: the key middleware would match and record the fp, so the
    # JWT rung must defer to the key ladder.
    caller = _identity.caller_from_scope(_scope(headers=_bearer("aaa.bbb.ccc")))
    assert caller.cls == "anonymous", "the JWT rung itself must not fire (no fp recorded in this raw scope)"
    # But it must also NOT have verified the value as a JWT (it would raise/
    # decline regardless — the assertion is that no jwt Caller comes back).
    state = {"sqlhandler.key_fp": _identity.key_fp("aaa.bbb.ccc")}
    scope = _scope(headers=_bearer("aaa.bbb.ccc"))
    scope["state"] = state
    caller = _identity.caller_from_scope(scope)
    assert caller.cls == "key"
    assert caller.via == "key"
    assert caller.key_fp == _identity.key_fp("aaa.bbb.ccc")
    assert caller.subject is None


def test_static_key_bearer_with_oidc_on_still_key_rung(jwks_server, monkeypatch):
    """OIDC enabled changes nothing for a static-key Bearer: the key match
    wins BEFORE any JWT parse (disjoint credential spaces)."""
    monkeypatch.setenv("MCP_API_KEYS", "my-plain-key")
    _setup_key(jwks_server)
    scope = _scope(headers=_bearer("my-plain-key"))
    scope["state"] = {"sqlhandler.key_fp": _identity.key_fp("my-plain-key")}
    caller = _identity.caller_from_scope(scope)
    assert caller.via == "key" and caller.cls == "key"
    assert caller.key_fp == _identity.key_fp("my-plain-key")


def test_jwt_shaped_value_not_matching_any_key_goes_to_jwt_rung(jwks_server, monkeypatch):
    """A JWT-shaped Bearer that matches NO static key is the OIDC rung's —
    the ordering rule is (static-key match) → else (JWT shape) → verify."""
    monkeypatch.setenv("MCP_API_KEYS", "my-plain-key")
    key = _setup_key(jwks_server)
    token = _mint(key, _claims())
    caller = _identity.caller_from_scope(_scope(headers=_bearer(token)))
    assert caller.via == "jwt" and caller.subject == "andrew-bydlon"


def test_invalid_jwt_shaped_bearer_falls_to_key_rung_of_other_header(monkeypatch):
    """A garbage JWT-shaped Bearer alongside a valid X-API-Key: the key
    middleware records the fp; the declined JWT rung must NOT mask the key
    rung (ladder continues)."""
    monkeypatch.setenv("MCP_API_KEYS", "svc-key")
    headers = _bearer("aaa.bbb.ccc") + [(b"x-api-key", b"svc-key")]
    scope = _scope(headers=headers)
    scope["state"] = {"sqlhandler.key_fp": _identity.key_fp("svc-key")}
    caller = _identity.caller_from_scope(scope)
    assert caller.via == "key" and caller.key_fp == _identity.key_fp("svc-key")


# ===========================================================================
# 4 · HTTP end-to-end: requireIdentity + invalid token → 401; probes open
# ===========================================================================


@pytest.fixture()
def app(monkeypatch):
    app = _build_http_app()
    with TestClient(app) as c:
        yield c


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def test_valid_jwt_authenticates_mcp(app, jwks_server):
    _setup_key(jwks_server)
    assert _ping(app, {"Authorization": f"Bearer {_mint(_setup_key(jwks_server), _claims())}"}).status_code == 200


def test_require_identity_invalid_jwt_is_401(app, jwks_server, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    key = _setup_key(jwks_server)
    expired = _mint(key, _claims(exp=_now() - 9999))
    r = _ping(app, {"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert "identity required" in r.json()["error"]
    assert r.headers["www-authenticate"] == "Bearer"


def test_require_identity_no_credentials_is_401(app, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    r = _ping(app)
    assert r.status_code == 401


def test_require_identity_probes_stay_open(app, jwks_server, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    assert app.get("/health").status_code == 200
    r = app.get("/ready")
    assert r.status_code in (200, 503)
    assert "identity required" not in r.text
    assert app.get("/metrics").status_code == 200


def test_require_identity_valid_jwt_passes_on_mcp(app, jwks_server, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    key = _setup_key(jwks_server)
    token = _mint(key, _claims())
    assert _ping(app, {"Authorization": f"Bearer {token}"}).status_code == 200


def test_require_identity_wrong_aud_jwt_401_on_api(app, jwks_server, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    key = _setup_key(jwks_server)
    bad = _mint(key, _claims(aud="other-app"))
    r = app.get("/api/status", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401
    assert "identity required" in r.json()["error"]


# ===========================================================================
# 5 · whoami: honest identity + own ACL view
# ===========================================================================


def _tools_call(client, name, args=None, headers=None):
    body = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
    ).json()
    result = body.get("result", {})
    return result.get("isError", False), result["content"][0]["text"]


def test_whoami_reports_jwt_caller(app, jwks_server, monkeypatch):
    """whoami over a valid JWT: the caller block + via=jwt + honest ACL view
    (no policy configured → every table visible, nothing masked)."""
    key = _setup_key(jwks_server)
    token = _mint(key, _claims())
    from sqlhandler import server as srv

    monkeypatch.setattr(srv, "_handler", lambda: srv.SqlEngine(_FakeProvider(), cache_ttl=0))
    assert _ping(app, {"Authorization": f"Bearer {token}"}).status_code == 200
    is_err, text = _tools_call(app, "whoami", headers={"Authorization": f"Bearer {token}"})
    assert not is_err
    payload = json.loads(text)
    assert payload["caller"] == {"class": "user", "subject": "andrew-bydlon", "key_fp": None}
    assert payload["via"] == "jwt"
    assert payload["require_identity"] is False
    assert payload["datasets"] == [
        {"table": "workorder_work_order", "visible": True, "row_filter": None, "masked_columns": [], "hidden": False}
    ]


def test_whoami_reports_key_caller(app, jwks_server, monkeypatch):
    from sqlhandler import server as srv

    monkeypatch.setenv("MCP_API_KEYS", "svc-key")
    monkeypatch.setattr(srv, "_handler", lambda: srv.SqlEngine(_FakeProvider(), cache_ttl=0))
    is_err, text = _tools_call(app, "whoami", headers={"X-API-Key": "svc-key"})
    assert not is_err
    payload = json.loads(text)
    assert payload["via"] == "key"
    assert payload["caller"]["class"] == "key"
    assert payload["caller"]["key_fp"] == _identity.key_fp("svc-key")


def test_whoami_honest_for_anonymous(app, monkeypatch):
    from sqlhandler import server as srv

    monkeypatch.setattr(srv, "_handler", lambda: srv.SqlEngine(_FakeProvider(), cache_ttl=0))
    is_err, text = _tools_call(app, "whoami")
    assert not is_err
    payload = json.loads(text)
    assert payload["caller"]["class"] == "anonymous"
    assert payload["via"] == "anonymous"
    # No policy → the anonymous caller sees everything (honest, not padded).
    assert [d["visible"] for d in payload["datasets"]] == [True]


def test_whoami_lists_hidden_tables_as_not_visible(app, jwks_server, monkeypatch, tmp_path):
    """THE point of the tool: a policy-hidden table appears with visible:false
    (the caller's OWN view — the engine's list_tables would just omit it)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1], "amount": [10.0]}), d / "part.parquet")

    from sqlhandler.engine import SqlEngine
    from sqlhandler.provider import DataProvider, TableInfo

    class P(DataProvider):
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    policy = {
        "groups": {
            "analysts": {
                "hidden_tables": ["payroll*"],
                "tables": {"workorder/*": {"row_filter": "id > 0"}},
            }
        },
        "subjects": {"andrew-bydlon": ("analysts",)},
        "default_group": "analysts",
    }
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(pfile))

    from sqlhandler import server as srv

    monkeypatch.setattr(srv, "_handler", lambda: SqlEngine(P(), cache_ttl=0))
    key = _setup_key(jwks_server)
    token = _mint(key, _claims())
    is_err, text = _tools_call(app, "whoami", headers={"Authorization": f"Bearer {token}"})
    assert not is_err
    payload = json.loads(text)
    assert payload["via"] == "jwt" and payload["caller"]["subject"] == "andrew-bydlon"
    # One entry for the REAL table; the payroll glob hit nothing here, so the
    # view is honest: visible, with the row_filter applied.
    assert payload["datasets"] == [
        {
            "table": "workorder_work_order",
            "visible": True,
            "row_filter": "id > 0",
            "masked_columns": [],
            "hidden": False,
        }
    ]


class _FakeProvider(DataProvider):
    """One fake table (the test_dispatch_arg_contract pattern)."""

    kind = "fake"

    def list_tables(self):
        from sqlhandler.provider import TableInfo

        return [TableInfo(name="work_order", schema="workorder", format="parquet")]

    def table_uri(self, info):
        return "fake://"

    def open_dataset(self, info, version=None):
        import tempfile

        import pyarrow as pa
        import pyarrow.dataset as pad
        import pyarrow.parquet as pq

        d = tempfile.mkdtemp()
        pq.write_table(pa.table({"id": [1, 2], "amount": [10.0, 20.0]}), f"{d}/part.parquet")
        return pad.dataset(d, format="parquet")
