"""Single-flight coalescing: identical cold queries execute once.

N callers with the SAME effective rules (the result-cache key already carries
the policy hash) issuing the same cold query should pay for ONE execution:
one leader runs it, the rest wait for the cache fill. This suite pins:

* the _SingleFlight join/finish contract (leader, waiter, wake-up, counters);
* an end-to-end engine test where the waiter never re-executes the query;
* the SQLHANDLER_SINGLEFLIGHT=0 opt-out;
* waiters falling through to their own execution when no result lands.
"""

import threading
import time

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import SqlEngine, _SingleFlight, _singleflight
from sqlhandler.identity import Caller
from sqlhandler.provider import DataProvider, TableInfo

ALICE = Caller(cls="user", subject="alice", key_fp="sha256:aaa")
SQL = "SELECT id FROM work_order ORDER BY id"


class FakeProvider(DataProvider):
    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.versions = {}

    def list_tables(self):
        return [TableInfo(name="work_order", schema="workorder", format="parquet")]

    def table_uri(self, info):
        return "fake://"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / "workorder" / "work_order"), format="parquet")

    def check_version(self, info):
        return self.versions.get(info.path)


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": list(range(20))}), d / "part.parquet")
    return SqlEngine(FakeProvider(tmp_path), cache_ttl=0)


@pytest.fixture(autouse=True)
def _clean_singleflight(monkeypatch):
    monkeypatch.delenv("SQLHANDLER_SINGLEFLIGHT", raising=False)
    monkeypatch.delenv("SQLHANDLER_SINGLEFLIGHT_TIMEOUT", raising=False)
    _singleflight.reset()
    yield
    _singleflight.reset()


# ----------------------------------------------------------------- contract


def test_join_finish_wakes_waiters_and_counts():
    sf = _SingleFlight()
    flight, leader = sf.join("k")
    assert leader is True
    joined, leader2 = sf.join("k")
    assert leader2 is False and joined is flight
    assert sf.stats() == {"in_flight": 1, "leaders": 1, "joined": 1, "served": 0}

    sf.finish("k", flight)
    assert joined.event.is_set()
    assert sf.stats()["in_flight"] == 0

    # the key is free again: a new caller becomes the next leader
    flight2, leader3 = sf.join("k")
    assert leader3 is True
    sf.finish("k", flight2)


# ------------------------------------------------------------ end to end


def _slow_register(engine, monkeypatch, started, release, calls):
    """Replace schema registration with a controllable barrier (one per exec)."""
    real = engine._register_schema

    def slow(con, sql, version=None, materialize=True, caller=None):
        calls.append(sql)
        started.set()
        release.wait(5)
        return real(con, sql, version=version, materialize=materialize, caller=caller)

    monkeypatch.setattr(engine, "_register_schema", slow)


def test_identical_cold_queries_coalesce(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []
    _slow_register(engine, monkeypatch, started, release, calls)

    results = []

    def run():
        results.append(engine.query_duckdb(SQL, caller=ALICE))

    leader = threading.Thread(target=run)
    leader.start()
    assert started.wait(5), "leader never entered execution"

    waiter = threading.Thread(target=run)
    waiter.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and engine.cache_stats()["singleflight"]["joined"] < 1:
        time.sleep(0.01)
    assert engine.cache_stats()["singleflight"]["joined"] == 1, "waiter did not join the flight"

    release.set()
    leader.join(10)
    waiter.join(10)
    assert not leader.is_alive() and not waiter.is_alive()

    assert len(calls) == 1, "the waiter re-executed the query instead of sharing"
    assert len(results) == 2
    assert results[0].to_pylist() == results[1].to_pylist()
    stats = engine.cache_stats()["singleflight"]
    assert stats["leaders"] == 1
    assert stats["served"] == 1
    assert stats["in_flight"] == 0


def test_singleflight_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_SINGLEFLIGHT", "0")
    engine = _make_engine(tmp_path)
    barrier = threading.Barrier(2)
    calls = []
    real = engine._register_schema

    def both_enter(con, sql, version=None, materialize=True, caller=None):
        calls.append(sql)
        barrier.wait(5)  # both executions must overlap: no coalescing
        return real(con, sql, version=version, materialize=materialize, caller=caller)

    monkeypatch.setattr(engine, "_register_schema", both_enter)
    threads = [threading.Thread(target=lambda: engine.query_duckdb(SQL, caller=ALICE)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(calls) == 2
    assert engine.cache_stats()["singleflight"]["joined"] == 0


def test_waiter_runs_its_own_copy_when_no_result_is_cached(tmp_path, monkeypatch):
    """A leader that finishes WITHOUT a cacheable result (here: the query is
    served by the preview fast path, which stores under a canonical key, not
    the exact key... but with the exact key it still stores) must not strand
    waiters: they wake and execute. We simulate the no-result case by making
    the leader's result uncacheable via a tiny result-cache cap."""
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_MAX_BYTES", "1")  # result never fits L1
    engine = _make_engine(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []
    _slow_register(engine, monkeypatch, started, release, calls)

    def run():
        return engine.query_duckdb(SQL, caller=ALICE)

    leader = threading.Thread(target=run)
    leader.start()
    assert started.wait(5)
    waiter = threading.Thread(target=run)
    waiter.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and engine.cache_stats()["singleflight"]["joined"] < 1:
        time.sleep(0.01)
    assert engine.cache_stats()["singleflight"]["joined"] == 1

    release.set()
    leader.join(10)
    waiter.join(10)
    # The leader could not cache (cap 1 byte), so the waiter executed too.
    assert len(calls) == 2
    assert engine.cache_stats()["singleflight"]["served"] == 0
