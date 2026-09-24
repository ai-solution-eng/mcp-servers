"""Ops observability: Prometheus metrics + structured query audit log.

Dependency-free (Prometheus text exposition 0.0.4, JSONL audit file), so the
server keeps zero extra packages. Everything is best-effort: a metrics or
audit failure must never affect a query.

Metrics (rendered at GET /metrics):

  sqlhandler_queries_total{outcome}        counter (ok | error | timeout | cancelled)
  sqlhandler_query_duration_seconds        histogram (per-query wall time)
  sqlhandler_query_rows_total              counter (rows returned, ok queries)
  sqlhandler_cache_hits_total{cache}       counter (describe | profile | dataset | l2)
  sqlhandler_cache_misses_total{cache}     counter
  sqlhandler_tables                        gauge  (current table count)
  sqlhandler_process_rss_bytes             gauge
  sqlhandler_container_memory_limit_bytes  gauge (0 when unlimited)

Additive families (later waves — absent until first observed, so existing
series and dashboards render byte-identically):

  sqlhandler_caller_queries_total{caller_class}   identity spine
  sqlhandler_writes_total{backend}                write tier (delta | iceberg)
  sqlhandler_writes_total_outcome{outcome}        write tier (ok | error | conflict)

Audit log (SQLHANDLER_AUDIT_LOG=path): one JSON line per query outcome —
{"ts", "event": "query", "sql", "state", "duration_ms", "n_rows", "error"} —
so every SQL executed against the lake is reviewable (SIEM-friendly). The
write tier adds ``event: "write"`` lines with target/backend/rows/caller.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time

logger = logging.getLogger("sqlhandler.observability")

_PROMPT_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Histogram buckets (seconds) — spanning the sub-second cache-hit path to
# multi-minute lake scans.
_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)


class _Counter:
    """A labeled set of monotonic counters (thread-safe)."""

    def __init__(self, name: str, help_text: str, label: str):
        self.name = name
        self.help = help_text
        self.label = label
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    def inc(self, label: str, amount: float = 1) -> None:
        with self._lock:
            self._values[label] = self._values.get(label, 0) + amount

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)


class _Histogram:
    """A fixed-bucket cumulative histogram (thread-safe, per-label)."""

    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...]):
        self.name = name
        self.help = help_text
        self.buckets = buckets
        self._counts: dict[str, list[int]] = {}  # label -> per-bucket cumulative
        self._sums: dict[str, float] = {}
        self._totals: dict[str, int] = {}
        self._lock = threading.Lock()

    def observe(self, label: str, value: float) -> None:
        with self._lock:
            counts = self._counts.setdefault(label, [0] * len(self.buckets))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            self._sums[label] = self._sums.get(label, 0.0) + value
            self._totals[label] = self._totals.get(label, 0) + 1

    def snapshot(self, label: str) -> tuple[list[int], float, int]:
        with self._lock:
            return (
                list(self._counts.get(label, [0] * len(self.buckets))),
                self._sums.get(label, 0.0),
                self._totals.get(label, 0),
            )


class Metrics:
    """Registry of the server's counters/histograms/gauges + text rendering."""

    #: The bounded caller-class vocabulary (identity spine). Labels are these
    #: four values ONLY — never a subject slug or key fingerprint (the
    #: cardinality rule); anything else collapses to ``anonymous``.
    CALLER_CLASSES = ("user", "browser", "key", "anonymous")

    def __init__(self):
        self.queries = _Counter("sqlhandler_queries_total", "Queries executed, by outcome.", "outcome")
        self.duration = _Histogram(
            "sqlhandler_query_duration_seconds", "Query wall time in seconds.", _DURATION_BUCKETS
        )
        self.rows = _Counter("sqlhandler_query_rows_total", "Rows returned by ok queries.", "table")
        # ADDITIVE (identity spine): per-caller-class queries. The series
        # above render byte-identically — this family only ADDS.
        self.caller_queries = _Counter(
            "sqlhandler_caller_queries_total", "Queries executed, by caller class.", "caller_class"
        )
        # ADDITIVE (write tier, review §4): writes by backend + outcome.
        # Backend label: delta | iceberg (DuckLake is a later wave) —
        # bounded vocabulary, like caller_class.
        self.writes = _Counter(
            "sqlhandler_writes_total", "Scratch writes executed, by backend and outcome.", "backend"
        )
        self.write_outcomes = _Counter(
            "sqlhandler_writes_total_outcome", "Scratch write outcomes.", "outcome"
        )

    def record_query(self, outcome: str, duration_s: float, n_rows: int | None, table: str = "") -> None:
        """One query outcome (called from the engine's record path)."""
        try:
            self.queries.inc(outcome)
            self.duration.observe(outcome, max(duration_s, 0.0))
            if outcome == "ok" and n_rows is not None:
                self.rows.inc(table or "unknown", n_rows)
        except Exception:
            logger.debug("metrics record failed", exc_info=True)

    def record_caller_query(self, caller_class: str | None) -> None:
        """One caller-class attribution (best-effort, bounded labels)."""
        try:
            cls = caller_class if caller_class in self.CALLER_CLASSES else "anonymous"
            self.caller_queries.inc(cls)
        except Exception:
            logger.debug("caller metric record failed", exc_info=True)

    #: The bounded write-backend vocabulary (write tier). Everything else
    #: collapses to ``other`` (the cardinality rule again).
    WRITE_BACKENDS = ("delta", "iceberg")

    def record_write(self, backend: str, outcome: str) -> None:
        """One write outcome: ``sqlhandler_writes_total{backend,outcome}``.

        Additive family (write tier): existing series render byte-identically;
        this renders only after the first write is observed. Labels bounded:
        backend in delta|iceberg|other, outcome in ok|error|conflict.
        """
        try:
            be = backend if backend in self.WRITE_BACKENDS else "other"
            oc = outcome if outcome in ("ok", "error", "conflict") else "error"
            self.writes.inc(be)
            self.write_outcomes.inc(oc)
        except Exception:
            logger.debug("write metric record failed", exc_info=True)

    def render(self, engine=None) -> str:
        """Prometheus text exposition (engine gauges included when given)."""
        lines: list[str] = []

        def emit(name: str, help_text: str, typ: str, series: list[tuple[str, str]]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            for labels, value in series:
                lines.append(f"{name}{labels} {value}")

        q = self.queries.snapshot()
        emit(
            self.queries.name,
            self.queries.help,
            "counter",
            [(f'{{outcome="{lbl}"}}', val) for lbl, val in sorted(q.items())],
        )
        for outcome in sorted(q):
            counts, total_sum, total_n = self.duration.snapshot(outcome)
            cumulative = 0
            for i, bucket in enumerate(self.duration.buckets):
                cumulative = counts[i]  # counts are cumulative by construction
                lines.append(f'{self.duration.name}_bucket{{outcome="{outcome}",le="{bucket}"}} {cumulative}')
            lines.append(f'{self.duration.name}_bucket{{outcome="{outcome}",le="+Inf"}} {total_n}')
            emit_count = f'{self.duration.name}_sum{{outcome="{outcome}"}} {round(total_sum, 6)}'
            lines.append(emit_count)
            lines.append(f'{self.duration.name}_count{{outcome="{outcome}"}} {total_n}')
        r = self.rows.snapshot()
        if r:
            emit(
                self.rows.name,
                self.rows.help,
                "counter",
                [(f'{{table="{lbl}"}}', val) for lbl, val in sorted(r.items())],
            )
        # ADDITIVE (identity spine): the caller-class series renders after the
        # historical families so every pre-existing line keeps its byte
        # position. Emitted even when empty? NO — only when a class has been
        # observed, matching how `rows` above stays absent until data exists
        # (dashboards that select the series handle absence).
        cq = self.caller_queries.snapshot()
        if cq:
            emit(
                self.caller_queries.name,
                self.caller_queries.help,
                "counter",
                [(f'{{caller_class="{lbl}"}}', val) for lbl, val in sorted(cq.items())],
            )
        # ADDITIVE (write tier): {backend} and {outcome} views of the write
        # counter — rendered only once a write has been observed, exactly
        # like the caller-class family above (dashboards handle absence).
        w = self.writes.snapshot()
        if w:
            emit(
                self.writes.name,
                self.writes.help,
                "counter",
                [(f'{{backend="{lbl}"}}', val) for lbl, val in sorted(w.items())],
            )
        wo = self.write_outcomes.snapshot()
        if wo:
            emit(
                self.write_outcomes.name,
                self.write_outcomes.help,
                "counter",
                [(f'{{outcome="{lbl}"}}', val) for lbl, val in sorted(wo.items())],
            )
        if engine is not None:
            try:
                stats = engine.cache_stats()
                for cache in ("describe", "profile", "dataset", "l2"):
                    # The "l2" series is additive: describe/profile/dataset
                    # lines render byte-identically to before the shared L2
                    # result cache existed (dashboards/alerts don't move).
                    hits = stats.get(f"{cache}_hits", 0)
                    misses = stats.get(f"{cache}_misses", 0)
                    lines.append("# HELP sqlhandler_cache_hits_total Cache hits by cache type.")
                    lines.append("# TYPE sqlhandler_cache_hits_total counter")
                    lines.append(f'sqlhandler_cache_hits_total{{cache="{cache}"}} {hits}')
                    lines.append("# TYPE sqlhandler_cache_misses_total counter")
                    lines.append(f'sqlhandler_cache_misses_total{{cache="{cache}"}} {misses}')
                tables = engine.list_tables()
                lines.append("# HELP sqlhandler_tables Tables currently exposed.")
                lines.append("# TYPE sqlhandler_tables gauge")
                lines.append(f"sqlhandler_tables {len(tables)}")
                rss = stats.get("process_rss_bytes")
                lines.append("# HELP sqlhandler_process_rss_bytes Process resident memory.")
                lines.append("# TYPE sqlhandler_process_rss_bytes gauge")
                lines.append(f"sqlhandler_process_rss_bytes {rss or 0}")
                mem = stats.get("container_memory_bytes")
                lines.append("# HELP sqlhandler_container_memory_limit_bytes Container memory limit.")
                lines.append("# TYPE sqlhandler_container_memory_limit_bytes gauge")
                lines.append(f"sqlhandler_container_memory_limit_bytes {mem or 0}")
            except Exception:
                logger.debug("engine gauges unavailable for /metrics", exc_info=True)
        return "\n".join(lines) + "\n"


metrics = Metrics()  # process-wide registry


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


def audit_log_path() -> str:
    """Configured audit JSONL path ('' = audit logging off)."""
    return os.environ.get("SQLHANDLER_AUDIT_LOG", "").strip()


def audit_query(
    sql: str,
    state: str,
    duration_ms: float | None,
    n_rows: int | None,
    error: str | None,
    caller=None,
) -> None:
    """Append one query outcome to the audit JSONL (best-effort, never raises).

    ``caller`` (the identity spine, additive): when one is bound, the record
    gains ``"caller": {"class", "subject", "key_fp"}`` — the class label, the
    attributed subject (or None) and the MATCHED KEY's fingerprint (never the
    key). Omitted entirely when no caller is bound, so existing trails and
    readers keep their exact shape (additive-field tolerance is the fleet
    precedent).
    """
    path = audit_log_path()
    if not path:
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "query",
        "sql": sql[:2000],
        "state": state,
        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        "n_rows": n_rows,
        "error": error[:500] if error else None,
    }
    if caller is not None:
        try:
            record["caller"] = caller.as_audit_dict()
        except Exception:
            logger.debug("audit caller field skipped", exc_info=True)
    try:
        line = json.dumps(record, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        logger.debug("audit log write failed", exc_info=True)


def audit_write(
    sql: str,
    state: str,
    duration_ms: float | None,
    target: str,
    backend: str,
    n_rows: int | None,
    error: str | None,
    caller=None,
) -> None:
    """Append one WRITE outcome to the audit JSONL (write tier, additive).

    ``event:"write"`` lines carry what the review specified — target, rows,
    backend — plus the same caller field shape as :func:`audit_query`
    (class/subject/key_fp; never the raw key). Query lines are untouched;
    readers that filter on ``event == "query"`` see byte-identical trails.
    """
    path = audit_log_path()
    if not path:
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "write",
        "sql": sql[:2000],
        "state": state,
        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        "target": target[:500] if target else None,
        "backend": backend,
        "n_rows": n_rows,
        "error": error[:500] if error else None,
    }
    if caller is not None:
        try:
            record["caller"] = caller.as_audit_dict()
        except Exception:
            logger.debug("audit caller field skipped", exc_info=True)
    try:
        line = json.dumps(record, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        logger.debug("audit log write failed", exc_info=True)


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time comparison of a provided API token against the expected one.

    Accepts both ``Bearer <token>`` (Authorization header) and a bare token
    (X-API-Token header).
    """
    if not expected:
        return True
    provided = (provided or "").strip()
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    return hmac.compare_digest(provided, expected)
