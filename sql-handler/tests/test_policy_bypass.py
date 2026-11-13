"""P0-1 regression suite: caller SQL may never name an engine-internal relation.

THE BYPASS (audit 2026-11-12, lead-verified end-to-end): ``_register_schema``
registers the RAW dataset of a policy-covered table as ``__sqlhandler_base_<n>``
on the SAME DuckDB connection the caller's statement runs on, then creates the
masking view over it. The name was a deterministic counter, so a masked caller
could join the base relation directly:

    SELECT b.ssn FROM work_order o JOIN __sqlhandler_base_1 b ON true

and read raw SSNs with the row filter gone. Three independent layers now stop
it, and every layer has a test here:

1. the base relation is named RANDOMLY per query (unaddressable);
2. the engine refuses caller SQL containing the reserved prefix before any
   registration, cache lookup or execution (query_duckdb, the direct QueryJob
   path the async job tier and the web UI use, and the write tier);
3. sqlguard refuses the prefix at the read-only MCP/web boundary.

Run:  python -m pytest tests/test_policy_bypass.py -v
"""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import _INTERNAL_REL_PREFIX, QueryJob, SqlEngine
from sqlhandler.identity import Caller
from sqlhandler.policy import POLICY_ENABLED_ENV, POLICY_FILE_ENV, reset_policy_store
from sqlhandler.provider import DataProvider, LakehouseError, TableInfo
from sqlhandler.sqlguard import INTERNAL_REL_PREFIX, assert_readonly

TABLES = [TableInfo(name="work_order", schema="workorder", format="parquet")]

#: The exact bypass from the audit repro (the pre-fix deterministic name).
BYPASS_JOIN = "SELECT b.ssn FROM work_order o JOIN __sqlhandler_base_1 b ON true"


class PolicyProvider(DataProvider):
    """FakeProvider with policy-relevant columns (ssn = the leak canary)."""

    kind = "fake"

    def __init__(self, root: Path):
        self.root = root

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return None


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
CAROL_UNMASKED = Caller(cls="key", subject=None, key_fp="sha256:cccccccccccc", via="key")
DAVE_HIDDEN = Caller(cls="key", subject=None, key_fp="sha256:dddddddddddd", via="key")


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    """Enforcement ON: alice masked+filtered, carol genuinely unmasked, dave
    bound to a group that HIDES the table (the empty-relation path)."""
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
                    "unrestricted": {},
                    "hidden_work": {"hidden_tables": ["workorder/work_order"]},
                },
                "subjects": {"alice": ["analysts"]},
                "key_fps": {
                    "sha256:cccccccccccc": ["unrestricted"],
                    "sha256:dddddddddddd": ["hidden_work"],
                },
            }
        )
    )
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    yield pf
    reset_policy_store()


class _RecordingCon:
    """DuckDB-connection stand-in: records what _register_schema registers."""

    def __init__(self):
        self.registered: list[str] = []
        self.executed: list[str] = []

    def register(self, name, obj):
        self.registered.append(str(name))

    def execute(self, sql, *args, **kwargs):
        self.executed.append(str(sql))


# ---------------------------------------------------------------------------
# (a) the exact bypass is REFUSED, and the error names the reason
# ---------------------------------------------------------------------------


def test_bypass_join_against_base_relation_is_refused(tmp_path, policy_env):
    eng = _make_engine(tmp_path)
    # Warm the result cache with a LEGITIMATE query first: the refusal must
    # happen before the cache lookup, so a bypass can never be served from it.
    eng.query_duckdb("SELECT id, ssn, kind FROM work_order ORDER BY id", caller=ALICE)
    with pytest.raises(LakehouseError) as ei:
        eng.query_duckdb(BYPASS_JOIN, caller=ALICE)
    msg = str(ei.value)
    assert "__sqlhandler_" in msg, msg
    assert "reserved" in msg.lower(), msg


# ---------------------------------------------------------------------------
# (b) the masked caller's normal query is still masked + row-filtered
# ---------------------------------------------------------------------------


def test_masked_caller_normal_query_still_masked_and_filtered(tmp_path, policy_env):
    eng = _make_engine(tmp_path)
    t = eng.query_duckdb("SELECT id, ssn, kind FROM work_order ORDER BY id", caller=ALICE)
    assert t.num_rows == 2, "row_filter must drop the 'secret' row"
    assert t.column("ssn").to_pylist() == ["***", "***"], "column_masks must still apply"
    assert t.column("kind").to_pylist() == ["a", "a"]


# ---------------------------------------------------------------------------
# (c) the unmasked caller is unaffected
# ---------------------------------------------------------------------------


def test_unmasked_caller_unaffected(tmp_path, policy_env):
    eng = _make_engine(tmp_path)
    t = eng.query_duckdb("SELECT id, ssn, kind FROM work_order ORDER BY id", caller=CAROL_UNMASKED)
    assert t.num_rows == 3
    assert t.column("ssn").to_pylist() == ["ssn-000", "ssn-001", "ssn-002"]


# ---------------------------------------------------------------------------
# (d) case-insensitive and quoted spellings are refused too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM __SQLHANDLER_BASE_1",
        'SELECT * FROM "__sqlhandler_base_1"',
        "select * from __SqlHandler_Base_1",
        "SELECT * FROM __sqlhandler_profile_base",
    ],
)
def test_bypass_refused_case_insensitively_and_quoted(tmp_path, policy_env, sql):
    eng = _make_engine(tmp_path)
    with pytest.raises(LakehouseError) as ei:
        eng.query_duckdb(sql, caller=ALICE)
    assert "reserved" in str(ei.value).lower(), str(ei.value)


# ---------------------------------------------------------------------------
# (e) hidden tables still register the EMPTY relation (unchanged behavior)
# ---------------------------------------------------------------------------


def test_hidden_table_still_registers_empty_relation(tmp_path, policy_env):
    eng = _make_engine(tmp_path)
    t = eng.query_duckdb("SELECT id, ssn FROM work_order", caller=DAVE_HIDDEN)
    assert t.num_rows == 0, "a hidden table must serve zero rows, not an error"
    assert t.column_names == ["id", "ssn"], "the empty relation keeps the real schema"


# ---------------------------------------------------------------------------
# (f) sqlguard refuses the prefix at the read-only boundary
# ---------------------------------------------------------------------------


def test_sqlguard_refuses_internal_prefix_for_plain_select():
    with pytest.raises(ValueError) as ei:
        assert_readonly("SELECT * FROM __sqlhandler_base_1")
    msg = str(ei.value)
    assert "internal relations" in msg and "reserved" in msg, msg
    # A normal SELECT still passes, byte-identically (trimmed).
    assert assert_readonly("  SELECT id FROM work_order  ") == "SELECT id FROM work_order"


def test_sqlguard_keeps_existing_statement_refusals():
    """The prefix check is ADDITIVE: a write still gets the statement-type
    refusal, so the existing error contract is unchanged."""
    with pytest.raises(ValueError) as ei:
        assert_readonly("INSERT INTO t SELECT * FROM __sqlhandler_base_1")
    assert "not allowed" in str(ei.value), str(ei.value)


# ---------------------------------------------------------------------------
# layer 1: the base relation name is random per query (unaddressable)
# ---------------------------------------------------------------------------


def test_base_relation_name_is_random_per_query(tmp_path, policy_env):
    eng = _make_engine(tmp_path)
    names: list[str] = []
    for _ in range(2):
        con = _RecordingCon()
        eng._register_schema(con, "SELECT id, ssn FROM work_order", caller=ALICE)
        bases = [n for n in con.registered if n.startswith("__sqlhandler_base_")]
        assert len(bases) == 1, con.registered
        # The masking view still reads the base relation (same name in the DDL).
        assert bases[0] in con.executed[0], con.executed[0]
        names.append(bases[0])
    assert names[0] != "__sqlhandler_base_1", "the deterministic counter name must be gone"
    assert names[0] != names[1], "the base relation name must be random per query"
    assert len(names[0]) == len("__sqlhandler_base_") + 12


def test_engine_and_sqlguard_share_one_prefix_constant():
    assert _INTERNAL_REL_PREFIX == INTERNAL_REL_PREFIX == "__sqlhandler_"


# ---------------------------------------------------------------------------
# layer 2 backstop: the direct QueryJob path (async job tier / web UI)
# ---------------------------------------------------------------------------


def test_direct_query_job_refuses_bypass(tmp_path, policy_env):
    eng = _make_engine(tmp_path)
    job = QueryJob(eng, BYPASS_JOIN, caller=ALICE)
    assert job.wait(10) == "error"
    err = job.error or ""
    assert "__sqlhandler_" in err and "reserved" in err.lower(), err
