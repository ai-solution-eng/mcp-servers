"""Tests for the applygate API-key middleware on /mcp (fleet pattern: K8S-MCP).

Scope matrix pinned here (fleet-audit CRITICAL): /mcp — the cluster's entire
governed write surface — requires a key; the read-only console (/, /ui,
/api/* — plan previews are ALWAYS dry-run, audit tail, policy view) and the
k8s probes stay public, exactly like K8S-MCP's inert console. Open-in-dev-
mode, both header forms, multi-key overlap rotation, and per-request env
re-read are all covered.

NOTE: never drive /mcp with a bare GET — the streamable-http transport treats
GET as an SSE stream request and blocks the TestClient. The positive-path
probe is a minimal JSON-RPC `ping` POST, which doubles as an end-to-end check
(gate AND transport both answer).

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    python -m pytest tests/test_auth.py -v
"""

import asyncio
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from starlette.testclient import TestClient

import mcp_auth
import server

PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}
MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}

# A minimal manifest the tool-level spoofing tests plan against.
SIMPLE = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\ndata:\n  k: v\n"


@pytest.fixture()
def app():
    return server._build_http_app()


def ping(c, headers):
    return c.post("/mcp", json=PING, headers={**MCP_ACCEPT, **headers})


def run(coro):
    return asyncio.run(coro)


def test_open_when_no_keys_configured(app, monkeypatch):
    """Dev mode: no APPLYGATE_API_KEYS → /mcp passes through unauthenticated."""
    monkeypatch.delenv(server.APPLYGATE_API_KEYS_ENV, raising=False)
    with TestClient(app) as c:
        assert ping(c, {}).status_code == 200


def test_health_and_console_public_even_with_keys(app, monkeypatch):
    """Probes and the read-only console stay public (no mutation endpoints)."""
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 200
        # Read-only console APIs stay reachable (plan is always dry-run).
        r = c.post("/api/plan", content=b"not json")
        assert r.status_code == 400  # reached the handler, not the gate


def test_mcp_missing_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        r = ping(c, {})
        assert r.status_code == 401
        assert "unauthorized" in r.json()["error"]
        assert r.headers["www-authenticate"] == "Bearer"


def test_mcp_wrong_key_is_401(app, monkeypatch):
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "nope"}).status_code == 401
        assert ping(c, {"Authorization": "Bearer nope"}).status_code == 401
        assert ping(c, {"Authorization": "Basic a2k="}).status_code == 401


def test_mcp_valid_key_via_both_header_forms(app, monkeypatch):
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1,k2")
    with TestClient(app) as c:
        r = ping(c, {"X-API-Key": "k1"})
        assert r.status_code == 200
        assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert ping(c, {"Authorization": "Bearer k2"}).status_code == 200


def test_multi_key_overlap_rotation(app, monkeypatch):
    """Old and new keys BOTH valid while listed together — zero-downtime
    rotation: append the new key, move clients over, drop the old one."""
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "old-key,new-key")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "old-key"}).status_code == 200
        assert ping(c, {"X-API-Key": "new-key"}).status_code == 200


def test_env_is_reread_per_request(app, monkeypatch):
    """Rotation without restart: the middleware re-reads the env every call."""
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k1")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "k1"}).status_code == 200
        monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "k2")
        assert ping(c, {"X-API-Key": "k1"}).status_code == 401
        assert ping(c, {"X-API-Key": "k2"}).status_code == 200


def test_universal_env_var_accepted(app, monkeypatch):
    """One-address wiring: MCP_API_KEYS alone authenticates fleet-wide."""
    monkeypatch.setenv("MCP_API_KEYS", "uni-key")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "uni-key"}).status_code == 200
        assert ping(c, {"X-API-Key": "applygate-only"}).status_code == 401


def test_universal_and_server_keys_are_unioned(app, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", "uni-key")
    monkeypatch.setenv(server.APPLYGATE_API_KEYS_ENV, "ag-key")
    with TestClient(app) as c:
        assert ping(c, {"X-API-Key": "uni-key"}).status_code == 200
        assert ping(c, {"X-API-Key": "ag-key"}).status_code == 200


# ---------------------------------------------------------------------------
# Spoofing enforcement (fleet Item A2 HARD INVARIANT — attribution-never-
# authorization). A forged X-MCP-Caller can change what the audit trail
# ATTRIBUTES (only when the direct peer is trusted — and a trusted peer is
# by definition the operator's own infrastructure), but it can never unlock
# anything: not namespaces, not kinds, not the D11 plan binding, not
# confirm_apply. These tests attack every gate with a spoofed identity and
# pin that the refusal paths are byte-for-byte unaffected.
# ---------------------------------------------------------------------------


def _spoofing_env(monkeypatch):
    """Operator config: namespace allowed, no registry, header IGNORED
    (the peer 127.0.0.1 is NOT in the trusted CIDRs)."""
    monkeypatch.setenv("APPLYGATE_API_KEYS", "k1")
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", "team-a")
    monkeypatch.delenv(server.CLIENTS_ENV, raising=False)
    monkeypatch.delenv("MCP_CALLER_TRUSTED_CIDRS", raising=False)


def test_spoofed_caller_header_cannot_unlock_namespaces(app, monkeypatch):
    """End-to-end over the real HTTP stack: a forged X-MCP-Caller naming an
    admin, a fingerprint, or a header-injection shape rides along on a
    VALID key's request — and the namespace gate refuses exactly as it does
    for an anonymous caller (attribution rides along, policy never moves)."""
    _spoofing_env(monkeypatch)
    forgeries = ["admin", "admin@gateway", "sha256:000000000000", "ops-bot", "ops-bot@gateway\r\nX-Injected: 1"]

    def call_plan(namespace, forged):
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "plan_apply", "arguments": {"namespace": namespace, "manifest": SIMPLE}},
        }
        r = c.post("/mcp", json=payload, headers={**MCP_ACCEPT, "X-API-Key": "k1", "X-MCP-Caller": forged})
        assert r.status_code == 200
        return json.loads(r.json()["result"]["content"][0]["text"])

    with TestClient(app) as c:
        for forged in forgeries:
            out = call_plan("team-b", forged)  # team-b is NOT allowlisted
            assert out["ok"] is False, forged
            assert "not matched by APPLYGATE_ALLOWED_NAMESPACES" in out["error"], forged


def test_spoofed_caller_cannot_widen_namespace_or_kind_policy(monkeypatch, tmp_path):
    """Direct tool-coroutine checks (the tool layer the MCP client reaches):
    with a maximally-forged identity in the caller slot, the namespace
    allowlist and the kind allowlist refuse EXACTLY as they do for an
    anonymous caller — same refusal, zero policy widening."""
    _spoofing_env(monkeypatch)
    server._caller_context.set(
        mcp_auth.Caller(
            key_fp="sha256:" + hashlib.sha256(b"k1").hexdigest()[:12],
            client="10.1.2.3:5000",
            name="admin",
            via="admin@gateway",
        )
    )
    # team-b: still refused, same self-describing reason as anonymous.
    out = json.loads(run(server.plan_apply(namespace="team-b", manifest=SIMPLE)))
    assert out["ok"] is False and "not matched by APPLYGATE_ALLOWED_NAMESPACES" in out["error"]
    # Secret: still hard-refused even with a spoofed "admin" identity.
    out = json.loads(
        run(
            server.plan_apply(
                namespace="team-a", manifest="apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\ndata: {}\n"
            )
        )
    )
    assert out["ok"] is False and "hard-refused" in out["documents"][0]["message"]
    server._caller_context.set(None)


def test_spoofed_caller_cannot_satisfy_the_d11_plan_binding(monkeypatch, tmp_path):
    """D11: apply without a recorded plan refuses in deny mode EVEN when the
    caller slot carries a maximally-forged identity; confirm_apply=True and
    a carried sha cannot substitute for the plan either."""
    _spoofing_env(monkeypatch)
    monkeypatch.delenv("APPLYGATE_UNPLANNED_APPLY", raising=False)  # default = deny
    server._caller_context.set(
        mcp_auth.Caller(
            key_fp="sha256:" + hashlib.sha256(b"k1").hexdigest()[:12],
            client="127.0.0.1:1",
            name="admin",
            via="admin@gateway",
        )
    )
    out = json.loads(run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True)))
    assert out["ok"] is False and "plan binding (D11)" in out["error"]

    # Even WITH a recorded plan (obtained as the legit key holder), the
    # forged identity changes nothing about the binding: tampered bytes refuse.
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    tampered = SIMPLE.replace("k: v", "k: FORGED")
    out = json.loads(run(server.apply_manifest(namespace="team-a", manifest=tampered, confirm_apply=True)))
    assert out["ok"] is False and "plan binding (D11)" in out["error"]
    server._caller_context.set(None)


def test_spoofed_via_via_untrusted_peer_is_ignored_in_capture(monkeypatch):
    """capture-level truth: a peer OUTSIDE the trusted CIDRs (or with no
    trusted CIDRs configured) gets via=None — the header is dropped, not
    sanitized-into-trust. Trust flips ONLY on the network position."""
    _spoofing_env(monkeypatch)
    headers = [(b"x-api-key", b"k1"), (b"x-mcp-caller", b"admin@gateway")]
    untrusted = mcp_auth.capture_caller(
        {"type": "http", "headers": headers, "client": ("127.0.0.1", 9999)}, server.AUTH_ENV_NAMES
    )
    assert untrusted.via is None  # fail-closed: no trusted CIDRs configured

    monkeypatch.setenv("MCP_CALLER_TRUSTED_CIDRS", "10.99.0.0/16")
    still_untrusted = mcp_auth.capture_caller(
        {"type": "http", "headers": headers, "client": ("127.0.0.1", 9999)}, server.AUTH_ENV_NAMES
    )
    assert still_untrusted.via is None  # CIDRs set, but the peer is not in them

    trusted = mcp_auth.capture_caller(
        {"type": "http", "headers": headers, "client": ("10.99.1.5", 9999)}, server.AUTH_ENV_NAMES
    )
    assert trusted.via == "admin@gateway"  # same header, trusted peer: honored


def test_spoofed_caller_registry_cannot_authenticate_or_widen(monkeypatch):
    """The APPLYGATE_CLIENTS registry only NAMES keys the auth middleware
    already matched: an unregistered/unmatched key stays anonymous, and a
    registry name never authenticates a request by itself."""
    _spoofing_env(monkeypatch)
    monkeypatch.setenv(server.CLIENTS_ENV, "ops-bot:k1")
    no_key = mcp_auth.capture_caller(
        {"type": "http", "headers": [(b"x-api-key", b"WRONG"), (b"x-mcp-caller", b"ops-bot")], "client": ("10.0.0.1", 1)},
        server.AUTH_ENV_NAMES,
        clients_env=server.CLIENTS_ENV,
    )
    assert no_key.key_fp is None and no_key.name is None
    right_key = mcp_auth.capture_caller(
        {"type": "http", "headers": [(b"x-api-key", b"k1")], "client": ("10.0.0.1", 1)},
        server.AUTH_ENV_NAMES,
        clients_env=server.CLIENTS_ENV,
    )
    assert right_key.name == "ops-bot" and right_key.key_fp is not None


def test_registry_env_is_reread_per_request_and_malformed_entries_are_skipped(monkeypatch):
    """Rotation + loud-skip: registry edits land without restart; malformed
    entries never authenticate anything — they are skipped (attribution
    degrades to fp-only, auth untouched)."""
    _spoofing_env(monkeypatch)
    scope = {"type": "http", "headers": [(b"x-api-key", b"k1")], "client": ("10.0.0.1", 1)}
    monkeypatch.setenv(server.CLIENTS_ENV, "old-name:k1")
    assert mcp_auth.capture_caller(scope, server.AUTH_ENV_NAMES, clients_env=server.CLIENTS_ENV).name == "old-name"
    monkeypatch.setenv(server.CLIENTS_ENV, "new-name:k1")
    assert mcp_auth.capture_caller(scope, server.AUTH_ENV_NAMES, clients_env=server.CLIENTS_ENV).name == "new-name"
    # Malformed shapes (2 fields are required: name:key — no silent 3rd field).
    for bad in ("just-a-name", "a:b:c", "a:b:c:d", ":", "a:", ":k1", "a:b;c:d"):
        monkeypatch.setenv(server.CLIENTS_ENV, bad)
        assert mcp_auth.capture_caller(scope, server.AUTH_ENV_NAMES, clients_env=server.CLIENTS_ENV).name is None


def test_via_sanitized_crlf_and_length_capped(monkeypatch):
    """Header-injection safety: CR/LF (and other controls) never reach the
    audit JSON; over-long claims are capped at 200 chars."""
    _spoofing_env(monkeypatch)
    monkeypatch.setenv("MCP_CALLER_TRUSTED_CIDRS", "10.0.0.0/8")
    evil = "bob@evil\r\nX-Injected: yes\x1b[31m"
    c = mcp_auth.capture_caller(
        {"type": "http", "headers": [(b"x-api-key", b"k1"), (b"x-mcp-caller", evil.encode("latin-1"))], "client": ("10.1.1.1", 2)},
        server.AUTH_ENV_NAMES,
    )
    assert c.via == "bob@evilX-Injected: yes\x1b[31m".replace("\x1b", "") or ("\r" not in c.via and "\n" not in c.via)
    assert len(c.via) <= 200
    # 300 'A's cap to 200.
    long = mcp_auth.capture_caller(
        {
            "type": "http",
            "headers": [(b"x-api-key", b"k1"), (b"x-mcp-caller", b"A" * 300)],
            "client": ("10.1.1.1", 2),
        },
        server.AUTH_ENV_NAMES,
    )
    assert long.via == "A" * 200
    # Whitespace-only sanitizes to None (no empty-string via in audit JSON).
    blank = mcp_auth.capture_caller(
        {"type": "http", "headers": [(b"x-api-key", b"k1"), (b"x-mcp-caller", b" \r\n ")], "client": ("10.1.1.1", 2)},
        server.AUTH_ENV_NAMES,
    )
    assert blank.via is None


def test_stale_plan_cannot_be_replayed_even_with_spoofed_caller(monkeypatch):
    """The carried-sha path is bound to THIS call's bytes: a forged identity
    plus a stale (valid-format) sha for different bytes still refuses."""
    _spoofing_env(monkeypatch)
    monkeypatch.delenv("APPLYGATE_UNPLANNED_APPLY", raising=False)  # deny
    run(server.plan_apply(namespace="team-a", manifest=SIMPLE))
    stale_sha = "0" * 64
    server._caller_context.set(mcp_auth.Caller(key_fp="sha256:deadbeefdead", client="1.2.3.4:1", name="admin", via="root"))
    out = json.loads(
        run(server.apply_manifest(namespace="team-a", manifest=SIMPLE, confirm_apply=True, plan_sha256=stale_sha))
    )
    assert out["ok"] is False and "plan binding (D11)" in out["error"]
    server._caller_context.set(None)
