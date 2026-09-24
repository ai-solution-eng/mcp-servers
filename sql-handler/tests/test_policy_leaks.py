"""THE MASKING-LEAK TEST SUITE (policy-as-code, the deliverable that matters).

The invariant under test: **a masked caller's results must NEVER reach — or
be served from — a cache entry or artifact keyed without the policy hash**,
on EVERY surface that caches or shares:

* L1 result cache (memory) + L2 (shared disk) — same SQL, different callers
  ⇒ distinct keys AND distinct results (the cross-caller leak test).
* Virtual materializations (PVC parquet) — masked callers get their OWN
  artifact (``-p<hash8>-`` infix); unmasked callers keep today's names.
* Describe cache — masked shape (masked columns omitted) never served to an
  unmasked caller and vice versa.
* Profile cache — stats over the masking view, keyed per policy hash.
* Query memory + saved-query store — owner-scoped under enforcement.
* Hidden tables — invisible on list/search/describe/profile/scan/query.
* Transitive masking — a virtual definition over a masked base returns
  masked rows (the composition path).
* scan_arrow delegation — covered tables refuse pyarrow filters and serve
  from the masking view.
* Policy hot-reload — a file change flips the hash, so old entries age out
  (no purge needed; distinct keys).
* Enforcement OFF — byte-identical keys everywhere (the golden contract).

Run:  python -m pytest tests/test_policy_leaks.py tests/test_policy.py -v
"""

import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import SqlEngine
from sqlhandler.identity import Caller
from sqlhandler.policy import (
    POLICY_ENABLED_ENV,
    POLICY_FILE_ENV,
    PolicyError,
    build_mask_select,
    canonical_hash,
    load_policy,
    owner_key,
    reset_policy_store,
    validate_row_filter,
)
from sqlhandler.provider import TableInfo

TABLES = [TableInfo(name="work_order", schema="workorder", format="parquet")]


class PolicyProvider:
    """FakeProvider with policy-relevant columns (ssn = the leak canary)."""

    kind = "fake"

    def __init__(self, root: Path):
        self.root = root
        self.versions: dict[str, int] = {}

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return self.versions.get(info.path)


def _seed(root: Path, rows: int = 3) -> None:
    d = root / "workorder" / "work_order"
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


ALICE = Caller(cls="user", subject="alice", key_fp="sha256:aaaaaaaaaaaa", via="relay")
BOB = Caller(cls="key", subject=None, key_fp="sha256:bbbbbbbbbbbb", via="key")
CAROL_UNMASKED = Caller(cls="key", subject=None, key_fp="sha256:cccccccccccc", via="key")


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    """Enforcement ON + a policy file: alice masked+filtered (restricted
    group), carol bound to a group with NO rules (genuinely unmasked), and
    the default group (unbound keys) masked+filtered like alice's rules."""
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "analysts",
                "groups": {
                    "analysts": {
                        "tables": {
                            "workorder/work_order": {
                                "row_filter": "kind != 'secret'",
                                "column_masks": {"ssn": "redact"},
                            }
                        }
                    },
                    "restricted": {
                        "tables": {"workorder/work_order": {"column_masks": {"ssn": "redact"}}},
                        "hidden_tables": [],
                    },
                    "unrestricted": {},
                },
                "subjects": {"alice": ["analysts"]},
                "key_fps": {"sha256:cccccccccccc": ["unrestricted"]},
            }
        )
    )
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


SQL_ALL = "SELECT id, ssn, amount, kind FROM work_order ORDER BY id"


# ---------------------------------------------------------------------------
# THE core leak tests: L1/L2 result cache
# ---------------------------------------------------------------------------


def test_masked_and_unmasked_callers_never_share_results(tmp_path, policy_env, monkeypatch):
    """THE cross-caller leak test (review gotcha #1, expressed at the RESULT
    level): same SQL, masked vs unmasked caller ⇒ distinct keys AND distinct
    results on BOTH cache layers. Run order is adversarial: unmasked FIRST
    (warms the cache), masked SECOND (must not hit it)."""
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    eng = _make_engine(tmp_path)

    unmasked = eng.query_duckdb(SQL_ALL, caller=CAROL_UNMASKED)
    masked = eng.query_duckdb(SQL_ALL, caller=ALICE)
    # Distinct keys: bind each caller and recompute.
    from sqlhandler.engine import reset_current_caller, set_current_caller

    t = set_current_caller(CAROL_UNMASKED)
    key_unmasked = eng._result_cache_key(SQL_ALL, None, None, None, None)
    reset_current_caller(t)
    t = set_current_caller(ALICE)
    key_masked = eng._result_cache_key(SQL_ALL, None, None, None, None)
    reset_current_caller(t)
    assert key_masked != key_unmasked, "masked and unmasked cache keys must differ"

    # Distinct results: alice's group (analysts) redacts ssn + filters the
    # secret row; carol (fp-bound to the empty group) sees the raw rows.
    assert unmasked.num_rows == 3
    assert sorted(unmasked.column("ssn").to_pylist()) == ["ssn-000", "ssn-001", "ssn-002"]
    assert masked.num_rows == 2, "row_filter must drop the 'secret' row"
    assert masked.column("ssn").to_pylist() == ["***", "***"], "masked ssn must be redacted"
    # The masked caller re-running the SAME SQL must get the MASKED result
    # back from the cache (their own entry), never the unmasked one.
    again = eng.query_duckdb(SQL_ALL, caller=ALICE)
    assert again.to_pylist() == masked.to_pylist()


def test_l2_shared_disk_never_leaks_across_callers(tmp_path, policy_env, monkeypatch):
    """L2 (shared PVC): a masked caller's entry and an unmasked caller's
    entry are different ARTIFACTS — one replica's masked result can never
    serve another caller. Exercises the exact disk path."""
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    l2dir = tmp_path / "l2"
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(l2dir))
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "0")  # force small results into L2
    from sqlhandler.l2cache import L2ResultCache

    eng = _make_engine(tmp_path)
    eng._l2_cache = L2ResultCache(str(l2dir), ttl=3600.0)

    unmasked = eng.query_duckdb(SQL_ALL, caller=CAROL_UNMASKED)
    masked = eng.query_duckdb(SQL_ALL, caller=ALICE)
    assert unmasked.num_rows == 3 and masked.num_rows == 2
    files = sorted(p.name for p in l2dir.glob("*.parquet"))
    assert len(files) >= 2, "masked and unmasked results must be distinct L2 artifacts"
    # Fresh engine (a "second replica") must serve the SAME masked result
    # from L2 — proving the artifact on disk IS the masked one.
    eng2 = _make_engine(tmp_path / "replica2")
    eng2._l2_cache = L2ResultCache(str(l2dir), ttl=3600.0)
    served = eng2.query_duckdb(SQL_ALL, caller=ALICE)
    assert served.to_pylist() == masked.to_pylist()
    served_unmasked = eng2.query_duckdb(SQL_ALL, caller=CAROL_UNMASKED)
    assert served_unmasked.to_pylist() == unmasked.to_pylist()


def test_enforcement_off_keys_byte_identical(tmp_path, no_policy):
    """The golden contract: enforcement OFF ⇒ the key is byte-identical to
    the historical format even with the identity spine fully wired."""
    eng = _make_engine(tmp_path)
    from sqlhandler.engine import _normalize_cache_sql

    expected_parts = [
        _normalize_cache_sql(SQL_ALL),
        repr(None),
        repr(None),
        repr(None),
        repr(None),
        "default/workorder/work_order=None",
    ]
    import hashlib

    expected = hashlib.sha256("\x1f".join(expected_parts).encode()).hexdigest()
    assert eng._result_cache_key(SQL_ALL, None, None, None, None) == expected
    assert eng._policy_hash() == ""


# ---------------------------------------------------------------------------
# describe / profile caches
# ---------------------------------------------------------------------------


def test_describe_masked_shape_per_caller_not_shared(tmp_path, policy_env):
    """Describe cache: the masked caller's shape (ssn omitted... no — masked
    columns are OMITTED) is a DIFFERENT cache entry from the unmasked one."""
    eng = _make_engine(tmp_path)
    d_full = eng.describe_table("work_order", caller=CAROL_UNMASKED)
    d_masked = eng.describe_table("work_order", caller=ALICE)
    cols_full = [c["name"] for c in d_full["columns"]]
    cols_masked = [c["name"] for c in d_masked["columns"]]
    assert "ssn" in cols_full
    assert "ssn" not in cols_masked, "masked column must be omitted from describe"
    assert "amount" in cols_masked and "id" in cols_masked
    # Cache hits: re-calling returns the SAME per-caller shape (not the other's).
    assert [c["name"] for c in eng.describe_table("work_order", caller=ALICE)["columns"]] == cols_masked
    assert [c["name"] for c in eng.describe_table("work_order", caller=CAROL_UNMASKED)["columns"]] == cols_full
    stats = eng.cache_stats()
    assert stats["describe_cached_tables"] >= 2, "two distinct describe entries"


def test_profile_over_masked_view(tmp_path, policy_env):
    """Profile: masked stats describe the MASKED data — ssn's min/max are
    the mask constant, the row filter dropped the secret row, and the cache
    keys differ per caller."""
    eng = _make_engine(tmp_path)
    p_unmasked = eng.profile_table("work_order", caller=CAROL_UNMASKED)
    p_masked = eng.profile_table("work_order", caller=ALICE)
    cols_u = {c["name"]: c for c in p_unmasked["columns"]}
    cols_m = {c["name"]: c for c in p_masked["columns"]}
    assert cols_u["ssn"]["min"] == "ssn-000"
    assert cols_m["ssn"]["min"] == "***", "profile must see the mask, never the raw value"
    assert p_masked["profiled_rows"] == 2, "row filter applies to the profile"
    assert p_unmasked["profiled_rows"] == 3
    # Cache-key separation: both entries coexist.
    assert eng.cache_stats()["profile_cached_tables"] >= 2


def test_profile_masked_column_refusal(tmp_path, policy_env):
    """column_stats on a masked column is refused HONESTLY (its top-values
    list would be a frequency oracle over the raw column)."""
    eng = _make_engine(tmp_path)
    from sqlhandler.provider import LakehouseError

    with pytest.raises(LakehouseError, match="masked|does not exist"):
        eng.column_stats("work_order", "ssn", caller=ALICE)
    # Unmasked caller still gets stats.
    s = eng.column_stats("work_order", "ssn", caller=CAROL_UNMASKED)
    assert s["min"] == "ssn-000"


# ---------------------------------------------------------------------------
# hidden tables
# ---------------------------------------------------------------------------


def test_hidden_table_invisible_everywhere(tmp_path, policy_env, monkeypatch):
    """A hidden table is INVISIBLE: not listed, not searchable, describe/
    profile/scan/query all treat it as absent — for the hidden caller only."""
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    pf = policy_env
    spec = json.loads(pf.read_text())
    spec["groups"]["analysts"]["hidden_tables"] = ["workorder/*"]
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)

    listed_u = [t.name for t in eng.list_tables(caller=CAROL_UNMASKED)]
    listed_a = [t.name for t in eng.list_tables(caller=ALICE)]
    assert "work_order" in listed_u
    assert "work_order" not in listed_a, "hidden table must not be listed"

    assert eng.search_tables("work order", caller=ALICE) == []
    assert len(eng.search_tables("work order", caller=CAROL_UNMASKED)) == 1

    from sqlhandler.provider import LakehouseError

    with pytest.raises(LakehouseError, match="not found|does not exist"):
        eng.describe_table("work_order", caller=ALICE)
    with pytest.raises(LakehouseError, match="not found|does not exist"):
        eng.profile_table("work_order", caller=ALICE)
    with pytest.raises(LakehouseError, match="not found|does not exist"):
        eng.scan_arrow("work_order", caller=ALICE)
    # Direct SQL naming the hidden table serves the EMPTY relation (the
    # deliberate no-existence-leak design — see test_hidden_query_...).
    assert eng.query_duckdb(SQL_ALL, caller=ALICE).num_rows == 0
    # The unmasked caller's view of everything is intact.
    assert eng.query_duckdb(SQL_ALL, caller=CAROL_UNMASKED).num_rows == 3


def test_hidden_query_returns_empty_not_error_leak(tmp_path, policy_env):
    """A caller who GUESSES a hidden table's name gets an empty relation —
    never an error naming the table, and never rows."""
    pf = policy_env
    spec = json.loads(pf.read_text())
    spec["groups"]["analysts"]["hidden_tables"] = ["workorder/work_order"]
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    eng = _make_engine(tmp_path)
    out = eng.query_duckdb("SELECT id, ssn, amount, kind FROM work_order", caller=ALICE)
    assert out.num_rows == 0


# ---------------------------------------------------------------------------
# transitive masking over virtual tables + materialization separation
# ---------------------------------------------------------------------------


class VirtualProvider(PolicyProvider):
    """PolicyProvider + one virtual table over the masked base."""

    def list_tables(self):
        return TABLES + [
            TableInfo(name="vw_orders", schema="workorder", format="virtual"),
        ]


def _make_virtual_engine(tmp_path: Path, catalog: Path, monkeypatch, **kw) -> SqlEngine:
    root = tmp_path / "data"
    _seed(root)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(catalog))
    eng = SqlEngine(VirtualProvider(root), cache_ttl=3600, dataset_cache_ttl=0, **kw)
    return eng


def test_transitive_masking_through_virtual(tmp_path, policy_env, monkeypatch):
    """A virtual definition over a masked base returns MASKED rows — the
    composition path (virtual views read the registered base views)."""
    cat = tmp_path / "catalog.json"
    cat.write_text(
        json.dumps(
            {
                "tables": {
                    "vw_orders": {
                        "definition": "SELECT id, ssn, amount, kind FROM work_order",
                    }
                }
            }
        )
    )
    eng = _make_virtual_engine(tmp_path, cat, monkeypatch)
    out_unmasked = eng.query_duckdb("SELECT id, ssn, amount FROM vw_orders ORDER BY id", caller=CAROL_UNMASKED)
    out_masked = eng.query_duckdb("SELECT id, ssn, amount FROM vw_orders ORDER BY id", caller=ALICE)
    assert out_unmasked.num_rows == 3
    assert out_unmasked.column("ssn").to_pylist() == ["ssn-000", "ssn-001", "ssn-002"]
    assert out_masked.num_rows == 2, "the base's row filter binds through the virtual"
    assert out_masked.column("ssn").to_pylist() == ["***", "***"], "mask composes transitively"


def test_virtual_materialization_artifacts_separated(tmp_path, policy_env, monkeypatch):
    """The materialization cache: masked callers get a ``-p<hash8>-`` artifact;
    unmasked callers keep today's exact name shape. The two artifacts coexist
    and each caller reads their own."""
    monkeypatch.setenv("SQLHANDLER_VIRTUAL_CACHE_TTL", "3600")
    monkeypatch.setenv("SQLHANDLER_VIRTUAL_CACHE_DIR", str(tmp_path / "vcache"))
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps({"tables": {"vw_orders": {"definition": "SELECT id, ssn, amount FROM work_order"}}}))
    eng = _make_virtual_engine(tmp_path, cat, monkeypatch)
    vdir = Path(eng._virtual_cache_dir)
    eng.query_duckdb("SELECT id, amount FROM vw_orders", caller=CAROL_UNMASKED)
    unmasked_files = [p.name for p in vdir.glob("*.parquet") if "-p" not in p.name]
    assert unmasked_files, "unmasked materialization keeps the historical name"
    eng.query_duckdb("SELECT id, amount FROM vw_orders", caller=ALICE)
    masked_files = [p.name for p in vdir.glob("*.parquet") if "-p" in p.name]
    assert masked_files, "masked materialization gets its own -p<hash8> artifact"
    # And the masked artifact really holds MASKED data:
    import pyarrow.parquet as pq

    tbl = pq.read_table(vdir / masked_files[0])
    assert set(tbl.column("ssn").to_pylist()) == {"***"}


# ---------------------------------------------------------------------------
# scan_arrow delegation
# ---------------------------------------------------------------------------


def test_scan_arrow_covered_table_delegates_and_masks(tmp_path, policy_env):
    """scan_arrow over a covered table: served through the masking view
    (masked ssn, filtered rows) and pyarrow filters REFUSED (they cannot
    express the policy — the _scan_virtual posture)."""
    eng = _make_engine(tmp_path)
    out = eng.scan_arrow("work_order", caller=ALICE)
    assert out.num_rows == 2
    assert out.column("ssn").to_pylist() == ["***", "***"]
    import pyarrow.compute as pc

    with pytest.raises(Exception, match="policy-covered"):
        eng.scan_arrow("work_order", filters=[pc.field("id") > 0], caller=ALICE)
    # The UNCOVERED caller keeps the raw pyarrow path (filters still work).
    out_u = eng.scan_arrow("work_order", filters=[pc.field("id") > 0], caller=CAROL_UNMASKED)
    assert out_u.num_rows == 2


# ---------------------------------------------------------------------------
# policy hot-reload
# ---------------------------------------------------------------------------


def test_hot_reload_changes_hash_ages_old_entries_out(tmp_path, policy_env, monkeypatch):
    """Editing the policy file changes the effective hash ⇒ the SAME caller's
    next query computes a NEW key; the old entry (old mask) is never served."""
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    pf = policy_env
    eng = _make_engine(tmp_path)
    before = eng.query_duckdb(SQL_ALL, caller=ALICE)
    assert set(before.column("ssn").to_pylist()) == {"***"}
    from sqlhandler.engine import reset_current_caller, set_current_caller

    t = set_current_caller(ALICE)
    h_before = eng._policy_hash()
    reset_current_caller(t)

    # Tighten the policy: redact becomes a const marker (a DIFFERENT mask).
    spec = json.loads(pf.read_text())
    spec["groups"]["analysts"]["tables"]["workorder/work_order"]["column_masks"]["ssn"] = "[REDACTED]"
    pf.write_text(json.dumps(spec))
    reset_policy_store()
    time.sleep(0.01)  # mtime resolution

    after = eng.query_duckdb(SQL_ALL, caller=ALICE)
    t = set_current_caller(ALICE)
    h_after = eng._policy_hash()
    reset_current_caller(t)
    assert h_before != h_after, "policy edit must change the effective hash"
    assert after.column("ssn").to_pylist() != before.column("ssn").to_pylist(), (
        "new rule served, not the cached old one"
    )
    assert set(after.column("ssn").to_pylist()) == {"[REDACTED]"}, "new mask applied"


# ---------------------------------------------------------------------------
# query memory + saved queries owner scoping
# ---------------------------------------------------------------------------


def test_query_memory_owner_scoped_under_enforcement(tmp_path, policy_env):
    """Under enforcement, the query-memory resource serves each caller their
    OWN entries — Bob's SQL (which may name hidden tables) never reaches
    Alice's resource read. Enforcement off keeps it shared."""
    eng = _make_engine(tmp_path)
    eng.query_duckdb("SELECT count(*) FROM work_order", caller=BOB)
    eng.query_duckdb("SELECT id FROM work_order", caller=ALICE)
    mem_alice = eng.query_memory(caller=ALICE)
    assert [e["sql"] for e in mem_alice] == ["SELECT id FROM work_order"]
    mem_bob = eng.query_memory(caller=BOB)
    assert [e["sql"] for e in mem_bob] == ["SELECT count(*) FROM work_order"]


def test_query_memory_shared_when_enforcement_off(tmp_path, no_policy):
    """Enforcement OFF: the shared history is byte-identical (no owner key)."""
    eng = _make_engine(tmp_path)
    eng.query_duckdb("SELECT count(*) FROM work_order", caller=BOB)
    eng.query_duckdb("SELECT id FROM work_order", caller=ALICE)
    mem = eng.query_memory(caller=ALICE)
    assert len(mem) == 2, "shared history when enforcement is off"
    assert all("owner" not in e for e in mem), "no owner field in the shared mode"


def test_saved_queries_owner_scoped(tmp_path, policy_env, monkeypatch):
    """Saved queries: owner-scoped under enforcement (Bob cannot run or even
    LIST Alice's saved SQL; the shared shape when off is tested elsewhere)."""
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved.json"))
    from sqlhandler.saved import SavedQueryStore

    store = SavedQueryStore(path=str(tmp_path / "saved.json"))
    store.save("alice_q", "SELECT id FROM work_order", caller=ALICE)
    store.save("bob_q", "SELECT count(*) FROM work_order", caller=BOB)
    assert [e["name"] for e in store.list(caller=ALICE)] == ["alice_q"]
    assert [e["name"] for e in store.list(caller=BOB)] == ["bob_q"]
    assert store.get("alice_q", caller=BOB) is None, "another owner's entry is invisible"
    assert store.get("alice_q", caller=ALICE) is not None
    from sqlhandler.saved import NotAuthorized

    with pytest.raises(NotAuthorized):
        store.delete("alice_q", caller=BOB)
    assert store.delete("alice_q", caller=ALICE) is True


# ---------------------------------------------------------------------------
# policy module unit tests (validation, hash, masking SQL)
# ---------------------------------------------------------------------------


def test_row_filter_validation_refuses_bogus_column():
    from sqlhandler.policy import PolicyError

    with pytest.raises(PolicyError, match="bind"):
        validate_row_filter("bogus_col > 1", ["id", "amount"], "t")


def test_row_filter_validation_accepts_real_column():
    assert validate_row_filter("amount > 1 AND id IS NOT NULL", ["id", "amount"], "t") == (
        "amount > 1 AND id IS NOT NULL"
    )


def test_effective_hash_stable_and_distinct():
    a = canonical_hash({"a": 1, "b": [1, 2]})
    b = canonical_hash({"b": [1, 2], "a": 1})
    assert a == b, "canonical: key order must not matter"
    c = canonical_hash({"a": 1, "b": [1, 3]})
    assert a != c


def test_owner_key_uses_subject_then_fp(tmp_path, no_policy):
    assert owner_key(ALICE) == "subject:alice"
    assert owner_key(BOB) == "key:sha256:bbbbbbbbbbbb"
    assert owner_key(None) == "anonymous"


def test_mask_sql_redact_hash_const():
    sql = build_mask_select("base", ["id", "ssn", "amount"], {"ssn": "redact", "amount": "sha256:6"}, None)
    assert "'***' AS \"ssn\"" in sql
    assert 'substring(sha256("amount"), 1, 6)' in sql
    sql2 = build_mask_select("base", ["id"], {"id": "0"}, None)
    assert "'0' AS \"id\"" in sql2


def test_broken_policy_file_fails_closed(tmp_path, policy_env, monkeypatch):
    """A BROKEN edit keeps the previous policy enforcing (never unmasks); a
    broken FIRST file means NO policy (loud) — never a half-applied file."""
    eng = _make_engine(tmp_path)
    masked = eng.query_duckdb(SQL_ALL, caller=ALICE)
    assert masked.column("ssn").to_pylist() == ["***", "***"]
    policy_env.write_text("{ this is not json")
    time.sleep(0.01)  # mtime resolution — the store hot-reloads on stat()
    still = eng.query_duckdb(SQL_ALL, caller=ALICE)
    assert still.column("ssn").to_pylist() == ["***", "***"], "previous policy still enforcing"
    assert still.num_rows == 2, "the row filter also still enforcing"


def test_default_group_applies_to_unbound_keys(tmp_path, policy_env):
    """Unbound keys fall to default_group when enforcement is on (review §5:
    restricted default for legacy keys) — DAVE has no binding at all, so he
    gets the DEFAULT group's rules (analysts: filter + redact). CAROL is
    deliberately fp-bound to the empty 'unrestricted' group and must stay
    raw (a binding beats the default)."""
    dave = Caller(cls="key", subject=None, key_fp="sha256:dddddddddddd", via="key")
    eng = _make_engine(tmp_path)
    out = eng.query_duckdb(SQL_ALL, caller=dave)
    # default_group = analysts: redact + row filter apply to him too.
    assert out.num_rows == 2
    assert out.column("ssn").to_pylist() == ["***", "***"]
    # carol's fp binding beats the default: raw rows.
    out_carol = eng.query_duckdb(SQL_ALL, caller=CAROL_UNMASKED)
    assert out_carol.num_rows == 3


def test_load_policy_rejects_unknown_group_reference(tmp_path):
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({"groups": {}, "subjects": {"alice": ["ghost"]}}))
    with pytest.raises(PolicyError, match="undefined group"):
        load_policy(str(pf))


def test_load_policy_rejects_bad_row_filter(tmp_path):
    pf = tmp_path / "p.json"
    pf.write_text(
        json.dumps(
            {
                "groups": {"g": {"tables": {"t/*": {"row_filter": "nonexistent > 1"}}}},
                "subjects": {},
            }
        )
    )
    with pytest.raises(PolicyError, match="bind"):
        load_policy(str(pf), table_columns={"t/one": ["id"]})


def test_policy_disabled_env_means_no_hash(tmp_path, policy_env, monkeypatch):
    """Flipping the flag OFF mid-flight returns to the empty hash (and the
    raw data path) — the operator's kill switch."""
    monkeypatch.setenv(POLICY_ENABLED_ENV, "0")
    reset_policy_store()
    eng = _make_engine(tmp_path)
    assert eng._policy_hash() == ""
    out = eng.query_duckdb(SQL_ALL, caller=ALICE)
    assert out.num_rows == 3, "enforcement off = raw data (the documented posture)"
