"""THE DATASET-ACL TEST SUITE (visible_tables + the datasets document).

The dataset-ACL vocabulary adds an ALLOW side to the deny-biased group
machinery without changing any enforcement path: ``rule_for_table`` stays
the single composition point, so allow-listed callers flow through the same
hidden-table semantics everywhere (list/describe/search/profile/scan/query
all refuse or omit — the policy_leaks suite holds the cross-surface proof
for hidden=True; here we prove the ALLOW side and the compilation).

Invariants under test:

* ``visible_tables`` in a resolved group ⇒ default-DENY: only matching
  tables exist for that caller; non-matching behave exactly like
  hidden_tables (omitted from list/search, describe raises LakehouseError,
  direct SQL serves the empty relation — never an error).
* An explicit ``hidden_tables`` hit beats any ``visible_tables`` grant
  (deny-bias over the allow side too).
* ``row_filter`` + ``column_masks`` compose with ``visible_tables`` in one
  group (partial access on one table).
* The ``datasets`` document compiles to the EXISTING group machinery
  (global/assignments/blocked → generated groups + bindings +
  default_group) and is MUTUALLY EXCLUSIVE with hand-written ``groups``;
  compilation is fail-closed (malformed fp, non-list/empty globs,
  duplicate subjects → PolicyError — the file refuses, never half-loads).
* Unknown identities fall to ``_acl_global`` (global-only view); a REAL
  key fingerprint (via ``mcp_fleet_common.audit.key_fingerprint``) binds
  by its bare ``sha256:<12hex>`` form — and the self-documenting
  ``key:sha256:<12hex>`` spelling compiles to the same binding.
* Hash discipline: different assignment sets ⇒ different per-caller
  effective hashes (identical sets share); a file edit flips the hash and
  PolicyStore hot-reloads; a visible_tables-bearing group NEVER hashes to
  "" (it is restrictive — sharing the unmasked cache-key space would be a
  leak).

Run:  python -m pytest tests/test_acl_policy.py tests/test_policy_leaks.py -v
"""

import json
import time
from pathlib import Path

import pytest

from sqlhandler.engine import SqlEngine
from sqlhandler.identity import Caller
from sqlhandler.mcp_fleet_common.audit import key_fingerprint
from sqlhandler.policy import (
    POLICY_ENABLED_ENV,
    POLICY_FILE_ENV,
    PolicyError,
    _parse_text,
    canonical_hash,
    dump_doc,
    load_policy,
    reset_policy_store,
)
from sqlhandler.provider import DataProvider, LakehouseError, TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="payroll", schema="payroll", format="parquet"),
]


class PolicyProvider(DataProvider):
    """FakeProvider with two ACL-able tables (the leak suite's provider + payroll)."""

    kind = "fake"

    def __init__(self, root: Path):
        self.root = root
        self.versions: dict[str, int] = {}

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        import pyarrow.dataset as pad

        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return self.versions.get(info.path)


def _seed(root: Path, rows: int = 3) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    for schema, name in (("workorder", "work_order"), ("payroll", "payroll")):
        d = root / schema / name
        d.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "id": pa.array(range(rows), type=pa.int64()),
                    "ssn": [f"ssn-{i:03d}" for i in range(rows)],
                    "amount": [float(10 * (i + 1)) for i in range(rows)],
                    "kind": ["a" if i % 2 == 0 else "secret" for i in range(rows)],
                }
            ),
            d / "part.parquet",
        )


def _make_engine(tmp_path: Path, **kw) -> SqlEngine:
    root = tmp_path / "data"
    _seed(root)
    kw.setdefault("cache_ttl", 3600)
    kw.setdefault("dataset_cache_ttl", 0)  # datasets stay out of the equation
    return SqlEngine(PolicyProvider(root), **kw)


def _caller(subject=None, key=None):
    return Caller(
        cls="user" if subject else "key",
        subject=subject,
        key_fp=key_fingerprint(key) if key else None,
        via="relay" if subject else "key",
    )


ALICE = Caller(cls="user", subject="alice", key_fp=None, via="relay")
FP_KEY = "acl-test-key-123"  # the raw key; its fingerprint binds in the policy
BOB_FP_CALLER = _caller(key=FP_KEY)
CAROL = Caller(cls="key", subject=None, key_fp="sha256:cccccccccccc", via="key")


def _write_policy(tmp_path: Path, spec: dict) -> Path:
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps(spec))
    return pf


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    """Enforcement ON + a hand-written visible_tables policy: alice is bound
    to a group that may see workorder/* only; carol's fp is bound to an
    allow-everything group (unmasked by the allow-list's own terms)."""
    pf = _write_policy(
        tmp_path,
        {
            "version": 1,
            "default_group": "all_disabled",
            "groups": {
                "wo_only": {
                    "visible_tables": ["workorder/*"],
                    "tables": {
                        "workorder/work_order": {
                            "row_filter": "kind != 'secret'",
                            "column_masks": {"ssn": "redact"},
                        }
                    },
                },
                "everything": {"visible_tables": ["workorder/*", "payroll/*"]},
                "all_disabled": {},
            },
            "subjects": {"alice": ["wo_only"]},
            "key_fps": {"sha256:cccccccccccc": ["everything"]},
        },
    )
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    yield pf
    reset_policy_store()


@pytest.fixture()
def datasets_env(tmp_path, monkeypatch):
    """Enforcement ON + a datasets document (no hand-written groups). The
    fp-bound assignment uses a REAL fingerprint computed from a raw key."""
    pf = _write_policy(
        tmp_path,
        {
            "version": 1,
            "datasets": {
                "global": ["workorder/*"],
                "assignments": {
                    "alice": ["payroll/*"],
                    key_fingerprint(FP_KEY): ["workorder/*", "payroll/*"],
                },
                "blocked": ["scratch/*"],
            },
        },
    )
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    yield pf
    reset_policy_store()


@pytest.fixture()
def no_policy(monkeypatch):
    monkeypatch.delenv(POLICY_ENABLED_ENV, raising=False)
    monkeypatch.delenv(POLICY_FILE_ENV, raising=False)
    reset_policy_store()
    yield
    reset_policy_store()


SQL_WO = "SELECT id, ssn, amount, kind FROM work_order ORDER BY id"
SQL_PAYROLL = "SELECT id, ssn, amount, kind FROM payroll ORDER BY id"


# ---------------------------------------------------------------------------
# visible_tables: the allow side of hidden_tables
# ---------------------------------------------------------------------------


def test_visible_tables_list_describe_search(tmp_path, policy_env):
    """A caller in a visible_tables group sees ONLY matching tables: list
    omits the rest, describe refuses with the same not-found error hidden
    tables get, search finds nothing, and the allowed table is fully there."""
    eng = _make_engine(tmp_path)
    listed = [t.name for t in eng.list_tables(caller=ALICE)]
    assert listed == ["work_order"], "default-deny: only allow-listed tables listed"
    listed_carol = sorted(t.name for t in eng.list_tables(caller=CAROL))
    assert listed_carol == ["payroll", "work_order"], "allow-everything group sees both"

    with pytest.raises(LakehouseError, match="not found|does not exist"):
        eng.describe_table("payroll", caller=ALICE)
    d = eng.describe_table("work_order", caller=ALICE)
    # The masked column is omitted from describe (the policy_leaks suite's
    # shape contract); the allowed table itself is fully describable.
    cols = [c["name"] for c in d["columns"]]
    assert "id" in cols and "amount" in cols and "ssn" not in cols

    assert eng.search_tables("payroll", caller=ALICE) == []
    assert len(eng.search_tables("work order", caller=ALICE)) == 1


def test_visible_nonmatching_table_query_empty_not_error(tmp_path, policy_env):
    """A caller who GUESSES a non-allowed table's name gets the empty typed
    relation (the no-existence-leak design) — rows never, error never."""
    eng = _make_engine(tmp_path)
    out = eng.query_duckdb(SQL_PAYROLL, caller=ALICE)
    assert out.num_rows == 0, "non-allowed table must serve empty rows, not an error"
    # The allowed table still returns its full (policy-masked) data.
    out_wo = eng.query_duckdb(SQL_WO, caller=ALICE)
    assert out_wo.num_rows == 2  # row_filter drops the 'secret' row
    assert out_wo.column("ssn").to_pylist() == ["***", "***"]


def test_hidden_tables_beats_visible_tables(tmp_path, policy_env):
    """Deny-bias over the allow side: an explicit hidden_tables hit hides a
    table even when visible_tables globs would allow it."""
    pf = policy_env
    spec = json.loads(pf.read_text())
    spec["groups"]["wo_only"]["hidden_tables"] = ["workorder/work_order"]
    spec["groups"]["wo_only"]["visible_tables"] = ["workorder/*"]
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    assert [t.name for t in eng.list_tables(caller=ALICE)] == [], "explicit hidden beats visible"
    with pytest.raises(LakehouseError, match="not found|does not exist"):
        eng.describe_table("work_order", caller=ALICE)
    # And even a direct SQL reference serves the empty relation.
    assert eng.query_duckdb(SQL_WO, caller=ALICE).num_rows == 0


def test_visible_composes_with_row_filter_and_masks(tmp_path, policy_env):
    """Partial access in ONE group: visible_tables grants the table, a
    row_filter drops rows, column_masks redact a column — all three apply."""
    eng = _make_engine(tmp_path)
    out = eng.query_duckdb(SQL_WO, caller=ALICE)
    assert out.num_rows == 2, "row_filter applies"
    assert out.column("ssn").to_pylist() == ["***", "***"], "mask applies"
    assert out.column("amount").to_pylist() == [10.0, 30.0], "unmasked columns pass through"
    # profile runs over the masking view: mask + filter both visible there.
    p = eng.profile_table("work_order", caller=ALICE)
    cols = {c["name"]: c for c in p["columns"]}
    assert cols["ssn"]["min"] == "***"
    assert p["profiled_rows"] == 2


def test_no_visible_tables_anywhere_byte_identical_posture(tmp_path, policy_env, no_policy):
    """A group WITHOUT visible_tables in a file that contains none anywhere
    keeps today's deny-list posture: a group with no rules at all leaves the
    caller unmasked and everything visible."""
    pf = policy_env
    spec = json.loads(pf.read_text())
    spec["groups"]["wo_only"].pop("visible_tables")
    spec["groups"]["everything"].pop("visible_tables")
    del spec["subjects"]["alice"]
    spec["key_fps"]["sha256:cccccccccccc"] = ["all_disabled"]  # no rules at all
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    assert sorted(t.name for t in eng.list_tables(caller=CAROL)) == ["payroll", "work_order"]
    out = eng.query_duckdb(SQL_WO, caller=CAROL)
    assert out.num_rows == 3, "no ACL vocabulary in play = raw data path"
    assert out.column("ssn").to_pylist() == ["ssn-000", "ssn-001", "ssn-002"]


def test_bare_name_and_case_insensitive_glob_semantics(tmp_path, policy_env):
    """Matching semantics are IDENTICAL to hidden_tables: globs hit both the
    ``schema/name`` path and the bare table name (and the case-insensitive
    retry inside _glob_match applies too)."""
    pf = policy_env
    spec = json.loads(pf.read_text())
    spec["groups"]["wo_only"]["visible_tables"] = ["work_order"]  # bare name only
    spec["groups"]["everything"]["visible_tables"] = ["WORKORDER/*"]  # case-insensitive hit
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    assert [t.name for t in eng.list_tables(caller=ALICE)] == ["work_order"], "bare-name glob hits"
    assert [t.name for t in eng.list_tables(caller=CAROL)] == ["work_order"], "case-insensitive glob hits"


# ---------------------------------------------------------------------------
# the datasets document
# ---------------------------------------------------------------------------


def test_datasets_global_plus_assignment_union(tmp_path, datasets_env):
    """A bound identity sees the UNION of the global globs and their own
    assignment's globs (compiled as two groups, one resolution)."""
    eng = _make_engine(tmp_path)
    # alice: assignment payroll/* ∪ global workorder/* = both tables.
    assert sorted(t.name for t in eng.list_tables(caller=ALICE)) == ["payroll", "work_order"]
    assert eng.query_duckdb(SQL_PAYROLL, caller=ALICE).num_rows == 3
    assert eng.query_duckdb(SQL_WO, caller=ALICE).num_rows == 3


def test_datasets_global_only_for_unknown_identity(tmp_path, datasets_env):
    """An unbound key resolves to default_group=_acl_global: global-only
    view. Failing open beyond the global globs would be a compilation bug."""
    eng = _make_engine(tmp_path)
    dave = Caller(cls="key", subject=None, key_fp="sha256:dddddddddddd", via="key")
    assert [t.name for t in eng.list_tables(caller=dave)] == ["work_order"]
    assert eng.query_duckdb(SQL_PAYROLL, caller=dave).num_rows == 0, "beyond global = hidden (empty relation)"
    assert eng.query_duckdb(SQL_WO, caller=dave).num_rows == 3


def test_datasets_key_fp_binding_real_fingerprint(tmp_path, datasets_env):
    """A REAL fingerprint (computed via mcp_fleet_common.audit.key_fingerprint
    from a raw key) binds in the assignments map by its bare form and gets
    exactly its assignment's globs over the global base."""
    eng = _make_engine(tmp_path)
    out = eng.query_duckdb(SQL_PAYROLL, caller=BOB_FP_CALLER)
    assert out.num_rows == 3, "fp-bound caller sees their assignment + global"
    assert eng.query_duckdb(SQL_WO, caller=BOB_FP_CALLER).num_rows == 3
    # An unbound fp stays on the global-only floor.
    dave = Caller(cls="key", subject=None, key_fp="sha256:eeeeeeeeeeee", via="key")
    assert eng.query_duckdb(SQL_PAYROLL, caller=dave).num_rows == 0


def test_datasets_blocked_wins_over_grants(tmp_path, datasets_env, monkeypatch):
    """blocked wins over EVERY grant: blocked-in-blocked-list but matching
    global means hidden — and over assignments too (blocked beats the union)."""
    pf = datasets_env
    spec = json.loads(pf.read_text())
    spec["datasets"]["blocked"] = ["workorder/work_order"]  # granted by global
    spec["datasets"]["assignments"]["alice"] = ["workorder/*"]  # granted by assignment too
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    assert [t.name for t in eng.list_tables(caller=ALICE)] == [], "blocked beats global+assignment"
    assert eng.query_duckdb(SQL_WO, caller=ALICE).num_rows == 0
    assert eng.query_duckdb(SQL_PAYROLL, caller=ALICE).num_rows == 0, "no grant left for alice"
    # CAROL is unbound here: her ONLY grant is the global one, and blocked
    # takes it away — nothing left to list.
    assert [t.name for t in eng.list_tables(caller=CAROL)] == []
    # The fp-bound caller still keeps payroll (blocked names only work_order):
    assert sorted(t.name for t in eng.list_tables(caller=BOB_FP_CALLER)) == ["payroll"]


def test_datasets_blocked_globs_match_paths_and_names(tmp_path, datasets_env):
    pf = datasets_env
    spec = json.loads(pf.read_text())
    spec["datasets"]["blocked"] = ["payroll"]  # bare-name spelling blocks the whole schema
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    assert sorted(t.name for t in eng.list_tables(caller=ALICE)) == ["work_order"], "bare-name blocked glob hits"


def test_datasets_doc_hash_and_log_counts(tmp_path, datasets_env, caplog):
    """PolicyStore.get() logs the datasets counts when the doc is present,
    and the compiled groups/bindings are exactly the contract's shapes."""
    import logging

    from sqlhandler.policy import policy_store

    _make_engine(tmp_path)
    pol = policy_store().get()
    assert pol.datasets is not None
    assert pol.default_group == "_acl_global"
    assert "_acl_global" in pol.groups
    assert pol.subjects["alice"] == ("_acl_global", "_acl_id_0")
    with caplog.at_level(logging.INFO, logger="sqlhandler.policy"):
        spec = json.loads(datasets_env.read_text())
        spec["datasets"]["assignments"].pop(key_fingerprint(FP_KEY))  # 2 -> 1 assignment
        datasets_env.write_text(json.dumps(spec))
        reset_policy_store()
        policy_store().get()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "datasets: global=1 blocked=1 assignments=1" in joined


def test_datasets_prefixed_key_spelling_compiles_same(tmp_path, monkeypatch):
    """The self-documenting ``key:sha256:<12hex>`` assignment spelling and
    the bare ``sha256:<12hex>`` form compile to the SAME binding (the minted
    bare form and the DECISIONS-documented prefixed form coexist)."""
    fp = key_fingerprint("prefix-spelling-key")
    pf = _write_policy(
        tmp_path,
        {
            "datasets": {
                "global": ["workorder/*"],
                "assignments": {f"key:{fp}": ["payroll/*"]},
            }
        },
    )
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    pol = load_policy(str(pf))
    assert pol.key_fps == {fp: ("_acl_global", "_acl_id_0")}
    assert pol.subjects == {}
    eng = _make_engine(tmp_path)
    caller = _caller(key="prefix-spelling-key")
    assert sorted(t.name for t in eng.list_tables(caller=caller)) == ["payroll", "work_order"]


def test_datasets_assignment_globs_validate_against_nothing(tmp_path, datasets_env):
    """Assignment globs are pure pattern vocabulary (no schema validation —
    tables may not exist yet); empty/non-list values are the refusals."""
    pf = datasets_env
    spec = json.loads(pf.read_text())
    spec["datasets"]["assignments"]["alice"] = ["  "]  # whitespace-only glob
    pf.write_text(json.dumps(spec))
    with pytest.raises(PolicyError, match="non-string or empty glob"):
        load_policy(str(pf))


# ---------------------------------------------------------------------------
# mutual exclusion + fail-closed validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"groups": {"g": {}}},
        {"subjects": {"alice": ["g"]}},
        {"key_fps": {"sha256:cccccccccccc": ["g"]}},
        {"default_group": "g"},
    ],
)
def test_datasets_plus_groups_is_policy_error(tmp_path, extra):
    """datasets + ANY hand-written identity vocabulary = load-time refusal."""
    pf = _write_policy(tmp_path, {"datasets": {"global": ["a/*"]}, **extra})
    with pytest.raises(PolicyError, match="mutually exclusive"):
        load_policy(str(pf))


def test_datasets_alone_compiles(tmp_path):
    """datasets alone (with only the global floor) compiles and enforces."""
    pf = _write_policy(tmp_path, {"datasets": {"global": ["workorder/*"]}})
    pol = load_policy(str(pf))
    assert pol.default_group == "_acl_global"
    assert pol.groups["_acl_global"] == {"visible_tables": ["workorder/*"]}
    assert pol.subjects == {} and pol.key_fps == {}


def test_datasets_empty_assignments_ok(tmp_path):
    """assignments/blocked are optional keys; absent = not compiled."""
    pf = _write_policy(tmp_path, {"datasets": {"global": ["a/*"]}})
    pol = load_policy(str(pf))
    assert pol.groups == {"_acl_global": {"visible_tables": ["a/*"]}}
    assert pol.groups["_acl_global"].get("hidden_tables") is None
    assert pol.subjects == {} and pol.key_fps == {}


def test_datasets_malformed_fp_refused(tmp_path):
    """Anything fp-SHAPED that fails the real fingerprint format is a
    refusal (fail-closed): wrong length, non-hex, uppercase hex."""
    fp_ok = key_fingerprint("k")
    for bad in [
        "sha256:zzzzzzzzzzzz",
        "sha256:abcdef01234",  # 11 hex
        "sha256:abcdef0123456",  # 13 hex
        "sha256:ABCDEF012345",  # uppercase
        "sha256:",  # no hex at all
        "key:sha256:abcdefghijkm",  # prefixed + non-hex (m excluded)
    ]:
        pf = _write_policy(tmp_path, {"datasets": {"global": ["a/*"], "assignments": {bad: ["b/*"]}}})
        with pytest.raises(PolicyError, match="sha256"):
            load_policy(str(pf))
    # And the valid forms accept cleanly.
    for good in [fp_ok, f"key:{fp_ok}"]:
        pf = _write_policy(tmp_path, {"datasets": {"global": ["a/*"], "assignments": {good: ["b/*"]}}})
        pol = load_policy(str(pf))
        assert fp_ok in pol.key_fps


def test_datasets_non_list_and_empty_values_refused(tmp_path):
    for bad_doc in [
        {"global": "a/*", "blocked": ["x"]},
        {"global": [], "blocked": ["x"]},
        {"global": ["a/*"], "blocked": []},
        {"global": ["a/*"], "blocked": ""},
        {"global": ["a/*"], "assignments": {"alice": "b/*"}},
        {"global": ["a/*"], "assignments": {"alice": []}},
        {"global": ["a/*"], "assignments": {"alice": [1]}},
        {"global": ["a/*"], "assignments": {"": ["b/*"]}},
    ]:
        pf = _write_policy(tmp_path, {"datasets": bad_doc})
        with pytest.raises(PolicyError):
            load_policy(str(pf))


def test_datasets_duplicate_subject_refused(tmp_path):
    """Two assignment keys that normalize to the same subject (here: the
    same bare spelling via whitespace) refuse the file."""
    pf = _write_policy(
        tmp_path,
        {"datasets": {"global": ["a/*"], "assignments": {" alice": ["b/*"], "alice": ["c/*"]}}},
    )
    with pytest.raises(PolicyError, match="twice"):
        load_policy(str(pf))


def test_datasets_duplicate_fp_two_spellings_refused(tmp_path):
    """The same fp bound via both spellings with DIFFERENT grants is a
    refusal (one fingerprint, one binding)."""
    fp = key_fingerprint("k")
    pf = _write_policy(
        tmp_path,
        {"datasets": {"global": ["a/*"], "assignments": {fp: ["b/*"], f"key:{fp}": ["c/*"]}}},
    )
    with pytest.raises(PolicyError, match="twice|one fingerprint"):
        load_policy(str(pf))


def test_datasets_duplicate_fp_same_globs_still_refused(tmp_path):
    """Even identical grants: one fingerprint bound under two spellings is a
    config error worth a human look, so it refuses (fail-closed)."""
    fp = key_fingerprint("k")
    pf = _write_policy(
        tmp_path,
        {"datasets": {"global": ["a/*"], "assignments": {fp: ["b/*"], f"key:{fp}": ["b/*"]}}},
    )
    with pytest.raises(PolicyError, match="twice|one fingerprint"):
        load_policy(str(pf))


def test_missing_or_unreadable_file_stays_as_today(tmp_path, monkeypatch):
    """No datasets-specific change to the file-level failure semantics: a
    missing/unreadable file is the historical PolicyError."""
    from sqlhandler.policy import policy_store

    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(tmp_path / "nope.json"))
    reset_policy_store()
    pol = policy_store().get()
    assert pol.groups == {}, "missing file = empty policy (no enforcement configured)"
    pf = _write_policy(tmp_path, {"groups": {"g": {}}})
    pf.chmod(0o000)
    try:
        with pytest.raises(PolicyError, match="could not read"):
            load_policy(str(pf))
    finally:
        pf.chmod(0o644)


# ---------------------------------------------------------------------------
# hash discipline
# ---------------------------------------------------------------------------


def test_effective_hash_differs_across_assignments(tmp_path, datasets_env):
    """Different assignment sets ⇒ different per-caller hashes; the same set
    (reloaded) ⇒ the same hash (identically-restricted callers share cache
    entries — the canonical_hash contract)."""
    pol = load_policy(str(datasets_env))
    h_alice = pol.effective_hash("alice", None)
    h_carol = pol.effective_hash(None, "sha256:cccccccccccc")
    assert h_alice and h_carol and h_alice != h_carol, "different assignments hash differently"
    # Same assignments, same hash: rewrite the file with identical content.
    before = pol.effective_hash("alice", None)
    pf = datasets_env
    pf.write_text(json.dumps(json.loads(pf.read_text())))
    pol2 = load_policy(str(pf))
    assert pol2.effective_hash("alice", None) == before, "identical assignment sets hash identically"


def test_effective_hash_changes_when_assignment_added(tmp_path, datasets_env):
    pf = datasets_env
    pol = load_policy(str(pf))
    h_before = pol.effective_hash("alice", None)
    spec = json.loads(pf.read_text())
    spec["datasets"]["assignments"]["alice"] = ["payroll/*", "audit/*"]
    pf.write_text(json.dumps(spec))
    time.sleep(0.01)  # mtime resolution
    pol2 = load_policy(str(pf))
    assert pol.hash != pol2.hash, "the file hash folds the datasets doc"
    assert pol2.effective_hash("alice", None) != h_before, "assignment edit ⇒ new caller hash"


def test_policy_store_hot_reload_on_mtime(tmp_path, datasets_env):
    """The store hot-reloads on mtime bump: get() returns a policy with the
    NEW hash after an edit adds an assignment (cache entries keyed by the
    old hash age out without any purge)."""
    eng = _make_engine(tmp_path)
    h1 = eng._policy_hash_for(ALICE) if hasattr(eng, "_policy_hash_for") else None
    pol_before = load_policy(str(datasets_env))
    spec = json.loads(datasets_env.read_text())
    spec["datasets"]["assignments"]["bob"] = ["payroll/*"]
    datasets_env.write_text(json.dumps(spec))
    time.sleep(0.01)
    from sqlhandler.policy import policy_store

    pol_after = policy_store().get()
    assert pol_after.hash != pol_before.hash
    assert "bob" in pol_after.subjects
    if h1 is not None:
        assert h1 != pol_after.effective_hash(ALICE.subject, ALICE.key_fp)


def test_visible_tables_group_never_hashes_empty(tmp_path):
    """THE leak-class guard: a visible_tables-bearing group is restrictive —
    _compose_empty_check must NOT count it as "no rules", else the caller
    hashes to "" and shares the UNMASKED cache-key space."""
    pf = _write_policy(tmp_path, {"groups": {"g": {"visible_tables": ["a/*"]}}, "default_group": "g"})
    pol = load_policy(str(pf))
    assert pol.effective_hash(None, None) != "", "restrictive group must produce a hash"
    assert pol._compose_empty_check(("g",)) is False
    # A genuinely rule-less group still hashes "" (the historical posture).
    pf2 = _write_policy(tmp_path, {"groups": {"g": {}}, "default_group": "g"})
    pol2 = load_policy(str(pf2))
    assert pol2.effective_hash(None, None) == ""


def test_file_hash_folds_datasets_document(tmp_path):
    """canonical_hash folding: the datasets doc is in the file hash payload
    (any edit ⇒ new hash even when the compiled groups don't change — they
    do here, but the fold is what guarantees it in general)."""
    doc_a = {"datasets": {"global": ["a/*"]}}
    doc_b = {"datasets": {"global": ["a/*", "b/*"]}}
    pa_ = _write_policy(tmp_path, doc_a)
    pol_a = load_policy(str(pa_))
    pb = tmp_path / "b.json"
    pb.write_text(json.dumps(doc_b))
    pol_b = load_policy(str(pb))
    assert pol_a.hash != pol_b.hash


def test_canonicalize_group_includes_visible_tables():
    """_canonicalize_group carries visible_tables so per-caller hashes see
    allow-list edits (the group spec is what effective_hash folds)."""
    from sqlhandler.policy import _canonicalize_group

    spec = {"visible_tables": ["b/*", "a/*"], "hidden_tables": ["z/*"]}
    out = _canonicalize_group(spec)
    assert out["visible_tables"] == ["a/*", "b/*"], "sorted, present"
    assert out["hidden_tables"] == ["z/*"]
    assert _canonicalize_group({})["visible_tables"] == []


def test_effective_hash_off_vocabulary_unchanged(tmp_path):
    """A policy with NEITHER datasets nor visible_tables hashes exactly as
    before the ACL vocabulary existed (byte-identical backward compat)."""
    pf = _write_policy(
        tmp_path,
        {
            "groups": {
                "g": {
                    "tables": {"t/*": {"column_masks": {"c": "redact"}}},
                    "hidden_tables": ["h/*"],
                }
            },
            "default_group": "g",
        },
    )
    pol = load_policy(str(pf))
    assert pol.datasets is None
    eff = {"groups": {"g": _canonicalize_group_from(pol, "g")}}
    assert pol.effective_hash(None, None) == canonical_hash(eff), "hand-computed historical shape"


def _canonicalize_group_from(pol, g):
    from sqlhandler.policy import _canonicalize_group

    return _canonicalize_group(pol.groups[g])


# ---------------------------------------------------------------------------
# backward compat: no datasets + no visible_tables anywhere
# ---------------------------------------------------------------------------


def test_handwritten_groups_suite_shape_still_enforces(tmp_path, policy_env, monkeypatch):
    """groups alone (the pre-ACL file shape) enforces exactly as before:
    masking, row filter, and the historical effective-hash shape."""
    pf = policy_env
    spec = json.loads(pf.read_text())
    spec["groups"] = {
        "analysts": {
            "tables": {"workorder/work_order": {"row_filter": "kind != 'secret'", "column_masks": {"ssn": "redact"}}}
        }
    }
    spec["default_group"] = "analysts"
    spec.pop("subjects", None)
    spec.pop("key_fps", None)
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    out = eng.query_duckdb(SQL_WO, caller=ALICE)
    assert out.num_rows == 2 and out.column("ssn").to_pylist() == ["***", "***"]
    assert eng.query_duckdb(SQL_PAYROLL, caller=ALICE).num_rows == 3, "no hidden_tables/visible_tables = no hiding"
    pol = load_policy(str(pf))
    assert pol.datasets is None


def test_no_policy_at_all_untouched(tmp_path, no_policy):
    """Enforcement off ⇒ empty hash and raw data (the golden contract)."""
    eng = _make_engine(tmp_path)
    assert eng._policy_hash() == ""
    out = eng.query_duckdb(SQL_WO, caller=ALICE)
    assert out.num_rows == 3
    assert out.column("ssn").to_pylist() == ["ssn-000", "ssn-001", "ssn-002"]


# ---------------------------------------------------------------------------
# the authored mirror + dump_doc (the grants view's editable policy_text)
# ---------------------------------------------------------------------------


def test_load_policy_authored_mirror_datasets_form(tmp_path):
    """Policy.authored is the WHOLE parsed document (datasets doc AND the
    top-level admins list) — the mirror the admin UI's editor prefills
    from; hash-neutral, so it must not disturb the compiled fields."""
    import yaml

    doc = {
        "datasets": {"global": ["workorder/*"], "assignments": {"alice": ["payroll/*"]}},
        "admins": ["alice"],
    }
    pf = _write_policy(tmp_path, doc)
    pol = load_policy(str(pf))
    assert pol.authored == doc
    assert pol.admins == ("alice",)
    assert pol.datasets == doc["datasets"]
    assert pol.default_group == "_acl_global"
    assert yaml.safe_load(dump_doc(pol.authored)) == doc


def test_load_policy_authored_mirror_groups_form(tmp_path):
    """The hand-written groups form mirrors too (the historic prefill was
    datasets-only, which emptied the editor and invited wiping the file)."""
    doc = {
        "groups": {"g": {"tables": {"t/*": {"column_masks": {"c": "redact"}}}}},
        "default_group": "g",
        "admins": ["sha256:aaaaaaaaaaaa"],
    }
    pf = _write_policy(tmp_path, doc)
    pol = load_policy(str(pf))
    assert pol.authored == doc
    assert pol.datasets is None
    assert pol.admins == ("sha256:aaaaaaaaaaaa",)


def test_dump_doc_yaml_then_parse_roundtrip():
    """dump_doc is the inverse of _parse_text: YAML default, JSON opt-in,
    operator key order kept, non-ASCII intact, and whatever dump_doc emits
    parses back to the same document."""
    import yaml

    doc = {
        "datasets": {"global": ["workorder/*", "résumé-*"], "assignments": {"zoe": ["reports/*"]}},
        "admins": ["zoe", "sha256:0123456789ab"],
    }
    text = dump_doc(doc)
    assert text.endswith("\n")
    assert list(yaml.safe_load(text)) == list(doc), "operator key order kept"
    assert "résumé-*" in text, "allow_unicode — no ?? escapes"
    assert _parse_text(text, "t") == doc
    js = dump_doc(doc, fmt="json")
    assert json.loads(js) == doc
    with pytest.raises(ValueError):
        dump_doc(doc, fmt="xml")


def test_dump_doc_json_fallback_without_pyyaml(monkeypatch):
    """No pyyaml → JSON text that _parse_text still accepts (the editor
    must always show text the server can parse back)."""

    doc = {"datasets": {"global": ["a/*"]}}
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _no_yaml(name, *a, **kw):
        if name == "yaml":
            raise ImportError("no yaml for you")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _no_yaml)
    text = dump_doc(doc)
    assert json.loads(text) == doc
    assert _parse_text(text, "t") == doc
