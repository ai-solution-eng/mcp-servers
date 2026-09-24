"""The fleet's shared blocked-wins namespace policy.

Wave-6 G1 (decision D16) extraction of the policy that three servers
implement separately today (verified 2026-09-13):

* **logsearch** ``_namespace_allowed(ns) -> bool`` — CSV env lists,
  ``fnmatch.fnmatchcase`` (platform-independent on purpose: namespace names
  are lowercase DNS labels), BLOCKED always wins, and since fleet decision
  D8 an EMPTY allowlist denies everything unless
  ``LOGSEARCH_EMPTY_ALLOWS_ALL`` is set to 1/true/yes/on (re-read per call).
* **applygate** ``_namespace_allowed(ns) -> (allowed, reason)`` — same
  blocked-wins core; EMPTY allowlist denies with no escape; refusals are
  self-describing (they name the env vars an operator must change).
* **K8S-MCP** ``namespace_violation(ns) -> None | str`` — the policy is
  ACTIVE only when at least one env var is set (unset/unconfigured server =
  everything allowed, its pre-D8 posture), patterns are validated against a
  lowercase-DNS-label-with-globs regex at parse time, and the namespace is
  normalized (strip + lower) before matching.

This module expresses all three as ONE class with the divergences as
explicit knobs, so a consumer migrates by binding its env names + posture:

    from mcp_fleet_common.namespace_policy import NamespacePolicy

    policy = NamespacePolicy(
        allowed_env="LOGSEARCH_ALLOWED_NAMESPACES",
        blocked_env="LOGSEARCH_BLOCKED_NAMESPACES",
        empty_allows_all_env="LOGSEARCH_EMPTY_ALLOWS_ALL",  # the D8 escape
        default_empty_allows=False,                          # D8 deny
    )
    decision = policy.check(ns)        # -> PolicyDecision(allowed, reason)

**Env re-read semantics preserved**: every ``check`` re-reads the
environment (the fleet convention — rotation/policy changes land without a
restart, and tests can monkeypatch env freely). Nothing is cached.

Reason strings model applygate's texts (the fleet's most complete), with the
env names interpolated; consumers that render their own refusal strings
(logsearch) keep doing so — their migration swaps the predicate, not the
error text.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass

__all__ = ["EMPTY_ALLOWS_ALL_VALUES", "NamespacePolicy", "PolicyDecision"]

#: The truthy spellings the D8 escape hatch accepts (logsearch's list).
EMPTY_ALLOWS_ALL_VALUES = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class PolicyDecision:
    """The outcome of one namespace-policy check.

    ``reason`` is empty exactly when ``allowed`` is True. ``__bool__`` is the
    decision itself, so ``if policy.check(ns):`` reads like the bool-style
    implementations; prefer explicit ``.allowed`` in new code.
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def _env_csv(name: str) -> list[str]:
    """Comma-separated env list, whitespace-tolerated, re-read per call."""
    return [p.strip() for p in (os.environ.get(name) or "").split(",") if p.strip()]


class NamespacePolicy:
    """Blocked-wins namespace policy over re-read-per-call env lists.

    Parameters (bind per app):
      * ``allowed_env`` / ``blocked_env`` — the env var names holding the
        comma-separated fnmatch glob lists.
      * ``empty_allows_all_env`` — optional escape-hatch env name: when the
        allowlist is empty, THIS var (truthy = 1/true/yes/on) restores the
        open behavior. The logsearch D8 shape. None = no escape.
      * ``default_empty_allows`` — what an EMPTY allowlist means when the
        escape is absent/unset: False = deny everything (logsearch D8 +
        applygate default-deny), True = allow everything (K8S-MCP's
        policy-inactive-when-unset posture).
    """

    def __init__(
        self,
        *,
        allowed_env: str,
        blocked_env: str,
        empty_allows_all_env: str | None = None,
        default_empty_allows: bool = False,
    ) -> None:
        self.allowed_env = allowed_env
        self.blocked_env = blocked_env
        self.empty_allows_all_env = empty_allows_all_env
        self.default_empty_allows = default_empty_allows

    # -- env plumbing (re-read per call — the fleet convention) -----------------

    def _empty_allows_all(self) -> bool:
        if self.empty_allows_all_env is None:
            return False
        raw = os.environ.get(self.empty_allows_all_env, "")
        return raw.strip().lower() in EMPTY_ALLOWS_ALL_VALUES

    # -- the predicate -----------------------------------------------------------

    def check(self, ns: str) -> PolicyDecision:
        """One namespace against the current env — blocked ALWAYS wins."""
        for pattern in _env_csv(self.blocked_env):
            if fnmatch.fnmatchcase(ns, pattern):
                return PolicyDecision(
                    False,
                    f"namespace {ns!r} matches {self.blocked_env} pattern {pattern!r} — the blocklist always wins",
                )
        allowed = _env_csv(self.allowed_env)
        if not allowed:
            if self._empty_allows_all() or self.default_empty_allows:
                return PolicyDecision(True)
            if self.empty_allows_all_env is not None:
                return PolicyDecision(
                    False,
                    f"DEFAULT-DENY: {self.allowed_env} is unset or empty and "
                    f"{self.empty_allows_all_env} is not set — no namespaces are enabled, "
                    "so every namespace is refused. Set "
                    f"{self.allowed_env} (comma-separated, globs like 'team-*' allowed) "
                    f"to make anything reachable, or set {self.empty_allows_all_env}=1 "
                    "to restore the pre-D8 open default.",
                )
            return PolicyDecision(
                False,
                f"DEFAULT-DENY: {self.allowed_env} is unset or empty — no namespaces are "
                "enabled, so every write is refused. Set "
                f"{self.allowed_env} (comma-separated, globs like 'team-*' allowed) "
                "to make anything writable.",
            )
        for pattern in allowed:
            if fnmatch.fnmatchcase(ns, pattern):
                return PolicyDecision(True)
        return PolicyDecision(
            False,
            f"namespace {ns!r} is not matched by {self.allowed_env} "
            f"(currently: {', '.join(allowed)!r}) — ask the operator to allowlist it explicitly",
        )

    def allowed(self, ns: str) -> bool:
        """Bool form — the logsearch predicate shape."""
        return self.check(ns).allowed

    def violation(self, ns: str) -> str | None:
        """Refusal-message form — the K8S-MCP shape (None = allowed)."""
        decision = self.check(ns)
        return None if decision.allowed else decision.reason


# ---------------------------------------------------------------------------
# K8S-MCP-style pattern validation (opt-in helper for its migration)
# ---------------------------------------------------------------------------

#: Lowercase DNS label, optionally with ``*``/``?`` globs (K8S-MCP's
#: ``_NS_PATTERN_RE``); namespace names themselves are DNS labels.
DNS_LABEL_GLOB_RE = re.compile(r"[a-z0-9*?][a-z0-9*?-]{0,61}")
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


def parse_ns_patterns(raw: str) -> tuple[str, ...]:
    """Parse + validate a comma-separated pattern list (K8S-MCP semantics).

    Lowercases and strips each comma-separated part, drops empties, and
    rejects anything that is not a lowercase DNS label with optional
    ``*``/``?`` globs — a misconfigured list raises ValueError with a
    self-describing message instead of silently matching nothing.
    """
    patterns = []
    for part in raw.split(","):
        pattern = part.strip().lower()
        if not pattern:
            continue
        if not DNS_LABEL_GLOB_RE.fullmatch(pattern):
            raise ValueError(
                f"invalid namespace pattern {pattern!r} in {raw!r}: use lowercase DNS labels with optional * / ? globs"
            )
        patterns.append(pattern)
    return tuple(patterns)
