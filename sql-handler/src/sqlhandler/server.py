"""SQLhandler MCP server - direct SQL access to columnar data, backend-agnostic.

Instead of round-tripping rows through the EzPresto/PrestoDB JDBC bridge, the
server exposes each table as a pyarrow Dataset and executes SQL with DuckDB
over it, pushing predicates and column projection down to the Parquet/Delta scan.

Backends are pluggable via SQLHANDLER_BACKEND:
  * onelake (default) - Microsoft Fabric OneLake (Delta Lake over ABFS)
  * s3 / minio        - S3-compatible object storage (Parquet files)
  * iceberg           - Apache Iceberg through a REST/SQL catalog

Transport
---------
The server is built on the LOW-LEVEL MCP 2.0 Server (mcp.server.lowlevel.Server),
which speaks the interoperable streamable-http protocol including the standard
initialize handshake. Any MCP client (DSH assistants, the official Python/TS
SDKs, MCP Inspector, Codex, Claude Code, etc.) can connect. The higher-level
MCPServer shipped in mcp>=2.0.0 only implements the newer stateless
per-request protocol and rejects the initialize handshake, which blocks
standard tools; we deliberately avoid that class here.

Tools exposed:
  * list_tables        - enumerate the tables in the configured source
  * search_tables      - keyword search over names/columns/catalog descriptions
  * describe_table     - inspect columns/types of a table
  * profile_table      - column-level statistics (min/max, null %, distinct,
                         quantiles) so agents write correct filters first try
  * run_sql            - execute SQL via DuckDB (aggregations etc.); output
                         as markdown, JSON, CSV or Arrow IPC
  * scan_table         - pull rows via pyarrow with column projection + limit
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import threading

import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response

from . import __version__, mcp_resources, observability
from . import errors as _errors
from . import identity as _identity
from . import jobs as _jobs
from . import saved as _saved
from . import writes as _writes
from .config import (
    load_backend_config,
    load_cache_config,
    load_compression_config,
    load_dotenv,
    load_source_providers,
)
from .engine import SqlEngine, _max_rows
from .jobs import JobError
from .provider import make_provider
from .rawfiles import is_raw_format
from .sqlguard import assert_mcp_readonly, mcp_readonly_enabled
from .webui import register_ui

logger = logging.getLogger("sqlhandler")

# --------------------------------------------------------------------------
# MCP server (standard MCP, interoperable initialize handshake)
# --------------------------------------------------------------------------


async def _handle_list_tools(ctx, params) -> ListToolsResult:
    """Return tools/list results (wired onto the low-level Server below)."""
    return ListToolsResult(tools=_TOOLS)


def _required_args(name: str) -> tuple[str, ...]:
    """The required argument names a tool's ADVERTISED input schema declares.

    tools/list (``_TOOLS``) is the single source of truth — every MCP client
    builds its calls from that schema, so validating the dispatch against the
    same declaration keeps the advertised contract and the enforced contract
    byte-identical by construction (no second table to drift).
    """
    for tool in _TOOLS:
        if tool.name == name:
            return tuple(tool.input_schema.get("required") or ())
    return ()


def _param_invalid(name: str, args: dict) -> tuple[str, list[str]] | None:
    """(message, fix_hints) when REQUIRED advertised args are absent, else None.

    Closes the silent-coercion gap: dispatch reads every argument with
    ``args.get(key, "")``-style defaults, so a call that mis-keys a required
    argument (e.g. ``run_sql {"query": ...}`` from a stale client manifest)
    used to arrive as an empty string and surface as a MASKED downstream
    error — run_sql reported the sqlguard message "Empty SQL statement.",
    which reads like a server bug, not a client-side contract violation.

    The check runs BEFORE the dispatch body, only when a required key is
    missing entirely, and names the expected shape (with a did-you-mean for
    mis-keyed calls) — the structured-errors contract. Never fires for
    explicitly empty strings (the tools' own internal checks keep governing
    those) and never fires for tools with no required args. The code is
    pinned at the call site via :func:`errors.structured` — the generic
    message classifier cannot know this is a parameter error.
    """
    required = _required_args(name)
    missing = [key for key in required if key not in args]
    if not missing:
        return None
    sent = sorted(args)
    received = ", ".join(sent) or "none"
    expected = ", ".join(f"{k!r}" for k in required)
    message = (
        f"Missing required argument(s) {missing} for tool {name!r} "
        f"(sent: {received}; the advertised schema requires: {expected})."
    )
    hints = [
        f"Call {name} with "
        + json.dumps({k: "<value>" for k in required})
        + " — the tools/list schema is the contract."
    ]
    if sent:
        hints.append(
            f"Sent key(s) [{received}] — did you mean to name it/them {expected}? "
            "Rename the argument(s) to match the advertised schema."
        )
    return message, hints


def _dispatch_tool(name: str, args: dict, request=None) -> tuple[str, bool]:
    """Run one tool synchronously; returns (text, is_error).

    Kept as a plain function so the async handler can offload it to a worker
    thread (asyncio.to_thread) and keep the event loop responsive for other
    sessions and health checks while a long scan is running.

    ``request`` is the transport's HTTP request when the tool call arrived
    over streamable-http (None over stdio) — the saved-query write tools use
    it to verify the caller's credential per call (mutation gate), and the
    identity spine reads the resolved Caller off its scope state
    (``caller_from_request_state``) for audit + policy enforcement.
    """
    caller = _identity.caller_from_request_state(request)
    # Advertised-contract gate (E_PARAM_INVALID): a missing required
    # argument is the caller's bug — refuse it with the shape named instead
    # of letting the args.get(key, "") defaults coerce it into a masked
    # downstream error (the "Empty SQL statement." masking this guards
    # against). An explicitly-sent empty string is NOT missing — the tool's
    # own validation keeps governing that case.
    invalid = _param_invalid(name, args)
    if invalid:
        message, hints = invalid
        return message + _errors.structured(_errors.E_PARAM_INVALID, hints), True
    try:
        if name == "list_tables":
            return list_tables(caller=caller), False
        elif name == "describe_table":
            return describe_table(str(args.get("table", "")), caller=caller), False
        elif name == "profile_table":
            cols = args.get("columns")
            col_list = [c.strip() for c in str(cols).split(",") if c.strip()] if cols else None
            return profile_table(str(args.get("table", "")), col_list, caller=caller), False
        elif name == "search_tables":
            return search_tables(str(args.get("query", "")), caller=caller), False
        elif name == "run_sql":
            params = args.get("params")
            if params is not None and not isinstance(params, (dict, list)):
                return "Error running SQL: params must be an object or an array.", True
            return run_sql(
                str(args.get("sql", "")),
                args.get("limit"),
                args.get("output_format") or "markdown",
                params,
                args.get("version_as_of"),
                caller=caller,
            ), False
        elif name == "scan_table":
            # An explicit limit of 0 is honored (empty result); only a
            # missing limit defaults to 100. A negative limit resolves to
            # the SQLHANDLER_MAX_ROWS cap inside scan_table (decision D4).
            raw_limit = args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else 100
            return scan_table(
                str(args.get("table", "")),
                args.get("columns"),
                limit,
                args.get("output_format") or "markdown",
                args.get("version_as_of"),
                caller=caller,
            ), False
        elif name == "column_stats":
            raw_top = args.get("top_n")
            top_n = int(raw_top) if raw_top is not None else 5
            return column_stats(str(args.get("table", "")), str(args.get("column", "")), top_n, caller=caller), False
        elif name == "sample_rows":
            # An explicit limit of 0 is honored (empty sample); the default
            # is a small honest head. Negative limits resolve to the
            # SQLHANDLER_MAX_ROWS cap inside the engine (D4 semantics, same
            # as scan_table).
            raw_limit = args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else 20
            return sample_rows(str(args.get("table", "")), limit, args.get("columns"), caller=caller), False
        elif name == "query_submit":
            params = args.get("params")
            if params is not None and not isinstance(params, (dict, list)):
                return "Error submitting query job: params must be an object or an array.", True
            return query_submit(
                str(args.get("sql", "")),
                args.get("limit"),
                params,
                args.get("version_as_of"),
                caller=caller,
            ), False
        elif name == "query_status":
            return query_status(str(args.get("job_id", ""))), False
        elif name == "query_result":
            return query_result(str(args.get("job_id", "")), args.get("output_format") or "markdown"), False
        elif name == "query_cancel":
            return query_cancel(str(args.get("job_id", ""))), False
        elif name == "query_save":
            return query_save(
                args.get("name"),
                str(args.get("sql", "")),
                args.get("params"),
                args.get("description"),
                request,
                caller=caller,
            ), False
        elif name == "query_list":
            return query_list(caller=caller), False
        elif name == "query_delete":
            return query_delete(args.get("name"), request, caller=caller), False
        elif name == "query_saved":
            params = args.get("params")
            if params is not None and not isinstance(params, (dict, list)):
                return "Error running saved query: params must be an object or an array.", True
            return query_saved(
                str(args.get("name", "")),
                params,
                args.get("limit"),
                args.get("output_format") or "markdown",
                args.get("version_as_of"),
                caller=caller,
            ), False
        elif name == "explain_query":
            # E_PARAM_INVALID for malformed args (structured errors exist and
            # the review requires citing it where args are malformed).
            params = args.get("params")
            if params is not None and not isinstance(params, (dict, list)):
                return _errors.enrich(
                    "Error explaining query: params must be an object (named $placeholders) or an array (positional ?)."
                ), True
            include_plan = args.get("include_plan")
            if include_plan is not None and not isinstance(include_plan, bool):
                return _errors.enrich("Error explaining query: include_plan must be a boolean."), True
            return explain_query(
                str(args.get("sql", "")),
                params,
                args.get("version_as_of"),
                bool(include_plan),
                caller=caller,
            ), False
        elif name == "ask_data":
            execute = args.get("execute")
            if execute is not None and not isinstance(execute, bool):
                return _errors.enrich("Error planning question: execute must be a boolean."), True
            return ask_data(str(args.get("question", "")), execute=bool(execute)), False
        return f"Unknown tool: {name}", True
    except Exception as exc:
        # The dispatch-level catch: the human message stays primary, the
        # structured code/fix_hints tail is appended when enabled (best-effort
        # — enrichment failures degrade to the plain message).
        return _errors.enrich(str(exc)), True


async def _handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    """Serve tools/call, dispatching to the underlying tool functions."""
    # The transport's HTTP request (None over stdio) rides the request
    # context; tools that mutate configuration (saved queries) verify the
    # caller's credential per call with it.
    text, is_error = await asyncio.to_thread(
        _dispatch_tool, params.name, params.arguments or {}, getattr(ctx, "request", None)
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=is_error,
    )


_TOOLS = [
    Tool(
        name="list_tables",
        description=(
            "List the tables available in the configured data source, plus a live "
            "inventory of any attached (read-only) external databases."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="describe_table",
        description="Return column names, types, and the canonical URI for a table.",
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": (
                        'Table name; use "schema/name" when the source uses schemas, '
                        'or "<db-alias>.<schema>.<table>" for an attached database.'
                    ),
                }
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="profile_table",
        description=(
            "Column-level statistics for a table: min/max, approx distinct count, "
            "null %, avg/std and q25/q50/q75 quantiles, plus the total row count. "
            "Use it before writing filters/aggregations to pick the right columns, "
            "value ranges and predicates on the first try instead of guessing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": (
                        'Table name; use "schema/name" when the source uses schemas, '
                        'or "<db-alias>.<schema>.<table>" for an attached database.'
                    ),
                },
                "columns": {
                    "type": "string",
                    "description": "Optional comma-separated subset of columns to profile (default: all).",
                },
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="search_tables",
        description=(
            "Find tables by keyword: matches table names, columns and catalog "
            "descriptions, ranked best-first (exact/substring terms, then fuzzy "
            "near-miss names for typos). Use when the table list is long or you "
            "don't know which table holds what."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to look for (e.g. 'work order amount').",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="run_sql",
        description=(
            "Execute a read-only SQL query against the source tables and return results. "
            "Tables are referenced by folder name (e.g. work_order_header, or schema/name); "
            "attached external databases (read-only) are referenced as "
            "<db-alias>.<schema>.<table> and can be joined with lake tables in the same "
            "query. Aggregations, filters, and joins are pushed into the scan. "
            "SELECT-only: DDL/DML (INSERT, CREATE, ATTACH, COPY, ...) are rejected "
            "unless the operator set SQLHANDLER_MCP_READONLY=0. When the write tier is "
            "enabled (SQLHANDLER_WRITES_ENABLED), ONE scratch-write statement "
            "(CREATE TABLE AS / INSERT INTO / COPY INTO under <scratch-root>/"
            "<your-subject>/...) is admitted and a write summary returns in place of rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "The SQL SELECT to run against the source tables. Read-only "
                        "by default (SQLHANDLER_MCP_READONLY); attached databases "
                        "are always read-only: writes against their catalogs fail."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Optional max number of rows to return. Default: capped by SQLHANDLER_MAX_ROWS (1000)."
                    ),
                },
                "output_format": {
                    "type": "string",
                    "enum": ["markdown", "json", "csv", "arrow"],
                    "description": (
                        "Result rendering: markdown (default, human/LLM friendly), "
                        "json ({columns, rows} — compact, machine-parseable), csv, "
                        "or arrow (base64 Arrow IPC stream — exact dtypes, "
                        "base64-decodes into pa.ipc.open_stream)."
                    ),
                },
                "params": {
                    "description": (
                        "Optional bind parameters: an object for named $placeholders "
                        '(e.g. {"status": "open"}) or an array for positional ?. '
                        "Keeps reusable query templates injection-safe."
                    ),
                },
                "version_as_of": {
                    "type": "integer",
                    "description": (
                        "Optional historical snapshot for time travel: a Delta snapshot "
                        "version (nfs/onelake backends) or Iceberg snapshot id. Applies "
                        "to every versionable table the query touches."
                    ),
                },
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="scan_table",
        description=(
            "Fetch rows/columns from a table via pyarrow (columnar). Prefer run_sql "
            "when filters or aggregations can be pushed into the scan; use this to "
            "sample raw columns or feed a programmatic caller without writing SQL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": 'Table name; use "schema/name" when the source uses schemas.',
                },
                "columns": {
                    "type": "string",
                    "description": "Optional comma-separated list of columns to project.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max rows to return (default 100). A negative limit "
                        "resolves to the SQLHANDLER_MAX_ROWS cap (decision D4) "
                        "instead of the whole table."
                    ),
                },
                "output_format": {
                    "type": "string",
                    "enum": ["markdown", "json", "csv", "arrow"],
                    "description": "Result rendering (default markdown).",
                },
                "version_as_of": {
                    "type": "integer",
                    "description": (
                        "Optional historical snapshot for time travel: a Delta snapshot "
                        "version (nfs/onelake) or Iceberg snapshot id."
                    ),
                },
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="column_stats",
        description=(
            "Per-column statistics for ONE column: distinct count, null count/%, "
            "min/max, q25/q50/q75 quantiles, and the top-5 values with counts — "
            "over a bounded sample (SQLHANDLER_PROFILE_MAX_ROWS, same cap as "
            "profile_table; never a full-table scan beyond it). Use it to pick "
            "filter values and spot skew before writing SQL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": (
                        'Table name; use "schema/name" when the source uses schemas, '
                        'or "<db-alias>.<schema>.<table>" for an attached database.'
                    ),
                },
                "column": {"type": "string", "description": "The column to profile."},
                "top_n": {
                    "type": "integer",
                    "description": "How many top values to return (default 5, max 20).",
                },
            },
            "required": ["table", "column"],
        },
    ),
    Tool(
        name="sample_rows",
        description=(
            "Sample actual rows from a table (a bounded head) with per-column "
            "fill rates (fill %, null counts) — one bounded look at the DATA. "
            "Schema comes from describe_table and statistics from profile_table; "
            "use this to see the actual row shape before writing SQL. The scan "
            "stops early (never reads the whole table for a small sample) and "
            "is capped by SQLHANDLER_PROFILE_MAX_ROWS."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": (
                        'Table name; use "schema/name" when the source uses schemas, '
                        'or "<db-alias>.<schema>.<table>" for an attached database.'
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max rows to sample (default 20). A negative limit resolves "
                        "to the SQLHANDLER_MAX_ROWS cap (same D4 semantics as scan_table)."
                    ),
                },
                "columns": {
                    "type": "string",
                    "description": "Optional comma-separated list of columns to project.",
                },
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="query_submit",
        description=(
            "Start an async query job and return its job_id immediately — for "
            "queries that may outlive a tool-call timeout. The read-only guard "
            "applies at SUBMIT time (DDL is refused, same as run_sql); the job "
            "runs on the same engine path with the same SQLHANDLER_QUERY_TIMEOUT "
            "(600s default, watchdog-enforced) and SQLHANDLER_MAX_ROWS cap. "
            "Poll query_status, then fetch ONCE with query_result. The registry "
            "is in-memory (a restart clears it) and bounded by SQLHANDLER_MAX_JOBS "
            "(default 8; beyond the cap the submit is refused)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL SELECT to run (read-only guard applies at submit).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional max rows (SQLHANDLER_MAX_ROWS caps it either way).",
                },
                "params": {
                    "description": (
                        "Optional bind parameters: an object for named $placeholders or an array for positional ?."
                    ),
                },
                "version_as_of": {
                    "type": "integer",
                    "description": "Optional historical snapshot (Delta version / Iceberg snapshot id).",
                },
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="query_status",
        description=(
            "Poll an async query job: state (running/done/error/cancelled), "
            "elapsed_ms, error, and — when done and not yet fetched — the column "
            "names and row count. No row data; fetch rows with query_result."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "The job id from query_submit."}},
            "required": ["job_id"],
        },
    ),
    Tool(
        name="query_result",
        description=(
            "Fetch a finished async query job's result ONCE (markdown default, "
            "json, csv, arrow), then the spooled result is freed from memory — a second "
            "fetch of the same job is refused (resubmit instead)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job id from query_submit."},
                "output_format": {
                    "type": "string",
                    "enum": ["markdown", "json", "csv", "arrow"],
                    "description": "Result rendering (default markdown).",
                },
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="query_cancel",
        description="Cancel a running async query job (DuckDB interrupt).",
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "The job id from query_submit."}},
            "required": ["job_id"],
        },
    ),
    Tool(
        name="query_save",
        description=(
            "Save a parameterized query under a name for reuse: "
            "query_save(name, sql, params, description). The SQL is validated at "
            "save time (parsed; SELECT-only while the read-only mode is on) and "
            "params are stored as BIND parameters ($name / ? placeholders — never "
            "string-interpolated). WRITES ARE AUTH-GATED: when SQLHANDLER_API_TOKEN "
            "or MCP_API_KEYS/SQLHANDLER_API_KEYS is configured, an unauthenticated "
            "save is refused; with no credential configured (single-user-local "
            "mode) saves are allowed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short name to address the query by (1-128 chars).",
                },
                "sql": {
                    "type": "string",
                    "description": 'The SQL template, with $name / ? placeholders for parameters (e.g. "SELECT * FROM t WHERE kind = $kind").',
                },
                "params": {
                    "description": "Optional default bind parameters (object for $names or array for ?).",
                },
                "description": {"type": "string", "description": "Optional human note (what/why)."},
            },
            "required": ["name", "sql"],
        },
    ),
    Tool(
        name="query_list",
        description="List saved parameterized queries (name, SQL, default params, description).",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="query_delete",
        description=("Delete a saved query by name. WRITES ARE AUTH-GATED (same posture as query_save)."),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The saved-query name to delete."}},
            "required": ["name"],
        },
    ),
    Tool(
        name="query_saved",
        description=(
            "Run a saved parameterized query by name. Call-time params override "
            "stored ones (dict merge) and travel as BIND parameters — the stored "
            "SQL is never string-interpolated. The read-only guard is re-applied "
            "at run time. output_format/limit/version_as_of work like run_sql."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The saved-query name."},
                "params": {
                    "description": "Optional bind params overriding the stored defaults (object for $names / array for ?).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional max rows (SQLHANDLER_MAX_ROWS caps it).",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["markdown", "json", "csv", "arrow"],
                    "description": "Result rendering (default markdown).",
                },
                "version_as_of": {
                    "type": "integer",
                    "description": "Optional historical snapshot (Delta version / Iceberg snapshot id).",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="explain_query",
        description=(
            "Estimate one read-only query's cost WITHOUT running it: referenced "
            "tables with metadata row counts and bytes-to-scan (each labeled "
            "exact/approx/none), the warm/cold band (is the exact result already "
            "in the L1/L2 result cache), and — with include_plan — DuckDB's own "
            "EXPLAIN tree summary (planning only; the query's data path never "
            "executes). Attached-database queries report confidence none. Use it "
            "before run_sql to pick the cheap variant of a query."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "The SELECT (or EXPLAIN SELECT) to estimate. Anything else is refused, exactly like run_sql."
                    ),
                },
                "params": {
                    "description": (
                        "Optional bind parameters (object for $placeholders / array for ?) — validated, never executed."
                    ),
                },
                "version_as_of": {
                    "type": "integer",
                    "description": "Optional historical snapshot the estimate describes (Delta version / Iceberg snapshot id).",
                },
                "include_plan": {
                    "type": "boolean",
                    "description": (
                        "Also compute DuckDB's EXPLAIN (FORMAT JSON) plan summary "
                        "(default false — rows + bytes + warm/cold only)."
                    ),
                },
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="ask_data",
        description=(
            "Plan a natural-language question against the lake WITHOUT executing: "
            "keyword-searches the tables (top 5), describes the best hit (up to 20 "
            "columns, + catalog docs), optionally profiles up to 6 of its columns, "
            "then drafts one candidate SELECT and a suggested follow-up. Output is "
            "markdown ending in a 'run this with run_sql' footer — execution is "
            "ALWAYS a separate, explicit run_sql call (the plan/apply separation); "
            "the execute argument is accepted for symmetry but has no effect."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The question in plain language (keywords are matched "
                        "against table/column names and catalog docs)."
                    ),
                },
                "execute": {
                    "type": "boolean",
                    "description": (
                        "Accepted for call-site symmetry, deliberately WITHOUT effect: "
                        "ask_data never executes — run the drafted SQL with run_sql."
                    ),
                },
            },
            "required": ["question"],
        },
    ),
]


def mcp_tool_specs() -> list[dict]:
    """The registered tools as plain dicts: [{name, description, inputSchema}].

    The WEB UI's Inspector tab (webui.py ``/api/inspector/*``) serves this so
    the browser sees the exact tool surface an MCP client's tools/list sees —
    one source of truth (_TOOLS), no second table to drift. Re-derived per
    call (a Tool is a pydantic model; 18 items is cheap) so test monkeypatching
    is reflected too. Tools only — resources/prompts stay out of scope, the
    same boundary an MCP client's tools/list draws.
    """
    return [
        {"name": t.name, "description": t.description or "", "inputSchema": t.input_schema}
        for t in _TOOLS
    ]


mcp = Server(
    "sqlhandler",
    title="SQLhandler MCP Server",
    # Single-sourced from sqlhandler.__version__ so the MCP handshake,
    # /api/status and pyproject.toml always agree (bump_version.sh updates
    # __init__.py; hardcoding a second number here is how 0.5.1/0.5.3/0.6.0
    # drifted apart).
    version=__version__,
    description=("Direct, fast SQL access to columnar data (OneLake/Delta, S3/MinIO/Parquet, Iceberg) as MCP tools."),
    instructions=(
        "Direct, fast access to columnar data (OneLake/Delta or S3/MinIO/Parquet) "
        "as an EzPresto replacement. Use list_tables to discover tables, describe_table "
        "for schema, profile_table for column statistics (value ranges, null %, distinct "
        "counts — helps write correct filters first try), column_stats for one column's "
        "top values and quantiles, and run_sql / scan_table to "
        "query. Prefers predicate filters and column projections to avoid full scans. "
        "Before running an expensive query, explain_query estimates its cost WITHOUT "
        "executing it (row counts, bytes-to-scan with confidence labels, warm/cold band); "
        "for a plain-language question ask_data plans it (search → describe → draft SQL) "
        "without executing — run the draft with an explicit run_sql call. "
        "When external databases are attached (see the list_tables output), their tables "
        "are addressed as <db-alias>.<schema>.<table>, join-able with lake tables in one "
        "query, and strictly read-only. For queries that may run long, query_submit "
        "starts an async job (poll query_status, fetch once with query_result, "
        "query_cancel to stop); query_save/query_saved reuse parameterized queries."
    ),
    on_list_tools=_handle_list_tools,
    on_call_tool=_handle_call_tool,
    # Resources + prompts (see mcp_resources.py): the other MCP primitives.
    # The handlers reuse the process-wide engine and its caches via _handler.
    on_list_resources=mcp_resources.handle_list_resources,
    on_list_resource_templates=mcp_resources.handle_list_resource_templates,
    on_read_resource=mcp_resources.handle_read_resource,
    on_list_prompts=mcp_resources.handle_list_prompts,
    on_get_prompt=mcp_resources.handle_get_prompt,
)


def _env_list(name: str) -> list[str]:
    """Parse a comma-separated env list into a clean list (empty when unset)."""
    raw = os.environ.get(name, "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# The SDK-side switch stays OFF: the SDK middleware rejects EVERY request
# (421) when its allowed_hosts list is empty, and a pod cannot know which
# externally-visible hostnames clients use (service DNS, ingress FQDN,
# port-forward) — flipping it on unconfigured would break every deployment.
# The equivalent protection runs in _McpTransportGuard below, which applies
# the same checks with deployment-configurable lists.
_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


class _McpTransportGuard:
    """DNS-rebinding protection for ``/mcp`` (audit quick win, default ON).

    Same checks as the MCP SDK's transport-security middleware, but with
    deployment-configurable lists (both envs re-read per request, like the
    API-key gate) and a default that does not reject legitimate clients:

    * **Origin** — any request that presents an ``Origin`` header must match
      ``SQLHANDLER_ALLOWED_ORIGINS`` (the same list the CORS middleware
      uses; decision D3). Browsers attach Origin to every cross-site /
      DNS-rebound request; MCP clients (Python/TS SDKs, curl, DSH) attach
      none — so the default (no origins configured) blocks exactly the
      browser path while every legitimate MCP client is unaffected.
    * **Host** — when ``SQLHANDLER_ALLOWED_HOSTS`` is set, the ``Host``
      header must match an entry (exact, or ``host:*`` port wildcard — the
      SDK's pattern) or the request is refused with 421; a missing Host
      header is refused too. Unset (default): no Host allowlist, because
      strict pinning without knowing the deployment's externally-visible
      hostnames rejects ALL traffic (the SDK's own middleware has no
      deployment discovery either). The chart value ``mcp.allowedHosts``
      makes strict pinning a one-line opt-in.

    The SDK-side switch stays off for the same reason (an empty SDK
    allowlist 421s every request); Content-Type validation of POSTs still
    runs SDK-side and is not duplicated here.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _matches(value: str, patterns: list[str]) -> bool:
        """Exact or ``base:*`` port-wildcard match (the SDK's semantics)."""
        if value in patterns:
            return True
        for pattern in patterns:
            if pattern.endswith(":*") and value.startswith(pattern[:-1]):
                return True
        return False

    @classmethod
    def _reject(cls, scope) -> tuple[int, str] | None:
        """(status, message) when the request fails a header check, else None."""
        host = origin = ""
        for k, v in scope.get("headers", []):
            lk = k.lower() if isinstance(k, bytes) else k
            if lk == b"host" and not host:
                host = v.decode("latin-1").strip()
            elif lk == b"origin" and not origin:
                origin = v.decode("latin-1").strip()
        if not host:
            return 421, "Missing Host header"
        allowed_hosts = _env_list("SQLHANDLER_ALLOWED_HOSTS")
        if allowed_hosts and not cls._matches(host, allowed_hosts):
            return 421, "Invalid Host header"
        if origin and not cls._matches(origin, _env_list("SQLHANDLER_ALLOWED_ORIGINS")):
            return 403, "Invalid Origin header"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            verdict = self._reject(scope)
            if verdict is not None:
                status, message = verdict
                resp = Response(message, status_code=status)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _ProbesAuthMiddleware:
    """Optional auth gate on ``/metrics`` only (default OFF).

    ``SQLHANDLER_METRICS_AUTH=1`` requires a valid credential from the same
    sources the other gates use — ``SQLHANDLER_API_TOKEN`` (the /api token)
    or the /mcp API keys (``MCP_API_KEYS`` / ``SQLHANDLER_API_KEYS``) — via
    ``Authorization: Bearer``, ``X-API-Key`` or ``X-API-Token``, compared
    constant-time. Default OFF keeps today's behavior (/metrics
    unauthenticated). When enabled with NO credential source configured the
    gate fails CLOSED (401 for everyone) with a loud startup warning —
    a metrics gate without a secret would be theater.

    /ready is NEVER gated: kubelet readiness probes cannot carry a secret
    (probe headers are literal, not secretRef), so a gated /ready strands
    every pod NotReady forever — seen live as pods 0/1 with probe 401s.
    Readiness is a cluster-internal signal; liveness stays on /health
    (always open).
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _expected() -> list[str]:
        expected = [os.environ.get("SQLHANDLER_API_TOKEN", "").strip()]
        expected.extend(_McpApiKeyMiddleware._keys())
        return [t for t in expected if t]

    async def __call__(self, scope, receive, send):
        gated = scope["type"] == "http" and scope.get("path", "") == "/metrics"
        if gated and _truthy("SQLHANDLER_METRICS_AUTH"):
            provided = ""
            for k, v in scope.get("headers", []):
                lk = k.lower() if isinstance(k, bytes) else k
                if lk == b"authorization":
                    scheme, _, token = v.decode("latin-1").partition(" ")
                    if scheme.lower() == "bearer":
                        provided = token.strip()
                    break
                if lk in (b"x-api-key", b"x-api-token"):
                    provided = v.decode("latin-1").strip()
                    break
            expected = self._expected()
            ok = bool(provided) and any(
                hmac.compare_digest(provided.encode("utf-8"), candidate.encode("utf-8")) for candidate in expected
            )
            if not ok:
                resp = JSONResponse(
                    {"error": "unauthorized: /metrics is gated (SQLHANDLER_METRICS_AUTH)"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _run_stdio(server: Server) -> None:
    """Serve over stdio until the client closes the pipe."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# --------------------------------------------------------------------------
# Process-wide engine + warm-up
# --------------------------------------------------------------------------

_handler_lock = threading.Lock()
_handler_singleton: SqlEngine | None = None


def _handler() -> SqlEngine:
    """Return the process-wide SqlEngine, building it on first use.

    The data source is selected by SQLHANDLER_BACKEND (onelake by default,
    or s3/minio, iceberg, nfs/file, sharing) and its config is built from
    the environment. On first use
    the engine is created and, when pre-warm tables are configured, a daemon
    thread warms the describe cache in the background so the first agent
    describe is already a cache hit.
    """
    global _handler_singleton
    with _handler_lock:
        if _handler_singleton is None:
            load_dotenv()
            # Federated multi-source mode (SQLHANDLER_SOURCES) or single backend.
            provider = load_source_providers()
            if provider is None:
                _, config = load_backend_config()
                provider = make_provider(config)
            cache = load_cache_config()
            _handler_singleton = SqlEngine(
                provider,
                cache_ttl=cache.ttl_seconds,
                dataset_cache_ttl=cache.dataset_cache_ttl,
                dataset_cache_tables=cache.dataset_cache_tables,
                version_check_interval=cache.version_check_interval,
                list_async_refresh=cache.list_async_refresh,
            )
            # Prewarm: explicit SQLHANDLER_PREWARM_TABLES wins; otherwise the
            # busiest tables from the previous run (usage counts persisted in
            # the disk-warm cache) are warmed — the server teaches itself
            # what to prewarm instead of relying on a hand-maintained list.
            prewarm_tables = cache.prewarm_tables or _handler_singleton.usage_top_tables()
            if prewarm_tables:
                logger.info(
                    "prewarming schema cache for %d table(s)%s",
                    len(prewarm_tables),
                    "" if cache.prewarm_tables else " (usage-driven)",
                )
                threading.Thread(
                    target=_prewarm,
                    args=(_handler_singleton, prewarm_tables),
                    daemon=True,
                    name="sqlhandler-prewarm",
                ).start()
        return _handler_singleton


def _prewarm(handler: SqlEngine, tables: tuple[str, ...]) -> None:
    """Warm the schemas for the tables your queries hit most often."""
    try:
        outcomes = handler.prewarm(tables)
        failed = [t for t, o in outcomes.items() if o != "ok"]
        if failed:
            logger.warning("prewarm failed for: %s", ", ".join(failed))
        else:
            logger.info("prewarmed describe cache for %d table(s)", len(outcomes))
    except Exception:
        logger.exception("prewarm failed")


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def list_tables(*, caller=None) -> str:
    """List the tables available in the configured data source.

    ``caller`` (identity spine): policy-hidden tables are omitted for a
    caller whose groups hide them (byte-identical list when enforcement is
    off / no caller).
    """
    try:
        handler = _handler()
        tables = handler.list_tables(caller=caller)
        lines: list[str]
        if not tables:
            lines = ["No tables found in the configured data source."]
        else:
            lines = ["Tables:"]
            n_virtual = 0
            n_raw = 0
            for t in tables:
                # Catalog descriptions annotate the list when present (compact:
                # name first, description after an em dash), so agents can pick
                # the right table without a describe round-trip per candidate.
                desc = handler.table_description(t)
                if t.format == "virtual":
                    n_virtual += 1
                    lines.append(f"  - {t.name} (VIRTUAL)" + (f" — {desc}" if desc else ""))
                elif is_raw_format(t.format):
                    n_raw += 1
                    lines.append(f"  - {t.name} (RAW {t.format})" + (f" — {desc}" if desc else ""))
                else:
                    lines.append(f"  - {t.name}" + (f" — {desc}" if desc else ""))
            if n_virtual:
                lines.append(
                    f"  ({n_virtual} VIRTUAL table{'s' if n_virtual != 1 else ''} — computed on the fly "
                    "from their semantic-catalog definitions; query them like any other table)"
                )
            if n_raw:
                lines.append(
                    f"  ({n_raw} RAW table{'s' if n_raw != 1 else ''} — landing-zone csv/tsv/json files, "
                    "no row-group pruning or statistics; promote to Parquet for large data)"
                )
        # Attached external databases (read-only): listed with fully-qualified
        # names so agents can address them in run_sql / describe_table
        # directly. Best-effort — a database that is down must not fail the
        # lake listing.
        try:
            for db in handler.attached_databases():
                lines.append("")
                if db.get("error"):
                    lines.append(
                        f"Attached database {db['name']} ({db['type']}, {db['uri']}): unavailable — {db['error']}"
                    )
                    continue
                lines.append(
                    f"Attached database {db['name']} ({db['type']}, {db['uri']}, "
                    "read-only) — address tables as <db-alias>.<schema>.<table>:"
                )
                for t in db.get("tables", []):
                    lines.append(f"  - {t['qualified']}")
                if db.get("truncated"):
                    lines.append(f"  … and {db['truncated']} more (use describe_table to confirm)")
        except Exception as exc:
            lines.append(f"(attached-database listing unavailable: {exc})")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing tables: {exc}"


def describe_table(table: str, *, caller=None) -> str:
    """Return column names, types, and the canonical URI for a table.

    ``caller`` (identity spine): masked columns are omitted and hidden
    tables raise (policy-consistent schema visibility).
    """
    try:
        handler = _handler()
        info = handler.describe_table(table, caller=caller)
        lines = [f"Table: {info['table']}", f"URI: {info['uri']}"]
        if info.get("virtual"):
            lines.append("Kind: VIRTUAL — computed on the fly from its semantic-catalog definition")
        elif is_raw_format(info.get("format", "")):
            lines.append(
                f"Kind: RAW ({info['format']}) — landing-zone raw text, scanned whole "
                "(no row groups/statistics); promote to Parquet for large data"
            )
        if info.get("description"):
            lines.append(f"Description: {info['description']}")
        if info.get("aliases"):
            lines.append(f"Also known as: {', '.join(info['aliases'])}")
        lines.append("Columns:")
        for c in info["columns"]:
            line = f"  - {c['name']}: {c['type']}"
            if c.get("description"):
                line += f" — {c['description']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        return f"Error describing table: {exc}"


def profile_table(table: str, columns: list[str] | None = None, *, caller=None) -> str:
    """Return per-column statistics for a table as a markdown table.

    ``caller`` (identity spine): profiles run over the masking view — the
    stats describe what this caller can actually read.
    """
    try:
        handler = _handler()
        p = handler.profile_table(table, columns=columns, caller=caller)
        header = [f"Table: {p['table']}"]
        if p.get("n_rows") is not None:
            header.append(
                f"Rows: {p['n_rows']}"
                + (
                    f" (profiled {p['profiled_rows']}, capped by SQLHANDLER_PROFILE_MAX_ROWS)"
                    if p.get("profile_max_rows", 0) > 0 and p.get("profiled_rows") == p.get("profile_max_rows")
                    else ""
                )
            )
        import pandas as pd

        df = pd.DataFrame(p["columns"])
        return "\n".join(header) + "\n\n" + df.to_markdown(index=False)
    except Exception as exc:
        return f"Error profiling table: {_errors.enrich(str(exc))}"


def search_tables(query: str, *, caller=None) -> str:
    """Keyword search over table names/columns/catalog descriptions."""
    try:
        handler = _handler()
        results = handler.search_tables(query, caller=caller)
        if not results:
            return f"No tables match {query!r}. Try broader keywords, or run list_tables."
        lines = [f"Found {len(results)} table(s) matching {query!r}:"]
        for r in results:
            line = f"  - {r['table']} ({r['format']})"
            if r["matched_columns"]:
                line += f" — columns: {', '.join(r['matched_columns'])}"
            lines.append(line)
            if r["description"]:
                lines.append(f"      {r['description']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error searching tables: {exc}"


def _validate_output_format(output_format: str) -> str:
    """Normalize one output_format name; ValueError names the valid set.

    Flows through each tool's catch-all, so a bad name surfaces as the
    same "Error <verb> ... {"error": {...}}" shape any other bad input
    produces (E_PARAM_INVALID — see errors.py).
    """
    fmt = str(output_format or "markdown").strip().lower()
    if fmt not in ("markdown", "json", "csv", "arrow"):
        raise ValueError(f"Unsupported output_format {fmt!r}; use 'markdown', 'json', 'csv' or 'arrow'.")
    return fmt


def run_sql(
    sql: str,
    limit: int | None = None,
    output_format: str = "markdown",
    params: object | None = None,
    version_as_of: int | None = None,
    *,
    caller=None,
) -> str:
    """Execute a SQL query against the source tables and return results.

    Read-only by default (fleet decision D2): the same DuckDB-parser guard
    the web API uses rejects every non-SELECT statement (multi-statement
    DDL, INSERT/UPDATE/DELETE, ATTACH, COPY, PRAGMA/SET, EXPLAIN of a
    write). ``SQLHANDLER_MCP_READONLY=0`` restores the old DDL capability
    for trusted callers; the guard re-reads the env per call so the flip
    needs no restart. Queries that touch an attached external catalog stay
    SELECT-only regardless (engine-level, see sqlhandler/engine.py).

    Write tier (additive, review §4 — GLOBAL FLAG, DEFAULT FALSE): when
    ``SQLHANDLER_WRITES_ENABLED`` is set, a single classified
    scratch-write statement (CREATE TABLE AS / INSERT INTO / COPY INTO
    targeting ``<scratch-root>/<subject-slug>/...``) is admitted here —
    BEFORE the read guard (which would refuse it by shape) — and the write
    SUMMARY (target, backend, rows written) returns in place of rows.
    ``SQLHANDLER_MCP_READONLY`` keeps governing multi-statement/DDL
    exactly as before — the new flag gates only the new capability, so the
    default posture is unchanged twice over. Writes are never cached and
    never served from cache (classification precedes the cache check).

    Args:
        sql: The SQL SELECT to run against the source tables (or, with the
            write tier enabled, one classified scratch-write statement).
        limit: Optional max rows to return; SQLHANDLER_MAX_ROWS (default 1000)
            caps the result either way.
        output_format: markdown (default) | json | csv | arrow.
        params: optional bind parameters (named dict or positional list).
    """
    try:
        fmt = _validate_output_format(output_format)
        # Write tier FIRST (classification before the read guard): when the
        # global flag is ON, a single classified scratch-write statement is
        # ADMITTED here (the read guard would refuse it by shape) and the
        # write summary returns in place of rows. Everything else —
        # multi-statement scripts, DDL, PRAGMA — still hits the D2 guard
        # below exactly as today (the new flag gates only the new
        # capability). Flag off (default): this branch is inert and the
        # guard below sees the statement first, byte-identically.
        if _writes.writes_enabled():
            classes = _writes.classify_sql(sql)
            if len(classes) == 1 and classes[0].kind == _writes.CLASS_WRITE_SCRATCH:
                arrow = _handler().execute_write(sql, params=params, caller=caller)
                return _write_summary_output(arrow, fmt=fmt)
        if mcp_readonly_enabled():
            # Decision D2: MCP callers get the web API's read-only guarantee
            # by default. ValueError -> the "Error running SQL:" text below,
            # with the env named in the message.
            sql = assert_mcp_readonly(sql)
        handler = _handler()
        arrow = handler.query_duckdb(sql, limit=limit, params=params, version_as_of=version_as_of, caller=caller)
        return _arrow_to_output(arrow, max_rows=limit, fmt=fmt)
    except Exception as exc:
        return f"Error running SQL: {_errors.enrich(str(exc))}"


def _write_summary_output(arrow, fmt: str = "markdown") -> str:
    """Render one write summary (rows -> the write's summary row)."""
    try:
        import pyarrow as pa

        if isinstance(arrow, pa.RecordBatchReader):
            arrow = arrow.read_all()
    except Exception:
        pass
    if fmt == "json":
        return _arrow_to_output(arrow, max_rows=1, fmt="json")
    if fmt == "csv":
        return _arrow_to_output(arrow, max_rows=1, fmt="csv")
    if fmt == "arrow":
        return _arrow_to_output(arrow, max_rows=1, fmt="arrow")
    row = {k: arrow.column(k)[0].as_py() for k in arrow.column_names} if arrow.num_rows else {}
    lines = [
        "Write complete.",
        "",
        f"- target: {row.get('target', '')}",
        f"- backend: {row.get('backend', '')} scratch",
        f"- rows written: {row.get('rows_written', 0)} ({row.get('mode', '')})",
        f"- duration_ms: {row.get('duration_ms', '')}",
        "",
        "Read it back with run_sql (the scratch table appears in list_tables).",
    ]
    return "\n".join(lines)


def scan_table(
    table: str,
    columns: str | None = None,
    limit: int = 100,
    output_format: str = "markdown",
    version_as_of: int | None = None,
    *,
    caller=None,
) -> str:
    """Fetch rows/columns from a table via pyarrow (columnar).

    Prefer run_sql when filters or aggregations can be pushed into the scan;
    use this to sample raw columns or feed a programmatic caller without
    writing SQL.

    Args:
        table: Table name (schema/name when the source uses schemas).
        columns: Optional comma-separated list of columns to project.
        limit: Max rows to return (default 100). Per decision D4, a missing
            or negative limit (the historical "whole table") resolves to the
            ``SQLHANDLER_MAX_ROWS`` cap — and the SAME resolved positive
            value feeds the output rendering, so no row is ever silently
            dropped at the boundary (``limit=-1`` used to reach pandas'
            ``.head(-1)``, which drops the LAST row). An explicit positive
            limit is honored exactly; ``SQLHANDLER_MAX_ROWS=0`` (cap
            disabled) resolves the negative/missing case to unlimited.
        output_format: markdown (default) | json | csv | arrow.
    """
    try:
        fmt = _validate_output_format(output_format)
        handler = _handler()
        col_list = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
        resolved_limit = _resolve_scan_limit(limit)
        arrow = handler.scan_arrow(
            table, columns=col_list, limit=resolved_limit, version_as_of=version_as_of, caller=caller
        )
        return _arrow_to_output(arrow, max_rows=resolved_limit, fmt=fmt)
    except Exception as exc:
        return f"Error scanning table: {_errors.enrich(str(exc))}"


def column_stats(table: str, column: str, top_n: int = 5, *, caller=None) -> str:
    """Statistics for ONE column over a bounded sample (additive).

    distinct count, null count/pct, min/max, q25/q50/q75 and the top-N
    values with counts — sampled with the same SQLHANDLER_PROFILE_MAX_ROWS
    cap profile_table uses (never a full-table scan beyond the profile cap).
    """
    try:
        s = _handler().column_stats(table, column, top_n, caller=caller)
        return _column_stats_markdown(s)
    except Exception as exc:
        return f"Error computing column stats: {_errors.enrich(str(exc))}"


def _md_cell(value) -> str:
    """Escape a value for a markdown table cell (pipes/newlines break rows)."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _column_stats_markdown(s: dict) -> str:
    """Render one column_stats dict as compact markdown."""
    n_rows = s.get("n_rows")
    scope = (
        f"sampled {s.get('sampled_rows', 0)} of {n_rows} rows"
        if n_rows is not None
        else f"sampled {s.get('sampled_rows', 0)} rows"
    )
    if s.get("sample_cap", 0) and s.get("sampled_rows") == s.get("sample_cap") and n_rows != s.get("sampled_rows"):
        scope += f" (cap {s.get('sample_cap')}, SQLHANDLER_PROFILE_MAX_ROWS)"
    lines = [
        f"Column stats: {s['table']}.{s['column']} ({s.get('type', '')}) — {scope}",
        "",
        "| metric | value |",
        "|---|---|",
        f"| distinct_count (sample) | {_md_cell(s.get('distinct_count'))} |",
        f"| approx_unique (sample) | {_md_cell(s.get('approx_unique'))} |",
        f"| null_count (sample) | {_md_cell(s.get('null_count'))} ({_md_cell(s.get('null_pct'))}%) |",
        f"| min | {_md_cell(s.get('min'))} |",
        f"| max | {_md_cell(s.get('max'))} |",
        f"| q25 / q50 / q75 | {_md_cell(s.get('q25'))} / {_md_cell(s.get('q50'))} / {_md_cell(s.get('q75'))} |",
    ]
    top = s.get("top_values") or []
    if top:
        lines += ["", "Top values (sample):", "", "| value | count |", "|---|---|"]
        for r in top:
            lines.append(f"| {_md_cell(r.get('value'))} | {_md_cell(r.get('count'))} |")
    return "\n".join(lines)


def _sample_rows_markdown(s: dict) -> str:
    """Render one sample_rows dict as compact markdown."""
    n_rows = s.get("n_rows")
    scope = (
        f"sampled {s.get('sampled_rows', 0)} of {n_rows} rows"
        if n_rows is not None
        else f"sampled {s.get('sampled_rows', 0)} rows"
    )
    if (
        s.get("profile_max_rows", 0)
        and s.get("sample_limit") == s.get("profile_max_rows")
        and n_rows != s.get("sampled_rows")
    ):
        scope += f" (capped by SQLHANDLER_PROFILE_MAX_ROWS={s.get('profile_max_rows')})"
    kind = " VIRTUAL" if s.get("virtual") else ""
    lines = [f"Sample rows: {s['table']}{kind} ({scope})", ""]

    # Fill rates: one row per column — the null story an agent needs before
    # writing filters (the same numbers the rows below were sampled from).
    lines += [
        "Fill rates (this sample):",
        "",
        "| column | type | fill % | nulls |",
        "|---|---|---|---|",
    ]
    for c in s.get("columns", []):
        lines.append(
            f"| {_md_cell(c.get('name'))} | {_md_cell(c.get('type'))} "
            f"| {_md_cell(c.get('fill_pct'))}% | {_md_cell(c.get('null_count'))} |"
        )

    rows = s.get("rows") or []
    if rows:
        cols = list(rows[0].keys())
        lines += ["", "Rows:", "", "| " + " | ".join(_md_cell(c) for c in cols) + " |"]
        lines.append("|" + "---|" * len(cols))
        for r in rows:
            lines.append("| " + " | ".join(_md_cell(r.get(c)) for c in cols) + " |")
    else:
        lines += ["", "(no rows in the sample — the table is empty, or the limit is 0)"]
    return "\n".join(lines)


def sample_rows(table: str, limit: int = 20, columns: str | None = None, *, caller=None) -> str:
    """Sample actual rows from a table with per-column fill rates.

    One bounded look at the data (schema comes from describe_table,
    statistics from profile_table — this shows the rows themselves). Physical
    tables read via the pyarrow profile-sampler posture (``head`` stops the
    scan early); virtual tables route through the SQL path
    (``SELECT ... LIMIT n``); attached external tables run the LIMIT
    server-side. The sample is bounded by SQLHANDLER_PROFILE_MAX_ROWS and
    the D4 limit semantics of scan_table (a negative limit resolves to the
    SQLHANDLER_MAX_ROWS cap).
    """
    try:
        handler = _handler()
        col_list = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
        raw_limit = limit if isinstance(limit, int) else None
        s = handler.sample_rows(table, limit=raw_limit, columns=col_list, caller=caller)
        return _sample_rows_markdown(s)
    except Exception as exc:
        return f"Error sampling rows: {_errors.enrich(str(exc))}"


# ---------------------------------------------------------------------------
# async query jobs (query_submit / status / result / cancel)
# ---------------------------------------------------------------------------


def query_submit(
    sql: str,
    limit: int | None = None,
    params: object | None = None,
    version_as_of: int | None = None,
    *,
    caller=None,
) -> str:
    """Start a read-only query job; returns JSON with the job_id.

    The D2 read-only guard runs at SUBMIT time (a DDL submission is refused
    with the same error run_sql raises, before any job starts). The job runs
    on the same engine path as run_sql — same SQLHANDLER_QUERY_TIMEOUT
    (watchdog-enforced), same SQLHANDLER_MAX_ROWS cap, same audit. The
    registry is in-memory and bounded by SQLHANDLER_MAX_JOBS (default 8).
    """
    result = _jobs.api_job_submit(
        _handler(),
        {"sql": sql, "limit": limit, "params": params, "version_as_of": version_as_of},
        caller=caller,
    )
    if result.get("error"):
        raise JobError(result["error"], status=result.get("status", 429))
    return json.dumps({k: result[k] for k in ("job_id", "state", "note") if k in result})


def query_status(job_id: str) -> str:
    """Poll one query job: state, elapsed, error, columns/n_rows (no rows)."""
    return json.dumps(_jobs.api_job_status(job_id), default=str)


def query_result(job_id: str, output_format: str = "markdown") -> str:
    """Fetch a finished job's result ONCE (markdown/json/csv); then freed.

    A second fetch is refused — the result is released from the registry's
    memory right after the first hand-over. Poll query_status first; a
    running job returns an error telling you to do exactly that.
    """
    arrow = _jobs.api_job_result(job_id)
    return _arrow_to_output(arrow, max_rows=None, fmt=_validate_output_format(output_format))


def query_cancel(job_id: str) -> str:
    """Cancel a running query job (DuckDB interrupt, like the web async API)."""
    return json.dumps(_jobs.api_job_cancel(job_id), default=str)


# ---------------------------------------------------------------------------
# saved parameterized queries (query_save / list / delete / saved)
# ---------------------------------------------------------------------------


def query_save(name, sql, params=None, description=None, request=None, *, caller=None) -> str:
    """Save a parameterized query under a name (writes are auth-gated).

    The SQL is validated at save time (parsed; SELECT-only while the MCP
    read-only mode is on) and parameters are stored as BIND params — the
    stored SQL keeps its $name / ? placeholders and is never
    string-interpolated at run time.
    """
    entry = _saved.api_saved_save(
        {"name": name, "sql": sql, "params": params, "description": description}, request, caller=caller
    )
    return json.dumps({"saved": True, **entry}, default=str)


def query_list(*, caller=None) -> str:
    """List saved queries (name, SQL, params, description).

    ``caller`` (identity spine): owner-scoped when policy enforcement is on
    (a saved query's SQL text can name hidden tables); the shared list when
    enforcement is off (byte-identical).
    """
    entries = _saved.api_saved_list(caller=caller)
    if not entries:
        return "No saved queries yet. Save one with query_save(name, sql, params)."
    lines = [f"Saved queries ({len(entries)}):"]
    for e in entries:
        line = f"  - {e['name']}: {e.get('sql', '')}"
        if e.get("params"):
            line += f" | params: {json.dumps(e['params'], default=str)}"
        if e.get("description"):
            line += f" — {e['description']}"
        lines.append(line)
    return "\n".join(lines)


def query_delete(name, request=None, *, caller=None) -> str:
    """Delete a saved query by name (writes are auth-gated)."""
    result = _saved.api_saved_delete(name, request, caller=caller)
    return f"Deleted saved query {result['deleted']!r}."


def query_saved(
    name: str,
    params: object | None = None,
    limit: int | None = None,
    output_format: str = "markdown",
    version_as_of: int | None = None,
    *,
    caller=None,
) -> str:
    """Run a saved query by name; call-time params override stored ones.

    Parameters travel as BIND params ($name / ?) — never string-interpolated
    into the SQL. The read-only guard is re-applied to the stored SQL at run
    time, so a hand-edited store cannot smuggle DDL past the read-only mode.
    """
    sql, merged, _entry = _saved.api_saved_run(name, {"params": params}, caller=caller)
    arrow = _handler().query_duckdb(sql, limit=limit, params=merged, version_as_of=version_as_of, caller=caller)
    return _arrow_to_output(arrow, max_rows=limit, fmt=_validate_output_format(output_format))


# ---------------------------------------------------------------------------
# agent pack: explain_query + ask_data (implementation review §2c / §2d)
# ---------------------------------------------------------------------------

# ask_data token-budget discipline (review §2d): the caps that keep one
# question's answer inside a model context — candidates listed (5), columns
# described per candidate table (20), columns profiled (6).
_ASK_MAX_TABLES = 5
_ASK_MAX_COLUMNS = 20
_ASK_MAX_PROFILE_COLUMNS = 6


def _confidence(value: object, label: str | None) -> str:
    """Render one estimate as ``value (label)`` — never a bare number."""
    if value is None or label is None:
        return f"{value} (confidence: none)"
    return f"{value} (confidence: {label})"


def _explain_query_markdown(r: dict) -> str:
    """Render one engine.explain_query dict as compact markdown."""
    lines: list[str] = [f"Query plan estimate (NOT executed): `{r['sql']}`", ""]
    if r.get("touches_external"):
        lines.append(
            "Touches an attached external database — no lake metadata exists for it, "
            "so every count below is unknown (degraded honestly)."
        )
    tables = r.get("tables") or []
    if tables:
        lines += [
            "| table | rows | rows confidence | bytes to scan | bytes confidence | snapshot |",
            "|---|---|---|---|---|---|",
        ]
        for t in tables:
            kind = " (VIRTUAL)" if t.get("virtual") else ""
            snap = t.get("snapshot_version")
            lines.append(
                f"| {_md_cell(t['table'] + kind)} | {_md_cell(t.get('rows'))} "
                f"| {_md_cell(t.get('rows_confidence', 'none'))} "
                f"| {_md_cell(t.get('bytes_to_scan'))} "
                f"| {_md_cell(t.get('bytes_confidence', 'none'))} "
                f"| {_md_cell(snap if snap is not None else 'current')} |"
            )
        bytes_note = {"exact": "from Delta/Iceberg file metadata", "approx": "uncompressed parquet row-group totals"}
        known = {t.get("bytes_confidence") for t in tables}
        hints = [bytes_note[c] for c in ("exact", "approx") if c in known]
        if hints:
            lines.append("")
            lines.append(f"Bytes note: {'; '.join(hints)}.")
    else:
        lines.append("(no recognizable table references)")
    wc = r.get("warm_cold") or {}
    if wc:
        lines += ["", "Warm/cold (result cache):"]
        if wc.get("l1"):
            lines.append("- L1 (this replica, memory): WARM — run_sql serves it from the in-memory cache")
        else:
            lines.append("- L1 (this replica, memory): cold")
        if wc.get("l2"):
            lines.append("- L2 (shared disk): WARM — a replica already published this exact result")
        elif wc.get("l2") is False and wc.get("l1") is False:
            lines.append("- L2 (shared disk): cold (or the L2 layer is not configured)")
        bc = wc.get("block_cache")
        if bc is None:
            lines.append("- Block cache: off (n/a)")
        else:
            lines.append("- Block cache: on; per-file warmth not tracked (mixed/unknown)")
    plan = r.get("plan")
    if plan:
        summary = plan.get("summary")
        if summary:
            lines += [
                "",
                "Plan summary (DuckDB's own EXPLAIN — planning only, nothing ran):",
                f"- operators: {summary.get('operators')}, scan nodes: {summary.get('scan_nodes')}",
                (
                    f"- estimated result rows: {_confidence(summary.get('estimated_root_rows'), 'approx')}"
                    if summary.get("estimated_root_rows") is not None
                    else "- estimated result rows: unknown (confidence: none)"
                ),
                (
                    "- pushdown: filters reached the scan operator"
                    if summary.get("pushdown") is True
                    else "- pushdown: no filter observed at any scan (nothing to push, or not pushed)"
                ),
            ]
        elif plan.get("error"):
            lines += ["", f"(plan unavailable: {plan.get('error')})"]
    return "\n".join(lines)


def explain_query(
    sql: str,
    params: object | None = None,
    version_as_of: int | None = None,
    include_plan: bool = False,
    *,
    caller=None,
) -> str:
    """Estimate one read-only query's cost WITHOUT running it.

    Returns markdown: referenced tables with metadata row counts and
    bytes-to-scan (each with a confidence label), the warm/cold band
    (L1/L2/block-cache), and — when ``include_plan`` is set — DuckDB's
    EXPLAIN (planning only; the query's data path never executes).
    Attach-DB queries degrade to confidence "none" everywhere.
    """
    try:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("Provide the SQL to estimate (a SELECT / EXPLAIN SELECT statement).")
        r = _handler().explain_query(
            sql, params=params, version_as_of=version_as_of, include_plan=include_plan, caller=caller
        )
        return _explain_query_markdown(r)
    except Exception as exc:
        return f"Error explaining query: {_errors.enrich(str(exc))}"


def _draft_sql(question: str, describe: dict) -> str:
    """Draft one SELECT for a question over a described table.

    A template over the table's real columns — deliberately generic and
    deliberately NOT executed (execution stays a separate, confirmable
    run_sql call). Catalog column docs steer the default projection when
    present; otherwise the draft is a bounded preview the agent refines.
    ``describe["draft_target"]`` (set by ``ask_data`` from the search hit's
    SQL-addressable name) picks the relation; the fallback quotes whatever
    name the describe carries.
    """
    target = _safe_sql_target(describe)
    columns = [c.get("name", "") for c in describe.get("columns", []) if c.get("name")]
    selected = ", ".join(_safe_ident_col(c) for c in columns[:_ASK_MAX_PROFILE_COLUMNS]) or "*"
    limit = _max_rows()
    return f"SELECT {selected} FROM {target} LIMIT {max(limit, 1)}"


def _safe_sql_target(describe: dict) -> str:
    """The SQL-addressable relation name for a draft, quoted when needed.

    DuckDB cannot reference the logical ``schema/name`` path form (that's
    the discovery key, not SQL): registered views are the qualified name
    (``schema_name``) — or the bare name when globally unique. ``ask_data``
    stamps the search hit's ``qualified_name`` into the describe as
    ``draft_target``; describe/external describes carry their own
    addressable form in ``table`` already (external qualified names are
    valid SQL).
    """
    name = str(describe.get("draft_target") or describe.get("table", ""))
    return name if name.replace("_", "").isalnum() else '"' + name.replace('"', '""') + '"'


def _safe_ident_col(name: str) -> str:
    """Quote one column identifier for the draft SQL when needed."""
    return name if name.replace("_", "").isalnum() else '"' + name.replace('"', '""') + '"'


def _suggest_follow_up(question: str, top: dict, describe: dict) -> str:
    """One concrete next step after the draft (the plan's suggested follow-up)."""
    cols = describe.get("columns") or []
    name = top.get("table", describe.get("table", ""))
    if cols:
        first = str(cols[0].get("name", ""))
        return f"Profile one column before filtering: column_stats(table='{name}', column='{first}')."
    return f"Run describe_table('{name}') to see the full column list before refining the draft."


def _ask_data_markdown(
    question: str,
    matches: list[dict],
    describes: dict[str, dict],
    draft: str | None,
    draft_table: str | None,
    profile: dict | None,
    follow_up: str,
    execution_state: str,
) -> str:
    """Render the ask_data answer: candidates → schema → draft → follow-up."""
    lines = [f"Question: {question}", ""]
    if not matches:
        lines += [
            "No tables matched. Try broader keywords with search_tables, or run list_tables to see everything.",
            "",
            "NOTHING WAS EXECUTED.",
        ]
        return "\n".join(lines)
    lines.append(f"Candidate tables ({len(matches)} of the data source):")
    for m in matches:
        line = f"- {m['table']} ({m['format']})"
        if m.get("description"):
            line += f" — {m['description']}"
        if m.get("matched_columns"):
            line += f" [matched columns: {', '.join(m['matched_columns'][:5])}]"
        lines.append(line)
    primary = matches[0]
    describe = describes.get(primary["table"])
    if describe:
        lines += ["", f"Schema of {primary['table']}:"]
        cols = describe.get("columns") or []
        shown = cols[:_ASK_MAX_COLUMNS]
        for c in shown:
            line = f"- {c.get('name')}: {c.get('type')}"
            if c.get("description"):
                line += f" — {c['description']}"
            lines.append(line)
        hidden = len(cols) - len(shown)
        if hidden > 0:
            lines.append(f"- … and {hidden} more columns (describe_table('{primary['table']}') for the rest)")
        if describe.get("description") and not primary.get("description"):
            lines.append(f"- description: {describe['description']}")
    if profile:
        lines += ["", f"Column statistics (top {_ASK_MAX_PROFILE_COLUMNS}, sampled — see profile_table for all):"]
        for c in profile.get("columns", [])[:_ASK_MAX_PROFILE_COLUMNS]:
            rng = ""
            if c.get("min") is not None or c.get("max") is not None:
                rng = f", range {c.get('min')} … {c.get('max')}"
            lines.append(
                f"- {c.get('name')}: null {c.get('null_pct', '?')}%, distinct≈ {c.get('approx_unique', '?')}{rng}"
            )
    lines += ["", "Drafted SQL (NOT executed — review, then run it explicitly):", "", "```sql"]
    if draft:
        lines.append(draft)
    else:
        lines.append(f"-- no draft: no table matched {question!r}")
    lines += ["```"]
    if draft_table:
        lines.append(f'Run this with run_sql(sql="…", table context: {draft_table}).')
    else:
        lines.append("Run this with run_sql after picking a table with search_tables/list_tables.")
    if profile and profile.get("n_rows") is not None:
        lines.append(
            f"Cost note: {draft_table} has {profile['n_rows']} rows (metadata count, confidence: exact); "
            "the LIMIT keeps the draft bounded."
        )
    lines += ["", f"Suggested follow-up: {follow_up}", "", execution_state]
    return "\n".join(lines)


def ask_data(question: str, execute: bool = False) -> str:
    """Plan one question against the lake: search → describe → draft SQL.

    Composes the existing discovery pieces (search_tables, describe_table,
    optional profile_table) and drafts ONE candidate SQL — WITHOUT executing
    anything. ``execute`` is accepted for call-site symmetry with run_sql but
    deliberately has no effect in this slice (execution stays a separate,
    confirmable ``run_sql`` call — the fleet's applygate plan→apply
    separation); the footer says exactly that.

    Token-budget discipline (review §2d): candidate tables capped at 5,
    columns described at 20, profiled columns at 6; profiling only the top
    hit, only when its schema is small enough to stay inside the budget.
    """
    del execute  # symmetric arg; execution is intentionally out of this tool (see docstring)
    try:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Provide the question to plan (e.g. 'total amount by order kind').")
        handler = _handler()
        matches = handler.search_tables(question, limit=_ASK_MAX_TABLES)
        describes: dict[str, dict] = {}
        profile: dict | None = None
        draft: str | None = None
        draft_table: str | None = None
        follow_up = "Run the draft with run_sql, or refine the WHERE clause with real column values."
        if matches:
            # Describe the top hit (schema for the draft) and, budget-permitting,
            # profile up to 6 columns of it so the draft's filters start honest.
            top = matches[0]
            try:
                describe = handler.describe_table(top["table"])
                # The draft targets the SQL-addressable identifier (the same
                # registered view a run_sql would use), NOT the discovery
                # path form — see _safe_sql_target.
                describe = {**describe, "draft_target": top.get("qualified_name") or top["table"]}
                describes[top["table"]] = describe
                cols = describe.get("columns") or []
                if cols and len(cols) > _ASK_MAX_COLUMNS:
                    cols = cols[:_ASK_MAX_COLUMNS]
                if cols:
                    try:
                        profile = handler.profile_table(
                            top["table"], columns=[c["name"] for c in cols[:_ASK_MAX_PROFILE_COLUMNS]]
                        )
                    except Exception:
                        profile = None  # profiling is optional enrichment
                draft = _draft_sql(question, describe)
                draft_table = top["table"]
                follow_up = _suggest_follow_up(question, top, describe)
            except Exception:
                draft = None  # describe failed: the candidate list still stands
        return _ask_data_markdown(
            question.strip(),
            matches,
            describes,
            draft,
            draft_table,
            profile,
            follow_up,
            "Nothing was executed (execute is accepted for symmetry but has no effect here — "
            "run the drafted SQL with an explicit run_sql call).",
        )
    except Exception as exc:
        return f"Error planning question: {_errors.enrich(str(exc))}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# Hard cap on markdown rows returned to a client, regardless of the requested
# limit (SQLHANDLER_MAX_OUTPUT_ROWS, default 1000). Guards against a client
# asking for an unbounded result set producing a huge payload / OOM.
_MAX_OUTPUT_ROWS = 1000

# E_ROWS_CAPPED fix hints (rendered in the structured tail of capped results).
_ROWS_CAPPED_HINTS = [
    "The result was capped (SQLHANDLER_MAX_OUTPUT_ROWS / the requested limit) — more rows exist upstream.",
    "Narrow with WHERE filters or aggregate; raise the limit for a bounded next page.",
]


def _resolve_scan_limit(limit: int | None) -> int | None:
    """Resolve a scan_table limit per decision D4 (SQLHANDLER_MAX_ROWS).

    ``None``/negative meant "whole table" historically; both resolve to the
    ``SQLHANDLER_MAX_ROWS`` cap — and the SAME resolved value (a positive
    int, or ``None`` when the cap is disabled) feeds the output layer, so
    the raw ``-1`` can never reach ``.head()`` and silently drop the last
    row (the Wave-2 defect: a 50-row table rendered 49 rows). An explicit
    positive limit is honored exactly; an explicit ``0`` stays an explicit
    empty result (the engine's ``head(0)``).
    """
    if limit is None or limit < 0:
        cap = _max_rows()
        return cap if cap > 0 else None
    return limit


def _arrow_to_markdown(arrow, max_rows: int | None = 100) -> str:
    """Render a pyarrow Table as a compact markdown table for an LLM."""
    try:
        raw = os.environ.get("SQLHANDLER_MAX_OUTPUT_ROWS", str(_MAX_OUTPUT_ROWS))
        try:
            cap = int(raw)
        except ValueError:
            cap = _MAX_OUTPUT_ROWS  # garbage env value: fall back to the default cap
        cap = max(cap, 0)
        if cap > 0 and max_rows is not None:
            max_rows = min(max_rows, cap)
        if cap > 0 and arrow.num_rows > cap:
            arrow = arrow.slice(0, cap)
        df = arrow.to_pandas()
        # Boundary rule: only a POSITIVE max_rows may reach .head(). 0 means
        # unlimited (no head at all), and a negative count would silently
        # drop the last row (pandas head(-N) semantics) — never allowed.
        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        return df.to_markdown(index=False)
    except Exception:
        return str(arrow)


def _arrow_to_output(arrow, max_rows: int | None, fmt: str) -> str:
    """Render a pyarrow Table as markdown (default), JSON, CSV, or Arrow IPC.

    All formats share the same row cap (SQLHANDLER_MAX_OUTPUT_ROWS) so a
    machine-readable format can't smuggle an unbounded payload either. JSON
    reuses the web API's payload shape ({columns, rows, n_rows, truncated});
    CSV is pandas' RFC-style rendering (header row, no index); arrow
    base64-encodes the Arrow IPC stream bytes under a one-line header —
    the only text format that round-trips decimals/timestamps/nulls exactly.
    """
    import csv
    import io

    from .webui import arrow_to_ipc_text, arrow_to_payload

    # Boundary rule (decision D4): a non-positive max_rows is "unlimited",
    # never a row count — .head(-1) drops the LAST row and .head(0) drops
    # all of them, so neither may ever be applied to the rendered payload.
    if max_rows is not None and max_rows <= 0:
        max_rows = None

    raw = os.environ.get("SQLHANDLER_MAX_OUTPUT_ROWS", str(_MAX_OUTPUT_ROWS))
    try:
        cap = int(raw)
    except ValueError:
        cap = _MAX_OUTPUT_ROWS
    cap = max(cap, 0)
    capped = False
    if cap > 0 and max_rows is not None:
        max_rows = min(max_rows, cap)
    if cap > 0 and arrow.num_rows > cap:
        arrow = arrow.slice(0, cap)
        capped = True

    fmt = (fmt or "markdown").strip().lower()
    if fmt == "json":
        payload = arrow_to_payload(arrow, limit=max_rows)
        # E_ROWS_CAPPED: JSON already carries `truncated`; the structured
        # tail tells an agent WHY more rows exist and how to reach them.
        if capped or payload.get("truncated"):
            payload["error"] = {"code": _errors.E_ROWS_CAPPED, "fix_hints": _ROWS_CAPPED_HINTS}
        return json.dumps(payload, default=str)
    if fmt == "csv":
        payload = arrow_to_payload(arrow, limit=max_rows)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(payload["columns"])
        writer.writerows(payload["rows"])
        return buf.getvalue()
    if fmt == "arrow":
        # The IPC stream is written AFTER the row cap above, so the payload
        # stays bounded like every other format; the header names the cap
        # when it bit (same additive notice posture as the markdown tail).
        body = arrow_to_ipc_text(arrow)
        if capped:
            body += "\n# rows capped at SQLHANDLER_MAX_OUTPUT_ROWS (more rows exist upstream)"
        return body
    body = _arrow_to_markdown(arrow, max_rows=max_rows)
    if capped or (max_rows is not None and max_rows > 0 and arrow.num_rows >= max_rows):
        # Markdown has no truncated field — the machine-readable cap notice
        # rides as a structured tail line (same additive posture as errors).
        return body + _errors.structured(_errors.E_ROWS_CAPPED, _ROWS_CAPPED_HINTS)
    return body


class _ApiTokenMiddleware:
    """ASGI middleware: require a shared token on every /api/* request.

    Comparison is constant-time (hmac.compare_digest). The token protects
    the JSON API on deployments without gateway auth; the MCP endpoint and
    the UI stay governed by their own layers.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/api"):
            provided = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    provided = v.decode("latin-1")
                    break
                if k == b"x-api-token":
                    provided = v.decode("latin-1")
                    break
            if not observability.token_matches(provided, self.token):
                resp = JSONResponse({"error": "Unauthorized: missing or invalid API token."}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _McpApiKeyMiddleware:
    """ASGI middleware: require an API key on every /mcp request (OPTIONAL).

    Fleet pattern (pcai_utils/mcp_auth.py) — the fleet mcp_auth module is NOT
    importable from this repo (it lives in pcai_utils, hardlink-meshed across
    mcp_servers/*); the copy stays inline and pins its behavior in
    tests/test_mcp_auth.py. The identity spine (sqlhandler.identity) consumes
    its per-request fingerprint recording below — semantics unchanged.

    Semantics (fleet decision 2026-09 — SQL is OPTIONAL-auth): when neither
    MCP_API_KEYS (the fleet-universal var) nor SQLHANDLER_API_KEYS is set,
    /mcp passes through exactly as before (dev/gateway-only deployments);
    when either IS set, a valid key is required — Bearer or X-API-Key, all
    comparisons constant-time, comma-separated keys = the rotation story.
    The env is re-read per request, so a Secret rotation reaches a running
    pod without a restart. /api/* keeps its own _ApiTokenMiddleware; /ui,
    /health, /ready and /metrics are unaffected.

    Identity spine (additive, behavior-neutral): when a key MATCHES, its
    fingerprint (``sha256:<12hex>`` — never the key) is recorded into
    ``scope["state"]["sqlhandler.key_fp"]`` for _CallerIdentityMiddleware
    (which sits INSIDE this one) and the audit/metrics layer. Same envs,
    headers, constant-time compare, fail-open-when-unset posture —
    the mcp-fleet-api-key experience is unchanged (hard constraint).
    """

    _ENV_NAMES = ("MCP_API_KEYS", "SQLHANDLER_API_KEYS")

    #: The scope["state"] slot carrying the matched key's fingerprint.
    KEY_FP_STATE = "sqlhandler.key_fp"

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _keys() -> list:
        keys: list = []
        for name in _McpApiKeyMiddleware._ENV_NAMES:
            for k in os.environ.get(name, "").split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        return keys

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            keys = self._keys()
            if keys:
                provided = ""
                for k, v in scope.get("headers", []):
                    lk = k.lower() if isinstance(k, bytes) else k
                    if lk == b"authorization":
                        scheme, _, token = v.decode("latin-1").partition(" ")
                        if scheme.lower() == "bearer":
                            provided = token.strip()
                        break
                    if lk == b"x-api-key":
                        provided = v.decode("latin-1").strip()
                        break
                matched = _identity.match_api_key(provided, keys)
                if matched is None:
                    resp = JSONResponse(
                        {"error": "unauthorized: missing or invalid API key"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await resp(scope, receive, send)
                    return
                # ADDITIVE identity feed: record WHICH key matched (its
                # fingerprint, never the key) for the inner middleware.
                state = scope.setdefault("state", {})
                state[self.KEY_FP_STATE] = _identity.key_fp(matched)
        await self.app(scope, receive, send)


class _CallerIdentityMiddleware:
    """ASGI middleware: resolve one Caller per request (identity spine, Stage 1).

    Added BEFORE ``add_middleware(_McpApiKeyMiddleware)`` — Starlette applies
    middleware LIFO, so this runs INSIDE the key check and sees the
    fingerprint the key middleware recorded in ``scope["state"]``.

    Pure resolution + recording: it never rejects anything (the 401 posture
    stays _McpApiKeyMiddleware's). The resolved Caller rides
    ``scope["state"]["sqlhandler.caller"]`` where the tool dispatch /
    webui / resource handlers read it, and is also published to
    ``identity.CALLER_CONTEXT`` so the audit writer (and any contextvar-based
    consumer inside the REQUEST's async task) sees it. The QueryJob thread
    still receives the caller EXPLICITLY — contextvars do not cross a raw
    ``threading.Thread``.

    Rungs (see sqlhandler.identity for the full ladder): relay attribution
    headers only over a key-valid request → oauth2-proxy headers only under
    SQLHANDLER_TRUST_BROWSER_HEADERS → matched-key fingerprint → anonymous.
    """

    CALLER_STATE = "sqlhandler.caller"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            caller = _identity.caller_from_scope(scope)
            state = scope.setdefault("state", {})
            state[self.CALLER_STATE] = caller
            # Request-scoped contextvar (audit reads it when no explicit
            # provider is bound). Set inside the request's async context so
            # it never leaks across requests — ContextVar.set is scoped to
            # the current task.
            _identity.CALLER_CONTEXT.set(caller)
        await self.app(scope, receive, send)


def main(argv: list | None = None) -> None:
    # Load .env FIRST so every setting -- including SQLHANDLER_TRANSPORT,
    # which is read by argparse below -- can come from the env file. The
    # later load_dotenv() inside _handler() stays for programmatic callers
    # and is a no-op once variables are set (existing env always wins).
    load_dotenv()
    parser = argparse.ArgumentParser(description="SQLhandler MCP server")
    parser.add_argument(
        "--transport",
        default=os.environ.get("SQLHANDLER_TRANSPORT", "stdio"),
        choices=["stdio", "streamable-http"],
        help="MCP transport (stdio or streamable-http).",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=9097, type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.transport == "stdio":
        asyncio.run(_run_stdio(mcp))
        return

    app = _build_http_app()
    uvicorn.run(app, host=args.host, port=args.port)


def _build_http_app():
    """Assemble the streamable-HTTP Starlette app (MCP + UI + probes + auth).

    Extracted from main() so tests can drive the real app stack (the fleet
    convention in every other server's test suite).
    """
    # Streamable HTTP with the standard initialize handshake AND stateless
    # per-request handling (stateless_http=True). The low-level Server serves
    # the standard handshake so any MCP client can connect, while each request
    # stays self-contained so the deployment can scale to multiple replicas
    # (no in-memory per-pod session to get "Session not found").
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )

    # Read-only web explorer + JSON API (list/describe/query/preview) over
    # the same process-wide engine. Served at / and /ui; API under /api.
    register_ui(app, _handler)

    # Liveness/readiness routes for the k8s probes.
    async def _health(_request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _ready(_request) -> JSONResponse:
        # Backend-aware readiness: only report "ready" when the configured data
        # source is actually reachable (OneLake DFS token+list, S3 list,
        # Iceberg catalog, NFS root, or Delta Sharing GET /shares). If the
        # credential/endpoint breaks, the pod drops out of the Service so
        # traffic stops reaching a dead backend and the failure becomes
        # visible. Disable with SQLHANDLER_READINESS_CHECK=0.
        if os.environ.get("SQLHANDLER_READINESS_CHECK", "1").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return JSONResponse({"status": "ready"})
        try:
            engine = await asyncio.wait_for(asyncio.to_thread(_handler), timeout=2)
            err = await asyncio.wait_for(asyncio.to_thread(engine.provider.check_connection), timeout=15)
        except TimeoutError:
            return JSONResponse({"status": "not ready", "error": "backend check timed out"}, status_code=503)
        except Exception as exc:
            return JSONResponse({"status": "not ready", "error": str(exc)}, status_code=503)
        if err:
            return JSONResponse({"status": "not ready", "error": err}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.add_route("/health", _health)
    app.add_route("/ready", _ready)

    # Prometheus scrape endpoint (Prometheus text exposition; engine gauges
    # included via the same process-wide engine — list_tables is cached so
    # scraping is cheap).
    async def _metrics(_request) -> Response:
        try:
            engine = await asyncio.to_thread(_handler)
        except Exception:
            engine = None
        return Response(
            content=observability.metrics.render(engine),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    app.add_route("/metrics", _metrics)

    # When SQLHANDLER_API_TOKEN is set, every /api/* call must present it
    # (Authorization: Bearer <token> or X-API-Token: <token>) — for machine
    # callers of the JSON API on deployments that are NOT behind the PCAI
    # oauth2-proxy gateway. /mcp, /ui and /health|/ready are unaffected.
    api_token = os.environ.get("SQLHANDLER_API_TOKEN", "").strip()
    if api_token:
        app.add_middleware(_ApiTokenMiddleware, token=api_token)

    # OPTIONAL /mcp API-key gate (fleet decision 2026-09 — SQL is optional
    # until per-user keys/roles land): when neither MCP_API_KEYS (the
    # fleet-universal var) nor SQLHANDLER_API_KEYS is set, /mcp behaves
    # exactly as before; when either is set, /mcp requires a key. Loud
    # startup line either way so the posture is never ambiguous.
    _mcp_keys = _McpApiKeyMiddleware._keys()
    # Caller-identity resolution (identity spine) is added BEFORE the key
    # middleware: Starlette applies middleware LIFO, so _CallerIdentity runs
    # INSIDE the key check and sees the matched key's fingerprint that
    # _McpApiKeyMiddleware records into scope["state"]. Resolution-only —
    # it never rejects (the 401 posture stays the key middleware's).
    app.add_middleware(_CallerIdentityMiddleware)
    app.add_middleware(_McpApiKeyMiddleware)
    if _identity.browser_headers_trusted():
        logging.getLogger("sqlhandler.server").info(
            "Browser-header identity TRUSTED on all routes (%s) — requires the "
            "workload AuthorizationPolicy pinning ingress to the gateway "
            "(identity.trustBrowserHeaders); oauth2-proxy headers resolve the "
            "caller when present.",
            _identity.TRUST_BROWSER_HEADERS_ENV,
        )
    else:
        logging.getLogger("sqlhandler.server").info(
            "Browser-header identity OFF (%s unset) — oauth2-proxy headers are "
            "ignored; identity resolves via relay attribution (over a valid key), "
            "key fingerprint, or anonymous.",
            _identity.TRUST_BROWSER_HEADERS_ENV,
        )
    if _mcp_keys:
        logging.getLogger("sqlhandler.server").info(
            "API-key auth ENABLED on /mcp (sources: MCP_API_KEYS/SQLHANDLER_API_KEYS; "
            "%d key(s) configured — comma-separated lists rotate with zero downtime)",
            len(_mcp_keys),
        )
    else:
        logging.getLogger("sqlhandler.server").warning("=" * 72)
        logging.getLogger("sqlhandler.server").warning(
            "Neither MCP_API_KEYS nor SQLHANDLER_API_KEYS is set — /mcp is OPEN "
            "(optional-auth mode). Set one from a Secret for service-level auth "
            "(the gateway remains the outer layer)."
        )
        logging.getLogger("sqlhandler.server").warning("=" * 72)

    # MCP read-only mode (decision D2): run_sql is SELECT-only unless the
    # operator sets SQLHANDLER_MCP_READONLY=0. Enforced in run_sql(); logged
    # here so the posture is visible at startup either way.
    _log = logging.getLogger("sqlhandler.server")
    if mcp_readonly_enabled():
        _log.info("MCP read-only mode ON (SQLHANDLER_MCP_READONLY): run_sql accepts SELECT-only queries.")
    else:
        _log.warning(
            "SQLHANDLER_MCP_READONLY=0 — MCP run_sql accepts multi-statement DDL. "
            "Attached external catalogs stay read-only regardless."
        )

    # Write tier (review §4): GLOBAL FLAG, DEFAULT FALSE. Logged either way
    # so the posture is never ambiguous; the roots count is named so an
    # operator who flips the flag but forgets the allowlist sees why every
    # write still refuses (fail closed).
    if _writes.writes_enabled():
        _roots = _writes.scratch_roots()
        if _roots:
            _log.warning(
                "WRITE TIER ENABLED (SQLHANDLER_WRITES_ENABLED=1): run_sql admits classified "
                "scratch writes (CTAS/INSERT/COPY) under %d allowlisted root(s) — "
                "<root>/<subject-slug>/... only, single-writer lease enforced. "
                "SQLHANDLER_MCP_READONLY continues to govern multi-statement/DDL exactly as before.",
                len(_roots),
            )
        else:
            _log.warning(
                "WRITE TIER ENABLED but %s is empty — no target can classify "
                "(fail closed). Configure scratch roots to make writes possible.",
                _writes.WRITE_SCRATCH_ROOTS_ENV,
            )
    else:
        _log.info("Write tier OFF (SQLHANDLER_WRITES_ENABLED unset/0 — the default): run_sql is SELECT-only.")

    # Async query jobs (additive): submit/status/result/cancel as MCP tools
    # + /api/jobs/* — the registry is in-memory (a restart clears it), so
    # say so at startup where the operator configures persistence knobs.
    _log.info(
        "Async query jobs enabled: in-memory registry (restart clears it), "
        "cap SQLHANDLER_MAX_JOBS=%d, timeout from SQLHANDLER_QUERY_TIMEOUT; "
        "results are handed over once then freed.",
        _jobs.max_jobs_env(),
    )

    # Saved-query write posture (the audit's poisoning warning): a saved
    # query is a template other agents run, so writes require a credential
    # whenever one is configured. No credential env = known
    # single-user-local mode — writes stay open, logged loudly either way.
    if _saved.auth_configured():
        _log.info(
            "Saved-query writes are gated: %s configured — unauthenticated "
            "query_save/query_delete (MCP or /api/saved-queries) is refused.",
            ", ".join(_saved.credential_sources()),
        )
    else:
        _log.warning(
            "No credential source is configured (SQLHANDLER_API_TOKEN / "
            "MCP_API_KEYS / SQLHANDLER_API_KEYS) — saved-query WRITES are OPEN "
            "(known single-user-local mode). Set a credential to gate them."
        )

    # DNS-rebinding protection on /mcp (ON by default — Origin validation
    # always; strict Host validation when SQLHANDLER_ALLOWED_HOSTS is set).
    _allowed_hosts = _env_list("SQLHANDLER_ALLOWED_HOSTS")
    app.add_middleware(_McpTransportGuard)
    _log.info(
        "MCP transport guard ON (/mcp): origin validation %s, host validation %s",
        "SQLHANDLER_ALLOWED_ORIGINS" if _env_list("SQLHANDLER_ALLOWED_ORIGINS") else "(default: no browser origins)",
        f"SQLHANDLER_ALLOWED_HOSTS={_allowed_hosts}"
        if _allowed_hosts
        else "(no allowlist set — set mcp.allowedHosts to pin)",
    )

    # Browser CORS (decision D3): same-origin only by default. The old
    # default ("*" via SQLHANDLER_CORS_ORIGINS) let ANY web page read
    # /api/* and drive /mcp cross-origin. SQLHANDLER_ALLOWED_ORIGINS lists
    # extra browser origins (comma-separated); the legacy
    # SQLHANDLER_CORS_ORIGINS is still honored when explicitly set (it used
    # to default to "*", so unset now means "no cross-origin at all").
    # With no middleware, responses carry no Access-Control-Allow-Origin —
    # browsers refuse cross-origin reads; same-origin requests are
    # unaffected, and _McpTransportGuard blocks browser-originated /mcp
    # traffic regardless of middleware order.
    origins = _env_list("SQLHANDLER_ALLOWED_ORIGINS") or _env_list("SQLHANDLER_CORS_ORIGINS")
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        )
        _log.info("CORS scoped to %d origin(s): %s", len(origins), ", ".join(origins))
    else:
        _log.info("CORS: same-origin only (no SQLHANDLER_ALLOWED_ORIGINS configured).")

    # Response compression (default ON — agent-facing tool results are
    # markdown/JSON-heavy text, so gzip compresses them 5-10x; the ingress hop
    # and slow callers both win). Two deliberate properties:
    #
    # ORDER — added BETWEEN the CORS block and the outermost probes gate, and
    # Starlette applies middleware LIFO, so the effective request flow is
    # _ProbesAuthMiddleware -> GZip -> CORS -> _McpTransportGuard -> key gates
    # (verified against build_middleware_stack in tests/test_compression.py).
    # GZip runs INNERMOST of the policy layers: auth/guard/CORS all see the
    # request first and decide on the uncompressed request; responses are
    # compressed only after every outer layer has signed off, so no auth layer
    # ever inspects (or must decompress) a compressed body, and a 401/421
    # refusal still reaches the client as plain bytes. CORS sits directly
    # outside GZip — it only rewrites headers, so compressed bodies pass
    # through it untouched.
    #
    # STREAMING SAFETY — the MCP streamable-HTTP transport can answer with SSE
    # (EventSourceResponse, media_type text/event-stream) and Starlette's
    # GZipMiddleware handles both shapes correctly: text/event-stream is in its
    # default EXCLUDED content types (passes through byte-for-byte, no
    # buffering), and plain chunked responses are compressed CHUNK-WISE with a
    # Z_SYNC_FLUSH per chunk — verified in tests/test_compression.py by reading
    # a streamed response incrementally. json_response=True (our /mcp mode)
    # answers POSTs with buffered JSON anyway; only the optional GET/SSE side
    # streams. /metrics and /health|/ready are ordinary buffered responses:
    # /metrics compresses fine (Prometheus scrapers send Accept-Encoding), the
    # tiny probe bodies fall under min_size and pass through untouched.
    _compression = load_compression_config()
    if _compression.mode == "gzip":
        app.add_middleware(GZipMiddleware, minimum_size=_compression.min_size)
        _log.info(
            "Response compression ON (gzip, min_size=%d bytes; SQLHANDLER_COMPRESSION / "
            "SQLHANDLER_COMPRESSION_MIN_SIZE). SSE responses are excluded by the middleware "
            "and stay uncompressed.",
            _compression.min_size,
        )
    else:
        _log.info("Response compression OFF (SQLHANDLER_COMPRESSION=off).")

    # Optional auth gate on /metrics + /ready (default OFF = unchanged).
    app.add_middleware(_ProbesAuthMiddleware)
    if _truthy("SQLHANDLER_METRICS_AUTH"):
        if not _ProbesAuthMiddleware._expected():
            _log.warning(
                "SQLHANDLER_METRICS_AUTH=1 but no credential source is configured "
                "(SQLHANDLER_API_TOKEN / MCP_API_KEYS / SQLHANDLER_API_KEYS) — "
                "/metrics and /ready will 401 for EVERYONE (fail closed)."
            )
        else:
            _log.info("Auth gate ON for /metrics (SQLHANDLER_METRICS_AUTH); /ready stays open for kubelet probes.")
    return app


if __name__ == "__main__":
    main()
