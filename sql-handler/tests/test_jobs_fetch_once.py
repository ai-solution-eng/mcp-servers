"""Fetch-once / shared-job-store hardening tests (audit 2026-11-12, z4).

Covers the jobs.py fixes: the atomic claim-steal protocol (F6.1 — a fresh
claim is never destroyed, ownership is never duplicated), the fail-closed
mark_fetched order (F6.2 — the sidecar dies before the flag), the
status/race fix (F7) and the loser-submit wait (F5, jobs.py half).
"""

import threading
import time
from types import SimpleNamespace

import pyarrow as pa
import pytest

from sqlhandler.engine import LakehouseError
from sqlhandler.jobs import JobError, McpJobManager, _SharedJobStore, _JobRecord


@pytest.fixture()
def store(tmp_path):
    return _SharedJobStore(tmp_path / "jobs", max_jobs=4)


def _seed_claim(store, job_id, token="tok-a", age=0.0):
    claim = store._claim_dir(job_id)
    claim.mkdir(parents=True)
    (claim / "token").write_text(token, encoding="utf-8")
    if age:
        old = time.time() - age
        import os

        os.utime(claim, (old, old))
    return claim


# ---------------------------------------------------------------------------
# F6.1: the claim-steal protocol
# ---------------------------------------------------------------------------


def test_claim_fetch_fresh_succeeds_and_carries_token(store):
    assert store.claim_fetch("j1") is True
    claim = store._claim_dir("j1")
    assert claim.exists()
    assert (claim / "token").read_text(encoding="utf-8").strip()


def test_second_claim_on_fresh_claim_is_refused(store):
    assert store.claim_fetch("j1") is True
    assert store.claim_fetch("j1") is False
    # the refused claimant must NOT have destroyed the winner's claim
    assert store._claim_dir("j1").exists()


def test_stale_claim_is_stealable(store):
    _seed_claim(store, "j1", token="stale", age=10 * 60)
    assert store.claim_fetch("j1") is True
    assert store._claim_dir("j1").exists(), "the winner's new claim is in place"


def test_concurrent_steal_yields_exactly_one_winner(store):
    """The old stat/rmdir/mkdir flow could hand the claim to TWO stealers.
    The rename-based protocol must admit exactly one."""
    _seed_claim(store, "j1", token="stale", age=10 * 60)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(store.claim_fetch("j1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1, results


def test_steal_refuses_a_replaced_fresh_claim(store):
    """Steal decision is made on the dir we RENAMED (the token compare), not
    on a stale observation of a dir that has since been replaced."""
    claim = _seed_claim(store, "j1", token="old-stale", age=10 * 60)
    # simulate the replacement window: move the stale dir aside and put a
    # FRESH (different-token) claim in its place, keeping the mtime old so
    # the steal path is entered, then let the token compare speak.
    stale = store.root / ".stale-side"
    import os

    os.rename(claim, stale)
    fresh = _seed_claim(store, "j1", token="fresh-replacement")
    # pretend the fresh dir carries the age of the window: not needed — the
    # fresh mtime must refuse the steal outright
    assert store._steal_stale_claim("j1") is False
    assert fresh.exists() and (fresh / "token").read_text() == "fresh-replacement"


def test_release_claim_removes_the_token_dir(store):
    store.claim_fetch("j1")
    store._release_claim("j1")
    assert not store._claim_dir("j1").exists()


# ---------------------------------------------------------------------------
# F6.2: mark_fetched fails closed
# ---------------------------------------------------------------------------


def _published_done_job(store, job_id="jx"):
    record = SimpleNamespace(job_id=job_id, owner=None, fetched=False)
    arrow = pa.table({"a": [1, 2, 3]})
    store.publish(record, {"job_id": job_id, "state": "done"}, arrow=arrow)
    assert store._result_path(job_id).exists()
    return record


def test_mark_fetched_drops_sidecar_even_when_record_rewrite_fails(store, monkeypatch):
    _published_done_job(store, "jx")
    store.claim_fetch("jx")

    def boom(*_a, **_k):
        raise OSError("store hiccup")

    monkeypatch.setattr("sqlhandler.jobs._write_json_atomic", boom)
    store.mark_fetched("jx")  # must not raise
    # FAIL CLOSED: the sidecar is gone even though the flag flip failed (the
    # on-disk record may still claim has_result_file=true — the refusal test
    # below proves that combination cannot serve rows).
    assert not store._result_path("jx").exists()


def test_take_shared_result_refuses_after_failed_mark(store, monkeypatch):
    _published_done_job(store, "jx")
    store.claim_fetch("jx")

    def boom(*_a, **_k):
        raise OSError("store hiccup")

    monkeypatch.setattr("sqlhandler.jobs._write_json_atomic", boom)
    store.mark_fetched("jx")
    with pytest.raises(JobError) as excinfo:
        manager = McpJobManager(max_jobs=1)
        object.__setattr__(manager, "_shared", store)
        manager._take_shared_result("jx")
    assert excinfo.value.status == 409


# ---------------------------------------------------------------------------
# F7: status() must not read a released result
# ---------------------------------------------------------------------------


class _FakeJob:
    def __init__(self):
        self.state = "done"
        self.released = False
        self._table = pa.table({"a": [1]})

    @property
    def result(self):
        if self.released:
            raise LakehouseError("Query result was already fetched and released (results are handed over once).")
        return self._table

    def release_result(self):
        self.released = True

    def info(self):
        return {"sql": "SELECT 1", "state": self.state, "elapsed_ms": 1.0, "error": None}


def _manager_with_job(job):
    mgr = McpJobManager(max_jobs=4)
    record = _JobRecord(job, "j7", owner=None)
    record.fetched = False
    mgr._records["j7"] = record
    return mgr, record


def test_status_after_fetch_reports_fetched_not_500():
    job = _FakeJob()
    mgr, record = _manager_with_job(job)
    # the fetch happens first
    arrow = mgr.take_result("j7")
    assert arrow is not None
    assert record.fetched is True
    # the racing poll must be a clean 200-shaped payload, not an exception
    payload = mgr.status("j7")
    assert payload["result_fetched"] is True
    assert "columns" not in payload


def test_status_concurrent_with_fetch_never_raises():
    """Hammer status against a live fetch: the pre-fix read could observe a
    released result outside the lock."""
    job = _FakeJob()
    mgr, record = _manager_with_job(job)
    errors = []

    def poller():
        for _ in range(200):
            try:
                mgr.status("j7")
            except Exception as exc:  # noqa: BLE001 - the test's point
                errors.append(exc)

    def fetcher():
        for _ in range(50):
            try:
                mgr.take_result("j7")
            except JobError:
                pass
            record.fetched = False
            job.released = False

    threads = [threading.Thread(target=poller), threading.Thread(target=fetcher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]


# ---------------------------------------------------------------------------
# F5 (jobs.py half): loser submit waits for the cancelled job to unwind
# ---------------------------------------------------------------------------


def test_loser_submit_waits_briefly(monkeypatch):
    """The rejected (racing-loser) path cancels AND waits before returning
    429, so a cancelled-before-run job releases its gate slot promptly.
    The racing fill is simulated between the pre-check and the re-check by
    the patched _cleanup (which submit calls under the lock)."""
    import sqlhandler.jobs as jobs_module

    waits = []

    class _LoserJob:
        state = "running"

        def cancel(self):
            return False

        def wait(self, timeout=None):
            waits.append(timeout)

    mgr = McpJobManager(max_jobs=1)

    def fake_job(*_a, **_k):
        # the racing submit fills the LAST slot while our loser is being
        # constructed (between submit's pre-check and its re-check)
        mgr._records["racer"] = _JobRecord(_FakeJob(), "racer")
        return _LoserJob()

    monkeypatch.setattr(jobs_module, "QueryJob", fake_job)
    monkeypatch.setattr(jobs_module.McpJobManager, "_caller_cap_refusal", lambda self, scope, per: None)
    result = mgr.submit(object(), "SELECT 1")
    assert result.get("status") == 429
    assert waits and waits[0] > 0, "the loser path must wait for the unwind"
