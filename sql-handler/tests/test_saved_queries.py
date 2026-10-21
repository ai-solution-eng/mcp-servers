"""Tests for saved parameterized queries (Wave 5 additive feature).

Covers CRUD + persistence, save-time validation (D2 guard + parse check),
the BIND-parameter contract (an injection attempt through call-time params
must behave as a literal value, never as string interpolation), the
write-mutation auth-gating matrix (configured credential → unauthenticated
writes refused; no credential → single-user-local mode allowed), and the
run-time re-application of the read-only guard against a hand-poisoned store.
"""

import json
import logging
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import saved as saved_module
from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo
from sqlhandler.saved import (
    NotAuthorized,
    SavedQueryStore,
    UnknownSavedQuery,
    assert_write_allowed,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Every test gets its own store file + clean auth env + fresh singleton."""
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved-queries.json"))
    for env in ("SQLHANDLER_API_TOKEN", "MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        monkeypatch.delenv(env, raising=False)
    saved_module.reset_saved_query_store()
    yield
    saved_module.reset_saved_query_store()


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    return SqlEngine(P(), cache_ttl=0)


class FakeRequest:
    """Mimics the transport's request enough for the write gate.

    headers feeds presented_credential() (a plain lowercase dict, as the
    credential helpers read it); scope is the ASGI shape the identity ladder
    resolves from (identity.caller_from_request_state reads scope["state"] and
    the raw header pairs). Both are derived from the same mapping, so a test
    can present a key, a browser identity, or nothing at all and have the gate
    see exactly that.
    """

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/saved-queries",
            "headers": [
                (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
                for k, v in self.headers.items()
            ],
            "state": {},
        }


# --------------------------------------------------------------------- CRUD


def test_save_list_get_delete_roundtrip():
    store = SavedQueryStore()
    entry = store.save("kind-a", "SELECT * FROM work_order WHERE kind = $k", {"k": "a"}, "kind A orders")
    assert entry["name"] == "kind-a"
    assert entry["sql"] == "SELECT * FROM work_order WHERE kind = $k"
    assert entry["params"] == {"k": "a"}
    assert entry["description"] == "kind A orders"
    assert entry["created_at"] and entry["updated_at"]

    listed = store.list()
    assert [e["name"] for e in listed] == ["kind-a"]

    fetched = store.get("kind-a")
    assert fetched["sql"].endswith("$k")

    assert store.delete("kind-a") is True
    assert store.list() == []
    assert store.delete("kind-a") is False  # store-level: plain False
    # the api layer is what turns "not found" into UnknownSavedQuery (→ 404):
    with pytest.raises(UnknownSavedQuery):
        saved_module.api_saved_delete("kind-a", None)


def test_upsert_preserves_created_at():
    store = SavedQueryStore()
    first = store.save("q", "SELECT 1")
    time.sleep(0.02)
    second = store.save("q", "SELECT 2")
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert store.get("q")["sql"] == "SELECT 2"


def test_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "persisted.json")
    SavedQueryStore(path).save("q1", "SELECT 1 AS one", {"x": 1})
    again = SavedQueryStore(path)
    assert [e["name"] for e in again.list()] == ["q1"]
    assert again.get("q1")["params"] == {"x": 1}


@pytest.mark.parametrize(
    "name",
    ["", "   ", "a/b", "a\\b", "with\nnewline", "x" * 129, 42],
)
def test_name_validation_refuses(name):
    with pytest.raises(ValueError, match="name"):
        SavedQueryStore().save(name, "SELECT 1")


def test_store_corrupt_file_is_quarantined_not_wiped(tmp_path):
    """A corrupt store is moved aside; the next save cannot destroy it.

    Before the fix a corrupt file loaded as EMPTY, so the next save rewrote
    the store with one entry and silently destroyed every other saved query
    (audit 2026-10-16 P2).
    """
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    store = SavedQueryStore(str(path))
    assert store.list() == []
    store.save("q", "SELECT 1")  # recovers by overwriting
    assert [e["name"] for e in store.list()] == ["q"]
    # ...and the original bytes are preserved for recovery
    quarantined = list(tmp_path.glob("broken.json.corrupt-*"))
    assert len(quarantined) == 1, quarantined
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"


def test_store_unreadable_file_refuses_write(tmp_path, monkeypatch):
    """An EXISTING but unreadable store must refuse the write, not wipe it.

    ``_load`` answers "what can I show?" (empty on any problem); a mutating
    caller needs "do I have the current contents?", because writing an empty
    base is data loss.
    """
    path = tmp_path / "store.json"
    store = SavedQueryStore(str(path))
    store.save("keep", "SELECT 1")
    path.chmod(0o000)
    try:
        with pytest.raises(saved_module.NotAuthorized, match="cannot be read"):
            SavedQueryStore(str(path)).save("other", "SELECT 2")
    finally:
        path.chmod(0o600)
    # the original entry survived
    assert [e["name"] for e in SavedQueryStore(str(path)).list()] == ["keep"]


# ------------------------------------------------- owner scoping (identity spine)

ALICE = None  # bound in the fixtures below to keep the Caller import local


def _caller(subject=None, key_fp=None):
    from sqlhandler.identity import Caller

    return Caller(cls="user" if subject else "key", subject=subject, key_fp=key_fp, via="relay")


@pytest.fixture()
def _policy_on(monkeypatch, tmp_path):
    """Policy enforcement ON with a valid (default_group-bearing) policy file."""
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps({"version": 1, "default_group": "analysts", "groups": {"analysts": {"tables": {}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(pf))
    from sqlhandler.policy import reset_policy_store

    reset_policy_store()
    yield
    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    reset_policy_store()


def test_owner_scoping_enforced(_policy_on, tmp_path):
    """A caller sees only their own entries; cross-owner delete is refused."""
    alice, bob = _caller(subject="alice"), _caller(key_fp="sha256:bbbb")
    store = SavedQueryStore(str(tmp_path / "s.json"))
    store.save("a_q", "SELECT 1", caller=alice)
    store.save("b_q", "SELECT 2", caller=bob)
    assert [e["name"] for e in store.list(caller=alice)] == ["a_q"]
    assert [e["name"] for e in store.list(caller=bob)] == ["b_q"]
    assert store.get("a_q", caller=bob) is None
    assert store.get("a_q", caller=alice) is not None
    with pytest.raises(NotAuthorized):
        store.delete("a_q", caller=bob)
    assert store.delete("a_q", caller=alice) is True


def test_owner_scoping_off_is_shared(_policy_on, tmp_path, monkeypatch):
    """Enforcement OFF -> the store is shared and byte-identical (no owner)."""
    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    from sqlhandler.policy import reset_policy_store

    reset_policy_store()
    alice, bob = _caller(subject="alice"), _caller(key_fp="sha256:bbbb")
    store = SavedQueryStore(str(tmp_path / "s.json"))
    store.save("a_q", "SELECT 1", caller=alice)
    store.save("b_q", "SELECT 2", caller=bob)
    assert [e["name"] for e in store.list(caller=alice)] == ["a_q", "b_q"]
    assert store.get("a_q", caller=bob) is not None
    # no owner field is written at all when enforcement is off
    raw = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert all("owner" not in e for e in raw["queries"].values())


def test_unowned_legacy_entries_stay_visible(_policy_on, tmp_path):
    """Entries saved before attribution must not vanish when enforcement turns on."""
    store = SavedQueryStore(str(tmp_path / "s.json"))
    store.save("legacy", "SELECT 1")  # no caller -> no owner recorded
    alice = _caller(subject="alice")
    assert [e["name"] for e in store.list(caller=alice)] == ["legacy"]
    assert store.get("legacy", caller=alice) is not None
    # ...but a cross-owner entry is still invisible
    store.save("mine", "SELECT 2", caller=alice)
    bob = _caller(key_fp="sha256:bbbb")
    assert [e["name"] for e in store.list(caller=bob)] == ["legacy"]


def test_store_entry_cap(tmp_path, monkeypatch):
    """The store is bounded; an OVERWRITE is still allowed at the cap."""
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_MAX", "3")
    path = tmp_path / "store.json"
    store = SavedQueryStore(str(path))
    for i in range(3):
        store.save(f"n{i}", f"SELECT {i}")
    assert len(store.list()) == 3
    with pytest.raises(ValueError, match="full"):
        store.save("overflow", "SELECT 1")
    assert len(store.list()) == 3
    store.save("n0", "SELECT 111")  # overwrite of an existing name is fine
    assert len(store.list()) == 3
    assert [e for e in store.list() if e["name"] == "n0"][0]["sql"] == "SELECT 111"


# ------------------------------------------------- save-time validation (D2)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE work_order",
        "CREATE TABLE x (a int)",
        "INSERT INTO work_order VALUES (1)",
        "ATTACH 'file.db' AS exfil",
        "SELECT 1; DROP TABLE work_order",
    ],
)
def test_save_refuses_ddl_with_d2_error(sql):
    with pytest.raises(ValueError, match="SQLHANDLER_MCP_READONLY"):
        SavedQueryStore().save("evil", sql)


def test_save_refuses_unparseable_sql():
    with pytest.raises(ValueError, match="Could not parse"):
        SavedQueryStore().save("bad", "not even sql")


def test_save_with_readonly_optout_allows_ddl(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    entry = SavedQueryStore().save("ddl", "CREATE TEMP TABLE t AS SELECT 1")  # noqa: DUO123 - opt-out posture under test
    assert "CREATE" in entry["sql"]


def test_save_refuses_nested_params():
    with pytest.raises(ValueError, match="scalars"):
        SavedQueryStore().save("q", "SELECT $x", {"x": [1, 2]})


# ----------------------------------------------- bind params + injection test


def test_run_uses_bind_params_injection_is_inert(tmp_path, monkeypatch):
    """THE injection proof: a param value that is classic SQL injection runs
    as a literal string through DuckDB's bind path — 0 rows, not all rows."""
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    store = SavedQueryStore()
    store.save("by-kind", "SELECT id FROM work_order WHERE kind = $k", {"k": "a"})
    payload = json.loads(server.query_saved("by-kind", params={"k": "a"}, output_format="json"))
    assert payload["rows"] == [[1], [3], [5]]

    # the injection attempt: stored SQL must keep its placeholder
    assert "$k" in store.get("by-kind")["sql"]
    payload = json.loads(
        server.query_saved("by-kind", params={"k": "a' OR 1=1 --"}, output_format="json")
    )
    assert payload["rows"] == [], "injection must not leak other rows (bind, not interpolate)"

    # positional-? variant too
    store.save("positional", "SELECT id FROM work_order WHERE kind = ?", ["b"])
    payload = json.loads(server.query_saved("positional", output_format="json"))
    assert payload["rows"] == [[2], [4]]
    payload = json.loads(
        server.query_saved("positional", params=["x' OR '1'='1"], output_format="json")
    )
    assert payload["rows"] == []


def test_call_params_override_stored(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    store = SavedQueryStore()
    store.save("by-kind", "SELECT id FROM work_order WHERE kind = $k", {"k": "a"})
    payload = json.loads(server.query_saved("by-kind", params={"k": "b"}, output_format="json"))
    assert payload["rows"] == [[2], [4]]


def test_run_reapplies_readonly_guard_against_poisoned_store(tmp_path, monkeypatch):
    """A hand-edited store file cannot smuggle DDL past the run-time guard."""
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    store = SavedQueryStore()
    store.save("q", "SELECT 1")
    # poison the file behind the API's back
    path = tmp_path / "saved-queries.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["queries"]["evil"] = {"sql": "DROP TABLE work_order", "created_at": "x", "updated_at": "x"}
    path.write_text(json.dumps(data), encoding="utf-8")
    text, is_error = server._dispatch_tool("query_saved", {"name": "evil"})
    assert is_error is True
    assert "SQLHANDLER_MCP_READONLY" in text
    # the lake table is untouched
    assert eng.query_duckdb("SELECT count(*) AS n FROM work_order").to_pydict() == {"n": [5]}


def test_run_unknown_name_is_clean_error():
    with pytest.raises(UnknownSavedQuery, match="Unknown saved query"):
        saved_module.api_saved_run("nope", {})


# ------------------------------------------------------------- MCP tools


def test_mcp_query_save_list_delete_roundtrip(monkeypatch):
    monkeypatch.setattr(server, "_handler", lambda: None)
    text, is_error = server._dispatch_tool(
        "query_save",
        {"name": "orders", "sql": "SELECT * FROM work_order WHERE kind = $k", "params": {"k": "a"}, "description": "d"},
    )
    assert is_error is False
    entry = json.loads(text)
    assert entry["saved"] is True and entry["name"] == "orders"

    text, is_error = server._dispatch_tool("query_list", {})
    assert is_error is False
    assert "orders" in text and "kind = $k" in text

    text, is_error = server._dispatch_tool("query_delete", {"name": "orders"})
    assert is_error is False
    assert "Deleted" in text
    text, is_error = server._dispatch_tool("query_delete", {"name": "orders"})
    assert is_error is True and "Unknown saved query" in text


def test_mcp_query_saved_run(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    server._dispatch_tool("query_save", {"name": "b-kind", "sql": "SELECT id FROM work_order WHERE kind = $k", "params": {"k": "b"}})
    text, is_error = server._dispatch_tool("query_saved", {"name": "b-kind"})
    assert is_error is False
    assert "4" in text and "2" in text  # ids 2 and 4 in the markdown


# ------------------------------------------------------- auth-gating matrix


def test_gate_no_auth_configured_allows_writes():
    """No credential env → single-user-local mode: writes allowed."""
    assert saved_module.auth_configured() is False
    assert_write_allowed(None)  # does not raise (stdio tool call included)
    assert_write_allowed(FakeRequest({}))  # unauthenticated HTTP caller too


def test_gate_token_configured_refuses_unauthenticated_writes():
    import os

    os.environ["SQLHANDLER_API_TOKEN"] = "sekret"
    try:
        assert saved_module.auth_configured() is True
        with pytest.raises(NotAuthorized, match="SQLHANDLER_API_TOKEN"):
            assert_write_allowed(None)  # stdio: cannot present credentials
        with pytest.raises(NotAuthorized, match="credential"):
            assert_write_allowed(FakeRequest({}))  # HTTP caller without one
        with pytest.raises(NotAuthorized):
            assert_write_allowed(FakeRequest({"authorization": "Bearer wrong"}))
        with pytest.raises(NotAuthorized):
            assert_write_allowed(FakeRequest({"x-api-key": "wrong"}))
        # correct credential passes (constant-time compared)
        assert_write_allowed(FakeRequest({"authorization": "Bearer sekret"}))
        assert_write_allowed(FakeRequest({"x-api-key": "sekret"}))
        assert_write_allowed(FakeRequest({"x-api-token": "sekret"}))
    finally:
        os.environ.pop("SQLHANDLER_API_TOKEN", None)


def test_gate_accepts_minted_store_key(tmp_path, monkeypatch):
    """F5: the minted/self-minted keys store is part of the accepted set —
    a key the panel minted must be a first-class write credential here (it
    already is on /mcp and /api/admin/*). The store was the one credential
    resolver saved.py never consulted."""
    from sqlhandler import admin_keys as _admin_keys

    keys_file = tmp_path / "admin-keys.json"
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(keys_file))
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")

    # a minted key in the store (raw exists only in this test)
    raw = "minted-raw-key-abcdef0123456789"
    _admin_keys.add_key(raw, label="t", created_by="test")

    # env key still passes; WRONG env key still refused
    assert saved_module.credential_ok("sekret") is True
    assert saved_module.credential_ok("not-a-key") is False
    # the MINTED key now passes too (the fix), including via the write gate
    assert saved_module.credential_ok(raw) is True
    assert_write_allowed(FakeRequest({"x-api-key": raw}))
    # and the gate still refuses an unknown key
    with pytest.raises(NotAuthorized):
        assert_write_allowed(FakeRequest({"x-api-key": "nope"}))


def test_gate_message_mentions_minted_keys_and_sso():
    """F6: the 401 text must not tell an SSO caller to 'present Bearer' as if
    their session were the problem — it names minted keys and the panel."""
    import os

    os.environ["SQLHANDLER_API_TOKEN"] = "sekret"
    try:
        with pytest.raises(NotAuthorized) as ei:
            assert_write_allowed(FakeRequest({}))
        msg = str(ei.value)
        assert "mint a key" in msg
        assert "minted keys are accepted" in msg
        assert "SSO" in msg
    finally:
        os.environ.pop("SQLHANDLER_API_TOKEN", None)


# ------------------------------- the designated-admin path (SSO without keys)
#
# The deployment's normal posture is an SSO/OIDC browser session: the caller
# presents NO api key at all. Gating on the key alone refused the very admins
# who can rewrite the ACL through /api/admin/*, so the gate now ALSO admits a
# caller the identity ladder resolves to a policy-designated admin. These
# tests pin both halves: the admin gets in, and nobody else gains anything.


def _policy_with_admins(monkeypatch, tmp_path, admins):
    """A policy file with the given admins list; store reset around it."""
    from sqlhandler.policy import reset_policy_store

    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps({"admins": admins, "datasets": {"global": ["*"]}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(pf))
    reset_policy_store()
    return pf


@pytest.fixture()
def _admins_reset():
    """Drop the cached policy after a test that loaded one."""
    from sqlhandler.policy import reset_policy_store

    yield
    reset_policy_store()


def test_write_caller_is_admin_helper_is_fail_closed(monkeypatch, tmp_path, _admins_reset):
    """The helper refuses everything that is not a resolved, designated admin.

    Fail-closed shape, one clause per refusal: no request object (stdio), an
    anonymous HTTP request, a resolved subject that is NOT designated, and no
    policy at all. Each must be False -- this helper sits on a REFUSAL path, so
    an "I could not tell" answer has to mean no.
    """
    from sqlhandler.policy import reset_policy_store

    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    # no policy configured at all -> nobody is designated
    reset_policy_store()
    assert saved_module.write_caller_is_admin(None) is False
    assert saved_module.write_caller_is_admin(FakeRequest({})) is False
    assert saved_module.write_caller_is_admin(FakeRequest({"x-auth-request-user": "andrew"})) is False

    # a policy WITH admins: only the designated subject resolves True
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])
    assert saved_module.write_caller_is_admin(FakeRequest({"x-auth-request-user": "andrew"})) is True
    assert saved_module.write_caller_is_admin(FakeRequest({"x-auth-request-user": "mallory"})) is False
    assert saved_module.write_caller_is_admin(FakeRequest({})) is False
    assert saved_module.write_caller_is_admin(None) is False


def test_write_caller_is_admin_needs_the_browser_rung_trusted(monkeypatch, tmp_path, _admins_reset):
    """The admin resolution is the LADDER's, including its trust gate: with
    SQLHANDLER_TRUST_BROWSER_HEADERS unset the browser-header rung is closed,
    so the same header resolves anonymous and the write is refused. The new
    path must not become a way to claim an admin identity by header alone on
    a deployment that never opted into trusting the gateway."""
    monkeypatch.delenv("SQLHANDLER_TRUST_BROWSER_HEADERS", raising=False)
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    assert saved_module.write_caller_is_admin(FakeRequest({"x-auth-request-user": "andrew"})) is False
    with pytest.raises(NotAuthorized):
        assert_write_allowed(FakeRequest({"x-auth-request-user": "andrew"}))


def test_gate_designated_admin_without_any_key_is_allowed(monkeypatch, tmp_path, _admins_reset):
    """THE fix: an admin whose identity arrives as an SSO session (browser
    headers, no api key anywhere on the request) may write.

    Before the fix this exact request was 401 -- the gate read only the key
    headers, found nothing, and refused a caller /api/admin/grants happily
    served.
    """
    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    admin = FakeRequest({"x-auth-request-user": "andrew"})
    assert saved_module.presented_credential(admin) == "", "the admin presents no key"
    assert_write_allowed(admin)  # does not raise -- the admin path admits it

    # ...and it reaches the store through the API layer too (both write verbs)
    entry = saved_module.api_saved_save({"name": "admin_q", "sql": "SELECT 1"}, admin)
    assert entry["name"] == "admin_q"
    assert saved_module.api_saved_delete("admin_q", admin) == {"deleted": "admin_q"}


def test_gate_sso_non_admin_without_key_is_still_refused(monkeypatch, tmp_path, _admins_reset):
    """The crux: an SSO login that is NOT a designated admin gains NOTHING.

    Same trusted browser rung, same policy -- the subject is simply not in the
    admins list. Refused with the same NotAuthorized, and the message still
    tells them to mint a key (never that their session was the problem).
    """
    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    with pytest.raises(NotAuthorized) as ei:
        assert_write_allowed(FakeRequest({"x-auth-request-user": "mallory"}))
    assert "mint a key" in str(ei.value)
    with pytest.raises(NotAuthorized):
        saved_module.api_saved_save({"name": "q", "sql": "SELECT 1"}, FakeRequest({"x-auth-request-user": "mallory"}))


def test_gate_anonymous_http_caller_still_refused_with_policy_loaded(monkeypatch, tmp_path, _admins_reset):
    """Regression guard for the whole change: with credentials configured and
    an admins list loaded, a caller that resolves ANONYMOUS is still refused.
    The admin path is an addition, not a general opening."""
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    with pytest.raises(NotAuthorized, match="SQLHANDLER_API_TOKEN"):
        assert_write_allowed(None)  # stdio cannot carry credentials
    with pytest.raises(NotAuthorized):
        assert_write_allowed(FakeRequest({}))
    with pytest.raises(NotAuthorized):
        assert_write_allowed(FakeRequest({"authorization": "Bearer wrong"}))
    with pytest.raises(NotAuthorized):
        assert_write_allowed(FakeRequest({"x-api-key": "wrong"}))


def test_gate_designated_admin_fingerprint_rides_the_key_path(monkeypatch, tmp_path, _admins_reset):
    """A key fingerprint in the admins list is served by the KEY path.

    credential_ok accepts the minted key, so the write is allowed and the
    admin helper is never consulted (it is the fallback for callers with no
    key). Pinned explicitly because the division of labour is the design:
    the helper deliberately does NOT re-derive a key identity — on /api/*
    the key middleware records no fingerprint into the scope, so a
    key-carrying caller is the key path's business, and only a keyless
    session caller reaches the ladder.
    """
    from sqlhandler import admin_keys as _admin_keys
    from sqlhandler.mcp_fleet_common.audit import key_fingerprint

    raw = "minted-designated-admin-0123456789"
    monkeypatch.setenv("SQLHANDLER_ADMIN_KEYS_FILE", str(tmp_path / "admin-keys.json"))
    monkeypatch.setenv("MCP_API_KEYS", "some-other-key")
    _admin_keys.add_key(raw, label="designated", created_by="test")
    _policy_with_admins(monkeypatch, tmp_path, [key_fingerprint(raw)])

    # the designated minted key writes through the key path (unchanged)
    assert saved_module.credential_ok(raw) is True
    assert_write_allowed(FakeRequest({"x-api-key": raw}))
    # an UNRECOGNIZED key is not rescued by the admin path either
    with pytest.raises(NotAuthorized):
        assert_write_allowed(FakeRequest({"x-api-key": "not-a-key"})),

    # the helper itself answers the SESSION question only: a bare key header
    # resolves anonymous on /api/* (no middleware recorded the fp), which is
    # why credential_ok must — and does — run first.
    assert saved_module.write_caller_is_admin(FakeRequest({"x-api-key": raw})) is False

def test_gate_admin_path_ignores_a_stale_explicit_key(monkeypatch, tmp_path, _admins_reset):
    """PINNED DIVERGENCE: a designated admin whose request ALSO carries a
    stale/rotated explicit key is admitted by the write gate, while the admin
    surface would 401 that exact shape.

    The admin routes treat an unmatched explicit ``X-API-Key`` as a hard
    refusal ("a wrong key is never redeemed as the browser user behind it").
    This gate never inspects key headers, so it resolves the session instead.
    The difference is deliberate (refusing a legitimate admin over a
    credential they are not required to present would recreate the bug this
    gate exists to fix) and elevates nothing: the caller must still be in the
    policy ``admins`` list. Pinned so a future "make the two gates identical"
    refactor has to face the trade explicitly.
    """
    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    stale = FakeRequest({"x-auth-request-user": "andrew", "x-api-key": "rotated-away"})
    assert saved_module.presented_credential(stale) == "rotated-away"
    assert saved_module.credential_ok("rotated-away") is False, "the key really is unrecognized"
    assert_write_allowed(stale)  # admitted on the ADMIN path, not the key path

    # ...and the same stale key on a NON-admin is still refused: the admin
    # path is what admits, never the stray header.
    mallory = FakeRequest({"x-auth-request-user": "mallory", "x-api-key": "rotated-away"})
    with pytest.raises(NotAuthorized):
        assert_write_allowed(mallory)


def test_gate_admin_path_does_not_require_a_key_header(monkeypatch, tmp_path, _admins_reset):
    """An explicit-but-unrecognized key claim must not shadow the admin's own
    session: behind oauth2-proxy the browser's SSO access token rides in
    Authorization on every request, so a hard refusal on an unmatched Bearer
    would refuse the very session this path exists to admit. Nothing is
    elevated -- the caller still has to be a designated admin."""
    monkeypatch.setenv("SQLHANDLER_TRUST_BROWSER_HEADERS", "1")
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    # ambient SSO bearer (not a key) + trusted browser identity of an admin
    assert_write_allowed(
        FakeRequest({"authorization": "Bearer <ambient-sso-token>", "x-auth-request-user": "andrew"})
    )
    # the same ambient bearer with a NON-admin identity is still refused
    with pytest.raises(NotAuthorized):
        assert_write_allowed(
            FakeRequest({"authorization": "Bearer <ambient-sso-token>", "x-auth-request-user": "mallory"})
        )


def test_gate_message_names_the_designated_admin_path(monkeypatch, tmp_path, _admins_reset):
    """The refusal must describe BOTH admissions honestly: a designated admin
    may write from their resolved session, and a signed-in NON-admin still
    needs a minted key (the SSO-authenticates-but-does-not-authorize sentence
    stays true)."""
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "sekret")
    _policy_with_admins(monkeypatch, tmp_path, ["andrew"])

    with pytest.raises(NotAuthorized) as ei:
        assert_write_allowed(FakeRequest({}))
    msg = str(ei.value)
    assert "designated admin" in msg
    assert "does NOT by itself authorize writes" in msg
    assert "mint a key" in msg
    assert "minted keys are accepted" in msg
    assert "SQLHANDLER_API_TOKEN" in msg


def test_gate_mcp_keys_configured_reach_the_mcp_tools(monkeypatch):
    """MCP keys configured: the /mcp middleware authenticates the transport,
    and the write tool re-verifies per call (defense in depth — covers
    deployments where ONLY the token is set and /mcp stays open)."""
    monkeypatch.setenv("MCP_API_KEYS", "key-1, key-2")
    # MCP over HTTP with the right key: allowed
    ok = FakeRequest({"authorization": "Bearer key-2"})
    entry = saved_module.api_saved_save({"name": "q", "sql": "SELECT 1"}, ok)
    assert entry["name"] == "q"
    # without a credential: refused
    with pytest.raises(NotAuthorized):
        saved_module.api_saved_save({"name": "q2", "sql": "SELECT 1"}, FakeRequest({}))
    with pytest.raises(NotAuthorized):
        saved_module.api_saved_delete("q", FakeRequest({}))
    # with the right key: allowed
    assert saved_module.api_saved_delete("q", ok) == {"deleted": "q"}


def test_rest_saved_queries_auth_matrix(tmp_path, monkeypatch):
    """REST matrix: no auth → open; token configured → 401 without it."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server

    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)

    def build_app(token=None):
        app = mcp_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=_transport_security,
        )
        from sqlhandler.webui import register_ui as reg

        reg(app, lambda: eng)
        if token:
            app.add_middleware(_ApiTokenMiddleware, token=token)
        return TestClient(app)

    # no auth configured: save/delete open (single-user-local)
    client = build_app()
    r = client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"})
    assert r.status_code == 200
    assert r.json()["name"] == "q"
    assert client.get("/api/saved-queries").json()["queries"][0]["name"] == "q"
    run = client.post("/api/saved-queries/q/run", json={})
    assert run.status_code == 200
    assert run.json()["rows"] == [[1]]
    assert client.delete("/api/saved-queries/q").json() == {"deleted": "q"}
    assert client.delete("/api/saved-queries/q").status_code == 404

    # token configured: unauthenticated writes refused; valid token passes
    client = build_app(token="tok-1")
    # Two independent layers refuse an unauthenticated write: the /api token
    # middleware and (for any caller that reaches it) the saved-write gate.
    # Either refusing is the contract; the gate's own message is asserted in
    # the unit tests + the MCP-keys-only REST test below.
    assert client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1"}).status_code == 401
    assert client.delete("/api/saved-queries/whatever").status_code == 401
    auth = {"X-API-Token": "tok-1"}
    assert client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"}, headers=auth).status_code == 200
    assert client.delete("/api/saved-queries/q", headers=auth).status_code == 200
    # reads follow the /api posture (token middleware), not the write gate
    assert client.get("/api/saved-queries").status_code == 401
    assert client.get("/api/saved-queries", headers=auth).status_code == 200


def test_rest_saved_queries_with_mcp_keys_only(tmp_path, monkeypatch):
    """MCP keys (not the /api token) configured: REST writes check the key
    per request — this is the poisoning gate's real teeth, since /api has no
    middleware of its own for MCP keys."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import register_ui

    eng = _make_engine(tmp_path)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    client = TestClient(app)

    monkeypatch.setenv("MCP_API_KEYS", "key-9")
    r = client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"})
    assert r.status_code == 401
    assert "MCP_API_KEYS" in r.json()["error"]
    ok = {"X-API-Key": "key-9"}
    assert client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"}, headers=ok).status_code == 200
    # reads stay open (same posture as /mcp without its own gate here)
    assert client.get("/api/saved-queries").status_code == 200
    assert client.post("/api/saved-queries/q/run", json={}).status_code == 200
    assert client.delete("/api/saved-queries/q", headers=ok).status_code == 200


def test_rest_saved_validation_and_run_errors(tmp_path, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import register_ui

    eng = _make_engine(tmp_path)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    client = TestClient(app)

    assert client.post("/api/saved-queries", json={"name": "q", "sql": "DROP TABLE x"}).status_code == 400
    assert client.post("/api/saved-queries", json={"name": "a/b", "sql": "SELECT 1"}).status_code == 400
    assert client.post("/api/saved-queries", json={"sql": "SELECT 1"}).status_code == 400
    assert client.post("/api/saved-queries/nope/run", json={}).status_code == 404
    bad = client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT $x", "params": {"x": [1]}})
    assert bad.status_code == 400


def test_mcp_over_http_write_gate_end_to_end(tmp_path, monkeypatch):
    """The per-call write gate through the REAL /mcp transport (POST only —
    never GET /mcp, fleet convention).

    Unique coverage here: with ONLY SQLHANDLER_API_TOKEN configured the /mcp
    transport itself stays open (the middleware gates on the MCP keys), so
    the tool-level per-call credential check is the poisoning gate. This
    proves the transport's HTTP request actually reaches the tool dispatch.
    """
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _build_http_app

    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "tok-e2e")
    app = _build_http_app()
    with TestClient(app) as client:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

        def rpc(method, params, rid=1, extra=None):
            h = dict(headers)
            if extra:
                h.update(extra)
            return client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                headers=h,
            )

        init = rpc(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            rid=0,
        )
        assert init.status_code == 200

        def call_tool(name, args, rid, extra=None):
            body = rpc("tools/call", {"name": name, "arguments": args}, rid=rid, extra=extra).json()
            result = body.get("result", {})
            return result.get("isError", False), result["content"][0]["text"]

        err, text = call_tool("query_save", {"name": "q", "sql": "SELECT 1"}, rid=2)
        assert err is True
        assert "SQLHANDLER_API_TOKEN" in text
        err, text = call_tool(
            "query_save",
            {"name": "q", "sql": "SELECT 1 AS one"},
            rid=3,
            extra={"Authorization": "Bearer tok-e2e"},
        )
        assert err is False
        assert json.loads(text)["saved"] is True

# ---------------------------------------------------------------------------
# Store LOCATION: operator content must not silently land on an emptyDir
# ---------------------------------------------------------------------------


def test_saved_queries_path_prefers_the_explicit_env(monkeypatch, tmp_path):
    """An explicit SQLHANDLER_SAVED_QUERIES_PATH always wins."""
    target = tmp_path / "explicit.json"
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(target))
    monkeypatch.setenv("SQLHANDLER_CACHE_DIR", str(tmp_path / "cache"))
    assert saved_module.saved_queries_path() == str(target)


def test_saved_queries_path_falls_back_to_cache_dir(monkeypatch, tmp_path):
    """Unset, the path follows the disk-warm cache dir (the historical default).

    Pinned as a DOCUMENTED FALLBACK, not as the recommended deployment shape:
    the chart points SQLHANDLER_CACHE_DIR at /tmp/sqlhandler-cache (an
    emptyDir), so this fallback is NOT durable and is per-replica. The chart
    sets the env explicitly for that reason; this test keeps the local/stdio
    behaviour from silently changing.
    """
    monkeypatch.delenv("SQLHANDLER_SAVED_QUERIES_PATH", raising=False)
    cache = tmp_path / "cache"
    monkeypatch.setenv("SQLHANDLER_CACHE_DIR", str(cache))
    assert saved_module.saved_queries_path() == str(cache / "sqlhandler-saved-queries.json")


def test_saved_queries_path_last_resort_is_the_temp_dir(monkeypatch):
    """Neither env set -> platform temp dir (never raises, always writable)."""
    monkeypatch.delenv("SQLHANDLER_SAVED_QUERIES_PATH", raising=False)
    monkeypatch.delenv("SQLHANDLER_CACHE_DIR", raising=False)
    expected = str(Path(tempfile.gettempdir()) / "sqlhandler-saved-queries.json")
    assert saved_module.saved_queries_path() == expected


def test_startup_warns_when_saved_queries_are_not_durable(monkeypatch, tmp_path, caplog):
    """The posture is stated at startup, either way.

    A silent emptyDir fallback is how saved queries quietly vanish on the
    next restart. The warning names the resolved path AND the fix.
    """
    monkeypatch.delenv("SQLHANDLER_SAVED_QUERIES_PATH", raising=False)
    monkeypatch.setenv("SQLHANDLER_CACHE_DIR", str(tmp_path / "cache"))

    with caplog.at_level(logging.WARNING):
        server._log_saved_query_durability()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("NOT durable" in m for m in msgs), msgs
    assert any("savedQueriesPath" in m for m in msgs), msgs


def test_startup_confirms_durability_when_a_path_is_configured(monkeypatch, tmp_path, caplog):
    """An explicit path logs INFO, not a warning."""
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "sq.json"))

    with caplog.at_level(logging.INFO):
        server._log_saved_query_durability()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("persist to" in m for m in msgs), msgs
    assert not any("NOT durable" in m for m in msgs), msgs




def test_chart_pvcs_survive_upgrades():
    """EVERY chart-created PVC must carry helm.sh/resource-policy: keep.

    Live incident 2026-10-21: a redeploy recreated the catalog claim (new UID,
    empty filesystem) and destroyed every minted API key in
    admin-keys/admin-keys.json, plus the uploaded catalog and saved queries.
    Callers saw "my key stopped working" with nothing in any log explaining
    it. The policy claim is worse still: losing it silently reverts the
    deployment to the PERMISSIVE default, because a missing policy file means
    "no restrictions".

    Without the annotation Helm deletes a PVC it owns as soon as the resource
    drops out of the rendered set — so a values change must never be able to
    destroy operator content or loosen an ACL. Renders the real chart, so a
    new PVC template added later without the annotation fails here.
    """
    import shutil
    import subprocess

    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm not installed")
    chart = Path(__file__).resolve().parent.parent / "helm"
    if not chart.is_dir():
        pytest.skip("helm chart not present")

    out = subprocess.run(
        [
            helm, "template", "t", str(chart),
            "--set", "semanticCatalog.store.enabled=true",
            "--set", "security.policy.enabled=true",
            "--set", "security.policy.existingConfigMap=sqlhandler-policy",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout

    # Assert on the PARSED objects, not the raw text: a missing "---" between
    # two PVC blocks makes YAML parsers keep only the LAST one (duplicate
    # keys, last-wins) while the raw text still shows both. That exact bug
    # shipped in this chart on 2026-10-21 — `helm lint` and a text-level grep
    # both passed while a fresh install silently created only the policy
    # claim. Parsing is the only check that reflects what reaches the cluster.
    import yaml

    docs = [d for d in yaml.safe_load_all(out) if d]
    pvcs = [d for d in docs if d.get("kind") == "PersistentVolumeClaim"]
    names = sorted(d["metadata"]["name"] for d in pvcs)
    assert len(pvcs) == 2, f"expected 2 PVCs (catalog + policy), got {len(pvcs)}: {names}"
    assert any(n.endswith("-catalog") for n in names), names
    assert any(n.endswith("-policy") for n in names), names

    offenders = [
        d["metadata"]["name"]
        for d in pvcs
        if (d["metadata"].get("annotations") or {}).get("helm.sh/resource-policy") != "keep"
    ]
    assert not offenders, (
        f"PVC(s) {offenders} lack helm.sh/resource-policy: keep — a helm upgrade "
        "can delete the volume and destroy minted keys / the catalog / the live policy"
    )


def test_chart_persists_saved_queries_onto_the_catalog_claim():
    """The chart must SET the env, so saved queries never hit the emptyDir.

    This is the deployment-level half of the fix: with the catalog store
    enabled, the rendered Deployment carries SQLHANDLER_SAVED_QUERIES_PATH
    pointing INSIDE the catalog claim's mount, not at the cache dir.
    Renders the real chart, so a values/template drift fails here.
    """
    import shutil
    import subprocess

    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm not installed")
    chart = Path(__file__).resolve().parent.parent / "helm"
    if not chart.is_dir():
        pytest.skip("helm chart not present")

    out = subprocess.run(
        [helm, "template", "t", str(chart), "--set", "semanticCatalog.store.enabled=true"],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    assert "SQLHANDLER_SAVED_QUERIES_PATH" in out, "chart does not set the saved-query path"
    assert "/var/lib/sqlhandler-catalog/saved-queries.json" in out
    # and it must NOT be the ephemeral cache dir
    assert "sqlhandler-cache/sqlhandler-saved-queries.json" not in out

    # store disabled -> env absent (historical pod-local behaviour preserved)
    out2 = subprocess.run(
        [helm, "template", "t", str(chart)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    assert "SQLHANDLER_SAVED_QUERIES_PATH" not in out2
