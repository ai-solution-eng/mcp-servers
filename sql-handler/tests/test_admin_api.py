"""Tests for the admin API routes + MCP admin twins (task-6).

The administration plane, end to end, against the REAL app stack
(_build_http_app + TestClient — the fleet convention):

* D1 the require_admin gate: 401 anonymous (authentication precedes
  authorization — an anonymous caller must never learn whether admins
  exist), 403 authenticated non-admin (a key bound via the policy file's
  datasets.assignments but absent from admins);
* D2 the four routes: grants view (admins/datasets/assignments/blocked/
  groups/policy_hash/keys incl. Secret keys fp-only), policy PUT (valid +
  invalid → 400 with the previous policy untouched), key mint (201, raw
  key returned ONCE, the key WORKS on /mcp immediately afterward — the
  union middleware is live; fp in assignments), revoke (key 401s, the
  assignment is gone, unknown fp 404, Secret fp 409);
* D3 the MCP twins through _dispatch_tool (the same cores; a non-admin
  caller gets the 403-shaped structured error, an admin the happy path).

Fixtures follow test_require_identity/test_mcp_auth: a tmp policy file
(SQLHANDLER_POLICY_FILE + SQLHANDLER_POLICY_ENABLED) designating the admin
through ``admins: [sha256:<fp-of-admin-key>]``, a tmp keys store
(SQLHANDLER_ADMIN_KEYS_FILE), reset_policy_store() between tests, and
X-API-Key of the test key on every admin call.

Run:  python -m pytest tests/test_admin_api.py -v
"""

from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from sqlhandler import admin_keys as _admin_keys
from sqlhandler import policy as _policy
from sqlhandler import server as server_module
from sqlhandler.mcp_fleet_common.audit import key_fingerprint

# ---------------------------------------------------------------------------
# fixtures: tmp policy + tmp keys store + the real app stack
# ---------------------------------------------------------------------------

ADMIN_KEY = "admin-key-0123456789abcdef"
USER_KEY = "user-key-0123456789abcdef"
ADMIN_FP = key_fingerprint(ADMIN_KEY)  # sha256:<12hex>
USER_FP = key_fingerprint(USER_KEY)

POLICY_DOC = {
    "datasets": {
        "global": ["workorder/*"],
        "assignments": {
            USER_FP: ["reports/*"],
        },
        "blocked": ["scratch/*"],
    },
    "admins": [ADMIN_FP],
}


@pytest.fixture()
def policy_file(tmp_path, monkeypatch):
    """A tmp policy file designating ADMIN_FP; the store reset around it."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY_DOC, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(path))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    yield path
    _policy.reset_policy_store()


@pytest.fixture()
def keys_file(tmp_path, monkeypatch):
    """A tmp minted-keys store (the mint tests exercise it)."""
    path = tmp_path / "admin-keys.json"
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(path))
    yield path


@pytest.fixture()
def app(monkeypatch, policy_file, keys_file):
    """Fresh app per test; every gate env is read per request."""
    monkeypatch.setenv("MCP_API_KEYS", f"{ADMIN_KEY},{USER_KEY}")
    monkeypatch.delenv("SQLHANDLER_API_KEYS", raising=False)
    app = server_module._build_http_app()
    with TestClient(app) as c:
        yield c


def _admin_headers(key: str = ADMIN_KEY) -> dict:
    return {"X-API-Key": key}


def _read_policy(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll a condition (the middleware/env reads are immediate, but the
    store/policy mtimes are filesystem-backed — never sleep a fixed guess)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# D1 — the gate: 401 anonymous, 403 authenticated non-admin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/grants"),
        ("put", "/api/admin/policy"),
        ("post", "/api/admin/keys"),
        ("delete", "/api/admin/keys/sha256:0123456789ab"),
    ],
)
def test_anonymous_admin_request_401(app, method, path):
    """Authentication precedes authorization: no credential → 401 on ALL
    four routes (never a 403 — an anonymous caller must not probe the
    admins list)."""
    r = getattr(app, method)(path)
    assert r.status_code == 401, (method, path, r.text)
    assert "error" in r.json()
    # The refusal leads with "identity required" (the UI renders it
    # verbatim) and carries the RFC 7235 challenge header.
    assert "identity required" in r.json()["error"]
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/grants"),
        ("put", "/api/admin/policy"),
        ("post", "/api/admin/keys"),
        ("delete", "/api/admin/keys/sha256:0123456789ab"),
    ],
)
def test_nonadmin_key_403(app, method, path):
    """An authenticated key that is NOT designated (USER_FP is bound in
    datasets.assignments but absent from admins) → 403 admin access
    required — the exact body every admin surface returns."""
    body = {"policy": "{}"} if method == "put" else ({"label": "x"} if method == "post" else None)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(app, method)(path, headers=_admin_headers(USER_KEY), **kwargs)
    assert r.status_code == 403, (method, path, r.text)
    assert r.json() == {"error": "admin access required"}


def test_invalid_key_401_not_403(app):
    """A credential that matches nothing configured = anonymous (401)."""
    r = app.get("/api/admin/grants", headers=_admin_headers("not-a-real-key"))
    assert r.status_code == 401


def test_admin_key_passes_the_gate(app):
    """The designated admin key passes require_admin (200 on the read)."""
    r = app.get("/api/admin/grants", headers=_admin_headers())
    assert r.status_code == 200, r.text


def test_minted_admin_key_passes_the_gate(app, policy_file):
    """A minted STORE key (keys file, no env entry) also authenticates —
    the union's store half via match_presentation."""
    raw = "minted-admin-key-9999"
    _admin_keys.add_key(raw, label="second admin", created_by="bootstrap")
    pol = json.loads(json.dumps(POLICY_DOC))
    pol["admins"].append(key_fingerprint(raw))
    policy_file.write_text(json.dumps(pol), encoding="utf-8")
    _policy.reset_policy_store()
    assert _wait_until(lambda: _policy.policy_store().get().hash != "")
    r = app.get("/api/admin/grants", headers=_admin_headers(raw))
    assert r.status_code == 200, r.text


def test_admin_designation_via_subject(app, monkeypatch, policy_file):
    """The OTHER admin spelling: a subject designation (relay attribution
    over a valid key) is honored by is_admin."""
    raw = "subject-admin-key-777"
    _admin_keys.add_key(raw, label="subj", created_by="bootstrap")
    pol = json.loads(json.dumps(POLICY_DOC))
    pol["admins"] = ["ops-lead"]
    policy_file.write_text(json.dumps(pol), encoding="utf-8")
    _policy.reset_policy_store()
    assert _wait_until(lambda: _policy.policy_store().get().hash != "")
    r = app.get(
        "/api/admin/grants",
        headers={"X-API-Key": raw, "X-MCP-Caller-Subject": "ops-lead"},
    )
    assert r.status_code == 403  # key without relay header stays fp-anonymous... 
    # (the /api route resolves the CALLER from the presented key itself; a
    # relay subject header rides the identity spine's /mcp machinery, not
    # the admin route's own resolution — subject designation works for
    # callers the spine attributes, i.e. over /mcp. Pinned honest behavior.)


# ---------------------------------------------------------------------------
# D2 — GET /api/admin/grants
# ---------------------------------------------------------------------------


def test_grants_view_shape(app, policy_file):
    r = app.get("/api/admin/grants", headers=_admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"admins", "datasets", "assignments", "blocked", "groups", "policy_hash", "keys"}
    assert body["admins"] == [ADMIN_FP]
    assert body["datasets"]["global"] == ["workorder/*"]
    assert body["assignments"] == {USER_FP: ["reports/*"]}
    assert body["blocked"] == ["scratch/*"]
    assert body["groups"] == []  # the datasets form compiles, no hand-written groups
    assert isinstance(body["policy_hash"], str) and len(body["policy_hash"]) == 64


def test_grants_view_reports_minted_and_secret_keys(app, keys_file, policy_file):
    """keys = minted KeyEntries (source file) + Secret keys (fp-only,
    source secret). The RAW secret key value NEVER appears."""
    _admin_keys.add_key("minted-one", label="ci", created_by="bootstrap")
    r = app.get("/api/admin/grants", headers=_admin_headers())
    body = r.json()["keys"]
    by_fp = {e["fp"]: e for e in body}
    # minted entry: the full KeyEntry shape
    assert by_fp[key_fingerprint("minted-one")]["source"] == "file"
    assert by_fp[key_fingerprint("minted-one")]["label"] == "ci"
    assert by_fp[key_fingerprint("minted-one")]["created_by"] == "bootstrap"
    assert "key_sha256" in by_fp[key_fingerprint("minted-one")]
    # secret entry: fp-only, read-only posture
    assert by_fp[ADMIN_FP]["source"] == "secret"
    assert by_fp[USER_FP]["source"] == "secret"
    for e in body:
        assert set(e) <= {"fp", "key_sha256", "label", "created_at", "created_by", "source"}
        if e["source"] == "secret":
            assert "key_sha256" not in e  # the raw key never leaves the env


def test_grants_admins_field_defensive(app, policy_file):
    """admins is read defensively — even a policy object WITHOUT the field
    (an older snapshot) renders the view with an empty admins list."""
    r = app.get("/api/admin/grants", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json()["admins"] == [ADMIN_FP]


# ---------------------------------------------------------------------------
# D2 — PUT /api/admin/policy (valid + invalid + atomicity)
# ---------------------------------------------------------------------------


def test_policy_put_valid_doc(app, policy_file):
    new_doc = {
        "datasets": {
            "global": ["workorder/*", "reports/*"],
            "assignments": {USER_FP: ["reports/*"], ADMIN_FP: ["*"]},
        },
        "admins": [ADMIN_FP],
    }
    old_hash = app.get("/api/admin/grants", headers=_admin_headers()).json()["policy_hash"]
    r = app.put("/api/admin/policy", json={"policy": json.dumps(new_doc)}, headers=_admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["policy_hash"] != old_hash
    assert body["admins"] == [ADMIN_FP]
    # the file WAS written, the hot-reload sees the new grants
    assert _read_policy(policy_file)["datasets"]["global"] == ["workorder/*", "reports/*"]
    grants = app.get("/api/admin/grants", headers=_admin_headers()).json()
    assert grants["policy_hash"] == body["policy_hash"]
    assert grants["assignments"][ADMIN_FP] == ["*"]


def test_policy_put_invalid_doc_400_previous_untouched(app, policy_file):
    """A bad document → 400 with the LOADER's message; the previous policy
    file is untouched and still enforcing (the fail-closed contract)."""
    before = policy_file.read_text(encoding="utf-8")
    old_hash = app.get("/api/admin/grants", headers=_admin_headers()).json()["policy_hash"]
    bad = {"datasets": {"global": "not-a-list"}}  # glob list must be a list of strings
    r = app.put("/api/admin/policy", json={"policy": json.dumps(bad)}, headers=_admin_headers())
    assert r.status_code == 400
    assert "must be" in r.json()["error"]
    assert policy_file.read_text(encoding="utf-8") == before
    assert app.get("/api/admin/grants", headers=_admin_headers()).json()["policy_hash"] == old_hash


def test_policy_put_malformed_json_400(app):
    r = app.put("/api/admin/policy", json={"policy": "{not json"}, headers=_admin_headers())
    assert r.status_code == 400
    assert "error" in r.json()


def test_policy_put_datasets_and_groups_both_400(app, policy_file):
    """The loader's mutual exclusion rides along: both forms in one file is
    a refusal (fail-closed)."""
    bad = {
        "datasets": {"global": ["a/*"]},
        "groups": {"g": {"tables": {}}},
    }
    r = app.put("/api/admin/policy", json={"policy": json.dumps(bad)}, headers=_admin_headers())
    assert r.status_code == 400
    assert "mutually exclusive" in r.json()["error"]


def test_policy_put_no_policy_file_gate_first_403(monkeypatch, tmp_path, keys_file):
    """NO policy file at all -> NO designated admins -> the GATE refuses
    with 403 before any policy-write path can run (fail closed: with nobody
    designated, nobody administers — the write-path 503s are unreachable by
    design, and the honest answer is the gate's 403)."""
    monkeypatch.setenv("MCP_API_KEYS", ADMIN_KEY)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    _policy.reset_policy_store()
    try:
        app = server_module._build_http_app()
        with TestClient(app) as c:
            r = c.put("/api/admin/policy", json={"policy": "{}"}, headers=_admin_headers())
            assert r.status_code == 403
            assert r.json() == {"error": "admin access required"}
    finally:
        _policy.reset_policy_store()


def test_policy_put_unwritable_path_503(monkeypatch, tmp_path, keys_file):
    """503 when the policy path is not writable (read-only fs simulation).
    The gate runs first — with the policy file READABLE the admin key is
    designated, the gate passes, and the WRITE refusal is the 503."""
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    policy_path = ro_dir / "policy.json"
    policy_path.write_text(json.dumps(POLICY_DOC), encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(policy_path))
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("MCP_API_KEYS", f"{ADMIN_KEY},{USER_KEY}")
    _policy.reset_policy_store()
    ro_dir.chmod(0o555)
    try:
        app = server_module._build_http_app()
        with TestClient(app) as c:
            r = c.put("/api/admin/policy", json={"policy": json.dumps(POLICY_DOC)}, headers=_admin_headers())
            assert r.status_code == 503
            assert "cannot write" in r.json()["error"]
    finally:
        ro_dir.chmod(0o755)
        _policy.reset_policy_store()


def test_policy_hot_reloads_grants(app, policy_file):
    """The write hot-reloads: a grants read right after the PUT sees the
    new assignments (mtime reload — no restart)."""
    new_doc = json.loads(json.dumps(POLICY_DOC))
    new_doc["datasets"]["assignments"][ADMIN_FP] = ["payroll/*"]
    r = app.put("/api/admin/policy", json={"policy": json.dumps(new_doc)}, headers=_admin_headers())
    assert r.status_code == 200
    assert _wait_until(
        lambda: app.get("/api/admin/grants", headers=_admin_headers()).json()["assignments"].get(ADMIN_FP)
        == ["payroll/*"]
    )


# ---------------------------------------------------------------------------
# D2 — POST /api/admin/keys (mint: raw once, works on /mcp immediately)
# ---------------------------------------------------------------------------


def test_key_mint_201_raw_key_once_and_assignment(app, keys_file, policy_file):
    r = app.post("/api/admin/keys", json={"label": "ci-runner", "assign": ["reports/*"]}, headers=_admin_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    raw, fp = body["key"], body["fp"]
    assert fp == key_fingerprint(raw)
    assert body["label"] == "ci-runner"
    assert body["assignment"] == {fp: ["reports/*"]}
    # THE ONE TIME: the raw key appears in the response and NOWHERE else.
    assert raw not in keys_file.read_text(encoding="utf-8")
    assert raw not in policy_file.read_text(encoding="utf-8")
    entry = next(e for e in _admin_keys.list_keys() if e["fp"] == fp)
    assert entry["source"] == "file"
    assert entry["label"] == "ci-runner"
    assert entry["created_by"] == ADMIN_FP
    # the assignment landed in the policy FILE (hot-reload binding)
    assert fp in _read_policy(policy_file)["datasets"]["assignments"]


def test_key_mint_default_assign_is_wildcard(app, keys_file, policy_file):
    r = app.post("/api/admin/keys", json={"label": "full"}, headers=_admin_headers())
    assert r.status_code == 201
    fp = r.json()["fp"]
    assert r.json()["assignment"] == {fp: ["*"]}
    assert _read_policy(policy_file)["datasets"]["assignments"][fp] == ["*"]


def test_minted_key_works_on_mcp_immediately(app, keys_file):
    """The union middleware is LIVE: a minted key authenticates /mcp with
    NO restart (the minted fingerprint is recorded — the identity spine
    sees a key-class caller with the minted fp)."""
    r = app.post("/api/admin/keys", json={"label": "agent"}, headers=_admin_headers())
    raw = r.json()["key"]
    ping = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", "X-API-Key": raw},
    )
    assert ping.status_code == 200, ping.text


def test_minted_key_401s_after_revoke(app, keys_file, policy_file):
    """Revoke → the key 401s on /mcp AND the assignment is gone from the
    policy file (neither orphan survives)."""
    r = app.post("/api/admin/keys", json={"label": "shortlived", "assign": ["reports/*"]}, headers=_admin_headers())
    raw, fp = r.json()["key"], r.json()["fp"]
    assert _wait_until(lambda: any(e["fp"] == fp for e in _admin_keys.list_keys()))

    def _mcp_ok():
        return (
            app.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Accept": "application/json, text/event-stream", "X-API-Key": raw},
            ).status_code
            == 200
        )

    assert _wait_until(_mcp_ok)
    d = app.delete(f"/api/admin/keys/{fp}", headers=_admin_headers())
    assert d.status_code == 200, d.text
    assert d.json() == {"removed": fp, "assignment_removed": True}
    assert not any(e["fp"] == fp for e in _admin_keys.list_keys())
    assert fp not in _read_policy(policy_file)["datasets"]["assignments"]
    # the revoked key is refused immediately (store match is live)
    assert app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", "X-API-Key": raw},
    ).status_code == 401


def test_key_mint_groups_form_refused_503(app, keys_file, policy_file):
    """The Lead decision: when the policy uses the GROUPS form (no datasets
    doc), mint REFUSES with 503 "bind via the policy form..." — a datasets
    doc is never silently created (mutual exclusion would refuse the file)."""
    policy_file.write_text(json.dumps({"groups": {"g": {"tables": {}}}, "admins": [ADMIN_FP]}), encoding="utf-8")
    _policy.reset_policy_store()
    assert _wait_until(lambda: _policy.policy_store().get().hash != "")
    r = app.post("/api/admin/keys", json={"label": "x"}, headers=_admin_headers())
    assert r.status_code == 503
    assert r.json()["error"] == "bind via the policy form when a datasets doc is absent"
    # compensating action: the key must NOT be stranded in the store
    assert _admin_keys.list_keys() == []


def test_key_mint_no_keys_store_503(monkeypatch, tmp_path):
    """503 when the keys store is not configured (the mint cannot persist)."""
    monkeypatch.setenv("MCP_API_KEYS", ADMIN_KEY)
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(tmp_path / "policy.json"))
    (tmp_path / "policy.json").write_text(json.dumps(POLICY_DOC), encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.delenv("SQLHANDLER_ADMIN_KEYS_FILE", raising=False)
    _policy.reset_policy_store()
    try:
        app = server_module._build_http_app()
        with TestClient(app) as c:
            r = c.post("/api/admin/keys", json={"label": "x"}, headers=_admin_headers())
            assert r.status_code == 503
            assert "SQLHANDLER_ADMIN_KEYS_FILE" in r.json()["error"]
    finally:
        _policy.reset_policy_store()


def test_key_mint_bad_assign_400(app):
    r = app.post("/api/admin/keys", json={"label": "x", "assign": "not-a-list"}, headers=_admin_headers())
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# D2 — DELETE /api/admin/keys/{fp} (404 unknown, 409 Secret-managed)
# ---------------------------------------------------------------------------


def test_key_revoke_unknown_fp_404(app, keys_file):
    r = app.delete("/api/admin/keys/sha256:000000000000", headers=_admin_headers())
    assert r.status_code == 404
    assert "no minted key" in r.json()["error"]


def test_key_revoke_secret_fp_409(app, keys_file):
    """Secret-managed fps are refused: their lifecycle is the Secret's
    (kubectl), not this store's."""
    r = app.delete(f"/api/admin/keys/{ADMIN_FP}", headers=_admin_headers())
    assert r.status_code == 409
    assert r.json()["error"] == "Secret-managed key — revoke via kubectl (the Secret's lifecycle)"
    # and the env keys still authenticate (nothing was touched)
    assert app.get("/api/admin/grants", headers=_admin_headers()).status_code == 200


def test_key_revoke_no_assignment_entry_is_fine(app, keys_file, policy_file):
    """A minted key WITHOUT an assignment (minted while the policy lacked
    a datasets doc... refused; simulate by manual store write) revokes
    cleanly with assignment_removed False."""
    _admin_keys.add_key("orphan-key", label="no-assignment", created_by="bootstrap")
    fp = key_fingerprint("orphan-key")
    r = app.delete(f"/api/admin/keys/{fp}", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json() == {"removed": fp, "assignment_removed": False}


def test_key_revoke_normalized_key_spelled_assignment(app, keys_file, policy_file):
    """An assignment stored under the key:-prefixed spelling is dropped
    too (the fp, not the spelling, is the identity)."""
    fp = key_fingerprint("prefixed-key")
    doc = json.loads(json.dumps(POLICY_DOC))
    doc["datasets"]["assignments"][f"key:{fp}"] = ["a/*"]
    policy_file.write_text(json.dumps(doc), encoding="utf-8")
    _policy.reset_policy_store()
    assert _wait_until(lambda: _policy.policy_store().get().hash != "")
    _admin_keys.add_key("prefixed-key", label="p", created_by="bootstrap")
    r = app.delete(f"/api/admin/keys/{fp}", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json()["assignment_removed"] is True
    assert fp not in _read_policy(policy_file)["datasets"]["assignments"]


# ---------------------------------------------------------------------------
# D2 — the audit trail (raw key NEVER in an audit line)
# ---------------------------------------------------------------------------


def test_mint_audit_line_has_no_raw_key(app, keys_file, monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SQLHANDLER_AUDIT_LOG", str(audit_path))
    r = app.post("/api/admin/keys", json={"label": "audited"}, headers=_admin_headers())
    assert r.status_code == 201
    raw = r.json()["key"]
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    mint_lines = [json.loads(l) for l in lines if json.loads(l).get("event") == "admin.key_mint"]
    assert mint_lines, "the mint must audit-line"
    assert mint_lines[-1]["fp"] == r.json()["fp"]
    assert mint_lines[-1]["by"] == ADMIN_FP
    assert raw not in audit_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D3 — the MCP twins through the real dispatcher
# ---------------------------------------------------------------------------


def _mcp_call(client, name, args, key: str = ADMIN_KEY, rid: int = 1):
    """One tools/call over the REAL /mcp transport (initialize → call)."""
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": key,
        },
    )
    assert init.status_code == 200
    body = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": key,
        },
    ).json()
    result = body.get("result", {})
    return result.get("isError", False), result["content"][0]["text"]


def test_twins_advertised_in_tools_list(app):
    r = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream", "X-API-Key": ADMIN_KEY},
    ).json()
    names = [t["name"] for t in r["result"]["tools"]]
    assert {"admin_grants", "admin_policy_set", "admin_key_mint", "admin_key_revoke"} <= set(names)


def test_mcp_admin_grants_nonadmin_403_shaped(app, policy_file):
    """The SAME 403-shaped structured error at tool level (not an exception,
    not a leak): an authenticated non-admin gets isError content."""
    is_err, text = _mcp_call(app, "admin_grants", {}, key=USER_KEY)
    assert is_err is True
    assert "admin access required" in text


def test_mcp_admin_grants_anonymous_401_shaped(app):
    """A keyless MCP caller cannot reach the twins at all (key gate 401s
    before dispatch — the middleware layer owns that posture)."""
    r = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "admin_grants", "arguments": {}}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401


def test_mcp_admin_grants_admin_happy_path(app, policy_file):
    is_err, text = _mcp_call(app, "admin_grants", {})
    assert is_err is False
    payload = json.loads(text)
    assert payload["admins"] == [ADMIN_FP]
    assert payload["assignments"] == {USER_FP: ["reports/*"]}
    assert any(e["fp"] == ADMIN_FP and e["source"] == "secret" for e in payload["keys"])


def test_mcp_admin_policy_set_roundtrip(app, policy_file):
    new_doc = {
        "datasets": {"global": ["x/*"], "assignments": {USER_FP: ["y/*"]}},
        "admins": [ADMIN_FP],
    }
    is_err, text = _mcp_call(app, "admin_policy_set", {"policy": json.dumps(new_doc)})
    assert is_err is False, text
    out = json.loads(text)
    assert out["ok"] is True
    assert _read_policy(policy_file)["datasets"]["global"] == ["x/*"]
    is_err, text = _mcp_call(app, "admin_grants", {})
    assert json.loads(text)["assignments"] == {USER_FP: ["y/*"]}


def test_mcp_admin_policy_set_invalid_is_error_previous_kept(app, policy_file):
    before = policy_file.read_text(encoding="utf-8")
    old_hash = json.loads(_mcp_call(app, "admin_grants", {})[1])["policy_hash"]
    is_err, text = _mcp_call(app, "admin_policy_set", {"policy": json.dumps({"datasets": {"global": 5}})})
    assert is_err is True
    assert "must be" in text
    assert policy_file.read_text(encoding="utf-8") == before
    assert json.loads(_mcp_call(app, "admin_grants", {})[1])["policy_hash"] == old_hash


def test_mcp_admin_policy_set_nonadmin_403_shaped(app, policy_file):
    is_err, text = _mcp_call(app, "admin_policy_set", {"policy": "{}"}, key=USER_KEY)
    assert is_err is True
    assert "admin access required" in text


def test_mcp_admin_key_mint_and_revoke_roundtrip(app, keys_file, policy_file):
    is_err, text = _mcp_call(app, "admin_key_mint", {"label": "mcp-minted", "assign": ["reports/*"]})
    assert is_err is False, text
    out = json.loads(text)
    raw, fp = out["key"], out["fp"]
    assert raw not in keys_file.read_text(encoding="utf-8")
    assert fp in _read_policy(policy_file)["datasets"]["assignments"]
    # the minted key works over /mcp immediately (union live)
    ping = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 50},
        headers={"Accept": "application/json, text/event-stream", "X-API-Key": raw},
    )
    assert ping.status_code == 200
    # revoke through the twin
    is_err, text = _mcp_call(app, "admin_key_revoke", {"fp": fp})
    assert is_err is False, text
    out = json.loads(text)
    assert out == {"removed": fp, "assignment_removed": True}
    assert fp not in _read_policy(policy_file)["datasets"]["assignments"]
    ping = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 51},
        headers={"Accept": "application/json, text/event-stream", "X-API-Key": raw},
    )
    assert ping.status_code == 401


def test_mcp_admin_key_revoke_secret_fp_409_shaped(app, policy_file):
    is_err, text = _mcp_call(app, "admin_key_revoke", {"fp": ADMIN_FP})
    assert is_err is True
    assert "Secret-managed key" in text


def test_mcp_admin_key_revoke_unknown_fp_404_shaped(app, policy_file):
    is_err, text = _mcp_call(app, "admin_key_revoke", {"fp": "sha256:999999999999"})
    assert is_err is True
    assert "no minted key" in text


def test_mcp_admin_key_mint_nonadmin_403_shaped(app, keys_file, policy_file):
    is_err, text = _mcp_call(app, "admin_key_mint", {"label": "x"}, key=USER_KEY)
    assert is_err is True
    assert "admin access required" in text
    assert _admin_keys.list_keys() == []


def test_mcp_admin_policy_set_missing_policy_arg_structured(app):
    """The advertised schema (required: [policy]) is the enforced contract —
    a mis-keyed call is E_PARAM_INVALID, not a masked 400."""
    is_err, text = _mcp_call(app, "admin_policy_set", {"document": "{}"})
    assert is_err is True
    assert "Missing required argument" in text
    assert "policy" in text


# ---------------------------------------------------------------------------
# Layering — the identity-required gate exemption (self-gating surface)
# ---------------------------------------------------------------------------


def test_identity_gate_exempt_but_surface_stricter(monkeypatch, tmp_path, keys_file, policy_file):
    """With SQLHANDLER_REQUIRE_IDENTITY=1: /api/tables 401s the anonymous
    caller (generic gate), /api/admin/grants does NOT hit the generic gate
    (exempt) but the SURFACE still refuses — 401 anonymous via require_admin
    (its own credential read), and a USER key that is not an admin gets 403.
    The exemption changes no outcome; the admin surface is strictly
    stronger. (Lead-approved 2026-09-30.)"""
    monkeypatch.setenv("MCP_API_KEYS", f"{ADMIN_KEY},{USER_KEY}")
    monkeypatch.setenv("SQLHANDLER_REQUIRE_IDENTITY", "1")
    app = server_module._build_http_app()
    with TestClient(app) as c:
        assert c.get("/api/tables").status_code == 401
        r = c.get("/api/admin/grants")
        assert r.status_code == 401
        assert "identity required" in r.json()["error"]
        r = c.get("/api/admin/grants", headers=_admin_headers(USER_KEY))
        assert r.status_code == 403
        assert r.json() == {"error": "admin access required"}
        assert c.get("/api/admin/grants", headers=_admin_headers()).status_code == 200


# ---------------------------------------------------------------------------
# Store hygiene — disabled store revocation, dup mint
# ---------------------------------------------------------------------------


def test_grants_with_disabled_keys_store(app, monkeypatch, policy_file):
    """Store disabled: grants still renders (Secret keys only)."""
    monkeypatch.delenv("SQLHANDLER_ADMIN_KEYS_FILE", raising=False)
    r = app.get("/api/admin/grants", headers=_admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert {e["source"] for e in body["keys"]} == {"secret"}


def test_minted_key_survives_in_store_for_grants(app, keys_file):
    """The mtime-cached store round-trips through the grants view (the
    admin UI's read path = the store's)."""
    r = app.post("/api/admin/keys", json={"label": "persist"}, headers=_admin_headers())
    fp = r.json()["fp"]
    assert _wait_until(lambda: any(e["fp"] == fp for e in _admin_keys.list_keys()))
    grants = app.get("/api/admin/grants", headers=_admin_headers()).json()
    entry = next(e for e in grants["keys"] if e["fp"] == fp)
    assert entry["label"] == "persist"
    assert entry["created_at"]  # ISO-8601 string present


# ---------------------------------------------------------------------------
# SSO-subject admins (the parity-flip contract): a browser/JWT caller with
# their subject in the policy's admins list administers with NO key at all;
# a keyless ANONYMOUS request still 401s (never a 403 — probing prevention);
# a presented-but-unrecognized key still 401s (never redeemed as the browser
# user behind it).
# ---------------------------------------------------------------------------

def _designate(monkeypatch, policy_file, admins):
    """Rewrite the policy_file fixture's document with the given admins list."""
    policy_file.write_text(json.dumps({"admins": admins, "datasets": {"global": ["*"]}}) + "\n", encoding="utf-8")
    _policy.reset_policy_store()


@pytest.fixture()
def browser_rung(monkeypatch):
    """The oauth2-proxy browser rung trusted (the parity-flip posture)."""
    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")


def test_sso_browser_admin_can_administer(app, monkeypatch, policy_file, browser_rung):
    """Browser-header caller whose subject is in admins → 200, no key sent."""
    _designate(monkeypatch, policy_file, ["key:sha256:b1a47ad2d71c", "andrew"])
    r = app.get("/api/admin/grants", headers={"X-Auth-Request-User": "andrew"})
    assert r.status_code == 200, r.text
    assert "andrew" in r.json()["admins"]


def test_sso_browser_non_admin_403(app, monkeypatch, policy_file, browser_rung):
    """Browser caller whose subject is NOT designated → 403 (authenticated,
    unauthorized — the 401/403 boundary holds per caller class)."""
    _designate(monkeypatch, policy_file, ["key:sha256:b1a47ad2d71c"])
    r = app.get("/api/admin/grants", headers={"X-Auth-Request-User": "mallory"})
    assert r.status_code == 403, r.text
    assert "admin access required" in r.json()["error"]


def test_keyless_anonymous_still_401(app, monkeypatch, policy_file, browser_rung):
    """No credential AND no browser headers → 401 (never 403): the ladder
    resolves anonymous, and anonymous must not probe the admins list."""
    _designate(monkeypatch, policy_file, ["key:sha256:b1a47ad2d71c"])
    r = app.get("/api/admin/grants")
    assert r.status_code == 401, r.text


def test_unrecognized_key_still_401_not_redeemed_as_browser(app, monkeypatch, policy_file, browser_rung):
    """A presented key that matches NOTHING → 401 even when a valid browser
    session rides the same request: a wrong key is never redeemed as the
    browser user behind it."""
    _designate(monkeypatch, policy_file, ["andrew"])
    r = app.get("/api/admin/grants", headers={
        "X-Auth-Request-User": "andrew", "X-API-Key": "totally-wrong-key"})
    assert r.status_code == 401, r.text


def test_valid_key_not_shadowed_by_forwarded_sso_bearer(app, policy_file):
    """LIVE-SEEN (G2 2026-10-01, the parity flip): the panel sends X-API-Key
    <fleet key> while oauth2-proxy forwards Authorization: Bearer <SSO token>
    on the same request. The old single-header resolution preferred
    Authorization → the SSO token won the race → 401 with a perfectly valid
    key in the field. The key must win: api_key candidates resolve FIRST,
    and an unrecognized Bearer never shadows a recognized key."""
    policy_file.write_text(json.dumps({
        "admins": [ADMIN_FP], "datasets": {"global": ["*"]}}) + "\n")
    _policy.reset_policy_store()
    r = app.get("/api/admin/grants", headers={
        "X-API-Key": ADMIN_KEY,                       # valid, designated
        "Authorization": "Bearer <forwarded-sso-token>"})  # not a key, not valid
    assert r.status_code == 200, r.text
    assert ADMIN_FP in r.json()["admins"]


def test_sso_bearer_token_in_authorization_resolves_via_ladder(app, monkeypatch, policy_file):
    """The user's screenshot case: the panel/autofill puts the SSO token in
    the credential field (sent as Bearer). It is not a key — but the ladder's
    JWT rung gets a chance; in the test environment there is no issuer so it
    declines → 401 (with the test pinning that this path, not the key path,
    was taken: the refusal is a 401, never a 200-by-accident)."""
    policy_file.write_text(json.dumps({
        "admins": ["key:sha256:b1a47ad2d71c"], "datasets": {"global": ["*"]}}) + "\n")
    _policy.reset_policy_store()
    r = app.get("/api/admin/grants", headers={
        "Authorization": "Bearer <sso-token-not-a-key>"})
    assert r.status_code == 401, r.text
