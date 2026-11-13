"""Web UI async-query registry: configurable caps + owner scoping.

The older /api/query/async registry (QueryJobManager) used to be a plain
process-global list: a hardcoded 100-job cap, no per-caller cap, and no
owner check on status/rows/cancel. This suite pins the fixed contract:

* every cap is env-controllable (SQLHANDLER_MAX_ASYNC_QUERIES[_PER_CALLER],
  SQLHANDLER_MAX_PAGE_ROWS, SQLHANDLER_EXPORT_DEFAULT_ROWS);
* the per-caller RUNNING cap is identity-keyed and the global cap stays on top;
* a job with an owner is invisible to another owner (unknown-id shape, never
  "belongs to someone else").

The QueryJob constructor is faked: this suite tests the registry, not DuckDB.
"""

import pytest

from sqlhandler import webui


class FakeJob:
    """The registry's surface: state/info/cancel + the fetch fields."""

    def __init__(self, *args, **kwargs):
        self.state = "running"
        self.error = None
        self.result = None
        self.elapsed_ms = 0.0

    def info(self):
        return {"state": self.state}

    def cancel(self):
        self.state = "cancelled"
        return True


@pytest.fixture()
def fake_jobs(monkeypatch):
    monkeypatch.setattr(webui, "QueryJob", FakeJob)
    for var in (
        "SQLHANDLER_MAX_ASYNC_QUERIES",
        "SQLHANDLER_MAX_ASYNC_QUERIES_PER_CALLER",
        "SQLHANDLER_MAX_PAGE_ROWS",
        "SQLHANDLER_EXPORT_DEFAULT_ROWS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_caps_are_env_controllable(monkeypatch):
    assert webui._max_async_queries() == 100
    assert webui._max_async_queries_per_caller(100) == 100
    assert webui._max_page_rows() == 1000
    assert webui._export_default_rows() == 10_000

    monkeypatch.setenv("SQLHANDLER_MAX_ASYNC_QUERIES", "7")
    monkeypatch.setenv("SQLHANDLER_MAX_ASYNC_QUERIES_PER_CALLER", "2")
    monkeypatch.setenv("SQLHANDLER_MAX_PAGE_ROWS", "50")
    monkeypatch.setenv("SQLHANDLER_EXPORT_DEFAULT_ROWS", "123")
    assert webui._max_async_queries() == 7
    assert webui._max_async_queries_per_caller(7) == 2
    assert webui._max_page_rows() == 50
    assert webui._export_default_rows() == 123

    # garbage degrades to the default, never to "unlimited"
    monkeypatch.setenv("SQLHANDLER_MAX_ASYNC_QUERIES", "banana")
    assert webui._max_async_queries() == 100


def test_registry_per_caller_running_cap(fake_jobs, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ASYNC_QUERIES_PER_CALLER", "1")
    manager = webui.QueryJobManager(max_jobs=8)
    first = manager.submit(None, "SELECT 1", caller_scope="subject:alice", owner="subject:alice")
    assert "query_id" in first
    second = manager.submit(None, "SELECT 1", caller_scope="subject:alice", owner="subject:alice")
    assert second["status"] == 429
    assert "SQLHANDLER_MAX_ASYNC_QUERIES_PER_CALLER" in second["error"]
    assert "for this caller" in second["error"]
    # another caller has its own bucket
    bob = manager.submit(None, "SELECT 1", caller_scope="subject:bob", owner="subject:bob")
    assert "query_id" in bob


def test_registry_global_cap_still_applies(fake_jobs):
    manager = webui.QueryJobManager(max_jobs=1)
    first = manager.submit(None, "SELECT 1", caller_scope="subject:alice")
    assert "query_id" in first
    second = manager.submit(None, "SELECT 1", caller_scope="subject:bob")
    assert second["status"] == 429
    assert "SQLHANDLER_MAX_ASYNC_QUERIES" in second["error"]


def test_registry_owner_scopes_status_and_cancel(fake_jobs):
    manager = webui.QueryJobManager(max_jobs=8)
    submitted = manager.submit(None, "SELECT 1", owner="subject:alice", caller_scope="subject:alice")
    query_id = submitted["query_id"]

    assert manager.get(query_id, owner="subject:alice") is not None
    assert manager.get(query_id, owner="subject:bob") is None, "another owner must not see the job"
    assert manager.get(query_id) is None, "an owner-scoped job is not the unowned (historical) shape"
    assert manager.cancel(query_id, owner="subject:bob") is None
    assert manager.cancel(query_id, owner="subject:alice") is not None


def test_registry_unowned_jobs_keep_the_historical_shared_posture(fake_jobs):
    manager = webui.QueryJobManager(max_jobs=8)
    submitted = manager.submit(None, "SELECT 1")
    query_id = submitted["query_id"]
    assert manager.get(query_id) is not None
    assert manager.get(query_id, owner="subject:alice") is not None
