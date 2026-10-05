"""OIDC bearer-JWT verification for SQLhandler (vendored from MultimodalRAG).

Source: ``MultimodalRAG/src/multimodal_rag/utils/oidc_identity.py`` (fleet
decision D21 / RAG v5.2.0). Vendored (not hardlink-meshed) because
SQLhandler is a standalone deployment; the verification core is preserved
byte-for-byte in behavior:

* RS256 ONLY — ``none`` and the ``HS*`` family are refused outright (the
  classic alg-confusion guard: a JWKS public key must never be abused as an
  HMAC secret);
* ``iss`` equality, ``exp``/``nbf`` with clock-skew leeway, and
  ``azp``/``aud`` audience validation;
* the JWKS is fetched with stdlib urllib, TLS verification ALWAYS ON
  (``REMOTE_CA_BUNDLE`` may point at a platform CA bundle — a dedicated SSL
  context trusts it, never disables verification), cached under a lock with
  a TTL; the fetch itself happens OUTSIDE the lock (a module-level
  ``_jwks_fetching`` set keeps concurrent callers from stampeding, and
  nobody holds the lock across a network round-trip); a failed fetch arms a
  60 s negative cache so a down IdP cannot be hammered per request; an
  unknown ``kid`` forces ONE immediate refetch so signing-key rotation is
  picked up without waiting out the TTL — RATE LIMITED to at most one forced
  refetch per ``SQLHANDLER_JWKS_FORCE_MIN_INTERVAL`` seconds per URL
  (default 60), so unauthenticated garbage tokens with random ``kid``\\ s
  cannot drive one serialized outbound fetch per request;
* in-cluster proxy isolation: ``*.svc`` / ``*.svc.cluster.local`` /
  ``*.local`` / ``localhost`` JWKS hosts bypass any ambient proxy;
* every failure returns ``None`` (fail-closed) and logs a short reason code
  at DEBUG (no token material, no per-request log spam);
* there is deliberately NO OIDC discovery and NO caching of verification
  RESULTS — only the JWKS document is cached (same as RAG).

Env mapping (RAG -> SQLhandler; read PER REQUEST, a config change needs no
restart — the fleet grep should find both spellings in this one docstring):

* ``RAG_OIDC_ENABLED``               -> ``SQLHANDLER_OIDC_ENABLED``
* ``RAG_OIDC_ISSUER``                -> ``SQLHANDLER_OIDC_ISSUER``
* ``RAG_OIDC_AUDIENCE``              -> ``SQLHANDLER_OIDC_AUDIENCE``
* ``RAG_OIDC_IDENTITY_CLAIM``        -> ``SQLHANDLER_OIDC_IDENTITY_CLAIM``
* ``RAG_OIDC_JWKS_URL``              -> ``SQLHANDLER_OIDC_JWKS_URL``
* ``RAG_OIDC_JWKS_REFRESH_SECONDS``  -> ``SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS``
* ``RAG_OIDC_FETCH_TIMEOUT_SECONDS`` -> ``SQLHANDLER_OIDC_FETCH_TIMEOUT_SECONDS``
* ``RAG_OIDC_CLOCK_SKEW_SECONDS``    -> ``SQLHANDLER_OIDC_CLOCK_SKEW_SECONDS``
* (no RAG counterpart)                ``SQLHANDLER_JWKS_FORCE_MIN_INTERVAL``

RAG-only concepts deliberately NOT carried over (SQLhandler has no per-user
dataset registry to consult): ``RAG_OIDC_OBSERVED_TTL`` and the
``{DATA_PATH}/access/observed.json`` sidecar, the admin/clients registry
consultation, and the overlay ``oidc`` alias / ``blocked`` machinery. This
module resolves VERIFIED CLAIMS; the caller rung in ``sqlhandler.identity``
decides what they mean. The subject convention matches RAG's registry
identity: the ``SQLHANDLER_OIDC_IDENTITY_CLAIM`` claim (default
``preferred_username``) with ``sub`` as the absent-claim fallback — a
present-but-unsuitable primary claim REJECTS the token rather than silently
falling back (the fallback exists for absent claims, not forged ones).

Dependency: stdlib + ``cryptography`` for the RS256 verify (present in the
SQLhandler venv). If ``cryptography`` were ever missing, verification
fail-CLOSED (every JWT declines) — never a hand-rolled crypto path.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("sqlhandler.oidc_identity")

ENABLED_ENV = "SQLHANDLER_OIDC_ENABLED"
ISSUER_ENV = "SQLHANDLER_OIDC_ISSUER"
AUDIENCE_ENV = "SQLHANDLER_OIDC_AUDIENCE"
IDENTITY_CLAIM_ENV = "SQLHANDLER_OIDC_IDENTITY_CLAIM"
JWKS_URL_ENV = "SQLHANDLER_OIDC_JWKS_URL"
JWKS_REFRESH_ENV = "SQLHANDLER_OIDC_JWKS_REFRESH_SECONDS"
FETCH_TIMEOUT_ENV = "SQLHANDLER_OIDC_FETCH_TIMEOUT_SECONDS"
CLOCK_SKEW_ENV = "SQLHANDLER_OIDC_CLOCK_SKEW_SECONDS"
FORCE_MIN_INTERVAL_ENV = "SQLHANDLER_JWKS_FORCE_MIN_INTERVAL"

DEFAULT_AUDIENCE = "ua"
DEFAULT_IDENTITY_CLAIM = "preferred_username"
DEFAULT_JWKS_REFRESH = 3600.0
DEFAULT_FETCH_TIMEOUT = 3.0
DEFAULT_CLOCK_SKEW = 60.0
#: Min seconds between two FORCED (unknown-kid) refetches of one JWKS URL.
#: The unknown-kid path is reachable with UNAUTHENTICATED garbage tokens, so
#: without this floor any client could drive one outbound fetch per request.
DEFAULT_FORCE_MIN_INTERVAL = 60.0

# The identity-claim fallback chain's last resort (stable, unique, opaque).
_FALLBACK_CLAIM = "sub"
# Down-IdP backoff: after a failed JWKS fetch, fail fast for this long.
_NEGATIVE_TTL = 60.0
# Response cap — a real JWKS is a few KB; anything larger is hostile.
_JWKS_MAX_BYTES = 1_048_576
# JWT segments are unpadded base64url (optional trailing '=' tolerated).
_JWT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

_TRUE = ("1", "true", "yes")
# RAG's registry name rules (admin_registry._NAME_RE): the identity claim /
# sub must yield a name of this shape — shared vocabulary, not a new one.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _env_str(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _flag_set() -> bool:
    return _env_str(ENABLED_ENV).lower() in _TRUE


def oidc_enabled() -> bool:
    """True when the OIDC resolver is both switched on and fully configured.

    ``SQLHANDLER_OIDC_ENABLED`` alone is not enough: without
    ``SQLHANDLER_OIDC_ISSUER`` there is nothing to verify tokens against, so
    the resolver stays inert (fail-closed) and :func:`warn_if_misconfigured`
    screams once at startup. Read per request — enabling OIDC needs no restart.
    """
    return _flag_set() and bool(_env_str(ISSUER_ENV))


def warn_if_misconfigured(server_label: str) -> bool:
    """Loud one-shot startup warning: enabled but unusable.

    Returns True when ``SQLHANDLER_OIDC_ENABLED`` is set without a usable
    issuer — the state in which the resolver is inert and every JWT 401s
    (fail-closed). Call from the server's HTTP-mode startup only (stdio dev
    use never verifies tokens).
    """
    if not _flag_set() or _env_str(ISSUER_ENV):
        return False
    line = "=" * 72
    print(line)
    print(f"WARNING: {ENABLED_ENV} is set but {ISSUER_ENV} is empty — {server_label} will NOT")
    print("accept OIDC JWTs (fail-closed). Set the issuer (e.g. a Keycloak realm URL) to")
    print(f"activate JWT sign-in, or clear {ENABLED_ENV} to silence this warning.")
    print(line)
    return True


def oidc_status() -> dict:
    """Small introspection helper — no secrets. ``enabled`` reports the
    EFFECTIVE state (flag AND issuer)."""
    return {
        "enabled": oidc_enabled(),
        "issuer": _env_str(ISSUER_ENV) or None,
        "audience": _env_str(AUDIENCE_ENV) or DEFAULT_AUDIENCE,
        "identity_claim": _env_str(IDENTITY_CLAIM_ENV) or DEFAULT_IDENTITY_CLAIM,
    }


# ---------------------------------------------------------------------------
# Token shape + verification
# ---------------------------------------------------------------------------


def is_jwt_format(token: str) -> bool:
    """Cheap pre-check: three dot-separated base64url segments (a JWT's shape).

    Opaque static keys are (with overwhelming probability) not JWT-shaped, so
    the identity ladder routes only plausible JWTs into verification — an
    ordinary key never costs a parse and is never mistaken for a token.
    Deliberately decodes and verifies NOTHING.
    """
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    return all(part and _JWT_SEGMENT_RE.match(part) for part in parts)


def _b64url_bytes(segment: str) -> bytes:
    if not _JWT_SEGMENT_RE.match(segment):
        raise ValueError("segment is not base64url")
    return base64.urlsafe_b64decode((segment + "=" * (-len(segment) % 4)).encode("ascii"))


def _b64url_uint(segment: str) -> int:
    return int.from_bytes(_b64url_bytes(segment), "big")


def _b64url_json(segment: str) -> dict | None:
    try:
        doc = json.loads(_b64url_bytes(segment).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _fail(reason: str, detail: str | None = None) -> None:
    """Fail closed with a short reason code (never any token material).
    DEBUG level: a per-request rung decline must not spam the log."""
    logger.debug("OIDC JWT rejected: %s%s", reason, f" ({detail})" if detail else "")


def _jwks_url() -> str:
    explicit = _env_str(JWKS_URL_ENV)
    if explicit:
        return explicit
    return f"{_env_str(ISSUER_ENV).rstrip('/')}/protocol/openid-connect/certs"


# JWKS cache (per JWKS URL): stamp -> keys, plus a negative cache so a down
# IdP cannot be hammered per request. The LOCK protects the cache dicts only —
# the network fetch runs OUTSIDE it, so a slow/hostile IdP can never serialize
# every request behind one in-flight fetch. ``_jwks_fetching`` records the URLs
# with a fetch in flight so concurrent callers do not stampede (single-process
# semantics, like the caches themselves); ``_forced_refetch_at`` rate-limits
# the unknown-kid force path per URL.
_jwks_lock = threading.Lock()
_jwks_cache: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}
_jwks_negative: dict[str, float] = {}
_jwks_fetching: set[str] = set()
_forced_refetch_at: dict[str, float] = {}


def _fetch_jwks(url: str, timeout: float) -> dict[str, tuple[int, int]]:
    """Fetch + parse the JWKS into ``{kid: (n, e)}`` (raises on failure).

    Cert verification stays ON; when ``REMOTE_CA_BUNDLE`` points at a
    platform CA bundle (the PCAI private-CA case), it is trusted via a
    dedicated SSL context — the D22 hotfix pattern from RAG's oidc_sso — so
    the EXTERNAL https issuer URL (e.g. the ezaf-gateway front) becomes
    usable from the pod, not just the in-cluster plain-HTTP endpoint.
    """
    import ssl
    import urllib.parse

    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sqlhandler"})
    handlers: list = []
    ca_bundle = os.environ.get("REMOTE_CA_BUNDLE", "").strip()
    if url.lower().startswith("https://") and ca_bundle and os.path.exists(ca_bundle):
        try:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_bundle)))
        except Exception:
            pass  # unusable bundle -> default verification (fail closed as usual)
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith((".svc", ".svc.cluster.local", ".local")) or host == "localhost":
        # in-cluster: never route through an ambient proxy
        handlers.insert(0, urllib.request.ProxyHandler({}))
    if handlers:
        # build_opener takes handlers POSITIONALLY (*handlers) — the handlers=
        # kwarg raised TypeError on every fetch, killing the OIDC rung when
        # REMOTE_CA_BUNDLE is set (the same live G2 bug RAG hit 2026-10-02).
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read(_JWKS_MAX_BYTES).decode("utf-8"))
    else:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read(_JWKS_MAX_BYTES).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise TypeError("JWKS payload is not a {keys: [...]} document")
    keys: dict[str, tuple[int, int]] = {}
    for jwk in payload["keys"]:
        if not isinstance(jwk, dict):
            continue
        kid = jwk.get("kid")
        pair = _rsa_pair_from_jwk(jwk)
        if isinstance(kid, str) and kid and pair is not None:
            keys[kid] = pair
    if not keys:
        raise ValueError("JWKS carries no usable RSA signing keys")
    return keys


def _rsa_pair_from_jwk(jwk: dict) -> tuple[int, int] | None:
    """``(n, e)`` for one RSA/signature JWK, else None (non-RSA skipped)."""
    if jwk.get("kty") != "RSA" or jwk.get("use") not in (None, "sig"):
        return None
    try:
        return _b64url_uint(str(jwk["n"])), _b64url_uint(str(jwk["e"]))
    except (KeyError, ValueError):
        return None


def _jwks_keys(url: str, *, force: bool = False) -> dict[str, tuple[int, int]] | None:
    """``{kid: (n, e)}`` from the TTL-cached JWKS (fetch when stale/forced).

    ``force=True`` is the unknown-``kid`` rotation path: ONE immediate
    refetch — RATE LIMITED to at most one per ``url`` per
    ``SQLHANDLER_JWKS_FORCE_MIN_INTERVAL`` seconds (default 60), because an
    UNAUTHENTICATED garbage token with a random ``kid`` reaches this path;
    inside the window the cached keys are returned as-is (or None when there
    are none) WITHOUT touching the network. A failed fetch (network/parse)
    arms the negative cache — forced or not, ``None`` comes back until the
    backoff elapses, so a down IdP sees at most one attempt per backoff
    window per replica. The fetch runs OUTSIDE ``_jwks_lock`` (a
    ``_jwks_fetching`` marker prevents concurrent stampedes), so a slow IdP
    cannot serialize unrelated requests behind the lock.
    """
    ttl = _env_float(JWKS_REFRESH_ENV, DEFAULT_JWKS_REFRESH)
    timeout = _env_float(FETCH_TIMEOUT_ENV, DEFAULT_FETCH_TIMEOUT)
    now = time.time()
    if force:
        min_interval = _env_float(FORCE_MIN_INTERVAL_ENV, DEFAULT_FORCE_MIN_INTERVAL)
        with _jwks_lock:
            last = _forced_refetch_at.get(url)
            if last is not None and (now - last) < min_interval:
                cached = _jwks_cache.get(url)
                return cached[1] if cached else None
            _forced_refetch_at[url] = now
    with _jwks_lock:
        cached = _jwks_cache.get(url)
        if not force and cached and (now - cached[0]) < ttl:
            return cached[1]
        negative_until = _jwks_negative.get(url)
        if negative_until and now < negative_until:
            return None
        if url in _jwks_fetching:
            # Another thread is already fetching: never block on it — serve
            # the stale copy (or None) rather than queue behind a timeout.
            return cached[1] if cached else None
        _jwks_fetching.add(url)
    try:
        try:
            keys = _fetch_jwks(url, timeout)  # network, OUTSIDE the lock
        except Exception as exc:  # network/parse failure — fail closed, back off
            with _jwks_lock:
                _jwks_negative[url] = time.time() + _NEGATIVE_TTL
            logger.warning(
                "OIDC JWKS fetch failed (%s: %s) — JWT verification is briefly unavailable",
                type(exc).__name__,
                exc,
            )
            return None
        with _jwks_lock:
            _jwks_negative.pop(url, None)
            _jwks_cache[url] = (time.time(), keys)
        return keys
    finally:
        # Always release the stampede marker (even on a BaseException), so a
        # single aborted fetch can never wedge the guard permanently.
        with _jwks_lock:
            _jwks_fetching.discard(url)


def _verify_rs256(signing_input: bytes, signature: bytes, key: tuple[int, int]) -> bool:
    """PKCS1v15/SHA256 verification against one JWKS RSA public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    n, e = key
    public_key = rsa.RSAPublicNumbers(e, n).public_key()
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def _audience_matches(claims: dict) -> bool:
    audience = _env_str(AUDIENCE_ENV) or DEFAULT_AUDIENCE
    azp = claims.get("azp")
    if azp is not None:
        return azp == audience
    aud = claims.get("aud")
    if isinstance(aud, str):
        return aud == audience
    if isinstance(aud, list):
        return audience in [a for a in aud if isinstance(a, str)]
    return False


def _claims_valid(claims: dict) -> bool:
    """iss/exp/nbf/azp-aud validation (fail-closed, reason-coded)."""
    if claims.get("iss") != _env_str(ISSUER_ENV):
        _fail("issuer")
        return False
    skew = _env_float(CLOCK_SKEW_ENV, DEFAULT_CLOCK_SKEW)
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or float(exp) <= now - skew:
        _fail("expired", "exp missing or in the past (clock-skew leeway applied)")
        return False
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or float(nbf) > now + skew):
        _fail("nbf", "nbf in the future (clock-skew leeway applied)")
        return False
    if not _audience_matches(claims):
        _fail("audience")
        return False
    return True


def verify_and_decode(token: str) -> dict | None:
    """Verify *token* and return its claims dict, or ``None``.

    Pipeline: header decode -> alg MUST be ``RS256`` (hardcoded; ``none`` /
    ``HS*`` rejected — alg-confusion guard) -> ``kid`` lookup in the cached
    JWKS (one forced refetch on an unknown kid) -> RS256 PKCS1v15/SHA256
    signature verification -> claim validation (``iss`` equality, ``exp``
    leeway, optional ``nbf``, ``azp``/``aud`` audience). ANY failure —
    malformed token, wrong alg, unknown key, bad signature, bad claims,
    disabled resolver, unreachable IdP — returns ``None`` (fail-closed) and
    logs a short reason code; token material is never logged or stored.
    """
    if not oidc_enabled() or not is_jwt_format(token):
        return _fail("disabled-or-format")
    header, payload_segment, signature_segment = token.split(".")
    header_doc = _b64url_json(header)
    claims = _b64url_json(payload_segment)
    if header_doc is None or claims is None:
        return _fail("format")
    alg = header_doc.get("alg")
    if alg != "RS256":
        return _fail("alg", f"rejected alg {alg!r} (RS256 only — alg-confusion guard)")
    kid = header_doc.get("kid")
    if not isinstance(kid, str) or not kid:
        return _fail("kid", "header carries no kid")
    try:
        signature = _b64url_bytes(signature_segment)
    except ValueError:
        return _fail("format")
    keys = _jwks_keys(_jwks_url())
    if not keys:
        return _fail("jwks", "no usable JWKS (fetch failed or negative-cached)")
    key = keys.get(kid)
    if key is None:
        # Unknown kid: ONE immediate refetch before failing (rotation pickup).
        key = (_jwks_keys(_jwks_url(), force=True) or {}).get(kid)
    if key is None:
        return _fail("kid")
    if not _verify_rs256(f"{header}.{payload_segment}".encode("ascii"), signature, key):
        return _fail("signature")
    if not _claims_valid(claims):
        return None
    return claims


def subject_from_claims(claims: dict) -> str | None:
    """The stable Caller.subject for verified *claims* — RAG's registry
    identity convention.

    The identity claim (``SQLHANDLER_OIDC_IDENTITY_CLAIM``, default
    ``preferred_username``) first, then ``sub`` as the ABSENT-claim
    fallback. The name must satisfy RAG's registry name rules
    (``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``) after whitespace stripping; a
    present-but-unsuitable primary claim REJECTS the token (returns None —
    the rung declines) rather than silently falling back: the fallback
    chain exists for absent claims, not for forged or malformed ones.
    """
    claim_name = _env_str(IDENTITY_CLAIM_ENV) or DEFAULT_IDENTITY_CLAIM
    primary = claims.get(claim_name)
    if isinstance(primary, str) and primary.strip():
        name = primary.strip()
        if not _name_ok(name):
            return _fail("identity", f"{claim_name} value does not satisfy the name rules") or None
        return name
    sub = claims.get(_FALLBACK_CLAIM)
    if isinstance(sub, str) and sub.strip():
        name = sub.strip()
        if not _name_ok(name):
            return _fail("identity", "sub value does not satisfy the name rules") or None
        return name
    return _fail("identity", f"neither {claim_name} nor {_FALLBACK_CLAIM} is a usable string") or None


def _name_ok(name: str) -> bool:
    try:
        return bool(_NAME_RE.match(str(name).strip()))
    except Exception:
        return False
