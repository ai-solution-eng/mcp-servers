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
  GET  /api/whoami    -> the caller's own identity preview (class/subject/fp,
                          which ladder rung resolved, `authenticated`) — the
                          REST twin of the MCP whoami tool (header widget probe)
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

  Admin API — the administration plane (admin-designation gated via
  server.require_admin: 401 anonymous / 403 non-admin; the REST twins of
  the admin_* MCP tools):
  GET    /api/admin/grants     -> admins, datasets doc, policy_text (the WHOLE
                                  authored policy as YAML — the editor's
                                  prefill), assignments, blocked, groups,
                                  policy_hash, keys (minted KeyEntries
                                  + Secret keys fp-only, source:"secret")
  PUT    /api/admin/policy     -> replace the policy document (JSON or YAML;
                                  validated first; atomic write; returns the
                                  new hash)
  POST   /api/admin/keys       -> mint one key {label, assign?} — the raw key
                                  is returned ONCE (201)
  DELETE /api/admin/keys/{fp}  -> revoke one minted key + its assignment
                                  (Secret-managed fps: 409; unknown: 404)

The HTML page is served at ``/`` and ``/ui``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json as _json
import logging
import math
import os
import re
import time as _time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import identity as _identity
from . import oidc_identity as _oidc_identity
from . import policy as _policy
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

#: The MCP tool dispatcher bridged from server.py (``_dispatch_tool``): the
#: inspector's tool-call route offloads it to a worker thread verbatim.
ToolDispatcher = Callable[[str, dict, Any], tuple[str, bool]]

_DEFAULT_LIMIT = 100
# Fallback UI cap when SQLHANDLER_MAX_ROWS is unset or 0 (unlimited for the
# MCP path): the browser API still needs a bounded payload size.
_FALLBACK_MAX_LIMIT = 1000

# Hard cap on any single API request body (POST /api/semantic-catalog,
# /api/semantic-catalog/import-dbt[/apply], ...). Generous but bounded: a
# manifest or catalog near this size is a legitimate upload; anything larger
# is abuse or a mistake and must be refused BEFORE it is read into memory
# (unbounded ``await request.body()`` on an open port is a memory-DoS lever).
_BODY_READ_MAX_BYTES = 8 * 1024 * 1024

logger = logging.getLogger("sqlhandler.webui")


class _BodyTooLarge(Exception):
    """Raised by :func:`_read_bounded_body` when the body cap is exceeded."""


async def _read_bounded_body(request, max_bytes: int = _BODY_READ_MAX_BYTES) -> bytes:
    """Read one request body under a hard byte cap (memory-DoS guard).

    Checks Content-Length first (refuses 413 without reading a byte) and
    otherwise reads the body in chunks, refusing 413 as soon as the cap is
    exceeded — so a chunked/unknown-length upload cannot buffer past the
    cap either. Returns the body bytes; raises :class:`_BodyTooLarge` when
    the cap is hit (the routes translate that into the 413 JSON error).
    """
    try:
        content_length = request.headers.get("content-length")
    except Exception:
        content_length = None
    if content_length is not None:
        try:
            declared = int(str(content_length).strip())
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > max_bytes:
            raise _BodyTooLarge()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _BodyTooLarge()
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


_HTML = (Path(__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# security headers (the /ui HTML + JSON API responses)
# ---------------------------------------------------------------------------


# The single HTML page inlines exactly two <script> blocks (the theme
# bootstrap and the app). They are static bytes of a static file, so the CSP
# pins them by sha256 computed from the SAME bytes the page serves — no
# unsafe-inline, no nonces, nothing to misconfigure at runtime. If the UI
# gains a third inline script or an inline event handler, the browser will
# block it and this hash list must be regenerated (deliberate tripwire).
def _ui_script_hashes() -> list[str]:
    import base64
    import hashlib
    import re as _re

    hashes: list[str] = []
    for m in _re.finditer(r"<script\b[^>]*>(.*?)</script>", _HTML, _re.DOTALL):
        body = m.group(1)
        if body.strip():
            # CSP sha256- tokens are BASE64 of the digest, not hex — the
            # browser compares base64(sha256(script_bytes)) literally, and a
            # hex token simply never matches (both scripts blocked, page
            # renders but stays fully inert). Verified against the spec's
            # hash-source algorithm.
            digest = hashlib.sha256(body.encode("utf-8")).digest()
            hashes.append(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


_UI_SCRIPT_HASHES = _ui_script_hashes()

_CSP = (
    "default-src 'none'; "
    f"script-src 'self' {' '.join(_UI_SCRIPT_HASHES) if _UI_SCRIPT_HASHES else chr(39) + 'self' + chr(39)}; "
    "style-src 'self' 'unsafe-inline'; "  # the page carries a large inline <style> block
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


class _SecurityHeadersMiddleware:
    """CSP + hardening headers on every response (audit quick-win fix).

    The UI page is the only HTML surface and holds the admin key in a JS
    variable; a strict CSP (script hashes, no frame ancestors, no base-uri)
    turns any future DOM-XSS from an admin-key exfiltration into a blocked
    request. JSON API responses are equally covered (nosniff + frame deny).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {k.lower() for k, _v in headers}
                add = [
                    (b"content-security-policy", _CSP.encode("latin-1")),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                for k, v in add:
                    if k not in existing:
                        headers.append((k, v))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _BodyLimitMiddleware:
    """Reject request bodies larger than the cap BEFORE they are buffered.

    Systemic backstop for every POST/PUT route (the per-route
    ``_read_bounded_body`` caps stay — this is the outer net): a chunked or
    lying-Content-Length upload is counted as bytes flow through ``receive``
    and refused once over the limit, so no route can be driven into
    unbounded buffering. Mechanism: raise Starlette's HTTPException(413)
    from the receive wrapper — the exception propagates out of the route's
    ``await request.json()`` and the stack's own ExceptionMiddleware (which
    wraps the router) renders the single, consistent 413 response. The app
    cannot have started a response yet (it is still blocked on the body),
    so there is no double-send risk. Responses are untouched.
    """

    _MAX_BYTES = _BODY_READ_MAX_BYTES

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT"):
            await self.app(scope, receive, send)
            return

        seen = 0

        async def receive_wrapper():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._MAX_BYTES:
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, receive_wrapper, send)


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


def _arrow_to_csv_bytes(arrow) -> bytes:
    """CSV-export an Arrow Table via pyarrow's native writer (no pandas).

    Bench follow-up (2026-09): the pandas round-trip (to_pandas().to_csv)
    was ~10x slower on a 200k-row export (297 ms vs 30 ms) and materialized
    a full DataFrame for the privilege. The Arrow writer's output differs
    from pandas' in exactly these deliberate ways, all parse-equivalent for
    CSV consumers (Excel, pandas.read_csv, duckdb read_csv_auto):

    * integers export EXACT ('4611686018427387904') where pandas upcast
      null-bearing int columns to float64 ('4.61...e+18') — the pandas
      behavior was a lossy artifact, not a feature;
    * booleans as 'True'/'False' (pandas' capitalization, kept by casting
      through string — Arrow's native 'true'/'false' would be a silent
      consumer-visible change);
    * float NaN exports as '' (pandas' na_rep default) instead of 'nan';
    * tz-aware timestamps use 'Z' where pandas used '+00:00' (both
      ISO-8601, identical when parsed).

    Falls back to the pandas path for anything Arrow's CSV writer refuses
    (nested types cannot appear in CSV anyway; the guard is cheap parity).
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    cols = []
    names = []
    for f in arrow.schema:
        col = arrow.column(f.name)
        if pa.types.is_boolean(f.type):
            col = pc.if_else(col, "True", "False")
        elif pa.types.is_floating(f.type):
            # pandas' to_csv writes NaN as ''; Arrow writes 'nan' — replace
            # NaN with null so the writer emits '' like pandas did.
            nan_mask = pc.fill_null(pc.is_nan(col), False)
            col = pc.if_else(nan_mask, pa.scalar(None, f.type), col)
        cols.append(col)
        names.append(f.name)
    normalized = pa.table(cols, names=names)
    sink = io.BytesIO()
    try:
        pa.csv.write_csv(
            normalized,
            sink,
            write_options=pa.csv.WriteOptions(quoting_header="none"),
        )
        return sink.getvalue()
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        # Arrow CSV writer refused (exotic type) — the historical path.
        return arrow.to_pandas().to_csv(index=False).encode("utf-8")


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

    def submit(self, engine: SqlEngine, sql: str, limit=None, params=None, version_as_of=None, caller=None) -> dict:
        """Validate + start a job; returns {"query_id", "state"}.

        Validation happens BEFORE the job starts, so a bad payload is a
        synchronous error, not a job that immediately fails. `caller` rides
        the QueryJob (identity spine, thread-boundary rule): without it the
        engine treats the async run as the trusted internal path — masks,
        row policies and hidden-table enforcement would never apply, even
        though the sync /api/query applies them.
        """
        _validate_params(params)
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        self._cleanup()
        if len(self._jobs) >= self._max_jobs:
            return {"error": "Too many tracked queries; retry later.", "status": 429}
        job = QueryJob(engine, sql, limit=limit, params=params, version_as_of=version_as_of, caller=caller)
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


def api_async_query(engine: SqlEngine, manager: QueryJobManager, body: dict, *, caller=None) -> dict:
    """POST /api/query/async — start a read-only query, return a job id.

    `caller` rides the job (identity spine): an async run must be masked /
    policy-scoped exactly like the sync /api/query — without it the engine
    takes caller=None for the trusted internal path and skips enforcement.
    """
    sql = str(body.get("sql", ""))
    safe = assert_readonly(sql)  # ValueError -> 400 (route wrapper)
    result = manager.submit(
        engine,
        safe,
        limit=body.get("limit"),
        params=body.get("params"),
        version_as_of=body.get("version_as_of"),
        caller=caller,
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


def api_export(engine: SqlEngine, body: dict, *, caller=None) -> dict:
    """POST /api/export — download a query or table result as CSV/Parquet/Arrow.

    Accepts {"sql": ...} (read-only guard applies) or {"table": ...}
    (preview-style scan), optional "limit" (clamped to
    SQLHANDLER_EXPORT_MAX_ROWS) and "format" ("csv" | "parquet" | "arrow").
    Returns {content: bytes, media_type, filename} for the route to send as
    an attachment.

    ``caller`` (keyword-only, identity spine — the same resolved Caller
    /api/query threads into engine.query_duckdb): rides into the engine call
    so row policies and column masks apply to exported rows too. Without it
    the export would silently bypass the caller's policy.
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
        arrow = engine.query_duckdb(safe, limit=cap, row_cap=cap, caller=caller)
        name = "query"
    elif table:
        arrow = engine.scan_arrow(str(table), limit=cap, caller=caller)
        name = str(table).replace("/", "_")
    else:
        raise ValueError("Provide either 'sql' or 'table' to export.")

    if fmt == "csv":
        content = _arrow_to_csv_bytes(arrow)
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

    # ---- GET /api/whoami — the header identity widget's probe (the REST
    # twin of the MCP whoami tool, preview shape only — never a grant).
    # Resolution mirrors the ADMIN surface (an explicitly presented
    # X-API-Key / Bearer is authenticated HERE; a wrong key stays
    # anonymous — never redeemed as the browser user behind it), a
    # keyless request resolves through the full ladder (SSO bearer JWT →
    # browser headers). Public like /api/status: the response is the
    # caller's OWN audit-safe shape (class/subject/fp — never key
    # material, never an admin-designation answer), so it leaks nothing
    # an unauthenticated caller could not already learn. Un-gated even
    # under requireIdentity (the UI shell is un-gated and this only
    # previews what a gated call would resolve to — it is the page's way
    # to ASK, not a way past the gate).
    async def whoami(request) -> JSONResponse:
        try:
            from .server import _whoami_rest_payload

            return JSONResponse(await asyncio.to_thread(_whoami_rest_payload, request))
        except Exception as exc:  # never 500 the identity probe
            return JSONResponse({"authenticated": False, "error": str(exc)})

    def _caller_for(request):
        """The request's resolved Caller (identity spine); None-safe."""
        try:
            return _identity.caller_from_request_state(request)
        except Exception:
            return None

    def _job_owner_for(request):
        """The caller's owner scope for async jobs (or None = unowned).

        The SAME derivation as the MCP dispatch's _job_owner / the
        saved-query write gate: policy.owner_key(caller) under enforcement,
        None otherwise (enforcement off keeps the historical shared
        posture). jobs.py compares; it never derives identities.
        """
        caller = _caller_for(request)
        if caller is not None and _policy.policy_enabled():
            return _policy.owner_key(caller)
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
        # (api_query returns `dict | str`; the arrow branch is the `str` one —
        # a plain Response, not a JSONResponse, is correct there.)
        if isinstance(result, str):
            return Response(content=result, media_type="text/plain; charset=utf-8")  # type: ignore[return-value]
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
            # the event loop. caller rides the job so masking/policy apply.
            return _response(
                await asyncio.to_thread(api_async_query, engine_getter(), manager, body, caller=_caller_for(request))
            )
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

    # ---- Browser SSO (D22, the MM-RAG approach ported verbatim): the app is
    # an OIDC CLIENT — /oauth/login redirects to the realm, the callback
    # exchanges + VERIFIES the token with the same D21 machinery every other
    # credential goes through, and plants an HttpOnly session cookie. The
    # identity ladder treats that cookie as the LOWEST-priority envelope
    # (an explicit key or Bearer always outranks it), so SSO resolves the
    # VERIFIED preferred_username — what the browser-header rung cannot
    # know (it only sees the IdP sub UUID). Inert by default: without
    # SQLHANDLER_OIDC_SSO_* fully configured these routes 404 and no cookie
    # is ever accepted (byte-identical to pre-D22). Redirects on failure —
    # never error bodies (no error detail leaked to the browser).
    async def oauth_login(request):
        from starlette.responses import RedirectResponse

        from . import oidc_sso

        if not oidc_sso.sso_enabled():
            return JSONResponse({"error": "SSO is not enabled on this deployment"}, status_code=404)
        url = await asyncio.to_thread(oidc_sso.build_authorization_url, oidc_sso.new_state())
        if not url:
            return JSONResponse({"error": "OIDC provider discovery unavailable — try again shortly"}, status_code=503)
        # The state cookie value is "<state>|<safe-next>" — the callback
        # splits it back (never trusting the query string for the target).
        state = url.split("state=")[-1]
        target = oidc_sso.safe_next_path(request.query_params.get("next", "/ui"))
        resp = RedirectResponse(url, status_code=302)
        secure = oidc_sso.cookie_secure()
        parts = [
            f"{oidc_sso.state_cookie_name()}={state}|{target}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=600",
        ]
        if secure:
            parts.append("Secure")
        resp.headers.append("set-cookie", "; ".join(parts))
        return resp

    async def oauth_callback(request):
        from starlette.responses import RedirectResponse

        from . import oidc_sso

        if not oidc_sso.sso_enabled():
            return JSONResponse({"error": "SSO is not enabled on this deployment"}, status_code=404)
        params = request.query_params
        code, state = params.get("code", ""), params.get("state", "")
        if not code or not state:
            return RedirectResponse("/ui?sso=error", status_code=302)
        # Split the state cookie: <nonce>|<safe-next>.
        raw_state = request.cookies.get(oidc_sso.state_cookie_name(), "")
        cookie_state, _, cookie_target = raw_state.partition("|")
        if not oidc_sso.state_matches(cookie_state, state):
            return RedirectResponse("/ui?sso=error", status_code=302)
        token_response = await asyncio.to_thread(oidc_sso.exchange_code, code)
        if not token_response:
            return RedirectResponse("/ui?sso=error", status_code=302)
        token = str(token_response.get("access_token") or "")
        if not token:
            return RedirectResponse("/ui?sso=error", status_code=302)
        # Verify with the D21 machinery — the SAME pipeline every credential
        # goes through (RS256/JWKS, iss/aud/exp via the vendored fork). An
        # unverifiable token never becomes a session.
        try:
            claims = await asyncio.to_thread(_oidc_identity.verify_and_decode, token)
        except Exception:
            claims = None
        if not claims:
            return RedirectResponse("/ui?sso=error", status_code=302)
        target = oidc_sso.safe_next_path(cookie_target or "/ui")
        resp = RedirectResponse(target, status_code=302)
        secure = oidc_sso.cookie_secure()
        parts = [
            f"{oidc_sso.cookie_name()}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={oidc_sso.cookie_max_age_for(token)}",
        ]
        if secure:
            parts.append("Secure")
        resp.headers.append("set-cookie", "; ".join(parts))
        # Clear the state cookie (its one job is done).
        resp.headers.append(
            "set-cookie",
            f"{oidc_sso.state_cookie_name()}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )
        subject = _oidc_identity.subject_from_claims(claims) or "unknown"
        logger.info("SSO sign-in: subject '%s' session established", subject)
        return resp

    async def oauth_logout(request):
        from starlette.responses import RedirectResponse

        from . import oidc_sso

        resp = RedirectResponse("/ui", status_code=302)
        for name in (oidc_sso.cookie_name(), oidc_sso.state_cookie_name()):
            resp.headers.append(
                "set-cookie",
                f"{name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
        return resp

    app.add_route("/oauth/login", oauth_login, methods=["GET"])
    app.add_route("/oauth/oidc/callback", oauth_callback, methods=["GET"])
    app.add_route("/oauth/logout", oauth_logout, methods=["GET"])

    app.add_route("/", html_page, methods=["GET"])
    app.add_route("/ui", html_page, methods=["GET"])
    app.add_route("/ui/index.html", html_page, methods=["GET"])
    app.add_route("/api/status", status, methods=["GET"])
    app.add_route("/api/whoami", whoami, methods=["GET"])
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
            # caller + owner ride the job (identity spine): masking/policy
            # enforcement on the async run + owner-scoped result access,
            # exactly like the MCP query_submit twin.
            result = await asyncio.to_thread(
                api_job_submit,
                engine_getter(),
                body,
                caller=_caller_for(request),
                owner=_job_owner_for(request),
            )
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
    async def saved_list(request) -> JSONResponse:
        try:
            # caller scopes the listing (enforcement ON → own entries only),
            # exactly like the MCP list twin — without it every caller sees
            # every subject's saved queries.
            return JSONResponse({"queries": await asyncio.to_thread(api_saved_list, caller=_caller_for(request))})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def saved_create(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            entry = await asyncio.to_thread(api_saved_save, body, request, caller=_caller_for(request))
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
            result = await asyncio.to_thread(
                api_saved_delete, request.path_params["name"], request, caller=_caller_for(request)
            )
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
            # caller scopes the lookup too: running another subject's saved
            # query must 404 under enforcement, not just mask the output.
            sql, params, _entry = await asyncio.to_thread(api_saved_run, name, body, caller=_caller_for(request))
        except UnknownSavedQuery as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LakehouseError as exc:  # concurrency-gate refusal
            return JSONResponse({"error": str(exc)}, status_code=429)
        try:
            # caller rides exactly like /api/query: without it the engine's
            # masking views / row policies never apply to a saved-query run.
            arrow = await asyncio.to_thread(
                engine_getter().query_duckdb,
                sql,
                limit,
                params,
                body.get("version_as_of"),
                caller=_caller_for(request),
            )
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
            # The resolved caller rides into the engine call (api_export) so
            # masking/row policies apply to exported rows too — same spine
            # as /api/query.
            result = await asyncio.to_thread(api_export, engine_getter(), body, caller=_caller_for(request))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        # Header-safe filename: strip quotes/backslashes/control chars so a
        # crafted table name can't break out of the quoted value or inject
        # CR/LF into the response headers (the filename is a convenience,
        # never trusted input).
        safe_name = re.sub(r'["\\\r\n]', "", str(result["filename"])) or "export"
        return Response(
            content=result["content"],
            media_type=result["media_type"],
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
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
        try:
            body = await _read_bounded_body(request)
        except _BodyTooLarge:
            return JSONResponse(
                {"error": f"Request body too large (cap is {_BODY_READ_MAX_BYTES} bytes)."}, status_code=413
            )
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
            raw = await _read_bounded_body(request)
        except _BodyTooLarge:
            return JSONResponse(
                {"error": f"Request body too large (cap is {_BODY_READ_MAX_BYTES} bytes)."}, status_code=413
            )
        try:
            body = _json.loads(raw.decode("utf-8"))
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
            raw = await _read_bounded_body(request)
        except _BodyTooLarge:
            return JSONResponse(
                {"error": f"Request body too large (cap is {_BODY_READ_MAX_BYTES} bytes)."}, status_code=413
            )
        try:
            body = _json.loads(raw.decode("utf-8"))
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
    def _tool_dispatcher() -> ToolDispatcher | None:
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
        t0 = _time.perf_counter()
        try:
            result: tuple[str, bool] = await asyncio.to_thread(dispatch, name.strip(), args, request)
            text, is_error = result
        except Exception as exc:  # dispatcher-level failure → MCP-shaped error
            text, is_error = f"Tool dispatch failed: {exc}", True
        # Time-to-result (perf review 2026-09): the inspector displays the
        # tool call's wall time (its slow-vs-cached story is the point of
        # the tab), and the response names the serving replica so an
        # operator correlating "same call, sometimes fast" with the L2
        # shared tier knows WHICH pod answered.
        payload = {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
            "duration_ms": round((_time.perf_counter() - t0) * 1000, 1),
        }
        headers = {}
        pod = os.environ.get("SQLHANDLER_POD_NAME") or os.environ.get("HOSTNAME") or ""
        if pod:
            headers["X-Sqlhandler-Pod"] = pod
        return JSONResponse(payload, headers=headers)

    app.add_route("/api/inspector/tools", inspector_tools, methods=["GET"])
    app.add_route("/api/inspector/call", inspector_call, methods=["POST"])

    # ---- Admin API (/api/admin/*): the administration-plane REST surface —
    # the REST twins of the admin_grants / admin_policy_set / admin_key_mint
    # / admin_key_revoke MCP tools (the SAME cores in server.py run under
    # both, so the REST and MCP contracts cannot drift). GATED by
    # server.require_admin (D1): authenticate the presented key FIRST
    # (401 anonymous — the /api surface has no key middleware, so the admin
    # routes read the credential themselves), THEN authorize against the
    # policy's admins list (403 non-admin). This surface is exempt from the
    # identity-required gate (self-gated, strictly stronger — see
    # _IdentityRequiredMiddleware.SELF_GATED_PREFIX).
    from .server import AdminHTTPError as _AdminHTTPError
    from .server import require_admin as _require_admin

    def _admin_response(exc: _AdminHTTPError) -> JSONResponse:
        # A 401 carries WWW-Authenticate: Bearer (RFC 7235 — the credential
        # schemas this surface accepts), matching the /mcp gate's posture.
        return JSONResponse(exc.body, status_code=exc.status, headers=exc.headers)

    async def admin_grants(request) -> JSONResponse:
        try:
            _require_admin(request)
            from .server import _admin_grants_payload

            return JSONResponse(await asyncio.to_thread(_admin_grants_payload))
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def admin_policy_set(request) -> JSONResponse:
        try:
            caller = _require_admin(request)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        try:
            from .server import _admin_created_by, _admin_write_policy, _audit_admin_event

            result = await asyncio.to_thread(_admin_write_policy, str(body.get("policy", body)))
            _audit_admin_event("admin.policy_set", policy_hash=result["policy_hash"], by=_admin_created_by(caller))
            return JSONResponse(result)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def admin_key_mint(request) -> JSONResponse:
        try:
            caller = _require_admin(request)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        label = body.get("label")
        assign = body.get("assign")
        if assign is not None and not isinstance(assign, list):
            return JSONResponse({"error": "'assign' must be a list of dataset globs."}, status_code=400)
        try:
            from .server import _admin_created_by, _admin_mint_key

            result = await asyncio.to_thread(
                _admin_mint_key,
                str(label) if label else "",
                [str(g) for g in assign if str(g).strip()] if isinstance(assign, list) else None,
                _admin_created_by(caller),
            )
            return JSONResponse(result, status_code=201)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def admin_key_revoke(request) -> JSONResponse:
        try:
            caller = _require_admin(request)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        try:
            from .server import _admin_created_by, _admin_revoke_key

            result = await asyncio.to_thread(
                _admin_revoke_key,
                str(request.path_params["fp"]).strip(),
                _admin_created_by(caller),
            )
            return JSONResponse(result)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    app.add_route("/api/admin/grants", admin_grants, methods=["GET"])
    app.add_route("/api/admin/policy", admin_policy_set, methods=["PUT"])
    app.add_route("/api/admin/keys", admin_key_mint, methods=["POST"])
    app.add_route("/api/admin/keys/{fp}", admin_key_revoke, methods=["DELETE"])

    # ---- Self-service keys (the SSO user's own long-lived X-API-KEY) —
    # NOT admin-gated: the caller is the subject. The route re-resolves the
    # caller from the request (the middleware already did the JWT
    # verification) and the CORE re-checks via=="jwt" — the browser sends
    # the SSO bearer it already holds. /api/admin/users + /api/admin/users/
    # {subject}/grants ARE admin-gated (require_admin, same as the other
    # admin routes). All of these are inside the /api/admin/* prefix, which
    # the identity-required gate exempts (self-gated, strictly stronger).

    async def selfservice_keys(request) -> JSONResponse:
        try:
            caller = _caller_for(request)
            from .server import _admin_keys_list_for_subject

            return JSONResponse(await asyncio.to_thread(_admin_keys_list_for_subject, caller))
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def selfservice_mint(request) -> JSONResponse:
        try:
            caller = _caller_for(request)
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
            label = body.get("label")
            assign = body.get("assign")
            if assign is not None and not isinstance(assign, list):
                return JSONResponse({"error": "'assign' must be a list of dataset globs."}, status_code=400)
            from .server import _self_mint_key

            result = await asyncio.to_thread(
                _self_mint_key,
                caller,
                str(label) if label else "",
                [str(g) for g in assign if str(g).strip()] if isinstance(assign, list) else None,
            )
            return JSONResponse(result, status_code=201)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def selfservice_revoke(request) -> JSONResponse:
        try:
            caller = _caller_for(request)
            from .server import _self_revoke_key

            result = await asyncio.to_thread(_self_revoke_key, caller, str(request.path_params["fp"]).strip())
            return JSONResponse(result)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def admin_users(request) -> JSONResponse:
        try:
            _require_admin(request)
            from .server import _admin_users_payload

            return JSONResponse(await asyncio.to_thread(_admin_users_payload))
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def admin_user_grants(request) -> JSONResponse:
        try:
            caller = _require_admin(request)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        try:
            from .server import _admin_assign_user_grants

            result = await asyncio.to_thread(
                _admin_assign_user_grants,
                caller,
                str(request.path_params["subject"]),
                # A missing "globs" is the revoke-all shape: an empty list
                # takes the same DROP-the-subject path as `[]` downstream.
                body.get("globs") or [],
            )
            return JSONResponse(result)
        except _AdminHTTPError as exc:
            return _admin_response(exc)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    app.add_route("/api/admin/keys/self", selfservice_keys, methods=["GET"])
    app.add_route("/api/admin/keys/self", selfservice_mint, methods=["POST"])
    app.add_route("/api/admin/keys/self/{fp}", selfservice_revoke, methods=["DELETE"])
    app.add_route("/api/admin/users", admin_users, methods=["GET"])
    app.add_route("/api/admin/users/{subject}/grants", admin_user_grants, methods=["PUT"])
