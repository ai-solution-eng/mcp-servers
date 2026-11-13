"""Dispatch/HTTP-level policy enforcement tests (the identity-spine wiring).

The engine-level leak suite (test_policy_leaks.py) proves the cache and view
invariants WHEN a caller is passed explicitly. This suite proves the other
half: every MCP tool and web route actually PASSES the resolved caller into
the engine -- the wiring gap that let masked callers see raw rows through
run_sql / list_tables / describe_table / ... while the engine tests passed.

Covered here:
* every data tool in server._dispatch_tool forwards caller=;
* run_sql through the dispatcher masks AND keys the result cache per policy;
* catalog tools hide policy-hidden tables; profile/scan read the masked view;
* saved-query tools are owner-scoped through the dispatcher;
* the ambient contextvar backstop (bound by the identity middleware) works;
* the web UI /api/query route threads the request caller.
"""

import asyncio
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import engine as engine_module
from sqlhandler import identity, server, webui
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import DataProvider, TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="payroll", schema="payroll", format="parquet"),
]

SQL = "SELECT id, ssn, amount, kind FROM work_order ORDER BY id"


class PolicyProvider(DataProvider):
    """Fake provider with a masked table and a policy-hidden one."""

    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.versions = {}

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return "fake://" + info.path

    def open_dataset(self, info, version=None):
        import pyarrow.dataset as pad

        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return self.versions.get(info.path)


def _seed(root):
    for schema, name in (("workorder", "work_order"), ("payroll", "payroll")):
        d = root / schema / name
        d.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "id": [0, 1, 2],
                    "ssn": ["ssn-000", "ssn-001", "ssn-002"],
                    "amount": [10.0, 20.0, 30.0],
                    "kind": ["a", "secret", "a"],
                }
            ),
            d / "part.parquet",
        )


ALICE = identity.Caller(cls="user", subject="alice", key_fp=None, via="relay")
ANON = identity.ANONYMOUS


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    """Enforcement on: analysts see workorder/* only, masked + row-filtered;
    the default group is analysts (so anonymous is masked too)."""
    from sqlhandler.policy import reset_policy_store

    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "analysts",
                "groups": {
                    "analysts": {
                        "visible_tables": ["workorder/*"],
                        "tables": {
                            "workorder/work_order": {
                                "row_filter": "kind != 'secret'",
                                "column_masks": {"ssn": "redact"},
                            }
                        },
                    },
                    "everything": {"visible_tables": ["workorder/*", "payroll/*"]},
                },
                "subjects": {"alice": ["analysts"]},
            }
        )
    )
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(pf))
    reset_policy_store()
    yield pf
    reset_policy_store()


@pytest.fixture()
def eng(tmp_path, policy_env, monkeypatch):
    root = tmp_path / "data"
    _seed(root)
    engine = SqlEngine(PolicyProvider(root), cache_ttl=3600, dataset_cache_ttl=0)
    monkeypatch.setattr(server, "_handler", lambda: engine)
    return engine


@pytest.fixture()
def as_alice(monkeypatch):
    """Resolve every request as alice (the dispatch path's caller source)."""
    monkeypatch.setattr(server._identity, "caller_from_request_state", lambda request: ALICE)
    return ALICE


@pytest.fixture()
def saved_store(tmp_path, monkeypatch):
    """An isolated saved-query store in single-user-local write mode."""
    from sqlhandler import saved as saved_module

    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved-queries.json"))
    for env in ("SQLHANDLER_API_TOKEN", "MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        monkeypatch.delenv(env, raising=False)
    saved_module.reset_saved_query_store()
    yield
    saved_module.reset_saved_query_store()


# ---------------------------------------------------------------------------
# structural: EVERY data tool forwards the caller
# ---------------------------------------------------------------------------

_DISPATCH_CALLS = {
    "list_tables": {},
    "search_tables": {"query": "work order"},
    "describe_table": {"table": "work_order"},
    "profile_table": {"table": "work_order"},
    "column_stats": {"table": "work_order", "column": "id"},
    "sample_rows": {"table": "work_order"},
    "run_sql": {"sql": "SELECT 1"},
    "scan_table": {"table": "work_order"},
    "explain_query": {"sql": "SELECT 1"},
    "ask_data": {"question": "work orders"},
    "whoami": {},
    "query_submit": {"sql": "SELECT 1"},
    "query_status": {"job_id": "x"},
    "query_result": {"job_id": "x"},
    "query_cancel": {"job_id": "x"},
    "query_save": {"name": "q", "sql": "SELECT 1"},
    "query_list": {},
    "query_delete": {"name": "q"},
    "query_saved": {"name": "q"},
}


def test_dispatch_forwards_caller_to_every_data_tool(eng, as_alice, monkeypatch):
    """The structural guard: no data tool may be dispatched without caller=.

    This is the regression test for the original wiring gap -- the engine
    tests all passed while these tools silently dropped the caller.
    """
    calls = {}

    def _spy(name):
        def _call(*args, **kwargs):
            calls[name] = kwargs.get("caller")
            return "ok"

        return _call

    for name in _DISPATCH_CALLS:
        monkeypatch.setattr(server, name, _spy(name))

    for name, args in _DISPATCH_CALLS.items():
        text, is_error = server._dispatch_tool(name, args)
        assert (text, is_error) == ("ok", False), name

    assert set(calls) == set(_DISPATCH_CALLS)
    missing = sorted(name for name, caller in calls.items() if caller is not ALICE)
    assert not missing, "tools dispatched without the resolved caller: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# run_sql: masking + policy-keyed cache through the dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_run_sql_masks_and_keys_cache_per_policy(eng, as_alice):
    text, is_error = server._dispatch_tool("run_sql", {"sql": SQL})
    assert is_error is False
    assert "ssn-000" not in text, "raw masked column leaked through MCP run_sql"
    assert "secret" not in text, "row filter not applied through MCP run_sql"
    assert len([line for line in text.splitlines() if "***" in line]) == 2

    # The cached entry is the POLICY-keyed one, not the unmasked key.
    from sqlhandler.server import _fetch_limit

    limit = _fetch_limit(None, 0)
    expected = eng._result_cache_key(SQL, None, limit, None, None, caller=ALICE)
    unmasked = eng._result_cache_key(SQL, None, limit, None, None, caller=None)
    assert list(eng._result_cache) == [expected]
    assert expected != unmasked

    # A second identical call is an L1 hit on that same entry.
    before = eng.cache_stats()["result_cache"]
    server._dispatch_tool("run_sql", {"sql": SQL})
    after = eng.cache_stats()["result_cache"]
    assert after["hits"] == before["hits"] + 1
    assert after["writes"] == before["writes"]


def test_dispatch_ambient_context_backstop(eng, monkeypatch):
    """A caller=-less engine call still enforces when the middleware slot is
    bound (the fail-closed backstop for any future unwired path)."""
    token = engine_module.set_current_caller(ALICE)
    try:
        text = server.run_sql(SQL)
    finally:
        engine_module.reset_current_caller(token)
    assert "ssn-000" not in text and "***" in text


def test_identity_middleware_binds_engine_caller():
    """The middleware publishes the resolved Caller to the ENGINE contextvar
    (not just the audit one), so to_thread dispatch bodies can see it."""
    seen = {}

    async def app(scope, receive, send):
        seen["caller"] = engine_module.current_caller()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _send(message):
        return None

    middleware = server._CallerIdentityMiddleware(app)
    asyncio.run(middleware({"type": "http", "state": {}, "headers": []}, None, _send))

    assert isinstance(seen["caller"], identity.Caller)
    assert seen["caller"].is_anonymous


# ---------------------------------------------------------------------------
# catalog / metadata / scan tools
# ---------------------------------------------------------------------------


def test_dispatch_catalog_tools_hide_policy_hidden_tables(eng, as_alice):
    text, _ = server._dispatch_tool("list_tables", {})
    assert "work_order" in text
    assert "payroll" not in text, "hidden table advertised by list_tables"

    text, _ = server._dispatch_tool("describe_table", {"table": "payroll"})
    assert text.startswith("Error describing table:")
    assert "not found" in text.lower()

    text, _ = server._dispatch_tool("search_tables", {"query": "payroll"})
    assert "No tables match" in text

    text, _ = server._dispatch_tool("scan_table", {"table": "payroll"})
    assert text.startswith("Error scanning table:")

    text, _ = server._dispatch_tool("column_stats", {"table": "payroll", "column": "id"})
    assert text.startswith("Error computing column stats:")

    text, _ = server._dispatch_tool("ask_data", {"question": "payroll totals"})
    assert "No tables matched" in text, "ask_data planned over a hidden table"


def test_dispatch_profile_and_scan_read_the_masked_view(eng, as_alice):
    text, _ = server._dispatch_tool("profile_table", {"table": "work_order"})
    assert "ssn-000" not in text, "profile saw the raw masked column"
    assert "***" in text

    text, _ = server._dispatch_tool("scan_table", {"table": "work_order", "limit": 10})
    assert "ssn-000" not in text, "scan returned the raw masked column"
    assert "***" in text

    text, _ = server._dispatch_tool("column_stats", {"table": "work_order", "column": "ssn"})
    assert text.startswith("Error computing column stats:"), "column_stats must refuse a masked column"
    assert "masked" in text.lower() or "does not exist" in text.lower()


# ---------------------------------------------------------------------------
# saved queries: owner-scoped through the dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_saved_query_is_owner_scoped(eng, as_alice, saved_store):
    text, is_error = server._dispatch_tool("query_save", {"name": "masked-q", "sql": SQL})
    assert is_error is False and "saved" in text.lower()

    text, is_error = server._dispatch_tool("query_saved", {"name": "masked-q"})
    assert is_error is False
    assert "ssn-000" not in text and "***" in text

    # Another identity cannot even see the entry, let alone run it.
    server._identity.caller_from_request_state = lambda request: ANON
    text, is_error = server._dispatch_tool("query_saved", {"name": "masked-q"})
    assert is_error is True
    assert "Unknown saved query" in text


# ---------------------------------------------------------------------------
# web UI /api/query
# ---------------------------------------------------------------------------


def test_webui_query_route_scopes_to_request_caller(eng, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from starlette.applications import Starlette

    monkeypatch.setattr(identity, "caller_from_scope", lambda scope: ALICE)

    app = Starlette()
    webui.register_ui(app, lambda: eng)
    app.add_middleware(server._CallerIdentityMiddleware)
    client = TestClient(app)

    response = client.post("/api/query", json={"sql": SQL})
    assert response.status_code == 200, response.text
    payload = response.json()
    ssn_idx = payload["columns"].index("ssn")
    assert [row[ssn_idx] for row in payload["rows"]] == ["***", "***"]
    assert len(payload["rows"]) == 2


def test_webui_jobs_route_forwards_caller_and_scope(eng, monkeypatch):
    """The /api/jobs REST twins must ride the caller on the JOB (masking views,
    policy-keyed cache space, per-caller query gate), not just the owner scope."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from starlette.applications import Starlette

    from sqlhandler import limits as limits_module

    monkeypatch.setattr(identity, "caller_from_scope", lambda scope: ALICE)
    captured = {}

    def spy_submit(engine, body, *, caller=None, owner=None, caller_scope=None):
        captured.update(caller=caller, owner=owner, caller_scope=caller_scope)
        return {"job_id": "j1", "state": "running"}

    monkeypatch.setattr(webui, "api_job_submit", spy_submit)

    app = Starlette()
    webui.register_ui(app, lambda: eng)
    app.add_middleware(server._CallerIdentityMiddleware)
    client = TestClient(app)

    response = client.post("/api/jobs", json={"sql": SQL})
    assert response.status_code == 200, response.text
    assert captured["caller"] is ALICE, "the job must receive the resolved caller"
    assert captured["caller_scope"] == limits_module.gate_scope(ALICE)
    assert captured["owner"] == "subject:alice"


def test_webui_async_query_route_forwards_owner_and_scope(eng, monkeypatch):
    """The older /api/query/async route must ride caller + owner + the
    identity-keyed fairness bucket onto the job registry."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from starlette.applications import Starlette

    from sqlhandler import limits as limits_module

    monkeypatch.setattr(identity, "caller_from_scope", lambda scope: ALICE)
    captured = {}

    def spy_submit(engine, manager, body, *, caller=None, owner=None, caller_scope=None):
        captured.update(caller=caller, owner=owner, caller_scope=caller_scope)
        return {"query_id": "q1", "state": "running"}

    monkeypatch.setattr(webui, "api_async_query", spy_submit)

    app = Starlette()
    webui.register_ui(app, lambda: eng)
    app.add_middleware(server._CallerIdentityMiddleware)
    client = TestClient(app)

    response = client.post("/api/query/async", json={"sql": SQL})
    assert response.status_code == 200, response.text
    assert captured["caller"] is ALICE
    assert captured["owner"] == "subject:alice"
    assert captured["caller_scope"] == limits_module.gate_scope(ALICE)
