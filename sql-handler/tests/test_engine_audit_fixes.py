"""Engine audit fixes (2026-11-12): regression tests for the P1/P2 items.

Each test names the audit item it pins. Everything here is behavioral — the
audit's findings were all in paths the green suite never exercised.
"""

import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler import resources
from sqlhandler.engine import QueryJob, SqlEngine, _normalize_cache_sql
from sqlhandler.identity import Caller
from sqlhandler.policy import POLICY_ENABLED_ENV, POLICY_FILE_ENV, policy_store, reset_policy_store
from sqlhandler.provider import DataProvider, LakehouseError, TableInfo
from sqlhandler.sqlguard import assert_readonly


class FakeProvider(DataProvider):
    kind = "fake"

    def __init__(self, root: Path, tables=None):
        self.root = root
        self.tables = tables or []

    def list_tables(self):
        return self.tables

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return None


def _seed(root: Path, rel: str, rows: int = 3) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": pa.array(range(rows), type=pa.int64()), "ssn": [f"s{i}" for i in range(rows)]}),
        d / "part.parquet",
    )


def _engine(tmp_path: Path, tables=None, **kw) -> SqlEngine:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    kw.setdefault("cache_ttl", 3600)
    kw.setdefault("dataset_cache_ttl", 0)
    return SqlEngine(FakeProvider(root, tables or []), **kw)


ALICE = Caller(cls="user", subject="alice", key_fp="sha256:aaaaaaaaaaaa", via="relay")
CAROL = Caller(cls="key", subject=None, key_fp="sha256:cccccccccccc", via="key")


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "all",
                "groups": {
                    "all": {},
                    "restricted": {"hidden_tables": ["hr/payroll"]},
                    "unrestricted": {},
                },
                "subjects": {"alice": ["restricted"]},
                "key_fps": {"sha256:cccccccccccc": ["unrestricted"]},
            }
        )
    )
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    yield pf
    reset_policy_store()


# ---------------------------------------------------------------- A (P1-1)


def test_escape_string_variants_do_not_share_a_cache_key():
    """E'' backslash escapes: two DIFFERENT strings used to normalize alike."""
    a = "SELECT E'a\\'  b\\'  c'"
    b = "SELECT E'a\\' b\\'  c'"
    assert a != b
    assert _normalize_cache_sql(a) != _normalize_cache_sql(b)
    # Normalization still works for the safe shapes.
    assert _normalize_cache_sql("SELECT   id   FROM t  ") == "SELECT id FROM t"
    assert _normalize_cache_sql("SELECT 'a  b'") == "SELECT 'a  b'"


# ---------------------------------------------------------------- B (P1-2)


def test_spill_to_disk_succeeds_under_the_filesystem_lockdown(tmp_path, monkeypatch):
    """The lockdown must not break the documented spill path."""
    monkeypatch.setenv("SQLHANDLER_DUCKDB_TEMP_DIR", str(tmp_path / "spill"))
    monkeypatch.setattr(resources, "duckdb_budget", lambda: {"memory_limit": "64MB", "threads": 2})
    eng = _engine(tmp_path)
    out = eng.query_duckdb("SELECT count(*) AS n FROM (SELECT range FROM range(20000000) ORDER BY range DESC)")
    assert out.column("n").to_pylist() == [20000000]


def test_lockdown_configures_the_spill_dir_before_closing_access(tmp_path, monkeypatch):
    """DuckDB refuses to change temp_directory AFTER external access closes.

    So the lockdown must configure it first (live-verified); otherwise the
    operator's SQLHANDLER_DUCKDB_TEMP_DIR (a mounted emptyDir in k8s) is
    silently ignored and spill goes to DuckDB's default.
    """
    import duckdb

    from sqlhandler.engine import _current_temp_dir, _duckdb_fs_lockdown

    spill = tmp_path / "spill"
    monkeypatch.setenv("SQLHANDLER_DUCKDB_TEMP_DIR", str(spill))
    con = duckdb.connect()
    try:
        _duckdb_fs_lockdown(con)
        assert os.path.realpath(_current_temp_dir(con)) == os.path.realpath(str(spill))
    finally:
        con.close()


def test_lockdown_still_refuses_local_file_reads(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    with pytest.raises(LakehouseError) as ei:
        eng.query_duckdb("SELECT count(*) FROM read_csv('/etc/hostname')")
    msg = str(ei.value).lower()
    assert "permission" in msg or "disabled" in msg or "cannot access" in msg, msg


# ---------------------------------------------------------------- D (P1-9)


def test_catalog_content_hides_entries_the_caller_cannot_see(tmp_path, policy_env):
    root = tmp_path / "data"
    _seed(root, "workorder/work_order")
    _seed(root, "hr/payroll")
    tables = [
        TableInfo(name="work_order", schema="workorder", format="parquet"),
        TableInfo(name="payroll", schema="hr", format="parquet"),
    ]
    eng = SqlEngine(FakeProvider(root, tables), cache_ttl=3600, dataset_cache_ttl=0)
    eng.set_catalog_text(
        json.dumps(
            {
                "tables": {
                    "workorder/work_order": {"description": "orders"},
                    "hr/payroll": {"description": "SECRET payroll docs"},
                }
            }
        )
    )
    restricted = json.loads(eng.catalog_content("json", caller=ALICE)["text"])
    assert "workorder/work_order" in restricted["tables"]
    assert "hr/payroll" not in restricted["tables"], "a hidden table's docs leaked through the editor view"
    unrestricted = json.loads(eng.catalog_content("json", caller=CAROL)["text"])
    assert set(unrestricted["tables"]) == {"workorder/work_order", "hr/payroll"}
    # The single-entry view is not an existence oracle either.
    with pytest.raises(LakehouseError):
        eng.catalog_table_entry("payroll", "json", caller=ALICE)
    assert eng.catalog_table_entry("payroll", "json", caller=CAROL)["found"] is True


# ---------------------------------------------------------------- E


def test_virtual_definition_may_not_reference_internal_relations(tmp_path):
    eng = _engine(tmp_path)
    assert eng._definition_error("SELECT * FROM __sqlhandler_base_1") is not None
    assert eng._definition_error("SELECT 1") is None


# ---------------------------------------------------------------- F1


def test_volatile_functions_are_never_cached(tmp_path, policy_env):
    eng = _engine(
        tmp_path, [TableInfo(name="work_order", schema="workorder", format="parquet")]
    )
    _seed(tmp_path / "data", "workorder/work_order")
    assert eng._result_cache_key("SELECT now() AS t", None, None, None, None) is None
    assert eng._result_cache_key("SELECT random() AS r", None, None, None, None) is None
    assert eng._result_cache_key("SELECT id FROM work_order", None, None, None, None) is not None


# ---------------------------------------------------------------- F6/F7


def test_cancel_before_the_connection_opens_stops_the_query(tmp_path):
    eng = _engine(tmp_path)
    job = QueryJob(eng, "SELECT count(*) FROM range(100000000)")
    assert job.cancel() is True
    assert job.wait(60) == "cancelled"


def test_cancel_after_completion_reports_false(tmp_path):
    eng = _engine(tmp_path)
    job = QueryJob(eng, "SELECT 1 AS x")
    assert job.wait(60) == "done"
    assert job.cancel() is False


# ---------------------------------------------------------------- F8


def test_readonly_guard_refuses_pragma_database_list():
    """DuckDB rewrites PRAGMA database_list to a paren-less pragma_* SELECT."""
    with pytest.raises(ValueError):
        assert_readonly("PRAGMA database_list")
    # A normal SELECT is untouched.
    assert assert_readonly("SELECT 1") == "SELECT 1"


# ---------------------------------------------------------------- F9


def test_negative_env_int_falls_back_to_the_default(monkeypatch):
    from sqlhandler import limits

    monkeypatch.setenv("SQLHANDLER_TEST_NEG_INT", "-5")
    assert limits._env_int("SQLHANDLER_TEST_NEG_INT", 7) == 7
    monkeypatch.setenv("SQLHANDLER_TEST_NEG_INT", "0")
    assert limits._env_int("SQLHANDLER_TEST_NEG_INT", 7) == 0


# ---------------------------------------------------------------- F13


def test_gate_release_returns_the_slot_to_the_semaphore_it_acquired(monkeypatch):
    from sqlhandler.engine import _QueryGate

    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "2")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "1")
    monkeypatch.delenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", raising=False)
    gate = _QueryGate()
    held = gate.acquire(None)
    sem, _slot = held
    # Simulate a resize between acquire and release: a NEW semaphore is current.
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "5")
    assert gate._semaphore() is not sem
    gate.release(held)
    # The slot went back to the ORIGINAL semaphore (not the new one).
    assert sem.acquire(blocking=False) is True


# ---------------------------------------------------------------- H1


def test_policy_columns_provider_is_wired_and_used(tmp_path, monkeypatch):
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "g",
                "groups": {"g": {"tables": {"workorder/work_order": {"row_filter": "no_such_col != 'x'"}}}},
            }
        )
    )
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    tables = [TableInfo(name="work_order", schema="workorder", format="parquet")]
    eng = _engine(tmp_path, tables)
    _seed(tmp_path / "data", "workorder/work_order")
    assert policy_store()._table_columns_provider is not None, "the provider was never wired"
    columns = eng._policy_table_columns()
    assert columns and columns["workorder/work_order"] == ["id", "ssn"]
    # With the provider wired, load-time validation sees the real columns and
    # rejects the bad filter (broken policy = fail closed) instead of loading it.
    assert policy_store().get().source == "<broken>"


# ---------------------------------------------------------------- H2


def test_write_source_snapshot_is_keyed_per_policy(tmp_path, policy_env):
    root = tmp_path / "data"
    _seed(root, "workorder/work_order")
    _seed(root, "hr/payroll")
    tables = [
        TableInfo(
            name="work_order",
            schema="workorder",
            format="parquet",
            location=str(root / "workorder" / "work_order"),
        ),
        TableInfo(name="payroll", schema="hr", format="parquet", location=str(root / "hr" / "payroll")),
    ]
    eng = SqlEngine(FakeProvider(root, tables), cache_ttl=3600, dataset_cache_ttl=0)
    scratch = str(tmp_path / "scratch" / "alice" / "t")
    # ALICE cannot see payroll: her snapshot omits it (built first, cached).
    eng._refuse_write_target_collision(None, scratch, caller=ALICE)
    # CAROL CAN see payroll: the guard must still refuse writing onto it.
    payroll_loc = os.path.realpath(str(root / "hr" / "payroll"))
    with pytest.raises(LakehouseError):
        eng._refuse_write_target_collision(None, payroll_loc, caller=CAROL)
