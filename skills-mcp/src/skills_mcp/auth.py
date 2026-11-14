"""Who is calling — the shared operator key, a minted per-person key, or SSO.

Two credentials, one rule: a NAMED identity always beats the anonymous base.

Resolution order:
  1. SKILLS_MCP_API_KEYS / MCP_API_KEYS   -> operator key   (env-base, NO name)
  2. sha256(key) in /data/state/<identity>.json -> minted key (named)
  3. no key, trusted SSO header from the enforcing proxy -> sso identity

There is deliberately no env->name registry: a name comes only from a minted
key or from SSO, so every named identity is self-service and revocable by its
owner. The base key stays nameless, which is what makes it a safe default
credential for the gateway.

Why a custom middleware instead of mcp_auth.ApiKeyAuthMiddleware: that one only
knows env keys, and a minted key is created at runtime. We still use mcp_auth's
header parsing (presented_keys), Caller and CALLER_CONTEXT — only the "is this
key valid, and whose is it" step is ours.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import NamedTuple

import mcp_auth
from starlette.responses import JSONResponse

from . import prefs

TRUST_PROXY_ENV = "SKILLS_TRUST_PROXY_IDENTITY"
# Ordered: the HUMAN name first, the opaque ids last. The edge injects all of
# them; x-auth-request-user carries the Keycloak UUID (claim "user"), which is
# useless as a display name and would split one person into two identities.
_PROXY_IDENTITY_HEADERS = (
    b"x-auth-request-preferred-username",
    b"username",
    b"x-forwarded-user",
    b"x-auth-request-user",
)
API_KEYS_ENV = "SKILLS_MCP_API_KEYS"
ENV_KEY_NAMES = (mcp_auth.UNIVERSAL_API_KEYS_ENV, API_KEYS_ENV)

#: Paths that require a valid credential. Everything else (probes, the browser
#: UI) is public by design.
DEFAULT_PROTECTED = ("/mcp", "/admin", "/api")


class Identity(NamedTuple):
    name: str | None      # None for the shared operator/base key
    source: str           # "env-base" | "minted" | "sso"
    key_fp: str           # sha256:<12 hex> of the presented key


def _fingerprint(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:12]


_minted_cache: tuple[float, dict[str, str]] | None = None


def minted_index() -> dict[str, str]:
    """{sha256 hex: identity} for every minted token on disk (mtime-cached)."""
    global _minted_cache
    directory = prefs.state_dir()
    try:
        stamp = max((p.stat().st_mtime for p in directory.glob("*.json")), default=0.0)
    except OSError:
        stamp = 0.0
    if _minted_cache is not None and _minted_cache[0] == stamp:
        return _minted_cache[1]
    index: dict[str, str] = {}
    for identity, doc in prefs.iter_identities():
        for record in doc.get("keys") or []:
            if not isinstance(record, dict) or not prefs.is_active(record):
                continue          # revoked or expired keys never resolve
            digest = record.get("sha256")
            if isinstance(digest, str) and digest:
                index[digest] = identity
    _minted_cache = (stamp, index)
    return index


def proxy_identity(scope) -> str | None:
    """The SSO user stamped by an enforcing proxy — ONLY when explicitly
    trusted (SKILLS_TRUST_PROXY_IDENTITY=true) AND no key was presented.

    Trusting this header is a deployment decision: it is only safe when the pod
    cannot be reached except through that proxy (NetworkPolicy), which is why it
    is off by default.
    """
    if os.getenv(TRUST_PROXY_ENV, "").strip().lower() not in ("1", "true", "yes"):
        return None
    headers = dict(scope.get("headers") or [])
    for name in _PROXY_IDENTITY_HEADERS:
        value = headers.get(name)
        if value:
            text = value.decode("latin-1").strip()
            if text:
                return text
    return None


def resolve(scope) -> Identity | None:
    """Resolve the caller: a matching KEY first (stronger), then a trusted SSO
    header.

    The fallback matters: an enforcing proxy injects
    ``Authorization: Bearer <id_token>``` upstream, which LOOKS like a key.
    Without the fallback, that unmatched pseudo-key would short-circuit and every
    browser session would resolve to anonymous.
    """
    keys = mcp_auth.presented_keys(scope)
    env_keys = set(mcp_auth.configured_keys(ENV_KEY_NAMES))
    for key in keys:
        if key in env_keys:
            return Identity(None, "env-base", _fingerprint(key))
    index = minted_index()
    if index:
        for key in keys:
            digest = hashlib.sha256(key.encode()).hexdigest()
            for stored, identity in index.items():
                if hmac.compare_digest(digest, stored):
                    return Identity(identity, "minted", _fingerprint(key))
    # Nothing matched. A trusted proxy may still have told us who this is.
    sso = proxy_identity(scope)
    if sso:
        return Identity(sso.lower(), "sso", "sso:" + sso.lower()[:12])
    return None


def identity_name(scope) -> str | None:
    found = resolve(scope)
    return found.name if found else None


def mint(identity: str, *, label: str = "", expires_at: str | None = None) -> str:
    """Mint a NEW key for this identity. Returns the plaintext ONCE.

    Multiple keys per identity are allowed: one per agent, independently
    revocable and expirable.
    """
    key = "sk_live_" + secrets.token_urlsafe(32)
    prefs.add_key(identity, key, label=label, expires_at=expires_at)
    return key


class AuthMiddleware:
    """Authenticate, then publish the caller into mcp_auth.CALLER_CONTEXT.

    One wrapper does both jobs (the old CallerCapture + ApiKeyAuthMiddleware
    pair) because they now share the same resolver.
    """

    def __init__(self, app, protected=DEFAULT_PROTECTED):
        self.app = app
        self._protected = tuple(protected)

    def _needs_auth(self, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self._protected)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        identity = resolve(scope)
        client = scope.get("client")
        caller = mcp_auth.Caller(
            key_fp=identity.key_fp if identity else None,
            client=(f"{client[0]}:{client[1]}" if client else None),
            name=identity.name if identity else None,
        )
        token = mcp_auth.CALLER_CONTEXT.set(caller)
        try:
            if identity is None and self._needs_auth(scope.get("path", "")):
                response = JSONResponse(
                    {"error": "unauthorized: missing or invalid API key"}, status_code=401
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            mcp_auth.CALLER_CONTEXT.reset(token)
