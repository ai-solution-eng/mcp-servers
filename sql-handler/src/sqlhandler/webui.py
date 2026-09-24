"""Read-only web UI + JSON API for the SQLhandler MCP server.

Serves a self-contained HTML explorer (``index.html``) and a small JSON API
that reuses the SAME process-wide ``SqlEngine`` (and its caches) that backs
the MCP tools. The API is deliberately **read-only**: every statement is
parsed with DuckDB's own grammar and only plain SELECT queries (plus EXPLAIN
of a SELECT) are accepted, so the UI is a safe human front-end for the same
data the MCP agents query. Additionally, the DuckDB connection used for
these queries has local-file access disabled (see ``engine.py``), so a
SELECT cannot read files inside the container or COPY results out.

Endpoints (all JSON unless noted):

  GET  /api/status    -> {"status": "ok", "version", "backend"}
  GET  /api/tables    -> {"tables": [{"name", "path", "format"}]}
  POST /api/describe  -> {"table", "uri", "columns": [{"name", "type"}]}
  POST /api/query     -> {"columns": [...], "rows": [[...]], "n_rows", "duration_ms"}
                         ({"format": "arrow"} returns a base64 Arrow IPC
                         stream text payload instead of the JSON shape)
  POST /api/query/async -> {"query_id", "state"} (long queries; poll /rows)
  GET  /api/query/{id}          -> job status (state, columns, n_rows)
  GET  /api/query/{id}/rows     -> paginated result rows (offset/limit)
  DELETE /api/query/{id}        -> cancel a running job
  POST /api/jobs                -> async query job (MCP query_submit twin;
                                   SQLHANDLER_MAX_JOBS cap, submit-time
                                   read-only guard, fetch-once result)
  GET  /api/jobs/{id}           -> job status
  GET  /api/jobs/{id}/result    -> the result, handed over ONCE then freed
  DELETE /api/jobs/{id}         -> cancel a running job
  GET    /api/saved-queries            -> saved parameterized queries
  POST   /api/saved-queries            -> save one (auth-gated write)
  DELETE /api/saved-queries/{name}     -> delete one (auth-gated write)
  POST   /api/saved-queries/{name}/run -> run one (bind params, read-only)
  POST /api/preview   -> {"columns": [...], "rows": [[...]], "n_rows", "duration_ms"}
  POST /api/profile   -> column-level statistics (min/max, null %, distinct, quantiles)
  POST /api/export    -> CSV/Parquet/Arrow file download of a query or table (attachment)

  Semantic catalog — documentation for agents and humans (the one mutating
  corner: it writes to the engine's catalog store, never data):
  GET    /api/semantic-catalog         -> which catalog is live, where from
  POST   /api/semantic-catalog         -> upload/replace the catalog (JSON or YAML body)
  DELETE /api/semantic-catalog         -> remove the uploaded catalog
  GET    /api/semantic-catalog/content -> the live catalog as editable YAML/JSON text
  GET    /api/semantic-catalog/table   -> one table's entry (?table=, ?format=)
  POST   /api/semantic-catalog/table   -> upsert one table's entry {table, content}
  DELETE /api/semantic-catalog/table   -> drop one table's entry (?table=)
  POST   /api/semantic-catalog/import-dbt         -> dbt manifest.json -> PROPOSED catalog (no write)
  POST   /api/semantic-catalog/import-dbt/apply   -> same body + writes the merged catalog to the store
  POST   /api/highlight                -> pygments-guessed HTML (rcat-style -g) for editors

The HTML page is served at ``/`` and ``/ui``.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import time as _time
import uuid
from collections import OrderedDict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse, Response

from . import identity as _identity
from .dbt_import import apply_import as _dbt_apply_import
from .dbt_import import import_dbt_manifest as _dbt_import_dbt_manifest
from .engine import (
    LakehouseError,
    QueryJob,
    SqlEngine,
    _max_rows,
    _validate_params,
    _validate_snapshot_version,
)
from .jobs import JobError, api_job_cancel, api_job_result, api_job_status, api_job_submit
from .saved import (
    NotAuthorized,
    UnknownSavedQuery,
    api_saved_delete,
    api_saved_list,
    api_saved_run,
    api_saved_save,
)
from .sqlguard import assert_readonly as _guard_assert_readonly
from .sqlguard import (
    extract_statement_spans,
)

_DEFAULT_LIMIT = 100
# Fallback UI cap when SQLHANDLER_MAX_ROWS is unset or 0 (unlimited for the
# MCP path): the browser API still needs a bounded payload size.
_FALLBACK_MAX_LIMIT = 1000

_HTML = (Path(__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# read-only guard
# ---------------------------------------------------------------------------

# The parser lives in sqlhandler.sqlguard (one implementation shared with the
# MCP run_sql path and the attached-catalog query path). The web surface keeps
# its own wrapper so error messages keep the historical "Read-only UI" prefix.


def assert_readonly(sql: str) -> str:
    """Return the trimmed SQL if it is a read-only statement, else raise.

    Every parsed statement must be a plain SELECT (DuckDB parses WITH/VALUES/
    SHOW/DESCRIBE/SUMMARIZE as SELECT too). EXPLAIN is allowed only when the
    explained statement is itself a SELECT — ``EXPLAIN ANALYZE INSERT``
    actually executes the insert, so it is rejected. PRAGMA/SET, COPY, and
    every write statement are rejected regardless of position.

    This guards the *web UI / JSON API* (always on). The MCP ``run_sql`` tool
    runs the same parser guard under decision D2 (``SQLHANDLER_MCP_READONLY``,
    default on); queries that can see an attached external catalog are
    SELECT-only unconditionally (see sqlhandler/engine.py).
    """
    return _guard_assert_readonly(sql, context="Read-only UI")


def _split_statements(sql: str) -> list[str]:
    """Per-statement text slices, taken from the parser's exact spans.

    Was a character-scanning heuristic (single-quote toggle + semicolon
    split); escaped quotes (''), double-quoted identifiers, dollar-quoted
    strings and comments confused it. ``duckdb.extract_statements`` provides
    each statement's exact source text, so the slices are now parser-exact.
    Only used for EXPLAIN inner-statement inspection, where the slice is
    re-parsed, not executed as-is.
    """
    return [text for _stmt_type, text in extract_statement_spans(sql) if text.strip()]


# ---------------------------------------------------------------------------
# JSON-safe row conversion
# ---------------------------------------------------------------------------


def _json_safe(value):
    """Convert one Arrow/pandas scalar into a JSON-serialisable value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # NaN / Inf are not valid JSON.
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    # numpy / pandas scalars expose .item()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return repr(value)


# ---------------------------------------------------------------------------
# Arrow IPC (format="arrow")
# ---------------------------------------------------------------------------

_ARROW_MEDIA_TYPE = "application/vnd.apache.arrow.stream"


def arrow_to_ipc_stream_bytes(arrow) -> bytes:
    """Serialize one Arrow Table as IPC stream bytes (the "arrow" format).

    ``pa.ipc.new_stream`` writes the schema + batches to the sink; a
    ``pa.ipc.open_stream`` reader reconstructs the table byte-faithfully —
    decimals, timestamps and nulls survive (markdown/csv re-render them as
    text, which is exactly the fidelity loss the format exists to avoid).
    """
    import io

    import pyarrow as pa

    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, arrow.schema) as writer:
        writer.write_table(arrow)
    return sink.getvalue()


def arrow_to_ipc_text(arrow) -> str:
    """Render one Arrow Table as a base64 IPC-stream text payload.

    The first line is a human/machine-readable header (the same additive
    posture as the JSON payload's meta keys); the base64 body follows and
    decodes straight into ``pa.ipc.open_stream``.
    """
    raw = arrow_to_ipc_stream_bytes(arrow)
    header = f"# arrow: {arrow.num_rows} rows x {arrow.num_columns} cols, {len(raw)} bytes ipc-stream base64"
    return header + "\n" + base64.b64encode(raw).decode("ascii")


def arrow_to_payload(arrow, limit: int | None = None) -> dict:
    """Convert a pyarrow Table into a JSON payload dict (columns + rows).

    ``n_rows`` is the number of rows in the payload (after any slicing);
    ``truncated`` is True when a positive ``limit`` was requested and the
    result was capped at it (more rows may exist upstream).
    """
    columns = [f.name for f in arrow.schema] if arrow is not None else []
    if arrow is None or arrow.num_rows == 0:
        return {"columns": columns, "rows": [], "n_rows": 0, "truncated": False}
    rows = arrow.to_pylist()
    truncated = False
    if limit is not None and limit > 0:
        if len(rows) > limit:
            rows = rows[:limit]
        # A request that hit the limit likely has more rows upstream.
        truncated = len(rows) >= limit
    return {
        "columns": columns,
        "rows": [[_json_safe(row.get(col)) for col in columns] for row in rows],
        "n_rows": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# API handler implementations (engine passed in for testability)
# ---------------------------------------------------------------------------


def api_status(engine: SqlEngine) -> dict:
    from . import __version__

    return {
        "status": "ok",
        "version": __version__,
        "backend": engine.provider.kind if hasattr(engine.provider, "kind") else "unknown",
    }


def api_tables(engine: SqlEngine, caller=None) -> dict:
    return {
        "tables": [
            {
                "name": t.name,
                "path": t.path,
                "qualified_name": t.qualified_name,
                "schema": t.schema,
                "format": t.format,
                "source": t.source,
            }
            for t in engine.list_tables(caller=caller)
        ]
    }


def api_describe(engine: SqlEngine, table: str, caller=None) -> dict:
    info = engine.describe_table(table, caller=caller)
    out = {
        "table": table,
        "uri": info["uri"],
        "columns": info["columns"],
        "n_columns": info["n_columns"],
    }
    # Semantic-catalog documentation, when a catalog is attached and covers
    # this table (the MCP surface has always carried it; the web API + UI get
    # it too so an uploaded catalog is visible where it was uploaded).
    if info.get("description"):
        out["description"] = info["description"]
    if info.get("aliases"):
        out["aliases"] = info["aliases"]
    # Raw-format marker (mirrors the virtual-table flag): the UI badges raw
    # landing-zone tables so their no-statistics nature is visible.
    if info.get("raw"):
        out["raw"] = True
        out["format"] = info.get("format", "")
    return out


def api_query(
    engine: SqlEngine,
    sql: str,
    limit: int | None = None,
    params: object | None = None,
    caller=None,
    output_format: str = "json",
) -> dict | str:
    import time

    safe = assert_readonly(sql)
    params = _validate_params(params)  # raises ValueError-like LakehouseError early
    limit = _clamp_limit(limit)
    t0 = time.monotonic()
    arrow = engine.query_duckdb(safe, limit=limit, params=params, caller=caller)
    duration_ms = round((time.monotonic() - t0) * 1000, 1)
    if str(output_format or "json").strip().lower() == "arrow":
        # Arrow IPC text payload: the duration meta rides as a second header
        # line (the base64 body has no meta keys to merge into).
        text = arrow_to_ipc_text(arrow)
        return text + f"\n# duration_ms: {duration_ms}\n# sql: {safe}"
    payload = arrow_to_payload(arrow, limit=limit)
    payload.update({"sql": safe, "duration_ms": duration_ms})
    return payload


def api_preview(engine: SqlEngine, table: str, limit: int | None = None, caller=None) -> dict:
    import time

    limit = _clamp_limit(limit)
    t0 = time.monotonic()
    arrow = engine.scan_arrow(table, limit=limit, caller=caller)
    duration_ms = round((time.monotonic() - t0) * 1000, 1)
    payload = arrow_to_payload(arrow, limit=limit)
    payload.update({"table": table, "duration_ms": duration_ms})
    return payload


def _json_safe_deep(value):
    """Recursively convert Decimal/bytes/etc. values for JSON serialization.

    DuckDB's SUMMARIZE returns avg/std etc. as strings, but the count column
    arrives as Decimal through Arrow — JSONResponse refuses Decimals.
    """
    if isinstance(value, dict):
        return {k: _json_safe_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_deep(v) for v in value]
    return _json_safe(value)


def api_profile(engine: SqlEngine, table: str, columns: list[str] | None = None, caller=None) -> dict:
    """Column-level statistics (shares the engine's profile cache with MCP)."""
    import time

    t0 = time.monotonic()
    profile = _json_safe_deep(engine.profile_table(table, columns=columns, caller=caller))
    profile["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return profile


def _clamp_limit(limit: int | None) -> int:
    """Normalise a client limit to (1, cap], defaulting to _DEFAULT_LIMIT.

    The cap follows ``SQLHANDLER_MAX_ROWS`` (same knob as the MCP path) when
    it is set to a positive value, so raising it raises the UI cap too. When
    it is 0 (unlimited) the UI still clamps to _FALLBACK_MAX_LIMIT to keep
    the JSON payload bounded.
    """
    cap = _max_rows()
    if cap <= 0:
        cap = _FALLBACK_MAX_LIMIT
    if limit is None or limit <= 0:
        return min(_DEFAULT_LIMIT, cap)
    return min(limit, cap)


# ---------------------------------------------------------------------------
# async query jobs (submit / poll / paginate / cancel)
# ---------------------------------------------------------------------------

# Max rows per page from GET /api/query/{id}/rows — keeps any single page
# response bounded regardless of what the client asks for.
_MAX_PAGE_ROWS = 1000


def _job_ttl() -> int:
    """Seconds a finished job stays fetchable (SQLHANDLER_ASYNC_JOB_TTL)."""
    import os

    raw = os.environ.get("SQLHANDLER_ASYNC_JOB_TTL", "")
    try:
        return max(int(raw), 0) if raw else 900
    except ValueError:
        return 900


class QueryJobManager:
    """Process-wide registry of async query jobs (bounded, TTL-evicted).

    Jobs are the SAME :class:`~sqlhandler.engine.QueryJob` the synchronous
    path runs, so a submitted query honors SQLHANDLER_QUERY_TIMEOUT, the row
    caps and the query-memory hook. Results are spooled in memory as Arrow
    tables (already limited by SQLHANDLER_MAX_ROWS) and served paginated.
    """

    def __init__(self, max_jobs: int = 100):
        self._jobs: OrderedDict[str, tuple[QueryJob, float | None]] = OrderedDict()
        self._max_jobs = max_jobs

    def submit(self, engine: SqlEngine, sql: str, limit=None, params=None, version_as_of=None) -> dict:
        """Validate + start a job; returns {"query_id", "state"}.

        Validation happens BEFORE the job starts, so a bad payload is a
        synchronous error, not a job that immediately fails.
        """
        _validate_params(params)
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        self._cleanup()
        if len(self._jobs) >= self._max_jobs:
            return {"error": "Too many tracked queries; retry later.", "status": 429}
        job = QueryJob(engine, sql, limit=limit, params=params, version_as_of=version_as_of)
        query_id = uuid.uuid4().hex
        self._jobs[query_id] = (job, None)  # None finished_at = running
        return {"query_id": query_id, "state": job.state}

    def get(self, query_id: str) -> QueryJob | None:
        self._cleanup()
        entry = self._jobs.get(query_id)
        return entry[0] if entry else None

    def cancel(self, query_id: str) -> dict | None:
        job = self.get(query_id)
        if job is None:
            return None
        interrupted = job.cancel()
        return {"query_id": query_id, "state": job.state, "interrupted": interrupted}

    def _cleanup(self) -> None:
        """Evict finished jobs past the TTL, then enforce the size cap."""
        ttl = _job_ttl()
        now = _time.monotonic()
        for qid, (job, finished_at) in list(self._jobs.items()):
            if finished_at is None and job.state != "running":
                self._jobs[qid] = (job, now)
        if ttl > 0:
            for qid, (job, finished_at) in list(self._jobs.items()):
                if finished_at is not None and now - finished_at > ttl:
                    del self._jobs[qid]
        # Make room for the submit that triggered cleanup: evict oldest
        # finished jobs until BELOW the cap (a full registry of RUNNING
        # jobs is refused at submit time, never evicted here).
        while len(self._jobs) >= self._max_jobs:
            for qid, (job, finished_at) in self._jobs.items():
                if finished_at is not None:
                    del self._jobs[qid]
                    break
            else:
                break


def api_async_query(engine: SqlEngine, manager: QueryJobManager, body: dict) -> dict:
    """POST /api/query/async — start a read-only query, return a job id."""
    sql = str(body.get("sql", ""))
    safe = assert_readonly(sql)  # ValueError -> 400 (route wrapper)
    result = manager.submit(
        engine,
        safe,
        limit=body.get("limit"),
        params=body.get("params"),
        version_as_of=body.get("version_as_of"),
    )
    if result.get("error"):
        return result  # carries its own "status" for the route wrapper
    result["sql"] = safe
    return result


def api_query_status(manager: QueryJobManager, query_id: str) -> dict:
    """GET /api/query/{id} — job status (no row data)."""
    job = manager.get(query_id)
    if job is None:
        return {"error": f"Unknown query id: {query_id}", "status": 404}
    payload = job.info()
    payload["query_id"] = query_id
    if job.state == "done":
        arrow = job.result
        payload["columns"] = [f.name for f in arrow.schema]
        payload["n_rows"] = arrow.num_rows
        payload["elapsed_ms"] = round((job.elapsed_ms or 0), 1)
    return payload


def api_query_rows(manager: QueryJobManager, query_id: str, offset: int = 0, limit: int = 100) -> dict:
    """GET /api/query/{id}/rows — paginate the spooled result."""
    job = manager.get(query_id)
    if job is None:
        return {"error": f"Unknown query id: {query_id}", "status": 404}
    if job.state == "running":
        return {"query_id": query_id, "state": "running"}
    if job.state == "error":
        return {"error": job.error, "status": 400}
    if job.state == "cancelled":
        return {"error": "Query was cancelled.", "status": 400}
    arrow = job.result
    try:
        offset = max(int(offset), 0)
        limit = min(max(int(limit), 0), _MAX_PAGE_ROWS)
    except (TypeError, ValueError):
        return {"error": "offset/limit must be integers.", "status": 400}
    page = arrow.slice(offset, limit)
    payload = arrow_to_payload(page)
    payload.update(
        {
            "query_id": query_id,
            "state": "done",
            "offset": offset,
            "page_size": limit,
            "total_rows": arrow.num_rows,
        }
    )
    return payload


def api_query_cancel(manager: QueryJobManager, query_id: str) -> dict:
    """DELETE /api/query/{id} — cancel a running job."""
    result = manager.cancel(query_id)
    if result is None:
        return {"error": f"Unknown query id: {query_id}", "status": 404}
    return result


# ---------------------------------------------------------------------------
# export (CSV / Parquet file downloads)
# ---------------------------------------------------------------------------

_EXPORT_DEFAULT_ROWS = 10_000


def _export_max_rows() -> int:
    """Hard row cap for exports (SQLHANDLER_EXPORT_MAX_ROWS, default 100k).

    Exports intentionally get a HIGHER cap than the on-screen/LLM result cap
    (SQLHANDLER_MAX_ROWS, default 1000) — a file download is the point. 0
    here still means a hard 1,000,000-row ceiling so no request can try to
    materialize an unbounded file.
    """
    import os

    raw = os.environ.get("SQLHANDLER_EXPORT_MAX_ROWS", "")
    try:
        value = max(int(raw), 0) if raw else 100_000
    except ValueError:
        return 100_000
    return value if value > 0 else 1_000_000


def api_export(engine: SqlEngine, body: dict) -> dict:
    """POST /api/export — download a query or table result as CSV/Parquet/Arrow.

    Accepts {"sql": ...} (read-only guard applies) or {"table": ...}
    (preview-style scan), optional "limit" (clamped to
    SQLHANDLER_EXPORT_MAX_ROWS) and "format" ("csv" | "parquet" | "arrow").
    Returns {content: bytes, media_type, filename} for the route to send as
    an attachment.
    """
    import io

    import pyarrow.parquet as pq

    fmt = str(body.get("format", "csv")).strip().lower()
    if fmt not in ("csv", "parquet", "arrow"):
        raise ValueError(f"Unsupported export format {fmt!r}; use 'csv', 'parquet' or 'arrow'.")
    try:
        raw_limit = body.get("limit")
        limit = _EXPORT_DEFAULT_ROWS if raw_limit is None else max(int(raw_limit), 0)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer.")
    cap = min(limit, _export_max_rows()) if limit > 0 else _export_max_rows()

    table = body.get("table")
    sql = body.get("sql")
    if sql:
        safe = assert_readonly(str(sql))  # ValueError -> 400
        arrow = engine.query_duckdb(safe, limit=cap, row_cap=cap)
        name = "query"
    elif table:
        arrow = engine.scan_arrow(str(table), limit=cap)
        name = str(table).replace("/", "_")
    else:
        raise ValueError("Provide either 'sql' or 'table' to export.")

    if fmt == "csv":
        content = arrow.to_pandas().to_csv(index=False).encode("utf-8")
        media_type = "text/csv"
    elif fmt == "arrow":
        content = arrow_to_ipc_stream_bytes(arrow)
        media_type = _ARROW_MEDIA_TYPE
    else:
        sink = io.BytesIO()
        pq.write_table(arrow, sink)
        content = sink.getvalue()
        media_type = "application/octet-stream"
    return {"content": content, "media_type": media_type, "filename": f"{name}.{fmt}"}


# ---- semantic catalog (status / upload / clear) ------------------------------
# The one mutating corner of the API: it writes documentation (table/column
# descriptions) to the engine's catalog store — never data. Guarded by the
# same layers as every /api route (SQLHANDLER_API_TOKEN / gateway auth), and
# switchable off entirely with SQLHANDLER_CATALOG_UPLOAD=0.

# Catalogs are documentation; anything near a megabyte is abuse or a mistake.
_CATALOG_UPLOAD_MAX_BYTES = 1_000_000


def api_catalog_status(engine: SqlEngine) -> dict:
    """GET /api/semantic-catalog — which catalog is live, where it came from."""
    return engine.catalog_status()


def api_catalog_upload(engine: SqlEngine, body: bytes) -> dict:
    """POST /api/semantic-catalog — validate + store an uploaded catalog.

    The body is the raw file contents, JSON or YAML (the UI reads the file
    client-side and POSTs the text — no multipart dependency; curl works too
    via --data-binary). Raises ValueError (-> 400) on invalid content and
    OSError (-> 500) on an unwritable store.
    """
    if not engine.catalog_uploads_enabled:
        raise ValueError("Semantic-catalog upload is disabled (SQLHANDLER_CATALOG_UPLOAD=0).")
    if len(body) > _CATALOG_UPLOAD_MAX_BYTES:
        raise ValueError(f"Catalog upload too large ({len(body)} bytes; cap is {_CATALOG_UPLOAD_MAX_BYTES}).")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Catalog upload must be UTF-8 text (JSON or YAML).") from exc
    if not text.strip():
        raise ValueError("Catalog upload is empty.")
    return engine.set_catalog_text(text)


def api_catalog_clear(engine: SqlEngine) -> dict:
    """DELETE /api/semantic-catalog — remove the uploaded catalog.

    Falls back to the configured SQLHANDLER_CATALOG file (if any).
    """
    removed = engine.clear_catalog()
    return {"removed": removed, **engine.catalog_status()}


# ---- dbt manifest import (semantic catalog) ----------------------------------
# A dbt compile artifact (target/manifest.json) already carries what the
# semantic catalog wants — model/column descriptions, dbt meta, compiled SQL.
# This importer turns one into catalog content: parse-only by default
# (import → preview → explicit Apply), writing through the SAME validated
# upload store as POST /api/semantic-catalog so validation, the PVC store and
# hot reload ride along. The parsing lives in sqlhandler.dbt_import (pure,
# stdlib-json, no dbt dependency); these wrappers only bind the HTTP shapes
# to the engine.


def api_dbt_import(engine: SqlEngine, body: object) -> dict:
    """POST /api/semantic-catalog/import-dbt — manifest → PROPOSED catalog.

    Body JSON::

        {"manifest": {...full manifest.json...}}
        {"manifest_b64": "<base64 of manifest.json>"}
        {"manifest": "...", "allow_virtual": true,             # default false
         "source_filter": "jaffle",                             # null = all
         "alias_map": {"dbt_node_name": "catalog_table_key"}}

    Virtual tables are double-gated: a node generates a ``definition`` only
    when it carries ``meta.sqlhandler.virtual: true`` AND the request passes
    ``allow_virtual: true`` — the global flag alone never turns on virtual
    tables. ``meta.sqlhandler.hide: true`` omits a node entirely. Nothing is
    written: the response returns the proposed tables mapping plus
    ``imported`` / ``virtuals`` / ``skipped`` / ``warnings`` counts, and the
    caller decides whether to POST the same body to the /apply route.
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    manifest = body["manifest"] if body.get("manifest") is not None else body.get("manifest_b64")
    if manifest is None:
        raise ValueError("Provide 'manifest' (the manifest.json content) or 'manifest_b64' (base64 of it).")
    alias_map = body.get("alias_map")
    if alias_map is not None and not isinstance(alias_map, dict):
        raise ValueError("alias_map must map dbt node names to catalog table keys.")
    return _dbt_import_dbt_manifest(
        manifest,
        allow_virtual=bool(body.get("allow_virtual", False)),
        source_filter=body.get("source_filter") or None,
        alias_map=alias_map,
    )


def api_dbt_import_apply(engine: SqlEngine, body: object) -> dict:
    """POST /api/semantic-catalog/import-dbt/apply — preview + write to the store.

    Takes the exact same body as the import (preview) route and additionally:

        {"force_overwrite": true}   # replace non-dbt entries too (default false)

    Merge rule: the importer output is merged node-by-node into the effective
    catalog; an existing key is overwritten ONLY when it was itself produced
    by a previous dbt import (the entry's ``meta.imported_from == "dbt"``
    marker — the importer tracks provenance in entry meta, which the schema
    preserves) or ``force_overwrite`` is true. Hand-written entries always
    survive and stay listed in ``warnings`` so the operator can see what the
    import did not touch. The merged catalog is written through
    ``engine.set_catalog_text`` (validation, canonical-JSON store, atomic
    replace, hot reload, "most recent intentional action wins" precedence).
    """
    result = api_dbt_import(engine, body)
    force = bool(body.get("force_overwrite", False)) if isinstance(body, dict) else False
    return {"proposed": result, **_dbt_apply_import(engine, result, force_overwrite=force)}


# ---- semantic catalog editor (global + per-table) ----------------------------
# The editor loads the active catalog as text and writes back through the
# same upload store a global upload uses, so one precedence rule ("the most
# recent intentional action wins") covers every path. Per-table edits reuse
# the store too — the merged catalog is stored, never the fragment alone.

# Syntax highlighting is the rcat approach from the terminal tool: let
# Pygments GUESS the lexer from filename + content (``pygmentize -g``
# semantics — a bare content guess misfires on small snippets) seeded by the
# pane's format, and degrade to plain text — never an error — when pygments
# is missing or the text is huge.
try:
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter as _HtmlFormatter
    from pygments.lexers import (
        JsonLexer as _JsonLexer,
    )
    from pygments.lexers import (
        TextLexer as _TextLexer,
    )
    from pygments.lexers import (
        YamlLexer as _YamlLexer,
    )
    from pygments.lexers import (
        guess_lexer_for_filename as _guess_lexer_for_filename,
    )

    _HAVE_PYGMENTS = True
except ImportError:  # optional dependency; the editor just shows plain text
    _HAVE_PYGMENTS = False

# Cap what we are willing to lex per request (client debounces; this guards
# the server from pathological pastes).
_HIGHLIGHT_MAX_CHARS = 200_000

# Theme-appropriate pygments styles, overridable like rcat's FV_PYG_STYLE.
_PYG_STYLES = {
    "dark": os.environ.get("SQLHANDLER_PYG_STYLE_DARK", "").strip() or "monokai",
    "light": os.environ.get("SQLHANDLER_PYG_STYLE_LIGHT", "").strip() or "default",
}


def api_catalog_content(engine: SqlEngine, fmt: str = "yaml") -> dict:
    """GET /api/semantic-catalog/content — the live catalog as editable text.

    YAML is the default user-facing format; ``?format=json`` swaps it.
    """
    if fmt not in ("yaml", "json"):
        raise ValueError("format must be 'yaml' or 'json'")
    return engine.catalog_content(fmt)


def api_catalog_table(engine: SqlEngine, table: str, fmt: str = "yaml") -> dict:
    """GET /api/semantic-catalog/table — one table's catalog breakout."""
    if fmt not in ("yaml", "json"):
        raise ValueError("format must be 'yaml' or 'json'")
    if not table or not table.strip():
        raise ValueError("Provide the table to document (?table=...).")
    return engine.catalog_table_entry(table.strip(), fmt)


def api_catalog_table_update(engine: SqlEngine, table: str, content: object) -> dict:
    """POST /api/semantic-catalog/table — upsert one table's entry.

    Body JSON: ``{"table": <path-or-name>, "content": "<YAML or JSON text>"}``.
    The fragment replaces that table's entry inside the effective catalog;
    the merged catalog is stored (uploads disabled -> 400 via ValueError).
    """
    if not engine.catalog_uploads_enabled:
        raise ValueError("Semantic-catalog edits are disabled (SQLHANDLER_CATALOG_UPLOAD=0).")
    if not isinstance(table, str) or not table.strip():
        raise ValueError("Provide the table to document.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Provide the edited catalog entry content.")
    if len(content) > _CATALOG_UPLOAD_MAX_BYTES:
        raise ValueError(f"Catalog entry too large ({len(content)} bytes; cap is {_CATALOG_UPLOAD_MAX_BYTES}).")
    return engine.catalog_update_table(table.strip(), content)


def api_catalog_table_remove(engine: SqlEngine, table: str) -> dict:
    """DELETE /api/semantic-catalog/table — drop one table's entry."""
    if not engine.catalog_uploads_enabled:
        raise ValueError("Semantic-catalog edits are disabled (SQLHANDLER_CATALOG_UPLOAD=0).")
    if not isinstance(table, str) or not table.strip():
        raise ValueError("Provide the table to remove (?table=...).")
    return engine.catalog_remove_table(table.strip())


def api_highlight(body: dict) -> dict:
    """POST /api/highlight — pygments-guessed HTML for the editor panes.

    Mirrors rcat: the lexer is GUESSED from filename + content
    (``pygmentize -g`` = ``guess_lexer_for_filename``, NOT a bare content
    guess — on small snippets that misfires spectacularly, e.g. Objective-C
    for YAML). The pane's format seeds the virtual filename. Returns
    inline-styled HTML (no stylesheet to sync) and degrades to
    ``highlighted: false`` — never an error — without pygments or over the
    size cap. The client inserts the HTML into a highlight layer behind a
    transparent-text textarea.
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")  # noqa: TRY004
    text = body.get("text")
    if not isinstance(text, str):
        raise ValueError("Provide the text to highlight.")  # noqa: TRY004
    raw_theme = body.get("theme")
    theme = raw_theme if isinstance(raw_theme, str) and raw_theme in _PYG_STYLES else "dark"
    raw_fmt = body.get("format")
    fmt = raw_fmt if isinstance(raw_fmt, str) and raw_fmt in ("yaml", "json") else "yaml"
    if not _HAVE_PYGMENTS or len(text) > _HIGHLIGHT_MAX_CHARS:
        return {"html": None, "highlighted": False}
    try:
        try:
            # -g semantics: filename AND content, with a virtual filename.
            lexer = _guess_lexer_for_filename("sv-edit." + fmt, text)
        except Exception:
            lexer = _TextLexer()
        if isinstance(lexer, _TextLexer):
            # -g has no opinion (blank/comment-only or unknown content):
            # sniff so a YAML/JSON pane still colors sensibly.
            stripped = text.lstrip()
            lexer = _JsonLexer() if stripped[:1] in ("{", "[") else _YamlLexer()
        style = _PYG_STYLES[theme]
        # nowrap: bare token spans (inline colors), NO <div class=highlight><pre>
        # wrapper — the client inserts this into its own highlight <pre>, whose
        # metrics must stay identical to the caret textarea layered over it.
        # The wrapper's inline background/line-height would break that register.
        html = _pyg_highlight(text, lexer, _HtmlFormatter(noclasses=True, nowrap=True, style=style))
        return {"html": html, "highlighted": True, "lexer": type(lexer).__name__}
    except Exception:
        # Highlighting is cosmetic — a pygments hiccup must never break the
        # editor; the client falls back to its escaped-plain-text layer.
        return {"html": None, "highlighted": False}


# ---------------------------------------------------------------------------
# Starlette route wiring
# ---------------------------------------------------------------------------


def register_ui(app, engine_getter) -> None:
    """Add the UI page + read-only JSON API routes to the Starlette app.

    ``engine_getter`` is a zero-arg callable returning the process-wide
    ``SqlEngine`` (so the same caches back the UI and the MCP tools).
    """

    def html_page(_request) -> HTMLResponse:
        return HTMLResponse(_HTML)

    # Every handler below offloads its engine/manager work to a worker
    # thread (asyncio.to_thread): the JSON API handlers call the engine
    # synchronously, and without the offload a 30 s query would stall
    # /health, /metrics and every other route on the same event loop.

    async def status(_request) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(api_status, engine_getter()))

    def _caller_for(request):
        """The request's resolved Caller (identity spine); None-safe."""
        try:
            return _identity.caller_from_request_state(request)
        except Exception:
            return None

    async def tables(_request) -> JSONResponse:
        try:
            return JSONResponse(await asyncio.to_thread(api_tables, engine_getter(), _caller_for(_request)))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def describe(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse(
                await asyncio.to_thread(api_describe, engine_getter(), str(body.get("table", "")), _caller_for(request))
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def query(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        caller = _caller_for(request)
        try:
            result = await asyncio.to_thread(
                api_query,
                engine_getter(),
                str(body.get("sql", "")),
                body.get("limit"),
                body.get("params"),
                caller,
                body.get("format", "json"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        # format="arrow" renders to a plain-text IPC payload, not a dict.
        if isinstance(result, str):
            return Response(content=result, media_type="text/plain; charset=utf-8")
        return JSONResponse(result)

    async def preview(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    api_preview, engine_getter(), str(body.get("table", "")), body.get("limit"), _caller_for(request)
                )
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def profile(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            cols = body.get("columns")
            col_list = [str(c) for c in cols if str(c).strip()] if isinstance(cols, list) else None
            return JSONResponse(
                await asyncio.to_thread(
                    api_profile, engine_getter(), str(body.get("table", "")), col_list, _caller_for(request)
                )
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ---- async query jobs (long queries: submit, poll, paginate, cancel)
    manager = QueryJobManager()

    def _response(payload: dict) -> JSONResponse:
        """payload dicts may carry a "status" hint for the HTTP code."""
        status = payload.pop("status", None)
        return JSONResponse(payload, status_code=status if isinstance(status, int) else 200)

    async def query_async(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        try:
            # submit may block on the query-concurrency gate — never block
            # the event loop.
            return _response(await asyncio.to_thread(api_async_query, engine_getter(), manager, body))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LakehouseError as exc:
            # concurrency-gate refusals (queue wait expired) → 429
            return JSONResponse({"error": str(exc)}, status_code=429)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def query_job_status(request) -> JSONResponse:
        return _response(await asyncio.to_thread(api_query_status, manager, request.path_params["query_id"]))

    async def query_job_rows(request) -> JSONResponse:
        params = request.query_params
        return _response(
            await asyncio.to_thread(
                api_query_rows,
                manager,
                request.path_params["query_id"],
                params.get("offset", 0),
                params.get("limit", 100),
            )
        )

    async def query_job_cancel(request) -> JSONResponse:
        return _response(await asyncio.to_thread(api_query_cancel, manager, request.path_params["query_id"]))

    app.add_route("/", html_page, methods=["GET"])
    app.add_route("/ui", html_page, methods=["GET"])
    app.add_route("/ui/index.html", html_page, methods=["GET"])
    app.add_route("/api/status", status, methods=["GET"])
    app.add_route("/api/tables", tables, methods=["GET"])
    app.add_route("/api/describe", describe, methods=["POST"])
    app.add_route("/api/query", query, methods=["POST"])
    app.add_route("/api/query/async", query_async, methods=["POST"])
    app.add_route("/api/query/{query_id}", query_job_status, methods=["GET"])
    app.add_route("/api/query/{query_id}/rows", query_job_rows, methods=["GET"])
    app.add_route("/api/query/{query_id}", query_job_cancel, methods=["DELETE"])

    # ---- async query jobs (/api/jobs/*): the REST twins of the MCP
    # query_submit / query_status / query_result / query_cancel tools — the
    # SAME bounded registry (SQLHANDLER_MAX_JOBS), submit-time read-only
    # guard, watchdog-enforced query timeout, and fetch-once results.
    async def jobs_submit(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        try:
            result = await asyncio.to_thread(api_job_submit, engine_getter(), body)
        except ValueError as exc:  # read-only guard / payload validation
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LakehouseError as exc:  # concurrency-gate queue wait expired
            return JSONResponse({"error": str(exc)}, status_code=429)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        if result.get("error"):
            return JSONResponse({"error": result["error"]}, status_code=result.get("status", 429))
        return JSONResponse(result)

    async def jobs_status(request) -> JSONResponse:
        try:
            return JSONResponse(await asyncio.to_thread(api_job_status, request.path_params["job_id"]))
        except JobError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def jobs_result(request) -> JSONResponse:
        try:
            arrow = await asyncio.to_thread(api_job_result, request.path_params["job_id"])
        except JobError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        payload = arrow_to_payload(arrow)  # the job's rows are already MAX_ROWS-bounded
        payload.update(
            {
                "job_id": request.path_params["job_id"],
                "total_rows": arrow.num_rows,
                "result_fetched": True,
            }
        )
        return JSONResponse(payload)

    async def jobs_cancel(request) -> JSONResponse:
        try:
            return JSONResponse(await asyncio.to_thread(api_job_cancel, request.path_params["job_id"]))
        except JobError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    app.add_route("/api/jobs", jobs_submit, methods=["POST"])
    app.add_route("/api/jobs/{job_id}", jobs_status, methods=["GET"])
    app.add_route("/api/jobs/{job_id}/result", jobs_result, methods=["GET"])
    app.add_route("/api/jobs/{job_id}", jobs_cancel, methods=["DELETE"])

    # ---- saved parameterized queries (/api/saved-queries/*): the REST
    # twins of the query_save / query_list / query_delete / query_saved MCP
    # tools. WRITES (save/delete) verify the caller's credential per request
    # (mutation gate — see sqlhandler/saved.py); reads follow the /api
    # posture (SQLHANDLER_API_TOKEN middleware).
    async def saved_list(_request) -> JSONResponse:
        try:
            return JSONResponse({"queries": await asyncio.to_thread(api_saved_list)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def saved_create(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            entry = await asyncio.to_thread(api_saved_save, body, request)
        except NotAuthorized as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": f"Cannot write the saved-query store: {exc}"}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"saved": True, **entry})

    async def saved_delete(request) -> JSONResponse:
        try:
            result = await asyncio.to_thread(api_saved_delete, request.path_params["name"], request)
        except NotAuthorized as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        except UnknownSavedQuery as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse(result)

    async def saved_run(request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        limit = body.get("limit")
        if limit is not None and not isinstance(limit, int):
            return JSONResponse({"error": "limit must be an integer."}, status_code=400)
        try:
            sql, params, _entry = await asyncio.to_thread(api_saved_run, name, body)
        except UnknownSavedQuery as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LakehouseError as exc:  # concurrency-gate refusal
            return JSONResponse({"error": str(exc)}, status_code=429)
        try:
            arrow = await asyncio.to_thread(engine_getter().query_duckdb, sql, limit, params, body.get("version_as_of"))
        except (ValueError, LakehouseError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        payload = arrow_to_payload(arrow, limit=limit)
        payload.update({"query": name})
        return JSONResponse(payload)

    app.add_route("/api/saved-queries", saved_list, methods=["GET"])
    app.add_route("/api/saved-queries", saved_create, methods=["POST"])
    app.add_route("/api/saved-queries/{name}", saved_delete, methods=["DELETE"])
    app.add_route("/api/saved-queries/{name}/run", saved_run, methods=["POST"])

    async def export(request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            result = await asyncio.to_thread(api_export, engine_getter(), body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return Response(
            content=result["content"],
            media_type=result["media_type"],
            headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'},
        )

    app.add_route("/api/preview", preview, methods=["POST"])
    app.add_route("/api/profile", profile, methods=["POST"])
    app.add_route("/api/export", export, methods=["POST"])

    # ---- semantic catalog: status / upload (JSON or YAML) / clear ----
    async def semantic_catalog_get(_request) -> JSONResponse:
        try:
            return JSONResponse(await asyncio.to_thread(api_catalog_status, engine_getter()))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def semantic_catalog_upload(request) -> JSONResponse:
        body = await request.body()
        try:
            return JSONResponse({"ok": True, **await asyncio.to_thread(api_catalog_upload, engine_getter(), body)})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            # Unwritable store (read-only fs, bad path) — operator-actionable.
            return JSONResponse(
                {
                    "error": f"Cannot write the catalog store "
                    f"({engine_getter().catalog_status().get('store_path')}): {exc}. "
                    "Set SQLHANDLER_CATALOG_STORE to a writable path."
                },
                status_code=500,
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def semantic_catalog_delete(_request) -> JSONResponse:
        try:
            return JSONResponse({"ok": True, **await asyncio.to_thread(api_catalog_clear, engine_getter())})
        except OSError as exc:
            return JSONResponse({"error": f"Cannot remove the uploaded catalog: {exc}"}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def semantic_catalog_content(request) -> JSONResponse:
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    api_catalog_content, engine_getter(), request.query_params.get("format", "yaml")
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def semantic_catalog_table_get(request) -> JSONResponse:
        params = request.query_params
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    api_catalog_table,
                    engine_getter(),
                    params.get("table", ""),
                    params.get("format", "yaml"),
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def semantic_catalog_table_update(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        try:
            result = await asyncio.to_thread(
                api_catalog_table_update,
                engine_getter(),
                str(body.get("table", "")),
                body.get("content"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse(
                {
                    "error": f"Cannot write the catalog store "
                    f"({engine_getter().catalog_status().get('store_path')}): {exc}. "
                    "Set SQLHANDLER_CATALOG_STORE to a writable path."
                },
                status_code=500,
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, **result})

    async def semantic_catalog_table_delete(request) -> JSONResponse:
        try:
            result = await asyncio.to_thread(
                api_catalog_table_remove, engine_getter(), request.query_params.get("table", "")
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": f"Cannot write the catalog store: {exc}"}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, **result})

    async def highlight(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse(await asyncio.to_thread(api_highlight, body))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    app.add_route("/api/semantic-catalog", semantic_catalog_get, methods=["GET"])
    app.add_route("/api/semantic-catalog", semantic_catalog_upload, methods=["POST"])
    app.add_route("/api/semantic-catalog", semantic_catalog_delete, methods=["DELETE"])
    app.add_route("/api/semantic-catalog/content", semantic_catalog_content, methods=["GET"])
    app.add_route("/api/semantic-catalog/table", semantic_catalog_table_get, methods=["GET"])
    app.add_route("/api/semantic-catalog/table", semantic_catalog_table_update, methods=["POST"])
    app.add_route("/api/semantic-catalog/table", semantic_catalog_table_delete, methods=["DELETE"])

    # ---- dbt manifest import: preview (parse-only) + apply (writes through
    # the same upload store as POST /api/semantic-catalog). Two routes, one
    # body shape — the UI previews first, then the operator clicks Apply.
    async def semantic_catalog_dbt_import(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse({"ok": True, **await asyncio.to_thread(api_dbt_import, engine_getter(), body)})
        except ValueError as exc:  # manifest / option validation
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def semantic_catalog_dbt_import_apply(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not engine_getter().catalog_uploads_enabled:
            # Fail BEFORE parsing when the whole surface is switched off —
            # same message the plain upload route gives.
            return JSONResponse(
                {"error": "Semantic-catalog upload is disabled (SQLHANDLER_CATALOG_UPLOAD=0)."}, status_code=400
            )
        try:
            return JSONResponse({"ok": True, **await asyncio.to_thread(api_dbt_import_apply, engine_getter(), body)})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse(
                {
                    "error": f"Cannot write the catalog store "
                    f"({engine_getter().catalog_status().get('store_path')}): {exc}. "
                    "Set SQLHANDLER_CATALOG_STORE to a writable path."
                },
                status_code=500,
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    app.add_route("/api/semantic-catalog/import-dbt", semantic_catalog_dbt_import, methods=["POST"])
    app.add_route("/api/semantic-catalog/import-dbt/apply", semantic_catalog_dbt_import_apply, methods=["POST"])
    app.add_route("/api/highlight", highlight, methods=["POST"])

    # ---- MCP Inspector tab (/api/inspector/*): a web bridge to the SAME tool
    # surface an MCP client sees. /tools serves the tools/list payload (name,
    # description, inputSchema — from server.mcp_tool_specs, one source of
    # truth); /call dispatches through the server's OWN tools/call dispatcher
    # so identity, policy, caching, the audit trail and the saved-query write
    # gate ride along byte-identically — the browser is just another MCP
    # client that happens to speak JSON instead of JSON-RPC. Tool errors come
    # back MCP-shaped (isError:true content, HTTP 200 — an inspector shows
    # tool errors); only BRIDGE validation (bad JSON body, non-dict
    # arguments) is an HTTP 4xx. Auth is the shared /api posture
    # (SQLHANDLER_API_TOKEN middleware / gateway) — never the /mcp keys.
    #
    # _tool_dispatcher is injected by server._build_http_app (the same
    # engine_getter injection pattern); the default keeps register_ui's
    # existing call sites and tests working by importing the dispatcher
    # lazily (no import cycle at module load).
    def _tool_dispatcher() -> object:
        try:
            from .server import _dispatch_tool  # lazy: avoids the import cycle

            return _dispatch_tool
        except Exception:  # pragma: no cover - server always present in prod
            return None

    async def inspector_tools(_request) -> JSONResponse:
        try:
            from .server import mcp_tool_specs  # lazy: avoids the import cycle

            return JSONResponse({"tools": await asyncio.to_thread(mcp_tool_specs)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def inspector_call(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "Provide the tool 'name' to call."}, status_code=400)
        args = body.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return JSONResponse({"error": "'arguments' must be a JSON object."}, status_code=400)
        dispatch = _tool_dispatcher()
        if dispatch is None:  # pragma: no cover - defensive
            return JSONResponse({"error": "Tool dispatcher unavailable."}, status_code=500)
        # The request rides along EXACTLY as the /mcp transport hands it to
        # tools/call: the dispatcher reads the caller identity off its scope
        # state (audit + policy) and query_save/query_delete verify the
        # presented credential per call (the mutation gate).
        try:
            text, is_error = await asyncio.to_thread(dispatch, name.strip(), args, request)
        except Exception as exc:  # dispatcher-level failure → MCP-shaped error
            text, is_error = f"Tool dispatch failed: {exc}", True
        return JSONResponse({"content": [{"type": "text", "text": text}], "isError": is_error})

    app.add_route("/api/inspector/tools", inspector_tools, methods=["GET"])
    app.add_route("/api/inspector/call", inspector_call, methods=["POST"])
