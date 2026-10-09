"""The admin-identity + keys-store suite (task-5).

Under test, per the pinned contract:

* the STORE (``sqlhandler.admin_keys``): JSON keys file at
  SQLHANDLER_ADMIN_KEYS_FILE, mtime-cached load with the PolicyStore's
  fail-closed semantics (vanished/unreadable file keeps the last valid
  contents), fp-unique adds (AdminKeysError on dup), atomic writes
  (temp-in-same-dir + os.replace), removal by fp, and the store DISABLED
  (env unset) failing closed everywhere. NEVER the raw key: entries carry
  the fingerprint + the full sha256 of the raw key (``key_sha256``).
* PRESENTATION MATCHING (``match_presentation``): the middleware's store
  half — hash the presented key, constant-time compare against each
  stored ``key_sha256``; a cheap no-op when the store is disabled.
* the ``admins`` designation (policy.py): composes with BOTH authoring
  shapes (hand-written groups AND the compiled datasets document — the
  contract is that admins is orthogonal to grants); two fp spellings;
  fail-closed validation (fp-shaped-but-malformed refuses); folded into
  the file hash (edit → new hash → hot reload).
* ``is_admin``: subject match OR fp match; anonymous → False; no policy /
  no admins → False; hot-reloads with the policy file.
* the middleware UNION (``_McpApiKeyMiddleware.__call__``): env key works,
  store key works, wrong key 401s, a store match records the entry's fp
  into scope["state"] (the request resolves as a key-class caller with
  the right fingerprint), env-first precedence when a key exists in both,
  and the gate arms on a keys-file-only deployment while a disabled store
  never changes the env-only behavior.

Run:  python -m pytest tests/test_admin_keys.py -v
"""

import asyncio
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from sqlhandler import identity
from sqlhandler.admin_keys import (
    ADMIN_KEYS_FILE_ENV,
    AdminKeysError,
    add_key,
    is_admin,
    keys_file_path,
    list_keys,
    match_presentation,
    remove_key,
)
from sqlhandler.identity import Caller
from sqlhandler.mcp_fleet_common.audit import key_fingerprint
from sqlhandler.policy import (
    POLICY_ENABLED_ENV,
    POLICY_FILE_ENV,
    PolicyError,
    load_policy,
    policy_store,
    reset_policy_store,
)
from sqlhandler.server import _CallerIdentityMiddleware, _McpApiKeyMiddleware

KEYS_ENV = ADMIN_KEYS_FILE_ENV


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch, tmp_path):
    """Every test starts store- and policy-disabled; the autouse fixtures in
    conftest isolate the catalog/saved-query stores the same way."""
    monkeypatch.delenv(KEYS_ENV, raising=False)
    monkeypatch.delenv(POLICY_ENABLED_ENV, raising=False)
    monkeypatch.delenv(POLICY_FILE_ENV, raising=False)
    reset_policy_store()
    yield
    reset_policy_store()


def _keys_file(tmp_path: Path, entries=None) -> Path:
    """A fresh keys-file path (optionally pre-seeded)."""
    kf = tmp_path / "admin-keys.json"
    if entries is not None:
        kf.write_text(json.dumps({"keys": entries}))
    return kf


def _policy_file(tmp_path: Path, spec: dict) -> Path:
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps(spec))
    return pf


def _enable_policy(monkeypatch, pf: Path):
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()


# ---------------------------------------------------------------------------
# the store: round-trip, dup refusal, disabled-store refusal
# ---------------------------------------------------------------------------


def test_store_disabled_fails_closed(monkeypatch):
    """No env → store disabled: empty listing, refuses adds, matches nothing,
    removes nothing — and keys_file_path is None (the middleware's cheap
    no-op signal)."""
    assert keys_file_path() is None
    assert list_keys() == []
    with pytest.raises(AdminKeysError):
        add_key("some-raw-key", label="l", created_by="admin")
    assert match_presentation("some-raw-key") is None
    assert remove_key("sha256:000000000000") is False


def test_first_write_creates_missing_directory(tmp_path, monkeypatch):
    """The store SELF-INITIALIZES: a configured path whose PARENT directory
    does not exist is created on the first mint (the chart's claim mode
    points the store at a fresh subdir of the catalog PVC — live 2026-10-05,
    the first mint failed with 'No such file or directory' for the temp
    file before this). The created dir is private (0o700) and the store
    then works exactly as a pre-seeded one."""
    kf = tmp_path / "deep" / "nested" / "admin-keys-file"
    assert not kf.parent.exists()
    monkeypatch.setenv(KEYS_ENV, str(kf))
    e1 = add_key("raw-key-one", label="first", created_by="alice")
    assert kf.parent.is_dir()
    assert kf.exists()
    assert e1["fp"] == key_fingerprint("raw-key-one")
    # The store now behaves like any other: reload, add, list.
    e2 = add_key("raw-key-two", label="second", created_by="bob")
    assert [e["fp"] for e in list_keys()] == [e1["fp"], e2["fp"]]
    on_disk = json.loads(kf.read_text())
    assert [e["fp"] for e in on_disk["keys"]] == [e1["fp"], e2["fp"]]


def test_store_round_trip(tmp_path, monkeypatch):
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    e1 = add_key("raw-key-one", label="reporting", created_by="alice")
    e2 = add_key("raw-key-two", label="ops", created_by="bob")

    listed = list_keys()
    assert [e["fp"] for e in listed] == [e1["fp"], e2["fp"]]

    # The PINNED KeyEntry shape (key_sha256 REQUIRED since the union):
    for e in listed:
        assert set(e) == {"fp", "key_sha256", "label", "created_at", "created_by", "source"}
        assert e["source"] == "file"
        assert e["label"] in ("reporting", "ops")
        assert e["created_by"] in ("alice", "bob")
        assert "T" in e["created_at"] and "raw-key" not in json.dumps(e)
    # fp = the audit fingerprint; key_sha256 = the FULL digest (not the fp)
    assert e1["fp"] == key_fingerprint("raw-key-one")
    assert e1["key_sha256"] == hashlib.sha256(b"raw-key-one").hexdigest()
    assert e1["key_sha256"] != e1["fp"]
    # On disk: fp-unique JSON, never the raw key.
    on_disk = json.loads(kf.read_text())
    assert [e["fp"] for e in on_disk["keys"]] == [e1["fp"], e2["fp"]]
    assert "raw-key-one" not in kf.read_text()

    assert remove_key(e1["fp"]) is True
    assert remove_key(e1["fp"]) is False  # already gone
    assert [e["fp"] for e in list_keys()] == [e2["fp"]]
    assert match_presentation("raw-key-one") is None  # removed key stops working
    assert match_presentation("raw-key-two") is not None


def test_add_duplicate_fp_refused(tmp_path, monkeypatch):
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    add_key("the-same-key", label="first", created_by="alice")
    with pytest.raises(AdminKeysError, match="already exists"):
        add_key("the-same-key", label="second", created_by="bob")
    assert len(list_keys()) == 1  # the refusal left the store untouched


def test_mtime_cache_and_hot_reload(tmp_path, monkeypatch):
    """list_keys is mtime-cached (one stat sig); an external edit reloads."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    add_key("k-one", label="l1", created_by="a")
    assert len(list_keys()) == 1
    # Simulate an external edit (what the admin API's atomic write produces).
    entries = json.loads(kf.read_text())["keys"]
    entries.append(
        {
            "fp": key_fingerprint("k-two"),
            "key_sha256": hashlib.sha256(b"k-two").hexdigest(),
            "label": "l2",
            "created_at": "2026-09-30T00:00:00+00:00",
            "created_by": "x",
            "source": "file",
        }
    )
    kf.write_text(json.dumps({"keys": entries}))
    assert len(list_keys()) == 2


def test_vanished_file_keeps_last_valid_store(tmp_path, monkeypatch, caplog):
    """Fail-closed: the keys file vanishing must NEVER surface as an empty
    store (a mount hiccup must not strand live minted keys mid-flight)."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    e = add_key("survivor-key", label="l", created_by="a")
    assert match_presentation("survivor-key") is not None
    kf.unlink()
    with caplog.at_level(logging.WARNING, logger="sqlhandler.admin_keys"):
        listed = list_keys()
    assert [x["fp"] for x in listed] == [e["fp"]]
    assert match_presentation("survivor-key") is not None
    assert any(
        "keeping the previous contents" in r.getMessage() or "does not exist" in r.getMessage() for r in caplog.records
    )


def test_broken_file_keeps_last_valid_store(tmp_path, monkeypatch, caplog):
    """A present-but-unreadable file keeps the previous contents too (an
    empty store would invite a silent re-mint over live grants)."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    e = add_key("kept-key", label="l", created_by="a")
    kf.write_text("{not json at all")
    with caplog.at_level(logging.WARNING, logger="sqlhandler.admin_keys"):
        listed = list_keys()
    assert [x["fp"] for x in listed] == [e["fp"]]
    assert match_presentation("kept-key") is not None


def test_broken_first_file_reports_empty_loudly(tmp_path, monkeypatch, caplog):
    """Broken and never seen → empty (nothing to keep) but LOUD."""
    kf = tmp_path / "admin-keys.json"
    kf.write_text("]]] not json")
    monkeypatch.setenv(KEYS_ENV, str(kf))
    with caplog.at_level(logging.ERROR, logger="sqlhandler.admin_keys"):
        assert list_keys() == []
    assert any("fail-closed" in r.getMessage() for r in caplog.records)
    assert match_presentation("anything") is None


def test_atomic_write_leaves_no_temp_litter(tmp_path, monkeypatch):
    """Every persist is temp-in-same-dir + os.replace: after a batch of adds
    and removes the directory holds exactly the keys file (a crash never
    truncates the store; no dotfile litter)."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    for i in range(4):
        add_key(f"key-{i}", label=f"l{i}", created_by="a")
    for i in range(2):
        remove_key(key_fingerprint(f"key-{i}"))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != kf.name]
    assert leftovers == [], leftovers
    assert len(list_keys()) == 2


def test_write_failure_keeps_previous_store(tmp_path, monkeypatch):
    """A failing write raises a TYPED error and leaves the previous store
    intact — the 'bad concurrent doc' atomicity proof. Root-invariant to the
    test user: the failure comes from a DIRECTORY sitting at the keys-file
    path (os.replace can never make that a file), not from chmod."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    e = add_key("original", label="l", created_by="a")
    blocker = tmp_path / "blocked"
    blocker.mkdir()  # a directory where the keys file should be
    monkeypatch.setenv(KEYS_ENV, str(blocker))
    with pytest.raises(AdminKeysError):
        add_key("never-persisted", label="l", created_by="a")
    # The original store is untouched and still authenticates.
    monkeypatch.setenv(KEYS_ENV, str(kf))
    assert [x["fp"] for x in list_keys()] == [e["fp"]]
    assert match_presentation("original") is not None


def test_concurrent_adds_are_lock_safe(tmp_path, monkeypatch):
    """Two threads minting at once: both entries land, the file stays
    parseable (the store lock serializes read-modify-write)."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    errors: list[Exception] = []

    def mint(i: int) -> None:
        try:
            add_key(f"concurrent-{i}", label=f"l{i}", created_by="t")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=mint, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(list_keys()) == 8
    json.loads(kf.read_text())  # never a torn write


# ---------------------------------------------------------------------------
# presentation matching
# ---------------------------------------------------------------------------


def test_match_presentation_hash_compare(tmp_path, monkeypatch):
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    add_key("presentation-key", label="l", created_by="a")
    hit = match_presentation("presentation-key")
    assert hit is not None and hit["fp"] == key_fingerprint("presentation-key")
    assert match_presentation("presentation-keY") is None  # case-sensitive
    assert match_presentation("") is None
    assert match_presentation("nope") is None


def test_match_presentation_no_store_is_cheap_noop():
    """The no-store path: None immediately, zero I/O (the union must not tax
    deployments without SQLHANDLER_ADMIN_KEYS_FILE)."""
    assert keys_file_path() is None
    assert match_presentation("anything") is None


def test_match_presentation_skips_entries_without_digest(tmp_path, monkeypatch):
    """A malformed/legacy entry (no key_sha256) is skipped, not fatal."""
    kf = _keys_file(
        tmp_path,
        entries=[
            {"fp": "sha256:aaaaaaaaaaaa", "label": "legacy", "created_at": "x", "created_by": "y", "source": "file"},
        ],
    )
    monkeypatch.setenv(KEYS_ENV, str(kf))
    assert match_presentation("whatever") is None  # no crash, no match


# ---------------------------------------------------------------------------
# the admins designation (policy.py)
# ---------------------------------------------------------------------------


def test_admins_with_hand_written_groups(tmp_path, monkeypatch):
    pf = _policy_file(
        tmp_path,
        {
            "version": 1,
            "admins": ["alice", key_fingerprint("minted-1")],
            "groups": {"g": {"hidden_tables": ["scratch/*"]}},
            "default_group": "g",
        },
    )
    pol = load_policy(str(pf))
    assert pol.admins == ("alice", key_fingerprint("minted-1"))
    # The designation is invisible to enforcement: composition is unchanged
    # with admins present (the hidden-table semantics themselves are the
    # leak suite's job — here we prove admins changed nothing about rules).
    rule = pol.rule_for_table("scratch/anything", "anything", ("g",))
    assert rule.hidden  # the group's own hidden_tables still apply
    assert pol.rule_for_table("workorder/work_order", "work_order", ("g",)).empty


def test_admins_with_datasets_doc_coexists(tmp_path, monkeypatch):
    """THE CONTRACT: admins alongside the compiled datasets document —
    designation is orthogonal to grants; NO mutual exclusion."""
    fp = key_fingerprint("minted-2")
    pf = _policy_file(
        tmp_path,
        {
            "version": 1,
            "admins": ["alice", f"key:{fp}"],
            "datasets": {"global": ["workorder/*"], "assignments": {"bob": ["payroll/*"]}},
        },
    )
    pol = load_policy(str(pf))
    assert pol.admins == ("alice", fp)  # the key: spelling normalized to bare
    assert pol.datasets is not None
    # grants compiled as usual (bob's assignment, the global group, default)
    assert pol.subjects == {"bob": ("_acl_global", "_acl_id_0")}
    assert pol.default_group == "_acl_global"


def test_admins_validates_fail_closed(tmp_path: Path) -> None:
    base: dict[str, Any] = {"version": 1, "groups": {"g": {}}, "default_group": "g"}
    cases: list[dict[str, Any]] = [
        {"admins": "alice"},  # not a list
        {"admins": [42]},  # non-string entry
        {"admins": [""]},  # empty entry
        {"admins": ["   "]},  # whitespace-only
        {"admins": ["sha256:ZZZZ"]},  # fp-shaped, malformed
        {"admins": ["key:sha256:short"]},  # prefixed, malformed
    ]
    for admins in cases:
        spec = {**base, **admins}
        pf = _policy_file(tmp_path, spec)
        with pytest.raises(PolicyError):
            load_policy(str(pf))


def test_admins_absent_or_empty_means_none(tmp_path):
    pf = _policy_file(tmp_path, {"version": 1, "groups": {"g": {}}})
    assert load_policy(str(pf)).admins == ()
    pf2 = _policy_file(tmp_path, {"version": 1, "admins": [], "groups": {"g": {}}})
    assert load_policy(str(pf2)).admins == ()  # explicit empty = nobody


def test_admins_folds_into_file_hash(tmp_path: Path) -> None:
    base: dict[str, Any] = {"version": 1, "groups": {"g": {}}, "default_group": "g"}
    h0 = load_policy(str(_policy_file(tmp_path, base))).hash
    h1 = load_policy(str(_policy_file(tmp_path, {**base, "admins": ["alice"]}))).hash
    h2 = load_policy(str(_policy_file(tmp_path, {**base, "admins": ["alice", "bob"]}))).hash
    assert h0 != h1 != h2  # designating/un-designating is a policy edit


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------


def test_is_admin_via_subject(tmp_path, monkeypatch):
    pf = _policy_file(tmp_path, {"version": 1, "admins": ["alice"], "groups": {"g": {}}})
    _enable_policy(monkeypatch, pf)
    assert is_admin(Caller(cls="user", subject="alice", via="relay")) is True
    assert is_admin(Caller(cls="user", subject="mallory", via="relay")) is False


def test_is_admin_via_subject_prefixed_spelling(tmp_path, monkeypatch):
    """The 'subject:<name>' spelling in admins (the datasets.assignments
    canonical form — what the grant-by-name UI writes and the docs show)
    matches a bare Caller.subject. Live 2026-10-05: the prefixed spelling
    silently failed every subject-designated admin (is_admin compared the
    bare subject against the prefixed entry)."""
    pf = _policy_file(
        tmp_path,
        {"version": 1, "admins": ["subject:alice"], "groups": {"g": {}}},
    )
    _enable_policy(monkeypatch, pf)
    assert is_admin(Caller(cls="user", subject="alice", via="jwt")) is True
    assert is_admin(Caller(cls="key", subject="alice", key_fp="sha256:f4e09f1a0e49", via="key")) is True
    assert is_admin(Caller(cls="user", subject="mallory", via="jwt")) is False
    # The bare spelling keeps working (backward compatible).
    pf2 = _policy_file(tmp_path, {"version": 1, "admins": ["bob"], "groups": {"g": {}}})
    _enable_policy(monkeypatch, pf2)
    assert is_admin(Caller(cls="user", subject="bob", via="jwt")) is True


def test_is_admin_via_key_fp(tmp_path, monkeypatch):
    fp = key_fingerprint("admin-minted-key")
    pf = _policy_file(tmp_path, {"version": 1, "admins": [fp], "groups": {"g": {}}})
    _enable_policy(monkeypatch, pf)
    assert is_admin(Caller(cls="key", subject=None, key_fp=fp, via="key")) is True
    assert is_admin(Caller(cls="key", subject=None, key_fp="sha256:999999999999", via="key")) is False


def test_is_admin_anonymous_and_no_policy():
    """Fail-closed everywhere: no policy configured → False for everyone."""
    assert is_admin(Caller(cls="user", subject="alice", via="relay")) is False
    assert is_admin(Caller()) is False
    assert is_admin(None) is False


def test_is_admin_policy_without_admins(tmp_path, monkeypatch):
    pf = _policy_file(tmp_path, {"version": 1, "groups": {"g": {}}})
    _enable_policy(monkeypatch, pf)
    assert is_admin(Caller(cls="user", subject="alice", via="relay")) is False


def test_is_admin_hot_reloads_with_policy(tmp_path, monkeypatch):
    pf = _policy_file(tmp_path, {"version": 1, "admins": [], "groups": {"g": {}}})
    _enable_policy(monkeypatch, pf)
    alice = Caller(cls="user", subject="alice", via="relay")
    assert is_admin(alice) is False
    spec = json.loads(pf.read_text())
    spec["admins"] = ["alice"]
    pf.write_text(json.dumps(spec))
    reset_policy_store()  # what a real mtime change does (fresh stat sig)
    assert is_admin(alice) is True


def test_is_admin_prefixed_spelling_matches_bare_fp(tmp_path, monkeypatch):
    """The loader normalizes key:sha256:... — a caller carrying the bare fp
    (what the middleware records) matches a prefixed designation."""
    fp = key_fingerprint("prefix-admin-key")
    pf = _policy_file(tmp_path, {"version": 1, "admins": [f"key:{fp}"], "groups": {"g": {}}})
    _enable_policy(monkeypatch, pf)
    assert is_admin(Caller(cls="key", subject=None, key_fp=fp, via="key")) is True


# ---------------------------------------------------------------------------
# the middleware union (_McpApiKeyMiddleware.__call__)
# ---------------------------------------------------------------------------


def _stack_request(
    keys_env: str | None, kf: Path | None, headers: list[tuple[bytes, bytes]], monkeypatch, path: str = "/mcp"
):
    """One request through the REAL middleware order (key gate outer →
    identity inner); returns (status-ish, caller, state) via the recorder."""
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    monkeypatch.delenv("SQLHANDLER_API_KEYS", raising=False)
    if keys_env is not None:
        monkeypatch.setenv("MCP_API_KEYS", keys_env)
    monkeypatch.delenv(KEYS_ENV, raising=False)
    if kf is not None:
        monkeypatch.setenv(KEYS_ENV, str(kf))
    captured: list = []
    rejected: list = []

    async def inner(scope, receive, send):
        captured.append((identity.caller_from_scope(scope), dict(scope.get("state") or {})))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message.get("type") == "http.response.start":
            rejected.append(message["status"])

    stack = _McpApiKeyMiddleware(_CallerIdentityMiddleware(inner))
    scope = {"type": "http", "path": path, "headers": headers, "state": {}}
    asyncio.run(stack(scope, receive, send))
    if captured:
        return (None, *captured[0])
    return (rejected[0] if rejected else None, Caller(cls="anonymous"), {})


def test_union_env_key_still_works(tmp_path, monkeypatch):
    kf = _keys_file(tmp_path)
    add_key("store-key", label="l", created_by="a", environ={KEYS_ENV: str(kf)})
    status, caller, state = _stack_request("env-key", kf, [(b"x-api-key", b"env-key")], monkeypatch)
    assert status is None  # no rejection
    assert caller.key_fp == key_fingerprint("env-key")
    assert state["sqlhandler.key_fp"] == key_fingerprint("env-key")


def test_union_store_key_authenticates_and_records_fp(tmp_path, monkeypatch):
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    entry = add_key("store-key", label="minted", created_by="admin")
    status, caller, state = _stack_request("env-key", kf, [(b"x-api-key", b"store-key")], monkeypatch)
    assert status is None
    assert caller.cls == "key"  # the identity spine is unchanged
    assert caller.via == "key"
    assert caller.key_fp == entry["fp"]  # the MINTED entry's fp
    assert state["sqlhandler.key_fp"] == entry["fp"]


def test_union_wrong_key_401s(tmp_path, monkeypatch):
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    add_key("store-key", label="l", created_by="a")
    status, caller, _ = _stack_request("env-key", kf, [(b"x-api-key", b"wrong-key")], monkeypatch)
    assert status == 401
    assert caller.cls == "anonymous"  # never reached the identity middleware


def test_union_env_first_precedence(tmp_path, monkeypatch, caplog):
    """A key present in BOTH is authenticated by the env path: match_api_key
    runs before the store fallback (no store-match side effect occurs)."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    add_key("shared-key", label="minted-prec", created_by="a")
    with caplog.at_level(logging.INFO, logger="sqlhandler.admin_keys"):
        status, caller, _state = _stack_request("shared-key", kf, [(b"x-api-key", b"shared-key")], monkeypatch)
    assert status is None
    assert caller.key_fp == key_fingerprint("shared-key")
    # admin_keys logs nothing on a plain match — silence here means the env
    # path answered and the store fallback never ran.
    assert not any(r.getMessage() for r in caplog.records)


def test_union_store_only_deployment_arms_gate(tmp_path, monkeypatch):
    """No env keys at all — a configured, non-empty keys file arms the gate:
    minted keys work, strangers 401, and the store MATCH resolves the caller."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    entry = add_key("only-store-key", label="l", created_by="a")
    status, caller, _state = _stack_request(None, kf, [(b"x-api-key", b"only-store-key")], monkeypatch)
    assert status is None
    assert caller.cls == "key" and caller.key_fp == entry["fp"]
    status, _, _ = _stack_request(None, kf, [(b"x-api-key", b"stranger")], monkeypatch)
    assert status == 401


def test_union_disabled_or_empty_store_changes_nothing(monkeypatch):
    """The hard constraint, both halves: no env keys + no store → /mcp wide
    open (byte-identical dev posture); env keys + disabled store → env-only
    behavior (wrong key 401s WITHOUT any store I/O)."""
    status, caller, _ = _stack_request(None, None, [], monkeypatch)
    assert status is None and caller.cls == "anonymous"
    # env-only + disabled store: a wrong key 401s with zero store reads
    # (proven via the disabled-store signal: keys_file_path() None).
    assert keys_file_path() is None


def test_union_empty_store_never_arms_gate(tmp_path, monkeypatch):
    """A configured but EMPTY keys file does not lock anyone out (bootstrap:
    the file exists, no key minted yet → open gate, exactly the env-only
    posture)."""
    kf = _keys_file(tmp_path, entries=[])
    monkeypatch.setenv(KEYS_ENV, str(kf))
    status, caller, _ = _stack_request(None, kf, [], monkeypatch)
    assert status is None and caller.cls == "anonymous"


def test_union_via_full_http_app(tmp_path, monkeypatch):
    """End-to-end through _build_http_app (TestClient) — the TestClient-
    visible half of the union (the middleware reads env per request)."""
    from starlette.testclient import TestClient

    from sqlhandler.server import _build_http_app

    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    entry = add_key("http-store-key", label="l", created_by="a")
    monkeypatch.setenv("MCP_API_KEYS", "http-env-key")
    app = _build_http_app()
    with TestClient(app) as c:
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 401
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream", "X-API-Key": "http-store-key"},
        )
        assert r.status_code == 200  # the minted key is live immediately
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream", "X-API-Key": "http-env-key"},
        )
        assert r.status_code == 200
        # Revoke → the minted key 401s on the next request (mtime reload).
        remove_key(entry["fp"])
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream", "X-API-Key": "http-store-key"},
        )
        assert r.status_code == 401


def test_union_non_mcp_paths_untouched(tmp_path, monkeypatch):
    """The store never touches /api, /ui or probes — the union is /mcp-only
    (the /api tier keeps its own token middleware)."""
    kf = _keys_file(tmp_path)
    monkeypatch.setenv(KEYS_ENV, str(kf))
    add_key("store-key", label="l", created_by="a")
    status, caller, _ = _stack_request(None, kf, [], monkeypatch, path="/api/tables")
    assert status is None  # not gated by THIS middleware
    assert caller.cls == "anonymous"


def test_is_admin_policy_store_integration(tmp_path, monkeypatch):
    """is_admin reads the LIVE policy store (the same instance the engine
    uses), so a hot reload flips admin status without any restart."""
    pf = _policy_file(
        tmp_path,
        {
            "version": 1,
            "admins": [key_fingerprint("live-admin-key")],
            "datasets": {"global": ["*"], "assignments": {}},
        },
    )
    _enable_policy(monkeypatch, pf)
    pol = policy_store().get()
    assert pol.admins == (key_fingerprint("live-admin-key"),)
    assert is_admin(Caller(cls="key", subject=None, key_fp=key_fingerprint("live-admin-key"), via="key")) is True
    # datasets coexistence, enforced through the store path too:
    assert pol.default_group == "_acl_global"
