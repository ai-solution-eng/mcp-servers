"""Async MCP query jobs — submit / status / result / cancel (additive, Wave 5).

The audit's "S effort, P0-value" item: the :class:`~sqlhandler.engine.QueryJob`
machinery (own thread, DuckDB `interrupt()` cancellation, concurrency gate,
row caps, audit) has powered the web async API since 0.9, but MCP callers had
no way to start a long query, walk away, and collect the result later. This
module exposes the same primitive as MCP tools (`query_submit` /
`query_status` / `query_result` / `query_cancel`) and REST equivalents
under `/api/jobs/*`.

Non-negotiable constraints (all shared with the synchronous path):

* **Same engine path** — a job IS an :class:`~sqlhandler.engine.QueryJob`, so
  it honors `SQLHANDLER_QUERY_TIMEOUT`, the row caps, the query-concurrency
  gate, the memory budget, and the JSONL audit log.
* **Same read-only guard, at SUBMIT time** — decision D2's parser guard
  (`sqlguard.assert_mcp_readonly`) runs on the submitted SQL BEFORE any job
  is created, so DDL/DML is refused synchronously with the same error text
  `run_sql` produces (and nothing is ever started). The opt-out
  (`SQLHANDLER_MCP_READONLY=0`) applies exactly as it does for `run_sql`;
  queries that can see an attached external catalog stay SELECT-only
  unconditionally at execution time (engine-level).
* **Same timeout** — `SQLHANDLER_QUERY_TIMEOUT` (default 600s) is enforced
  by a watchdog timer armed at submit: a job that outlives the timeout is
  interrupted via DuckDB and reported as `error` with the same
  "Query timed out after Ns" message the synchronous path raises. The
  watchdog fires even when nobody is polling, so a runaway job can never
  hold its concurrency-gate slot forever.
* **Same MAX_ROWS bounding** — the job's result is capped by
  `SQLHANDLER_MAX_ROWS` inside the query (QueryJob), and the rendered
  output honors `SQLHANDLER_MAX_OUTPUT_ROWS` like `run_sql`.
* **Bounded registry** — at most `SQLHANDLER_MAX_JOBS` jobs (default 8) are
  tracked; submitting beyond the cap is refused with a clear message (HTTP
  429 on the REST surface). Finished jobs are TTL-evicted using the same
  `SQLHANDLER_ASYNC_JOB_TTL` knob as the web async API.
* **Fetch-once results** — `query_result` hands the result over ONCE, then
  frees the spooled Arrow table from memory. A second fetch is refused
  (HTTP 409 on REST); `query_status` keeps working and reports
  `"result_fetched": true`. With the shared store enabled the once-only
  contract holds cluster-wide (an atomic claim file serializes fetches
  across replicas).
* **Registry scope** — the registry is in-memory and process-local: a pod
  restart clears it (jobs are not durable by design; resubmit after a
  restart), AND — the non-obvious one — each replica of a scaled-out
  deployment keeps its OWN registry. Submitting on one replica and polling
  on another is the classic "Unknown job id" outage on multi-replica
  deployments (live-seen 2026-09: a 4-replica release where every
  status/result call 404'd three times out of four). The fix is opt-in and
  additive: point `SQLHANDLER_JOBS_DIR` at a directory every replica can
  read-write (k8s: an RWX PVC — the same discipline as the semantic-catalog
  store and the L2 result cache) and finished jobs publish their state and
  result there, so ANY replica can serve `query_status` /
  `query_result` / `query_cancel` for any job. Unset (the default) the
  behavior is exactly the historical in-memory one. Documented in the tool
  descriptions, README and FEATURES.md.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from . import errors as _errors
from .engine import (
    LakehouseError,
    QueryJob,
    SqlEngine,
    _query_timeout,
    _validate_params,
    _validate_snapshot_version,
)
from .sqlguard import assert_mcp_readonly, extract_statement_spans, mcp_readonly_enabled

logger = logging.getLogger("sqlhandler.jobs")

_MAX_JOBS_ENV = "SQLHANDLER_MAX_JOBS"
_MAX_JOBS_DEFAULT = 8
_JOBS_DIR_ENV = "SQLHANDLER_JOBS_DIR"


def max_jobs_env() -> int:
    """Tracked-job cap (`SQLHANDLER_MAX_JOBS`, default 8).

    Garbage or non-positive values fall back to the default — the registry is
    bounded by design, so an operator typo can never disable the bound.
    (Named `max_jobs_env` so the `McpJobManager(max_jobs=...)` parameter
    can never shadow it.)
    """
    raw = os.environ.get(_MAX_JOBS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return _MAX_JOBS_DEFAULT
    return value if value > 0 else _MAX_JOBS_DEFAULT


def jobs_dir_env() -> str:
    """Shared job-store directory (`SQLHANDLER_JOBS_DIR`, default empty).

    Empty (the default) keeps the registry purely in-memory: the historical
    behavior, byte-for-byte. When set to a directory every replica of a
    scaled-out deployment can read-write (k8s: an RWX PVC), finished jobs
    publish their state and result there so ANY replica can serve
    `query_status` / `query_result` / `query_cancel` for any job.
    A broken path degrades to in-memory with a warning rather than breaking
    job submission (best-effort by contract: sharing must never make job
    handling worse than not sharing).
    """
    return os.environ.get(_JOBS_DIR_ENV, "").strip()


class JobError(Exception):
    """A job-flow error carrying the HTTP status it should surface as."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _JobRecord:
    """One tracked job: the QueryJob plus its lifecycle bookkeeping."""

    __slots__ = (
        "fetched",
        "fetched_at",
        "job",
        "job_id",
        "shared_published",
        "timed_out",
        "timeout_message",
        "timer",
    )

    def __init__(self, job: QueryJob, job_id: str):
        self.job = job
        self.job_id = job_id
        self.timer: threading.Timer | None = None
        self.timed_out = False
        self.timeout_message: str | None = None
        self.fetched = False
        self.fetched_at: float | None = None
        self.shared_published = False

    def finished_at(self) -> float | None:
        """Monotonic timestamp the job finished (best-effort, for TTL aging)."""
        if self.fetched_at is not None:
            return self.fetched_at
        if self.job.state == "running":
            return None
        elapsed = self.job.elapsed_ms
        if elapsed is None:
            return None
        return self.job._t0 + elapsed / 1000.0

    def effective_state(self) -> str:
        """The user-visible state (a timed-out job reports `error`)."""
        if self.timed_out and self.job.state != "done":
            return "error"
        return self.job.state


def _atomic_tmp(root: Path, job_id: str) -> tuple[int, str]:
    """A temp fd in `root` for this job's atomic write (temp + rename)."""
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix="." + job_id + ".", suffix=".tmp")
    return fd, tmp


def _write_json_atomic(root: Path, final: Path, payload: dict) -> None:
    """Write one JSON record atomically (temp + fsync + rename)."""
    fd, tmp = _atomic_tmp(root, final.stem)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, final)


class _SharedJobStore:
    """Cross-replica hand-off for async query jobs (opt-in, SQLHANDLER_JOBS_DIR).

    The problem this solves: the registry is process-local, so on a scaled-out
    deployment the pod that ran `query_submit` is the only one that knows the
    job — every other replica answers "Unknown job id" (live-seen on a
    4-replica release, 2026-09). The HTTP layer is deliberately stateless
    (any replica serves any request), so finished job state must become
    shareable for async jobs to work there at all.

    Design (mirrors the L2 result cache's RWX discipline):

    * A RUNNING job is shared as a small tombstone (`state: running`) so a
      foreign replica reports "running (elsewhere)" instead of a misleading
      404. The DuckDB thread, its interrupt handle and the concurrency gate
      are inherently pod-local — only the OWNER can run or cancel the query.
    * When a job FINISHES, the owner publishes the full outcome as one JSON
      file (`<job_id>.json`, atomic temp+rename — readers never see a torn
      write) plus, for a `done` job, the spooled result as Parquet
      (`<job_id>.parquet`) so ANY replica can hand the result over. The
      publish happens on a per-job watcher thread started at submit (so the
      outcome reaches the store even when the submitter only ever polls
      OTHER replicas) and, belt-and-braces, on the owner's own next
      status/result call — whichever comes first.
    * Fetch-once stays honest cluster-wide: the fetcher claims the result
      with an atomic directory create (mkdir is atomic on every filesystem
      the chart mounts), flips `fetched` in the shared record, and deletes
      the Parquet sidecar. A loser (or a late second fetch on any replica)
      gets the same 409 as a same-pod double fetch. A claimant that crashes
      mid-fetch leaves a claim dir that victims may steal after 60s, and
      cleanup removes stale ones.
    * A cancel for a RUNNING job that lands on another replica raises a
      per-job cancel flag (`cancel-<job_id>`, atomic temp+rename): the
      owner's publish watcher polls it and turns it into the local DuckDB
      interrupt. Without this, a foreign "cancel" only rewrote the tombstone
      while the query kept its concurrency-gate slot until the timeout
      watchdog fired (live-fixed 2026-09).
    * TTL/cap cleanup runs on every publish and store read pass — no daemon
      thread, and an idle deployment accumulates nothing. Zombie tombstones
      (their owner pod died mid-run) age out after the query timeout plus a
      grace period.

    Every store failure degrades to the in-memory answer (best-effort by
    contract); the owner's local registry stays the primary record.
    """

    # A fetch claim older than this is considered abandoned (steal it).
    CLAIM_STEAL_SECONDS = 60

    # How often a running job's owner re-checks the shared cancel flag. A
    # cross-replica cancel is honored within ~this interval while the query
    # runs; each poll is one stat() on the (RWX) store, so the cost is nil.
    CANCEL_POLL_SECONDS = 0.25

    def __init__(self, root: str | os.PathLike, max_jobs: int, save_result: bool = True):
        self.root = Path(root)
        self.max_jobs = max_jobs
        self.save_result = save_result

    # ------------------------------------------------------------ paths
    def _job_path(self, job_id: str) -> Path:
        return self.root / (job_id + ".json")

    def _result_path(self, job_id: str) -> Path:
        return self.root / (job_id + ".parquet")

    def _claim_dir(self, job_id: str) -> Path:
        return self.root / ("fetch-" + job_id)

    def _cancel_flag_path(self, job_id: str) -> Path:
        return self.root / ("cancel-" + job_id)

    # ------------------------------------------------------------ publish
    def publish_running(self, job_id: str, sql: str) -> None:
        """Tombstone at submit: foreign replicas see running, not 404."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # A cancel flag left over for this id (a drop/recreate in tests,
            # or a store shared across registry resets) must never fire on a
            # fresh run.
            self._cancel_flag_path(job_id).unlink(missing_ok=True)
            self._cleanup()
            _write_json_atomic(
                self.root,
                self._job_path(job_id),
                {
                    "job_id": job_id,
                    "sql": sql,
                    "state": "running",
                    "submitted_at_wall": time.time(),
                    "fetched": False,
                    "has_result_file": False,
                },
            )
        except Exception as exc:  # sharing is best-effort
            logger.warning("async job %s tombstone failed in %s: %s", job_id, self.root, exc)

    def publish(self, record: _JobRecord, status_payload: dict, arrow=None) -> None:
        """Write one finished job's shared record (best-effort).

        `arrow` is the spooled result table when the job finished `done`
        with the result still unfetched; it is spooled to Parquet so another
        replica can serve the fetch. The owner KEEPS its in-memory record —
        publishing is additive and idempotent (the atomic rename makes every
        rewrite of the same outcome identical).
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._cleanup()
            payload = dict(status_payload)
            payload["fetched"] = bool(record.fetched)
            payload["finished_at_wall"] = time.time()
            if arrow is not None and self.save_result:
                import pyarrow.parquet as pq

                tmp = self.root / ("." + record.job_id + ".parquet.tmp")
                pq.write_table(arrow, tmp)
                os.replace(tmp, self._result_path(record.job_id))
                payload["has_result_file"] = True
            else:
                payload["has_result_file"] = False
            _write_json_atomic(self.root, self._job_path(record.job_id), payload)
            logger.info("async job %s published to shared store %s", record.job_id, self.root)
        except Exception as exc:
            logger.warning(
                "async job %s could not be published to shared store %s: %s",
                record.job_id,
                self.root,
                exc,
            )

    def mark_fetched(self, job_id: str) -> None:
        """Flip `fetched` in the shared record, drop the result + the claim."""
        try:
            path = self._job_path(job_id)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["fetched"] = True
                payload["has_result_file"] = False
                _write_json_atomic(self.root, path, payload)
            try:
                self._result_path(job_id).unlink()
            except FileNotFoundError:
                pass
        except Exception as exc:
            logger.warning("shared job %s fetch-mark failed: %s", job_id, exc)
        finally:
            self._release_claim(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        """Best-effort shared-record update after a cancel.

        Only a RUNNING tombstone is rewritten (a finished record is already
        the outcome; the owner's own finish-publish would overwrite a racing
        tombstone anyway — the shared state self-heals toward the truth).
        """
        try:
            path = self._job_path(job_id)
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("state") != "running":
                return
            payload["state"] = "cancelled"
            payload["error"] = "Query was cancelled."
            payload["finished_at_wall"] = time.time()
            payload["has_result_file"] = False
            _write_json_atomic(self.root, path, payload)
        except Exception as exc:
            logger.warning("shared job %s cancel-mark failed: %s", job_id, exc)

    def flag_cancel(self, job_id: str) -> None:
        """Raise the cross-replica cancel flag (best-effort).

        The owner's publish-watcher polls this while the job runs and turns
        it into a local DuckDB interrupt — a cancel landing on ANY replica
        reaches the query no matter which replica owns it. The flag is
        cleaned up by `_drop` and by the next `publish_running` for the id.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                self.root,
                self._cancel_flag_path(job_id),
                {"cancelled_at_wall": time.time()},
            )
        except Exception as exc:
            logger.warning("shared job %s cancel-flag failed: %s", job_id, exc)

    def cancel_flagged(self, job_id: str) -> bool:
        try:
            return self._cancel_flag_path(job_id).exists()
        except Exception:
            return False

    # ------------------------------------------------------------ read
    def load(self, job_id: str) -> dict | None:
        """Read one shared record (or None). Best-effort."""
        try:
            path = self._job_path(job_id)
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("shared job %s read failed: %s", job_id, exc)
            return None

    def claim_fetch(self, job_id: str) -> bool:
        """Atomically claim the right to hand over this job's result.

        mkdir is atomic everywhere, so exactly one replica's fetch wins; the
        winner releases the claim via :meth:`mark_fetched`. A stale claim
        (the claimant crashed mid-fetch) may be stolen after
        `CLAIM_STEAL_SECONDS`.
        """
        claim = self._claim_dir(job_id)
        try:
            claim.mkdir(parents=True)
            return True
        except FileExistsError:
            try:
                age = time.time() - claim.stat().st_mtime
            except OSError:
                return False
            if age <= self.CLAIM_STEAL_SECONDS:
                return False
            self._release_claim(job_id)
            try:
                claim.mkdir(parents=True)
                return True
            except FileExistsError:
                return False
        except OSError:
            return False

    def _release_claim(self, job_id: str) -> None:
        try:
            self._claim_dir(job_id).rmdir()
        except OSError:
            pass

    def read_shared_result(self, job_id: str):
        """Read the spooled result Parquet (or None when absent/failed)."""
        try:
            import pyarrow.parquet as pq

            path = self._result_path(job_id)
            if not path.exists():
                return None
            return pq.read_table(path)
        except Exception as exc:
            logger.warning("shared job %s result read failed: %s", job_id, exc)
            return None

    def _cleanup(self) -> None:
        """TTL/cap eviction inside the shared dir (best-effort).

        Mirrors the in-memory policy: finished records age out after
        `SQLHANDLER_ASYNC_JOB_TTL`, oldest-finished-first when over the
        cap. Running tombstones are never TTL-evicted by the finished-job
        clock — a legitimately long job (up to SQLHANDLER_QUERY_TIMEOUT)
        must survive — but a tombstone older than the timeout plus a grace
        period is a zombie (its owner died) and is dropped. Stale fetch
        claims go the same way.
        """
        from .webui import _job_ttl  # the same knob, one definition

        ttl = _job_ttl()
        timeout = _query_timeout()
        max_running_age = (timeout + 300) if timeout > 0 else 3600
        now = time.time()
        finished: list[tuple[str, float]] = []  # (job_id, finished_at_wall)
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if data.get("state") == "running":
                submitted = data.get("submitted_at_wall") or 0
                if now - submitted > max_running_age:
                    self._drop(path.stem)
                continue
            finished_wall = data.get("finished_at_wall")
            if not finished_wall:
                self._drop(path.stem)  # legacy/partial record: unreferenceable
                continue
            if ttl > 0 and now - finished_wall > ttl:
                self._drop(path.stem)
                continue
            finished.append((path.stem, finished_wall))
        if len(finished) >= self.max_jobs:
            finished.sort(key=lambda e: e[1])
            for job_id, _wall in finished[: len(finished) - self.max_jobs + 1]:
                self._drop(job_id)
        for claim in self.root.glob("fetch-*"):
            try:
                if now - claim.stat().st_mtime > 2 * self.CLAIM_STEAL_SECONDS:
                    claim.rmdir()
            except OSError:
                pass

    def _drop(self, job_id: str) -> None:
        for path in (self._job_path(job_id), self._result_path(job_id)):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self._cancel_flag_path(job_id).unlink()
        except OSError:
            pass
        self._release_claim(job_id)


class McpJobManager:
    """Process-wide registry of MCP/REST async query jobs (bounded, TTL-evicted).

    Deliberately its OWN registry next to the web UI's
    :class:`~sqlhandler.webui.QueryJobManager` (which keeps its historical
    100-job/long-poll behavior byte-unchanged): this one enforces the MCP
    contract — a small default cap (`SQLHANDLER_MAX_JOBS`), the query
    timeout enforced via watchdog even without polling, and fetch-once
    results. Both registries submit the SAME `QueryJob` primitive against
    the SAME engine, so guard/timeout/cancellation semantics cannot drift.

    When `SQLHANDLER_JOBS_DIR` points at a shared directory, finished jobs
    are ALSO published there (see :class:`_SharedJobStore`) so any replica
    of a scaled-out deployment can poll/fetch/cancel them; without it the
    behavior is exactly the historical in-memory registry.
    """

    def __init__(self, max_jobs: int | None = None):
        self._max_jobs = max_jobs if (max_jobs is not None and max_jobs > 0) else max_jobs_env()
        self._records: OrderedDict[str, _JobRecord] = OrderedDict()
        self._lock = threading.RLock()
        self._shared: _SharedJobStore | None = None
        root = jobs_dir_env()
        if root:
            self._shared = _SharedJobStore(root, self._max_jobs)
            logger.info(
                "async query jobs: shared store enabled at %s (multi-replica hand-off; "
                "any replica can poll/fetch/cancel a finished job)",
                root,
            )

    @property
    def shared_store(self) -> _SharedJobStore | None:
        """The cross-replica store when `SQLHANDLER_JOBS_DIR` is set, else None."""
        return self._shared

    # ------------------------------------------------------------ submit
    def submit(
        self,
        engine: SqlEngine,
        sql: str,
        limit: int | None = None,
        params: object | None = None,
        version_as_of: int | None = None,
        caller=None,
    ) -> dict:
        """Validate + start a job; returns `{"job_id", "state"}`.

        The D2 read-only guard runs HERE (submit time) — a refused statement
        raises :class:`ValueError` before any job exists, exactly like the
        synchronous `run_sql`. Payload validation (params shape, snapshot
        version) is synchronous too: a bad payload is an immediate error, not
        a job that instantly fails. Over-cap submits are refused with
        `{"error": ..., "status": 429}`.

        `caller` (identity spine) rides the QueryJob (keyword-only) so the
        worker thread — where contextvars do NOT cross — records the outcome
        against the submitter.
        """
        if mcp_readonly_enabled():
            # Decision D2 at SUBMIT time: the same parser guard, the same
            # error text (named env included) the MCP run_sql produces.
            sql = assert_mcp_readonly(sql)
        else:
            # The opt-out still requires the SQL to PARSE (garbage is a
            # client error, not a job that fails in its thread).
            extract_statement_spans(sql)
        _validate_params(params)
        if version_as_of is not None:
            # Snapshot-version validation raises LakehouseError (the engine's
            # own type); a submit-time payload problem is a CLIENT error, so
            # it is re-raised as ValueError -> HTTP 400 on the REST surface
            # (the job's own re-validation still guards the engine path).
            try:
                _validate_snapshot_version(version_as_of, "Time travel")
            except LakehouseError as exc:
                raise ValueError(str(exc)) from exc

        with self._lock:
            self._cleanup()
            if len(self._records) >= self._max_jobs:
                return {
                    "error": _errors.enrich(
                        f"Too many active query jobs ({len(self._records)} of "
                        f"{self._max_jobs}, SQLHANDLER_MAX_JOBS); cancel or fetch "
                        "results and retry later."
                    ),
                    "status": 429,
                }
        # QueryJob construction re-validates (cheap), acquires the
        # query-concurrency gate (may queue up to SQLHANDLER_QUEUE_TIMEOUT —
        # its LakehouseError surfaces as a submit-time refusal) and starts
        # the worker thread. Done OUTSIDE the manager lock so submits never
        # serialize behind each other's queue waits.
        job = QueryJob(engine, sql, limit=limit, params=params, version_as_of=version_as_of, caller=caller)
        job_id = uuid.uuid4().hex
        with self._lock:
            if len(self._records) >= self._max_jobs:
                # A racing submit filled the last slot: cancel and refuse
                # (the losing job releases its gate slot when it unwinds).
                job.cancel()
                return {
                    "error": _errors.enrich(
                        f"Too many active query jobs ({self._max_jobs}, "
                        f"SQLHANDLER_MAX_JOBS); cancel or fetch results and retry later."
                    ),
                    "status": 429,
                }
            record = _JobRecord(job, job_id)
            self._records[job_id] = record
        self._arm_timeout(record)
        if self._shared is not None:
            # Tombstone FIRST so a foreign replica's very first poll says
            # "running" instead of "Unknown job id" (live-seen failure mode).
            self._shared.publish_running(job_id, sql)
            self._spawn_publish_watcher(record)
        return {"job_id": job_id, "state": job.state}

    def _spawn_publish_watcher(self, record: _JobRecord) -> None:
        """Publish the outcome to the shared store the moment the job ends.

        Without this, a finished job only reaches the store when the OWNER
        replica is polled — on a load-balanced client that would never
        happen (every poll lands elsewhere), so foreign replicas would
        report `running` forever. The watcher blocks on the job thread and
        publishes once; publishing is idempotent and the owner's own
        status/result calls take the same path (first writer wins, content
        identical).
        """

        def _watch() -> None:
            try:
                # Re-check the shared cancel flag while the job runs: a
                # cancel that landed on another replica must interrupt the
                # query HERE (the DuckDB interrupt handle is process-local,
                # so only the owner can deliver it). The wait timeout doubles
                # as the poll cadence; with the store absent/unreadable
                # `cancel_flagged` is False and this is a plain blocking wait.
                while True:
                    state = record.job.wait(self._shared.CANCEL_POLL_SECONDS)
                    if state != "running":
                        break
                    if self._shared.cancel_flagged(record.job_id):
                        record.job.cancel()
                        # stay in the loop: the interrupt may take a moment
                        # to unroll the query; the next non-running state
                        # breaks out.
                self.status(record.job_id)  # builds the payload + publishes
            except Exception:  # best-effort by contract
                logger.debug("publish watcher for job %s ended", record.job_id, exc_info=True)

        threading.Thread(target=_watch, daemon=True, name="sqlhandler-job-publish").start()

    # ------------------------------------------------------------ watchdog
    def _arm_timeout(self, record: _JobRecord) -> None:
        """Enforce SQLHANDLER_QUERY_TIMEOUT even when nobody polls."""
        timeout = _query_timeout()
        if timeout <= 0:
            return
        timer = threading.Timer(timeout, self._expire, args=(record.job_id,))
        timer.daemon = True
        with self._lock:
            record.timer = timer
        timer.start()

    def _expire(self, job_id: str) -> None:
        """Watchdog: interrupt a job that outlived the query timeout."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.timed_out or record.job.state != "running":
                return
            record.job.cancel()
            record.timed_out = True
            record.timeout_message = _errors.enrich(
                f"Query timed out after {_query_timeout()}s (SQLHANDLER_QUERY_TIMEOUT) and was cancelled."
            )
            logger.warning("async query job %s timed out and was cancelled", job_id)

    def _disarm(self, record: _JobRecord) -> None:
        timer = record.timer
        if timer is not None:
            timer.cancel()
            record.timer = None

    def _maybe_disarm_locked(self, record: _JobRecord) -> None:
        """Cancel the watchdog once the job finished on its own."""
        if record.timer is not None and record.job.state != "running":
            self._disarm(record)

    # -------------------------------------------------------------- lookup
    def get(self, job_id: str) -> _JobRecord | None:
        with self._lock:
            self._cleanup()
            return self._records.get(job_id)

    # -------------------------------------------------------------- status
    def status(self, job_id: str) -> dict:
        """Status snapshot for one job (no row data).

        Lookup order: the local registry first (the owner is authoritative
        while its job runs), then the shared store when configured — so on a
        scaled-out deployment any replica can report a job submitted or
        finished on another one. Neither knows the id: the historical 404.
        """
        with self._lock:
            record = self._records.get(job_id)
            if record is not None:
                if not record.fetched:
                    self._reconcile_shared_locked(record)
                fetched = record.fetched
                timed_out = record.timed_out
                self._maybe_disarm_locked(record)
            else:
                fetched = timed_out = False
            shared = None
            if record is None and self._shared is not None:
                shared = self._shared.load(job_id)
        if record is None:
            if shared is None:
                raise JobError(f"Unknown job id: {job_id}", status=404)
            if shared.get("state") == "running":
                shared = dict(shared)
                shared["note"] = (
                    "job submitted on another replica (SQLHANDLER_JOBS_DIR shared store); "
                    "its result becomes fetchable from any replica once it finishes"
                )
            return shared
        payload = record.job.info()
        state = record.effective_state()
        payload["state"] = state
        if record.timed_out and state == "error" and record.timeout_message:
            payload["error"] = record.timeout_message
        payload.update({"job_id": job_id, "result_fetched": fetched, "timed_out": timed_out})
        if state == "done" and not fetched:
            arrow = record.job.result
            payload["columns"] = [f.name for f in arrow.schema]
            payload["n_rows"] = arrow.num_rows
        self._publish_if_shared(record, payload)
        return payload

    def _reconcile_shared_locked(self, record: _JobRecord) -> None:
        """Adopt a cluster-wide fetch that happened on another replica.

        The shared record is the cluster's source of truth for fetch-once:
        if another replica already handed the result over, this replica must
        report `result_fetched: true` and release its local copy too —
        otherwise the owner would happily hand the data over a second time.
        Best-effort: a store read failure leaves the local view untouched.
        """
        if self._shared is None:
            return
        shared = self._shared.load(record.job_id)
        if shared is not None and shared.get("fetched") and not record.fetched:
            record.fetched = True
            record.fetched_at = time.monotonic()
            try:
                record.job.release_result()
            except Exception:
                pass

    def _publish_if_shared(self, record: _JobRecord, payload: dict) -> None:
        """Publish a finished job to the shared store (once; best-effort).

        Triggered by the owner's own status/result calls — the normal client
        flow polls at least once after submit, so the outcome reaches the
        shared store without any background thread. Running jobs are never
        published (only the submit-time tombstone represents them).
        """
        if self._shared is None or record.shared_published:
            return
        with self._lock:
            if record.shared_published or record.job.state == "running":
                return
            record.shared_published = True
        arrow = None
        if record.job.state == "done" and not record.fetched:
            try:
                arrow = record.job.result
            except LakehouseError:
                arrow = None
        self._shared.publish(record, payload, arrow=arrow)

    # -------------------------------------------------------------- result
    def take_result(self, job_id: str):
        """Return the Arrow result ONCE, then free it from memory.

        Raises :class:`JobError` for an unknown id (404), a job still
        running (409 — poll `query_status`), an error/cancelled/timeout
        state (400), or an already-fetched result (409). After a successful
        fetch the spooled table is released (`QueryJob.release_result`), so
        the registry never accumulates results beyond one fetch each.

        With a shared store configured, a job that finished on ANOTHER
        replica is served from the shared Parquet sidecar; the fetch is
        claimed atomically so exactly one fetch succeeds cluster-wide, and a
        second fetch — from any replica — gets the same 409.
        """
        with self._lock:
            record = self._records.get(job_id)
        if record is None:
            return self._take_shared_result(job_id)
        with self._lock:
            if not record.fetched:
                self._reconcile_shared_locked(record)
            if record.fetched:
                raise JobError(self._fetched_message(job_id), status=409)
            self._maybe_disarm_locked(record)
        if record.timed_out:
            # The watchdog already interrupted this job (its thread may still
            # be unwinding): report the timeout, never "still running".
            raise JobError(record.timeout_message or "Query timed out.", status=400)
        state = record.job.state
        if state == "running":
            raise JobError(f"Job {job_id} is still running; poll query_status first.", status=409)
        if state == "cancelled":
            raise JobError("Query was cancelled.", status=400)
        if state != "done":
            raise JobError(
                _errors.enrich(record.job.error or f"Job did not complete (state: {state})."),
                status=400,
            )
        if self._shared is not None and not self._shared.claim_fetch(job_id):
            # A foreign replica is handing the result over right now (or
            # already did): the once-only contract holds cluster-wide.
            shared = self._shared.load(job_id) or {}
            if shared.get("fetched"):
                raise JobError(self._fetched_message(job_id), status=409)
            raise JobError(
                f"Job {job_id}'s result is being fetched on another replica right now; "
                "retry shortly if that fetch does not complete.",
                status=409,
            )
        arrow = record.job.result
        with self._lock:
            if record.fetched:  # a racing fetch won; honor once-only
                if self._shared is not None:
                    self._shared.mark_fetched(job_id)  # release our claim
                raise JobError(self._fetched_message(job_id), status=409)
            record.fetched = True
            record.fetched_at = time.monotonic()
            record.job.release_result()
        if self._shared is not None:
            self._shared.mark_fetched(job_id)
        return arrow

    def _take_shared_result(self, job_id: str):
        """Fetch-once path for a job that finished on ANOTHER replica."""
        if self._shared is None:
            raise JobError(f"Unknown job id: {job_id}", status=404)
        payload = self._shared.load(job_id)
        if payload is None:
            raise JobError(f"Unknown job id: {job_id}", status=404)
        if payload.get("fetched"):
            raise JobError(self._fetched_message(job_id), status=409)
        state = payload.get("state")
        if state == "running":
            # The owner has not finished (and thus not published the outcome).
            raise JobError(f"Job {job_id} is still running; poll query_status first.", status=409)
        if state == "cancelled":
            raise JobError("Query was cancelled.", status=400)
        if state != "done":
            raise JobError(
                _errors.enrich(payload.get("error") or f"Job did not complete (state: {state})."),
                status=400,
            )
        if not payload.get("has_result_file"):
            raise JobError(
                f"Job {job_id} finished on another replica but its result is not in the "
                "shared store (it may already have been fetched there, or result sharing "
                "was disabled when it finished). Resubmit for a fresh result.",
                status=409,
            )
        if not self._shared.claim_fetch(job_id):
            payload = self._shared.load(job_id) or payload
            if payload.get("fetched"):
                raise JobError(self._fetched_message(job_id), status=409)
            raise JobError(
                f"Job {job_id}'s result is being fetched on another replica right now; "
                "retry shortly if that fetch does not complete.",
                status=409,
            )
        arrow = self._shared.read_shared_result(job_id)
        if arrow is None:
            self._shared.mark_fetched(job_id)  # release the claim either way
            raise JobError(
                f"Job {job_id}'s shared result could not be read from "
                f"{self._shared.root}; see the server log. Resubmit for a fresh result.",
                status=409,
            )
        self._shared.mark_fetched(job_id)
        return arrow

    @staticmethod
    def _fetched_message(job_id: str) -> str:
        return f"Job {job_id} was already fetched; results are handed over once. Resubmit the query for a fresh result."

    # -------------------------------------------------------------- cancel
    def cancel(self, job_id: str) -> dict:
        """Cancel a running job; unknown ids raise JobError(404).

        The interrupt only reaches a job running on THIS replica (the DuckDB
        connection handle is process-local); a cancel for a foreign running
        job says so honestly and cancels the shared tombstone, so polls on
        other replicas stop reporting it as running. Cancelling an already
        finished job is a no-op with its current state (historical behavior).
        """
        with self._lock:
            record = self._records.get(job_id)
        if record is None:
            if self._shared is None:
                raise JobError(f"Unknown job id: {job_id}", status=404)
            payload = self._shared.load(job_id)
            if payload is None:
                raise JobError(f"Unknown job id: {job_id}", status=404)
            # Forward the cancel to the owner THROUGH the shared store: the
            # owner's publish-watcher polls the flag and interrupts DuckDB
            # locally. mark_cancelled still flips the tombstone so polls on
            # other replicas stop reporting "running" immediately; the owner's
            # own finish-publish later overwrites it with the authoritative
            # outcome (cancelled — or done, if the query finished first; the
            # shared state self-heals toward the truth).
            self._shared.flag_cancel(job_id)
            self._shared.mark_cancelled(job_id)
            return {
                "job_id": job_id,
                "state": payload.get("state", "running"),
                "interrupted": False,
                "note": (
                    "job is running on another replica; the cancel was forwarded via "
                    "the shared store and the owner will interrupt the query shortly "
                    "(poll any replica for the final state)"
                ),
            }
        with self._lock:
            self._disarm(record)
        interrupted = record.job.cancel()
        if self._shared is not None:
            self._shared.mark_cancelled(job_id)
        return {
            "job_id": job_id,
            "state": record.effective_state(),
            "interrupted": interrupted,
        }

    # -------------------------------------------------------------- cleanup
    def _cleanup(self) -> None:
        """Evict finished jobs past the TTL; keep the registry under the cap.

        Mirrors the web QueryJobManager: finished jobs age out after
        `SQLHANDLER_ASYNC_JOB_TTL` seconds, oldest finished first when room
        is needed for a submit; a registry full of RUNNING jobs is refused at
        submit time, never force-evicted.
        """
        from .webui import _job_ttl  # the same knob, one definition

        ttl = _job_ttl()
        now = time.monotonic()
        for record in self._records.values():
            if record.job.state != "running":
                self._maybe_disarm_locked(record)
        if ttl > 0:
            for job_id, record in list(self._records.items()):
                finished = record.finished_at()
                if finished is not None and now - finished > ttl:
                    del self._records[job_id]
        while len(self._records) >= self._max_jobs:
            for job_id, record in self._records.items():
                if record.job.state != "running":
                    del self._records[job_id]
                    break
            else:
                break

    def stats(self) -> dict:
        """Registry snapshot for introspection (counts, cap, sharing)."""
        with self._lock:
            running = sum(1 for r in self._records.values() if r.job.state == "running")
            payload = {
                "tracked": len(self._records),
                "running": running,
                "max_jobs": self._max_jobs,
            }
            if self._shared is not None:
                payload["shared_store"] = str(self._shared.root)
            return payload


# ---------------------------------------------------------------------------
# Process-wide manager (shared by the MCP tools and the /api/jobs routes)
# ---------------------------------------------------------------------------

_manager_lock = threading.Lock()
_manager: McpJobManager | None = None


def job_manager() -> McpJobManager:
    """The process-wide job registry (restart clears it — by design)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = McpJobManager()
            note = (
                "async query jobs enabled: in-memory registry (restart clears it), "
                f"cap SQLHANDLER_MAX_JOBS={_manager._max_jobs}, timeout from "
                "SQLHANDLER_QUERY_TIMEOUT, results are handed over once then freed"
            )
            if _manager.shared_store is not None:
                note += "; shared store SQLHANDLER_JOBS_DIR=" + str(_manager.shared_store.root)
            logger.info(note)
        return _manager


def reset_job_manager() -> None:
    """Test hook: drop the process-wide registry (a fresh one builds lazily)."""
    global _manager
    with _manager_lock:
        _manager = None


# ---------------------------------------------------------------------------
# API-level handlers (shared by the MCP tool wrappers and the REST routes)
# ---------------------------------------------------------------------------


def api_job_submit(engine: SqlEngine, body: dict, *, caller=None) -> dict:
    """POST /api/jobs + MCP query_submit — start a read-only query job.

    `caller` (identity spine) rides the QueryJob across the thread
    boundary so the outcome record attributes to the submitter.
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    result = job_manager().submit(
        engine,
        str(body.get("sql", "")),
        limit=body.get("limit"),
        params=body.get("params"),
        version_as_of=body.get("version_as_of"),
        caller=caller,
    )
    if result.get("error"):
        return result  # carries its own "status" for the route wrapper
    result["note"] = "poll query_status(job_id); fetch once with query_result(job_id)"
    return result


def api_job_status(job_id: str) -> dict:
    """GET /api/jobs/{id} + MCP query_status."""
    return job_manager().status(job_id)


def api_job_result(job_id: str):
    """GET /api/jobs/{id}/result + MCP query_result — the Arrow table, once."""
    return job_manager().take_result(job_id)


def api_job_cancel(job_id: str) -> dict:
    """DELETE /api/jobs/{id} + MCP query_cancel."""
    return job_manager().cancel(job_id)
