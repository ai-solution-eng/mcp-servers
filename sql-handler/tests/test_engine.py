"""Unit tests for the shared SQL engine (caches + scans + SQL), no network.

Exercises SqlEngine + a tiny in-memory/temp-dir DataProvider: parquet files
on the local filesystem stand in for the source, so DuckDB + pyarrow run
for real while nothing touches the network.
"""

import json
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import LakehouseError, TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="work_order_note", schema="workorder", format="parquet"),
]


class FakeProvider:
    """A DataProvider backed by a local temp directory of Parquet files."""

    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.list_calls = 0
        self.open_calls: list[str] = []

    def list_tables(self):
        self.list_calls += 1
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        self.open_calls.append(info.path)
        d = self.root / info.path
        if (d / "part.parquet").exists():
            return pad.dataset(str(d), format="parquet")
        # Arbitrary tables in the cache tests: serve a tiny in-memory dataset.
        return pad.dataset(pa.table({"id": [1], "x": ["a"]}))


def _write(root, rel, table):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, d / "part.parquet")


def _make_engine(tmp_path, **kw):
    _write(
        tmp_path,
        "workorder/work_order",
        pa.table(
            {
                "id": [1, 2, 3],
                "amount": [10.5, 20.0, 30.5],
                "kind": ["a", "b", "a"],
            }
        ),
    )
    _write(
        tmp_path,
        "workorder/work_order_note",
        pa.table(
            {
                "note_id": [10, 20],
                "text": ["hi", "bye"],
            }
        ),
    )
    provider = FakeProvider(tmp_path)
    return SqlEngine(provider, **kw), provider


# ---------------------------------------------------------------------------
# list_tables cache
# ---------------------------------------------------------------------------


def test_list_tables_cached_within_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600)
    t1 = eng.list_tables()
    t2 = eng.list_tables()
    assert t1 == t2
    assert provider.list_calls == 1  # second call served from cache


def test_list_tables_not_cached_when_disabled(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=0)
    eng.list_tables()
    eng.list_tables()
    assert provider.list_calls == 2


class BlockingFakeProvider(FakeProvider):
    """FakeProvider whose list_tables can be made to block on demand."""

    def __init__(self, root):
        super().__init__(root)
        self._hold = threading.Event()
        self._hold.set()  # set = allow listing; clear = block it

    def release(self):
        self._hold.set()

    def list_tables(self):
        if not self._hold.is_set():
            self._hold.wait()  # block until released
        return super().list_tables()


def _wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_list_tables_serves_stale_then_async_refresh(tmp_path):
    # A stale list is returned immediately, and a background thread refreshes it.
    provider = BlockingFakeProvider(tmp_path)
    eng = SqlEngine(provider, cache_ttl=3600)
    assert eng.list_tables() == TABLES
    assert provider.list_calls == 1

    provider._hold.clear()  # make the next listing block
    # Age the cache past the TTL. Do NOT use 0.0: time.monotonic() starts
    # near boot, so on a freshly booted machine a 0.0 timestamp is still
    # "fresh" (< TTL since boot) and the background refresh never triggers.
    eng._tables_ts = time.monotonic() - (eng.cache_ttl + 10)
    t0 = time.monotonic()
    result = eng.list_tables()
    dt = time.monotonic() - t0
    assert result == TABLES  # served from cache
    assert dt < 1.0  # did NOT wait on the blocked listing
    assert provider.list_calls == 1  # listing not re-done on the call itself

    provider.release()  # release the background refresh
    assert _wait_until(lambda: provider.list_calls == 2)
    assert _wait_until(lambda: eng._tables_ts > 0.0)
    assert eng.cache_stats()["list_refreshing"] is False


def test_list_tables_async_disabled_blocks_on_stale(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, list_async_refresh=False)
    eng.list_tables()
    assert provider.list_calls == 1
    assert eng.cache_stats()["list_async_refresh"] is False

    eng._tables_ts = time.monotonic() - (eng.cache_ttl + 10)
    assert eng.list_tables() == TABLES
    assert provider.list_calls == 2  # synchronous refresh on the call


def test_list_tables_auto_refresh_without_calls(tmp_path):
    # The daemon timer re-lists every cache_ttl even with no callers.
    eng, provider = _make_engine(tmp_path, cache_ttl=1)
    eng.list_tables()
    assert provider.list_calls == 1
    assert _wait_until(lambda: provider.list_calls >= 2, timeout=5)


def test_resolve_preserves_location_for_deep_schema(tmp_path):
    # A table whose folder is deeper than its logical schema/name (e.g.
    # finance/b/c -> schema b, name c) must keep its physical location when
    # resolved by schema/name or qualified name, or the dataset can't be
    # opened (open_dataset falls back to info.path otherwise).
    provider = FakeProvider(tmp_path)
    provider.list_tables = lambda: [
        TableInfo(name="c", schema="b", format="parquet", location="finance/b/c"),
        *TABLES,
    ]
    eng = SqlEngine(provider, cache_ttl=3600, list_async_refresh=False)
    assert eng._resolve("b/c").location == "finance/b/c"  # schema/name path
    assert eng._resolve("b_c").location == "finance/b/c"  # qualified name
    assert eng._resolve("c").name == "c"  # bare name


# --------------------------------------------------------------------------- describe cache
# ---------------------------------------------------------------------------


def test_describe_cached_within_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    first = eng.describe_table("workorder/work_order")
    second = eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 1  # second call served from describe cache
    assert first == second
    stats = eng.cache_stats()
    assert stats["describe_hits"] >= 1
    assert stats["describe_cached_tables"] == 1


def test_describe_not_cached_when_disabled(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=0, dataset_cache_ttl=0)
    eng.describe_table("workorder/work_order")
    eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 2


def test_describe_cache_expires_after_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=60, dataset_cache_ttl=0)
    eng.describe_table("workorder/work_order")
    key = ("default", "workorder/work_order")
    # Age past the TTL without relying on monotonic()'s boot-time origin.
    eng._describe_cache[key] = (
        time.monotonic() - (eng.cache_ttl + 10),
        eng._describe_cache[key][1],
    )
    eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 2


def test_describe_columns_and_uri(tmp_path):
    eng, _ = _make_engine(tmp_path)
    d = eng.describe_table("work_order")
    assert d["uri"] == "fake://workorder/work_order"
    assert {c["name"] for c in d["columns"]} == {"id", "amount", "kind"}
    assert d["n_columns"] == 3


# --------------------------------------------------------------------------- dataset -- cache
# ---------------------------------------------------------------------------


def test_dataset_cached_within_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=3600)
    info = TABLES[0]
    d1 = eng._open_dataset(info)
    d2 = eng._open_dataset(info)
    assert d1 is d2
    assert len(provider.open_calls) == 1
    assert eng.cache_stats()["dataset_hits"] >= 1


def test_dataset_not_cached_when_disabled(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=0, dataset_cache_ttl=0)
    info = TableInfo("a", "s")
    eng._open_dataset(info)
    eng._open_dataset(info)
    assert len(provider.open_calls) == 2


def test_dataset_lru_eviction(tmp_path):
    eng, provider = _make_engine(
        tmp_path, cache_ttl=3600, dataset_cache_ttl=3600, dataset_cache_tables=2
    )
    for name in ("a", "b", "c"):
        eng._open_dataset(TableInfo(name, "s"))
    assert len(eng._dataset_cache) == 2  # capped
    before = len(provider.open_calls)
    eng._open_dataset(TableInfo("a", "s"))  # "a" was evicted -> re-open
    assert len(provider.open_calls) == before + 1


# --------------------------------------------------------------------- prewarm --
# ---------------------------------------------------------------------


def test_prewarm_populates_describe_cache(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    outcomes = eng.prewarm(("workorder/work_order", "workorder/work_order_note"))
    assert outcomes == {
        "workorder/work_order": "ok",
        "workorder/work_order_note": "ok",
    }
    assert len(provider.open_calls) == 2
    # A direct describe afterwards is now a cache hit (no extra open).
    eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 2


def test_prewarm_records_failures(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600)

    def boom(info):
        raise RuntimeError("unavailable")

    provider.open_dataset = boom
    outcomes = eng.prewarm(("workorder/work_order",))
    assert outcomes["workorder/work_order"].startswith("error:")


# --------------------------------------------------------------------- scans + SQL --
# ---------------------------------------------------------------------


def test_scan_arrow_with_projection_and_limit(tmp_path):
    eng, _ = _make_engine(tmp_path)
    t = eng.scan_arrow("work_order", columns=["id"], limit=2)
    assert t.column_names == ["id"]
    assert t.num_rows == 2


def test_scan_arrow_with_filter(tmp_path):
    import pyarrow.compute as pc

    eng, _ = _make_engine(tmp_path)
    t = eng.scan_arrow("work_order", columns=["id"], filters=[pc.field("amount") > 15])
    assert t.to_pydict()["id"] == [2, 3]


def test_query_duckdb(tmp_path):
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT count(*) AS cnt, sum(amount) AS total FROM work_order")
    assert arrow.to_pydict() == {"cnt": [3], "total": [61.0]}


def test_query_duckdb_join_across_tables(tmp_path):
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb(
        "SELECT w.kind, n.note_id FROM work_order w JOIN work_order_note n ON 1=1 LIMIT 3"
    )
    assert arrow.num_rows == 3


def test_query_duckdb_unknown_table_raises(tmp_path):
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(LakehouseError):
        eng.query_duckdb("SELECT * FROM missing_thing")


# --------------------------------------------------------------------------- profile


def test_profile_table_stats(tmp_path):
    eng, _ = _make_engine(tmp_path)
    p = eng.profile_table("work_order")
    assert p["n_rows"] == 3
    assert p["profiled_rows"] == 3
    by_name = {c["name"]: c for c in p["columns"]}
    assert set(by_name) == {"id", "amount", "kind"}
    # min/max come back as strings from SUMMARIZE, but they must be right.
    assert by_name["amount"]["min"] == "10.5"
    assert by_name["amount"]["max"] == "30.5"
    assert by_name["id"]["min"] == "1"
    assert by_name["id"]["max"] == "3"
    assert by_name["kind"]["approx_unique"] == 2
    assert by_name["kind"]["null_pct"] == 0.0
    # quantiles present for numeric columns
    assert by_name["amount"]["q50"] is not None


def test_profile_table_is_cached(tmp_path):
    eng, _provider = _make_engine(tmp_path, dataset_cache_ttl=3600)
    first = eng.profile_table("work_order")
    second = eng.profile_table("work_order")
    assert first == second
    stats = eng.cache_stats()
    assert stats["profile_hits"] == 1
    assert stats["profile_cached_tables"] == 1
    # profiled rows are not cached when TTL is disabled
    eng2, provider2 = _make_engine(tmp_path, cache_ttl=0)
    eng2.profile_table("work_order")
    eng2.profile_table("work_order")
    assert provider2.open_calls.count("workorder/work_order") >= 1


def test_profile_table_column_subset(tmp_path):
    eng, _ = _make_engine(tmp_path)
    p = eng.profile_table("work_order", columns=["amount"])
    assert [c["name"] for c in p["columns"]] == ["amount"]
    # different subset = different cache entry, both served
    p2 = eng.profile_table("work_order", columns=["id"])
    assert [c["name"] for c in p2["columns"]] == ["id"]
    assert eng.profile_table("work_order", columns=["amount"]) == p


def test_profile_table_unknown_column_raises(tmp_path):
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(LakehouseError):
        eng.profile_table("work_order", columns=["nope"])


def test_profile_max_rows_env_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "2")
    eng, _ = _make_engine(tmp_path)
    p = eng.profile_table("work_order")
    assert p["profiled_rows"] == 2
    assert p["n_rows"] == 3  # full count still from metadata
    assert p["profile_max_rows"] == 2


# --------------------------------------------------------------------------- semantic catalog


_CATALOG = {
    "version": 1,
    "tables": {
        "workorder/work_order": {
            "description": "Work order headers, one row per maintenance order",
            "aliases": ["work orders"],
            "columns": {
                "amount": "Order total in USD",
                "kind": "Order class: a=planned, b=unplanned",
            },
        },
        "work_order": {"description": "bare-name fallback entry"},
    },
}


def _write_catalog(tmp_path, data=_CATALOG):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_catalog_merges_into_describe(tmp_path, monkeypatch):
    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    d = eng.describe_table("workorder/work_order")
    assert d["description"] == "Work order headers, one row per maintenance order"
    assert d["aliases"] == ["work orders"]
    cols = {c["name"]: c for c in d["columns"]}
    assert cols["amount"]["description"] == "Order total in USD"
    assert cols["kind"]["description"] == "Order class: a=planned, b=unplanned"
    assert "description" not in cols["id"]  # undocumented columns untouched


def test_catalog_absent_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLHANDLER_CATALOG", raising=False)
    eng, _ = _make_engine(tmp_path)
    d = eng.describe_table("work_order")
    assert "description" not in d
    assert all("description" not in c for c in d["columns"])


def test_catalog_broken_file_is_ignored(tmp_path, monkeypatch):
    p = tmp_path / "catalog.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    d = eng.describe_table("work_order")  # must not raise
    assert "description" not in d


def test_catalog_hot_reload_on_mtime_change(tmp_path, monkeypatch):
    import os as _os

    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    # path-keyed entry wins (lookup order: path, qualified, bare name)
    assert (
        eng.describe_table("workorder/work_order")["description"]
        == "Work order headers, one row per maintenance order"
    )
    # Rewrite with a new description and bump the mtime.
    _write_catalog(tmp_path, {"tables": {"workorder/work_order": {"description": "updated"}}})
    _os.utime(p, (time.time() + 5, time.time() + 5))
    assert eng.describe_table("workorder/work_order")["description"] == "updated"


def test_table_description_for_list(tmp_path, monkeypatch):
    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    tables = eng.list_tables()
    target = next(t for t in tables if t.path == "workorder/work_order")
    assert eng.table_description(target).startswith("Work order headers")
    other = next(t for t in tables if t.path == "workorder/work_order_note")
    assert eng.table_description(other) == ""


def test_catalog_yaml_file_loaded(tmp_path, monkeypatch):
    """The engine accepts YAML catalog files, not just JSON (JSON tried first)."""
    p = tmp_path / "catalog.yaml"
    p.write_text(
        "tables:\n"
        "  workorder/work_order:\n"
        "    description: Work order headers (YAML)\n"
        "    aliases:\n"
        "      - work orders\n"
        "    columns:\n"
        "      amount: Order total in USD\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    d = eng.describe_table("workorder/work_order")
    assert d["description"] == "Work order headers (YAML)"
    assert d["aliases"] == ["work orders"]
    cols = {c["name"]: c for c in d["columns"]}
    assert cols["amount"]["description"] == "Order total in USD"


def test_catalog_store_upload_overrides_configured(tmp_path, monkeypatch):
    """An uploaded catalog wins over the configured file until cleared."""
    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    assert (
        eng.describe_table("workorder/work_order")["description"]
        == "Work order headers, one row per maintenance order"
    )
    # Upload as YAML text — the friendlier format must work end to end.
    res = eng.set_catalog_text("tables:\n  workorder/work_order:\n    description: uploaded\n")
    assert res["tables"] == 1
    assert eng.describe_table("workorder/work_order")["description"] == "uploaded"
    status = eng.catalog_status()
    assert status["active_source"] == "upload"
    assert status["uploaded"]["tables"] == 1
    # The store file is canonical JSON regardless of upload format.
    data = json.loads(Path(status["uploaded"]["path"]).read_text(encoding="utf-8"))
    assert data["tables"]["workorder/work_order"]["description"] == "uploaded"


def test_catalog_clear_falls_back_to_configured(tmp_path, monkeypatch):
    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    eng.set_catalog_text('{"tables": {"workorder/work_order": {"description": "uploaded"}}}')
    assert eng.catalog_status()["active_source"] == "upload"
    assert eng.clear_catalog() is True
    status = eng.catalog_status()
    assert status["active_source"] == "configured"
    assert (
        eng.describe_table("workorder/work_order")["description"]
        == "Work order headers, one row per maintenance order"
    )
    assert eng.clear_catalog() is False  # nothing left to remove


def test_set_catalog_text_rejects_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    bad_inputs = [
        "{not json or yaml",  # neither format parses
        "[1, 2]",  # valid YAML but not an object
        '"a scalar"',  # valid JSON but not an object
        '{"no_tables_key": {}}',  # missing the tables mapping
        '{"tables": {"t": "not-a-mapping"}}',  # entry must be a mapping
    ]
    for text in bad_inputs:
        with pytest.raises(ValueError):
            eng.set_catalog_text(text)
    status = eng.catalog_status()
    assert status["uploaded"] is not None  # peeked, but
    assert not status["uploaded"].get("exists")  # nothing was written


def test_catalog_yaml_without_pyyaml_clear_error(tmp_path, monkeypatch):
    """Without pyyaml, YAML uploads fail with an actionable message."""
    import builtins

    real_import = builtins.__import__

    def _no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    monkeypatch.setattr(builtins, "__import__", _no_yaml)
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(ValueError, match="PyYAML is not installed"):
        eng.set_catalog_text("tables: {}")


# ---- catalog editing: content / per-table upsert + remove --------------------


def test_catalog_content_yaml_and_json(tmp_path, monkeypatch):
    """catalog_content serializes the ACTIVE catalog (YAML default, JSON swap)."""
    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    y = eng.catalog_content()
    assert y["format"] == "yaml"
    assert y["source"] == "configured"
    assert y["tables"] == 2
    assert "description: Work order headers" in y["text"]
    j = eng.catalog_content("json")
    parsed = json.loads(j["text"])
    expected = _CATALOG["tables"]["workorder/work_order"]["description"]
    assert parsed["tables"]["workorder/work_order"]["description"] == expected
    with pytest.raises(ValueError):
        eng.catalog_content("xml")


def test_catalog_content_starter_when_absent(tmp_path, monkeypatch):
    """No catalog: the editor gets a starter skeleton, not an empty pane."""
    monkeypatch.delenv("SQLHANDLER_CATALOG", raising=False)
    eng, _ = _make_engine(tmp_path)
    c = eng.catalog_content()
    assert c["source"] == "none" and c["tables"] == 0
    assert "tables:" in c["text"]  # the server must be able to parse it back
    assert eng.catalog_content("json")["text"].lstrip().startswith("{")


def test_catalog_table_entry_found_and_missing(tmp_path, monkeypatch):
    p = _write_catalog(tmp_path)
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    eng, _ = _make_engine(tmp_path)
    d = eng.catalog_table_entry("workorder/work_order")
    assert d["found"] is True
    assert d["key"] == "workorder/work_order"
    assert "Order class" in d["text"]
    # a table with no entry: found False, empty text, canonical key = path
    d2 = eng.catalog_table_entry("workorder/work_order_note")
    assert d2["found"] is False and d2["text"] == ""
    assert d2["key"] == "workorder/work_order_note"
    with pytest.raises(LakehouseError):
        eng.catalog_table_entry("nope/missing")


def test_catalog_update_table_preserves_key(tmp_path, monkeypatch):
    """An edit updates the key actually being served (bare name here)."""
    p = _write_catalog(tmp_path, {"tables": {"work_order": {"description": "old"}}})
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    res = eng.catalog_update_table(
        "workorder/work_order",
        "description: updated\ncolumns:\n  amount: USD total\n",
    )
    assert res["key"] == "work_order"  # the existing bare-name key, not a fork
    assert res["tables"] == 1
    assert eng.catalog_status()["active_source"] == "upload"
    info = next(t for t in eng.list_tables() if t.name == "work_order")
    assert eng.table_description(info) == "updated"


def test_catalog_update_table_creates_and_roundtrips(tmp_path, monkeypatch):
    """No catalog at all: a per-table edit bootstraps the store; JSON works."""
    monkeypatch.delenv("SQLHANDLER_CATALOG", raising=False)
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    eng.catalog_update_table(
        "workorder/work_order", '{"description": "from json", "aliases": ["wo"]}'
    )
    d = eng.catalog_table_entry("workorder/work_order")
    assert d["found"] is True and "from json" in d["text"]
    # a single-entry `tables:` wrapper is forgiven and unwrapped
    eng.catalog_update_table(
        "workorder/work_order", "tables:\n  whatever:\n    description: wrapped\n"
    )
    assert eng.catalog_table_entry("workorder/work_order")["text"].startswith(
        "description: wrapped"
    )


def test_catalog_update_table_rejects_bad_fragments(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    for frag in (
        "- just\n- a list\n",  # not a mapping
        "description: [not, a, string]\n",  # wrong type
        "aliases: not-a-list\n",
        "columns:\n  amount: [nope]\n",
        "",  # empty
        "tables:\n  a:\n    description: x\n  b:\n    description: y\n",  # multi-entry wrapper
    ):
        with pytest.raises(ValueError):
            eng.catalog_update_table("workorder/work_order", frag)
    assert not (tmp_path / "store.json").exists()  # nothing was written


def test_catalog_update_table_preserves_unknown_keys(tmp_path, monkeypatch):
    """Hand-written extra fields survive an edit instead of being dropped."""
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    eng.catalog_update_table("workorder/work_order", "description: d\nowner: data-team\n")
    assert eng._catalog()["workorder/work_order"]["owner"] == "data-team"


def test_catalog_remove_table(tmp_path, monkeypatch):
    p = _write_catalog(tmp_path)  # workorder/work_order + bare work_order
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(p))
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    eng, _ = _make_engine(tmp_path)
    # an undocumented table is a no-op
    assert eng.catalog_remove_table("workorder/work_order_note")["removed"] is False
    # removing via the table's path drops the path-keyed entry, keeps the rest
    res = eng.catalog_remove_table("workorder/work_order")
    assert res == {"removed": True, "key": "workorder/work_order", "tables": 1}
    # the table is still documented through the bare-name fallback entry
    d = eng.catalog_table_entry("workorder/work_order")
    assert d["found"] is True and d["key"] == "work_order"
    # removing the last remaining entry clears the store entirely — no empty
    # override shadowing the configured file, which takes back over.
    assert eng.catalog_remove_table("workorder/work_order") == {
        "removed": True,
        "key": "work_order",
        "tables": 0,
    }
    assert not (tmp_path / "store.json").exists()
    assert eng.catalog_status()["active_source"] == "configured"
    d = eng.catalog_table_entry("workorder/work_order")
    assert d["found"] is True and d["key"] == "workorder/work_order"


# ---------------------------------------------------------------- did-you-mean


def test_query_error_suggests_columns(tmp_path):
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(LakehouseError) as ei:
        eng.query_duckdb("SELECT amont FROM work_order")
    # DuckDB >=1.x already prints its own "Candidate bindings" for unknown
    # columns; we don't duplicate that, but the bindings must reach the agent.
    msg = str(ei.value)
    assert "amount" in msg


def test_query_error_suggests_tables(tmp_path):
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(LakehouseError) as ei:
        eng.query_duckdb("SELECT * FROM work_ordr")
    msg = str(ei.value)
    assert "Did you mean" in msg
    assert "work_order" in msg


def test_query_error_unrelated_gets_no_hint_garbage(tmp_path):
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(LakehouseError) as ei:
        eng.query_duckdb("SELECT amont FROM work_order")
    # the original DuckDB text is still present for context
    assert "DuckDB query failed" in str(ei.value)


# ------------------------------------------------------------ usage prewarm


def test_usage_counts_and_top_tables(tmp_path):
    eng, _ = _make_engine(tmp_path, cache_ttl=0)
    eng.describe_table("workorder/work_order")
    eng.describe_table("workorder/work_order")
    eng.describe_table("workorder/work_order_note")
    top = eng.usage_top_tables()
    assert top[0] == "workorder/work_order"
    assert len(top) == 2


def test_usage_persisted_to_disk_and_restored(tmp_path):
    cache_dir = tmp_path / "warm"
    eng, _ = _make_engine(tmp_path, cache_ttl=3600, cache_dir=str(cache_dir))
    eng.describe_table("workorder/work_order")
    eng._save_cache_to_disk()  # force flush for the test
    eng2, _ = _make_engine(tmp_path, cache_ttl=3600, cache_dir=str(cache_dir))
    assert eng2.usage_top_tables()[0] == "workorder/work_order"
