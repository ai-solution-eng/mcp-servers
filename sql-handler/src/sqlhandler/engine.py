"""Shared, backend-agnostic SQL engine.

A :class: SqlEngine owns everything that does not care *where* the data
lives: the in-process caches (table list, describe results, open pyarrow
Datasets), the DuckDB query path, columnar scans, prewarm and cache stats.
It talks to storage only through the thin :class: DataProvider interface,
so OneLake and S3/MinIO (and any future backend) share exactly this code.

Design notes (kept from the original OneLake handler):

- A single process-wide engine is reused (a fresh one per tool call would
  silently discard the table-list cache on every request).
- list_tables / describe_table are cached for cache_ttl seconds - these
  hit the metadata endpoint (DFS REST / S3 list) and the file footers, and
  agents repeat them every session.
- Open pyarrow Datasets are reused per table for dataset_cache_ttl seconds,
  LRU-bounded by dataset_cache_tables - this skips re-reading the metadata
  (Delta _delta_log / Parquet footer) on every query. Only the handle is
  held; rows are still read from the source on each scan.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

if TYPE_CHECKING:
    import duckdb

from . import errors as _errors
from . import observability, resources
from . import policy as policy_mod
from . import writes as writes_mod
from .external import (
    AttachSpec,
    apply_external,
    parse_attach_config,
    sql_references_attach,
    validate_qualified_name,
)
from .l2cache import L2ResultCache, load_l2_config
from .policy import TableRule, policy_store
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version
from .rawfiles import is_raw_format
from .sqlguard import _explain_inner_sql, assert_attached_readonly

logger = logging.getLogger("sqlhandler.engine")

# The CURRENT caller, carried per request by the server layer (Stage 1) and
# passed EXPLICITLY into the engine's entry points (keyword ``caller``).
# contextvars DO work within one request's async task (the middleware sets
# the caller there and the tool handler reads it in the same task), but they
# do NOT cross QueryJob's raw threading.Thread — so the engine's query path
# takes the caller as an argument and hands it to the job/audit directly
# (the verified thread-boundary hazard). This module-level contextvar is the
# ASYNC-side default only.
_CALLER_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("sqlhandler_engine_caller", default=None)


def set_current_caller(caller) -> object:
    """Bind the caller for the CURRENT async context (the server middleware
    sets it; tool handlers read it via :func:`current_caller`). Returns the
    token for :func:`reset_current_caller`."""
    return _CALLER_CONTEXT.set(caller)


def reset_current_caller(token) -> None:
    _CALLER_CONTEXT.reset(token)


def current_caller():
    """The caller bound in this context, or None (anonymous/dev/stdio)."""
    return _CALLER_CONTEXT.get()


def _caller_key_fp(caller) -> str | None:
    """The caller's key fingerprint when one exists ('' never — None only)."""
    if caller is None:
        return None
    return getattr(caller, "key_fp", None)


def _query_memory_size() -> int:
    """How many recent query outcomes to remember (SQLHANDLER_QUERY_MEMORY_SIZE).

    Recorded (sql, duration, rows, error) tuples power the query-memory MCP
    resource — later agent sessions reuse proven query patterns instead of
    rediscovering them. 0 disables recording. Default 50.
    """
    raw = os.environ.get("SQLHANDLER_QUERY_MEMORY_SIZE", "")
    try:
        return max(int(raw), 0) if raw else 50
    except ValueError:
        return 50


# Usage counts flush to the disk-warm cache after this many dataset opens
# (piggybacked on the per-query note_query hook — no IO on the scan path).
_USAGE_SAVE_EVERY = 20


def _query_timeout() -> float:
    """Per-query wall-clock timeout in seconds (SQLHANDLER_QUERY_TIMEOUT).

    Applies to the DuckDB SQL path (MCP run_sql and the web API alike);
    default 600s (decision D5): a runaway query used to hold one of the
    concurrency-gate slots forever. On expiry the query is interrupted
    inside DuckDB (not leaked) and a LakehouseError surfaces. 0 restores
    the old no-timeout behavior; queries longer than 10 minutes must opt
    in via this env.
    """
    raw = os.environ.get("SQLHANDLER_QUERY_TIMEOUT", "")
    try:
        return max(float(raw), 0.0) if raw else 600.0
    except ValueError:
        return 600.0


# After a timeout-triggered interrupt, how long to wait for the query thread
# to unwind before reporting the timeout anyway (the result is discarded
# either way; this only bounds the error latency).
_CANCEL_GRACE_SECONDS = 5.0


def _max_concurrent_queries() -> int:
    """Max simultaneous DuckDB queries (SQLHANDLER_MAX_CONCURRENT_QUERIES).

    Each QueryJob runs in its own thread against its own DuckDB connection;
    without a cap an agent burst (or a loop of eager clients) can pile up
    dozens of scans on the same pod. Default 8; 0 = unlimited (old behavior).
    Excess queries QUEUE (waiting for a slot) up to SQLHANDLER_QUEUE_TIMEOUT
    seconds, then fail with a clear error instead of running unbounded.
    """
    raw = os.environ.get("SQLHANDLER_MAX_CONCURRENT_QUERIES", "")
    try:
        return max(int(raw), 0) if raw else 8
    except ValueError:
        return 8


def _queue_timeout() -> float:
    """Seconds a query may wait for a concurrency slot (SQLHANDLER_QUEUE_TIMEOUT)."""
    raw = os.environ.get("SQLHANDLER_QUEUE_TIMEOUT", "")
    try:
        return max(float(raw), 0.0) if raw else 30.0
    except ValueError:
        return 30.0


class _QueryGate:
    """A resizable-ish concurrency gate for queries (semaphore based).

    Sized once from SQLHANDLER_MAX_CONCURRENT_QUERIES at first use. Acquire
    blocks (queueing) up to SQLHANDLER_QUEUE_TIMEOUT; cross-thread release
    is safe with a plain Semaphore.
    """

    def __init__(self):
        self._sem: threading.Semaphore | None = None
        self._size = -1
        self._lock = threading.Lock()

    def _semaphore(self) -> threading.Semaphore | None:
        size = _max_concurrent_queries()
        if size <= 0:
            return None
        with self._lock:
            if self._sem is None or self._size != size:
                # Tests reconfigure via env: rebuild when the size changes.
                self._sem = threading.Semaphore(size)
                self._size = size
            return self._sem

    def acquire(self) -> None:
        sem = self._semaphore()
        if sem is None:
            return
        timeout = _queue_timeout()
        if not sem.acquire(timeout=timeout):
            raise LakehouseError(
                _errors.enrich(
                    f"Too many concurrent queries (limit {_max_concurrent_queries()}), and the "
                    f"queue wait of {_queue_timeout()}s expired. Retry later."
                )
            )

    def release(self) -> None:
        sem = self._sem
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass


_query_gate = _QueryGate()


class QueryJob:
    """One DuckDB query running on its own thread: cancellable and observable.

    The engine's synchronous :meth:`SqlEngine.query_duckdb` is implemented ON
    TOP of this class (submit + wait), and the web API's async-query endpoints
    submit the same jobs and poll them — one execution path for both, so
    timeouts and cancellation behave identically everywhere.

    Cancellation works because the connection lives for the duration of the
    job: ``cancel()`` calls DuckDB's ``interrupt()`` from the caller's thread,
    which raises inside the running query.
    """

    def __init__(
        self,
        engine: SqlEngine,
        sql: str,
        limit: int | None = None,
        params: object | None = None,
        version_as_of: int | None = None,
        row_cap: int | None = None,
        caller=None,
    ):
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        self._engine = engine
        self.sql = sql
        self._limit = limit
        self._params = _validate_params(params)
        self._version = version_as_of
        # Explicit cap override for this query (None = SQLHANDLER_MAX_ROWS).
        # The export endpoint raises it so a file download isn't truncated
        # by the (much lower) default LLM-payload cap.
        self._row_cap = row_cap
        # THREAD-BOUNDARY RULE (identity spine): contextvars do NOT cross a
        # raw threading.Thread — the caller rides the job object so the
        # outcome record (query memory + audit) attributes correctly.
        self._caller = caller
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None  # live only while the query runs (cancel handle)
        self._state = "running"
        self._error: str | None = None
        self._result: pa.Table | None = None
        self._cancelled = False
        self._t0 = time.monotonic()
        self._elapsed_ms: float | None = None
        # Concurrency gate: acquire BEFORE the thread starts so an over-cap
        # query queues in the caller's thread (works for both the sync path
        # and the async API, whose submit runs off the event loop). Raises
        # LakehouseError when the queue wait expires — construction fails,
        # no job exists.
        _query_gate.acquire()
        self._gate_held = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="sqlhandler-query")
        self._thread.start()

    # ---------------------------------------------------------- execution
    def _run(self) -> None:
        import duckdb

        con = duckdb.connect()
        with self._lock:
            self._con = con
        try:
            if self._engine._sql_needs_external(self.sql):
                # The attached-catalog connection is SELECT-only, UNCONDITIONALLY
                # (see sqlhandler/external.py: "the database is a source, never
                # a sink"). Enforced BEFORE any scanner extension is loaded, so
                # even a caller who opted out of the MCP read-only mode
                # (SQLHANDLER_MCP_READONLY=0) cannot use the attach connection
                # as an exfiltration sink: ATTACH/INSERT/COPY/multi-statement
                # scripts are refused here, and the plain (lake) connection this
                # job would otherwise use never has the attached catalogs — nor
                # the scanner extensions to create one. Decision D2's escape
                # hatch restores DDL for LAKE data only.
                assert_attached_readonly(self.sql)
                # Extension LOAD + ATTACH must precede the fs lockdown: both
                # use DuckDB's filesystem layer internally (the scanner .so
                # and the attach bind), while query-time data fetch does not
                # (libpq talks to the server directly). After the lockdown
                # the attached catalogs stay fully queryable and the fs
                # protections still hold — see sqlhandler/external.py.
                apply_external(con, self._engine.attaches)
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            # THE CALLER CROSSES THE THREAD BOUNDARY HERE: the masking views
            # (policy enforcement) register under the JOB's caller — inside
            # this worker thread the ambient contextvar is empty.
            self._engine._register_schema(con, self.sql, version=self._version, caller=self._caller)
            rel = con.sql(self.sql, params=self._params)
            # Row-cap semantics: a LIMIT is pushed into the query so an
            # unbounded SELECT can't materialize millions of rows in memory.
            cap = self._row_cap if self._row_cap is not None else _max_rows()
            eff = self._limit if (self._limit is not None and self._limit >= 0) else None
            if cap > 0:
                eff = min(eff, cap) if eff is not None else cap
            if eff is not None:
                rel = rel.limit(eff)
            arrow_table = rel.arrow()
            if isinstance(arrow_table, pa.RecordBatchReader):
                arrow_table = arrow_table.read_all()
            if eff is not None and arrow_table.num_rows > eff:
                arrow_table = arrow_table.slice(0, eff)
            with self._lock:
                if self._cancelled:
                    # cancel() raced the finish and lost — the caller still
                    # asked for cancellation, so the result is discarded.
                    self._state = "cancelled"
                    self._error = "Query was cancelled."
                    self._result = None
                else:
                    self._result = arrow_table
                    self._state = "done"
                    self._elapsed_ms = (time.monotonic() - self._t0) * 1000
            if self._state == "done":
                self._engine._record_outcome(
                    self.sql, self._elapsed_ms, arrow_table.num_rows, state="ok", caller=self._caller
                )
        except Exception as exc:
            elapsed = (time.monotonic() - self._t0) * 1000
            with self._lock:
                self._elapsed_ms = elapsed
                if self._cancelled:
                    self._state = "cancelled"
                    self._error = "Query was cancelled."
                else:
                    self._state = "error"
                    hinted = _with_hints(self._engine, self.sql, exc)
                    self._error = f"DuckDB query failed: {hinted}"
            self._engine._record_outcome(
                self.sql, elapsed, None, state=self._state, error=self._error, caller=self._caller
            )
        finally:
            try:
                con.close()
            except Exception:
                pass
            with self._lock:
                self._con = None
            if self._gate_held:
                self._gate_held = False
                _query_gate.release()

    # ------------------------------------------------------------ control
    def cancel(self) -> bool:
        """Interrupt a running query; True if a live query was interrupted."""
        with self._lock:
            self._cancelled = True
            con = self._con
        if con is None:
            return False
        try:
            con.interrupt()
            return True
        except Exception:
            return False

    def wait(self, timeout: float | None = None) -> str:
        """Block until the job finishes (or ``timeout`` elapses); returns state."""
        self._thread.join(timeout)
        return self.state

    @property
    def state(self) -> str:
        """running | done | error | cancelled."""
        with self._lock:
            if self._state == "running" and not self._thread.is_alive():
                # Defensive: a thread that died without recording a state is
                # an error, not an eternal "running".
                self._state = "error"
                self._error = self._error or "Query thread ended without a result."
            return self._state

    @property
    def elapsed_ms(self) -> float | None:
        return self._elapsed_ms

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def result(self) -> pa.Table:
        """The arrow result; raises LakehouseError unless the job is done."""
        state = self.state
        if state == "done":
            with self._lock:
                if self._result is None:
                    # Fetch-once flows (async query jobs) release the spooled
                    # table after handing it over; a second fetch is refused
                    # instead of tripping the assert below.
                    raise LakehouseError(
                        "Query result was already fetched and released (results are handed over once)."
                    )
                return self._result
        if state == "cancelled":
            raise LakehouseError("Query was cancelled.")
        raise LakehouseError(self._error or f"Query did not complete (state: {state}).")

    def release_result(self) -> None:
        """Drop the spooled result table (fetch-once async-job flows).

        The async job registry hands each result over exactly once and frees
        it immediately afterwards, so a registry of finished jobs cannot
        accumulate unbounded Arrow tables in memory. Status stays queryable
        (state/elapsed/error) — only the row data is dropped. Synchronous
        callers never call this; their behavior is unchanged.
        """
        with self._lock:
            self._result = None

    def info(self) -> dict:
        """Status snapshot for the async-query API (no row data)."""
        return {
            "sql": self.sql,
            "state": self.state,
            "elapsed_ms": round(self._elapsed_ms, 1) if self._elapsed_ms is not None else None,
            "error": self._error,
        }


def _max_rows() -> int:
    """Server-side row cap for SQL results (SQLHANDLER_MAX_ROWS, default 1000).

    Prevents a client with no limit from materializing an unbounded result
    (e.g. SELECT * over a multi-million-row table) that would exhaust memory.
    0 disables the cap.
    """
    raw = os.environ.get("SQLHANDLER_MAX_ROWS", "")
    try:
        return max(int(raw), 0) if raw else 1000
    except ValueError:
        return 1000


def _preview_fastpath_enabled() -> bool:
    """Whether bare-LIMIT previews take the first-row-group fast path.

    ``SQLHANDLER_PREVIEW_FASTPATH`` — on by default (the empty/unset value
    means ON, matching ``SQLHANDLER_LIST_ASYNC_REFRESH``'s default-true
    convention; "0/false/no/off" switches it off, anything else keeps it
    on). Cheap per call (one env read), never raises — the fast path is an
    accelerator, and a garbage value must degrade to "on", not break
    queries.
    """
    return os.environ.get("SQLHANDLER_PREVIEW_FASTPATH", "").strip().lower() not in ("0", "false", "no", "off")


def _prewarm_rowgroups() -> int:
    """Row groups to prewarm per table (SQLHANDLER_PREWARM_ROWGROUPS, default 1).

    0 disables the data prewarm entirely (describe-cache warming only —
    the historical behavior). Kept small by design: the point is the first
    query of the day hitting warm object-store blocks, not staging the
    table.
    """
    raw = os.environ.get("SQLHANDLER_PREWARM_ROWGROUPS", "")
    try:
        return max(int(raw), 0) if raw else 1
    except ValueError:
        return 1


def _profile_max_rows() -> int:
    """Row cap for the profiling input (SQLHANDLER_PROFILE_MAX_ROWS).

    ``profile_table`` scans data to compute column statistics; this bounds
    how many rows of each table are summarized (default 1,000,000; 0 = the
    full table). Statistics over a large uniform sample are what an LLM
    needs to write correct filters; the exact full-table row count still
    comes from the Parquet/Delta metadata for free.
    """
    raw = os.environ.get("SQLHANDLER_PROFILE_MAX_ROWS", "")
    try:
        return max(int(raw), 0) if raw else 1_000_000
    except ValueError:
        return 1_000_000


def _resolve_sample_limit(limit: int | None) -> int | None:
    """Resolve a sample_rows limit with scan_table's D4 semantics.

    ``None``/negative means "no explicit limit": resolve to the
    ``SQLHANDLER_MAX_ROWS`` cap (a positive int, or ``None`` when the cap
    is disabled — the caller then applies the PROFILE cap or scans unbounded
    exactly as scan_arrow does). An explicit positive limit is honored
    exactly; an explicit ``0`` stays an explicit empty sample. Mirrors
    server._resolve_scan_limit so both tools behave identically at the
    boundary.
    """
    if limit is None or limit < 0:
        cap = _max_rows()
        return cap if cap > 0 else None
    return limit


def _duckdb_fs_lockdown(con) -> None:
    """Disable DuckDB's own file/network access for SQL queries (default on).

    Tables reach DuckDB as registered pyarrow datasets, and ALL object-store
    IO (S3/ABFS/NFS) is done by pyarrow *outside* DuckDB — so locking DuckDB's
    built-in filesystems away costs nothing and closes real holes on a
    network-facing endpoint: `read_csv('/etc/passwd')`, `COPY ... TO '/tmp'`,
    `parquet_scan(...)` of local files, and extension-based URL fetches
    (httpfs) all fail closed. Opt out with SQLHANDLER_DUCKDB_FILE_ACCESS=1 if
    a query genuinely needs DuckDB file/table functions.
    """
    raw = os.environ.get("SQLHANDLER_DUCKDB_FILE_ACCESS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return
    try:
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
    except Exception:  # older DuckDB without a knob: fail open rather than break queries
        logger.debug("DuckDB filesystem lockdown partially unavailable", exc_info=True)


_budget_logged = False


def _apply_memory_budget(con) -> None:
    """Give DuckDB a budget derived from the container's own limits (fail open).

    DuckDB's defaults are sized from the NODE's RAM (80% of /proc/meminfo) —
    on a big node with a small pod that means a single wide scan can grow RSS
    past the pod's cgroup limit and get the process OOMKilled, wiping every
    in-process cache (table list, describe results, open datasets). Setting
    ``memory_limit`` to a fraction of the container limit makes DuckDB spill
    to ``temp_directory`` (k8s: mount an emptyDir there) instead; ``threads``
    matches the CPU limit so the pool doesn't get sized from the node's cores.
    Every step is best-effort: an old DuckDB or an unreadable cgroup just
    leaves DuckDB's defaults in place.
    """
    global _budget_logged
    try:
        budget = resources.duckdb_budget()
        if not budget:
            return
        mem = budget.get("memory_limit")
        if mem:
            con.execute(f"SET memory_limit='{mem}'")
        threads = budget.get("threads")
        if isinstance(threads, int) and threads:
            con.execute(f"SET threads={int(threads)}")
        temp_dir = budget.get("temp_directory")
        if temp_dir:
            safe_dir = str(temp_dir).replace("'", "")
            os.makedirs(safe_dir, exist_ok=True)
            con.execute(f"SET temp_directory='{safe_dir}'")
        con.execute("SET preserve_insertion_order=false")
        if not _budget_logged:
            _budget_logged = True
            logger.info(
                "duckdb budget applied: memory_limit=%s threads=%s temp_directory=%s "
                "(container limit: %s bytes / %s cpus)",
                mem,
                threads,
                temp_dir,
                budget.get("container_memory_bytes"),
                budget.get("container_cpu_count"),
            )
    except Exception:
        logger.debug("DuckDB memory budget not applied", exc_info=True)


def _safe_table_name(table: str) -> str:
    """Reject table names that try to escape their source root.

    Guards the engine's resolve-fallback (which builds ``<schema>/<name>``
    from raw user input): no absolute paths, no ``..`` traversal segments,
    no NUL bytes. Legitimate names are bare identifiers or ``schema/name``.
    """
    if not table or "\x00" in table or table.startswith(("/", "\\")):
        raise LakehouseError(f"Invalid table name: {table!r}")
    if any(part == ".." for part in table.replace("\\", "/").split("/")):
        raise LakehouseError(f"Invalid table name (path traversal): {table!r}")
    return table


def _safe_ident(name: str) -> str:
    """Quote an identifier for DuckDB if it contains special characters."""
    if name.replace("_", "").isalnum():
        return name
    return '"' + name.replace('"', '""') + '"'


def _normalize_cache_sql(sql: str) -> str:
    """Whitespace-normalize SQL for result-cache keys (quote-aware, fail-open).

    Collapses whitespace runs OUTSIDE quoted regions to single spaces and
    trims both ends, so formatting-only variants of a query share one cache
    entry. Quoted content — string literals AND double-quoted identifiers —
    is preserved byte-for-byte, because ``'a  b'`` and ``'a b'`` can return
    different results and ``"my col"`` is a different identifier from
    ``"mycol"``. Trailing statement terminators (``;``) are stripped.

    The tiny scanner BAILS OUT (returns the raw text) on anything it cannot
    reason about safely: ``--``/``/*`` comments (an apostrophe inside a
    comment would corrupt the quote state) and ``$`` (dollar-quoted strings).
    Bailing reproduces today's exact key, so normalization can only ever ADD
    cache hits — never serve a result a distinct query would not have gotten.
    """
    try:
        out: list[str] = []
        i, n = 0, len(sql)
        in_quote: str | None = None
        pending_ws = False
        while i < n:
            ch = sql[i]
            if in_quote is not None:
                out.append(ch)
                if ch == in_quote:
                    if i + 1 < n and sql[i + 1] == in_quote:
                        out.append(sql[i + 1])  # doubled quote stays inside
                        i += 1
                    else:
                        in_quote = None
                i += 1
                continue
            if ch in ("'", '"'):
                in_quote = ch
                pending_ws = False
                out.append(ch)
                i += 1
                continue
            if ch == "$":
                return sql  # dollar-quoted strings — beyond this scanner
            if (ch == "-" and sql[i : i + 2] == "--") or (ch == "/" and sql[i : i + 2] == "/*"):
                return sql  # comments — beyond this scanner
            if ch.isspace():
                if not pending_ws:
                    out.append(" ")
                    pending_ws = True
                i += 1
                continue
            pending_ws = False
            out.append(ch)
            i += 1
        if in_quote is not None:
            return sql  # unterminated quote — sqlguard refuses these anyway
        norm = "".join(out).strip()
        while norm.endswith(";"):
            norm = norm[:-1].rstrip()
        return norm or sql
    except Exception:
        return sql


def _validate_params(params: object) -> object | None:
    """Validate user-supplied query parameters (named dict or positional list).

    Only scalar parameter values are accepted (str, int, float, bool, bytes,
    datetime/date/time, Decimal, None) — nested containers are refused, since
    list/struct parameters need matching SQL types and would otherwise fail
    deep inside DuckDB with a confusing error. Raises ValueError (a client
    input error, mapped to HTTP 400 by the web API).
    """
    import datetime
    from decimal import Decimal

    if params is None:
        return None
    if isinstance(params, dict):
        if not all(isinstance(k, str) for k in params):
            raise ValueError("Query params: named parameters need string keys.")
        values = list(params.values())
    elif isinstance(params, (list, tuple)):
        values = list(params)
    else:
        # ValueError, not TypeError: this is CLIENT INPUT validation, mapped
        # to HTTP 400 by the web API (a TypeError would surface as a 500).
        raise ValueError(  # noqa: TRY004
            "Query params must be an object ({$name: value}) or an array (positional ?)."
        )
    scalars = (
        str,
        int,
        float,
        bool,
        bytes,
        datetime.datetime,
        datetime.date,
        datetime.time,
        Decimal,
    )
    for v in values:
        if v is not None and not isinstance(v, scalars):
            raise ValueError(
                f"Query params must be scalars (str/int/float/bool/datetime/Decimal/None); got {type(v).__name__}."
            )
    return params


# ---------------------------------------------------------------------------
# column_stats (additive, Wave 5): per-column statistics over a bounded sample
# ---------------------------------------------------------------------------

# Types quantile_cont accepts (everything else → quantiles reported as null).
_QUANTILE_TYPE_RE = re.compile(
    r"\b(tinyint|smallint|integer|bigint|hugeint|utinyint|usmallint|uinteger|ubigint|"
    r"u?int\d*|float\d*|double|real|decimal|numeric|timestamp|timestamptz|date|time|interval)\b",
    re.IGNORECASE,
)


def _validate_column(info: dict, column: str, table: str) -> tuple[str, str]:
    """Resolve ONE column against a describe result (case-insensitive).

    Returns the table's actual (case-correct) column name and its type; a
    missing column raises a LakehouseError that names the available columns
    so an agent self-corrects in one round-trip.
    """
    wanted = str(column).strip()
    if not wanted:
        raise LakehouseError("Provide the column to profile (column_stats(table, column)).")
    for c in info.get("columns", []):
        if str(c.get("name", "")).lower() == wanted.lower():
            return str(c["name"]), str(c.get("type", ""))
    available = ", ".join(str(c.get("name", "")) for c in info.get("columns", [])[:15])
    raise LakehouseError(f"Column {column!r} does not exist on table {table!r}. Available columns: {available}")


def _column_stats_queries(con, target: str, col: str, cap: int, top_n: int, col_type: str) -> dict:
    """Run the bounded-sample stats queries for one column on one connection.

    ``target`` is a DuckDB-quoted relation name (registered view, virtual
    target, or an attached qualified name); ``col`` is a quoted identifier
    resolved against the real schema. Every query reads at most ``cap`` rows
    (``_profile_max_rows``) — the same sampling posture as ``profile_table``,
    never a full-table scan beyond the existing profile cap. When ``cap`` is
    0 (the profile cap disabled) the sample is the whole column, exactly like
    profile_table's SUMMARIZE.
    """
    col_ident = _safe_ident(col)
    inner = f"SELECT {col_ident} FROM {target}"
    if cap > 0:
        inner = f"SELECT * FROM ({inner}) LIMIT {int(cap)}"

    row = con.sql(
        f"SELECT count(*) AS sampled_rows, count({col_ident}) AS non_null, "
        f"min({col_ident}) AS min, max({col_ident}) AS max, "
        f"approx_count_distinct({col_ident}) AS approx_unique, "
        f"count(DISTINCT {col_ident}) AS distinct_count "
        f"FROM ({inner})"
    ).fetchone()
    sampled_rows = int(row[0]) if row and row[0] is not None else 0
    non_null = int(row[1]) if row and row[1] is not None else 0
    null_count = max(sampled_rows - non_null, 0)
    null_pct = round(null_count * 100.0 / sampled_rows, 1) if sampled_rows else 0.0

    top_values: list[dict] = []
    if non_null:
        top_rows = con.sql(
            f"SELECT {col_ident} AS value, count(*) AS n FROM ({inner}) "
            f"WHERE {col_ident} IS NOT NULL GROUP BY 1 ORDER BY n DESC, value ASC LIMIT {int(max(min(top_n, 20), 1))}"
        ).fetchall()
        top_values = [{"value": r[0], "count": int(r[1])} for r in top_rows]

    quantiles: dict[str, float | None] = {"q25": None, "q50": None, "q75": None}
    if non_null and _QUANTILE_TYPE_RE.search(col_type or ""):
        try:
            qrow = con.sql(
                f"SELECT quantile_cont({col_ident}, 0.25), quantile_cont({col_ident}, 0.5), "
                f"quantile_cont({col_ident}, 0.75) FROM ({inner}) WHERE {col_ident} IS NOT NULL"
            ).fetchone()
            if qrow:
                quantiles = {"q25": qrow[0], "q50": qrow[1], "q75": qrow[2]}
        except Exception:
            pass  # exotic orderable type DuckDB's quantile_cont rejects — report nulls

    return {
        "sampled_rows": sampled_rows,
        "distinct_count": int(row[5]) if row and row[5] is not None else None,
        "approx_unique": row[4],
        "null_count": null_count,
        "null_pct": null_pct,
        "min": row[2],
        "max": row[3],
        **quantiles,
        "top_values": top_values,
    }


# ---------------------------------------------------------------------------
# "did you mean" error hints
# ---------------------------------------------------------------------------

_TABLE_ERROR = re.compile(r"[Tt]able with name [\"']?([\w/]+)[\"']? does not exist")
_COLUMN_ERROR = re.compile(r'[Cc]olumn "?([\w ]+)"? (?:not found|does not exist)')


def _with_hints(engine: SqlEngine, sql: str, exc: Exception) -> Exception:
    """Attach nearest-name suggestions to table/column resolution errors.

    LLM agents self-correct in one round-trip when the error says what WAS
    available ("did you mean: amount, order_type") instead of three
    blind retries. Best-effort: any failure here returns the original
    exception untouched.
    """
    try:
        import difflib

        msg = str(exc)

        def _suggest(name: str, candidates: list[str]) -> list[str]:
            pool = sorted(set(candidates))
            close = difflib.get_close_matches(name, pool, n=3, cutoff=0.35)
            partial = [c for c in pool if c not in close and name.lower() in c.lower()]
            return (close + partial)[:3]

        m = _TABLE_ERROR.search(msg)
        if m:
            names = [i.qualified_name for i in engine.list_tables()]
            hints = _suggest(m.group(1), names)
            if hints:
                exc = LakehouseError(f"{msg}\nDid you mean one of: {', '.join(hints)}?")
            return _attach_structured_tail(exc)
        m = _COLUMN_ERROR.search(msg)
        if m:
            # DuckDB >= 1.x already prints "Candidate bindings: ..." for
            # unknown columns — don't duplicate its suggestions.
            if "Candidate bindings" in msg:
                return _attach_structured_tail(exc)
            columns: list[str] = []
            for info in engine._referenced_tables(sql):
                try:
                    dset = engine._open_dataset(info)
                    columns.extend(f.name for f in dset.schema)
                except Exception:
                    continue
            hints = _suggest(m.group(1), columns)
            if hints:
                exc = LakehouseError(f"{msg}\nDid you mean one of: {', '.join(hints)}?")
            return _attach_structured_tail(exc)
    except Exception:
        return exc
    return _attach_structured_tail(exc)


def _attach_structured_tail(exc: Exception) -> Exception:
    """Append the stable code/fix_hints JSON tail to a resolution error's text.

    Table/column errors are the two classes agents actually self-correct on,
    so this wraps the `_with_hints` result (the human "Did you mean" line
    stays primary; the machine tail is additive). Best-effort: any failure
    returns the exception untouched.
    """
    try:
        tail = _errors.enrich(str(exc))
        if tail != str(exc):
            return LakehouseError(tail)
    except Exception:
        pass
    return exc


class SqlEngine:
    """Runs SQL + columnar scans over a DataProvider, with in-process caching."""

    def __init__(
        self,
        provider: DataProvider,
        cache_ttl: int = 3600,
        dataset_cache_ttl: int = 3600,
        dataset_cache_tables: int = 8,
        version_check_interval: int = 10,
        list_async_refresh: bool = True,
        cache_dir: str | None = None,
    ):
        self.provider = provider
        self.cache_ttl = cache_ttl
        self.dataset_cache_ttl = dataset_cache_ttl
        self.dataset_cache_tables = dataset_cache_tables
        self.version_check_interval = version_check_interval
        self._tables: list[TableInfo] | None = None
        self._tables_ts: float = 0.0
        # list_tables is served from the cache immediately (never blocks the
        # caller on a slow S3/DFS listing): a stale list is refreshed on a
        # background thread, and a daemon timer re-lists every cache_ttl so
        # the cache stays warm between calls. Disabled when cache_ttl == 0
        # (caching off) or when list_async_refresh is False.
        self._async_list = bool(list_async_refresh) and cache_ttl > 0
        self._list_refreshing = False
        self._describe_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._describe_hits = 0
        self._describe_misses = 0
        # Profile results (column statistics) — same TTL discipline as
        # describe, but keyed additionally by the requested column subset:
        # (source, path, columns) -> (ts, profile dict).
        self._profile_cache: dict[tuple, tuple[float, dict]] = {}
        self._profile_hits = 0
        self._profile_misses = 0
        # Reused open Datasets (metadata handles, NOT row data):
        # path -> (ts, dset, version), LRU-bounded.
        self._dataset_cache: OrderedDict[tuple[str, str, int | None], tuple[float, object, object | None]] = (
            OrderedDict()
        )
        self._dataset_hits = 0
        self._dataset_misses = 0
        # Delta snapshot-version checks are throttled to this many seconds
        # per table (0 = check on every reuse).
        self._version_checked_at: dict[tuple[str, str, int | None], float] = {}
        self._lock = threading.RLock()
        # Semantic catalog (SQLHANDLER_CATALOG): an optional JSON **or YAML**
        # file of human-written table/column descriptions merged into
        # describe / list output so agents see business meaning, not just
        # dtypes. Hot-reloaded on mtime change; a missing/broken file degrades
        # to an empty catalog (never an error).
        self._catalog_path = os.environ.get("SQLHANDLER_CATALOG", "").strip() or None
        self._catalog_file: str | None = None
        self._catalog_mtime: float | None = None
        self._catalog_data: dict = {}
        # Virtual-table definitions (catalog entries with a `definition`):
        # validation verdicts memoized per distinct definition text, so a
        # hot-reloaded catalog re-parses only the definitions it changed.
        self._definition_verdicts: dict[str, str | None] = {}
        # and the rewritten (Snowflake -> DuckDB) SQL, memoized the same way.
        self._definition_rewrites: dict[str, str] = {}
        # Policy row filters already binder-validated against a real table
        # (policy-as-code): filter text -> True. Load-time validation covers
        # tables with known columns; this memoizes first-use validation for
        # the rest (a hot-reloaded policy resets the memo by TEXT change —
        # a changed filter is a new key).
        self._validated_filters: dict[str, bool] = {}
        # Virtual-table materialization cache: an unfiltered query against a
        # virtual table must run its whole definition (blocking aggregates
        # defeat LIMIT), which for big base tables is a multi-second payment
        # on EVERY query. So the definition's full result is written to a
        # parquet file once and reused (registered like any physical table —
        # same pushdown path) until the definition, any base table's snapshot
        # version, or the TTL changes. SQLHANDLER_VIRTUAL_CACHE_TTL=0
        # disables it; SQLHANDLER_VIRTUAL_CACHE_DIR overrides the location
        # (default: the disk-warm cache dir, resolved below). Point it at an
        # RWX PVC shared by all replicas to pay the materialization once per
        # deployment instead of once per pod.
        raw = os.environ.get("SQLHANDLER_VIRTUAL_CACHE_TTL", "")
        try:
            self._virtual_cache_ttl = max(int(raw), 0) if raw else 3600
        except ValueError:
            self._virtual_cache_ttl = 3600
        raw = os.environ.get("SQLHANDLER_VIRTUAL_CACHE_MAX_BYTES", "")
        try:
            # Skip caching results larger than this (serve them live) — a
            # runaway definition must not fill the disk. 0 = unlimited.
            self._virtual_cache_max_bytes = max(int(raw), 0) if raw else 2 * 1024**3
        except ValueError:
            self._virtual_cache_max_bytes = 2 * 1024**3
        self._virtual_cache_hits = 0
        self._virtual_cache_writes = 0
        # Cluster (sort) materialized virtual results by their lowest-
        # cardinality columns so row-group stats prune filtered reads.
        self._virtual_cache_sort = os.environ.get("SQLHANDLER_VIRTUAL_CACHE_SORT", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        # Query result cache (Snowflake's result-cache analog): repeated
        # IDENTICAL queries — agent retries, loops, multi-agent sessions —
        # are served from memory instead of re-running. Keyed by the full
        # query identity (sql, params, limits, time travel) plus every
        # referenced table's snapshot-version token, so a new ETL commit
        # invalidates immediately. Virtual-table queries are excluded (their
        # materialization cache already covers them); attached-database
        # queries are excluded (their catalogs version differently).
        raw = os.environ.get("SQLHANDLER_RESULT_CACHE_TTL", "")
        try:
            self._result_cache_ttl = max(int(raw), 0) if raw else 3600
        except ValueError:
            self._result_cache_ttl = 3600
        raw = os.environ.get("SQLHANDLER_RESULT_CACHE_MAX_BYTES", "")
        try:
            self._result_cache_max_bytes = max(int(raw), 0) if raw else 256 * 1024**2
        except ValueError:
            self._result_cache_max_bytes = 256 * 1024**2
        self._result_cache: OrderedDict[str, tuple[float, pa.Table]] = OrderedDict()
        self._result_cache_bytes = 0
        self._result_cache_hits = 0
        self._result_cache_writes = 0
        # Write-tier side map: cache key -> the SQL that produced it (the
        # stored key is a sha256 hex, so a post-write eviction cannot
        # path-match stored keys without this). Bounded WITH the cache:
        # entries are dropped wherever cache entries are.
        self._result_cache_sql: OrderedDict[str, str] = OrderedDict()
        # Shared L2 result cache (sqlhandler/l2cache.py): the same key-space
        # published to a directory every replica can read (k8s: an RWX PVC),
        # so a warm result computed by one replica serves all of them. Purely
        # additive — self._l2_cache is None (everything byte-identical to the
        # memory-only behavior) unless SQLHANDLER_L2_DIR is set AND
        # SQLHANDLER_L2_ENABLED is not 0. Virtual-table and attach-DB queries
        # stay excluded automatically: _result_cache_key already returns None
        # for both, and that must not "fixed".
        l2_cfg = load_l2_config()
        self._l2_cache: L2ResultCache | None = L2ResultCache(l2_cfg["dir"], ttl=l2_cfg["ttl"]) if l2_cfg else None
        self._l2_min_bytes = l2_cfg["min_bytes"] if l2_cfg else 0
        self._l2_max_bytes = l2_cfg["max_bytes"] if l2_cfg else 0
        # External read-only database attaches (SQLHANDLER_ATTACH[_FILE]):
        # config parses loudly at startup (operator-authored, security
        # relevant — a typo should kill the pod, not silently skip a source).
        # Connections attach on demand per query; see sqlhandler/external.py.
        self.attaches: list[AttachSpec] = parse_attach_config()
        self._attached_listing: tuple[float, list[dict]] | None = None
        # Write tier (review §4): the frozen set of source-table locations,
        # snapshotted lazily at the FIRST write-target resolution (before
        # this process has written anything). A scratch write may never
        # resolve onto a covered source table; anything written later is
        # not in this set and stays overwritable by its owner.
        self._write_tier_source_paths: set[str] | None = None
        # Query memory: recent query outcomes for the query-memory MCP
        # resource (self-improving loop — agents reuse proven patterns).
        self._query_memory: deque = deque(maxlen=_query_memory_size())
        # Per-table usage counts (source, path) -> accesses through
        # _open_dataset. Powers usage-driven prewarm: when no explicit
        # SQLHANDLER_PREWARM_TABLES is configured, the busiest tables from
        # the previous run (persisted in the disk-warm cache) are warmed
        # instead — the server teaches itself what to pre-warm.
        self._table_usage: dict[tuple[str, str], int] = {}
        # Opens since the last disk-cache write; note_query flushes the
        # usage counts to disk every _USAGE_SAVE_EVERY opens so a restart
        # prewarms from fresh popularity without IO per query.
        self._unsaved_opens = 0
        # Disk-warm layer (SQLHANDLER_CACHE_DIR, k8s: mount an emptyDir there):
        # describe/table-list results are persisted after each fill and
        # reloaded at startup, so a container restart (OOMKill, node drain)
        # no longer costs a full cold metadata fetch. The engine's own caches
        # stay in-process and TTL-driven; disk entries are re-validated
        # against the wall-clock TTL at load, and never outlive the same
        # cache_ttl the memory layer uses.
        env_cache_dir = os.environ.get("SQLHANDLER_CACHE_DIR", "").strip()
        self._cache_dir = cache_dir or (env_cache_dir or None)
        # Resolved here because it defaults to the disk-warm cache dir above.
        self._virtual_cache_dir = (
            os.environ.get("SQLHANDLER_VIRTUAL_CACHE_DIR", "").strip()
            or self._cache_dir
            or str(Path(tempfile.gettempdir()) / "sqlhandler-virtual-cache")
        )
        if self._cache_dir and self.cache_ttl > 0:
            self._load_cache_from_disk()
        # Uploadable catalog store (POST /api/semantic-catalog): a WRITABLE
        # catalog file that OVERRIDES the operator's SQLHANDLER_CATALOG file
        # while it exists — the most recent intentional action wins. Defaults
        # next to the disk cache, which is writable in every supported
        # deployment (the chart's hardened profile backs /tmp with an
        # emptyDir); point SQLHANDLER_CATALOG_STORE at a PVC path to make
        # uploads survive pod rescheduling. SQLHANDLER_CATALOG_UPLOAD=0
        # disables the upload/clear API (read-only catalog posture).
        self.catalog_uploads_enabled = os.environ.get("SQLHANDLER_CATALOG_UPLOAD", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        self._catalog_store = os.environ.get("SQLHANDLER_CATALOG_STORE", "").strip() or (
            str(Path(self._cache_dir) / "semantic-catalog.json")
            if self._cache_dir
            else str(Path(tempfile.gettempdir()) / "sqlhandler-semantic-catalog.json")
        )
        if self._async_list:
            threading.Thread(
                target=self._auto_refresh_loop,
                daemon=True,
                name="sqlhandler-list-autorefresh",
            ).start()
        # One daemon sweep of the shared L2 dir (expired sidecars; lazy
        # deletion on lookup is the guaranteed path — this is the backstop
        # for keys this replica never looks up again). The virtual
        # materialization cache deliberately gets NO such sweeper in this
        # slice: adding GC there is a separate decision, not L2 scope.
        if self._l2_cache is not None:
            self._l2_cache.start_sweeper()

    # ---------------------------------------------------------------- list
    def list_tables(self, *, caller=None) -> list[TableInfo]:
        """Every addressable table: the provider's physical tables plus the
        semantic catalog's virtual tables (entries carrying a ``definition``).

        Virtual tables are appended after the physical ones (sorted by name)
        and carry ``format="virtual"`` — they are computed at query time from
        their definitions and are backed by no storage at all (see
        ``_register_schema``). The disk-warm cache and the provider caches
        below stay physical-only: virtual tables always derive live from the
        hot-reloaded catalog.

        Identity spine: policy-HIDDEN tables are OMITTED for a caller whose
        groups hide them (invisible — list/search/describe/profile/scan all
        refuse or omit consistently). Enforcement off (or no groups) returns
        the full list byte-identically. The underlying provider cache is
        UNCHANGED — filtering is per-call on top of the shared cached list.
        """
        tables = self._provider_tables()
        tables = tables + self._virtual_infos(tables)
        if not policy_mod.policy_enabled():
            return tables
        pol = policy_store().get()
        if not pol.groups:
            return tables
        effective_caller = caller if caller is not None else current_caller()
        if effective_caller is None:
            # NO caller context (engine internals, tests, pre-middleware):
            # the unfiltered list. A real HTTP request ALWAYS carries a
            # Caller (anonymous at worst) from the identity middleware, so
            # this branch is internal-only and never a policy bypass.
            return tables
        groups = pol.groups_for(getattr(effective_caller, "subject", None), getattr(effective_caller, "key_fp", None))
        if not groups:
            return tables
        return [t for t in tables if not pol.rule_for_table(t.path, t.name, groups).hidden]

    def _provider_tables(self) -> list[TableInfo]:
        """List the tables the provider exposes (cached for cache_ttl).

        With async refresh (the default) the cached list is returned
        immediately - even a stale one - and the list is re-fetched on a
        background thread so callers never block on the slow S3/DFS listing.
        A daemon timer also refreshes the list every ``cache_ttl`` seconds, so
        it is kept fresh automatically between calls. When caching is disabled
        (``cache_ttl == 0``) or ``list_async_refresh`` is off, every call
        re-lists synchronously, preserving the original behavior.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._tables
            fresh = cached is not None and (now - self._tables_ts < self.cache_ttl)
        if cached is not None and (fresh or self._async_list):
            if not fresh:  # async_list must be enabled here
                self._maybe_refresh_async()  # serve stale, refresh in background
            return cached
        # No cache yet, or caching disabled: fill synchronously so callers
        # always get a current result.
        tables = self.provider.list_tables()
        with self._lock:
            self._tables = tables
            self._tables_ts = time.monotonic()
            self._list_refreshing = False
        self._save_cache_to_disk()
        return tables

    def _maybe_refresh_async(self) -> None:
        """Start a background list refresh (no-op when one is in flight)."""
        with self._lock:
            if self._list_refreshing:
                return
            self._list_refreshing = True
        try:
            threading.Thread(
                target=self._refresh_list_worker,
                daemon=True,
                name="sqlhandler-list-refresh",
            ).start()
        except Exception:
            with self._lock:
                self._list_refreshing = False
            raise

    def _refresh_list_worker(self) -> None:
        """Re-run provider.list_tables(); fills the cache, never raises."""
        try:
            tables = self.provider.list_tables()
        except Exception:
            logger.exception("background list-tables refresh failed")
            with self._lock:
                self._list_refreshing = False
            return
        with self._lock:
            self._tables = tables
            self._tables_ts = time.monotonic()
            self._list_refreshing = False
        self._save_cache_to_disk()

    def _auto_refresh_loop(self) -> None:
        """Keep the table list warm: refresh in the background every TTL."""
        while True:
            time.sleep(max(self.cache_ttl, 1))
            try:
                self._maybe_refresh_async()
            except Exception:
                logger.debug("auto list refresh skipped", exc_info=True)

    # ------------------------------------------------------- disk warm layer
    def _cache_file(self) -> Path:
        assert self._cache_dir is not None  # both callers run only when a cache dir is configured
        return Path(self._cache_dir) / "metadata-cache.json"

    def _load_cache_from_disk(self) -> None:
        """Reload describe/table-list results persisted by a previous run.

        Every entry carries a wall-clock epoch and is only accepted when
        younger than ``cache_ttl`` — the same lifetime the memory layer uses,
        so a warm entry is never *more* stale than an in-process one. Loaded
        entries get fresh monotonic timestamps (the in-process TTL restarts),
        and each step is best-effort: a missing, corrupt or unwritable file
        just means a cold start, exactly as before.
        """
        try:
            path = self._cache_file()
            if not path.exists():
                return
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            now = time.time()
            tables = data.get("tables")
            if isinstance(tables, dict) and now - float(tables.get("ts", 0)) < self.cache_ttl:
                items = tables.get("items")
                if isinstance(items, list) and items:
                    infos = [TableInfo(**item) for item in items if isinstance(item, dict)]
                    if infos:
                        self._tables = infos
                        self._tables_ts = time.monotonic()
                        logger.info("warm-started table list from %s (%d tables)", path, len(infos))
            describes = data.get("describes")
            if isinstance(describes, list):
                loaded = 0
                for entry in describes:
                    if not isinstance(entry, dict):
                        continue
                    if now - float(entry.get("ts", 0)) >= self.cache_ttl:
                        continue
                    key = (str(entry.get("source", "default")), str(entry.get("path", "")))
                    result = entry.get("result")
                    if isinstance(result, dict):
                        self._describe_cache[key] = (time.monotonic(), result)
                        loaded += 1
                if loaded:
                    logger.info("warm-started %d describe result(s) from %s", loaded, path)
            # Usage counts restore without a TTL (they are a popularity
            # prior for the next prewarm, not a freshness-sensitive value).
            usage = data.get("usage")
            if isinstance(usage, list):
                restored = 0
                for row in usage:
                    if isinstance(row, list) and len(row) == 3:
                        try:
                            key = (str(row[0]), str(row[1]))
                            self._table_usage[key] = max(self._table_usage.get(key, 0), int(row[2]))
                            restored += 1
                        except (TypeError, ValueError):
                            continue
                if restored:
                    logger.info("warm-started usage counts for %d table(s)", restored)
        except Exception:
            logger.debug("disk cache warm-start skipped", exc_info=True)

    def _save_cache_to_disk(self) -> None:
        """Persist the current caches for the next process start (best-effort).

        Called right after a synchronous cache fill, so the wall-clock epoch
        written here is the fill time; entries already on disk get their
        epoch refreshed, which at most extends one entry's disk lifetime by a
        single TTL — the memory layer's own TTL still governs correctness.
        """
        if not self._cache_dir or self.cache_ttl <= 0:
            return
        try:
            import json

            with self._lock:
                payload = {
                    "tables": {
                        "ts": time.time(),
                        "items": [asdict(info) for info in self._tables or []],
                    },
                    "describes": [
                        {
                            "source": source,
                            "path": path,
                            "ts": time.time(),
                            "result": result,
                        }
                        for (source, path), (_, result) in self._describe_cache.items()
                    ],
                    "usage": [[source, path, count] for (source, path), count in self._table_usage.items()],
                }
            cache_dir = Path(self._cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._cache_file())
        except Exception:
            logger.debug("disk cache save skipped", exc_info=True)

    # ------------------------------------------------------------- catalog
    @staticmethod
    def _parse_catalog_text(text: str, origin: str) -> dict:
        """Parse catalog text as JSON, falling back to YAML (pyyaml).

        Both formats carry the same shape: an object whose ``tables`` key
        maps table keys to their documentation. JSON is tried first (it is
        the documented format), YAML second so hand-written catalogs can use
        the friendlier syntax. Raises ``ValueError`` with a precise message
        on invalid content — callers decide whether that means "reject the
        upload" (API) or "ignore the file" (engine load).
        """
        try:
            data = json.loads(text)
        except Exception:
            try:
                import yaml  # optional dependency (pyproject: pyyaml)
            except ImportError:
                raise ValueError(
                    f"{origin}: not valid JSON, and PyYAML is not installed (YAML catalogs need the 'pyyaml' package)"
                ) from None
            try:
                data = yaml.safe_load(text)
            except Exception as exc:
                raise ValueError(f"{origin}: not valid JSON or YAML: {exc}") from exc
        if not isinstance(data, dict):
            # ValueError on purpose: wrong-typed catalog content is user
            # input to reject with HTTP 400, not an internal type bug.
            raise ValueError(f"{origin}: catalog must be an object with a 'tables' mapping")  # noqa: TRY004
        return data

    def _effective_catalog_file(self) -> str | None:
        """The catalog file the engine currently serves.

        An uploaded catalog (the writable store) overrides the operator's
        SQLHANDLER_CATALOG file for as long as it exists; deleting it (DELETE
        /api/semantic-catalog) falls back to the configured file.
        """
        if self._catalog_store:
            try:
                if os.path.exists(self._catalog_store):
                    return self._catalog_store
            except OSError:
                pass
        return self._catalog_path

    def _catalog(self) -> dict:
        """Return the semantic catalog's ``tables`` mapping (hot-reloaded).

        The active file (uploaded store, else SQLHANDLER_CATALOG) is re-read
        whenever its path or mtime changes — or whenever it disappears — so
        editing, uploading, or clearing the catalog takes effect without a
        restart. Any problem reading/parsing it logs a warning and yields an
        empty catalog — a broken catalog must never break queries.
        """
        path = self._effective_catalog_file()
        try:
            mtime = os.stat(path).st_mtime if path else None
        except OSError:
            return {}
        if (path, mtime) != (self._catalog_file, self._catalog_mtime):
            if path:
                try:
                    self._catalog_data = self._load_catalog_file(path)
                except Exception:
                    logger.warning("semantic catalog %s unreadable; ignoring it", path, exc_info=True)
                    self._catalog_data = {}
            else:
                # The catalog went away entirely (upload cleared, env file
                # removed) — the empty mapping is the new truth.
                self._catalog_data = {}
            self._catalog_file = path
            self._catalog_mtime = mtime
            # Catalog documentation is merged INTO cached describe results,
            # so a catalog change must invalidate them or the old wording
            # would keep being served until the TTL expires.
            with self._lock:
                self._describe_cache.clear()
        return self._catalog_data

    def _load_catalog_file(self, path: str) -> dict:
        """Read + parse one catalog file; returns its ``tables`` mapping."""
        data = self._parse_catalog_text(Path(path).read_text(encoding="utf-8"), path)
        tables = data.get("tables")
        result = tables if isinstance(tables, dict) else {}
        logger.info(
            "semantic catalog loaded: %d table entr%s from %s",
            len(result),
            "y" if len(result) == 1 else "ies",
            path,
        )
        return result

    def _write_catalog_store(self, tables: dict) -> str:
        """Atomically write ``tables`` to the upload store as canonical JSON.

        The store file is always written as canonical JSON (the parsed
        content re-serialized) so the file on disk stays trivially
        machine-readable regardless of the format the user edited in.
        The temp file is created exclusively per write (``mkstemp``) — on a
        SHARED store (RWX PVC, replicas > 1) a fixed ``.tmp`` name would let
        two replicas applying at once interleave into one temp file and
        publish corrupt JSON. Publication itself stays atomic ``os.replace``.
        Raises ``OSError`` on an unwritable store (propagates to the API
        layer, which turns it into an operator-actionable 500).
        """
        target = Path(self._catalog_store)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"tables": tables}, indent=2, ensure_ascii=False))
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)  # never leave a stray temp on failure
            raise
        # Hot-reload happens on the next _catalog() call (mtime changed), but
        # resolve it eagerly so a mutation response reflects reality.
        self._catalog()
        return str(target)

    def set_catalog_text(self, text: str) -> dict:
        """Validate + atomically store an uploaded catalog (JSON or YAML).

        Raises ``ValueError`` on invalid content; ``OSError`` on an
        unwritable store propagates to the API layer.
        """
        data = self._parse_catalog_text(text, "<upload>")
        tables = data.get("tables")
        if not isinstance(tables, dict):
            # ValueError on purpose: user input -> HTTP 400 (see _parse_catalog_text).
            raise ValueError("catalog must contain a top-level 'tables' mapping")  # noqa: TRY004
        for name, entry in tables.items():
            if not isinstance(entry, dict):
                raise ValueError(f"tables[{name!r}] must be a mapping of documentation fields")  # noqa: TRY004
        target = self._write_catalog_store(tables)
        logger.info("semantic catalog uploaded: %d tables -> %s", len(tables), target)
        return {"tables": len(tables), "path": target}

    def clear_catalog(self) -> bool:
        """Remove the uploaded catalog; the configured file takes over again."""
        try:
            existed = os.path.exists(self._catalog_store)
        except OSError:
            return False
        if existed:
            os.remove(self._catalog_store)
            logger.info("semantic catalog upload removed: %s", self._catalog_store)
            self._catalog()  # eager reload back to the configured file
        return existed

    def catalog_status(self) -> dict:
        """Current semantic-catalog state for the API/UI.

        Reports both sources (the configured file and the upload store) with
        their parsed table counts where readable, so the UI can show exactly
        which documentation is live and where it came from.
        """

        def _peek(path: str | None) -> dict | None:
            if not path:
                return None
            info: dict = {"path": path}
            try:
                info["exists"] = os.path.exists(path)
            except OSError:
                info["exists"] = False
            if info["exists"]:
                try:
                    info["tables"] = len(self._load_catalog_file(path))
                except Exception as exc:
                    info["tables"] = None
                    info["error"] = str(exc)
            return info

        active = self._effective_catalog_file()
        return {
            "uploads_enabled": self.catalog_uploads_enabled,
            "store_path": self._catalog_store,
            "configured": _peek(self._catalog_path),
            "uploaded": _peek(self._catalog_store),
            "active_source": ("upload" if active == self._catalog_store else "configured") if active else None,
            "active_path": active,
            "active_tables": len(self._catalog()) if active else 0,
        }

    # ---- catalog editing (global + per-table, behind the web UI) ----------
    # The editor loads the ACTIVE catalog as text, the user edits it, and
    # applying writes through the same upload store a global upload uses —
    # so every edit keeps the "most recent intentional action wins"
    # precedence, the canonical-JSON on-disk format, and hot reload.

    _CATALOG_STARTER_YAML = (
        "# Semantic catalog — human documentation merged into the MCP and UI\n"
        "# table/column listings. One entry per table, keyed by its path\n"
        "# (schema/name), source-qualified name, or bare table name.\n"
        "tables:\n"
        "  workorder/work_order:\n"
        "    description: Maintenance work order headers, one row per order\n"
        "    aliases: [work orders]\n"
        "    columns:\n"
        "      amount: Order total in USD\n"
        '      kind: "Order class: a=planned, b=unplanned"\n'
    )
    _CATALOG_STARTER_JSON = (
        "{\n"
        '  "tables": {\n'
        '    "workorder/work_order": {\n'
        '      "description": "Maintenance work order headers, one row per order",\n'
        '      "aliases": ["work orders"],\n'
        '      "columns": {"amount": "Order total in USD", "kind": "Order class: a=planned, b=unplanned"}\n'
        "    }\n"
        "  }\n"
        "}\n"
    )

    @staticmethod
    def _serialize_doc(data: dict, fmt: str) -> str:
        """Serialize catalog content as editable YAML (default) or JSON.

        YAML is the user-facing default (friendlier to hand-edit); JSON is
        the one-keystroke swap. If pyyaml is missing, YAML falls back to
        JSON rather than to nothing — the editor must always show text the
        server can parse back.
        """
        if fmt != "yaml":
            return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        try:
            import yaml  # optional dependency (pyproject: pyyaml)
        except ImportError:
            return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

    def catalog_content(self, fmt: str = "yaml") -> dict:
        """The ACTIVE catalog serialized as editable text (YAML by default).

        Serves exactly the mapping ``_catalog()`` merges into describe/list
        output — the uploaded store first, else the configured
        SQLHANDLER_CATALOG file — re-serialized in the requested format. With
        no catalog attached (or an unreadable one) a commented starter
        template is returned instead, so "edit" on an empty deployment
        begins from a valid skeleton.
        """
        if fmt not in ("yaml", "json"):
            raise ValueError(f"format must be 'yaml' or 'json', got {fmt!r}")
        tables = self._catalog()
        active = self._effective_catalog_file()
        if active and tables:
            text = self._serialize_doc({"tables": tables}, fmt)
            source = "upload" if active == self._catalog_store else "configured"
        else:
            text = self._CATALOG_STARTER_JSON if fmt == "json" else self._CATALOG_STARTER_YAML
            source = "none"
        return {
            "format": fmt,
            "text": text,
            "tables": len(tables),
            "source": source,
            "path": active,
            "uploads_enabled": self.catalog_uploads_enabled,
        }

    def _catalog_key_for(self, info: TableInfo) -> str | None:
        """The catalog key that currently documents ``info`` (None if absent).

        Same precedence as :meth:`_catalog_for` — an edit must update the
        key that is actually being served, not fork a second entry for the
        same table under a different spelling.
        """
        catalog = self._catalog()
        for key in (info.path, info.qualified_name, info.name):
            entry = catalog.get(key)
            if isinstance(entry, dict):
                return key
        return None

    def _resolve_catalog_target(self, table: str) -> TableInfo:
        """Resolve a table for catalog editing — it must exist in the source.

        Unlike :meth:`_resolve` (which synthesizes a schema/name pair so SQL
        can address not-yet-listed tables), documenting a table the source
        does not expose is a user error — a typo would silently create an
        orphan entry — so it fails loudly instead.
        """
        info = self._resolve(table)
        for t in self.list_tables():
            if info.qualified_name == t.qualified_name or (info.path, info.name) == (
                t.path,
                t.name,
            ):
                return info
        raise LakehouseError(f"Table '{table}' not found in data source")

    def catalog_table_entry(self, table: str, fmt: str = "yaml") -> dict:
        """One table's catalog breakout, serialized for the editor.

        ``table`` resolves exactly like a query (path, source-qualified or
        bare name). When the catalog has no entry for it, ``found`` is False
        and ``text`` is empty — the UI prefills a skeleton from the table's
        real schema so documenting a dataset starts from its columns.
        """
        if fmt not in ("yaml", "json"):
            raise ValueError(f"format must be 'yaml' or 'json', got {fmt!r}")
        info = self._resolve_catalog_target(table)
        key = self._catalog_key_for(info)
        entry = self._catalog().get(key) if key is not None else None
        return {
            "table": info.path,
            "found": entry is not None,
            "key": key if key is not None else info.path,
            "text": self._serialize_doc(entry, fmt) if entry is not None else "",
            "uploads_enabled": self.catalog_uploads_enabled,
        }

    @staticmethod
    def _validate_catalog_entry(entry: object, origin: str) -> dict:
        """Validate one table's documentation entry; returns the cleaned copy.

        ``description`` must be a string, ``aliases`` a list of strings and
        ``columns`` a mapping of column name -> string description. Unknown
        keys are preserved as-is so hand-written extras survive an edit.
        Raises ``ValueError`` (user input -> HTTP 400) on wrong types.
        """
        if not isinstance(entry, dict):
            raise ValueError(f"{origin}: entry must be a mapping of documentation fields")  # noqa: TRY004
        out: dict = {}
        desc = entry.get("description")
        if desc is not None:
            if not isinstance(desc, str):
                raise ValueError(f"{origin}: 'description' must be a string")
            out["description"] = desc
        aliases = entry.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise ValueError(f"{origin}: 'aliases' must be a list of strings")
            out["aliases"] = aliases
        columns = entry.get("columns")
        if columns is not None:
            if not isinstance(columns, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in columns.items()
            ):
                raise ValueError(f"{origin}: 'columns' must map column names to string descriptions")
            out["columns"] = columns
        for k, v in entry.items():
            if k not in out and k not in ("description", "aliases", "columns"):
                out[k] = v
        return out

    # ------------------------------------------------------------- virtual
    # Virtual tables: semantic-catalog entries that carry a ``definition`` —
    # a SELECT/WITH query — instead of pointing at stored data. They appear
    # in list/describe/search like any table (marked ``format="virtual"``)
    # and are constructed on the fly at query time as a DuckDB view over the
    # base tables their definition references, so user filters and
    # projections still push down into the physical scans. Definitions run
    # on exactly the same locked-down, read-only connections as any other
    # query and are validated up front: a single, parseable, read-only
    # statement. The virtual-table surface adds no new capability (the SQL
    # endpoints already execute read-only queries with these guardrails) —
    # it adds *reusable* query shapes that data owners curate.

    _VIRTUAL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def _virtual_entries(self) -> dict[str, dict]:
        """Catalog entries that define a virtual table, keyed by table name.

        An entry qualifies when its key is a clean bare identifier and it
        carries a non-empty string ``definition`` that parses as a single
        read-only statement (validated once per distinct definition text —
        see ``_definition_error``). Anything else carrying a ``definition``
        is skipped with a warning: a broken catalog entry must never break
        queries. ``virtual: true`` WITHOUT a definition is a plain
        documentation marker, not an error.
        """
        out: dict[str, dict] = {}
        for key, entry in self._catalog().items():
            if not isinstance(entry, dict) or not entry.get("definition"):
                continue
            if not isinstance(key, str) or "/" in key or not self._VIRTUAL_NAME_RE.match(key):
                logger.warning(
                    "semantic catalog: virtual-table key %r invalid (must be a bare identifier); skipping",
                    key,
                )
                continue
            definition = entry["definition"]
            if not isinstance(definition, str) or not definition.strip():
                logger.warning("semantic catalog: virtual table %r has an empty definition; skipping", key)
                continue
            error = self._definition_error(definition)
            if error:
                logger.warning("semantic catalog: virtual table %r rejected: %s", key, error)
                continue
            out[key] = entry
        return out

    def _definition_error(self, definition: str) -> str | None:
        """Validate a definition with DuckDB's own parser (memoized by text).

        Must be exactly one statement and a plain read-only SELECT (WITH /
        VALUES parse as SELECT — the same rule the web API's
        ``assert_readonly`` applies). A definition is stored as a view body,
        so it could only ever execute with the guardrails of a regular
        query, but rejecting non-SELECT text up front gives the catalog
        author a clear log line instead of a runtime surprise. Returns None
        when the definition is valid.
        """
        if definition in self._definition_verdicts:
            return self._definition_verdicts[definition]
        if len(self._definition_verdicts) > 128:  # bound the memo (edits churn texts)
            self._definition_verdicts.clear()
        verdict: str | None
        try:
            import duckdb

            types = [str(s.type).split(".")[-1] for s in duckdb.extract_statements(definition)]
        except Exception as exc:
            verdict = f"definition does not parse: {exc}"
        else:
            if len(types) != 1:
                verdict = f"definition must be exactly one statement, found {len(types)}"
            elif types[0] != "SELECT":
                verdict = f"definition must be a single read-only SELECT (WITH ...) statement, not {types[0]}"
            else:
                verdict = None
        self._definition_verdicts[definition] = verdict
        return verdict

    def _virtual_infos(self, physical: list[TableInfo]) -> list[TableInfo]:
        """TableInfo records for the catalog's virtual tables (name-sorted).

        A virtual table whose name collides with a physical table is dropped:
        stored data wins over documentation — a catalog edit must never
        shadow a real table into something else. ``physical`` is the caller's
        already-fetched provider list (never re-listed here).
        """
        taken: set[str] = set()
        for info in physical:
            taken.update((info.name, info.path, info.qualified_name))
        return [
            info
            for info in (
                TableInfo(name=name, schema="default", format="virtual") for name in sorted(self._virtual_entries())
            )
            if info.name not in taken and info.path not in taken and info.qualified_name not in taken
        ]

    def _parse_catalog_entry_text(self, text: str) -> dict:
        """Parse one table's documentation fragment (JSON or YAML).

        Accepts the entry itself (``description``/``aliases``/``columns``);
        a single-entry ``tables:`` wrapper is also unwrapped, so pasting a
        slice of a full catalog still lands on the right table.
        """
        try:
            data = json.loads(text)
        except Exception:
            try:
                import yaml  # optional dependency (pyproject: pyyaml)
            except ImportError:
                raise ValueError(
                    "entry is not valid JSON, and PyYAML is not installed (YAML entries need the 'pyyaml' package)"
                ) from None
            try:
                data = yaml.safe_load(text)
            except Exception as exc:
                raise ValueError(f"catalog entry is not valid JSON or YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("catalog entry must be a mapping (description / aliases / columns)")  # noqa: TRY004
        if set(data) == {"tables"} and isinstance(data["tables"], dict):
            if len(data["tables"]) != 1:
                raise ValueError("a per-table edit takes ONE table's entry — use the global editor for a full catalog")
            (data,) = data["tables"].values()
        return self._validate_catalog_entry(data, "<entry>")

    def catalog_update_table(self, table: str, text: str) -> dict:
        """Upsert ONE table's documentation from an edited fragment.

        The fragment (JSON or YAML) replaces that table's entry inside the
        effective catalog and the merged result is written to the upload
        store — which overrides the operator's configured file, exactly like
        a global upload. The existing entry key is preserved when the table
        is already documented, so an edit never forks a duplicate entry.
        """
        entry = self._parse_catalog_entry_text(text)
        info = self._resolve_catalog_target(table)
        key = self._catalog_key_for(info) or info.path
        merged = dict(self._catalog())
        merged[key] = entry
        target = self._write_catalog_store(merged)
        logger.info("semantic catalog: updated entry %r -> %s", key, target)
        return {"tables": len(merged), "key": key, "path": target}

    def catalog_remove_table(self, table: str) -> dict:
        """Remove ONE table's entry from the effective catalog.

        The remaining entries are written to the upload store. When nothing
        remains, the store is removed entirely instead of left as an empty
        override, so the configured file (if any) takes back over.
        """
        info = self._resolve_catalog_target(table)
        key = self._catalog_key_for(info)
        if key is None:
            return {"removed": False, "key": info.path, "tables": len(self._catalog())}
        merged = {k: v for k, v in self._catalog().items() if k != key}
        if merged:
            self._write_catalog_store(merged)
        else:
            self.clear_catalog()
        logger.info("semantic catalog: removed entry %r", key)
        return {"removed": True, "key": key, "tables": len(merged)}

    def _catalog_for(self, info: TableInfo) -> dict:
        """The catalog entry for a table, matched by path/qualified/bare name."""
        catalog = self._catalog()
        if not catalog:
            return {}
        for key in (info.path, info.qualified_name, info.name):
            entry = catalog.get(key)
            if isinstance(entry, dict):
                return entry
        return {}

    def table_description(self, info: TableInfo) -> str:
        """Human description for a table from the semantic catalog ('' if none)."""
        return str(self._catalog_for(info).get("description") or "")

    # ------------------------------------------------------------- resolve
    def _resolve(self, table: str) -> TableInfo:
        """Resolve a bare name (or schema/name) to a TableInfo.

        Matches the discovered tables first so the physical ``location`` is
        preserved: a table's logical ``schema/name`` can differ from its
        object-store folder when the folder is nested more than one level
        deep (e.g. ``finance/b/c`` -> schema ``b``, name ``c``). Falling back
        to constructing a fresh TableInfo would drop ``location`` and break
        opening the dataset.
        """
        _safe_table_name(table)
        for info in self.list_tables():
            if info.path == table or info.name == table or info.qualified_name == table:
                return info
        # Policy-hidden tables resolve from the UNFILTERED provider list: the
        # registration path needs their TableInfo (schema) to serve the empty
        # relation / refuse honestly. _resolve does not decide VISIBILITY —
        # every caller-facing surface checks the rule after resolving.
        for info in self._provider_tables() + self._virtual_infos(self._provider_tables()):
            if info.path == table or info.name == table or info.qualified_name == table:
                return info
        if "/" in table:
            schema, name = table.split("/", 1)
            if schema and name:
                return TableInfo(name=name, schema=schema)
        raise LakehouseError(f"Table '{table}' not found in data source")

    # ------------------------------------------------------------ datasets
    def _safe_version(self, info: TableInfo):
        """Run the provider's cheap version check; never raise."""
        try:
            return self.provider.check_version(info)
        except Exception:
            return None

    def _open_dataset(self, info: TableInfo, version: int | None = None):
        """Return a pyarrow Dataset for info, reusing a cached one when fresh.

        For versionable sources (Delta Lake) the cached handle is invalidated
        when the source's snapshot version changes, so new ETL commits are
        visible without waiting out the dataset cache TTL. The version check is
        throttled to the configured interval per table (0 = every reuse) to
        keep the per-query cost negligible. Non-versioned sources (plain
        Parquet/S3/Iceberg) reuse until the TTL as before.

        ``version`` (time travel) pins a HISTORICAL snapshot: the cache key
        includes it and freshness checks are skipped entirely (a historical
        snapshot never changes — it stays valid until the TTL evicts it).
        """
        # Source-aware cache key so same-named tables in federated sources
        # never share a dataset handle; the requested snapshot version is
        # part of the key so historical reads never alias the live dataset.
        key = (info.source, info.path, version)
        now = time.monotonic()
        hit = None
        with self._lock:
            cand = self._dataset_cache.get(key)
            if cand is not None and now - cand[0] < self.dataset_cache_ttl:
                hit = cand
        if hit is not None and version is not None:
            # Historical snapshot: immutable, serve from cache until TTL.
            with self._lock:
                self._dataset_cache.move_to_end(key)
                self._dataset_hits += 1
            return hit[1]
        if hit is not None and version is None:
            _, cached_dset, cached_ver = hit
            if cached_ver is None:
                with self._lock:
                    self._dataset_cache.move_to_end(key)
                    self._dataset_hits += 1
                return cached_dset
            with self._lock:
                last = self._version_checked_at.get(key, 0.0)
            if self.version_check_interval > 0 and now - last < self.version_check_interval:
                with self._lock:
                    self._dataset_cache.move_to_end(key)
                    self._dataset_hits += 1
                return cached_dset
            current = self._safe_version(info)
            with self._lock:
                self._version_checked_at[key] = time.monotonic()
                if current == cached_ver:
                    self._dataset_cache.move_to_end(key)
                    self._dataset_hits += 1
                    return cached_dset
        dset = self.provider.open_dataset(info, version)
        version = None if version is not None else self._safe_version(info)
        # One access = one bump, whether the open was fresh or revalidated.
        with self._lock:
            self._table_usage[(info.source, info.path)] = self._table_usage.get((info.source, info.path), 0) + 1
            self._unsaved_opens += 1
        with self._lock:
            self._dataset_misses += 1
            self._version_checked_at[key] = time.monotonic()
            if self.dataset_cache_ttl > 0 and self.dataset_cache_tables > 0:
                self._dataset_cache[key] = (time.monotonic(), dset, version)
                self._dataset_cache.move_to_end(key)
                while len(self._dataset_cache) > self.dataset_cache_tables:
                    self._dataset_cache.popitem(last=False)
        return dset

    # ------------------------------------------------------------ describe
    def describe_table(self, table: str, *, caller=None) -> dict:
        """Return column names/types and the canonical URI for a table.

        Cached in-process for cache_ttl seconds keyed by the resolved table
        path, so frequently-described tables come from memory instead of
        re-opening the metadata on every agent call.

        Identity spine: the cache key appends the caller's policy hash when
        non-empty (a masked caller's describe omits masked columns — caching
        it under the raw key would serve the MASKED shape to an unmasked
        caller, and the full shape to a masked one; distinct keys, distinct
        results). A HIDDEN table raises ``LakehouseError`` — for this caller
        it does not exist (list/search never advertise it either).
        """
        effective_caller = caller if caller is not None else current_caller()
        ext = self._match_external_table(table)
        if ext is not None:
            return self._describe_external(*ext)
        info = self._resolve(table)
        rule = self._effective_rule(info, effective_caller)
        if rule.hidden:
            raise LakehouseError(f"Table '{table}' not found in data source")
        if info.format == "virtual":
            return self._describe_virtual(info, table)
        policy_hash = self._caller_policy_hash(effective_caller)
        key: tuple = (info.source, info.path)
        if policy_hash:
            key = (info.source, info.path, policy_hash)
        now = time.monotonic()
        # Poll the catalog's mtime BEFORE the cache lookup: catalog
        # documentation is merged into cached describe results, so an edited
        # catalog must invalidate them (see _catalog()). One stat() per call.
        if self._catalog_path:
            self._catalog()
        with self._lock:
            hit = self._describe_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._describe_hits += 1
                return hit[1]
        dset = self._open_dataset(info)
        schema = dset.schema
        columns_out: list[dict] = [{"name": f.name, "type": str(f.type)} for f in schema]
        # Policy: masked columns are OMITTED from describe (the caller cannot
        # SELECT them — the masking view does not expose them — so advertising
        # them would invite queries that fail, or worse, profile leaks).
        if rule.column_masks:
            masked = {c.lower() for c in rule.column_masks}
            columns_out = [c for c in columns_out if c["name"].lower() not in masked]
        result = {
            "table": table,
            "uri": self.provider.table_uri(info),
            "columns": columns_out,
            "n_columns": len(columns_out),
        }
        # Raw-format marker (landing zone): surfaced so the MCP layer / web UI
        # can badge the table "RAW" the way virtual tables badge "VIRTUAL" —
        # additive; every other format omits the key exactly as before.
        if is_raw_format(info.format):
            result["format"] = info.format
            result["raw"] = True
        # Merge semantic-catalog documentation when present: a table-level
        # description plus per-column notes. LLMs write far better SQL with
        # the business meaning attached, and the catalog is optional — no
        # entry means the output is exactly as before.
        self._merge_catalog_docs(self._catalog_for(info), result)
        with self._lock:
            self._describe_misses += 1
            if self.cache_ttl > 0:
                self._describe_cache[key] = (time.monotonic(), result)
        self._save_cache_to_disk()
        return result

    @staticmethod
    def _merge_catalog_docs(entry: dict | None, result: dict) -> None:
        """Merge semantic-catalog documentation into a describe result (in place).

        Table-level description, aliases (capped like the MCP output) and
        per-column notes for the columns the result actually has.
        """
        if not entry:
            return
        if entry.get("description"):
            result["description"] = str(entry["description"])
        if entry.get("aliases"):
            result["aliases"] = [str(a) for a in entry["aliases"]][:8]
        col_docs = entry.get("columns")
        if isinstance(col_docs, dict):
            for col in result["columns"]:
                doc = col_docs.get(col["name"])
                if doc:
                    col["description"] = str(doc)

    def _describe_virtual(self, info: TableInfo, table: str) -> dict:
        """Describe a virtual table: derive its real schema from the definition.

        The definition is created as a view on a throwaway locked-down
        connection and DESCRIBEd — DuckDB binds views lazily, so the
        (potentially expensive) definition does NOT run; only its output
        schema is computed against the registered base tables. Types come
        from the engine, not from the catalog's ``columns`` docs (those merge
        in as documentation, exactly like physical tables). Results cache and
        invalidate with the catalog's mtime like every other describe.
        """
        key = (info.source, info.path)
        now = time.monotonic()
        # Poll the catalog's mtime BEFORE the cache lookup — a virtual table's
        # schema AND docs both derive from the catalog (see _catalog()).
        if self._effective_catalog_file():
            self._catalog()
        with self._lock:
            hit = self._describe_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._describe_hits += 1
                return hit[1]
        import duckdb

        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            # The registration SQL is only matched for table references, never
            # executed — it exists to pull in this virtual table's closure.
            # materialize=False: describing must stay a cheap schema bind —
            # a describe must never trigger the (potentially expensive) first
            # materialization.
            self._register_schema(con, f"SELECT * FROM {_safe_ident(info.name)}", materialize=False)
            rows = con.sql(f"DESCRIBE {_safe_ident(info.name)}").fetchall()
        except Exception as exc:
            raise LakehouseError(f"Describing virtual table '{info.name}' failed: {exc}") from exc
        finally:
            con.close()
        result = {
            "table": table,
            "uri": f"virtual://{info.name}",
            "virtual": True,
            "columns": [{"name": r[0], "type": r[1]} for r in rows],
            "n_columns": len(rows),
        }
        self._merge_catalog_docs(self._catalog_for(info), result)
        with self._lock:
            self._describe_misses += 1
            if self.cache_ttl > 0:
                self._describe_cache[key] = (time.monotonic(), result)
        self._save_cache_to_disk()
        return result

    # ---------------------------------------------------------- external
    def _sql_needs_external(self, sql: str) -> bool:
        """True when the SQL references an attached database's catalog."""
        return bool(self.attaches) and bool(sql_references_attach(sql, self.attaches))

    def _match_external_table(self, table: str) -> tuple[AttachSpec, str] | None:
        """Match a ``<alias>.<...>`` table reference against the attach config.

        Returns ``(spec, validated_dotted_name)`` for attached-database
        references, None for lake tables (and for anything without a dot —
        bare names can never address another catalog).
        """
        if not self.attaches or "." not in table:
            return None
        first = table.split(".", 1)[0].lower()
        for spec in self.attaches:
            if spec.name.lower() == first:
                return spec, validate_qualified_name(spec.name, table)
        return None

    def _external_connection(self):
        """A locked-down DuckDB connection with every database attached.

        The exact production sequence (see sqlhandler/external.py):
        LOAD scanners -> ATTACH READ_ONLY -> fs lockdown. Used by the
        metadata routes (attached listing / describe / profile); data
        queries go through QueryJob, which applies the same sequence on a
        fresh connection per query.
        """
        import duckdb

        con = duckdb.connect()
        try:
            apply_external(con, self.attaches)
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
        except Exception:
            con.close()
            raise
        return con

    def attached_databases(self, refresh: bool = False) -> list[dict]:
        """Live inventory of the attached databases (TTL-cached).

        One probe connection per refresh; each entry carries the catalog
        alias, a credential-free display URI, and its tables. A database
        that fails to attach is reported with an ``error`` key (scrubbed)
        instead of failing the whole listing.
        """
        if not self.attaches:
            return []
        now = time.monotonic()
        with self._lock:
            if not refresh and self._attached_listing is not None:
                ts, listing = self._attached_listing
                if now - ts < self.cache_ttl:
                    return listing
        listing = []
        for spec in self.attaches:
            entry: dict = {
                "name": spec.name,
                "type": spec.type,
                "uri": spec.display_uri,
                "read_only": True,
                "tables": [],
                "error": None,
            }
            try:
                con = self._external_connection()
            except Exception as exc:
                entry["error"] = str(exc)
                listing.append(entry)
                continue
            try:
                rows = con.execute(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_catalog = ? AND table_schema NOT IN "
                    "('pg_catalog', 'information_schema', 'mysql', "
                    "'performance_schema', 'sys') "
                    "ORDER BY table_schema, table_name",
                    [spec.name],
                ).fetchall()
                entry["tables"] = [{"qualified": f"{spec.name}.{s}.{t}", "schema": s, "name": t} for s, t in rows[:500]]
                if len(rows) > 500:
                    entry["truncated"] = len(rows) - 500
            except Exception as exc:
                entry["error"] = str(exc)
            finally:
                con.close()
            listing.append(entry)
        with self._lock:
            self._attached_listing = (time.monotonic(), listing)
        return listing

    def _describe_external(self, spec: AttachSpec, qualified: str) -> dict:
        """describe_table for an attached-database table (cached like lake describes).

        ``DESCRIBE SELECT …`` is bind-only — schema comes from the server's
        catalog without fetching any rows.
        """
        key = ("external", qualified)
        now = time.monotonic()
        with self._lock:
            hit = self._describe_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._describe_hits += 1
                return hit[1]
        con = self._external_connection()
        try:
            rows = con.execute(f"DESCRIBE SELECT * FROM {qualified}").fetchall()
        except Exception as exc:
            raise LakehouseError(f"Describing attached table '{qualified}' failed: {exc}") from exc
        finally:
            con.close()
        result = {
            "table": qualified,
            "uri": spec.display_uri,
            "source": "external",
            "read_only": True,
            "columns": [{"name": r[0], "type": r[1]} for r in rows],
            "n_columns": len(rows),
        }
        with self._lock:
            self._describe_misses += 1
            if self.cache_ttl > 0:
                self._describe_cache[key] = (time.monotonic(), result)
        self._save_cache_to_disk()
        return result

    def _profile_external(self, spec: AttachSpec, qualified: str, columns: Sequence[str] | None = None) -> dict:
        """profile_table for an attached-database table.

        ``SUMMARIZE`` runs against the live server (bounded by
        ``SQLHANDLER_PROFILE_MAX_ROWS``); there is no cheap metadata row
        count for a live database, so ``n_rows`` is None — the profiled
        count comes from the capped sample.
        """
        col_key = tuple(columns) if columns else ()
        key = ("external", qualified, col_key)
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]
        cap = _profile_max_rows()
        con = self._external_connection()
        try:
            col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
            inner = f"SELECT {col_sel} FROM {qualified}"
            if cap > 0:
                inner = f"SELECT * FROM ({inner}) LIMIT {cap}"
            profiled = con.sql(f"SELECT count(*) FROM ({inner})").fetchone()
            summary = con.sql(f"SUMMARIZE {inner}").arrow()
            if isinstance(summary, pa.RecordBatchReader):
                summary = summary.read_all()
        except Exception as exc:
            raise LakehouseError(f"Profiling attached table '{qualified}' failed: {exc}") from exc
        finally:
            con.close()
        result = {
            "table": qualified,
            "uri": spec.display_uri,
            "source": "external",
            "read_only": True,
            "n_rows": None,
            "profiled_rows": int(profiled[0]) if profiled else 0,
            "profile_max_rows": cap,
            "n_columns": len(summary),
            "columns": [
                {
                    "name": str(r.get("column_name", "")),
                    "type": str(r.get("column_type", "")),
                    "min": r.get("min"),
                    "max": r.get("max"),
                    "approx_unique": r.get("approx_unique"),
                    "null_pct": r.get("null_percentage"),
                    "avg": r.get("avg"),
                    "std": r.get("std"),
                    "q25": r.get("q25"),
                    "q50": r.get("q50"),
                    "q75": r.get("q75"),
                    "non_null": r.get("count"),
                }
                for r in summary.to_pylist()
            ],
        }
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    # ------------------------------------------------------------- search
    def search_tables(self, query: str, limit: int = 20, *, caller=None) -> list[dict]:
        """Find tables matching a free-text query (names, columns, catalog docs).

        Deliberately cheap: matches against the cached table list, the
        semantic catalog (descriptions/aliases/column docs) and any already
        cached describe results — it never triggers a schema fetch per
        table, so it stays usable when ``list_tables`` would flood a model's
        context with hundreds of entries.

        Returns a list of matches, best first:
          ``{"table", "name", "qualified_name", "format", "source",
             "description", "matched_columns", "score", "matched_on"}``

        Ranking layers EXACT/substring matching — table-name equality,
        substring hits on name/aliases, term hits on names, catalog docs and
        column names — with a FUZZY layer (additive): difflib similarity so
        typos and near-miss names ("work oder" -> ``work_order``) still match.
        Substring bonuses are strictly larger than fuzzy bonuses, so exact
        hits always outrank near-misses, and a query that is neither a
        substring nor close to any name still returns an empty list.

        Identity spine: iterates the CALLER-VISIBLE table list (policy-hidden
        tables are never searched/advertised — ``list_tables(caller=...)``
        applies the same filter), so a hidden table cannot be discovered by
        search either.
        """
        q = query.strip().lower()
        if not q:
            return []
        terms = [t for t in re.split(r"[^a-z0-9_]+", q) if t]
        results: list[dict] = []
        with self._lock:
            cached_describes = {k: v for k, v in self._describe_cache.items()}
        for info in self.list_tables(caller=caller):
            entry = self._catalog_for(info)
            desc = str(entry.get("description") or "")
            aliases = [str(a) for a in entry.get("aliases") or []]
            raw_cols = entry.get("columns")
            col_docs = raw_cols if isinstance(raw_cols, dict) else {}
            # Column names from an already-cached describe (never a fetch).
            cached = cached_describes.get((info.source, info.path))
            col_names = [c["name"] for c in cached[1].get("columns", [])] if cached else []

            hay_name = f"{info.source}/{info.path}".lower()
            hay_docs = " ".join([desc, *aliases, *map(str, col_docs.values())]).lower()
            # Column index: name -> doc text (doc may be empty for cache-only
            # columns). A column "matches" when a term hits its name OR its
            # catalog documentation.
            col_index = {**{c: "" for c in col_names}, **col_docs}
            score = 0
            matched_cols: list[str] = []
            matched_on: list[str] = []
            if info.name.lower() == q or info.path.lower() == q or info.qualified_name.lower() == q:
                score += 100
                matched_on.append("exact-name")
            elif q in hay_name:
                score += 80
                matched_on.append("substring-name")
            if any(q in a.lower() for a in aliases):
                score += 60
                matched_on.append("alias")
            if terms:
                if any(t in hay_name for t in terms):
                    score += 40
                    matched_on.append("terms-name")
                if any(t in hay_docs for t in terms):
                    score += 30
                    matched_on.append("terms-docs")
                matched_cols = [
                    c for c, doc in col_index.items() if any(t in c.lower() or t in str(doc).lower() for t in terms)
                ]
                if matched_cols:
                    score += 30
                    matched_on.append("columns")
            elif q:
                matched_cols = [c for c, doc in col_index.items() if q in c.lower() or q in str(doc).lower()]
                if matched_cols:
                    score += 30
                    matched_on.append("columns")

            # Fuzzy layer (additive): near-miss names via difflib similarity.
            # Whole-query ratio against the name forms, then per-term ratios
            # against name/alias tokens and column names/docs. Deliberately
            # lower bonuses than any substring layer (<=30 vs 30-100) so exact
            # matches keep ranking first, with thresholds high enough that an
            # unrelated query still matches nothing at all.
            import difflib

            best_name = max(
                difflib.SequenceMatcher(None, q, form).ratio()
                for form in (info.name, info.path, info.qualified_name, hay_name)
            )
            if best_name >= 0.6:
                score += round(30 * best_name)
                matched_on.append("fuzzy-name")
            if terms:
                name_tokens = {t for t in re.split(r"[^a-z0-9_]+", hay_name) if t} | {a.lower() for a in aliases}
                token_bonus = 0
                for t in terms:
                    if any(difflib.SequenceMatcher(None, t, tok).ratio() >= 0.8 for tok in name_tokens if tok):
                        token_bonus += 12
                        matched_on.append("fuzzy-name-token")
                score += min(token_bonus, 24)
                for c, doc in col_index.items():
                    if c in matched_cols:
                        continue
                    cl = c.lower()
                    if any(
                        difflib.SequenceMatcher(None, t, cl).ratio() >= 0.8
                        or (doc and difflib.SequenceMatcher(None, t, str(doc).lower()).ratio() >= 0.8)
                        for t in terms
                    ):
                        matched_cols.append(c)
                        score += 10
                        matched_on.append("fuzzy-column")

            if score <= 0:
                continue
            results.append(
                {
                    "table": info.path,
                    "name": info.name,
                    "qualified_name": info.qualified_name,
                    "format": info.format,
                    "source": info.source,
                    "description": desc,
                    "matched_columns": matched_cols[:10],
                    "score": score,
                    "matched_on": list(dict.fromkeys(matched_on))[:5],
                }
            )
        results.sort(key=lambda r: (-r["score"], r["table"]))
        return results[: max(limit, 0)]

    # ------------------------------------------------------------- profile
    def profile_table(self, table: str, columns: Sequence[str] | None = None, *, caller=None) -> dict:
        """Column-level statistics for a table (cached like describe_table).

        Runs DuckDB's ``SUMMARIZE`` over the table's registered Dataset, so
        the same storage path (pyarrow scan) does the IO and the container
        memory budget applies. Returns, per column: min/max, approx distinct
        count, null percentage, avg/std and the q25/q50/q75 quantiles —
        exactly what an agent needs to write correct filters without
        trial-and-error queries. The full-table row count comes from the
        Parquet/Delta metadata (cheap) and is reported separately from the
        number of rows actually summarized (bounded by
        ``SQLHANDLER_PROFILE_MAX_ROWS``).

        Args:
            table: table name (``schema/name`` when the source uses schemas;
                ``<db-alias>.<schema>.<table>`` for an attached database).
            columns: optional subset of columns to profile (default: all).
            caller: keyword-only (identity spine). Policy enforcement
                profiles through the MASKING VIEW (a masked caller's stats
                describe the masked rows/columns only — never the raw data),
                the cache key folds the caller's policy hash when non-empty,
                and a HIDDEN table raises not-found.
        """
        effective_caller = caller if caller is not None else current_caller()
        ext = self._match_external_table(table)
        if ext is not None:
            return self._profile_external(*ext, columns=columns)
        info = self._resolve(table)
        rule = self._effective_rule(info, effective_caller)
        if rule.hidden:
            raise LakehouseError(f"Table '{table}' not found in data source")
        if info.format == "virtual":
            return self._profile_virtual(info, table, columns, caller=effective_caller)
        policy_hash = self._caller_policy_hash(effective_caller)
        col_key = tuple(columns) if columns else ()
        key = (info.source, info.path, col_key, policy_hash) if policy_hash else (info.source, info.path, col_key)
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]

        dset = self._open_dataset(info)

        # Full-table row count from metadata (Parquet row-group counts /
        # Delta log stats) — no data IO for well-formed files. A policy-
        # covered table does NOT get the raw metadata count: the row count
        # of the UNMASKED table leaks through min/max-free metadata. The
        # masked SUMMARIZE's own count (profiled_rows) is the honest number.
        try:
            n_rows: int | None = None if not rule.empty else int(dset.count_rows())
        except Exception:
            n_rows = None

        import duckdb

        cap = _profile_max_rows()
        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            view = "_sqlhandler_profile_target"
            if rule.empty:
                con.register(view, dset)
            else:
                # Profile through the masking view: same builder as the query
                # path, so stats describe EXACTLY what run_sql would return.
                base = "__sqlhandler_profile_base"
                con.register(base, dset)
                mask_sql = policy_mod.build_mask_select(
                    base, [f.name for f in dset.schema], rule.column_masks, rule.row_filter
                )
                con.execute(f"CREATE OR REPLACE VIEW {view} AS ({mask_sql})")
            # Masked columns are omitted up front: SUMMARIZE over the view
            # already excludes them, but an explicit column subset naming a
            # masked column must be refused honestly (not silently dropped).
            if rule.column_masks and columns:
                masked = {c.lower() for c in rule.column_masks}
                offending = [c for c in columns if c.lower() in masked]
                if offending:
                    raise LakehouseError(
                        f"Column(s) {', '.join(offending)} on table '{table}' are masked for this "
                        "caller and cannot be profiled."
                    )
            col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
            inner = f"SELECT {col_sel} FROM {view}"
            if cap > 0:
                inner = f"SELECT * FROM ({inner}) LIMIT {cap}"
            # Single data scan: the row count of the bounded sample follows
            # from the metadata count (the LIMITed subquery yields exactly
            # min(n_rows, cap) rows), so SUMMARIZE is the only query that
            # touches the data — the separate count(*) ran the same scan
            # twice (audit performance finding). The count query only comes
            # back for the rare metadata-unreadable case (or policy-covered
            # tables, where n_rows starts as None by design).
            if n_rows is not None:
                profiled_rows = min(n_rows, cap) if cap > 0 else n_rows
            else:
                profiled_rows = self._profile_count_fallback(con, inner)
            summary = con.sql(f"SUMMARIZE {inner}").arrow()
            if isinstance(summary, pa.RecordBatchReader):
                summary = summary.read_all()
        except LakehouseError:
            raise
        except Exception as exc:
            raise LakehouseError(f"Profiling failed for table '{table}': {exc}") from exc
        finally:
            con.close()

        result = {
            "table": table,
            "uri": self.provider.table_uri(info),
            "n_rows": n_rows,
            "profiled_rows": profiled_rows,
            "profile_max_rows": cap,
            "n_columns": len(summary),
            "columns": self._summarize_columns(summary),
        }
        if not rule.empty:
            result["policy_applied"] = True
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    @staticmethod
    def _profile_count_fallback(con, inner: str) -> int:
        """Row count of the bounded profile sample, by query.

        Only used when the metadata count is unavailable (the source's
        row-group/log stats could not be read) — the one profile path that
        still scans the data twice.
        """
        row = con.sql(f"SELECT count(*) FROM ({inner})").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _summarize_columns(summary: pa.Table) -> list[dict]:
        """Shape a DuckDB ``SUMMARIZE`` result into the profile column dicts."""
        return [
            {
                "name": str(r.get("column_name", "")),
                "type": str(r.get("column_type", "")),
                "min": r.get("min"),
                "max": r.get("max"),
                "approx_unique": r.get("approx_unique"),
                "null_pct": r.get("null_percentage"),
                "avg": r.get("avg"),
                "std": r.get("std"),
                "q25": r.get("q25"),
                "q50": r.get("q50"),
                "q75": r.get("q75"),
                "non_null": r.get("count"),
            }
            for r in summary.to_pylist()
        ]

    def _profile_virtual(self, info: TableInfo, table: str, columns: Sequence[str] | None, *, caller=None) -> dict:
        """Profile a virtual table by running its definition under SUMMARIZE.

        Unlike physical profiling there is no metadata shortcut: both the row
        count and the summary execute the definition — the same locked-down
        DuckDB path as run_sql, with the summary bounded by
        ``SQLHANDLER_PROFILE_MAX_ROWS``. Cached like the physical path, so
        repeated profiling pays the definition cost once per TTL.

        ``caller`` rides into ``_register_schema``: the definition composes
        over masked base views (transitive masking) and the cache key folds
        the caller's policy hash — a masked caller's virtual profile is a
        DISTINCT cached entry from an unmasked one.
        """
        effective_caller = caller if caller is not None else current_caller()
        policy_hash = self._caller_policy_hash(effective_caller)
        col_key = tuple(columns) if columns else ()
        key = (info.source, info.path, col_key, policy_hash) if policy_hash else (info.source, info.path, col_key)
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]
        import duckdb

        cap = _profile_max_rows()
        target = _safe_ident(info.name)
        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            self._register_schema(con, f"SELECT * FROM {target}", caller=effective_caller)
            try:
                counted = con.sql(f"SELECT count(*) FROM {target}").fetchone()
                n_rows: int | None = int(counted[0]) if counted else None
            except Exception:
                n_rows = None  # the summary below still works on a sample
            col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
            inner = f"SELECT {col_sel} FROM {target}"
            if cap > 0:
                inner = f"SELECT * FROM ({inner}) LIMIT {cap}"
            summary = con.sql(f"SUMMARIZE {inner}").arrow()
            if isinstance(summary, pa.RecordBatchReader):
                summary = summary.read_all()
        except Exception as exc:
            raise LakehouseError(f"Profiling failed for virtual table '{info.name}': {exc}") from exc
        finally:
            con.close()
        profiled_rows = n_rows if (n_rows is None or cap <= 0) else min(n_rows, cap)
        result = {
            "table": table,
            "uri": f"virtual://{info.name}",
            "virtual": True,
            "n_rows": n_rows,
            "profiled_rows": profiled_rows,
            "profile_max_rows": cap,
            "n_columns": len(summary),
            "columns": self._summarize_columns(summary),
        }
        if policy_hash:
            result["policy_applied"] = True
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    # -------------------------------------------------------- column stats
    def column_stats(self, table: str, column: str, top_n: int = 5, *, caller=None) -> dict:
        """Statistics for ONE column over a bounded sample (additive, Wave 5).

        A focused complement to :meth:`profile_table` (which stays untouched):
        distinct count, null count/pct, min/max, q25/q50/q75 quantiles and the
        top-N values with counts — the shape an agent needs before writing a
        filter, for exactly one column instead of the whole table.

        Sampling mirrors profile_table: every query reads at most
        ``SQLHANDLER_PROFILE_MAX_ROWS`` rows (default 1M; 0 = full column) —
        never a full-table scan beyond the existing profile cap. The full
        row count still comes from Parquet/Delta metadata where it is free
        (physical tables); virtual/attached tables report ``n_rows: None``
        and the sample sizes instead. Cached like describe/profile.

        Identity spine: a MASKED column is refused honestly (its stats would
        describe values the caller cannot read — and a hash mask's top-values
        list would be a frequency oracle over the raw column); stats over a
        covered-but-unmasked column run through the masking VIEW (the row
        filter applies); hidden tables raise not-found; the cache key folds
        the caller's policy hash when non-empty.
        """
        effective_caller = caller if caller is not None else current_caller()
        ext = self._match_external_table(table)
        if ext is not None:
            return self._column_stats_external(*ext, column, top_n)
        info = self._resolve(table)
        rule = self._effective_rule(info, effective_caller)
        if rule.hidden:
            raise LakehouseError(f"Table '{table}' not found in data source")
        cap = _profile_max_rows()
        policy_hash = self._caller_policy_hash(effective_caller)
        col_key = (str(column).strip().lower(),)
        key = (
            ("colstats", info.source, info.path, col_key, policy_hash)
            if policy_hash
            else ("colstats", info.source, info.path, col_key)
        )
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]

        described = self.describe_table(table, caller=effective_caller)
        col, col_type = _validate_column(described, column, table)
        if rule.column_masks and col.lower() in {c.lower() for c in rule.column_masks}:
            raise LakehouseError(f"Column '{col}' on table '{table}' is masked for this caller and cannot be profiled.")
        import duckdb

        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            if info.format == "virtual":
                target = _safe_ident(info.name)
                self._register_schema(con, f"SELECT * FROM {target}", caller=effective_caller)
                n_rows: int | None = None  # the definition's full count is not paid for a column sample
                virtual = True
            else:
                dset = self._open_dataset(info)
                if rule.empty:
                    try:
                        n_rows = int(dset.count_rows())
                    except Exception:
                        n_rows = None
                else:
                    # Row count of the UNMASKED table is metadata the caller
                    # has not earned — the masked view's own count is honest.
                    n_rows = None
                target = "_sqlhandler_colstats_target"
                if rule.empty:
                    con.register(target, dset)
                else:
                    base = "__sqlhandler_colstats_base"
                    con.register(base, dset)
                    mask_sql = policy_mod.build_mask_select(
                        base, [f.name for f in dset.schema], rule.column_masks, rule.row_filter
                    )
                    con.execute(f"CREATE OR REPLACE VIEW {target} AS ({mask_sql})")
                virtual = False
            stats = _column_stats_queries(con, target, col, cap, top_n, col_type)
        except LakehouseError:
            raise
        except Exception as exc:
            raise LakehouseError(f"Column stats failed for '{table}.{column}': {exc}") from exc
        finally:
            con.close()
        result = {
            "table": table,
            "column": col,
            "type": col_type,
            "uri": f"virtual://{info.name}" if virtual else self.provider.table_uri(info),
            "n_rows": n_rows,
            "sample_cap": cap,
            **stats,
        }
        if virtual:
            result["virtual"] = True
        if not rule.empty:
            result["policy_applied"] = True
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    def _column_stats_external(self, spec: AttachSpec, qualified: str, column: str, top_n: int) -> dict:
        """column_stats for an attached-database table (LIMIT runs server-side)."""
        cap = _profile_max_rows()
        key = ("colstats-ext", qualified, (str(column).strip().lower(),))
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]
        con = self._external_connection()
        try:
            col, col_type = _validate_column(self._describe_external(spec, qualified), column, qualified)
            stats = _column_stats_queries(con, qualified, col, cap, top_n, col_type)
        except LakehouseError:
            raise
        except Exception as exc:
            raise LakehouseError(f"Column stats failed for attached table '{qualified}.{column}': {exc}") from exc
        finally:
            con.close()
        result = {
            "table": qualified,
            "column": col,
            "type": col_type,
            "uri": spec.display_uri,
            "source": "external",
            "read_only": True,
            "n_rows": None,
            "sample_cap": cap,
            **stats,
        }
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    def usage_top_tables(self, n: int = 5) -> tuple[str, ...]:
        """The n most-accessed tables this/past run (for usage-driven prewarm).

        Read counts are accumulated in-memory and persisted with the disk
        warm cache, so a restarted server warms what the previous one
        actually served — no hand-maintained SQLHANDLER_PREWARM_TABLES.
        """
        with self._lock:
            ranked = sorted(self._table_usage.items(), key=lambda kv: -kv[1])
        return tuple(path for (_, path), _ in ranked[: max(n, 0)])

    # -------------------------------------------------------- query memory
    def note_query(
        self,
        sql: str,
        duration_ms: float,
        n_rows: int | None,
        error: str | None = None,
        caller=None,
    ) -> None:
        """Record one query outcome for the query-memory resource (best-effort).

        SQL text is truncated; failures are recorded too so agents can see
        what NOT to repeat. ``caller`` (keyword-only, default None) rides the
        entry so the query-memory RESOURCE can owner-scope entries when
        policy enforcement is on (cross-caller query patterns would leak
        table knowledge a caller's policy hides).
        """
        if self._query_memory.maxlen and self._unsaved_opens >= _USAGE_SAVE_EVERY:
            self._unsaved_opens = 0
            self._save_cache_to_disk()
        if not self._query_memory.maxlen:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "sql": sql[:500],
            "duration_ms": round(duration_ms, 1),
            "n_rows": n_rows,
            "error": error[:200] if error else None,
        }
        # Owner scope — set ONLY when the caller is known (enforcement keeps
        # the shared byte-identical shape when off: entries stay 5-key).
        if caller is not None and policy_mod.policy_enabled():
            entry["owner"] = policy_mod.owner_key(caller)
        with self._lock:
            self._query_memory.append(entry)

    def query_memory(self, caller=None) -> list[dict]:
        """Recent query outcomes, oldest first (a snapshot copy).

        With policy enforcement ON and a caller given: only that caller's
        OWN entries (owner-scoped — another caller's SQL text can reveal
        hidden tables/columns). With enforcement off (or no caller):
        everything, byte-identical to the shared-history behavior.
        """
        with self._lock:
            snapshot = list(self._query_memory)
        if caller is not None and policy_mod.policy_enabled():
            owner = policy_mod.owner_key(caller)
            return [e for e in snapshot if e.get("owner") == owner]
        return snapshot

    def _record_outcome(
        self,
        sql: str,
        duration_ms: float | None,
        n_rows: int | None,
        state: str,
        error: str | None = None,
        caller=None,
    ) -> None:
        """Single outcome choke point: query memory + metrics + audit log.

        Called by QueryJob for every finished query (ok, error, cancelled);
        each consumer is individually best-effort so observability can never
        break a query. ``caller`` (explicit, default None — the QueryJob
        thread boundary) feeds the audit ``caller`` field and the caller-class
        metric; None keeps every existing series byte-identical.
        """
        self.note_query(sql, duration_ms or 0.0, n_rows, error, caller=caller)
        observability.metrics.record_query(state, (duration_ms or 0.0) / 1000.0, n_rows)
        observability.metrics.record_caller_query(getattr(caller, "cls", None) or "anonymous")
        observability.audit_query(sql, state, duration_ms, n_rows, error, caller=caller)

    # ------------------------------------------------------------- scans
    def scan_arrow(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        filters: Sequence | None = None,
        limit: int | None = None,
        version_as_of: int | None = None,
        *,
        caller=None,
    ) -> pa.Table:
        """Read a table as an in-memory Arrow table.

        filters may be a single pyarrow.compute expression or a list of
        expressions (AND-ed). Column projection and predicates push down.

        A missing/negative limit (the historical "whole table") is clamped
        to ``SQLHANDLER_MAX_ROWS`` (decision D4) so ``scan_table(limit=-1)``
        can no longer materialize an entire table; pass an explicit positive
        limit for more rows. ``SQLHANDLER_MAX_ROWS=0`` keeps the old
        unlimited behavior.

        ``version_as_of`` reads a historical snapshot (Delta version or
        Iceberg snapshot id) instead of the current one.

        ``caller`` (keyword-only, identity spine): when policy enforcement
        covers this table, the scan DELEGATES to the SQL path (the masking
        views) — pyarrow predicates cannot express row filters/column masks,
        the same refusal posture ``_scan_virtual`` already has for virtual
        tables. Uncovered tables scan exactly as before (byte-identical).
        """
        effective_caller = caller if caller is not None else current_caller()
        if limit is None or limit < 0:
            cap = _max_rows()
            limit = cap if cap > 0 else None
        info = self._resolve(table)
        rule = self._effective_rule(info, effective_caller)
        if rule.hidden:
            raise LakehouseError(f"Table '{table}' not found in data source")
        if info.format == "virtual":
            return self._scan_virtual(info, columns, filters, limit, version_as_of, caller=effective_caller)
        # Policy enforcement: a covered table canNOT go through the pyarrow
        # scanner (masks/row filters have no pyarrow predicate form) — the
        # SQL path serves it from the masking view instead.
        if not rule.empty:
            if filters:
                raise LakehouseError(
                    f"scan_table filters cannot push into policy-covered table '{info.name}' — "
                    "the table is masked for this caller; use run_sql with a WHERE clause instead"
                )
            return self._sql_scan(
                info,
                columns,
                limit,
                version_as_of,
                caller=effective_caller,
                note="policy-covered: delegated to the SQL path",
            )
        _validate_snapshot_version(version_as_of, "Time travel") if version_as_of is not None else None
        dset = self._open_dataset(info, version_as_of)

        expr = None
        for f in filters or []:
            expr = f if expr is None else (expr & f)

        scan = dset.scanner(
            columns=list(columns) if columns else None,
            filter=expr,
            batch_size=65536,
        )
        if limit is not None and limit >= 0:
            # head() stops the scan early; slicing after to_table() would
            # first load the ENTIRE table into memory.
            return scan.head(limit)
        return scan.to_table()

    def _sql_scan(
        self,
        info: TableInfo,
        columns: Sequence[str] | None,
        limit: int | None,
        version_as_of: int | None,
        *,
        caller=None,
        note: str = "",
    ) -> pa.Table:
        """One table through the SQL path with column projection + limit.

        The scan_arrow-under-policy and _scan_virtual shared body (both
        surface "read this table as rows" through DuckDB, where masking
        views / virtual definitions live). Time travel passes through.
        """
        target = _safe_ident(info.name)
        col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
        if note:
            logger.debug("scan %s: %s", info.name, note)
        return self.query_duckdb(
            f"SELECT {col_sel} FROM {target}",
            limit=limit if (limit is not None and limit >= 0) else None,
            version_as_of=version_as_of,
            caller=caller,
        )

    # -------------------------------------------------------- sample rows
    def sample_rows(
        self,
        table: str,
        limit: int | None = None,
        columns: Sequence[str] | None = None,
        *,
        caller=None,
    ) -> dict:
        """Head-of-table sample + per-column fill rates (agent productivity).

        One bounded look at the DATA (describe/profile give schema and
        statistics; this shows actual rows). Returns::

            {"table", "uri", "n_rows", "sampled_rows", "sample_limit",
             "columns": [{name, type, fill_count, fill_pct, null_count}...],
             "rows": [{col: value, ...}, ...]}

        Posture per surface:

        * **Physical tables** — the profile-sampler posture: a pyarrow
          scanner projection over the dataset with ``.head(limit)`` (head()
          stops the scan early — never a full-table read to fetch N rows).
          The limit is resolved with the same D4 semantics as scan_table
          (``SQLHANDLER_MAX_ROWS`` caps a missing/negative limit).
        * **Virtual tables** — routed through the SQL path
          (``SELECT ... LIMIT n``): they have no Dataset, and pyarrow
          predicates/projections cannot push into their definitions (the
          same refusal ``scan_arrow`` shows for virtual tables).
        * **Attached external tables** — SQL ``LIMIT`` runs server-side on
          the attached catalog (the profile-external posture).

        ``SQLHANDLER_PROFILE_MAX_ROWS`` bounds the sample (the fill rates
        and the rows must describe the SAME sample, so the cap applies to
        both), and the per-row payload respects the
        ``SQLHANDLER_MAX_OUTPUT_ROWS`` discipline (the renderer caps rows).

        Stratification v1 = an honest head + fill rates — no hidden
        ordering, no binning: the sample states exactly which rows it took.

        Identity spine: a policy-covered table DELEGATES to the SQL path
        (the masking view supplies the sample — masked columns never appear,
        the row filter applies); hidden tables raise not-found. Uncovered
        tables sample byte-identically.
        """
        effective_caller = caller if caller is not None else current_caller()
        ext = self._match_external_table(table)
        if ext is not None:
            return self._sample_rows_external(*ext, limit=limit, columns=columns)
        info = self._resolve(table)
        rule = self._effective_rule(info, effective_caller)
        if rule.hidden:
            raise LakehouseError(f"Table '{table}' not found in data source")
        if info.format == "virtual":
            return self._sample_rows_virtual(info, table, limit=limit, columns=columns, caller=effective_caller)

        cap = _profile_max_rows()
        # D4 limit semantics, shared with scan_table: missing/negative ->
        # SQLHANDLER_MAX_ROWS; an explicit positive limit is honored exactly;
        # an explicit 0 stays an explicit empty sample.
        resolved = _resolve_sample_limit(limit)
        # The PROFILE cap bounds how much of the table a sample may read
        # (when the MAX_ROWS cap is off/unlimited, the profile cap is the
        # guardrail; when both apply, the smaller governs).
        if resolved is None and cap > 0:
            resolved = cap
        elif resolved is not None and cap > 0:
            resolved = min(resolved, cap)

        if not rule.empty:
            # Policy-covered: the SQL path (masking view) — sample rows MUST
            # be the rows run_sql would return, not raw-head rows.
            if columns:
                masked = {c.lower() for c in rule.column_masks}
                offending = [c for c in columns if c.lower() in masked]
                if offending:
                    raise LakehouseError(
                        f"Column(s) {', '.join(offending)} on table '{table}' are masked for this "
                        "caller and cannot be sampled."
                    )
            sample = self._sql_scan(info, columns, resolved, None, caller=effective_caller)
            return self._shape_sample(
                table=table,
                uri=self.provider.table_uri(info),
                n_rows=None,  # raw metadata count would leak unmasked cardinality
                sample=sample,
                requested_limit=limit,
                resolved_limit=resolved,
                cap=cap,
                policy_applied=True,
            )

        dset = self._open_dataset(info)
        scan = dset.scanner(
            columns=list(columns) if columns else None,
            batch_size=65536,
        )
        sample = scan.head(resolved) if resolved is not None and resolved >= 0 else scan.to_table()

        return self._shape_sample(
            table=table,
            uri=self.provider.table_uri(info),
            n_rows=self._metadata_row_count(dset),
            sample=sample,
            requested_limit=limit,
            resolved_limit=resolved,
            cap=cap,
        )

    def _sample_rows_virtual(
        self,
        info: TableInfo,
        table: str,
        limit: int | None,
        columns: Sequence[str] | None,
        *,
        caller=None,
    ) -> dict:
        """sample_rows for a virtual table: the SQL path (it has no Dataset).

        pyarrow filters/projections cannot push into a definition, so the
        sample is ``SELECT ... LIMIT n`` — exactly how scan_arrow serves
        virtual tables. The same D4/PROFILE cap resolution bounds the LIMIT.
        ``caller`` passes into query_duckdb (transitive masking via the
        registered base views).
        """
        cap = _profile_max_rows()
        resolved = _resolve_sample_limit(limit)
        if resolved is None and cap > 0:
            resolved = cap
        elif resolved is not None and cap > 0:
            resolved = min(resolved, cap)
        target = _safe_ident(info.name)
        col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
        inner = f"SELECT {col_sel} FROM {target}"
        if resolved is not None and resolved >= 0:
            inner = f"SELECT * FROM ({inner}) LIMIT {int(resolved)}"
        sample = self.query_duckdb(inner, caller=caller)
        n_rows: int | None = None
        try:
            counted = self.query_duckdb(f"SELECT count(*) FROM {target}", caller=caller)
            n_rows = int(counted.column(0)[0]) if counted.num_rows else None
        except Exception:
            n_rows = None  # the sample still stands on its own
        return self._shape_sample(
            table=table,
            uri=f"virtual://{info.name}",
            n_rows=n_rows,
            sample=sample,
            requested_limit=limit,
            resolved_limit=resolved,
            cap=cap,
            virtual=True,
        )

    def _sample_rows_external(
        self,
        spec: AttachSpec,
        qualified: str,
        limit: int | None,
        columns: Sequence[str] | None,
    ) -> dict:
        """sample_rows for an attached-database table: LIMIT runs server-side."""
        cap = _profile_max_rows()
        resolved = _resolve_sample_limit(limit)
        if resolved is None and cap > 0:
            resolved = cap
        elif resolved is not None and cap > 0:
            resolved = min(resolved, cap)
        con = self._external_connection()
        try:
            col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
            inner = f"SELECT {col_sel} FROM {qualified}"
            if resolved is not None and resolved >= 0:
                inner = f"SELECT * FROM ({inner}) LIMIT {int(resolved)}"
            sample = con.sql(inner).arrow()
            if isinstance(sample, pa.RecordBatchReader):
                sample = sample.read_all()
        except Exception as exc:
            raise LakehouseError(f"Sampling attached table '{qualified}' failed: {exc}") from exc
        finally:
            con.close()
        return self._shape_sample(
            table=qualified,
            uri=spec.display_uri,
            n_rows=None,  # a live database has no cheap metadata count
            sample=sample,
            requested_limit=limit,
            resolved_limit=resolved,
            cap=cap,
            source="external",
        )

    @staticmethod
    def _metadata_row_count(dset) -> int | None:
        """Full-table row count from Parquet/Delta metadata (best-effort)."""
        try:
            return int(dset.count_rows())
        except Exception:
            return None

    @staticmethod
    def _shape_sample(
        table: str,
        uri: str,
        n_rows: int | None,
        sample: pa.Table,
        requested_limit: int | None,
        resolved_limit: int | None,
        cap: int,
        virtual: bool = False,
        source: str | None = None,
        policy_applied: bool = False,
    ) -> dict:
        """Shape one sample + per-column fill rates into the result dict.

        Fill rates come from the SAME bounded sample the rows come from —
        one honest posture, never mixed: a column's fill_pct is
        ``(sampled_rows - null_count) / sampled_rows``.
        """
        sampled_rows = sample.num_rows
        fill: list[dict] = []
        for field in sample.schema:
            col = sample.column(field.name)
            null_count = col.null_count
            fill_count = sampled_rows - null_count
            fill_pct = round(fill_count * 100.0 / sampled_rows, 1) if sampled_rows else 0.0
            fill.append(
                {
                    "name": field.name,
                    "type": str(field.type),
                    "fill_count": fill_count,
                    "fill_pct": fill_pct,
                    "null_count": null_count,
                }
            )
        rows = sample.to_pylist()
        result: dict = {
            "table": table,
            "uri": uri,
            "n_rows": n_rows,
            "sampled_rows": sampled_rows,
            "sample_limit": resolved_limit,
            "profile_max_rows": cap,
            "columns": fill,
            "rows": rows,
        }
        if requested_limit is not None and requested_limit < 0:
            result["limit_resolved"] = True  # the D4 clamp applied (limit=-1 -> cap)
        if virtual:
            result["virtual"] = True
        if source is not None:
            result["source"] = source
            result["read_only"] = True
        if policy_applied:
            result["policy_applied"] = True
        return result

    def _scan_virtual(
        self,
        info: TableInfo,
        columns: Sequence[str] | None,
        filters: Sequence | None,
        limit: int | None,
        version_as_of: int | None,
        *,
        caller=None,
    ) -> pa.Table:
        """Scan a virtual table through the SQL path (it has no Dataset).

        Column projection and row limits translate into SQL; pyarrow filter
        expressions have no equivalent without parsing, so they are refused
        with a pointer to run_sql (whose predicates push down into the
        definition's base scans anyway). Time travel passes through: the
        definition's base tables are then read at the requested snapshot.

        ``caller`` passes through to ``query_duckdb``: a virtual definition
        composes over its base tables' registered views, so masking the
        bases masks the virtual transitively (the verified composition path)
        — but only when the enforcement rule actually applies (empty rule =
        byte-identical path, including the materialization cache).
        """
        if filters:
            raise LakehouseError(
                f"scan_table filters cannot push into virtual table '{info.name}' — "
                "use run_sql with a WHERE clause instead"
            )
        target = _safe_ident(info.name)
        col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
        return self.query_duckdb(
            f"SELECT {col_sel} FROM {target}",
            limit=limit if (limit is not None and limit >= 0) else None,
            version_as_of=version_as_of,
            caller=caller,
        )

    # ------------------------------------------------------------ duckdb
    def query_duckdb(
        self,
        sql: str,
        limit: int | None = None,
        params: object | None = None,
        version_as_of: int | None = None,
        row_cap: int | None = None,
        *,
        caller=None,
    ) -> pa.Table:
        """Execute SQL via DuckDB over the provider Datasets.

        Each referenced table is registered as a DuckDB view backed by its
        pyarrow Dataset; DuckDB pushes predicates/projections into the scan.
        The connection runs with DuckDB's own filesystem access disabled
        (see :func:`_duckdb_fs_lockdown`): all storage IO is pyarrow's, and
        DuckDB-side local-file reads / URL fetches / COPY are refused by
        default (``SQLHANDLER_DUCKDB_FILE_ACCESS=1`` opts out).

        Args:
            sql: the SELECT (or WITH ...) statement to run.
            limit: optional row cap applied inside the query.
            params: optional bind parameters — a dict for named ``$name``
                placeholders or a list for positional ``?`` ones. Values must
                be scalars (see :func:`_validate_params`). Bind parameters
                keep client-side query templates injection-safe.
            version_as_of: optional historical snapshot applied to every
                versionable table the query touches (Delta snapshot version
                or Iceberg snapshot id). Plain-Parquet tables in the same
                query are an error (they have no history).
            caller: keyword-only (identity spine). The resolved Caller for
                this query — thread-boundary rule: the query runs on a raw
                ``threading.Thread`` where contextvars do NOT cross, so the
                caller rides the job explicitly. None (default) = the async
                context's caller (``current_caller()``), else anonymous:
                audit lines stay field-absent and metrics stay byte-identical.
                Also feeds policy enforcement (the masking views) and the
                policy hash in the result-cache key.
        """
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        # Keyword-only default: explicit caller wins; else the ambient
        # request context; else None (anonymous — no attribution fields).
        effective_caller = caller if caller is not None else current_caller()
        t0 = time.monotonic()
        # WRITE-TIER INVARIANT (review §4, pinned by test_writes.py): a
        # classified write NEVER touches the cache paths — classification
        # happens BEFORE the cache check, and the write executes outside
        # DuckDB entirely. With the tier flag OFF (default) this block is
        # inert and every line below is exactly today's behavior; with it
        # ON, run_sql hands classified writes here and the cache never sees
        # a write's "result" (a cache hit serving a stale pre-write read
        # would be the failure mode this ordering makes impossible).
        if writes_mod.writes_enabled():
            plan = writes_mod.classify_sql(sql)
            if len(plan) == 1 and plan[0].kind == writes_mod.CLASS_WRITE_SCRATCH:
                return self.execute_write(sql, params=params, caller=effective_caller)
        fast = self._metadata_count_fastpath(sql, version_as_of)
        if fast is not None:
            self._record_outcome(sql, (time.monotonic() - t0) * 1000, 1, state="ok", caller=effective_caller)
            return fast
        # Preview fast path (same choke-point posture as the count fast-path
        # above): env-gated (SQLHANDLER_PREVIEW_FASTPATH, default ON), off
        # switches and params/time-travel/row-cap mismatches fall straight
        # through to the normal path — the flag loader reads with defaults
        # and NEVER raises. The requested `limit` (an explicit arg, distinct
        # from the statement's own LIMIT) and time travel both refuse the
        # shortcut so every row-cap and snapshot contract stays on the full
        # path; params would change the SQL identity mid-flight. The result
        # IS identical in shape to the normal path's, so the cache and
        # rendering below never see the difference.
        if _preview_fastpath_enabled() and not params and version_as_of is None and limit is None:
            fast = self._preview_fastpath(sql, version_as_of)
            if fast is not None:
                cap = _max_rows()
                eff_cap = row_cap if row_cap is not None else cap
                if fast.num_rows > eff_cap > 0:
                    fast = fast.slice(0, eff_cap)
                self._record_outcome(sql, (time.monotonic() - t0) * 1000, fast.num_rows, state="ok", caller=effective_caller)
                return fast
        cache_key = self._result_cache_key(sql, params, limit, row_cap, version_as_of, caller=effective_caller)
        if cache_key is not None:
            cached = self._result_cache_lookup(cache_key)
            if cached is not None:
                self._record_outcome(sql, 0.0, cached.num_rows, state="ok", caller=effective_caller)
                return cached
        timeout = _query_timeout()
        job = QueryJob(
            self,
            sql,
            limit=limit,
            params=params,
            version_as_of=version_as_of,
            row_cap=row_cap,
            caller=effective_caller,
        )
        if timeout > 0:
            job.wait(timeout)
            if job.state == "running":
                # Interrupt inside DuckDB so the worker thread unwinds and
                # the connection is closed instead of leaking.
                job.cancel()
                job.wait(_CANCEL_GRACE_SECONDS)
                raise LakehouseError(
                    _errors.enrich(f"Query timed out after {timeout}s (SQLHANDLER_QUERY_TIMEOUT) and was cancelled.")
                )
        else:
            job.wait()
        if job.state == "done" and cache_key is not None and job.result is not None:
            self._result_cache_store(cache_key, job.result, sql=sql)
        return job.result

    # ------------------------------------------------------------- writes
    def execute_write(
        self,
        sql: str,
        params: object | None = None,
        *,
        caller=None,
        mode: str | None = None,
    ) -> pa.Table:
        """Execute ONE classified write statement; return the write summary.

        The write tier's executor (implementation review §4). The tier flag
        (``SQLHANDLER_WRITES_ENABLED``, default FALSE) is checked HERE as
        well as at the MCP surface — an engine-level caller cannot bypass
        the gate. Flow:

        1. **Classification FIRST** (``writes.classify_sql`` on the same
           DuckDB-parser spans as the read guard) — before ANY cache lookup
           would happen, and before any execution. Reads are NOT accepted
           here: a read goes through :meth:`query_duckdb` (which classifies
           first itself; the invariant is pinned by test_writes.py).
        2. Target validation: ``<allowlisted-root>/<subject-slug>/...`` only
           (no subject -> refused, always; anonymous gets NO write
           capability) — and the target must not collide with a configured
           source table (writes stay outside policy-covered tables).
        3. The SELECT payload runs on a fresh locked-down DuckDB connection
           with the caller's masking views registered (policy applies to
           reads INSIDE the write too: what lands in scratch is exactly
           what the caller could read).
        4. The rows are written OUTSIDE DuckDB — delta-rs ``write_deltalake``
           (default backend) or pyiceberg (``iceberg://``-named roots) — so
           ``_duckdb_fs_lockdown`` and the READ_ONLY attach posture stay
           untouched (the documented conflict; a dedicated writer
           connection is the follow-up if DuckDB MERGE is demanded).
        5. Single-writer discipline: in-process lock keyed
           (backend, path) + advisory lease file (O_EXCL + TTL) on the
           scratch PVC; contention is a RETRYABLE ``E_WRITE_CONFLICT``.

        Returns a ONE-ROW summary Arrow table (target, backend, rows
        written, mode) — the write SUMMARY returns in place of rows.
        """
        if not writes_mod.writes_enabled():
            raise LakehouseError(
                _errors.enrich(
                    "Write refused: the write tier is disabled (SQLHANDLER_WRITES_ENABLED unset/0 — "
                    "the deliberate default; writes are operator-gated)."
                )
            )
        effective_caller = caller if caller is not None else current_caller()
        # 1. classify BEFORE anything else — no cache, no dataset opens, no IO.
        plan = writes_mod.classify_sql(sql)
        if len(plan) != 1:
            raise LakehouseError(
                _errors.enrich(
                    "Write refused: exactly one statement per call on the write tier "
                    f"(got {len(plan)}). Run the read and the write separately."
                )
            )
        cls = plan[0]
        if cls.kind == writes_mod.CLASS_READ:
            raise LakehouseError(
                _errors.enrich(
                    "Write refused: the statement is a read — run_sql handles reads "
                    "(the write tier adds capability, it does not replace the read path)."
                )
            )
        if cls.kind != writes_mod.CLASS_WRITE_SCRATCH:
            detail = f" ({cls.reason})" if cls.reason else ""
            raise LakehouseError(
                _errors.enrich(f"Write refused: {cls.stmt_type} statements are not in the v1 write tier{detail}.")
            )
        # 2. resolve the target under the caller's subject namespace
        #    (refuses: no subject, no roots, traversal, sibling aliases).
        try:
            if cls.target is None:  # a scratch class always parsed a target
                raise writes_mod.WriteError(
                    "Write refused: the statement's target could not be parsed.",
                    code="E_WRITE_TARGET",
                )
            backend, canonical, _uri = writes_mod.resolve_write_target(cls.target, effective_caller)
        except writes_mod.WriteError as exc:
            raise LakehouseError(_errors.enrich(str(exc))) from exc
        self._refuse_write_target_collision(cls.target, canonical)
        # 3. run the SELECT payload on a fresh locked-down connection with
        #    the caller's policy views — reads inside the write are governed
        #    reads (the mask applies; hidden tables register empty).
        select_sql = cls.source_sql or "SELECT 1"
        t0 = time.monotonic()
        rows = self._select_rows_for_write(select_sql, params, effective_caller)
        # 4/5. write through the backend under the single-writer discipline.
        # The parse (above) decides before the lease exists, so a refused
        # write never creates lease litter. ``mode`` (optional) overrides
        # the statement-shape default (append for INSERT, overwrite for
        # CTAS/COPY): ``overwrite`` is the explicit replace semantics,
        # ``append`` fails on a schema mismatch (fail closed — silently
        # widening a target's schema is not v1 behavior).
        write_mode = mode or ("append" if cls.stmt_type == "INSERT" else "overwrite")
        if backend == "iceberg":
            summary = self._write_iceberg_scratch(canonical, rows, write_mode)
        else:
            summary = self._write_delta_scratch(canonical, rows, write_mode)
        # The write REPLACED files under a path the engine may hold a cached
        # dataset handle for (a scratch root inside the provider root):
        # evict it so the next read opens the NEW snapshot (the delta
        # version check would catch an append; the CTAS drop-create changes
        # the file set under the SAME version-0 log, so eviction is the
        # correct invalidation).
        self._evict_dataset_cache_for(canonical)
        # AND the result cache: a read of the written table cached before
        # the write must never be served after it (the never-stale rule).
        # The CTAS drop-create RESETS the Delta log to version 0 — the
        # version token REGRESSES, so the snapshot-token key comparison
        # (which catches every normal ETL commit) cannot see this change;
        # a write-keyed eviction is the only honest invalidation. L2 entries
        # are keyed by the same key material on disk; evicting them keeps
        # the other replicas honest too.
        self._evict_result_cache_for_write(backend, canonical)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self._record_write(cls, backend, canonical, rows.num_rows, elapsed_ms, state="ok", caller=effective_caller)
        return pa.table(
            {
                "target": pa.array([summary["target"]], type=pa.string()),
                "backend": pa.array([summary["backend"]], type=pa.string()),
                "rows_written": pa.array([summary["rows"]], type=pa.int64()),
                "mode": pa.array([summary["mode"]], type=pa.string()),
                "duration_ms": pa.array([round(elapsed_ms, 1)], type=pa.float64()),
            }
        )

    def _select_rows_for_write(self, select_sql: str, params: object | None, caller) -> pa.Table:
        """Run the SELECT payload of a write on a fresh governed connection.

        The exact QueryJob execution shape (locked-down connection + masking
        views + row-cap NOTE: the cap does NOT apply to a write's SELECT —
        the caller asked to write the query RESULT, not a truncated one; the
        statement-shape guard already refused everything but single
        SELECT-payload writes). Attach-touching payloads stay REFUSED:
        attached catalogs are a source, never a sink, and the write tier
        does not change that (the target is scratch; reads stay lake-only
        here for the same exfiltration-shape reasons).
        """
        import duckdb

        if self._sql_needs_external(select_sql):
            raise LakehouseError(
                _errors.enrich(
                    "Write refused: the write tier reads lake tables only — attached external "
                    "databases cannot feed a scratch write (source-never-sink holds for reads "
                    "inside the write too)."
                )
            )
        _validated = _validate_params(params)
        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            self._register_schema(con, select_sql, version=None, caller=caller)
            rel = con.sql(select_sql, params=_validated)
            arrow = rel.arrow()
            if isinstance(arrow, pa.RecordBatchReader):
                arrow = arrow.read_all()
            return arrow
        except Exception as exc:
            hinted = _with_hints(self, select_sql, exc)
            raise LakehouseError(_errors.enrich(f"Write failed (source query): {hinted}")) from exc
        finally:
            try:
                con.close()
            except Exception:
                pass

    def _write_delta_scratch(self, canonical: str, rows: pa.Table, mode: str = "overwrite") -> dict:
        """Delta-rs write of the query result into the subject's scratch.

        ``mode`` is the delta-rs mode: ``overwrite`` (CTAS/COPY semantic —
        the first write creates the table, a re-run replaces it) or
        ``append`` (INSERT semantic — rows are added; a schema mismatch is
        an ERROR, not an implicit schema evolution). All of it under the
        single-writer lock + lease.
        """
        from deltalake import write_deltalake

        if mode not in ("overwrite", "append"):
            raise LakehouseError(f"Write refused: unsupported write mode {mode!r}.")
        key = ("delta", canonical)
        with writes_mod.single_writer_lock(key):
            lease = writes_mod.WriteLease(canonical)
            lease.acquire()
            try:
                exists = os.path.isdir(canonical) and os.path.isdir(os.path.join(canonical, "_delta_log"))
                if not exists and mode == "append":
                    # An INSERT creating its own target: still the overwrite
                    # (create) form internally — nothing exists to append to.
                    mode = "overwrite"
                if exists and mode == "overwrite":
                    # The CTAS semantic is REPLACE, schema included: a re-run
                    # with different columns is a NEW table at the same path,
                    # not a schema conflict. delta-rs' overwrite refuses a
                    # narrower/different payload schema (verified 1.6.3), so
                    # the honest replace is: drop the old directory under the
                    # lease, then create fresh. The lease + lock make the
                    # drop-create atomic against other writers.
                    import shutil

                    shutil.rmtree(canonical)
                    exists = False
                if not exists:
                    os.makedirs(canonical, exist_ok=True)
                try:
                    write_deltalake(canonical, rows, mode="overwrite" if not exists else mode)
                except Exception as exc:
                    if mode == "append" and "Schema" in type(exc).__name__:
                        # A schema mismatch on APPEND is refused, not evolved:
                        # silently widening a scratch table's schema (the
                        # schema_mode=merge behavior) hides drift between what
                        # the caller thinks the target is and what it became.
                        raise LakehouseError(
                            _errors.enrich(
                                f"Write refused: append to '{canonical}' does not match the target's "
                                f"schema ({exc}). Use CREATE TABLE (overwrite) to replace it, or "
                                "align the SELECT's columns/types with the target."
                            )
                        ) from exc
                    raise
            finally:
                lease.release()
        return {"target": canonical, "backend": "delta", "rows": rows.num_rows, "mode": mode}

    def _write_iceberg_scratch(self, canonical: str, rows: pa.Table, mode: str = "overwrite") -> dict:
        """pyiceberg write of the query result into a catalog-managed scratch.

        The root's ``iceberg://`` marker declares a local SQL catalog for v1
        (``ICEBERG_SCRATCH_URI``/``warehouse`` config); REST-catalog scratch
        is the same code path with a different ``load_catalog`` — the
        lockdown posture is untouched either way (pyiceberg writes over
        pyarrow IO, never through DuckDB).
        """
        from pyiceberg.catalog.sql import SqlCatalog

        key = ("iceberg", canonical)
        with writes_mod.single_writer_lock(key):
            lease = writes_mod.WriteLease(canonical)
            lease.acquire()
            try:
                uri = os.environ.get("SQLHANDLER_WRITE_ICEBERG_CATALOG_URI", "").strip()
                warehouse = os.environ.get("SQLHANDLER_WRITE_ICEBERG_WAREHOUSE", "").strip() or None
                if not uri and warehouse:
                    # pyiceberg's SQL catalog REQUIRES a connection URI; when
                    # the operator configured only a warehouse, default the
                    # catalog DB next to it (a sqlite file — local scratch).
                    wh_fs = warehouse.removeprefix("file://")
                    os.makedirs(wh_fs, exist_ok=True)
                    uri = f"sqlite:///{wh_fs}/.sqlhandler-iceberg-catalog.db"
                catalog = SqlCatalog("sqlhandler-scratch", uri=uri, warehouse=warehouse)
                if warehouse:
                    fs_location = warehouse.removeprefix("file://")
                else:
                    fs_location = os.path.dirname(canonical)
                os.makedirs(fs_location, exist_ok=True)
                namespace = os.path.basename(os.path.dirname(canonical)) or "scratch"
                name = os.path.basename(canonical)
                try:
                    catalog.create_namespace(namespace)
                except Exception:
                    pass  # exists
                full_name = (namespace, name)
                created = False
                try:
                    table = catalog.load_table(full_name)
                except Exception:
                    table = catalog.create_table(full_name, rows.schema)
                    created = True
                if created or mode == "overwrite":
                    table.overwrite(rows)
                else:
                    table.append(rows)
                write_mode = "create" if created else mode
            finally:
                lease.release()
        return {"target": canonical, "backend": "iceberg", "rows": rows.num_rows, "mode": write_mode}

    def _refuse_write_target_collision(self, target: str | None, canonical: str) -> None:
        """Writes are ALWAYS outside policy-covered tables (review §4).

        A sloppy root could overlap a source root; refuse a target whose
        canonical path resolves onto a CONFIGURED SOURCE table's location.
        The discriminator is stable: the engine snapshots its source-table
        locations the FIRST time the write tier resolves a target (before
        any write of this process) — a table that was a source at snapshot
        time is a source forever, and anything written since (the caller's
        own prior CTAS re-listed under a scratch root) is not in the
        snapshot and stays writable.
        """
        if self._write_tier_source_paths is None:
            paths: set[str] = set()
            for info in self.list_tables():
                loc = info.location
                if not loc:
                    continue
                if not os.path.isabs(loc):
                    root = getattr(getattr(self.provider, "config", None), "root_dir", "") or ""
                    loc = os.path.join(root, loc) if root else loc
                try:
                    paths.add(os.path.realpath(loc))
                except OSError:
                    paths.add(os.path.normpath(os.path.abspath(loc)))
            self._write_tier_source_paths = paths
        if not self._write_tier_source_paths:
            return
        try:
            canonical_loc = os.path.realpath(canonical) if os.path.isabs(canonical) else canonical
        except OSError:
            canonical_loc = canonical
        if canonical_loc in self._write_tier_source_paths:
            raise LakehouseError(
                _errors.enrich(
                    f"Write refused: target '{canonical}' resolves onto a configured source "
                    "table's location — write targets are always outside the covered source "
                    "tables. Choose a target under your scratch namespace."
                )
            )

    def _evict_result_cache_for_write(self, backend: str, canonical_path: str) -> None:
        """Drop result-cache entries (L1 + L2) referencing a written table.

        The write-tier complement of the snapshot-token invalidation: the
        result-cache key embeds ``<source>/<path>=<version>`` per referenced
        table, but a scratch CTAS drop-create RESETS the Delta log to
        version 0 (the token regresses, so the token comparison that catches
        normal ETL commits cannot see the change).

        A stored key is the FINAL sha256 HEX (the parts are hashed away), so
        path-matching stored keys is impossible; instead the key for a
        candidate SQL is RECOMPUTED with the written table's NEW snapshot
        token and checked for membership — a cached read of the written
        table (whose old entry carried the old token) has a DIFFERENT key
        only if the token changed, which is exactly the case the
        drop-create defeats. So the honest eviction is: recompute the keys
        of cached SQLs. The engine keeps a bounded key->sql side map
        (populated at store time; entries dropped with their keys), scans
        it for SQLs that REFERENCE the written table (the same
        word-boundary name match ``_referenced_tables`` uses), and evicts
        those keys from L1 and L2.

        The written table's logical path is derived from the canonical
        scratch path exactly as the provider lists it (the subject-slug
        segment is the schema): ``.../<root>/<slug>/<name>`` lists as
        ``<slug>/<name>``. Runs ONLY on the write path — a read-only
        deployment pays nothing.
        """
        parts = [p for p in canonical_path.rstrip("/").split("/") if p]
        table_path = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else canonical_path
        table_name = parts[-1] if parts else canonical_path
        # Every identifier the provider could list this table under: the
        # bare name (how the write addressed it), the listed path, and the
        # engine's QUALIFIED form (schema/name -> schema_name — DuckDB sees
        # the subject-slug segment as a schema, so reads resolve
        # ``alice/t`` as ``alice_t``).
        idents = {table_name, table_path}
        if len(parts) >= 2:
            idents.add(f"{parts[-2]}_{parts[-1]}")
        stale: list[str] = []
        with self._lock:
            for key, sql in list(self._result_cache_sql.items()):
                try:
                    # Same reference decision as _referenced_tables: a
                    # word-boundary name match on the SQL text.
                    if any(re.search(rf"\b{re.escape(i)}\b", sql) for i in idents):
                        stale.append(key)
                except Exception:
                    continue
            for k in stale:
                entry = self._result_cache.pop(k, None)
                self._result_cache_sql.pop(k, None)
                if entry is not None:
                    self._result_cache_bytes -= entry[1].nbytes
        if self._l2_cache is not None:
            try:
                self._l2_cache.drop_for_table(table_path, table_name)
            except Exception:
                logger.debug("L2 write-eviction failed for %s", table_path, exc_info=True)

    def _evict_dataset_cache_for(self, canonical_path: str) -> None:
        """Drop cached dataset handles for the written table (any version).

        The write tier's invalidation: a scratch write physically replaced
        (or appended under) a path some cached handle points at. Every
        matching entry is dropped — the next open re-reads the Delta log
        fresh. Cheap (a dict scan over the bounded cache) and best-effort.
        The match is on the cache KEY's table path (``<source>/<path>``):
        the canonical scratch path ends ``.../<slug>/<name>`` and the
        provider lists the written table as path ``<slug>/<name>``, so the
        suffix test identifies exactly the written table — without
        depending on whether the dataset lists full paths (pyarrow) or
        bare names (delta-rs, verified).
        """
        parts = [p for p in canonical_path.rstrip("/").split("/") if p]
        table_path = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else canonical_path
        with self._lock:
            for key in [k for k in self._dataset_cache]:
                if key[1] and key[1].endswith(table_path) and key[0] == "default":
                    del self._dataset_cache[key]
                    self._version_checked_at.pop(key, None)

    def _record_write(
        self,
        cls,
        backend: str,
        target: str,
        rows: int,
        duration_ms: float,
        state: str,
        error: str | None = None,
        caller=None,
    ) -> None:
        """The write outcome choke point: audit event:"write" + additive metric.

        Audit lines gain ``event:"write"`` with the target/backend/rows (the
        review's additive line shape — existing query lines byte-identical).
        The metric is ``sqlhandler_writes_total{backend,outcome}``.
        """
        try:
            observability.audit_write(
                sql=cls.statement,
                state=state,
                duration_ms=duration_ms,
                target=target,
                backend=backend,
                n_rows=rows if state == "ok" else None,
                error=error,
                caller=caller,
            )
        except Exception:
            logger.debug("write audit failed", exc_info=True)
        try:
            observability.metrics.record_write(backend, state)
        except Exception:
            logger.debug("write metric failed", exc_info=True)

    # ------------------------------------------------- fast path + results
    # Bare ``SELECT COUNT(*) FROM <table>`` — row counts live in the parquet
    # footers / Delta log / Iceberg manifest; no data scan needed. DuckDB
    # still enumerates row groups through an ARROW_SCAN (~125 ms on a 20M-row
    # table); the metadata read is ~0.1 ms, and the gap grows with size.
    _COUNT_STAR_RE = re.compile(
        r"^\s*SELECT\s+COUNT\(\*\)\s*(?:AS\s+([A-Za-z_][A-Za-z0-9_]*)\s*)?"
        r"FROM\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*;?\s*$",
        re.IGNORECASE,
    )

    # Bare-LIMIT preview detector (Task A). Same sqlglot-free philosophy as
    # _referenced_tables / _COUNT_STAR_RE: a deliberately NARROW regex plus
    # resolve-and-check — when in doubt, the normal query path. The regex
    # anchors the head hard (SELECT + a projection list that cannot contain
    # parens/semicolons) so FROM/JOIN/ON/WITH cannot hide inside the column
    # text; the reject-word list then refuses aggregates and clause keywords.
    _PREVIEW_RE = re.compile(
        r"^\s*SELECT\s+(?P<cols>[^;()]+?)\s+FROM\s+"
        r"(?P<table>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*"
        r"(?:;\s*)?LIMIT\s+(?P<limit>\d+)\s*;?\s*$",
        re.IGNORECASE,
    )
    # Words that never appear in a plain projection list: clause keywords
    # (WHERE/GROUP/ORDER/... — belt-and-braces beside the regex anchoring),
    # window constructs and aggregates. A bare `*` IS matched; only
    # decorated lists are refused.
    _PREVIEW_REJECT = re.compile(
        r"\b(?:WHERE|GROUP|HAVING|ORDER|WINDOW|UNION|EXCEPT|INTERSECT|VALUES|OFFSET|AS|"
        r"OVER|FILTER|SUM|COUNT|MIN|MAX|AVG|DISTINCT|CASE|CAST|COALESCE)\b",
        re.IGNORECASE,
    )

    def _is_bare_preview(self, sql: str) -> tuple[str, int] | None:
        """Match the bare-preview shape: ``(table, limit)`` — else None.

        Steps, in order (each refusal = the normal path, never an error):

        1. Regex shape — one SELECT, a projection list of plain identifiers
           (``*`` allowed alone), FROM one table, a bare trailing LIMIT.
        2. Column sanity — each comma-separated piece is a plain identifier
           (dotted parts allowed, no aliases/expressions/quotes); the
           reject-word list refuses aggregates and clause keywords.
        3. Table resolve — `_safe_table_name` + `_resolve` must find a REAL
           table (virtual / external-attach / raw-format fall through), the
           same word-boundary + qualified-name discipline the registration
           (`_register_schema`'s ``[source_]schema_name`` views) uses.
        """
        m = self._PREVIEW_RE.match(sql)
        if not m or self._sql_needs_external(sql):
            return None
        cols = m.group("cols").strip()
        if cols.lower() != "*" and self._PREVIEW_REJECT.search(cols):
            return None
        for piece in cols.split(","):
            piece = piece.strip()
            if piece == "*":
                continue
            if not piece or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", piece
            ):
                return None
        try:
            table = m.group("table")
            _safe_table_name(table)
            info = self._resolve(table)
        except Exception:
            return None
        if info.format in ("virtual", "external") or is_raw_format(info.format):
            return None
        return table, int(m.group("limit"))

    def _preview_fastpath(self, sql: str, version: int | None) -> pa.Table | None:
        """Serve a bare ``SELECT cols FROM t LIMIT n`` from the first rows.

        Reads the FIRST row group of the table's FIRST data file (Parquet:
        one footer + one row group; Delta: the current snapshot's first
        add-action file — the same enumeration skip that motivates the
        count(*) fast-path, minus the full-scan semantics) and slices to
        ``limit``.

        SEMANTICS (stated honestly): for LIMIT without ORDER BY, any subset
        of rows is a correct answer — the SQL contract fixes nothing about
        WHICH rows. The fast path returns the first physical rows, which is
        exactly that contract; the full path may hand back different rows
        because DuckDB's read order/batch boundaries and pyarrow's row-group
        selection are implementation details, not result guarantees. What
        the fast path DOES guarantee: same column set/types, same row cap
        and the same Arrow-table shape the normal path returns (rendering,
        caps and audit/metrics all live above and are identical).

        ANY snag (unresolvable fragment, a read error, a projected column
        the fragment doesn't carry) returns None — the normal query path
        takes over, never an error surfaces.

        Structural split: :meth:`_is_bare_preview` decides the SHAPE (pure,
        no IO), THIS method does the READ — tests and future callers can
        monkeypatch the executor alone and assert it was (not) called.
        """
        parsed = self._is_bare_preview(sql)
        if parsed is None:
            return None
        table, limit = parsed
        try:
            info = self._resolve(table)
            if limit <= 0:
                # LIMIT 0: no rows to read — shape-only answer, no IO.
                dset = self._open_dataset(info, version)
                return dset.schema.empty_table()
            dset = self._open_dataset(info, version)
            fragment = next(iter(dset.get_fragments()), None)
            if fragment is None:
                return None  # empty table (or unlisted backend): normal path
            cols = self._preview_columns(sql)
            # No explicit dataset schema is passed: parquet/delta fragments
            # carry their physical schema (partition fields fold in at scan
            # time) and iceberg/sharing datasets carry their own; passing an
            # explicit `columns=` list to head() would REJECT names the
            # fragment knows loudly instead of falling back — the plain
            # projection keeps any mismatch on the fallback path.
            rows = fragment.head(limit, columns=(cols or None))
            if rows.num_rows > limit:
                rows = rows.slice(0, limit)
            return rows
        except Exception:
            logger.debug("preview fast-path failed for %r; using the query path", sql, exc_info=True)
            return None

    def _preview_columns(self, sql: str) -> list[str]:
        """The preview's projected column names (``[]`` = star / all)."""
        cols = self._PREVIEW_RE.match(sql).group("cols").strip()  # type: ignore[union-attr]
        if cols.lower() == "*":
            return []
        # Dotted names: keep the last part (the column), as SQL does.
        return [c.strip().split(".")[-1] for c in cols.split(",")]

    def _metadata_count_fastpath(self, sql: str, version: int | None) -> pa.Table | None:
        """Serve a bare ``SELECT COUNT(*) FROM <table>`` from metadata.

        Returns the one-row result, or None whenever the query is anything
        other than an exact, unfiltered, single-provider-table count —
        WHERE/JOIN/GROUP/aliases aside, external attaches and virtual tables
        (whose count is the definition's business) all fall through to the
        normal query path, and any failure down here does too.
        """
        m = self._COUNT_STAR_RE.match(sql)
        if not m or self._sql_needs_external(sql):
            return None
        alias = m.group(1) or "count_star()"
        try:
            info = self._resolve(m.group(2))
        except Exception:
            return None
        if info.format == "virtual":
            return None
        if is_raw_format(info.format):
            # Raw-text tables (csv/tsv/json/ndjson) carry no row-count
            # metadata — no footers — so count_rows() here would SCAN the
            # whole table while pretending to be the metadata fast path.
            # Fall through to the query path (correct, just not free).
            return None
        try:
            dset = self._open_dataset(info, version)
            n = int(dset.count_rows())
        except Exception:
            logger.debug("count(*) fast-path failed for %s; using the query path", info.path, exc_info=True)
            return None
        return pa.table({alias: pa.array([n], type=pa.int64())})

    def _policy_hash(self, caller=None) -> str:
        """The caller's effective-policy hash for cache keys (Stage 2 live).

        Returns the sha256 of the caller's canonicalized effective rule set
        (``policy.canonical_hash`` over the group specs that resolve for the
        CURRENT caller), or ``""`` when enforcement is off / no caller is
        bound / the caller's groups resolve to no rules. Every cache (result
        L1/L2, virtual materialization, describe/profile) folds this value in
        through :func:`_cache_policy_part` — the conditional-slot rule.

        THE WIRING (identity spine Stage 1 + policy Stage 2): the caller
        rides :func:`current_caller` — set by the server per request (MCP
        tools/web UI) and passed EXPLICITLY into ``query_duckdb`` (keyword
        ``caller``) where a raw ``threading.Thread`` (``QueryJob``) would
        otherwise lose it (contextvars do not cross threads). With
        enforcement OFF this returns ``""`` everywhere: keys stay
        byte-identical, cross-caller sharing unchanged (the golden tests
        pin it).
        """
        if not policy_mod.policy_enabled():
            return ""
        pol = policy_mod.policy_store().get()
        if pol is None or not pol.groups:
            return ""
        effective = caller if caller is not None else current_caller()
        if effective is None:
            return ""
        return pol.effective_hash(getattr(effective, "subject", None), getattr(effective, "key_fp", None))

    @staticmethod
    def _cache_policy_part(policy_hash: str) -> str:
        """The ``policy=<hash>`` cache-key part, or "" when there is no policy.

        Kept next to the key builders so the conditional is visible in one
        place: a non-empty hash APPENDS the part; an empty hash contributes
        nothing at all (not a placeholder, not a separator) — the parts join
        byte-identically to today's.
        """
        return f"policy={policy_hash}" if policy_hash else ""

    def _result_cache_key(
        self,
        sql: str,
        params: object | None,
        limit: int | None,
        row_cap: int | None,
        version_as_of: int | None,
        caller=None,
    ) -> str | None:
        """Cache key for a query: full identity + base-snapshot version tokens.

        The SQL enters the key whitespace-normalized (outside quoted
        literals — see :func:`_normalize_cache_sql`), so formatting-only
        re-runs hit the same entry; quoted content and comment-bearing
        queries keep byte-exact keys.

        A trailing ``policy=<hash>`` part is appended ONLY when the caller's
        policy hash is non-empty (:meth:`_policy_hash` — empty everywhere in
        this slice), so masked results can never share an entry with
        unmasked ones; with no policy the key is byte-identical to the
        historical format.

        None when the result must not be cached: caching disabled, no
        recognizable tables, any virtual table involved (its materialization
        cache already accelerates it), or an attached-database query.
        """
        if self._result_cache_ttl <= 0:
            return None
        try:
            refs = self._referenced_tables(sql)
            if not refs or any(i.format == "virtual" for i in refs):
                return None
            if self._sql_needs_external(sql):
                return None
            parts = [
                _normalize_cache_sql(sql),
                repr(params),
                repr(limit),
                repr(row_cap),
                repr(version_as_of),
            ]
            for info in sorted(refs, key=lambda t: (t.source, t.path)):
                parts.append(f"{info.source}/{info.path}={self._safe_version(info)}")
            policy_part = self._cache_policy_part(self._policy_hash(caller))
            if policy_part:
                parts.append(policy_part)
            return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
        except Exception:
            return None

    def _result_cache_lookup(self, key: str) -> pa.Table | None:
        """The cached result for ``key``: memory L1 first, then the shared L2.

        An L1 hit is exactly today's path. On an L1 miss the shared-disk L2
        (all replicas pointing at one directory — a PVC in k8s) is consulted;
        an L2 hit warms L1, so the NEXT identical query on this replica is a
        memory hit again. Every L2 step is best-effort: a missing, corrupt
        or expired entry is an ordinary miss, and the L2 is disabled
        entirely unless SQLHANDLER_L2_DIR is set.
        """
        with self._lock:
            hit = self._result_cache.get(key)
            if hit is not None:
                ts, table = hit
                if time.time() - ts >= self._result_cache_ttl:
                    del self._result_cache[key]
                    self._result_cache_sql.pop(key, None)
                    self._result_cache_bytes -= table.nbytes
                else:
                    self._result_cache.move_to_end(key)
                    self._result_cache_hits += 1
                    return table
        if self._l2_cache is not None:
            table = self._l2_cache.lookup(key)
            if table is not None:
                self._result_cache_store(key, table)
                return table
        return None

    def _result_cache_store(self, key: str, table: pa.Table, sql: str | None = None) -> None:
        """Cache one query result: memory L1 always, shared L2 when in band.

        L2 gets only results in the byte band ``SQLHANDLER_L2_MIN_BYTES``
        (smaller results round-trip the PVC slower than recomputing them) ≤
        nbytes ≤ ``SQLHANDLER_L2_MAX_BYTES`` (a runaway result must not fill
        the shared volume; default mirrors the virtual cache's 2 GiB). The
        L1 cap logic is unchanged; L2 failures never raise.

        The key -> referenced-table-paths mapping is remembered (write tier):
        the stored key is the FINAL sha256 hex — the parts are hashed away —
        so a write's post-write eviction cannot path-match stored keys
        without this side map. Bounded with the cache itself (entries are
        dropped when their key is).
        """
        nbytes = table.nbytes
        if self._result_cache_max_bytes > 0 and nbytes > self._result_cache_max_bytes:
            return
        with self._lock:
            while self._result_cache and self._result_cache_bytes + nbytes > self._result_cache_max_bytes:
                _, evicted = self._result_cache.popitem(last=False)
                if self._result_cache_sql:
                    self._result_cache_sql.popitem(last=False)
                self._result_cache_bytes -= evicted[1].nbytes
            self._result_cache[key] = (time.time(), table)
            self._result_cache_bytes += nbytes
            self._result_cache_writes += 1
            if sql is not None:
                self._result_cache_sql[key] = sql
        if (
            self._l2_cache is not None
            and self._l2_min_bytes <= nbytes
            and (self._l2_max_bytes <= 0 or nbytes <= self._l2_max_bytes)
        ):
            self._l2_cache.store(key, table)

    def _referenced_tables(self, sql: str) -> list[TableInfo]:
        """Return the tables referenced by a SQL query.

        Matches table identifiers (bare or qualified) against the known
        tables, so we only open the datasets the query actually touches.

        Policy-aware: hidden tables resolve TOO (the caller-visible list
        excludes them, but a query naming one must reach the registration
        path — which serves the EMPTY relation; dropping the reference here
        would surface a catalog error naming a table that "doesn't exist",
        which is both uglier and an existence oracle for probe loops). The
        visibility contract lives in list_tables/describe/scan/query;
        registration handles hidden tables deliberately.
        """
        known = self.list_tables()
        if policy_mod.policy_enabled():
            # Merge back any table the caller's policy HIDES: the empty-
            # relation registration needs the TableInfo (schema) to build it.
            pol = policy_store().get()
            if pol.groups:
                effective_caller = current_caller()
                subject = getattr(effective_caller, "subject", None) if effective_caller is not None else None
                key_fp = getattr(effective_caller, "key_fp", None) if effective_caller is not None else None
                if effective_caller is not None:
                    groups = pol.groups_for(subject, key_fp)
                    hidden_known = {(t.source, t.path) for t in known}
                    for t in self._provider_tables() + self._virtual_infos(self._provider_tables()):
                        if (t.source, t.path) in hidden_known:
                            continue
                        if pol.rule_for_table(t.path, t.name, groups).hidden:
                            known = known + [t]
                            hidden_known.add((t.source, t.path))
        wanted: dict[tuple[str, str], TableInfo] = {}
        for info in known:
            for ident in (re.escape(info.name), re.escape(info.qualified_name)):
                if re.search(rf"\b{ident}\b", sql):
                    wanted[(info.source, info.path)] = info
                    break
        return list(wanted.values())

    def _register_schema(
        self, con, sql: str, version: int | None = None, materialize: bool = True, caller=None
    ) -> None:
        """Register each referenced table as a DuckDB view over its Dataset.

        Each table gets its qualified view (``[source_]schema_name``) always,
        and its bare-name view only when that name is globally unique - so
        same-named tables across federated sources (or schemas) can't silently
        shadow each other. Queries should prefer qualified names.

        Virtual tables (semantic-catalog ``definition`` entries) register as
        real DuckDB views built from their definition SQL, created AFTER the
        physical views so the definition's base tables resolve. Those base
        tables are usually not referenced by the user's SQL directly, so they
        are pulled in transitively from the definitions themselves (see
        ``_expand_query_tables``), and virtual views are created in
        dependency order so virtual-on-virtual definitions compose. DuckDB
        pushes predicates/projections through the views into the base scans.

        ``version`` (time travel) opens every versionable table at that
        historical snapshot instead of the current one (virtual views then
        read their base tables at that same snapshot for free).

        ``caller`` (keyword-friendly; identity spine): when policy
        enforcement covers a physical table, the MASKING VIEW is registered
        under the caller-visible names instead of the raw dataset — the
        base dataset goes under a private name (``__sqlhandler_base_<n>``)
        and the masking view (row filter + column masks, policy.py) reads
        it. Virtual definitions compose over these views, so masking is
        inherited TRANSITIVELY (the verified composition path). No caller /
        enforcement off / empty rule → register exactly as before
        (byte-identical behavior and materialization keys).
        """
        name_counts: dict[str, int] = {}
        for info in self.list_tables():
            name_counts[info.name] = name_counts.get(info.name, 0) + 1
        effective_caller = caller if caller is not None else current_caller()
        physical, virtuals = self._expand_query_tables(self._referenced_tables(sql))
        base_seq = 0
        for info in physical:
            views = {_safe_ident(info.qualified_name)}
            if name_counts.get(info.name, 0) <= 1:
                views.add(_safe_ident(info.name))
            try:
                dset = self._open_dataset(info, version)
            except Exception:
                if version is not None:
                    # A time-travel query must fail loudly: silently skipping
                    # the table would produce a misleading "table not found"
                    # instead of the real reason (e.g. plain Parquet has no
                    # version history).
                    raise
                logger.debug("Could not open dataset for %s", info.path)
                continue
            rule = self._effective_rule(info, effective_caller)
            if rule.hidden:
                # The table does not exist for this caller. Registering
                # nothing would surface a confusing catalog error; registering
                # an EMPTY relation keeps queries resolvable but yields zero
                # rows and no columns... actually: an empty-typed relation
                # from the real schema, so SELECT * still binds and returns
                # nothing. The visibility layer (list/describe/search) hides
                # it entirely; queries that name it explicitly get an empty
                # result — information-theoretically the same as a table with
                # a row_filter of FALSE, never an error message that leaks
                # the table's existence to a tool that already knows the name.
                try:
                    empty = self._empty_table_for(dset)
                    for view in views:
                        con.register(view, empty)
                except Exception:
                    logger.debug("hidden-table empty registration failed for %s", info.path, exc_info=True)
                continue
            if rule.empty:
                for view in views:
                    try:
                        con.register(view, dset)
                    except Exception:
                        logger.debug("Could not register view %s from %s", view, info.path)
                continue
            # POLICY-COVERED: raw dataset under a private base name, masking
            # view under every caller-visible name.
            base_seq += 1
            base_name = f"__sqlhandler_base_{base_seq}"
            try:
                con.register(base_name, dset)
                columns = [f.name for f in dset.schema]
                mask_sql = policy_mod.build_mask_select(base_name, columns, rule.column_masks, rule.row_filter)
                for view in views:
                    con.execute(f"CREATE OR REPLACE VIEW {view} AS ({mask_sql})")
                logger.debug(
                    "policy view registered for %s (filter=%s, masks=%s)",
                    info.path,
                    bool(rule.row_filter),
                    sorted(rule.column_masks),
                )
            except Exception as exc:
                # Fail CLOSED: a masking view that cannot be built must never
                # degrade to the raw dataset (that would be a silent leak).
                raise LakehouseError(f"policy masking for table '{info.name}' could not be applied: {exc}") from exc
        if not virtuals:
            return
        self._apply_compat_macros(con)
        for info in virtuals:
            self._register_virtual(con, info, version, materialize, name_counts, caller=effective_caller)

    def _empty_table_for(self, dset) -> pa.Table:
        """A zero-row arrow table with the dataset's REAL schema (hidden-table
        registration: the shape is visible, the data is not — and only to a
        caller who already names the hidden table explicitly)."""
        schema = dset.schema
        return pa.table([pa.array([], type=f.type) for f in schema], schema=schema)

    def _effective_rule(self, info: TableInfo, caller=None, _validating: bool = False) -> TableRule:
        """The policy rule for one table under ``caller`` (or the ambient
        context's). Enforcement OFF / no policy file / no groups → the empty
        rule (byte-identical behavior). A HIDDEN rule wins over everything.

        Fail-closed on rule resolution errors: an exception inside policy
        lookup refuses the scan path with LakehouseError rather than serving
        the raw table (a resolution bug must never become a leak).

        ``_validating`` breaks the describe→rule→describe recursion (the
        first-use filter validation reads the SCHEMA, not the policy shape).
        """
        if not policy_mod.policy_enabled():
            return TableRule()
        try:
            pol = policy_store().get()
            if not pol.groups:
                return TableRule()
            if caller is None:
                caller = current_caller()
            if caller is None:
                # NO caller context (engine internals/tests/pre-middleware):
                # the empty rule. A real HTTP request always carries a Caller
                # (anonymous at worst — which DOES get the default group);
                # None is the internal trusted path, never a policy subject.
                return TableRule()
            subject = getattr(caller, "subject", None)
            key_fp = getattr(caller, "key_fp", None)
            groups = pol.groups_for(subject, key_fp)
            if not groups:
                # No group resolves (no default_group configured): the caller
                # is unrestricted BY THE FILE — but only when the file
                # deliberately has no default. groups_for already encodes it.
                return TableRule()
            rule = pol.rule_for_table(info.path, info.name, groups)
            if rule.row_filter and not self._validated_filters.get(rule.row_filter) and not _validating:
                # Validate the filter against THIS table's real columns at
                # first use (load-time validation covers known tables; this
                # catches tables described after the policy loaded). The
                # schema read here must NOT re-enter rule resolution — the
                # filter needs the RAW schema, which describe computes from
                # the dataset directly; call the resolution-free path.
                columns = self._raw_columns(info)
                if columns:
                    policy_mod.validate_row_filter(rule.row_filter, columns, info.path)
                self._validated_filters[rule.row_filter] = True
            return rule
        except policy_mod.PolicyError as exc:
            raise LakehouseError(f"policy for '{info.name}' is invalid: {exc}") from exc
        except LakehouseError:
            raise
        except Exception as exc:
            raise LakehouseError(f"policy enforcement failed for '{info.name}': {exc}") from exc

    def _raw_columns(self, info: TableInfo) -> list[str]:
        """The table's raw column names straight from the dataset (NO policy
        resolution — the recursion-free schema read for filter validation)."""
        try:
            dset = self._open_dataset(info)
            return [f.name for f in dset.schema]
        except Exception:
            return []

    def _register_virtual(
        self,
        con,
        info: TableInfo,
        version: int | None,
        materialize: bool,
        name_counts: dict[str, int],
        caller=None,
    ) -> None:
        """Put virtual table ``info`` on the connection: from the
        materialization cache when valid, otherwise as a view built from the
        definition — populating the cache for the next query when allowed.

        On a cache miss with materialization allowed, the definition runs
        once here and its full result is published to parquet; the paying
        query then runs against the materialized copy, so it pays the
        definition cost exactly once and every later query (any filter, any
        LIMIT) reads a small parquet. Failures degrade to the live view —
        the cache is only an accelerator, never a dependency. Time-travel
        queries bypass the cache entirely (their base snapshots differ).

        Policy (identity spine): the definition composes over the REGISTERED
        views, so masking on its base tables is inherited transitively — the
        same definition text serves masked and unmasked callers and the
        materialization is keyed per policy hash (``_virtual_cache_base``
        folds the caller's hash in when non-empty, producing SEPARATE
        artifacts; unmasked callers keep today's exact filenames).
        """
        views = [_safe_ident(info.qualified_name)]
        if name_counts.get(info.name, 0) <= 1:
            views.append(_safe_ident(info.name))
        definition = self._definition_sql(info.name)
        dset = None
        if self._virtual_cache_ttl > 0 and version is None:
            dset = self._virtual_cache_lookup(info, definition, caller=caller)
            if dset is None and materialize:
                dset = self._virtual_cache_materialize(con, info, definition, caller=caller)
        if dset is not None:
            try:
                for view in views:
                    con.register(view, dset)
                with self._lock:
                    self._virtual_cache_hits += 1
                return
            except Exception:
                logger.warning(
                    "virtual table %s: cached dataset unusable; building the live definition",
                    info.name,
                    exc_info=True,
                )
        for view in views:
            try:
                # OR REPLACE: for schema-less tables the bare and qualified
                # names coincide — same SQL body, one view either way.
                con.execute(f"CREATE OR REPLACE VIEW {view} AS ({definition})")
            except Exception as exc:
                # The definition parsed at catalog load, so a failure here
                # is a binding problem (missing base table, type clash):
                # fail the query with the real reason instead of letting a
                # bare "table not found" mislead the caller.
                raise LakehouseError(
                    f"virtual table '{info.name}' could not be built from its catalog definition: {exc}"
                ) from exc

    def _virtual_cache_base(self, info: TableInfo, definition: str, caller=None) -> dict:
        """Identity of a virtual table's cache entry.

        The key covers the definition text (rewritten) of the table AND of
        every virtual table it transitively builds on (a nested definition
        change must invalidate its dependents), plus the resolved base
        tables' snapshot-version tokens, so a new ETL commit on any base
        invalidates automatically. Returns the parquet path, the meta sidecar
        path, the key hash, and the current expected version tokens.

        Policy (Stage 2): the CALLER's policy hash rides the digest walk
        (after the definition texts, before the version tokens) when
        non-empty — masked callers materialize to ``…-p<hash8>-<digest>.parquet``
        and unmasked callers keep today's exact ``…-<digest>.parquet`` names
        (the conditional infix rule; review §5).
        """
        physical, virtuals = self._expand_query_tables([info])
        h = hashlib.sha256()
        for v in virtuals:  # topologically ordered -> deterministic
            h.update(v.name.encode())
            h.update(b"\0")
            h.update(self._definition_sql(v.name).encode())
            h.update(b"\0")
        # The caller's effective policy hash — the transitive definitions
        # compose over masked base views, so the MATERIALIZED RESULT differs
        # per policy and must not share an artifact across policies.
        policy_hash = self._caller_policy_hash(caller)
        if policy_hash:
            h.update(b"policy\0")
            h.update(policy_hash.encode())
            h.update(b"\0")
        versions = {
            f"{i.source}/{i.path}": str(self._safe_version(i))
            for i in sorted(physical, key=lambda t: (t.source, t.path))
        }
        digest = h.hexdigest()
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", info.name)
        if policy_hash:
            # Masked materialization: policy infix separates the artifacts
            # (review §5: `…-p<hash8>-<digest16>.parquet`).
            path = Path(self._virtual_cache_dir) / f"{safe}-p{policy_hash[:8]}-{digest[:16]}.parquet"
        else:
            path = Path(self._virtual_cache_dir) / f"{safe}-{digest[:16]}.parquet"
        return {
            "path": str(path),
            "meta": str(path) + ".json",
            "sha256": digest,
            "versions": versions,
        }

    def _caller_policy_hash(self, caller=None) -> str:
        """The caller's effective policy hash (empty = no policy for them).

        One resolution point for the virtual-cache walk (``_virtual_cache_base``)
        and any other place that needs the hash WITHOUT going through
        ``_policy_hash()`` (which reads the ambient context). Explicit caller
        wins, then the ambient context, then empty.
        """
        if not policy_mod.policy_enabled():
            return ""
        pol = policy_store().get()
        if pol is None or not pol.groups:
            return ""
        effective = caller if caller is not None else current_caller()
        if effective is None:
            return ""
        subject = getattr(effective, "subject", None)
        key_fp = getattr(effective, "key_fp", None)
        if not pol.groups_for(subject, key_fp):
            return ""
        return pol.effective_hash(subject, key_fp)

    def _virtual_cache_lookup(self, info: TableInfo, definition: str, caller=None):
        """A pyarrow Dataset over the cached materialization when it is valid
        for this definition + base snapshots + TTL (+ this caller's policy),
        else None (never raises)."""
        import pyarrow.dataset as pad

        base = self._virtual_cache_base(info, definition, caller=caller)
        try:
            meta = json.loads(Path(base["meta"]).read_text(encoding="utf-8"))
        except Exception:
            return None
        if meta.get("sha256") != base["sha256"] or meta.get("versions") != base["versions"]:
            return None
        if time.time() - float(meta.get("created", 0)) >= self._virtual_cache_ttl:
            return None
        try:
            return pad.dataset(base["path"], format="parquet")
        except Exception:
            logger.warning("virtual table %s: cache file unreadable; ignoring it", info.name, exc_info=True)
            return None

    def _virtual_cache_materialize(self, con, info: TableInfo, definition: str, caller=None):
        """Run the definition once and publish its full result to the cache.

        The result is CLUSTERED before writing (see ``_cluster_result``):
        sorted by its most discriminative columns so the parquet row-group
        min/max statistics become selective and filters on those columns
        skip whole row groups on every later read. Returns the pyarrow
        Dataset over the published parquet (registered like any physical
        table for the rest of this query), or None on any failure — the
        query then falls back to building the live view. Writes go to a
        unique temp file + atomic rename, so concurrent queries may
        materialize simultaneously without corrupting anything (last writer
        wins; both results are valid).

        ``caller`` rides into ``_virtual_cache_base`` so a masked caller's
        materialization lands on its OWN artifact (policy infix) — never
        into (or out of) an unmasked entry.
        """
        import pyarrow.dataset as pad
        import pyarrow.parquet as pq

        base = self._virtual_cache_base(info, definition, caller=caller)
        path = Path(base["path"])
        tmp_path: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            table = con.sql(definition).arrow()
            if isinstance(table, pa.RecordBatchReader):
                table = table.read_all()
            if self._virtual_cache_max_bytes > 0 and table.nbytes > self._virtual_cache_max_bytes:
                logger.info(
                    "virtual table %s: result too large to cache (%d bytes > %d); serving live",
                    info.name,
                    table.nbytes,
                    self._virtual_cache_max_bytes,
                )
                return None
            table = self._cluster_result(con, info, table)
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
            os.close(fd)
            pq.write_table(table, tmp_path, compression="zstd")
            os.replace(tmp_path, path)
            tmp_path = None
            meta = {
                "table": info.name,
                "sha256": base["sha256"],
                "versions": base["versions"],
                "created": time.time(),
                "rows": table.num_rows,
                "bytes": table.nbytes,
            }
            fd, tmp_meta = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".meta.tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(meta))
            os.replace(tmp_meta, base["meta"])
            with self._lock:
                self._virtual_cache_writes += 1
            logger.info("virtual table %s materialized: %d rows -> %s", info.name, table.num_rows, path)
            return pad.dataset(str(path), format="parquet")
        except Exception:
            logger.warning(
                "virtual table %s: materialization failed; serving the live definition",
                info.name,
                exc_info=True,
            )
            return None
        finally:
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    def _cluster_result(self, con, info: TableInfo, table: pa.Table) -> pa.Table:
        """Sort a materialized virtual result by its most discriminative columns.

        Parquet row groups carry per-column min/max statistics; on randomly
        ordered data every row group spans the full value range, so filters
        must read everything. Sorting by the lowest-cardinality columns
        (dimension-like first — the ones most likely used as equality
        filters) makes those statistics selective: whole row groups drop out
        of every later filtered read. This is Snowflake-style clustering
        applied at materialization time. Best-effort: any failure returns
        the table unsorted. Disabled with SQLHANDLER_VIRTUAL_CACHE_SORT=0.
        """
        if not self._virtual_cache_sort or table.num_rows < 1024 or table.num_columns < 2:
            return table
        try:
            probe = "_sqlhandler_sort_probe"
            con.register(probe, table)
            cols = table.column_names[:16]
            stats = con.sql(
                "SELECT "
                + ", ".join(f"approx_count_distinct({_safe_ident(c)}) AS k{i}" for i, c in enumerate(cols))
                + f" FROM {probe}"
            ).fetchone()
            ranked = sorted(zip(cols, stats), key=lambda kv: kv[1])
            keys = [c for c, distinct in ranked if distinct > 1][:3]
            if not keys:
                return table  # every column is constant — nothing to cluster on
            clustered = table.sort_by([(k, "ascending") for k in keys])
            logger.info(
                "virtual table %s clustered by %s (row-group pruning)",
                info.name,
                ", ".join(keys),
            )
            return clustered
        except Exception:
            logger.debug("clustering skipped for %s", info.name, exc_info=True)
            return table

    @staticmethod
    def _apply_compat_macros(con) -> None:
        """Define scalar helpers DuckDB lacks but Snowflake-flavored virtual
        definitions use, so definitions port with minimal edits.

        Per-connection (ephemeral) and best-effort: a name DuckDB already
        provides always wins. Currently: ``iff(c, t, e)`` — Snowflake's
        ternary, exactly ``CASE WHEN c THEN t ELSE e END`` (NULL condition →
        ELSE branch, matching Snowflake). ``ARRAY_CONSTRUCT_COMPACT`` cannot
        be shimmed this way (DuckDB macros don't overload by arity) — it is
        rewritten textually instead (see ``_rewrite_snowflake_constructs``).
        """
        try:
            con.execute("CREATE MACRO iff(c, t, e) AS CASE WHEN c THEN t ELSE e END")
        except Exception:
            pass  # already defined this connection, or macro support absent

    # Snowflake constructs rewritten textually at registration time. Matched
    # case-insensitively on word boundaries, string-literal-aware; arguments
    # may contain nested calls with their own commas/parens.
    _COMPACT_RE = re.compile(r"\barray_construct_compact\s*\(", re.IGNORECASE)

    def _definition_sql(self, name: str) -> str:
        """Executable SQL for virtual table ``name``: the catalog definition
        with Snowflake-only constructs rewritten to DuckDB (memoized per
        distinct text — see ``_rewrite_snowflake_constructs``)."""
        definition = self._virtual_entries()[name]["definition"]
        if "array_construct_compact" not in definition.lower():
            return definition
        with self._lock:
            rewritten = self._definition_rewrites.get(definition)
        if rewritten is None:
            rewritten = self._rewrite_snowflake_constructs(definition)
            with self._lock:
                if len(self._definition_rewrites) > 128:
                    self._definition_rewrites.clear()
                self._definition_rewrites[definition] = rewritten
        return rewritten

    @classmethod
    def _rewrite_snowflake_constructs(cls, sql: str) -> str:
        """Rewrite ``ARRAY_CONSTRUCT_COMPACT(...)`` calls to DuckDB.

        Becomes ``list_filter([...], __compact_val -> __compact_val IS NOT
        NULL)`` — same semantics (elements kept, NULLs dropped). The
        argument list is split on top-level commas only (paren- and
        quote-aware), and an unbalanced call or a token inside a string
        literal is copied through untouched so the SQL parser reports the
        real problem. DuckDB list literals need one common element type —
        CAST heterogeneous args.
        """
        out: list[str] = []
        i, n = 0, len(sql)
        while i < n:
            match = cls._COMPACT_RE.search(sql, i)
            if not match:
                out.append(sql[i:])
                break
            start = match.start()
            prefix = sql[i:start]
            if cls._in_open_string(prefix):
                # token appears inside a string literal — copy verbatim and
                # resume scanning after the first quote of that literal
                out.append(prefix + sql[start])
                i = start + 1
                continue
            open_paren = match.end() - 1
            close = cls._matching_paren(sql, open_paren)
            if close is None:
                out.append(prefix)
                i = start + 1  # unbalanced: let the SQL parser report it
                continue
            args = cls._split_top_level(sql[open_paren + 1 : close])
            items = ", ".join(a.strip() for a in args if a.strip())
            out.append(prefix)
            out.append(f"list_filter([{items}], __compact_val -> __compact_val IS NOT NULL)")
            i = close + 1
        return "".join(out)

    @staticmethod
    def _in_open_string(sql: str) -> bool:
        """True when ``sql`` ends inside an unclosed single-quoted string."""
        i, n = 0, len(sql)
        while i < n:
            if sql[i] == "'":
                i += 1
                closed = False
                while i < n:
                    if sql[i] == "'":
                        if i + 1 < n and sql[i + 1] == "'":
                            i += 2  # escaped quote ('')
                            continue
                        closed = True
                        i += 1
                        break
                    i += 1
                if not closed:
                    return True
            else:
                i += 1
        return False

    @staticmethod
    def _matching_paren(sql: str, open_at: int) -> int | None:
        """Index of the ')' matching the '(' at ``open_at`` (quote-aware)."""
        depth = 0
        i, n = open_at, len(sql)
        while i < n:
            ch = sql[i]
            if ch == "'":
                i += 1
                while i < n:
                    if sql[i] == "'":
                        if i + 1 < n and sql[i + 1] == "'":
                            i += 2  # escaped quote ('')
                            continue
                        break
                    i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    @staticmethod
    def _split_top_level(s: str) -> list[str]:
        """Split on commas that sit outside parens and string literals."""
        parts: list[str] = []
        depth = 0
        start = 0
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == "'":
                i += 1
                while i < n:
                    if s[i] == "'":
                        if i + 1 < n and s[i + 1] == "'":
                            i += 2  # escaped quote ('')
                            continue
                        break
                    i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(s[start:i])
                start = i + 1
            i += 1
        parts.append(s[start:])
        return parts

    @staticmethod
    def _definition_table_names(definition: str, by_ident: dict[str, TableInfo]) -> list[str]:
        """Known table identifiers (bare or qualified) a definition references.

        Same cheap identifier matching as ``_referenced_tables`` — a
        definition is operator-authored SQL over the lake's real table names.
        """
        return [ident for ident in by_ident if re.search(rf"\b{re.escape(ident)}\b", definition)]

    def _expand_query_tables(self, seed: list[TableInfo]) -> tuple[list[TableInfo], list[TableInfo]]:
        """Resolve a query's table set into (physical, ordered virtual).

        A virtual table referenced by the SQL — or by another virtual table's
        definition — pulls in every base table its definition names, so the
        registration order is: physical datasets first, then virtual views
        dependencies-first. A definition cycle is an operator error and
        raises (the query fails with a clear message instead of hanging).
        """
        virtual_entries = self._virtual_entries()
        by_ident: dict[str, TableInfo] = {}
        for info in self.list_tables():
            by_ident.setdefault(info.name, info)
            by_ident.setdefault(info.qualified_name, info)
        physical: dict[tuple[str, str], TableInfo] = {}
        virtuals: dict[str, TableInfo] = {}
        seen: set[tuple[str, str]] = set()
        stack = list(seed)
        while stack:
            info = stack.pop()
            key = (info.source, info.path)
            if key in seen:
                continue
            seen.add(key)
            if info.format == "virtual":
                virtuals[info.name] = info
                for ref in self._definition_table_names(virtual_entries[info.name]["definition"], by_ident):
                    dep = by_ident.get(ref)
                    if dep is not None:
                        stack.append(dep)
            else:
                physical[key] = info
        order: list[TableInfo] = []
        state: dict[str, int] = {}  # 0/absent = unvisited, 1 = visiting, 2 = done

        def visit(name: str) -> None:
            mark = state.get(name, 0)
            if mark == 1:
                raise LakehouseError(f"virtual tables form a definition cycle at '{name}'")
            if mark == 2:
                return
            state[name] = 1
            for ref in self._definition_table_names(virtual_entries[name]["definition"], by_ident):
                if ref != name and ref in virtuals:
                    visit(ref)
            state[name] = 2
            order.append(virtuals[name])

        for name in virtuals:
            visit(name)
        return list(physical.values()), order

    # -------------------------------------------------------- explain_query
    # Operator extra_info text whose presence means "this scan is filtered
    # in place" — the predicate was pushed INTO the scan rather than applied
    # above it. Evaluated per scan node from its own extra_info
    # ("Filters: amt>2.0"), which the JSON EXPLAIN exposes verbatim.
    _PUSHDOWN_HINT_RE = re.compile(r"Filters", re.IGNORECASE)

    @staticmethod
    def _explain_scan_pushdown(node: dict) -> bool | None:
        """Whether ONE scan node shows pushed-down filters, or None when unknown.

        DuckDB prints the pushed-down predicates on the scan operator itself
        (``Filters: amt>2.0`` in the scan's extra_info) — the whole point of
        the view-registered pyarrow path. A scan with no ``Filters`` line is
        NOT evidence of no pushdown (the query may genuinely have no
        predicate over that table), so the verdict is reported per scan and
        aggregated honestly: None = nothing observable either way.
        """
        extra = node.get("extra_info") or {}
        return bool(extra.get("Filters")) if extra else None

    @staticmethod
    def _explain_plan_summary(tree: list[dict]) -> dict:
        """Reduce one JSON EXPLAIN tree to {operators, scans, pushdown, est_rows}.

        ``scan_nodes`` counts leaf scan operators (ARROW_SCAN / PARQUET_SCAN
        / SQLITE_SCAN / ... — anything whose name ends in SCAN);
        ``pushdown`` is True when at least one scan carries a Filters entry,
        None when no scan exposes one (nothing observable — the query may
        simply have no predicate). ``estimated_root_rows`` is the root's
        Estimated Cardinality when it parses as a number (DuckDB's own
        estimate, confidence "approx" by definition).
        """
        operators = 0
        scans = 0
        pushdown: bool | None = None
        est_rows: float | None = None
        found_root = False

        def _walk(node: dict) -> None:
            nonlocal operators, scans, pushdown, est_rows, found_root
            operators += 1
            if not found_root:
                extra = node.get("extra_info") or {}
                raw = str(extra.get("Estimated Cardinality", "")).replace(",", "").replace("~", "").strip()
                if raw:
                    try:
                        est_rows = float(raw)
                        found_root = True
                    except ValueError:
                        pass
            name = str(node.get("name", "")).upper()
            if name.endswith("SCAN"):
                scans += 1
                if pushdown is not True and SqlEngine._explain_scan_pushdown(node) is True:
                    pushdown = True
            for child in node.get("children") or []:
                _walk(child)

        for root in tree:
            _walk(root)
        return {
            "operators": operators,
            "scan_nodes": scans,
            "pushdown": pushdown,
            "estimated_root_rows": est_rows,
        }

    def _table_bytes_to_scan(self, info: TableInfo, dset, source: str) -> tuple[int | None, str]:
        """Best-effort bytes-to-scan for one table, with its confidence label.

        Backends in priority order (every failure degrades — the estimate is
        the point, never a dependency):

        * **Delta** (onelake / Delta-on-S3 / nfs) — the snapshot's add
          actions already carry every data file's size in the delta log (the
          same seed the block cache uses, ``onelake._delta_file_sizes``):
          exact on-disk bytes, confidence ``"exact"``.
        * **Iceberg** — ``plan_files`` reports per-file sizes from the
          manifests (metadata only); confidence ``"exact"``.
        * **Parquet over any provider** — the fragment metadata's
          uncompressed row-group byte totals (one metadata read per file, no
          column IO); confidence ``"approx"`` — compression makes on-disk
          bytes smaller, typically several-fold.
        * Anything else (or any failure) — None with confidence ``"none"``.
        """
        if source == "external":
            return None, "none"
        try:
            if info.format == "delta":
                dt = self._delta_handle(info)
                if dt is None:
                    return None, "none"
                sizes = self._delta_file_sizes(dt)
                if not sizes:
                    return None, "none"
                return sum(int(v) for v in sizes.values()), "exact"
            if info.format == "iceberg":
                file_sizes = self._iceberg_file_sizes(info)
                if not file_sizes:
                    return None, "none"
                return sum(file_sizes), "exact"
            total = 0
            for frag in dset.get_fragments():
                for rg in frag.row_groups:
                    total += int(rg.total_byte_size)
            return (total, "approx") if total else (None, "none")
        except Exception:
            return None, "none"

    def _delta_handle(self, info: TableInfo, version: int | None = None):
        """The provider's DeltaTable handle when it exposes one (else None).

        Providers open Delta handles internally (``_open_delta``); asking
        for one generically would couple the engine to every backend, so a
        bounded ``getattr`` probe REUSES the provider's existing opener (and
        therefore its auth plumbing) instead of duplicating it. Providers
        without delta handles — or which fail to open — answer None and the
        size estimate degrades honestly.
        """
        opener = getattr(self.provider, "_open_delta", None)
        if not callable(opener):
            return None
        try:
            return opener(info, version) if version is not None else opener(info)
        except Exception:
            return None

    @staticmethod
    def _delta_file_sizes(dt) -> dict[str, int] | None:
        """Relative data-file path -> size, from the snapshot's add actions.

        Read here from the ALREADY-OPEN handle (no second metadata round
        trip): the same shape the block cache seeds from (the providers'
        ``_delta_file_sizes``), duplicated as a static helper because the
        engine must not import from the provider modules (they import from
        ``provider.py``; the reverse edge would be a cycle).
        """
        try:
            adds = dt.get_add_actions(flatten=True)
            table = adds if isinstance(adds, pa.Table) else pa.table(adds)
            return dict(zip(table.column("path").to_pylist(), table.column("size_bytes").to_pylist()))
        except Exception:
            return None

    def _iceberg_file_sizes(self, info: TableInfo) -> list[int] | None:
        """Data-file byte sizes for an Iceberg table, from its manifests.

        Mirrors IcebergProvider.open_dataset's plan (catalog load + scan
        planning — metadata only, no data files read); None on any failure,
        including the optional pyiceberg dependency being absent.
        """
        loader = getattr(self.provider, "_load_table", None)
        if not callable(loader):
            return None
        try:
            table = loader(info)
            return [int(t.file.file_size_in_bytes) for t in table.scan().plan_files()]
        except Exception:
            return None

    def _warm_cold_state(
        self, sql: str, params: object, limit: int | None, row_cap: int | None, version, caller=None
    ) -> dict:
        """Where the query's result would come from right now (no execution).

        ``l1`` = the in-memory result cache holds this exact query identity;
        ``l2`` = the shared-disk layer does (when configured — the probe
        reads one JSON sidecar, metadata, never the result). The cache-key
        identity is EXACTLY ``_result_cache_key``'s, so a reported-warm
        result would be served by ``query_duckdb`` itself. When the key
        can't be computed (virtual table, attached catalog, caching off) the
        band reports a plain false — for virtual tables the virtual
        materialization cache is the warm path and is out of this band's
        scope (named as such in the tool's output).
        """
        state = {"l1": False, "l2": False, "block_cache": None}
        key = None
        if self._result_cache_ttl > 0:
            try:
                key = self._result_cache_key(sql, params, limit, row_cap, version, caller=caller)
            except Exception:
                key = None
        if key is not None:
            with self._lock:
                hit = self._result_cache.get(key)
                if hit is not None and time.time() - hit[0] < self._result_cache_ttl:
                    state["l1"] = True
            if not state["l1"] and self._l2_cache is not None:
                try:
                    state["l2"] = self._l2_cache.has_entry(key)
                except Exception:
                    state["l2"] = False
        state["block_cache"] = self._block_cache_warm()
        return state

    @staticmethod
    def _block_cache_warm() -> bool | None:
        """Block-cache warm state for this query (None = the cache is off).

        ``None`` when the disk block cache is not enabled (honest N/A — most
        deployments). With the cache ON the answer is the mixed/unknown
        False: per-file block presence needs the scan node → file mapping
        (the JSON plan's scan nodes carry no file lists in this DuckDB
        version), and a claimed-warm signal that could be wrong is worse
        than an honest "cache on, warmth unknown". The one provable warm
        case — the OneLake Delta data-file handler seeded from the delta
        log — is per-table and is reported in ``bytes_confidence``
        (``"exact"`` = the same log the block cache seeds from).
        """
        from .blockcache import block_cache_enabled

        return False if block_cache_enabled() else None

    def explain_query(
        self,
        sql: str,
        params: object | None = None,
        version_as_of: int | None = None,
        include_plan: bool = False,
        *,
        caller=None,
    ) -> dict:
        """Cost estimate for one read-only query WITHOUT running it.

        The explain_query agent tool's engine method (agent productivity
        pack; implementation review §2c). Composed of existing pieces, none
        of which executes the query's data path:

        * **Parse** — sqlguard's ``extract_statement_spans`` through
          ``assert_attached_readonly``, with the ``_explain_inner_sql``
          unwrap, so EXPLAIN-of-SELECT arrives normalized and anything else
          (INSERT, multi-statement, EXPLAIN-of-a-write) is refused by the
          SAME parser rule run_sql applies (this tool is an estimator, not a
          new guard to dodge).
        * **Table refs** — ``_referenced_tables`` (the same cheap identifier
          matching the cache keys use).
        * **Row counts** — dataset metadata (``count_rows()``, the exact
          number the count(*) fast-path serves for free).
        * **Bytes-to-scan** — Delta add-action file sizes (the block cache's
          seed), Iceberg manifest file sizes, or Parquet row-group
          uncompressed totals; each with its own confidence label.
        * **Plan tree** — ``EXPLAIN (FORMAT JSON)`` on a fresh locked-down
          connection after ``_register_schema``; DuckDB plans and binds
          only (no row is read), so virtual-table definitions are safe to
          bind. Computed only when ``include_plan`` is set — the review's
          first slice is rows + bytes + warm/cold; the tree is the
          enricher, and its absence must never fail the tool.
        * **Warm/cold** — the exact result-cache key identity, probed
          against L1 and L2 (see :meth:`_warm_cold_state`).

        Every number carries a confidence label (``exact`` | ``approx`` |
        ``none``), and per-table estimates carry the snapshot-version token
        they were keyed to, so an agent can see when an estimate describes
        a different ETL snapshot than the one a fresh ``run_sql`` would
        read.

        Attached-database queries have no lake metadata at all: every count
        reports confidence ``"none"`` (degrade honestly — the review's
        verified gotcha), the plan is skipped (their catalogs attach on
        execution connections only), and the rest of the shape stands.
        """
        sql = assert_attached_readonly(sql)
        touches_external = bool(self.attaches) and bool(sql_references_attach(sql, self.attaches))
        tables: list[dict] = []
        if not touches_external:
            for info in self._referenced_tables(sql):
                entry: dict = {
                    "table": info.qualified_name,
                    "path": info.path,
                    "source": info.source,
                    "format": info.format,
                    "rows": None,
                    "rows_confidence": "none",
                    "bytes_to_scan": None,
                    "bytes_confidence": "none",
                    "snapshot_version": None,
                }
                if info.format == "virtual":
                    entry["virtual"] = True
                else:
                    try:
                        dset = self._open_dataset(info)
                        n = self._metadata_row_count(dset)
                        if n is not None:
                            entry["rows"] = n
                            entry["rows_confidence"] = "exact"
                        nbytes, nbytes_conf = self._table_bytes_to_scan(info, dset, info.source)
                        entry["bytes_to_scan"] = nbytes
                        entry["bytes_confidence"] = nbytes_conf
                        entry["snapshot_version"] = self._safe_version(info)
                    except Exception:
                        pass  # unreachable source: the ref is still listed, unnumbered
                tables.append(entry)

        inner = _explain_inner_sql(sql)
        result: dict = {
            "sql": sql,
            "read_only": True,
            "touches_external": touches_external,
            "tables": tables,
            "n_tables": len(tables),
            # Param validation is free and fails loudly here rather than
            # surprising the follow-up run_sql call.
            "params_ok": True,
        }
        try:
            _validate_params(params)
        except Exception as exc:
            result["params_ok"] = False
            result["params_error"] = str(exc)

        effective_caller = caller if caller is not None else current_caller()
        result["warm_cold"] = self._warm_cold_state(inner, params, None, None, version_as_of, caller=effective_caller)

        if include_plan and not touches_external:
            result["plan"] = self._explain_plan(inner, params, version_as_of, caller=effective_caller)
        return result

    def _explain_plan(self, sql: str, params: object | None, version: int | None, caller=None) -> dict:
        """One JSON EXPLAIN of the (SELECT) query on a fresh locked connection.

        DuckDB's ``EXPLAIN (FORMAT JSON)`` plans and binds only — the
        referenced tables (virtual views included) are resolved, no row is
        read. The connection applies the standard posture (fs lockdown +
        memory budget + registered schema views — the QueryJob sequence), so
        the plan reflects exactly what a ``run_sql`` of this text would
        execute. Failures degrade to an error entry; the rows/bytes/warm
        fields stand on their own.
        """
        import duckdb

        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            # materialize=False (the _describe_virtual posture): EXPLAIN must
            # stay a cheap schema bind — an explain must never trigger the
            # (potentially expensive) first materialization of a virtual
            # table. The plan of the UNMATERIALIZED definition is exactly
            # what a run_sql would execute when no materialization exists;
            # when one does, its scan is a plain parquet read (the filter is
            # pre-applied, so the plan shows less pushdown — a harmless
            # under-estimate of an already-cheaper path).
            self._register_schema(con, sql, version=version, materialize=False, caller=caller)
            raw = con.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchall()
            tree = json.loads(raw[0][1])
            return {"tree": tree, "summary": self._explain_plan_summary(tree)}
        except Exception as exc:
            return {"tree": None, "summary": None, "error": str(exc)}
        finally:
            con.close()

    # -------------------------------------------------------------- help
    def prewarm(self, tables: Sequence[str]) -> dict[str, str]:
        """Fill the describe cache for tables; return per-table outcome.

        Failures are recorded per table and never raised (a table may be
        temporarily unavailable or renamed).

        Data prewarm (Task B, opt-in via row groups > 0): when the disk
        block cache is ENABLED, each table's first
        ``SQLHANDLER_PREWARM_ROWGROUPS`` (default 1, 0 = off) row groups are
        ALSO read through the table's wrapped dataset — the cache fills
        naturally on the read (the same wrapped-filesystem path every later
        scan takes), so a cold block cache stops charging the first query
        of the day. Local/NFS backends are skipped explicitly (the OS page
        cache does this job for free — the same includeLocal semantics the
        block cache itself applies). The block cache is OFF by default, so
        with no operator opt-in this loop is exactly the historical
        describe-only prewarm and the outcome stays ``"ok"``.
        """
        rowgroups = _prewarm_rowgroups()
        outcomes: dict[str, str] = {}
        for name in tables:
            try:
                self.describe_table(name)
                outcomes[name] = "ok"
            except Exception as exc:
                outcomes[name] = f"error: {exc}"
                logger.warning("prewarm describe %s failed: %s", name, exc)
                continue  # the data warm would just fail the same way
            if rowgroups <= 0:
                continue  # 0 = data prewarm off (describe-only prewarm)
            try:
                outcomes[name] = self._prewarm_data(name, rowgroups, outcomes[name])
            except Exception as exc:  # never fail startup over a warm read
                outcomes[name] = "data-error"
                logger.warning("prewarm data %s failed: %s", name, exc)
        return outcomes

    def _prewarm_data(self, name: str, rowgroups: int, describe_status: str) -> str:
        """Read a table's first row groups through the block cache; status.

        Returns ``"data-ok"`` when row groups were read, ``"ok"`` when the
        table was skipped (block cache off / a pure-local backend the cache
        intentionally does not cover), ``"data-skipped-no-cache"`` when the
        cache is on but the table's format has no wrapped read path, and
        ``"data-error"`` when the read failed (the describe cache keeps
        whatever it got). Called from :meth:`prewarm`'s try/except — never
        on the startup critical path, bounded by the small row-group count.
        """
        from .blockcache import block_cache_enabled

        if not block_cache_enabled():
            # Cache off: the read would land nowhere durable and local disk
            # is the page cache's job anyway — skip explicitly.
            return describe_status
        try:
            info = self._resolve(name)
        except Exception:
            return describe_status  # describe warmed; nothing more to do
        if info.format in ("virtual", "external", "sharing"):
            return "data-skipped-no-cache"
        # includeLocal semantics (blockcache.maybe_block_cache): pure-local
        # filesystems are NOT wrapped by default — the OS page cache already
        # serves them, so a prewarm read of local data buys nothing. NFS
        # mounts ARE LocalFileSystem to pyarrow; sites that opted them into
        # the block cache (SQLHANDLER_BLOCK_CACHE_INCLUDE_LOCAL=1) prewarm
        # them like any other remote source.
        provider = self._prewarm_provider(info)
        if provider is None:
            return "data-skipped-no-cache"
        if getattr(provider, "kind", "") == "nfs" and not self._block_cache_includes_local():
            return "data-skipped-no-cache"
        dset = self._open_dataset(info)
        fragment = next(iter(dset.get_fragments()), None)
        if fragment is None:
            return describe_status  # empty table: nothing to warm
        # fragment.head() rides the dataset's WRAPPED filesystem, so the
        # parquet footer + column chunks land in the block cache on this
        # read — the identical read path (and cache keys) a later query
        # takes. The row-group count bounds the bytes: head(n) stops at the
        # first batch covering n rows, so head over one row group's worth of
        # rows reads ~one row group. No try here: a failed warm read
        # propagates to prewarm()'s per-table handler ("data-error") —
        # startup never sees it.
        rg_rows = self._first_row_group_rows(fragment)
        fragment.head(max(rg_rows, 1) * rowgroups)
        return "data-ok"

    @staticmethod
    def _first_row_group_rows(fragment) -> int:
        """Rows in the fragment's first row group (0 when unknown)."""
        try:
            rg = fragment.row_groups[0]
            return int(getattr(rg, "num_rows", 0) or 0)
        except Exception:
            return 0

    @staticmethod
    def _block_cache_includes_local() -> bool:
        """Whether the block cache was opted into local/NFS paths."""
        from .blockcache import _cfg

        return bool(_cfg()["include_local"])

    def _prewarm_provider(self, info: TableInfo):
        """The provider that owns ``info`` (MultiProvider dispatch), or None.

        Mirrors MultiProvider._owner's tag dispatch without importing it —
        a single-provider engine returns ``self.provider`` directly.
        """
        provider = self.provider
        owner = getattr(provider, "_owner", None)
        if callable(owner):
            try:
                return owner(info)
            except Exception:
                return None
        return provider

    def cache_stats(self) -> dict:
        """Small in-memory snapshot of the metadata caches (for observability)."""
        with self._lock:
            return {
                "describe_cached_tables": len(self._describe_cache),
                "describe_hits": self._describe_hits,
                "describe_misses": self._describe_misses,
                "profile_cached_tables": len(self._profile_cache),
                "profile_hits": self._profile_hits,
                "profile_misses": self._profile_misses,
                "dataset_cached_tables": len(self._dataset_cache),
                "dataset_hits": self._dataset_hits,
                "dataset_misses": self._dataset_misses,
                "virtual_cache": {
                    "ttl": self._virtual_cache_ttl,
                    "dir": self._virtual_cache_dir,
                    "hits": self._virtual_cache_hits,
                    "materializations": self._virtual_cache_writes,
                },
                "result_cache": {
                    "ttl": self._result_cache_ttl,
                    "entries": len(self._result_cache),
                    "bytes": self._result_cache_bytes,
                    "hits": self._result_cache_hits,
                    "writes": self._result_cache_writes,
                },
                # Shared-disk L2 behind the memory LRU (l2cache.py). Top-level
                # l2_hits/l2_misses feed the `cache="l2"` metric series (the
                # same flat shape describe/profile/dataset already use);
                # "l2" carries the full config + counters for humans, and is
                # None when the layer is disabled so dashboards can
                # distinguish "off" from "on with zero hits". Additive — with
                # the L2 off every pre-existing key renders byte-identically.
                "l2_hits": self._l2_cache.stats()["hits"] if self._l2_cache is not None else 0,
                "l2_writes": self._l2_cache.stats()["writes"] if self._l2_cache is not None else 0,
                "l2": self._l2_cache.stats() if self._l2_cache is not None else None,
                "tables_cached": self._tables is not None,
                "tables_cached_age_s": round((time.monotonic() - self._tables_ts), 1)
                if self._tables is not None
                else None,
                "list_async_refresh": self._async_list,
                "list_refreshing": self._list_refreshing,
                "cache_ttl": self.cache_ttl,
                "dataset_cache_ttl": self.dataset_cache_ttl,
                # Resource budget derived from the container's cgroup limits,
                # plus the process's current RSS — so an OOM-bound pod (RSS
                # climbing toward the limit) is visible before the kernel
                # kills it and wipes every cache.
                "container_memory_bytes": resources.container_memory_bytes(),
                "container_cpu_count": resources.container_cpu_count(),
                "process_rss_bytes": resources.process_rss_bytes(),
            }
