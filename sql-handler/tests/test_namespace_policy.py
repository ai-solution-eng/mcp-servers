"""Tests for mcp_fleet_common.namespace_policy normalization (audit P1-12).

The K8S-MCP behavior lost in the port: namespaces are normalized
(strip + lower) and patterns are matched case-insensitively, so "Prod" or
" Team-A " can never slip past a blocklist written in canonical form.
"""

import os

from sqlhandler.mcp_fleet_common.namespace_policy import NamespacePolicy


ALLOWED = "SQLHANDLER_TEST_NP_ALLOWED"
BLOCKED = "SQLHANDLER_TEST_NP_BLOCKED"


def _policy(monkeypatch, **env):
    # monkeypatch (not bare os.environ writes): every test gets automatic
    # restore, so a case/whitespace variant test can never leak an env value
    # into a later test (reviewer-provided ordering-fragility fix).
    monkeypatch.delenv(ALLOWED, raising=False)
    monkeypatch.delenv(BLOCKED, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return NamespacePolicy(allowed_env=ALLOWED, blocked_env=BLOCKED)


def test_blocklist_blocks_case_and_whitespace_variants(monkeypatch):
    p = _policy(monkeypatch, **{BLOCKED: "prod"})
    assert not p.check("Prod").allowed
    assert not p.check(" PROD ").allowed
    assert not p.check("prod").allowed  # canonical form still blocked


def test_allowlist_allows_case_variant(monkeypatch):
    p = _policy(monkeypatch, **{ALLOWED: "team-*"})
    d = p.check(" TEAM-A ")
    assert d.allowed, d.reason


def test_blocked_wins_over_allowed_even_with_case_games(monkeypatch):
    p = _policy(monkeypatch, **{ALLOWED: "*", BLOCKED: "kube-system"})
    assert not p.check("KUBE-SYSTEM").allowed
    assert p.check("default").allowed


def test_default_deny_still_holds_after_normalization(monkeypatch):
    p = _policy(monkeypatch)  # no allowlist configured
    assert not p.check(" anything ").allowed
