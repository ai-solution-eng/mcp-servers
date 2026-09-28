"""Tests for the cross-replica shared job store (SQLHANDLER_JOBS_DIR).

The outage this pins down (live-seen 2026-09, 4-replica release): the job
registry is process-local, so a client that submits on one replica and polls
on another got "Unknown job id" from ~3 of 4 status/result calls. The fix
publishes finished jobs to a directory every replica shares; these tests
simulate two replicas as two McpJobManager instances over the same directory
(the registry is per-process, exactly like two pods).
"""

import json
import os
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import jobs as jobs_module
from sqlhandler.engine import SqlEngine
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
        con.register("work_order", pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]}))

    monkeypatch.setattr(eng_mod.SqlEngine, "_register_schema", register)


@pytest.fixture(autouse=True)
def _shared_store(tmp_path, monkeypatch):
    """Each test gets its own shared store dir + fresh global registry."""
    monkeypatch.setenv("SQLHANDLER_JOBS_DIR", str(tmp_path / "shared-jobs"))
    jobs_module.reset_job_manager()
    yield
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


def test_store_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLHANDLER_JOBS_DIR", raising=False)
    mgr = McpJobManager()
    assert mgr.shared_store is None


def test_submit_writes_tombstone_then_finish_publishes_outcome(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    assert mgr.shared_store is not None
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]

    tomb = json.loads((mgr.shared_store.root / f"{job_id}.json").read_text())
    assert tomb["state"] == "running"

    _wait_done(mgr, job_id)
    done = json.loads((mgr.shared_store.root / f"{job_id}.json").read_text())
    assert done["state"] == "done"
    assert done["has_result_file"] is True
    assert (mgr.shared_store.root / f"{job_id}.parquet").exists()
    assert done["n_rows"] == 5


def test_outcome_published_without_owner_poll(tmp_path, monkeypatch):
    """The watcher publishes even when the client only ever polls elsewhere.

    Regression for the load-balanced-client case: without the watcher the
    owner would only publish when polled ITSELF, which never happens when
    every poll lands on a foreign replica.
    """
    _patch_register(monkeypatch, sleep=0.4)
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order")["job_id"]
    foreign = McpJobManager()

    deadline = time.monotonic() + 10.0
    payload = {"state": "running"}
    while time.monotonic() < deadline:
        payload = foreign.status(job_id)  # ONLY foreign polls
        if payload["state"] != "running":
            break
        time.sleep(0.05)
    assert payload["state"] == "done"
    assert payload["n_rows"] == 5
    assert payload["result_fetched"] is False
    # the owner never polled: its own record is untouched
    assert owner.status(job_id)["state"] == "done"


def test_foreign_replica_can_poll_and_fetch(tmp_path):
    """THE outage regression: submit on A, poll + fetch on B.

    Two McpJobManager instances over one shared dir behave like two pods;
    without the shared store B would raise "Unknown job id" (404).
    """
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order WHERE kind = 'a'")["job_id"]
    _wait_done(owner, job_id)

    foreign = McpJobManager()
    status = foreign.status(job_id)
    assert status["state"] == "done"
    assert status["columns"] == ["id", "kind"]
    assert status["result_fetched"] is False

    arrow = foreign.take_result(job_id)
    assert arrow.to_pydict() == {"id": [1, 3, 5], "kind": ["a", "a", "a"]}

    # the fetch is visible cluster-wide: owner reconciles, everyone refuses a 2nd
    assert owner.status(job_id)["result_fetched"] is True
    with pytest.raises(JobError, match="already fetched") as exc:
        foreign.take_result(job_id)
    assert exc.value.status == 409
    with pytest.raises(JobError, match="already fetched"):
        owner.take_result(job_id)
    # the parquet sidecar is gone with the hand-over
    assert not (owner.shared_store.root / f"{job_id}.parquet").exists()


def test_running_tombstone_foreign_poll_not_404(tmp_path, monkeypatch):
    """A foreign poll of a RUNNING job says running (with the note), not 404."""
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order")["job_id"]
    foreign = McpJobManager()

    payload = foreign.status(job_id)
    assert payload["state"] == "running"
    assert "another replica" in payload["note"]

    # a foreign RESULT fetch while running is a 409, same as same-pod
    with pytest.raises(JobError, match="still running"):
        foreign.take_result(job_id)

    owner.cancel(job_id)


def test_foreign_cancel_running_job_honest_noop(tmp_path, monkeypatch):
    """Cancel from a foreign replica cannot interrupt; it says so."""
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order")["job_id"]
    foreign = McpJobManager()

    result = foreign.cancel(job_id)
    assert result["interrupted"] is False
    assert "another replica" in result["note"]

    # the tombstone is marked cancelled; once the owner's job finishes the
    # real outcome republishes over it (shared state self-heals)
    owner.cancel(job_id)


def test_fetch_claim_is_atomic_and_stale_claims_are_stolen(tmp_path):
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order")["job_id"]
    _wait_done(owner, job_id)

    foreign = McpJobManager()
    store = foreign.shared_store
    assert store.claim_fetch(job_id) is True
    # a second claim while held is refused...
    assert store.claim_fetch(job_id) is False
    # ...and the OWNER sees "being fetched on another replica"
    with pytest.raises(JobError, match="another replica"):
        owner.take_result(job_id)
    # a stale claim is stolen
    old = time.time() - (store.CLAIM_STEAL_SECONDS + 5)
    os.utime(store._claim_dir(job_id), (old, old))
    assert store.claim_fetch(job_id) is True
    store.mark_fetched(job_id)  # cleanup for the rest of the test


def test_ttl_and_cap_bound_the_shared_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_ASYNC_JOB_TTL", "0")
    eng = _make_engine(tmp_path)
    mgr = McpJobManager(max_jobs=2)
    for _ in range(4):
        job_id = mgr.submit(eng, "SELECT count(*) AS n FROM work_order")["job_id"]
        _wait_done(mgr, job_id)
        mgr.take_result(job_id)
    records = list(mgr.shared_store.root.glob("*.json"))
    assert len(records) <= 2


def test_zombie_tombstone_aged_out(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]
    tomb = mgr.shared_store.root / f"{job_id}.json"
    data = json.loads(tomb.read_text())
    # pretend the owner pod died long ago (timeout + grace in the past)
    data["submitted_at_wall"] = time.time() - 100000
    tomb.write_text(json.dumps(data))
    mgr.shared_store._cleanup()
    assert not tomb.exists()


# ------------------------------------------------------------------ REST twins


def test_rest_shared_store_cross_replica(tmp_path, monkeypatch):
    """End-to-end through /api/jobs: submit, reset the (per-process) registry,
    then poll + fetch + double-fetch-refused as if on another replica."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import register_ui

    _patch_register(monkeypatch)
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

    job_id = client.post("/api/jobs", json={"sql": "SELECT * FROM work_order"}, headers=auth).json()["job_id"]
    for _ in range(200):
        if client.get(f"/api/jobs/{job_id}", headers=auth).json().get("state") == "done":
            break
        time.sleep(0.05)

    # simulate the client landing on a different replica: a fresh registry
    jobs_module.reset_job_manager()

    r = client.get(f"/api/jobs/{job_id}", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "done"

    r1 = client.get(f"/api/jobs/{job_id}/result", headers=auth)
    assert r1.status_code == 200
    assert r1.json()["total_rows"] == 5
    r2 = client.get(f"/api/jobs/{job_id}/result", headers=auth)
    assert r2.status_code == 409
    assert "already fetched" in r2.json()["error"]

    jobs_module.reset_job_manager()


def test_stats_reports_shared_store(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = McpJobManager()
    assert "shared_store" in mgr.stats()
    job_id = mgr.submit(eng, "SELECT * FROM work_order")["job_id"]
    _wait_done(mgr, job_id)
    assert mgr.stats()["shared_store"] == str(mgr.shared_store.root)



# ------------------------------------------------------- cross-replica cancel

# The 2026-09 HA-review fix: a cancel landing on a FOREIGN replica used to
# only rewrite the shared tombstone while the query kept running on its owner
# (holding a concurrency-gate slot until the timeout watchdog fired). The
# cancel now raises a per-job flag file in the shared store; the owner's
# publish watcher polls it and delivers the DuckDB interrupt locally.


def test_foreign_cancel_interrupts_owner_query(tmp_path, monkeypatch):
    """cancel() from a foreign manager reaches the owner's query thread.

    The register patch sleeps 1s in PYTHON (con.interrupt() cannot break a
    Python sleep — the interrupt lands at the next DuckDB call), so the
    distinguishing signal is the FINAL STATE: forwarded cancel -> the
    pending interrupt makes the job report "cancelled"; a broken forward
    would let it finish "done" at ~1s.
    """
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    foreign = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order")["job_id"]
    time.sleep(0.2)
    result = foreign.cancel(job_id)
    assert result["interrupted"] is False  # honest: this replica did not interrupt
    assert "forwarded" in result["note"]
    deadline = time.monotonic() + 5.0
    payload = {"state": "running"}
    while time.monotonic() < deadline:
        payload = owner.status(job_id)
        if payload["state"] != "running":
            break
        time.sleep(0.02)
    assert payload["state"] == "cancelled", payload
    # other replicas see the same terminal state
    assert foreign.status(job_id)["state"] == "cancelled"


def test_own_cancel_still_works_with_shared_store(tmp_path, monkeypatch):
    """Same-pod cancel semantics are unchanged (interrupted=True)."""
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    owner = McpJobManager()
    job_id = owner.submit(eng, "SELECT * FROM work_order")["job_id"]
    time.sleep(0.2)
    result = owner.cancel(job_id)
    assert result["interrupted"] is True
    assert _wait_done(owner, job_id)["state"] == "cancelled"


def test_cancel_flag_lifecycle(tmp_path):
    """flag_cancel raises the file; _drop and publish_running clear it."""
    eng = _make_engine(tmp_path)  # noqa: F841 - keeps the fixture shape uniform
    owner = McpJobManager()
    store = owner.shared_store
    store.flag_cancel("job-x")
    assert store.cancel_flagged("job-x") is True
    store._drop("job-x")
    assert store.cancel_flagged("job-x") is False
    store.flag_cancel("job-y")
    store.publish_running("job-y", "SELECT 1")
    assert store.cancel_flagged("job-y") is False
