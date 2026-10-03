"""Tests for the async MCP query jobs (Wave 5 additive feature).

Covers the mission constraints: the job lifecycle (submit → status → result
→ cancel), the submit-time read-only guard (decision D2 — a DDL submission
is refused with the D2 error BEFORE any job exists), the SQLHANDLER_MAX_JOBS
registry cap with a clear refusal, the watchdog-enforced query timeout, and
the fetch-once-then-free result semantics — plus the /api/jobs/* REST twins.
"""

import json
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import jobs as jobs_module
from sqlhandler import policy as policy_module
from sqlhandler import server
from sqlhandler.engine import LakehouseError, SqlEngine
from sqlhandler.identity import Caller
from sqlhandler.jobs import JobError, McpJobManager
from sqlhandler.provider import TableInfo


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


def _patch_register(monkeypatch, sleep=None):
    """Replace schema registration (optionally with a slow one)."""
    import sqlhandler.engine as eng_mod

    def register(self, con, sql, version=None, **kw):
        if sleep is not None:
            time.sleep(sleep)
        con.register(
            "work_order", pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]})
        )

    monkeypatch.setattr(eng_mod.SqlEngine, "_register_schema", register)


@pytest.fixture(autouse=True)
def _fresh_manager(monkeypatch):
    """Every test starts with a clean, env-fresh job registry."""
    monkeypatch.delenv("SQLHANDLER_MAX_JOBS", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    policy_module.reset_policy_store()
    jobs_module.reset_job_manager()
    yield
    policy_module.reset_policy_store()
    jobs_module.reset_job_manager()


def _wait_done(mgr, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = mgr.status(job_id)
        if payload["state"] != "running":
            return payload
        time.sleep(0.02)
    return mgr.status(job_id)


# ------------------------------------------------------------------ lifecycle


def test_job_lifecycle_submit_status_result(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    submitted = mgr.submit(eng, "SELECT * FROM work_order WHERE kind = 'a'")
    assert submitted["state"] in ("running", "done")
    job_id = submitted["job_id"]

    status = _wait_done(mgr, job_id)
    assert status["state"] == "done"
    assert status["columns"] == ["id", "kind"]
    assert status["n_rows"] == 3
    assert status["result_fetched"] is False

    arrow = mgr.take_result(job_id)
    assert arrow.to_pydict() == {"id": [1, 3, 5], "kind": ["a", "a", "a"]}


def test_job_result_fetched_once_then_freed(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]
    _wait_done(mgr, job_id)
    first = mgr.take_result(job_id)
    assert first.num_rows == 5
    with pytest.raises(JobError, match="already fetched") as exc:
        mgr.take_result(job_id)
    assert exc.value.status == 409
    # status still works and reports the hand-over honestly
    status = mgr.status(job_id)
    assert status["result_fetched"] is True
    assert status["state"] == "done"
    # the spooled table is really gone from the registry
    assert mgr.get(job_id).job._result is None


def test_job_status_unknown_id_404(tmp_path):
    _make_engine(tmp_path)
    mgr = McpJobManager()
    with pytest.raises(JobError, match="Unknown job id") as exc:
        mgr.status("nope")
    assert exc.value.status == 404
    with pytest.raises(JobError) as exc:
        mgr.cancel("nope")
    assert exc.value.status == 404
    with pytest.raises(JobError) as exc:
        mgr.take_result("nope")
    assert exc.value.status == 404


def test_job_cancel_running(tmp_path, monkeypatch):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]
    time.sleep(0.1)
    result = mgr.cancel(job_id)
    assert result["interrupted"] is True
    assert _wait_done(mgr, job_id)["state"] == "cancelled"
    with pytest.raises(JobError, match="cancelled"):
        mgr.take_result(job_id)


# --------------------------------------------------------- read-only at submit


def test_ddl_refused_at_submit_with_d2_error(tmp_path):
    """The D2 guard runs at SUBMIT time: refused before any job exists."""
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    with pytest.raises(ValueError, match="SQLHANDLER_MCP_READONLY") as exc:
        mgr.submit(eng, "CREATE TABLE x (a int)")
    assert "CREATE" in str(exc.value)
    assert mgr.stats()["tracked"] == 0  # nothing was ever started

    with pytest.raises(ValueError, match="SQLHANDLER_MCP_READONLY"):
        mgr.submit(eng, "ATTACH 'file.db' AS exfil; SELECT 1")
    with pytest.raises(ValueError, match="SQLHANDLER_MCP_READONLY"):
        mgr.submit(eng, "INSERT INTO work_order VALUES (1)")
    assert mgr.stats()["tracked"] == 0


def test_ddl_refused_via_mcp_tool_with_d2_error(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, is_error = server._dispatch_tool("query_submit", {"sql": "DROP TABLE work_order"})
    assert is_error is True
    assert "SQLHANDLER_MCP_READONLY" in text
    assert jobs_module.job_manager().stats()["tracked"] == 0


def test_readonly_optout_applies_to_jobs(tmp_path, monkeypatch):
    """SQLHANDLER_MCP_READONLY=0 (the D2 escape hatch) reaches jobs too —
    the same posture as run_sql: the guard no longer refuses at submit
    (execution is the engine's business), but garbage SQL still fails the
    parse check."""
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    submitted = mgr.submit(eng, "CREATE TEMP TABLE tmp_x AS SELECT 1 AS a")  # noqa: DUO123 - opt-out posture under test
    assert "job_id" in submitted  # no D2 ValueError: the opt-out reached the guard
    mgr.cancel(submitted["job_id"])
    # garbage SQL still refused (parse check with the opt-out)
    with pytest.raises(ValueError, match="Could not parse"):
        mgr.submit(eng, "not even sql")


def test_invalid_params_refused_at_submit(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    with pytest.raises(ValueError, match="params"):
        mgr.submit(eng, "SELECT 1", params={"x": [1, 2]})  # nested container
    with pytest.raises(ValueError, match="non-negative integer"):
        mgr.submit(eng, "SELECT 1", version_as_of="zero")  # submit-time client error
    assert mgr.stats()["tracked"] == 0


# ------------------------------------------------------------------- timeout


def test_job_timeout_watchdog(tmp_path, monkeypatch):
    """The query timeout is enforced even when nobody polls the job."""
    monkeypatch.setenv("SQLHANDLER_QUERY_TIMEOUT", "0.2")
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]
    status = _wait_done(mgr, job_id, timeout=5.0)
    assert status["state"] == "error"
    assert status["timed_out"] is True
    assert "timed out" in status["error"]
    assert "SQLHANDLER_QUERY_TIMEOUT" in status["error"]


def test_job_timeout_message_matches_sync_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_QUERY_TIMEOUT", "0.2")
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]
    _wait_done(mgr, job_id, timeout=5.0)
    with pytest.raises(JobError) as exc:
        mgr.take_result(job_id)
    assert "Query timed out after 0.2s (SQLHANDLER_QUERY_TIMEOUT) and was cancelled." in str(
        exc.value
    )
    # ...the exact text the synchronous path raises:
    eng2 = _make_engine(tmp_path)
    with pytest.raises(
        LakehouseError, match="Query timed out after 0.2s \\(SQLHANDLER_QUERY_TIMEOUT\\)"
    ):
        eng2.query_duckdb("SELECT * FROM work_order")


# ------------------------------------------------------------- registry cap


def test_max_jobs_refusal(tmp_path, monkeypatch):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = McpJobManager(max_jobs=1)
    first = mgr.submit(eng, "SELECT * FROM work_order")
    second = mgr.submit(eng, "SELECT * FROM work_order")
    assert second["status"] == 429
    assert "SQLHANDLER_MAX_JOBS" in second["error"]
    assert "cancel or fetch results" in second["error"]
    mgr.cancel(first["job_id"])


def test_max_jobs_default_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS", "3")
    assert jobs_module.max_jobs_env() == 3
    assert McpJobManager()._max_jobs == 3
    # garbage / non-positive → the safe default (bounded by design)
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS", "garbage")
    assert jobs_module.max_jobs_env() == 8
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS", "0")
    assert jobs_module.max_jobs_env() == 8
    assert McpJobManager()._max_jobs == 8


def test_finished_jobs_evicted_to_make_room(tmp_path, monkeypatch):
    """Finished jobs age out (TTL 0 = size-cap only): quick sequential jobs
    never hit the cap."""
    monkeypatch.setenv("SQLHANDLER_ASYNC_JOB_TTL", "0")
    eng = _make_engine(tmp_path)
    mgr = McpJobManager(max_jobs=2)
    ids = []
    for _ in range(4):
        r = mgr.submit(eng, "SELECT count(*) AS n FROM work_order")
        assert "job_id" in r, r
        job_id = r["job_id"]
        _wait_done(mgr, job_id)
        ids.append(job_id)
    assert len(mgr._records) <= 2
    assert ids[-1] in mgr._records


def test_registry_full_of_running_jobs_refuses(tmp_path, monkeypatch):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = McpJobManager(max_jobs=2)
    a = mgr.submit(eng, "SELECT * FROM work_order")
    b = mgr.submit(eng, "SELECT * FROM work_order")
    c = mgr.submit(eng, "SELECT * FROM work_order")
    assert c["status"] == 429
    mgr.cancel(a["job_id"])
    mgr.cancel(b["job_id"])


def test_singleton_registry_shared_and_resettable():
    jobs_module.reset_job_manager()
    one = jobs_module.job_manager()
    assert jobs_module.job_manager() is one
    jobs_module.reset_job_manager()
    assert jobs_module.job_manager() is not one


# ------------------------------------------------------------- MCP tools


def test_mcp_query_tools_roundtrip(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, is_error = server._dispatch_tool(
        "query_submit",
        {"sql": "SELECT * FROM work_order WHERE kind = $k", "params": {"k": "b"}},
    )
    assert is_error is False
    payload = json.loads(text)
    assert payload["state"] in ("running", "done")
    job_id = payload["job_id"]

    text, is_error = server._dispatch_tool("query_status", {"job_id": job_id})
    assert is_error is False
    status = json.loads(text)
    for _ in range(200):
        status = json.loads(server.query_status(job_id))
        if status["state"] != "running":
            break
        time.sleep(0.02)
    assert status["state"] == "done"
    assert status["n_rows"] == 2

    text, is_error = server._dispatch_tool(
        "query_result", {"job_id": job_id, "output_format": "json"}
    )
    assert is_error is False
    rows = json.loads(text)
    assert rows["rows"] == [[2, "b"], [4, "b"]]
    assert rows["n_rows"] == 2  # MCP rendering uses the run_sql payload shape

    text, is_error = server._dispatch_tool("query_result", {"job_id": job_id})
    assert is_error is True
    assert "already fetched" in text

    # bad params shape at the tool boundary
    text, is_error = server._dispatch_tool("query_submit", {"sql": "SELECT 1", "params": "bad"})
    assert is_error is True
    assert "params must be an object or an array" in text


def test_mcp_query_cancel_tool(tmp_path, monkeypatch):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    job_id = json.loads(server.query_submit("SELECT * FROM work_order"))["job_id"]
    time.sleep(0.1)
    text, is_error = server._dispatch_tool("query_cancel", {"job_id": job_id})
    assert is_error is False
    assert json.loads(text)["interrupted"] is True
    for _ in range(200):
        if json.loads(server.query_status(job_id))["state"] != "running":
            break
        time.sleep(0.02)
    assert json.loads(server.query_status(job_id))["state"] == "cancelled"


# ------------------------------------------------------------- REST /api/jobs


@pytest.fixture
def rest_client(tmp_path, monkeypatch):
    """The real HTTP app with the jobs routes + a token middleware."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import (
        _ApiTokenMiddleware,
        _transport_security,
    )
    from sqlhandler.server import (
        mcp as mcp_server,
    )

    eng = _make_engine(tmp_path)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    from sqlhandler.webui import register_ui

    register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    return TestClient(app), {"X-API-Token": "tok-123"}


def test_rest_jobs_flow(rest_client):
    client, auth = rest_client
    r = client.post(
        "/api/jobs", json={"sql": "SELECT * FROM work_order WHERE kind = 'a'"}, headers=auth
    )
    assert r.status_code == 200
    body = r.json()
    job_id = body["job_id"]
    assert body["state"] in ("running", "done")

    # poll status
    for _ in range(200):
        status = client.get(f"/api/jobs/{job_id}", headers=auth).json()
        if status["state"] != "running":
            break
        time.sleep(0.02)
    assert status["state"] == "done"
    assert status["n_rows"] == 3

    # result: handed over once, then freed (409 on the second fetch)
    r = client.get(f"/api/jobs/{job_id}/result", headers=auth)
    assert r.status_code == 200
    payload = r.json()
    assert payload["columns"] == ["id", "kind"]
    assert payload["total_rows"] == 3
    assert payload["result_fetched"] is True
    r2 = client.get(f"/api/jobs/{job_id}/result", headers=auth)
    assert r2.status_code == 409
    assert "already fetched" in r2.json()["error"]


def test_rest_jobs_ddl_refused_at_submit(rest_client):
    client, auth = rest_client
    r = client.post("/api/jobs", json={"sql": "CREATE TABLE x (a int)"}, headers=auth)
    assert r.status_code == 400
    assert "SQLHANDLER_MCP_READONLY" in r.json()["error"]


def test_rest_jobs_cancel_and_unknown(rest_client, monkeypatch):
    client, auth = rest_client
    _patch_register(monkeypatch, sleep=1.0)
    job_id = client.post(
        "/api/jobs", json={"sql": "SELECT * FROM work_order"}, headers=auth
    ).json()["job_id"]
    time.sleep(0.1)
    r = client.delete(f"/api/jobs/{job_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["interrupted"] is True
    assert client.delete("/api/jobs/nope", headers=auth).status_code == 404
    assert client.get("/api/jobs/nope", headers=auth).status_code == 404
    assert client.get("/api/jobs/nope/result", headers=auth).status_code == 404


def test_rest_jobs_cap_refusal_429(tmp_path, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import register_ui

    monkeypatch.setenv("SQLHANDLER_MAX_JOBS", "1")
    jobs_module.reset_job_manager()
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok")
    client = TestClient(app)
    auth = {"X-API-Token": "tok"}
    first = client.post("/api/jobs", json={"sql": "SELECT * FROM work_order"}, headers=auth).json()
    r = client.post("/api/jobs", json={"sql": "SELECT * FROM work_order"}, headers=auth)
    assert r.status_code == 429
    assert "SQLHANDLER_MAX_JOBS" in r.json()["error"]
    client.delete(f"/api/jobs/{first['job_id']}", headers=auth)


# ------------------------------------------------------- ownership scoping

# The verified finding: an async job had NO owner — anyone with a job_id
# could read the result (destructively: fetch is once-only) or cancel
# someone else's job. Under policy enforcement the job now records the
# submitter's owner scope (the SAME policy.owner_key derivation the
# saved-query store uses) and foreign callers get the unknown-id 404 —
# indistinguishable from a bogus id, so a wrong guess learns nothing.


ALICE = Caller(cls="user", subject="alice", key_fp=None, via="relay")
MALLORY = Caller(cls="user", subject="mallory", key_fp=None, via="relay")
KEY_CALLER = Caller(cls="key", subject=None, key_fp="sha256:dddddddddddd", via="key")


@pytest.fixture()
def enforcement_on(tmp_path, monkeypatch):
    """Policy enforcement ON with a minimal allow-everything policy."""
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps(
            {
                "version": 1,
                "default_group": "open",
                "groups": {"open": {"visible_tables": ["workorder/*"]}},
            }
        )
    )
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(pf))
    policy_module.reset_policy_store()
    yield pf
    policy_module.reset_policy_store()


def test_owned_job_readable_by_owner_not_by_other(tmp_path, enforcement_on):
    """Owner-submitted job: the owner polls/fetches; a different caller's
    status/result/cancel all get the 404-shaped unknown-id refusal."""
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    submitted = mgr.submit(eng, "SELECT * FROM work_order WHERE kind = 'a'", owner="subject:alice")
    job_id = submitted["job_id"]

    # the owner's own flow is untouched
    status = _wait_done_owner(mgr, job_id, "subject:alice")
    assert status["state"] == "done"
    assert status["n_rows"] == 3
    arrow = mgr.take_result(job_id, owner="subject:alice")
    assert arrow.num_rows == 3

    # a different subject cannot even see it exists
    for call in (
        lambda: mgr.status(job_id, owner="subject:mallory"),
        lambda: mgr.take_result(job_id, owner="subject:mallory"),
        lambda: mgr.cancel(job_id, owner="subject:mallory"),
    ):
        with pytest.raises(JobError, match="Unknown job id") as exc:
            call()
        assert exc.value.status == 404


def _wait_done_owner(mgr, job_id, owner, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = mgr.status(job_id, owner=owner)
        if payload["state"] != "running":
            return payload
        time.sleep(0.02)
    return mgr.status(job_id, owner=owner)


def test_owned_job_cross_owner_cannot_cancel_or_burn_fetch(tmp_path, enforcement_on, monkeypatch):
    """The cross-owner refusals happen BEFORE any effect: a foreign cancel
    leaves the job running, and a foreign fetch does not consume the
    once-only hand-over."""
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order", owner="subject:alice")["job_id"]
    time.sleep(0.1)

    with pytest.raises(JobError, match="Unknown job id"):
        mgr.cancel(job_id, owner="subject:mallory")
    with pytest.raises(JobError, match="Unknown job id"):
        mgr.take_result(job_id, owner="subject:mallory")
    # neither side-effect happened: still running, result still unfetched
    assert mgr.status(job_id, owner="subject:alice")["state"] == "running"
    mgr.cancel(job_id, owner="subject:alice")
    assert _wait_done_owner(mgr, job_id, "subject:alice")["state"] == "cancelled"


def test_legacy_unowned_record_still_accessible(tmp_path, enforcement_on):
    """Backward compat: an owner=None record (submitted before enforcement
    was on, or by the REST layer with no resolved caller) is accessible to
    every caller — old in-flight jobs must not break."""
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order WHERE kind = 'b'")["job_id"]  # no owner
    assert mgr._records[job_id].owner is None

    status = _wait_done(mgr, job_id)
    assert status["state"] == "done"
    # alice, mallory, a key-fp caller and an unowned poll all read it
    assert mgr.status(job_id, owner="subject:alice")["n_rows"] == 2
    assert mgr.status(job_id, owner="subject:mallory")["state"] == "done"
    assert mgr.status(job_id, owner="key:sha256:dddddddddddd")["result_fetched"] is False
    arrow = mgr.take_result(job_id, owner="subject:mallory")
    assert arrow.num_rows == 2


def test_owner_scope_uses_full_key_not_prefix(tmp_path, enforcement_on):
    """The comparison is exact equality on the owner scope: a caller whose
    key fingerprint merely shares a prefix with the owner's cannot read."""
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order", owner="key:sha256:dddddddddddd")["job_id"]
    _wait_done_owner(mgr, job_id, "key:sha256:dddddddddddd")
    with pytest.raises(JobError, match="Unknown job id"):
        mgr.status(job_id, owner="key:sha256:ddddddddddX")


def test_enforcement_off_derives_no_owner(tmp_path, monkeypatch):
    """Enforcement OFF (the default): the server layer derives NO owner
    (byte-identical shared behavior), and jobs.py itself stores whatever it
    is given — never deriving or filtering identities on its own."""
    from sqlhandler.server import _job_owner

    eng = _make_engine(tmp_path)
    mgr = jobs_module.job_manager()  # the singleton query_submit submits into
    # the caller layer (what query_submit actually passes) derives nothing:
    assert _job_owner(ALICE) is None
    # a submit through the MCP tool therefore records no owner...
    monkeypatch.setattr(server, "_handler", lambda: eng)
    job_id = json.loads(server.query_submit("SELECT * FROM work_order"))["job_id"]
    assert mgr._records[job_id].owner is None
    status = _wait_done(mgr, job_id)
    assert status["state"] == "done"
    assert mgr.take_result(job_id, owner="subject:mallory").num_rows == 5
    # ...while a DIRECT jobs.py caller may still store an explicit owner
    # (store-what's-given contract; jobs.py never derives one itself):
    other = mgr.submit(eng, "SELECT * FROM work_order", owner="subject:alice")
    assert mgr._records[other["job_id"]].owner == "subject:alice"
    mgr.cancel(other["job_id"], owner="subject:alice")


def test_list_jobs_owner_scoped(tmp_path, enforcement_on):
    """list_jobs: an owner sees only its OWN jobs (with truncated SQL);
    nobody's list ever includes another owner's jobs."""
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    long_sql = "SELECT * FROM work_order WHERE kind = '" + "x" * 200 + "'"
    alice_1 = mgr.submit(eng, "SELECT * FROM work_order WHERE kind = 'a'", owner="subject:alice")["job_id"]
    alice_2 = mgr.submit(eng, long_sql, owner="subject:alice")["job_id"]
    mallory_1 = mgr.submit(eng, "SELECT count(*) AS n FROM work_order", owner="subject:mallory")["job_id"]
    unowned = mgr.submit(eng, "SELECT 1")["job_id"]  # legacy/unowned

    alice_jobs = {j["job_id"]: j for j in mgr.list_jobs(owner="subject:alice")}
    # alice sees her OWN jobs plus the unowned (legacy/shared-state) record —
    # never another owner's: mallory_1 is absent from her list.
    assert set(alice_jobs) == {alice_1, alice_2, unowned}
    assert mallory_1 not in alice_jobs
    assert all(j["state"] in ("running", "done") for j in alice_jobs.values())
    assert all(len(j["sql"]) <= 120 for j in alice_jobs.values())
    assert alice_jobs[alice_2]["sql"].endswith("…")

    mallory_ids = {j["job_id"] for j in mgr.list_jobs(owner="subject:mallory")}
    assert mallory_ids == {mallory_1, unowned}
    assert alice_1 not in mallory_ids and alice_2 not in mallory_ids
    # enforcement off / no owner scope: everything tracked (shared posture)
    assert {j["job_id"] for j in mgr.list_jobs()} == {alice_1, alice_2, mallory_1, unowned}


def test_mcp_dispatch_owner_scoped_end_to_end(tmp_path, enforcement_on, monkeypatch):
    """The full MCP path: the dispatch resolves the caller, derives the SAME
    owner scope as the saved-query gate, and the four handlers enforce it —
    alice's job is invisible to mallory's tool calls, legible to alice's."""
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)

    def _dispatch(name, args, caller):
        # Mirror _dispatch_tool's caller resolution without HTTP machinery.
        import sqlhandler.server as srv

        original = srv._identity.caller_from_request_state
        monkeypatch.setattr(srv._identity, "caller_from_request_state", lambda request: caller)
        try:
            return srv._dispatch_tool(name, args)
        finally:
            monkeypatch.setattr(srv._identity, "caller_from_request_state", original)

    text, is_error = _dispatch("query_submit", {"sql": "SELECT * FROM work_order WHERE kind = 'a'"}, ALICE)
    assert is_error is False
    job_id = json.loads(text)["job_id"]

    # owner reads it
    text, is_error = _dispatch("query_status", {"job_id": job_id}, ALICE)
    assert is_error is False
    assert json.loads(text)["sql"] == "SELECT * FROM work_order WHERE kind = 'a'"

    # a different caller gets the unknown-id shape (an error result, not a leak)
    text, is_error = _dispatch("query_status", {"job_id": job_id}, MALLORY)
    assert is_error is True
    assert "Unknown job id" in text
    text, is_error = _dispatch("query_result", {"job_id": job_id}, MALLORY)
    assert is_error is True and "Unknown job id" in text
    text, is_error = _dispatch("query_cancel", {"job_id": job_id}, MALLORY)
    assert is_error is True and "Unknown job id" in text

    # alice can still complete the flow
    for _ in range(200):
        if json.loads(server.query_status(job_id, caller=ALICE))["state"] != "running":
            break
        time.sleep(0.02)
    text, is_error = _dispatch("query_result", {"job_id": job_id, "output_format": "json"}, ALICE)
    assert is_error is False
    assert json.loads(text)["n_rows"] == 3
    # the once-only hand-over was NOT burned by mallory's refused fetch
    text, is_error = _dispatch("query_result", {"job_id": job_id}, ALICE)
    assert is_error is True and "already fetched" in text


def test_mcp_dispatch_stdio_unowned_jobs_shared(tmp_path, monkeypatch):
    """No enforcement + no caller (stdio): the four handlers keep the
    historical shared behavior (dispatch passes owner=None; nothing owned)."""
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    job_id = json.loads(server.query_submit("SELECT * FROM work_order WHERE kind = 'b'"))["job_id"]
    for _ in range(200):
        if json.loads(server.query_status(job_id))["state"] != "running":
            break
        time.sleep(0.02)
    text, is_error = server._dispatch_tool("query_status", {"job_id": job_id})
    assert is_error is False and json.loads(text)["n_rows"] == 2
    text, is_error = server._dispatch_tool("query_result", {"job_id": job_id, "output_format": "json"})
    assert is_error is False and json.loads(text)["n_rows"] == 2


def test_owner_key_derivation_matches_saved_query_gate(enforcement_on):
    """server._job_owner IS the saved-query derivation: subject first, else
    key fingerprint, and None whenever enforcement is off."""
    from sqlhandler.server import _job_owner

    assert _job_owner(ALICE) == policy_module.owner_key(ALICE) == "subject:alice"
    assert _job_owner(KEY_CALLER) == policy_module.owner_key(KEY_CALLER)
    assert _job_owner(None) is None
