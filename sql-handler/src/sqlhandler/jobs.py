"""Async MCP query jobs — submit / status / result / cancel (additive, Wave 5).

The audit's "S effort, P0-value" item: the :class:`~sqlhandler.engine.QueryJob`
machinery (own thread, DuckDB ``interrupt()`` cancellation, concurrency gate,
row caps, audit) has powered the web async API since 0.9, but MCP callers had
no way to start a long query, walk away, and collect the result later. This
module exposes the same primitive as MCP tools (``query_submit`` /
``query_status`` / ``query_result`` / ``query_cancel``) and REST equivalents
under ``/api/jobs/*``.

Non-negotiable constraints (all shared with the synchronous path):

* **Same engine path** — a job IS an :class:`~sqlhandler.engine.QueryJob`, so
  it honors ``SQLHANDLER_QUERY_TIMEOUT``, the row caps, the query-concurrency
  gate, the memory budget, and the JSONL audit log.
* **Same read-only guard, at SUBMIT time** — decision D2's parser guard
  (``sqlguard.assert_mcp_readonly``) runs on the submitted SQL BEFORE any job
  is created, so DDL/DML is refused synchronously with the same error text
  ``run_sql`` produces (and nothing is ever started). The opt-out
  (``SQLHANDLER_MCP_READONLY=0``) applies exactly as it does for ``run_sql``;
  queries that can see an attached external catalog stay SELECT-only
  unconditionally at execution time (engine-level).
* **Same timeout** — ``SQLHANDLER_QUERY_TIMEOUT`` (default 600s) is enforced
  by a watchdog timer armed at submit: a job that outlives the timeout is
  interrupted via DuckDB and reported as ``error`` with the same
  "Query timed out after Ns" message the synchronous path raises. The
  watchdog fires even when nobody is polling, so a runaway job can never
  hold its concurrency-gate slot forever.
* **Same MAX_ROWS bounding** — the job's result is capped by
  ``SQLHANDLER_MAX_ROWS`` inside the query (QueryJob), and the rendered
  output honors ``SQLHANDLER_MAX_OUTPUT_ROWS`` like ``run_sql``.
* **Bounded registry** — at most ``SQLHANDLER_MAX_JOBS`` jobs (default 8) are
  tracked; submitting beyond the cap is refused with a clear message (HTTP
  429 on the REST surface). Finished jobs are TTL-evicted using the same
  ``SQLHANDLER_ASYNC_JOB_TTL`` knob as the web async API.
* **Fetch-once results** — ``query_result`` hands the result over ONCE, then
  frees the spooled Arrow table from memory. A second fetch is refused
  (HTTP 409 on REST); ``query_status`` keeps working and reports
  ``"result_fetched": true``.
* **In-memory registry** — restart clears it (jobs are not durable by
  design; resubmit after a restart). Documented in the tool descriptions,
  README and FEATURES.md.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict

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


def max_jobs_env() -> int:
    """Tracked-job cap (``SQLHANDLER_MAX_JOBS``, default 8).

    Garbage or non-positive values fall back to the default — the registry is
    bounded by design, so an operator typo can never disable the bound.
    (Named ``max_jobs_env`` so the ``McpJobManager(max_jobs=...)`` parameter
    can never shadow it.)
    """
    raw = os.environ.get(_MAX_JOBS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return _MAX_JOBS_DEFAULT
    return value if value > 0 else _MAX_JOBS_DEFAULT


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
        """The user-visible state (a timed-out job reports ``error``)."""
        if self.timed_out and self.job.state != "done":
            return "error"
        return self.job.state


class McpJobManager:
    """Process-wide registry of MCP/REST async query jobs (bounded, TTL-evicted).

    Deliberately its OWN registry next to the web UI's
    :class:`~sqlhandler.webui.QueryJobManager` (which keeps its historical
    100-job/long-poll behavior byte-unchanged): this one enforces the MCP
    contract — a small default cap (``SQLHANDLER_MAX_JOBS``), the query
    timeout enforced via watchdog even without polling, and fetch-once
    results. Both registries submit the SAME ``QueryJob`` primitive against
    the SAME engine, so guard/timeout/cancellation semantics cannot drift.
    """

    def __init__(self, max_jobs: int | None = None):
        self._max_jobs = max_jobs if (max_jobs is not None and max_jobs > 0) else max_jobs_env()
        self._records: OrderedDict[str, _JobRecord] = OrderedDict()
        self._lock = threading.RLock()

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
        """Validate + start a job; returns ``{"job_id", "state"}``.

        The D2 read-only guard runs HERE (submit time) — a refused statement
        raises :class:`ValueError` before any job exists, exactly like the
        synchronous ``run_sql``. Payload validation (params shape, snapshot
        version) is synchronous too: a bad payload is an immediate error, not
        a job that instantly fails. Over-cap submits are refused with
        ``{"error": ..., "status": 429}``.

        ``caller`` (identity spine) rides the QueryJob (keyword-only) so the
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
        return {"job_id": job_id, "state": job.state}

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

    def _require(self, job_id: str) -> _JobRecord:
        record = self.get(job_id)
        if record is None:
            raise JobError(f"Unknown job id: {job_id}", status=404)
        return record

    def status(self, job_id: str) -> dict:
        """Status snapshot for one job (no row data)."""
        with self._lock:
            record = self._require(job_id)
            fetched = record.fetched
            timed_out = record.timed_out
            self._maybe_disarm_locked(record)
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
        return payload

    # -------------------------------------------------------------- result
    def take_result(self, job_id: str):
        """Return the Arrow result ONCE, then free it from memory.

        Raises :class:`JobError` for an unknown id (404), a job still running
        (409 — poll ``query_status``), an error/cancelled/timeout state
        (400), or an already-fetched result (409). After a successful fetch
        the spooled table is released (``QueryJob.release_result``), so the
        registry never accumulates results beyond one fetch each.
        """
        with self._lock:
            record = self._require(job_id)
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
        arrow = record.job.result
        with self._lock:
            if record.fetched:  # a racing fetch won; honor once-only
                raise JobError(self._fetched_message(job_id), status=409)
            record.fetched = True
            record.fetched_at = time.monotonic()
            record.job.release_result()
        return arrow

    @staticmethod
    def _fetched_message(job_id: str) -> str:
        return f"Job {job_id} was already fetched; results are handed over once. Resubmit the query for a fresh result."

    # -------------------------------------------------------------- cancel
    def cancel(self, job_id: str) -> dict:
        """Cancel a running job; unknown ids raise JobError(404)."""
        with self._lock:
            record = self._require(job_id)
            self._disarm(record)
        interrupted = record.job.cancel()
        return {
            "job_id": job_id,
            "state": record.effective_state(),
            "interrupted": interrupted,
        }

    # -------------------------------------------------------------- cleanup
    def _cleanup(self) -> None:
        """Evict finished jobs past the TTL; keep the registry under the cap.

        Mirrors the web QueryJobManager: finished jobs age out after
        ``SQLHANDLER_ASYNC_JOB_TTL`` seconds, oldest finished first when room
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
        """Registry snapshot for introspection (counts, cap)."""
        with self._lock:
            running = sum(1 for r in self._records.values() if r.job.state == "running")
            return {
                "tracked": len(self._records),
                "running": running,
                "max_jobs": self._max_jobs,
            }


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
            logger.info(
                "async query jobs enabled: in-memory registry (restart clears it), "
                "cap SQLHANDLER_MAX_JOBS=%d, timeout from SQLHANDLER_QUERY_TIMEOUT, "
                "results are handed over once then freed",
                _manager._max_jobs,
            )
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

    ``caller`` (identity spine) rides the QueryJob across the thread
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
