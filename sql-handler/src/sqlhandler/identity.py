"""Per-caller identity spine (implementation review §3/§5 — Stage 1).

The SQL surfaces had NO caller identity: `_McpApiKeyMiddleware` authenticated
keys without recording which one matched, the audit lines were anonymous, and
metrics counted outcomes only. This module resolves ONE :class:`Caller` per
request and carries it through the engine — explicitly, because the engine's
query path runs on a raw ``threading.Thread`` (``QueryJob``) where contextvars
do NOT cross (the verified thread-boundary hazard).

Resolution ladder (the final OAuth posture, DECISIONS.md "OAuth REVISION"):

1. **Relay attribution headers** ``X-MCP-Caller-Subject`` (+ ``X-MCP-Caller-Class``)
   — accepted ONLY on a request that presented a valid /mcp key. Attribution-
   never-authorization: the gateway stamps the header on already-authorized
   calls, so a leaked header without a key resolves to the key-fingerprint rung
   at worst.
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

import hmac
import logging
import os
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
    "HEADER_BROWSER_GROUPS",
    "HEADER_BROWSER_USER",
    "HEADER_CALLER_CLASS",
    "HEADER_CALLER_SUBJECT",
    "TRUST_BROWSER_HEADERS_ENV",
    "Caller",
    "anonymous_caller",
    "browser_headers_trusted",
    "caller_from_request_state",
    "caller_from_scope",
    "class_for_subject_kind",
    "key_fp",
    "match_api_key",
    "resolve_bearer_jwt",
    "resolve_browser_caller",
    "resolve_relay_caller",
    "set_static_keys_source",
]

# -- the resolution ladder's inputs -------------------------------------------

#: Gateway relay attribution (Phase B): the subject the gateway bound to the
#: presented key at issuance. Accepted only over a key-valid request.
HEADER_CALLER_SUBJECT = "X-MCP-Caller-Subject"
#: Optional companion label (``user`` | ``browser`` | ``token`` | ``key-bound``).
HEADER_CALLER_CLASS = "X-MCP-Caller-Class"

#: oauth2-proxy trusted headers (browser rung) — same convention the gateway's
#: identity module uses (X-Auth-Request-User, groups via X-Forwarded-Groups).
HEADER_BROWSER_USER = "X-Auth-Request-User"
HEADER_BROWSER_GROUPS = "X-Forwarded-Groups"

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


def resolve_relay_caller(scope, key_valid: bool) -> Caller | None:
    """Rung 1: the relay's attribution headers over a KEY-VALID request.

    Returns None when the request has no valid key (spoofing refusal — the
    header is then never trusted) or carries no subject header.
    """
    if not key_valid:
        return None
    subject = _sanitize_subject(_header(scope, HEADER_CALLER_SUBJECT))
    if not subject:
        return None
    cls = class_for_subject_kind(_header(scope, HEADER_CALLER_CLASS))
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
    keys = _static_keys()
    if match_api_key(token, keys) is not None:
        return None  # a static key's credential — never parsed as JWT
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
    global _static_keys_fn
    fn = _static_keys_fn
    if fn is None:
        return []
    try:
        return fn() or []
    except Exception:
        return []


#: Injected by server.py at import time (``identity.set_static_keys_source(
#: _McpApiKeyMiddleware._keys)``) — avoids importing server from identity.
_static_keys_fn: "callable | None" = None


def set_static_keys_source(fn) -> None:
    """Register the static-key source (the key middleware's ``_keys()``).

    Called once from server.py import; the callable itself re-reads the env
    PER CALL (the fleet convention), so key rotation needs no restart and
    no caching happens here.
    """
    global _static_keys_fn
    _static_keys_fn = fn


def caller_from_scope(scope) -> Caller:
    """Resolve one request's identity from the ASGI scope (the middleware's
    entry point). Rungs: relay-attribution → OIDC bearer-JWT →
    browser-headers → key-fp.

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
    jwt_caller = resolve_bearer_jwt(scope)
    if jwt_caller is not None:
        return jwt_caller
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
