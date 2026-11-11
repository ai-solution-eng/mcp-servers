"""Per-caller concurrency limits (identity-keyed) + the caller-scope metrics.

The concurrency gate and the async-job cap used to be process-global: ONE
caller could occupy every slot on the pod. They are now keyed on the caller
identity the identity layer already resolved, with the global cap kept as the
outer bound and NO identity falling back to the global bucket (the historical
behavior). Covered here:

* limits.caller_scope / bounded_scope (the stable, bounded bucket vocabulary)
* limits.CallerGate (per-scope semaphores; pass-through without an identity)
* engine._QueryGate (per-caller bucket + global outer bound, both released)
* jobs.McpJobManager (per-caller RUNNING-job cap, global cap on top)
* observability (the per-caller duration / queue-wait histograms)

Run: python -m pytest tests/test_per_caller_limits.py -v
"""

import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import limits, observability
from sqlhandler.engine import LakehouseError, SqlEngine, _QueryGate
from sqlhandler.identity import Caller
from sqlhandler.jobs import McpJobManager
from sqlhandler.provider import DataProvider, TableInfo

ALICE = Caller(cls="user", subject="alice", key_fp="sha256:aaa")
BOB = Caller(cls="user", subject="bob", key_fp="sha256:bbb")
KEYED = Caller(cls="key", key_fp="sha256:ccc")


@pytest.fixture(autouse=True)
def _clean_scopes(monkeypatch):
    limits.reset_scopes()
    for var in (
        "SQLHANDLER_MAX_CONCURRENT_QUERIES",
        "SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER",
        "SQLHANDLER_QUEUE_TIMEOUT",
        "SQLHANDLER_MAX_JOBS",
        "SQLHANDLER_MAX_JOBS_PER_CALLER",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    limits.reset_scopes()


# ------------------------------------------------------------------- scope


def test_caller_scope_is_none_without_identity():
    assert limits.caller_scope(None) is None
    assert limits.caller_scope(Caller()) is None  # the shared ANONYMOUS caller


def test_caller_scope_keys_on_subject_then_key_fp_never_the_key():
    assert limits.caller_scope(ALICE) == "subject:alice"
    assert limits.caller_scope(KEYED) == "key:sha256:ccc"


def test_gate_scope_is_bounded_and_none_without_identity():
    assert limits.gate_scope(None) is None
    assert limits.gate_scope(Caller()) is None
    assert limits.gate_scope(ALICE) == "subject:alice"


def test_bounded_scope_collapses_past_the_cap(monkeypatch):
    monkeypatch.setattr(limits, "MAX_SCOPES", 2)
    limits.reset_scopes()
    assert limits.bounded_scope("subject:a") == "subject:a"
    assert limits.bounded_scope("subject:b") == "subject:b"
    assert limits.bounded_scope("subject:c") == limits.OTHER
    # already-seen scopes keep their series
    assert limits.bounded_scope("subject:a") == "subject:a"
    assert limits.bounded_scope(None) == limits.ANONYMOUS


def test_per_caller_caps_default_to_the_global_cap(monkeypatch):
    assert limits.max_concurrent_per_caller(8) == 8
    assert limits.max_jobs_per_caller(8) == 8
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", "2")
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS_PER_CALLER", "3")
    assert limits.max_concurrent_per_caller(8) == 2
    assert limits.max_jobs_per_caller(8) == 3
    # garbage degrades to the default, never to "unlimited"
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", "banana")
    assert limits.max_concurrent_per_caller(8) == 8


# --------------------------------------------------------------- CallerGate


def test_caller_gate_enforces_per_scope_and_ignores_no_identity():
    gate = limits.CallerGate()
    ok, held = gate.acquire("subject:alice", 1, timeout=0.01)
    assert ok and held is not None
    # the same caller is now at its cap
    blocked, none_held = gate.acquire("subject:alice", 1, timeout=0.01)
    assert blocked is False and none_held is None
    # another caller has its own bucket
    other_ok, other_held = gate.acquire("subject:bob", 1, timeout=0.01)
    assert other_ok and other_held is not None
    # no identity = pass-through (the global gate is the only bound)
    anon_ok, anon_held = gate.acquire(None, 1, timeout=0.01)
    assert anon_ok and anon_held is None
    gate.release(held)
    gate.release(other_held)
    ok_again, held_again = gate.acquire("subject:alice", 1, timeout=0.01)
    assert ok_again and held_again is not None
    gate.release(held_again)


# --------------------------------------------------------------- query gate


def test_query_gate_keys_on_caller_and_keeps_the_global_bound(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "4")
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", "1")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "0.05")
    gate = _QueryGate()
    alice_slot = gate.acquire(ALICE)
    try:
        with pytest.raises(LakehouseError) as excinfo:
            gate.acquire(ALICE)
        assert "for this caller" in str(excinfo.value)
        assert "SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER" in str(excinfo.value)
        # a different caller still gets its own slot
        bob_slot = gate.acquire(BOB)
        gate.release(bob_slot)
    finally:
        gate.release(alice_slot)


def test_query_gate_without_identity_keeps_the_historical_path(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "2")
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", "1")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "0.05")
    gate = _QueryGate()
    # No identity: the per-caller bucket never applies — two anonymous
    # acquisitions both succeed even though the per-caller cap is 1.
    first = gate.acquire(None)
    second = gate.acquire(None)
    gate.release(first)
    gate.release(second)


def test_query_gate_global_cap_still_bounds_all_callers(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", "1")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "0.05")
    gate = _QueryGate()
    alice_slot = gate.acquire(ALICE)
    try:
        with pytest.raises(LakehouseError) as excinfo:
            gate.acquire(BOB)  # bob's own bucket is free, the global slot is not
        assert "Too many concurrent queries (limit 1)" in str(excinfo.value)
    finally:
        gate.release(alice_slot)


def test_query_gate_releases_the_caller_slot_on_global_timeout(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", "1")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "0.05")
    gate = _QueryGate()
    alice_slot = gate.acquire(ALICE)
    try:
        with pytest.raises(LakehouseError):
            gate.acquire(BOB)
    finally:
        gate.release(alice_slot)
    # Bob's failed acquire must NOT have leaked its per-caller slot: with the
    # global slot free again, bob acquires cleanly.
    bob_slot = gate.acquire(BOB)
    gate.release(bob_slot)


# ------------------------------------------------------------------- jobs


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1, 2, 3]}), d / "part.parquet")

    class P(DataProvider):
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    return SqlEngine(P(), cache_ttl=0)


def test_job_registry_caps_running_jobs_per_caller(tmp_path, monkeypatch):
    import sqlhandler.engine as eng_mod

    def register(self, con, sql, version=None, **kw):
        time.sleep(0.05)  # keep the first job RUNNING while the second submits
        con.register("work_order", pa.table({"id": [1, 2, 3]}))

    monkeypatch.setattr(eng_mod.SqlEngine, "_register_schema", register)
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS", "8")
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS_PER_CALLER", "1")
    mgr = McpJobManager(max_jobs=8)
    eng = _make_engine(tmp_path)
    first = mgr.submit(eng, "SELECT * FROM work_order", caller=ALICE, caller_scope=limits.gate_scope(ALICE))
    assert "job_id" in first
    second = mgr.submit(eng, "SELECT * FROM work_order", caller=ALICE, caller_scope=limits.gate_scope(ALICE))
    assert second.get("status") == 429
    assert "SQLHANDLER_MAX_JOBS_PER_CALLER" in second["error"]
    assert "for this caller" in second["error"]
    # another caller is unaffected, and the GLOBAL cap still applies on top
    bob = mgr.submit(eng, "SELECT * FROM work_order", caller=BOB, caller_scope=limits.gate_scope(BOB))
    assert "job_id" in bob
    mgr.cancel(first["job_id"])
    mgr.cancel(bob["job_id"])


def test_job_registry_ignores_the_per_caller_cap_without_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS", "4")
    monkeypatch.setenv("SQLHANDLER_MAX_JOBS_PER_CALLER", "1")
    mgr = McpJobManager(max_jobs=4)
    eng = _make_engine(tmp_path)
    submitted = [mgr.submit(eng, "SELECT 1", caller=None, caller_scope=None) for _ in range(2)]
    assert all("job_id" in r for r in submitted)
    for r in submitted:
        mgr.cancel(r["job_id"])


def test_per_caller_refusals_carry_the_per_caller_fix_hint():
    """The structured tail must name the PER-CALLER knob (errors.py)."""
    from sqlhandler import errors

    query_msg = (
        "Too many concurrent queries for this caller (limit 2, "
        "SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER), and the queue wait of 30.0s expired. Retry later."
    )
    job_msg = (
        "Too many active query jobs for this caller (1 of 1, "
        "SQLHANDLER_MAX_JOBS_PER_CALLER); cancel or fetch results and retry later."
    )
    for message in (query_msg, job_msg):
        enriched = errors.enrich(message)
        tail = enriched.splitlines()[-1]
        payload = __import__("json").loads(tail)["error"]
        assert payload["code"] == errors.E_CONCURRENCY_GATE
        assert any("_PER_CALLER" in hint for hint in payload["fix_hints"]), payload


# ---------------------------------------------------------------- metrics


def test_caller_scope_metrics_render_per_caller():
    m = observability.Metrics()
    m.record_caller_scope(ALICE, 1.5, 0.25)
    text = m.render(None)
    assert "# HELP sqlhandler_caller_query_duration_seconds " in text
    assert text.count("# HELP sqlhandler_caller_query_duration_seconds ") == 1
    assert 'sqlhandler_caller_query_duration_seconds_bucket{caller_scope="subject:alice"' in text
    assert 'sqlhandler_caller_query_duration_seconds_count{caller_scope="subject:alice"} 1' in text
    assert 'sqlhandler_caller_queue_wait_seconds_sum{caller_scope="subject:alice"} 0.25' in text


def test_caller_scope_metrics_are_absent_until_observed():
    m = observability.Metrics()
    text = m.render(None)
    assert "sqlhandler_caller_query_duration_seconds" not in text
    assert "sqlhandler_caller_queue_wait_seconds" not in text


def test_record_outcome_attributes_duration_and_queue_wait_to_the_caller(tmp_path):
    eng = _make_engine(tmp_path)
    observability.metrics.record_caller_scope(ALICE, 0.0, None)
    eng._record_outcome("SELECT 1", 2000.0, 1, state="ok", caller=ALICE, queue_wait_s=0.5)
    text = observability.metrics.render(None)
    assert 'sqlhandler_caller_query_duration_seconds_sum{caller_scope="subject:alice"}' in text
    assert 'sqlhandler_caller_queue_wait_seconds_sum{caller_scope="subject:alice"}' in text
