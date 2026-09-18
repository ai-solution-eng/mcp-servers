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

from . import observability, resources
from .external import (
    AttachSpec,
    apply_external,
    parse_attach_config,
    sql_references_attach,
    validate_qualified_name,
)
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version
from .sqlguard import assert_attached_readonly

logger = logging.getLogger("sqlhandler.engine")


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
                f"Too many concurrent queries (limit {_max_concurrent_queries()}), and the "
                f"queue wait of {_queue_timeout()}s expired. Retry later."
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
            self._engine._register_schema(con, self.sql, version=self._version)
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
                self._engine._record_outcome(self.sql, self._elapsed_ms, arrow_table.num_rows, state="ok")
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
            self._engine._record_outcome(self.sql, elapsed, None, state=self._state, error=self._error)
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
    scalars = (str, int, float, bool, bytes, datetime.datetime, datetime.date, datetime.time, Decimal)
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
            return exc
        m = _COLUMN_ERROR.search(msg)
        if m:
            # DuckDB >= 1.x already prints "Candidate bindings: ..." for
            # unknown columns — don't duplicate its suggestions.
            if "Candidate bindings" in msg:
                return exc
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
            return exc
    except Exception:
        return exc
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
        # External read-only database attaches (SQLHANDLER_ATTACH[_FILE]):
        # config parses loudly at startup (operator-authored, security
        # relevant — a typo should kill the pod, not silently skip a source).
        # Connections attach on demand per query; see sqlhandler/external.py.
        self.attaches: list[AttachSpec] = parse_attach_config()
        self._attached_listing: tuple[float, list[dict]] | None = None
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

    # ---------------------------------------------------------------- list
    def list_tables(self) -> list[TableInfo]:
        """Every addressable table: the provider's physical tables plus the
        semantic catalog's virtual tables (entries carrying a ``definition``).

        Virtual tables are appended after the physical ones (sorted by name)
        and carry ``format="virtual"`` — they are computed at query time from
        their definitions and are backed by no storage at all (see
        ``_register_schema``). The disk-warm cache and the provider caches
        below stay physical-only: virtual tables always derive live from the
        hot-reloaded catalog.
        """
        tables = self._provider_tables()
        return tables + self._virtual_infos(tables)

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
            if info.qualified_name == t.qualified_name or (info.path, info.name) == (t.path, t.name):
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
    def describe_table(self, table: str) -> dict:
        """Return column names/types and the canonical URI for a table.

        Cached in-process for cache_ttl seconds keyed by the resolved table
        path, so frequently-described tables come from memory instead of
        re-opening the metadata on every agent call.
        """
        ext = self._match_external_table(table)
        if ext is not None:
            return self._describe_external(*ext)
        info = self._resolve(table)
        if info.format == "virtual":
            return self._describe_virtual(info, table)
        key = (info.source, info.path)
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
        result = {
            "table": table,
            "uri": self.provider.table_uri(info),
            "columns": [{"name": f.name, "type": str(f.type)} for f in schema],
            "n_columns": len(schema),
        }
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
    def search_tables(self, query: str, limit: int = 20) -> list[dict]:
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
        """
        q = query.strip().lower()
        if not q:
            return []
        terms = [t for t in re.split(r"[^a-z0-9_]+", q) if t]
        results: list[dict] = []
        with self._lock:
            cached_describes = {k: v for k, v in self._describe_cache.items()}
        for info in self.list_tables():
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
    def profile_table(self, table: str, columns: Sequence[str] | None = None) -> dict:
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
        """
        ext = self._match_external_table(table)
        if ext is not None:
            return self._profile_external(*ext, columns=columns)
        info = self._resolve(table)
        if info.format == "virtual":
            return self._profile_virtual(info, table, columns)
        col_key = tuple(columns) if columns else ()
        key = (info.source, info.path, col_key)
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]

        dset = self._open_dataset(info)

        # Full-table row count from metadata (Parquet row-group counts /
        # Delta log stats) — no data IO for well-formed files.
        try:
            n_rows: int | None = int(dset.count_rows())
        except Exception:
            n_rows = None

        import duckdb

        cap = _profile_max_rows()
        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            view = "_sqlhandler_profile_target"
            con.register(view, dset)
            col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
            inner = f"SELECT {col_sel} FROM {view}"
            if cap > 0:
                inner = f"SELECT * FROM ({inner}) LIMIT {cap}"
            # Single data scan: the row count of the bounded sample follows
            # from the metadata count (the LIMITed subquery yields exactly
            # min(n_rows, cap) rows), so SUMMARIZE is the only query that
            # touches the data — the separate count(*) ran the same scan
            # twice (audit performance finding). The count query only comes
            # back for the rare metadata-unreadable case.
            if n_rows is not None:
                profiled_rows = min(n_rows, cap) if cap > 0 else n_rows
            else:
                profiled_rows = self._profile_count_fallback(con, inner)
            summary = con.sql(f"SUMMARIZE {inner}").arrow()
            if isinstance(summary, pa.RecordBatchReader):
                summary = summary.read_all()
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

    def _profile_virtual(self, info: TableInfo, table: str, columns: Sequence[str] | None) -> dict:
        """Profile a virtual table by running its definition under SUMMARIZE.

        Unlike physical profiling there is no metadata shortcut: both the row
        count and the summary execute the definition — the same locked-down
        DuckDB path as run_sql, with the summary bounded by
        ``SQLHANDLER_PROFILE_MAX_ROWS``. Cached like the physical path, so
        repeated profiling pays the definition cost once per TTL.
        """
        col_key = tuple(columns) if columns else ()
        key = (info.source, info.path, col_key)
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
            self._register_schema(con, f"SELECT * FROM {target}")
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
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    # -------------------------------------------------------- column stats
    def column_stats(self, table: str, column: str, top_n: int = 5) -> dict:
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
        """
        ext = self._match_external_table(table)
        if ext is not None:
            return self._column_stats_external(*ext, column, top_n)
        info = self._resolve(table)
        cap = _profile_max_rows()
        col_key = (str(column).strip().lower(),)
        key = ("colstats", info.source, info.path, col_key)
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]

        described = self.describe_table(table)
        col, col_type = _validate_column(described, column, table)
        import duckdb

        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            if info.format == "virtual":
                target = _safe_ident(info.name)
                self._register_schema(con, f"SELECT * FROM {target}")
                n_rows: int | None = None  # the definition's full count is not paid for a column sample
                virtual = True
            else:
                dset = self._open_dataset(info)
                try:
                    n_rows = int(dset.count_rows())
                except Exception:
                    n_rows = None
                target = "_sqlhandler_colstats_target"
                con.register(target, dset)
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
    def note_query(self, sql: str, duration_ms: float, n_rows: int | None, error: str | None = None) -> None:
        """Record one query outcome for the query-memory resource (best-effort).

        SQL text is truncated; failures are recorded too so agents can see
        what NOT to repeat.
        """
        if self._query_memory.maxlen and self._unsaved_opens >= _USAGE_SAVE_EVERY:
            self._unsaved_opens = 0
            self._save_cache_to_disk()
        if not self._query_memory.maxlen:
            return
        with self._lock:
            self._query_memory.append(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "sql": sql[:500],
                    "duration_ms": round(duration_ms, 1),
                    "n_rows": n_rows,
                    "error": error[:200] if error else None,
                }
            )

    def query_memory(self) -> list[dict]:
        """Recent query outcomes, oldest first (a snapshot copy)."""
        with self._lock:
            return list(self._query_memory)

    def _record_outcome(
        self, sql: str, duration_ms: float | None, n_rows: int | None, state: str, error: str | None = None
    ) -> None:
        """Single outcome choke point: query memory + metrics + audit log.

        Called by QueryJob for every finished query (ok, error, cancelled);
        each consumer is individually best-effort so observability can never
        break a query.
        """
        self.note_query(sql, duration_ms or 0.0, n_rows, error)
        observability.metrics.record_query(state, (duration_ms or 0.0) / 1000.0, n_rows)
        observability.audit_query(sql, state, duration_ms, n_rows, error)

    # ------------------------------------------------------------- scans
    def scan_arrow(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        filters: Sequence | None = None,
        limit: int | None = None,
        version_as_of: int | None = None,
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
        """
        if limit is None or limit < 0:
            cap = _max_rows()
            limit = cap if cap > 0 else None
        info = self._resolve(table)
        if info.format == "virtual":
            return self._scan_virtual(info, columns, filters, limit, version_as_of)
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

    def _scan_virtual(
        self,
        info: TableInfo,
        columns: Sequence[str] | None,
        filters: Sequence | None,
        limit: int | None,
        version_as_of: int | None,
    ) -> pa.Table:
        """Scan a virtual table through the SQL path (it has no Dataset).

        Column projection and row limits translate into SQL; pyarrow filter
        expressions have no equivalent without parsing, so they are refused
        with a pointer to run_sql (whose predicates push down into the
        definition's base scans anyway). Time travel passes through: the
        definition's base tables are then read at the requested snapshot.
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
        )

    # ------------------------------------------------------------ duckdb
    def query_duckdb(
        self,
        sql: str,
        limit: int | None = None,
        params: object | None = None,
        version_as_of: int | None = None,
        row_cap: int | None = None,
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
        """
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        t0 = time.monotonic()
        fast = self._metadata_count_fastpath(sql, version_as_of)
        if fast is not None:
            self._record_outcome(sql, (time.monotonic() - t0) * 1000, 1, state="ok")
            return fast
        cache_key = self._result_cache_key(sql, params, limit, row_cap, version_as_of)
        if cache_key is not None:
            cached = self._result_cache_lookup(cache_key)
            if cached is not None:
                self._record_outcome(sql, 0.0, cached.num_rows, state="ok")
                return cached
        timeout = _query_timeout()
        job = QueryJob(
            self,
            sql,
            limit=limit,
            params=params,
            version_as_of=version_as_of,
            row_cap=row_cap,
        )
        if timeout > 0:
            job.wait(timeout)
            if job.state == "running":
                # Interrupt inside DuckDB so the worker thread unwinds and
                # the connection is closed instead of leaking.
                job.cancel()
                job.wait(_CANCEL_GRACE_SECONDS)
                raise LakehouseError(f"Query timed out after {timeout}s (SQLHANDLER_QUERY_TIMEOUT) and was cancelled.")
        else:
            job.wait()
        if job.state == "done" and cache_key is not None and job.result is not None:
            self._result_cache_store(cache_key, job.result)
        return job.result

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
        try:
            dset = self._open_dataset(info, version)
            n = int(dset.count_rows())
        except Exception:
            logger.debug("count(*) fast-path failed for %s; using the query path", info.path, exc_info=True)
            return None
        return pa.table({alias: pa.array([n], type=pa.int64())})

    def _result_cache_key(
        self,
        sql: str,
        params: object | None,
        limit: int | None,
        row_cap: int | None,
        version_as_of: int | None,
    ) -> str | None:
        """Cache key for a query: full identity + base-snapshot version tokens.

        The SQL enters the key whitespace-normalized (outside quoted
        literals — see :func:`_normalize_cache_sql`), so formatting-only
        re-runs hit the same entry; quoted content and comment-bearing
        queries keep byte-exact keys.
        

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
            parts = [_normalize_cache_sql(sql), repr(params), repr(limit), repr(row_cap), repr(version_as_of)]
            for info in sorted(refs, key=lambda t: (t.source, t.path)):
                parts.append(f"{info.source}/{info.path}={self._safe_version(info)}")
            return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
        except Exception:
            return None

    def _result_cache_lookup(self, key: str) -> pa.Table | None:
        """The cached result for ``key`` when fresh, else None (LRU-ordered)."""
        with self._lock:
            hit = self._result_cache.get(key)
            if hit is None:
                return None
            ts, table = hit
            if time.time() - ts >= self._result_cache_ttl:
                del self._result_cache[key]
                self._result_cache_bytes -= table.nbytes
                return None
            self._result_cache.move_to_end(key)
            self._result_cache_hits += 1
            return table

    def _result_cache_store(self, key: str, table: pa.Table) -> None:
        """Cache one query result (in-memory, byte-capped, LRU-evicted)."""
        nbytes = table.nbytes
        if self._result_cache_max_bytes > 0 and nbytes > self._result_cache_max_bytes:
            return
        with self._lock:
            while self._result_cache and self._result_cache_bytes + nbytes > self._result_cache_max_bytes:
                _, evicted = self._result_cache.popitem(last=False)
                self._result_cache_bytes -= evicted[1].nbytes
            self._result_cache[key] = (time.time(), table)
            self._result_cache_bytes += nbytes
            self._result_cache_writes += 1

    def _referenced_tables(self, sql: str) -> list[TableInfo]:
        """Return the tables referenced by a SQL query.

        Matches table identifiers (bare or qualified) against the known
        tables, so we only open the datasets the query actually touches.
        """
        known = self.list_tables()
        wanted: dict[tuple[str, str], TableInfo] = {}
        for info in known:
            for ident in (re.escape(info.name), re.escape(info.qualified_name)):
                if re.search(rf"\b{ident}\b", sql):
                    wanted[(info.source, info.path)] = info
                    break
        return list(wanted.values())

    def _register_schema(self, con, sql: str, version: int | None = None, materialize: bool = True) -> None:
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
        """
        name_counts: dict[str, int] = {}
        for info in self.list_tables():
            name_counts[info.name] = name_counts.get(info.name, 0) + 1
        physical, virtuals = self._expand_query_tables(self._referenced_tables(sql))
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
            for view in views:
                try:
                    con.register(view, dset)
                except Exception:
                    logger.debug("Could not register view %s from %s", view, info.path)
        if not virtuals:
            return
        self._apply_compat_macros(con)
        for info in virtuals:
            self._register_virtual(con, info, version, materialize, name_counts)

    def _register_virtual(
        self, con, info: TableInfo, version: int | None, materialize: bool, name_counts: dict[str, int]
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
        """
        views = [_safe_ident(info.qualified_name)]
        if name_counts.get(info.name, 0) <= 1:
            views.append(_safe_ident(info.name))
        definition = self._definition_sql(info.name)
        dset = None
        if self._virtual_cache_ttl > 0 and version is None:
            dset = self._virtual_cache_lookup(info, definition)
            if dset is None and materialize:
                dset = self._virtual_cache_materialize(con, info, definition)
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

    def _virtual_cache_base(self, info: TableInfo, definition: str) -> dict:
        """Identity of a virtual table's cache entry.

        The key covers the definition text (rewritten) of the table AND of
        every virtual table it transitively builds on (a nested definition
        change must invalidate its dependents), plus the resolved base
        tables' snapshot-version tokens, so a new ETL commit on any base
        invalidates automatically. Returns the parquet path, the meta sidecar
        path, the key hash, and the current expected version tokens.
        """
        physical, virtuals = self._expand_query_tables([info])
        h = hashlib.sha256()
        for v in virtuals:  # topologically ordered -> deterministic
            h.update(v.name.encode())
            h.update(b"\0")
            h.update(self._definition_sql(v.name).encode())
            h.update(b"\0")
        versions = {
            f"{i.source}/{i.path}": str(self._safe_version(i))
            for i in sorted(physical, key=lambda t: (t.source, t.path))
        }
        digest = h.hexdigest()
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", info.name)
        path = Path(self._virtual_cache_dir) / f"{safe}-{digest[:16]}.parquet"
        return {
            "path": str(path),
            "meta": str(path) + ".json",
            "sha256": digest,
            "versions": versions,
        }

    def _virtual_cache_lookup(self, info: TableInfo, definition: str):
        """A pyarrow Dataset over the cached materialization when it is valid
        for this definition + base snapshots + TTL, else None (never raises)."""
        import pyarrow.dataset as pad

        base = self._virtual_cache_base(info, definition)
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

    def _virtual_cache_materialize(self, con, info: TableInfo, definition: str):
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
        """
        import pyarrow.dataset as pad
        import pyarrow.parquet as pq

        base = self._virtual_cache_base(info, definition)
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

    # -------------------------------------------------------------- help
    def prewarm(self, tables: Sequence[str]) -> dict[str, str]:
        """Fill the describe cache for tables; return per-table outcome.

        Failures are recorded per table and never raised (a table may be
        temporarily unavailable or renamed).
        """
        outcomes: dict[str, str] = {}
        for name in tables:
            try:
                self.describe_table(name)
                outcomes[name] = "ok"
            except Exception as exc:
                outcomes[name] = f"error: {exc}"
                logger.warning("prewarm describe %s failed: %s", name, exc)
        return outcomes

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
