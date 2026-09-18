"""Hash-chained audit trail tests — the additive hardening fields.

Every audit entry now carries:
  prev_sha256 — sha256 of the PREVIOUS line's JSON text (no trailing
                newline); the first entry of an empty trail uses the
                64-zero genesis constant;
  caller      — non-secret caller identity ({"key_fp", "client"}) resolved
                at the auth layer (fleet pattern: K8S-MCP's _Caller).

The chain makes the trail tamper-EVIDENT: any modification, truncation, or
reordering of a line breaks every later link. Readers of the old 7-key
format stay compatible (new fields only). The verification procedure is
documented in the README and executed by server.verify_audit_chain() — both
are pinned here.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_audit_chain.py -v
"""

import asyncio
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import server

ENV_VARS = (
    "APPLYGATE_ALLOWED_NAMESPACES",
    "APPLYGATE_AUDIT_FILE",
    "APPLYGATE_UNPLANNED_APPLY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    server._caller_context.set(None)  # no caller leakage between tests
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "allow")  # gate isolation
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", "team-a")


def run(coro):
    return asyncio.run(coro)


def audit_path(tmp_path):
    return str(tmp_path / "audit.jsonl")


def raw_lines(tmp_path):
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def audit_entries(tmp_path):
    return [json.loads(ln) for ln in raw_lines(tmp_path)]


SIMPLE = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\ndata:\n  k: v\n"


# ---------------------------------------------------------------------------
# the chain itself
# ---------------------------------------------------------------------------


def test_entries_chain_from_genesis(tmp_path):
    server._audit("plan_apply", "team-a", "ConfigMap", "cm-1", True, "dry-run")
    server._audit("apply_manifest", "team-a", "ConfigMap", "cm-1", False, "applied")
    server._audit("delete_resource", "team-a", "ConfigMap", "cm-1", False, "deleted")

    lines = raw_lines(tmp_path)
    assert len(lines) == 3
    prev = server._AUDIT_GENESIS
    for ln in lines:
        entry = json.loads(ln)
        assert entry["prev_sha256"] == prev, "each entry must link to the sha256 of the previous line"
        prev = hashlib.sha256(ln.encode("utf-8")).hexdigest()


def test_multi_writer_same_process_chains_correctly(tmp_path):
    """Concurrent tool calls serialize on the audit lock — no two entries
    may claim the same previous line."""
    import threading

    def write_n(n, tag):
        for i in range(n):
            server._audit("plan_apply", "team-a", "ConfigMap", f"{tag}-{i}", True, "dry-run")

    threads = [threading.Thread(target=write_n, args=(10, f"t{t}")) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = audit_entries(tmp_path)
    assert len(entries) == 40
    seen_prev = [e["prev_sha256"] for e in entries]
    # The genesis link appears exactly once (the first entry); every other
    # link points at a line that actually precedes it.
    assert seen_prev.count(server._AUDIT_GENESIS) == 1
    lines = raw_lines(tmp_path)
    sha_by_index = {i: hashlib.sha256(ln.encode("utf-8")).hexdigest() for i, ln in enumerate(lines)}
    for idx, entry in enumerate(entries):
        if idx == 0:
            continue
        prev_sha = entry["prev_sha256"]
        assert prev_sha in sha_by_index.values()
        assert sha_by_index[idx - 1] == prev_sha


# ---------------------------------------------------------------------------
# tamper evidence via the documented verification
# ---------------------------------------------------------------------------


def test_verify_audit_chain_clean_trail(tmp_path):
    for i in range(5):
        server._audit("plan_apply", "team-a", "ConfigMap", f"cm-{i}", True, "dry-run")
    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is True and report["error"] == ""
    assert report["entries"] == 5 and report["legacy_entries"] == 0
    assert report["first_bad_line"] is None


def test_verify_audit_chain_detects_a_modified_line(tmp_path):
    for i in range(4):
        server._audit("plan_apply", "team-a", "ConfigMap", f"cm-{i}", True, "dry-run")
    lines = raw_lines(tmp_path)
    # TAMPER: rewrite line 2 (index 1) — flip the outcome and keep the link.
    tampered = json.loads(lines[1])
    tampered["outcome"] = "applied"  # forge a cleaner-looking history
    lines[1] = json.dumps(tampered, sort_keys=True)
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is False
    assert report["first_bad_line"] == 3  # line 3's link no longer matches line 2
    assert "tampered" in report["error"] or "reordered" in report["error"]


def test_verify_audit_chain_detects_truncation_and_reordering(tmp_path):
    for i in range(5):
        server._audit("plan_apply", "team-a", "ConfigMap", f"cm-{i}", True, "dry-run")
    lines = raw_lines(tmp_path)

    # TRUNCATION: drop the first entry — line 2's link points at a removed line.
    (tmp_path / "audit.jsonl").write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is False and report["first_bad_line"] == 1

    # REORDER: swap two adjacent lines.
    (tmp_path / "audit.jsonl").write_text(
        "\n".join([lines[0], lines[2], lines[1], lines[3], lines[4]]) + "\n", encoding="utf-8"
    )
    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is False

    # INJECTED blank line = a chain gap.
    (tmp_path / "audit.jsonl").write_text("\n".join(lines[:2] + [""] + lines[2:]) + "\n", encoding="utf-8")
    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is False and "gap" in report["error"]


def test_verify_audit_chain_missing_and_unparseable(tmp_path):
    report = server.verify_audit_chain(str(tmp_path / "missing.jsonl"))
    assert report["exists"] is False and report["ok"] is True  # fresh trail is fine

    (tmp_path / "audit.jsonl").write_text("not json at all\n", encoding="utf-8")
    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is False and report["first_bad_line"] == 1
    assert "not valid JSON" in report["error"]


def test_verify_audit_chain_accepts_pre_hardening_entries_as_roots(tmp_path):
    """Backward compatibility: an OLD trail (7-key entries, no chain fields)
    verifies — each legacy line seeds the chain, so tamper detection begins
    at the first chained entry."""
    legacy = {
        "ts": "2026-01-01T00:00:00Z",
        "tool": "plan_apply",
        "namespace": "team-a",
        "kind": "ConfigMap",
        "name": "old",
        "dry_run": True,
        "outcome": "dry-run",
    }
    p = tmp_path / "audit.jsonl"
    p.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")
    server._audit("apply_manifest", "team-a", "ConfigMap", "new", False, "applied")  # chains onto the legacy line
    report = server.verify_audit_chain(audit_path(tmp_path))
    assert report["ok"] is True
    assert report["legacy_entries"] == 1 and report["entries"] == 2


# ---------------------------------------------------------------------------
# caller identity
# ---------------------------------------------------------------------------


def test_audit_entries_carry_caller_identity_fields(tmp_path):
    server._audit("plan_apply", "team-a", "ConfigMap", "cm", True, "dry-run")
    entry = audit_entries(tmp_path)[0]
    assert set(entry["caller"]) == {"key_fp", "client"}
    # Anonymous outside the HTTP auth layer (direct sync call here).
    assert entry["caller"] == {"key_fp": None, "client": None}


def test_caller_fingerprint_is_stable_non_secret_twelve_hex(monkeypatch):
    """The fingerprint is derived from the MATCHED configured key — stable
    across restarts, joinable fleet-wide, and never the key itself."""
    monkeypatch.setenv("APPLYGATE_API_KEYS", "secret-key-value")
    scope = {"type": "http", "headers": [(b"x-api-key", b"secret-key-value")], "client": ("10.1.2.3", 51000)}
    caller = server._resolve_caller(scope)
    assert caller.key_fp == "sha256:" + hashlib.sha256(b"secret-key-value").hexdigest()[:12]
    assert "secret-key-value" not in caller.key_fp
    assert caller.client == "10.1.2.3:51000"

    # Wrong key → anonymous (never an unauthorized caller's fingerprint).
    scope_bad = {"type": "http", "headers": [(b"x-api-key", b"wrong")], "client": ("10.9.9.9", 1)}
    assert server._resolve_caller(scope_bad).key_fp is None

    # No keys configured at all (dev mode) → anonymous.
    monkeypatch.delenv("APPLYGATE_API_KEYS", raising=False)
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    assert server._resolve_caller(scope).key_fp is None


def test_http_path_audits_carry_the_resolved_caller(monkeypatch, tmp_path):
    """The caller contextvar — set by _CallerAuditMiddleware exactly like
    this — flows through asyncio.to_thread into the audit entry: MCP-path
    entries are attributed; the raw key never appears."""
    monkeypatch.setenv("APPLYGATE_API_KEYS", "k1")
    scope = {"type": "http", "headers": [(b"x-api-key", b"k1")], "client": ("127.0.0.1", 4242)}
    server._caller_context.set(server._resolve_caller(scope))
    server._plan_apply_sync("team-a", SIMPLE, False)  # sync body, like the console handler
    entry = audit_entries(tmp_path)[0]
    assert entry["caller"]["key_fp"] == "sha256:" + hashlib.sha256(b"k1").hexdigest()[:12]
    assert entry["caller"]["client"] == "127.0.0.1:4242"
    assert "k1" not in json.dumps(entry)


def test_caller_capture_middleware_sets_the_context(monkeypatch):
    """The middleware wrapper stores the resolved caller for the request."""
    monkeypatch.setenv("APPLYGATE_API_KEYS", "k1")

    captured = {}

    async def inner_app(scope, receive, send):
        captured["caller"] = server._caller_context.get()

    mw = server._CallerAuditMiddleware(inner_app)
    scope = {"type": "http", "headers": [(b"x-api-key", b"k1")], "client": ("h", 1)}
    asyncio.run(mw(scope, None, None))
    assert captured["caller"].key_fp == "sha256:" + hashlib.sha256(b"k1").hexdigest()[:12]

    # Non-http scopes (lifespan etc.) pass through untouched — the inner
    # app runs but the middleware never SET a caller for it.
    captured.clear()
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert captured["caller"] is None


def test_middleware_exposes_routes_passthrough(monkeypatch):
    app = server._build_http_app()
    assert app.routes  # introspection of the wrapped app keeps working
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp" in paths and "/healthz" in paths
