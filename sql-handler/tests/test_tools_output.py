"""Tests for the search_tables tool and structured (json/csv) tool output."""

import base64
import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="work_order_note", schema="workorder", format="parquet"),
]


class SearchStubEngine:
    """Engine surface the tool functions touch, with canned results."""

    def __init__(self, results=None, arrow=None):
        self._results = results or []
        self._arrow = arrow

    def search_tables(self, query, **kw):
        return self._results

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
        return self._arrow

    def scan_arrow(self, table, columns=None, limit=None, version_as_of=None, **kw):
        return self._arrow


@pytest.fixture
def stub(monkeypatch):
    eng = SearchStubEngine(
        results=[
            {
                "table": "workorder/work_order",
                "name": "work_order",
                "qualified_name": "workorder_work_order",
                "format": "parquet",
                "source": "default",
                "description": "Work order headers, one row per order",
                "matched_columns": ["amount"],
                "score": 150,
            }
        ],
        arrow=pa.table({"id": [1, 2], "amount": [10.5, 20.0]}),
    )
    monkeypatch.setattr(server, "_handler", lambda: eng)
    return eng


# ---------------------------------------------------------------- search


def test_search_tables_rendering(stub):
    text = server.search_tables("work order amount")
    assert "1 table(s) matching" in text
    assert "workorder/work_order (parquet)" in text
    assert "columns: amount" in text
    assert "Work order headers" in text


def test_search_tables_no_matches(stub):
    stub._results = []
    assert "No tables match" in server.search_tables("zzz")


def test_search_tables_error_path(monkeypatch):
    class Boom:
        def search_tables(self, _):
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "_handler", lambda: Boom())
    assert "Error searching tables" in server.search_tables("x")


def test_engine_search_matches_name_and_catalog(tmp_path, monkeypatch):

    # Real engine over parquet fixtures + catalog descriptions.
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1], "amount": [10.0], "kind": ["a"]}), d / "p.parquet")

    class P:
        kind = "fake"

        def list_tables(self, **kw):
            return TABLES

        def table_uri(self, info):
            return f"fake://{info.path}"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    cat = tmp_path / "catalog.json"
    cat.write_text(
        json.dumps(
            {
                "tables": {
                    "workorder/work_order": {
                        "description": "maintenance work orders",
                        "columns": {"amount": "total cost in USD"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    eng = SqlEngine(P())

    # matches on table name
    hits = eng.search_tables("work_order")
    assert hits and hits[0]["table"] == "workorder/work_order"
    # matches on catalog description terms
    hits = eng.search_tables("maintenance")
    assert hits and hits[0]["description"].startswith("maintenance")
    # matches on documented column + reports the column
    hits = eng.search_tables("cost usd")
    assert hits and "amount" in hits[0]["matched_columns"]
    # no match
    assert eng.search_tables("completely unrelated") == []
    # empty query
    assert eng.search_tables("") == []


# ------------------------------------------------------- structured output


def test_run_sql_json_output(stub):
    out = server.run_sql("SELECT * FROM t", output_format="json")
    payload = json.loads(out)
    assert payload["columns"] == ["id", "amount"]
    assert payload["rows"] == [[1, 10.5], [2, 20.0]]
    assert payload["n_rows"] == 2


def test_run_sql_csv_output(stub):
    out = server.run_sql("SELECT * FROM t", output_format="csv")
    lines = out.strip().splitlines()
    assert lines[0] == "id,amount"
    assert lines[1] == "1,10.5"


def test_run_sql_markdown_default(stub):
    out = server.run_sql("SELECT * FROM t")
    assert "id" in out and "10.5" in out and "|" in out


def test_scan_table_json_output(stub):
    out = server.scan_table("t", limit=5, output_format="json")
    payload = json.loads(out)
    assert payload["columns"] == ["id", "amount"]


def test_output_rows_cap_applies_to_json(monkeypatch):
    eng = SearchStubEngine(arrow=pa.table({"x": list(range(50))}))
    monkeypatch.setattr(server, "_handler", lambda: eng)
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "10")
    payload = json.loads(server.run_sql("SELECT * FROM t", output_format="json"))
    assert payload["n_rows"] == 10


# ------------------------------------------------------------- arrow output


def _arrow_ipc_table():
    """Fidelity table: markdown/csv re-render these as text, IPC does not."""
    import datetime
    from decimal import Decimal

    utc = datetime.UTC
    return pa.table(
        {
            "id": pa.array([1, 2, 3], type=pa.int64()),
            "amount": pa.array([Decimal("1.50"), None, Decimal("2.25")], type=pa.decimal128(10, 2)),
            "when": pa.array(
                [
                    datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=utc),
                    None,
                    datetime.datetime(2024, 2, 2, tzinfo=utc),
                ],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "kind": pa.array(["a", "b", None], type=pa.string()),
        }
    )


def _read_arrow_text(text):
    """Strip the header lines and decode the base64 IPC body back to a Table."""
    body = "".join(line for line in text.splitlines() if not line.startswith("#"))
    with pa.ipc.open_stream(io.BytesIO(base64.b64decode(body))) as reader:
        return reader.read_all()


def test_run_sql_arrow_output_roundtrip(stub):
    table = _arrow_ipc_table()
    stub._arrow = table
    out = server.run_sql("SELECT * FROM t", output_format="arrow")
    header = out.splitlines()[0]
    # header names the shape and byte size (the documented arrow format)
    assert header.startswith("# arrow: 3 rows x 4 cols, ")
    assert header.endswith(" bytes ipc-stream base64")
    # the payload round-trips EXACTLY — dtypes markdown/csv would flatten
    back = _read_arrow_text(out)
    assert back.equals(table)  # decimal128(10,2), timestamp[us, tz=UTC], NULLs


def test_run_sql_arrow_rows_match_json(stub):
    table = _arrow_ipc_table()
    stub._arrow = table
    arrow_back = _read_arrow_text(server.run_sql("SELECT * FROM t", output_format="arrow"))
    payload = json.loads(server.run_sql("SELECT * FROM t", output_format="json"))
    assert payload["columns"] == arrow_back.schema.names
    assert payload["n_rows"] == arrow_back.num_rows == 3
    # Same rows — but ONLY arrow keeps the dtypes: json flattens the decimal
    # to float (1.50 -> 1.5) and the timestamp to an isoformat string.
    import datetime
    from decimal import Decimal

    for arrow_row, json_row in zip(arrow_back.to_pylist(), payload["rows"]):
        zipped = dict(zip(payload["columns"], json_row))
        assert arrow_row["id"] == zipped["id"]
        assert (arrow_row["amount"] is None) == (zipped["amount"] is None)
        if arrow_row["amount"] is not None:
            assert float(arrow_row["amount"]) == zipped["amount"]  # Decimal -> float
        assert (arrow_row["when"] is None) == (zipped["when"] is None)
        if arrow_row["when"] is not None:
            assert arrow_row["when"].isoformat() == zipped["when"]  # datetime -> iso
    assert arrow_row["kind"] == zipped["kind"]
    assert isinstance(arrow_back.column("amount")[0].as_py(), Decimal)
    assert isinstance(arrow_back.column("when")[0].as_py(), datetime.datetime)
    # csv loses the fidelity too (decimal renders bare, NULL becomes empty)
    assert "1.50" not in server.run_sql("SELECT * FROM t", output_format="csv")


def test_run_sql_arrow_empty_result(stub):
    stub._arrow = pa.table({"a": pa.array([], type=pa.int64())})
    out = server.run_sql("SELECT * FROM t", output_format="arrow")
    assert out.splitlines()[0].startswith("# arrow: 0 rows x 1 cols, ")
    back = _read_arrow_text(out)
    assert back.num_rows == 0 and back.schema.field("a").type == pa.int64()


def test_run_sql_arrow_respects_output_rows_cap(monkeypatch):
    eng = SearchStubEngine(arrow=pa.table({"x": list(range(50))}))
    monkeypatch.setattr(server, "_handler", lambda: eng)
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "10")
    out = server.run_sql("SELECT * FROM t", output_format="arrow")
    assert out.splitlines()[0].startswith("# arrow: 10 rows x 1 cols, ")
    assert len(_read_arrow_text(out)) == 10
    assert "rows capped at SQLHANDLER_MAX_OUTPUT_ROWS" in out


def test_scan_table_arrow_output(stub):
    stub._arrow = _arrow_ipc_table()
    out = server.scan_table("t", limit=5, output_format="arrow")
    assert out.splitlines()[0].startswith("# arrow: 3 rows x 4 cols, ")
    assert _read_arrow_text(out).equals(_arrow_ipc_table())


def test_invalid_output_format_matches_error_shape(stub):
    text = server.run_sql("SELECT * FROM t", output_format="parquet")
    assert text.startswith("Error running SQL: Unsupported output_format 'parquet'")
    # same structured tail shape a bad table name produces (E_PARAM_INVALID)
    tail = json.loads(text.splitlines()[-1])
    assert tail["error"]["code"] == "E_PARAM_INVALID"
    assert "fix_hints" in tail["error"]
    scan_text = server.scan_table("t", output_format="nope")
    assert scan_text.startswith("Error scanning table: Unsupported output_format 'nope'")
    # case-insensitive, valid names still pass
    assert server.run_sql("SELECT * FROM t", output_format="ARROW").startswith("# arrow: ")


# ---------------------------------------------------------------- params


class ParamsEngine:
    def __init__(self, arrow):
        self._arrow = arrow
        self.last_params = None

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
        self.last_params = params
        return self._arrow


def test_run_sql_named_params_passthrough(monkeypatch):
    import pyarrow as pa

    eng = ParamsEngine(pa.table({"id": [2], "kind": ["b"]}))
    monkeypatch.setattr(server, "_handler", lambda: eng)
    out = server.run_sql("SELECT * FROM work_order WHERE kind = $k", params={"k": "b"})
    assert eng.last_params == {"k": "b"}
    assert "Error" not in out


def test_engine_params_named_and_positional(tmp_path):
    from sqlhandler.engine import SqlEngine

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2, 3], "kind": ["a", "b", "a"]}), d / "p.parquet")

    class P:
        kind = "fake"

        def list_tables(self, **kw):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    eng = SqlEngine(P())
    r1 = eng.query_duckdb("SELECT count(*) AS n FROM work_order WHERE kind = $k", params={"k": "a"})
    assert r1.to_pydict() == {"n": [2]}
    r2 = eng.query_duckdb(
        "SELECT count(*) AS n FROM work_order WHERE id > ? AND kind = ?", params=[1, "a"]
    )
    assert r2.to_pydict() == {"n": [1]}  # id 3 only (id 2 is kind b)
    # invalid params are refused with a clear error
    with pytest.raises(ValueError):
        eng.query_duckdb("SELECT 1", params="not-a-container")
    with pytest.raises(ValueError):
        eng.query_duckdb("SELECT 1", params={"x": {"nested": 1}})


def test_dispatcher_rejects_non_container_params():
    text, is_error = server._dispatch_tool("run_sql", {"sql": "SELECT 1", "params": "bad"})
    assert is_error is True
    assert "object or an array" in text


# --------------------------------------------------- arrow output: jobs + API


def test_async_job_arrow_result_roundtrip(tmp_path, monkeypatch):
    """The spooled Arrow table survives the fetch-once hand-over as IPC."""
    import time as _time

    from sqlhandler import jobs as jobs_module
    from sqlhandler.engine import SqlEngine

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(_arrow_ipc_table(), d / "part.parquet")

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    eng = SqlEngine(P(), cache_ttl=0)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    jobs_module.reset_job_manager()
    try:
        text, is_error = server._dispatch_tool("query_submit", {"sql": "SELECT * FROM work_order"})
        assert is_error is False, text
        job_id = json.loads(text)["job_id"]
        for _ in range(200):
            status = json.loads(server.query_status(job_id))
            if status["state"] != "running":
                break
            _time.sleep(0.02)
        assert status["state"] == "done", status

        text, is_error = server._dispatch_tool("query_result", {"job_id": job_id, "output_format": "arrow"})
        assert is_error is False, text
        assert text.splitlines()[0].startswith("# arrow: 3 rows x 4 cols, ")
        # Equal to the table the job actually spooled (the parquet scan may
        # relabel a tz field — same instants, the engine's own schema wins).
        expected = eng.query_duckdb("SELECT * FROM work_order")
        back = _read_arrow_text(text)
        assert back.schema.equals(expected.schema, check_metadata=False)
        assert back.equals(expected.cast(back.schema) if not back.equals(expected) else expected)
        # the fetch-once contract is untouched by the format
        text, is_error = server._dispatch_tool("query_result", {"job_id": job_id})
        assert is_error is True and "already fetched" in text
    finally:
        jobs_module.reset_job_manager()


def test_webui_api_query_arrow_format(tmp_path):
    """/api/query with {"format": "arrow"} returns the IPC text payload."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from starlette.applications import Starlette

    from sqlhandler.webui import register_ui

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(_arrow_ipc_table(), d / "part.parquet")

    from sqlhandler.config import FileConfig
    from sqlhandler.engine import SqlEngine
    from sqlhandler.file import FileProvider

    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    app = Starlette()
    register_ui(app, lambda: eng)
    client = TestClient(app)

    r = client.post("/api/query", json={"sql": "SELECT * FROM work_order", "format": "arrow"})
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert r.text.splitlines()[0].startswith("# arrow: 3 rows x 4 cols, ")
    # Equal to the engine's own result (schema from the scan, not the fixture)
    back = _read_arrow_text(r.text)
    assert back.equals(eng.query_duckdb("SELECT * FROM work_order"))

    # the default (no format) keeps the JSON shape
    payload = client.post("/api/query", json={"sql": "SELECT * FROM work_order"}).json()
    assert payload["columns"] == ["id", "amount", "when", "kind"]
    assert payload["rows"][0][1] == 1.5  # decimal renders as float in JSON
    # timestamp renders as iso text (tz label follows the environment; the
    # instant is the same one arrow keeps exact)
    when = payload["rows"][0][2]
    assert when.startswith("2024-01-01T") and when.endswith(("+00:00", "-05:00", "-04:00"))


def test_webui_api_export_arrow(tmp_path):
    """/api/export with format="arrow" downloads a readable IPC stream."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from starlette.applications import Starlette

    from sqlhandler.webui import register_ui

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(_arrow_ipc_table(), d / "part.parquet")

    from sqlhandler.config import FileConfig
    from sqlhandler.engine import SqlEngine
    from sqlhandler.file import FileProvider

    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    app = Starlette()
    register_ui(app, lambda: eng)
    client = TestClient(app)

    r = client.post("/api/export", json={"sql": "SELECT * FROM work_order", "format": "arrow"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.apache.arrow.stream"
    assert r.headers["content-disposition"] == 'attachment; filename="query.arrow"'
    with pa.ipc.open_stream(io.BytesIO(r.content)) as reader:
        assert reader.read_all().equals(eng.query_duckdb("SELECT * FROM work_order"))

    # an unsupported name keeps the historical refusal shape
    bad = client.post("/api/export", json={"sql": "SELECT 1", "format": "xlsx"})
    assert bad.status_code == 400
    assert "Unsupported export format" in bad.json()["error"]
