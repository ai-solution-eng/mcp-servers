"""Per-caller identity spine (implementation review §3/§5 — Stage 1).

The SQL surfaces had NO caller identity: `_McpApiKeyMiddleware` authenticated
keys without recording which one matched, the audit lines were anonymous, and
metrics counted outcomes only. This module resolves ONE :class:`Caller` per
request and carries it through the engine — explicitly, because the engine's
query path runs on a raw ``threading.Thread`` (``QueryJob``) where contextvars
do NOT cross (the verified thread-boundary hazard).

Resolution ladder (the final OAuth posture, DECISIONS.md "OAuth REVISION"):

1. **Relay attribution headers** ``X-MCP-Caller-Subject`` (+ ``X-MCP-Caller-Class``)
   — accepted ONLY on a request that presented a valid /mcp key. Key validity
   alone proves a key matched — NOT that the gateway wrote the header — and the
   attributed subject is an AUTHORIZATION input (policy ``admins``, ``subjects``
   grants, owner scopes, write namespaces). So the header is trusted only with
   PROOF: ``SQLHANDLER_RELAY_HMAC_SECRET`` set plus a valid signature (the
   normal posture), or the explicit legacy opt-in
   ``SQLHANDLER_TRUST_RELAY_HEADERS=1`` (audit 2026-11-12 P1-5: an unsigned
   header is REFUSED by default, not trusted).
3. **OIDC bearer JWT** — an ``Authorization: Bearer`` value that (a) has the
   three-segment JWT shape and (b) does NOT match a configured static key
   is verified RS256-only against the operator's JWKS
   (``sqlhandler.oidc_identity``; envs SQLHANDLER_OIDC_*). A Bearer value
   that DOES match a configured static key is the key rung, never parsed
   as JWT (disjoint credential spaces, RAG D21/D19 convention). Verified
   token → a named ``user`` Caller (subject = the identity claim, default
   ``preferred_username``, fallback ``sub`` — RAG's registry convention);
   ANY verification failure declines SILENTLY (debug-level) and the ladder
   continues — the 401 happens downstream when the identity gate is on.
4. **oauth2-proxy browser headers** ``X-Auth-Request-User`` (+ ``X-Forwarded-Groups``)
   — honored only when ``SQLHANDLER_TRUST_BROWSER_HEADERS`` is set. TRUST-GATED:
   the flag asserts the deployment's workload AuthorizationPolicy pins ingress
   to the gateway (the chart ships the template); enabling it without that pin
   lets any client that can reach the pod claim any identity.
5. **Matched-key fingerprint** — ``_McpApiKeyMiddleware`` records the matched
   key's fingerprint into ``scope["state"]``; an unattributed key-valid request
   resolves to this pseudonymous rung (enforcement unchanged for legacy keys).
   The matched key may be EITHER a bootstrap static key (the env vars) OR a
   minted key from the admin keys store (``sqlhandler.admin_keys``,
   SQLHANDLER_ADMIN_KEYS_FILE) — the middleware authenticates env-first and
   falls back to the store (presentation-hash match), recording the SAME
   fingerprint vocabulary either way, so this rung is source-agnostic by
   construction (it reads the recorded fp, never the credential).
6. **Anonymous** — no key gate active (or stdio transport): the dev posture.

The class label vocabulary matches the gateway's subject kinds:
``user`` (relay-attributed / OIDC JWT) | ``browser`` (trusted headers)
| ``key`` (fingerprint) | ``anonymous``.

Everything here is cheap and re-read per request (the fleet convention) — no
caching of identity, no global state beyond the per-request ``scope["state"]``
slot.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .mcp_fleet_common.audit import CALLER_CONTEXT, key_fingerprint
from .mcp_fleet_common.audit import Caller as FleetCaller
from .oidc_identity import is_jwt_format as _is_jwt_format
from .oidc_identity import subject_from_claims as _oidc_subject_from_claims
from .oidc_identity import verify_and_decode as _oidc_verify_and_decode

logger = logging.getLogger("sqlhandler.identity")

__all__ = [
    "ANONYMOUS",
    "CALLER_CLASS_ANONYMOUS",
    "CALLER_CLASS_BROWSER",
    "CALLER_CLASS_KEY",
    "CALLER_CLASS_USER",
    "CALLER_CONTEXT",
    "HEADER_BROWSER_ACCESS_TOKEN",
    "HEADER_BROWSER_GROUPS",
    "HEADER_BROWSER_USER",
    "HEADER_CALLER_CLASS",
    "HEADER_CALLER_SIG",
    "HEADER_CALLER_SUBJECT",
    "HEADER_CALLER_TS",
    "RELAY_HMAC_SECRET_ENV",
    "RELAY_SIG_MAX_SKEW_SECONDS",
    "TRUST_BROWSER_HEADERS_ENV",
    "TRUST_RELAY_HEADERS_ENV",
    "Caller",
    "anonymous_caller",
    "browser_headers_trusted",
    "caller_from_request_state",
    "caller_from_scope",
    "class_for_subject_kind",
    "key_fp",
    "match_api_key",
    "relay_headers_trusted",
    "relay_hmac_secret",
    "relay_signature_headers",
    "resolve_api_token_caller",
    "resolve_bearer_jwt",
    "resolve_browser_caller",
    "resolve_explicit_key_caller",
    "resolve_forwarded_access_token",
    "resolve_relay_caller",
    "resolve_sso_cookie",
    "set_key_matcher",
    "set_key_subject_lookup",
    "set_static_keys_source",
]

# -- the resolution ladder's inputs -------------------------------------------

#: Gateway relay attribution (Phase B): the subject the gateway bound to the
#: presented key at issuance. Accepted only over a key-valid request.
HEADER_CALLER_SUBJECT = "X-MCP-Caller-Subject"
#: Optional companion label (``user`` | ``browser`` | ``token`` | ``key-bound``).
HEADER_CALLER_CLASS = "X-MCP-Caller-Class"
#: OPT-IN attribution proof: the gateway's HMAC-SHA256 (hex) over
#: ``"{ts}\n{subject}\n{cls}"``. Present only when the deployment configured
#: the shared secret below; ignored (never required) without it.
HEADER_CALLER_SIG = "X-MCP-Caller-Sig"
#: Unix-seconds timestamp covered by the signature (replay window, ±300 s).
HEADER_CALLER_TS = "X-MCP-Caller-Ts"

#: The relay-attribution HMAC secret (env, re-read per call). SET = the relay
#: headers are honored ONLY with a valid signature (the normal posture).
#: UNSET = attribution is NOT trusted unless the legacy opt-in below is on:
#: the subject feeds authorization (admins, subject grants, owner scopes), so
#: an unsigned header would let any key holder claim any identity.
RELAY_HMAC_SECRET_ENV = "SQLHANDLER_RELAY_HMAC_SECRET"  # secret-scan:allow (env var NAME, not a secret)
#: The LEGACY trust opt-in for SIGNATURE-LESS relay attribution. Unset/false =
#: an unsigned ``X-MCP-Caller-Subject`` is refused (fail closed, the default);
#: truthy = the pre-2026-11 behavior (any key-valid request may claim any
#: subject). Only for a deployment whose gateway cannot sign AND whose ingress
#: pins the attribution header.
TRUST_RELAY_HEADERS_ENV = "SQLHANDLER_TRUST_RELAY_HEADERS"
#: Max |now - ts| accepted for a relay signature (replay window).
RELAY_SIG_MAX_SKEW_SECONDS = 300

#: oauth2-proxy trusted headers (browser rung) — same convention the gateway's
#: identity module uses (X-Auth-Request-User, groups via X-Forwarded-Groups).
HEADER_BROWSER_USER = "X-Auth-Request-User"
HEADER_BROWSER_GROUPS = "X-Forwarded-Groups"
#: The oauth2-proxy FORWARDED ACCESS TOKEN envelope (D21, fleet mcp_auth.py
#: convention): when the edge proxy is configured with pass-access-token it
#: forwards the user's OIDC access token on every authenticated browser
#: request. This is an ENVELOPE, not a trust grant — whatever arrives in it
#: is fully verified (RS256, iss/aud/exp) by the JWT rung before it can
#: resolve any identity, so spoofing the header gains nothing. It outranks
#: the user-header rung because a verified JWT is a proof of possession
#: while X-Auth-Request-User is an injected assertion — and it carries the
#: ``preferred_username`` claim, the human-meaningful identity the header
#: rung lacks (the header carries the IdP's ``sub``, a UUID on this realm).
HEADER_BROWSER_ACCESS_TOKEN = "X-Auth-Request-Access-Token"  # secret-scan:allow (HTTP header name)

#: The app's OWN D22 SSO session cookie (the oidc_sso flow's session): the
#: VERIFIED access token planted by /oauth/oidc/callback. The LOWEST-priority
#: envelope — an explicit key or Bearer always outranks the browser session
#: behind it (D19's explicit-over-ambient), and the token inside is verified
#: like every other JWT-shaped credential before it resolves anything. The
#: cookie NAME is the oidc_sso module's (default ``pcai-sso``) — read from
#: there, never hardcoded here.
SSO_COOKIE_STATE = "sqlhandler.sso_cookie_token"

#: The trust gate for rung 2. Unset/untruthy = browser headers NEVER resolve an
#: identity (default; hardening posture). Truthy = the operator asserts the
#: workload AuthorizationPolicy pins ingress to the gateway — documented in the
#: chart values (``identity.trustBrowserHeaders``) and logged loudly at startup.
TRUST_BROWSER_HEADERS_ENV = "SQLHANDLER_TRUST_BROWSER_HEADERS"

# -- the class-label vocabulary (audit + metrics label values) -----------------

CALLER_CLASS_USER = "user"
CALLER_CLASS_BROWSER = "browser"
CALLER_CLASS_KEY = "key"
CALLER_CLASS_ANONYMOUS = "anonymous"

#: Gateway subject kinds (PCAI_LLM core.identity) → our class labels.
_KIND_CLASS_MAP = {
    "browser": CALLER_CLASS_BROWSER,
    "token": CALLER_CLASS_USER,
    "key-bound": CALLER_CLASS_USER,
    "key-anon": CALLER_CLASS_KEY,
    "user": CALLER_CLASS_USER,
}


def class_for_subject_kind(kind: str | None) -> str:
    """Map a relay ``X-MCP-Caller-Class`` value to a bounded class label.

    Unknown values collapse to ``user`` (the header only exists on a
    key-valid request, so the caller is at minimum an authenticated key).
    """
    if not kind:
        return CALLER_CLASS_USER
    return _KIND_CLASS_MAP.get(kind.strip().lower(), CALLER_CLASS_USER)


@dataclass(frozen=True)
class Caller:
    """The resolved identity of one request's caller.

    ``subject`` — the stable identity string when one was attributed
    (relay rung / browser rung). ``None`` = pseudonymous or anonymous.
    ``cls`` — one of the four bounded class labels (never the raw header
    value: audit/metrics labels must be low-cardinality).
    ``key_fp`` — the stable fingerprint of the MATCHED key (``sha256:<12hex>``),
    or None when no key was presented/needed. Never the raw key.
    ``groups`` — IdP groups from the browser rung (empty elsewhere).
    ``via`` — which ladder rung resolved: ``relay`` | ``jwt`` | ``browser`` |
    ``key`` | ``anonymous`` | ``stdio``.

    Frozen so a resolved identity cannot be mutated after the fact; hashable
    so tests/engines can key on it.
    """

    cls: str = CALLER_CLASS_ANONYMOUS
    subject: str | None = None
    key_fp: str | None = None
    groups: tuple[str, ...] = field(default_factory=tuple)
    via: str = "anonymous"

    def as_audit_dict(self) -> dict:
        """The additive ``caller`` audit field (review §5: class + subject +
        key_fp; never raw keys)."""
        return {"class": self.cls, "subject": self.subject, "key_fp": self.key_fp}

    def to_fleet_caller(self) -> FleetCaller:
        """The vendored mcp_fleet_common ``Caller`` (for HashChainedAuditLog
        providers and any shared-audit interop)."""
        return FleetCaller(key_fp=self.key_fp, client=None)

    @property
    def is_anonymous(self) -> bool:
        return self.cls == CALLER_CLASS_ANONYMOUS


#: Shared anonymous/dev identity (stdio transport, no-key HTTP, gates off).
ANONYMOUS = Caller(cls=CALLER_CLASS_ANONYMOUS, via="anonymous")


def anonymous_caller(via: str = "anonymous") -> Caller:
    """A fresh anonymous Caller with a specific ``via`` (stdio vs no-gate)."""
    return Caller(cls=CALLER_CLASS_ANONYMOUS, via=via)


def key_fp(raw_key: str) -> str:
    """The stable, NON-SECRET fingerprint of a matched key (fleet helper)."""
    return key_fingerprint(raw_key)


# -- middleware-side resolution ------------------------------------------------


def browser_headers_trusted(environ: dict[str, str] | None = None) -> bool:
    """True when the operator opted into the oauth2-proxy browser rung.

    Re-read per call (the fleet convention). The env asserts the deployment
    pins ingress with the workload AuthorizationPolicy — see the chart's
    ``identity.trustBrowserHeaders``.
    """
    env = os.environ if environ is None else environ
    return env.get(TRUST_BROWSER_HEADERS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def relay_headers_trusted(environ: dict[str, str] | None = None) -> bool:
    """True when the operator opted into SIGNATURE-LESS relay attribution.

    Re-read per call (the fleet convention). Unset/false = an unsigned
    ``X-MCP-Caller-Subject`` is refused (fail closed, audit 2026-11-12 P1-5);
    truthy = the legacy posture, which makes any key holder able to claim any
    subject the policy grants on (including an admin subject). The env asserts
    the deployment's gateway cannot sign and its ingress pins the header.
    """
    env = os.environ if environ is None else environ
    return env.get(TRUST_RELAY_HEADERS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _header(scope, name: str) -> str:
    """One header value from an ASGI scope (bytes-keyed), or ''."""
    lname = name.lower().encode("latin-1")
    for k, v in scope.get("headers", []) or []:
        lk = k.lower() if isinstance(k, bytes) else str(k).lower().encode("latin-1")
        if lk == lname:
            try:
                return v.decode("latin-1").strip() if isinstance(v, bytes) else str(v).strip()
            except Exception:
                return ""
    return ""


def _sanitize_subject(value: str, cap: int = 200) -> str:
    """Strip control characters + cap length (the gateway's sanitize rule)."""
    cleaned = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)
    return cleaned[:cap].strip()


def relay_hmac_secret(environ: dict[str, str] | None = None) -> str:
    """The configured relay-attribution HMAC secret ('' when unset/blank).

    Re-read per call (the fleet convention) so enabling verification — or
    rotating the secret — needs no restart.
    """
    env = os.environ if environ is None else environ
    return env.get(RELAY_HMAC_SECRET_ENV, "").strip()


def relay_signature_headers(scope) -> tuple[str, str] | None:
    """The relay attribution's ``(ts, sig)`` proof, or None when either is absent.

    Both headers are read from the same ASGI scope the subject/class headers
    come from; the returned values are the RAW (un-stripped-of-nothing) header
    values — the signature covers the already-sanitized subject and the mapped
    class label, which the caller computes identically on both sides.
    """
    ts = _header(scope, HEADER_CALLER_TS)
    sig = _header(scope, HEADER_CALLER_SIG)
    if not ts or not sig:
        return None
    return ts, sig


def _relay_sig_valid(secret: str, ts: str, subject: str, cls: str, sig: str) -> bool:
    """Constant-time check of one relay attribution signature (pure).

    Verifies ``HMAC-SHA256(secret, f"{ts}\\n{subject}\\n{cls}")`` (lowercase
    hex) with :func:`hmac.compare_digest`, and refuses a timestamp outside
    the ±:data:`RELAY_SIG_MAX_SKEW_SECONDS` replay window. An unset/blank
    *secret*, a non-numeric timestamp, or any malformed input returns False
    (fail closed) — this is the seam the unit tests drive directly, with no
    HTTP machinery.
    """
    if not secret or not ts or not sig:
        return False
    try:
        sent_at = float(ts)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(sent_at) or abs(time.time() - sent_at) > RELAY_SIG_MAX_SKEW_SECONDS:
        return False
    message = f"{ts}\n{subject}\n{cls}"
    expected = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, sig)
    except TypeError:
        # compare_digest refuses non-ASCII str — and header bytes decode
        # latin-1, so a binary-garbage signature header is reachable input.
        # Malformed input must FAIL CLOSED, not 500 the request.
        return False


#: One-shot warnings (the fleet convention: loud once, never per request).
_relay_unsigned_warned = False
_relay_optin_warned = False


def _warn_unsigned_relay_rejected() -> None:
    """One WARNING per process when an UNSIGNED relay attribution is refused."""
    global _relay_unsigned_warned
    if _relay_unsigned_warned:
        return
    _relay_unsigned_warned = True
    logger.warning(
        "refused an UNSIGNED relay attribution (%s) over a valid key: %s is unset, so the "
        "subject is not trusted (it is an authorization input for admin/policy grants). Set %s "
        "to verify the gateway's signature, or %s=1 for the legacy trust posture.",
        HEADER_CALLER_SUBJECT,
        RELAY_HMAC_SECRET_ENV,
        RELAY_HMAC_SECRET_ENV,
        TRUST_RELAY_HEADERS_ENV,
    )


def _warn_relay_optin() -> None:
    """One WARNING per process when the signature-less opt-in actually grants."""
    global _relay_optin_warned
    if _relay_optin_warned:
        return
    _relay_optin_warned = True
    logger.warning(
        "RELAY ATTRIBUTION IS UNSIGNED AND TRUSTED (%s=1, %s unset): any valid key holder can "
        "claim any caller subject, including one the policy designates as an admin. Set %s to "
        "verify the gateway's HMAC signature.",
        TRUST_RELAY_HEADERS_ENV,
        RELAY_HMAC_SECRET_ENV,
        RELAY_HMAC_SECRET_ENV,
    )


def resolve_relay_caller(scope, key_valid: bool) -> Caller | None:
    """Rung 1: the relay's attribution headers over a KEY-VALID request.

    Returns None when the request has no valid key (spoofing refusal — the
    header is then never trusted), carries no subject header, or cannot PROVE
    the attribution.

    KEY VALIDITY IS NECESSARY BUT NOT SUFFICIENT: any key holder could
    otherwise claim ANY subject (including an admin subject — ``admin_keys``
    checks ``subject in pol.admins``, and the subject also keys dataset
    grants, owner scopes and write namespaces). Two ways to prove it:

    * ``SQLHANDLER_RELAY_HMAC_SECRET`` set — the attribution must carry a
      valid ``X-MCP-Caller-Sig`` / ``X-MCP-Caller-Ts`` pair; a missing, stale
      or wrong signature resolves to None (the ladder falls through to the
      key-fingerprint rung). This is the normal posture.
    * the secret UNSET — the attribution is REFUSED (fail closed, audit
      2026-11-12 P1-5) unless the operator sets the explicit legacy opt-in
      ``SQLHANDLER_TRUST_RELAY_HEADERS=1``, which restores the pre-2026-11
      behavior (any key-valid request may claim any subject).
    """
    if not key_valid:
        return None
    subject = _sanitize_subject(_header(scope, HEADER_CALLER_SUBJECT))
    if not subject:
        return None
    cls = class_for_subject_kind(_header(scope, HEADER_CALLER_CLASS))
    secret = relay_hmac_secret()
    if secret:
        proof = relay_signature_headers(scope)
        if proof is None or not _relay_sig_valid(secret, proof[0], subject, cls, proof[1]):
            logger.debug("relay attribution rejected: missing or invalid signature")
            return None
        return Caller(cls=cls, subject=subject, key_fp=None, via="relay")
    # NO SECRET: an unsigned subject is an unproven AUTHORIZATION claim.
    if not relay_headers_trusted():
        logger.debug("relay attribution rejected: unsigned and %s is not set", TRUST_RELAY_HEADERS_ENV)
        _warn_unsigned_relay_rejected()
        return None
    _warn_relay_optin()
    return Caller(cls=cls, subject=subject, key_fp=None, via="relay")


def resolve_browser_caller(scope) -> Caller | None:
    """Rung 2: oauth2-proxy identity headers, ONLY under the trust flag.

    None unless ``SQLHANDLER_TRUST_BROWSER_HEADERS`` is truthy AND the request
    carries a user header (absence of the flag means the rung is closed —
    headers arriving anyway are ignored, never honored).
    """
    if not browser_headers_trusted():
        return None
    user = _sanitize_subject(_header(scope, HEADER_BROWSER_USER))
    if not user:
        return None
    raw_groups = _header(scope, HEADER_BROWSER_GROUPS)
    groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip()) if raw_groups else ()
    return Caller(cls=CALLER_CLASS_BROWSER, subject=user, key_fp=None, groups=groups, via="browser")


def match_api_key(provided: str, keys: list[str]) -> str | None:
    """Constant-time match of a presented credential against the configured
    keys; returns the MATCHED key (for fingerprinting) or None.

    Same comparison shape as ``_McpApiKeyMiddleware`` — every configured key
    compared constant-time; an empty presented value matches nothing.
    """
    if not provided or not keys:
        return None
    for candidate in keys:
        if candidate and hmac.compare_digest(provided.encode("utf-8"), candidate.encode("utf-8")):
            return candidate
    return None


def _presented_credentials(scope) -> list[str]:
    """Every credential presented on THIS request, explicit claims first (D19).

    Explicit X-API-Key / X-API-Token before the ambient Authorization Bearer,
    so a relay's server-level Bearer cannot shadow a per-request key riding the
    same request (the pcai-llm-gateway header shape).
    """
    candidates: list[str] = []
    for name in ("X-API-Key", "X-API-Token"):
        value = _header(scope, name)
        if value and value.strip():
            candidates.append(value.strip())
    raw = _header(scope, "Authorization")
    if raw:
        scheme, _, token = raw.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            candidates.append(token.strip())
    return candidates


def resolve_explicit_key_caller(scope) -> Caller | None:
    """Rung: an EXPLICITLY presented static key on a surface with no key gate.

    /mcp has _McpApiKeyMiddleware, which records the matched fingerprint (and
    a self-minted key's store-verified subject) in scope["state"]. /api/* has
    no such middleware, so before this rung a caller holding a VALID key
    resolved to ANONYMOUS on /api - and with the chart default
    SQLHANDLER_REQUIRE_IDENTITY=1 every /api read returned 401 while
    /api/whoami (which resolves keys on its own) reported "authenticated".
    The documented "paste an API key" recovery therefore did not exist
    (audit 2026-11-12 cross-validation, P1).

    Explicit-only (D19): the credential must be presented on THIS request as
    X-API-Key, X-API-Token or a Bearer that matches a configured key. When the
    /mcp middleware already resolved one (state slot present) this rung
    declines - that path is richer (it carries the store subject), but the
    subject is now looked up HERE too for a self-minted key, so a subject-
    bound key used over /api carries the same grants it has on /mcp.
    A matched key yields via="key" and its fingerprint, never the key itself.
    """
    if (scope.get("state") or {}).get("sqlhandler.key_fp"):
        return None  # the /mcp middleware already resolved (and enriched) it
    for provided in _presented_credentials(scope):
        matched = _match_configured_key(provided)
        if matched is not None:
            fp, subject = matched
            return Caller(cls=CALLER_CLASS_KEY, subject=subject, key_fp=fp, via="key")
    return None


def resolve_api_token_caller(scope) -> Caller | None:
    """Rung: the deployment-wide shared token (SQLHANDLER_API_TOKEN).

    That token is a CREDENTIAL, not merely a gate. With
    SQLHANDLER_REQUIRE_IDENTITY on, a caller who presents it must resolve to a
    non-anonymous identity or the two layers contradict each other: the token
    middleware (inner) accepts the request while the identity gate (outer)
    401s it - a deployment that configured SQLHANDLER_API_TOKEN would be
    locked out of /api entirely (found 2026-11-12 while answering "does
    requireIdentity mean an API key is always required?").

    A shared secret has no subject, so this resolves to the key class with the
    token's FINGERPRINT (never the token) and via="api-token"; policy resolves
    it through the default group exactly as it would an unbound key.
    """
    expected = [t.strip() for t in os.environ.get(API_TOKEN_ENV, "").split(",") if t.strip()]
    if not expected:
        return None
    for provided in _presented_credentials(scope):
        for candidate in expected:
            if hmac.compare_digest(provided.encode("utf-8"), candidate.encode("utf-8")):
                return Caller(cls=CALLER_CLASS_KEY, subject=None, key_fp=key_fp(candidate), via="api-token")
    return None


def resolve_bearer_jwt(scope) -> Caller | None:
    """The OIDC rung: a JWT-shaped Bearer that no static key owns.

    Reads the ``Authorization: Bearer <value>`` header. A value that matches
    a configured STATIC key (``match_api_key`` against
    ``_McpApiKeyMiddleware._keys()``) is the KEY rung's credential — this
    rung declines (returns None) so the key ladder resolves it; a static key
    is never parsed as a JWT (disjoint credential spaces — the RAG D21/D19
    convention). A three-segment base64url value that no static key claims
    is verified (RS256 + iss + aud/azp + exp/nbf, ``sqlhandler.oidc_identity``);
    a verified token resolves to Caller(cls=user, subject=<identity claim or
    sub>, via="jwt"). ANY verification failure declines SILENTLY (debug-level
    reason code, no log spam) — the ladder continues and the 401 (when the
    identity gate is on) happens downstream. An unparseable/absent Bearer,
    a disabled resolver, or a non-JWT-shaped Bearer also declines here.
    """
    raw = _header(scope, "Authorization")
    if not raw:
        return None
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    token = token.strip()
    if _match_configured_key(token) is not None:
        return None  # a configured key's credential — never parsed as JWT
    if not _is_jwt_format(token):
        return None
    try:
        claims = _oidc_verify_and_decode(token)
    except Exception:
        # Fail closed AND fail quiet: an unexpected verification error must
        # never 500 a request that would otherwise be anonymous.
        logger.debug("OIDC JWT verification raised; rung declines", exc_info=True)
        return None
    if claims is None:
        return None
    subject = _oidc_subject_from_claims(claims)
    if not subject:
        return None
    return Caller(cls=CALLER_CLASS_USER, subject=subject, key_fp=None, via="jwt")


def _static_keys() -> list[str]:
    """The configured static keys, read lazily to avoid an import cycle with
    server.py (the middleware module owns the env names)."""
    fn = _static_keys_fn
    if fn is None:
        return []
    try:
        return fn() or []
    except Exception:
        return []


#: Injected by server.py at import time (identity.set_static_keys_source(
#: _McpApiKeyMiddleware._keys)) - avoids importing server from identity.
_static_keys_fn: Callable[[], list[str]] | None = None

#: Injected by server.py: fingerprint -> the subject a SELF-MINTED key is bound
#: to (admin_keys keeps the mint-time-verified subject). Used so a subject-bound
#: key presented over /api carries the same grants it has over /mcp; a plain
#: admin-minted key has no subject and resolves as the key itself.
_key_subject_fn: Callable[[str], str | None] | None = None

#: The deployment-wide shared token (comma-separated list). Read here so the
#: identity ladder can RESOLVE it, not just the /api gate that validates it.
API_TOKEN_ENV = "SQLHANDLER_API_TOKEN"  # secret-scan:allow — env-var NAME, not a credential


def set_key_subject_lookup(fn: Callable[[str], str | None] | None) -> None:
    """Register the fingerprint -> subject lookup (server.py wires this)."""
    global _key_subject_fn
    _key_subject_fn = fn


#: Injected by server.py: raw presented credential -> (fingerprint, subject).
#: Covers BOTH credential stores the /mcp gate consults — the env key list and
#: the minted keys file (which stores digests, not raw keys) — so the identity
#: ladder authenticates exactly what the key gate accepts.
_key_matcher_fn: Callable[[str], tuple[str, str | None] | None] | None = None


def set_key_matcher(fn: Callable[[str], tuple[str, str | None] | None] | None) -> None:
    """Register the raw-key matcher (server.py wires the env+store union)."""
    global _key_matcher_fn
    _key_matcher_fn = fn


def _match_configured_key(raw: str) -> tuple[str, str | None] | None:
    """Match one presented credential to a configured key: (fp, subject) or None.

    Falls back to the env-only list when no matcher is injected (a unit test
    driving the ladder directly), which is also the shape the /mcp middleware
    records for env keys.
    """
    fn = _key_matcher_fn
    if fn is not None:
        try:
            matched = fn(raw)
        except Exception:  # a store hiccup must never crash the ladder
            matched = None
        if matched is not None:
            return matched
        return None
    matched_key = match_api_key(raw, _static_keys())
    if matched_key is None:
        return None
    fp = key_fp(matched_key)
    return (fp, _key_subject_for_fp(fp))


def _key_subject_for_fp(fp: str | None) -> str | None:
    """The store-verified subject bound to a self-minted key, if any."""
    fn = _key_subject_fn
    if fn is None or not fp:
        return None
    try:
        return fn(fp) or None
    except Exception:
        return None


def set_static_keys_source(fn: Callable[[], list[str]]) -> None:
    """Register the static-key source (the key middleware's ``_keys()``).

    Called once from server.py import; the callable itself re-reads the env
    PER CALL (the fleet convention), so key rotation needs no restart and
    no caching happens here.
    """
    global _static_keys_fn
    _static_keys_fn = fn


def resolve_forwarded_access_token(scope) -> Caller | None:
    """The D21 forwarded-token envelope: ``X-Auth-Request-Access-Token``.

    The oauth2-proxy edge (pass-access-token) forwards the browser user's
    OIDC access token on every authenticated request. The header is an
    ENVELOPE, not a trust grant: the value is verified through the SAME
    JWT path as an Authorization Bearer (RS256 + iss/aud/exp against the
    realm JWKS) before it can resolve anything — a forged header value is
    just an invalid token and declines silently. Verified success resolves
    exactly like the bearer rung: Caller(cls=user, subject=<identity claim
    — preferred_username on this realm>, via="jwt").

    Precedence: ABOVE the browser-header rung (a verified token is proof
    of possession; X-Auth-Request-User is an injected assertion carrying
    only the ``sub`` UUID), but BELOW an explicit Authorization Bearer —
    the caller's own presented credential always governs over the ambient
    forwarded one (D19's explicit-over-ambient convention).
    """
    raw = _header(scope, HEADER_BROWSER_ACCESS_TOKEN)
    if not raw:
        return None
    token = raw[7:].strip() if raw[:7].lower() == "bearer " else raw.strip()
    if not token:
        return None
    # A static key arriving in this envelope stays the KEY rung's
    # credential — never parsed as a JWT (disjoint credential spaces).
    if match_api_key(token, _static_keys()) is not None:
        return None
    if not _is_jwt_format(token):
        return None
    try:
        claims = _oidc_verify_and_decode(token)
    except Exception:
        logger.debug("forwarded access token verification raised; rung declines", exc_info=True)
        return None
    if claims is None:
        return None
    subject = _oidc_subject_from_claims(claims)
    if not subject:
        return None
    return Caller(cls=CALLER_CLASS_USER, subject=subject, key_fp=None, via="jwt")


def resolve_sso_cookie(scope) -> Caller | None:
    """The D22 SSO session-cookie envelope (the oidc_sso flow's session).

    Reads the cookie the /oauth/oidc/callback planted (an HttpOnly session
    carrying the VERIFIED access token) and verifies it through the SAME
    JWT path as every other token-shaped credential — the cookie is an
    envelope, not a trust grant (a forged/tampered/expired cookie is just
    an invalid token and declines silently). Verified success resolves
    exactly like the bearer rung: Caller(cls=user, subject=<
    preferred_username>, via="jwt").

    Precedence: the LOWEST envelope — above nothing. An explicit
    Authorization Bearer, a forwarded access token, and (on trust-flagged
    deployments) the injected user headers all outrank the browser session:
    the session is what you fall back to, never what overrides you. The
    cookie name comes from the oidc_sso module (env-configurable), so a
    deployment that renames the cookie stays consistent at both ends.
    """
    from . import oidc_sso

    name = oidc_sso.cookie_name()
    raw = ""
    for k, v in scope.get("headers", []) or []:
        lk = k.lower() if isinstance(k, bytes) else str(k).lower().encode("latin-1")
        if lk == b"cookie":
            try:
                raw = v.decode("latin-1") if isinstance(v, bytes) else str(v)
            except Exception:
                raw = ""
            break
    if not raw:
        return None
    token = ""
    for part in raw.split(";"):
        pname, _, pvalue = part.strip().partition("=")
        if pname.strip() == name:
            token = pvalue.strip()
            break
    if not token:
        return None
    if match_api_key(token, _static_keys()) is not None:
        return None
    if not _is_jwt_format(token):
        return None
    try:
        claims = _oidc_verify_and_decode(token)
    except Exception:
        logger.debug("SSO cookie verification raised; rung declines", exc_info=True)
        return None
    if claims is None:
        return None
    subject = _oidc_subject_from_claims(claims)
    if not subject:
        return None
    return Caller(cls=CALLER_CLASS_USER, subject=subject, key_fp=None, via="jwt")


def caller_from_scope(scope) -> Caller:
    """Resolve one request's identity from the ASGI scope (the middleware's
    entry point). Rungs: relay-attribution → OIDC bearer-JWT →
    forwarded-access-token envelope → SSO session cookie → browser-headers
    → key-fp.

    The key fingerprint comes from ``scope["state"]`` where
    ``_McpApiKeyMiddleware`` recorded it when a key matched. When no key gate
    is active (no keys configured) the static-key rung has nothing to
    anchor, but the OIDC rung stays LIVE (a Bearer JWT is verified on its
    own merit, independent of any configured static key); without a verified
    JWT the relay/browser headers are NOT trusted either (a keyless request
    cannot prove anything they say).
    """
    if scope.get("type") != "http":
        return anonymous_caller(via="stdio")
    state = scope.get("state") or {}
    key_fpr = state.get("sqlhandler.key_fp")
    key_valid = bool(key_fpr)
    relay = resolve_relay_caller(scope, key_valid)
    if relay is not None:
        return Caller(cls=relay.cls, subject=relay.subject, key_fp=key_fpr, via="relay")
    # D19 explicit-over-ambient: a key presented on THIS request outranks the
    # ambient rungs below (SSO cookie, forwarded token, browser headers) and
    # is the only credential the /api surface can carry without a gateway.
    explicit_key = resolve_explicit_key_caller(scope)
    if explicit_key is not None:
        return explicit_key
    # The shared deployment token is a credential too (a token-gated /api must
    # not 401 under requireIdentity just because the credential is not a key).
    shared_auth_caller = resolve_api_token_caller(scope)
    if shared_auth_caller is not None:
        return shared_auth_caller
    jwt_caller = resolve_bearer_jwt(scope)
    if jwt_caller is not None:
        return jwt_caller
    forwarded = resolve_forwarded_access_token(scope)
    if forwarded is not None:
        return forwarded
    sso = resolve_sso_cookie(scope)
    if sso is not None:
        return sso
    browser = resolve_browser_caller(scope)
    if browser is not None:
        return Caller(
            cls=CALLER_CLASS_BROWSER,
            subject=browser.subject,
            key_fp=key_fpr,
            groups=browser.groups,
            via="browser",
        )
    if key_valid:
        # A SUBJECT-BOUND key (self-minted by an SSO user; the middleware
        # records the subject the store verified at mint time) resolves to
        # a subject-carrying caller: policy grants BY NAME apply to the
        # human. The class stays "key" (audit/metrics vocabulary) but the
        # subject rides — an authenticated attribution, never spoofable,
        # because the subject came from the STORE (mint-time-verified),
        # not from any request header.
        bound_subject = str(state.get("sqlhandler.key_subject") or "").strip()
        if bound_subject:
            return Caller(cls=CALLER_CLASS_KEY, subject=bound_subject, key_fp=key_fpr, via="key")
        return Caller(cls=CALLER_CLASS_KEY, subject=None, key_fp=key_fpr, via="key")
    return anonymous_caller(via="anonymous")


def caller_from_request_state(request) -> Caller:
    """Resolve a Caller from a Starlette ``Request`` (the tool/webui path).

    Reads the same ``scope["state"]`` slot the middlewares populate; a stdio
    or test request without the slot resolves to anonymous (never raises).
    """
    try:
        scope = request.scope
    except AttributeError:
        return ANONYMOUS
    try:
        return caller_from_scope(scope)
    except Exception:
        logger.debug("caller resolution failed; treating as anonymous", exc_info=True)
        return ANONYMOUS
