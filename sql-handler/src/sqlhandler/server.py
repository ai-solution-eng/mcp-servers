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
  * whoami             - the caller's own identity + their own per-table ACL view
  * search_tables      - keyword search over names/columns/catalog descriptions
  * describe_table     - inspect columns/types of a table
  * profile_table      - column-level statistics (min/max, null %, distinct,
                         quantiles) so agents write correct filters first try
  * run_sql            - execute SQL via DuckDB (aggregations etc.); output
                         as markdown, JSON, CSV or Arrow IPC
  * scan_table         - pull rows via pyarrow with column projection + limit

Admin tools (admin-designation gated — policy ``admins:`` list):
  * admin_grants       - the whole grants view (keys, assignments, blocked, admins)
  * admin_policy_set   - replace the policy document (validated first)
  * admin_key_mint     - mint one API key (raw key returned ONCE)
  * admin_key_revoke   - revoke one key by fingerprint
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

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
from . import admin_keys as _admin_keys
from . import errors as _errors
from . import identity as _identity
from . import jobs as _jobs
from . import oidc_identity as _oidc_identity
from . import policy as _policy
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
from .webui import _BodyLimitMiddleware, _SecurityHeadersMiddleware, register_ui

logger = logging.getLogger("sqlhandler")

# The OIDC rung's static-key source: a Bearer value matching a configured
# static key is the KEY rung's credential and is NEVER parsed as a JWT.
# The callable is registered at the middleware-definition site below (after
# _McpApiKeyMiddleware exists) and re-reads the env per call, so key
# rotation needs no restart.

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
        elif name == "whoami":
            return whoami(caller=caller), False
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
            return query_status(str(args.get("job_id", "")), caller=caller), False
        elif name == "query_result":
            return (
                query_result(str(args.get("job_id", "")), args.get("output_format") or "markdown", caller=caller),
                False,
            )
        elif name == "query_cancel":
            return query_cancel(str(args.get("job_id", "")), caller=caller), False
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
        elif name == "admin_grants":
            return admin_grants(caller=caller), False
        elif name == "admin_policy_set":
            doc = args.get("policy")
            if not isinstance(doc, str):
                return (
                    "Error setting policy: 'policy' must be the FULL policy document as a JSON string"
                    + _errors.structured(
                        _errors.E_PARAM_INVALID,
                        ['Call admin_policy_set with {"policy": "{...}"} — the whole document, JSON-encoded.'],
                    ),
                    True,
                )
            return admin_policy_set(doc, caller=caller), False
        elif name == "admin_key_mint":
            label = args.get("label")
            if label is not None and not isinstance(label, str):
                return _errors.enrich("Error minting key: label must be a string."), True
            assign = args.get("assign")
            if assign is not None and not isinstance(assign, list):
                return _errors.enrich("Error minting key: assign must be a list of glob strings."), True
            return admin_key_mint(label, assign, caller=caller), False
        elif name == "admin_key_revoke":
            return admin_key_revoke(str(args.get("fp", "")), caller=caller), False
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
            "List every table in the data source — START HERE before any query, "
            "plus a live inventory of any attached (read-only) external databases. "
            "Returns lake table names with one-line catalog descriptions when "
            "present. When this list is long or you have keywords but no table "
            "name, use search_tables instead; follow with describe_table on a "
            "candidate."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="whoami",
        description=(
            "Report the caller's OWN resolved identity (class/subject — never a raw "
            "key), which ladder rung resolved it (relay | jwt | browser | key | "
            "anonymous | stdio), whether the identity-required gate is on, and the "
            "caller's OWN policy view per table (visible / row_filter / "
            "masked_columns / hidden — hidden tables listed with visible:false). "
            "Use it to confirm authentication and see exactly which tables and "
            "columns your credentials expose before writing queries."
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
            "Find tables by keyword — the fast entry point when the table list "
            "is long or you don't know which table holds what. Matches against "
            "table and column names, human-written semantic-catalog "
            "descriptions and aliases (business terms like 'work orders'), and "
            "column documentation — so business-language queries like 'customer "
            "churn' or 'order amounts' surface the right table even when no "
            "name matches literally. Ranked best-first: exact/substring hits "
            "outrank fuzzy near-miss matches for typos. Returns each match "
            "with its description and matched columns; follow with "
            "describe_table on the best hit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords — business terms work ('work order amount', "
                        "'customer churn'); matches names, aliases, and column docs."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="run_sql",
        description=(
            "Execute one SQL SELECT against the lake tables and return rows — "
            "the workhorse for 'run this query / count / aggregate / join / top-N'. "
            "Read-only: non-SELECT statements (INSERT, CREATE, DROP, ATTACH, COPY, "
            "PRAGMA, multi-statement scripts) are refused. Reference tables by "
            "their listed name (e.g. work_order_header, or schema/name); attached "
            "external databases (see list_tables) are addressed as "
            "<db-alias>.<schema>.<table> and joinable with lake tables in one "
            "query. Workflow: list_tables / search_tables -> describe_table / "
            "profile_table -> this. Estimate an expensive query first with "
            "explain_query; a query that may outlive the tool-call timeout belongs "
            "in query_submit. To chart the result, pass the SAME sql to a charting "
            "tool (e.g. the seaborn MCP plot tool's `sql` argument) — this server "
            "returns data, not charts."
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
                        "Time travel: read the table as of a Delta snapshot version "
                        "(nfs/onelake) or Iceberg snapshot id."
                    ),
                },
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="scan_table",
        description=(
            "Fetch raw rows/columns from one table WITHOUT writing SQL (pyarrow "
            "scan) — for 'just give me the rows/columns of <table>', or to hand "
            "exact-dtype Arrow data to a programmatic caller. Prefer run_sql "
            "whenever filters/aggregations/joins can be pushed into the query "
            "(they prune the scan). Unlike sample_rows (which adds per-column "
            "fill/null rates for filter planning), this is the plain row/column "
            "fetch."
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
            "Preview a table's actual rows plus per-column fill rates (fill %, "
            "null counts), in one bounded call — the 'what does this data look "
            "like' step before writing SQL. Use describe_table for types, "
            "profile_table/column_stats for full statistics — this shows the "
            "rows themselves. The scan stops early (never reads the whole table "
            "for a small sample) and is capped by SQLHANDLER_PROFILE_MAX_ROWS."
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
            "Start a read-only SQL query as a background job; returns its job_id "
            "immediately. Use INSTEAD of run_sql when the query may outlive the "
            "tool-call timeout (big scans, heavy joins). Same read-only guard as "
            "run_sql, enforced at submit time (DDL refused). Then: poll "
            "query_status, fetch the finished result ONCE with query_result (a "
            "second fetch is refused — resubmit instead), and query_cancel to "
            "stop a running job. Same row caps and audit as run_sql; the "
            "registry holds at most SQLHANDLER_MAX_JOBS (default 8) jobs and is "
            "cleared on restart. Multi-replica deployments need the operator's "
            "SQLHANDLER_JOBS_DIR shared store for cross-replica fetch; under "
            "policy enforcement jobs are owner-scoped."
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
            "Poll an async query job (from query_submit): state "
            "(running/done/error/cancelled), elapsed_ms, error, and — when done "
            "and not yet fetched — the column names and row count. No row data; "
            "fetch rows with query_result. With the operator's SQLHANDLER_JOBS_DIR "
            "shared store any replica can poll any job; under policy enforcement "
            "jobs are owner-scoped (another caller's job id reads as unknown)."
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
            "json, csv, arrow) — a second fetch of the same job is refused "
            "(resubmit instead), so capture the output the first time. On "
            "multi-replica deployments (SQLHANDLER_JOBS_DIR shared store) the "
            "once-only contract holds cluster-wide; under policy enforcement "
            "another caller's job is refused as unknown before any rows are "
            "handed over."
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
        description=(
            "Cancel a running async query job (DuckDB interrupt). Owner-scoped "
            "under policy enforcement: another caller's job reads as unknown."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "The job id from query_submit."}},
            "required": ["job_id"],
        },
    ),
    Tool(
        name="query_save",
        description=(
            "Save a parameterized SQL query under a name for reuse with "
            "query_saved: query_save(name, sql, params, description). The SQL is "
            "validated at save time (parsed; SELECT-only while the read-only mode "
            "is on) and params are stored as BIND parameters ($name / ? "
            "placeholders — never string-interpolated). Saves are auth-gated when "
            "the deployment has API keys configured; single-user-local mode "
            "allows them."
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
            "Estimate one read-only query's cost WITHOUT running it — use when a "
            "query looks slow or expensive, before run_sql. Reports referenced "
            "tables with metadata row counts and bytes-to-scan (each labeled "
            "exact/approx/none), the warm/cold band (is the exact result already "
            "in the L1/L2 result cache), and — with include_plan — DuckDB's own "
            "EXPLAIN tree summary (planning only; the query's data path never "
            "executes). Attached-database queries report confidence none."
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
            "Turn a plain-language data question into a SQL plan — never executes. "
            "For 'how many work orders are overdue?' / 'answer this question about "
            "the data': keyword-searches the tables (top 5), describes the best hit "
            "(up to 20 columns, + catalog docs), optionally profiles up to 6 of its "
            "columns, then drafts one candidate SELECT and a suggested follow-up. "
            "Output is markdown ending in a 'run this with run_sql' footer — "
            "execution is ALWAYS a separate, explicit run_sql call (the plan/apply "
            "separation); the execute argument is accepted for symmetry but has no "
            "effect. If the answer should be a chart, run the draft, then hand the "
            "same SQL to a charting tool (e.g. the seaborn MCP plot tool's `sql` "
            "argument)."
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
    # ---- admin twins (task-6): the administration plane. Registered like
    # every other tool so tools/list is the single contract surface, but
    # each one is ADMIN-GATED AT THE TOOL LEVEL (require_admin's is_admin
    # check in-process — MCP has no HTTP routes for middleware to guard);
    # a non-admin caller gets the SAME 403-shaped structured error.
    Tool(
        name="admin_grants",
        description=(
            "ADMIN (policy-designated admins only). The whole grants view: "
            "designated admins, the raw datasets document (or null), "
            "policy_text (the WHOLE authored policy document as YAML — the "
            "editable truth the UI editor prefills from), the assignments "
            "map (identity -> globs), the blocked globs, group names, the "
            "policy hash, and every API key (minted keys with "
            "label/created_at/created_by/source:file; bootstrap Secret keys "
            "fp-only with source:secret — not removable here). Never returns "
            "a raw key."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="admin_policy_set",
        description=(
            "ADMIN (policy-designated admins only). Replace the policy "
            "document: pass the FULL document as a JSON or YAML string "
            "(datasets form AND/OR groups form + optional admins list — the "
            "same shapes the policy file accepts; admin_grants' policy_text "
            "round-trips as-is). Validated FIRST (an invalid document is "
            "refused with the loader's message and the previous policy "
            "keeps enforcing), then written atomically; the mtime "
            "hot-reload picks it up. Returns the new policy hash."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "policy": {
                    "type": "string",
                    "description": (
                        "The FULL policy document as a JSON or YAML string, "
                        'e.g. \'{"datasets": {"global": ["workorder/*"], '
                        '"assignments": {...}}, "admins": ["sha256:..."]}\' '
                        "or the same document in YAML (as admin_grants' "
                        "policy_text serves it)."
                    ),
                },
            },
            "required": ["policy"],
        },
    ),
    Tool(
        name="admin_key_mint",
        description=(
            "ADMIN (policy-designated admins only). Mint one API key: "
            "generates the secret, stores ONLY its fingerprint + sha256 "
            "(never the raw), binds the assignment into the policy's "
            "datasets.assignments (datasets form required — the groups form "
            "binds by hand), and returns the raw key ONCE — copy it now, it "
            "is not recoverable. The minted key authenticates on /mcp "
            "immediately (the key middleware matches the store live)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Free-text label for the key's owner/purpose (audit + UI display).",
                },
                "assign": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        'Optional dataset globs to grant, e.g. ["workorder/*"]. '
                        'Omitted = ["*"] (full access) recorded in the policy.'
                    ),
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="admin_key_revoke",
        description=(
            "ADMIN (policy-designated admins only). Revoke one minted key "
            "by fingerprint (the sha256:<12hex> admin_grants lists): drops "
            "the store entry AND its policy assignment. Secret-managed keys "
            "are refused (409 — revoke via kubectl, the Secret's lifecycle); "
            "an unknown fingerprint is a 404-shaped error."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fp": {
                    "type": "string",
                    "description": "The key fingerprint to revoke, 'sha256:' + 12 hex (from admin_grants).",
                },
            },
            "required": ["fp"],
        },
    ),
]


def mcp_tool_specs() -> list[dict]:
    """The registered tools as plain dicts: [{name, description, inputSchema}].

    The WEB UI's Inspector tab (webui.py ``/api/inspector/*``) serves this so
    the browser sees the exact tool surface an MCP client's tools/list sees —
    one source of truth (_TOOLS), no second table to drift. Re-derived per
    call (a Tool is a pydantic model; 23 items is cheap) so test monkeypatching
    is reflected too. Tools only — resources/prompts stay out of scope, the
    same boundary an MCP client's tools/list draws.
    """
    return [{"name": t.name, "description": t.description or "", "inputSchema": t.input_schema} for t in _TOOLS]


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
        "Read-only SQL analytics over columnar lake tables (Delta/Parquet/Iceberg) "
        "plus attached read-only external databases. Every non-SELECT statement is "
        "refused. "
        "Workflow: 1) list_tables (or search_tables for keyword/fuzzy lookup) to "
        "find real table names — never guess. "
        "2) describe_table for columns/types; profile_table / column_stats / "
        "sample_rows for value ranges, null rates, top values — check these BEFORE "
        "writing WHERE clauses. "
        "3) run_sql for the query. Estimate expensive queries first with "
        "explain_query (bytes-to-scan, warm/cold cache, optional EXPLAIN). A query "
        "that may outlive the tool-call timeout: query_submit, poll query_status, "
        "fetch ONCE with query_result, query_cancel to stop. "
        "4) Plain-language questions: ask_data drafts SQL + a follow-up; nothing "
        "executes — run the draft yourself with an explicit run_sql. "
        "Charts: this server returns data, not charts — pass the same SQL to a "
        "charting tool (the seaborn MCP plot tool takes a `sql` argument). "
        "Time travel: version_as_of (Delta snapshot version / Iceberg snapshot id) "
        "reads history. Results render as markdown | json | csv | arrow; rows are "
        "capped by limit / SQLHANDLER_MAX_ROWS (default 1000)."
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


def _ready_response(status_code: int, error: str) -> JSONResponse:
    """Build the /ready body for a failing backend check (drift-aware).

    The HTTP status follows the check's age: a first failure still returns
    200 {status: degraded} so the kubelet does NOT drain the pod for a
    blip; once SQLHANDLER_READY_DEGRADED_GRACE of continuous failure has
    elapsed it becomes 503 (the historical outcome). Callers that want the
    raw verdict read "backend_ok" / "degraded".
    """
    rollup = observability.drift.rollup()
    if rollup >= 1.0:  # unreachable here (only called on failure) but safe
        return JSONResponse({"status": "ready"})
    failed_s = round(observability.drift.failed_seconds(), 1)
    if rollup > 0.0:
        return JSONResponse(
            {
                "status": "degraded",
                "backend_ok": False,
                "degraded": True,
                "failing_for_s": failed_s,
                "error": error,
            }
        )
    return JSONResponse(
        {"status": "not ready", "backend_ok": False, "failing_for_s": failed_s, "error": error},
        status_code=status_code,
    )


class _DriftGateMiddleware:
    """ASGI middleware: shed NEW SQL work while the backend check is failing.

    Active only when SQLHANDLER_READY_DRIFT_GATE=1. Gate classes: the /api
    run/scan endpoints (calls that would otherwise queue a doomed scan into
    the concurrency gate); /mcp is EXEMPT (agents must still be able to
    interrogate the failure); /health, /ready, /metrics and UI pages pass
    through. Off by default — enabling it is a per-site decision (it trades
    "always answer something" for "fail fast on doomed work").
    """

    _GATED_PREFIXES = ("/api/query", "/api/scan", "/api/sample", "/api/profile")

    def __init__(self, app):
        self.app = app
        self.enabled = os.environ.get("SQLHANDLER_READY_DRIFT_GATE", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    async def __call__(self, scope, receive, send):
        gated = scope["type"] == "http" and scope.get("path", "").startswith(self._GATED_PREFIXES)
        if not (self.enabled and gated):
            await self.app(scope, receive, send)
            return
        if observability.drift.backend_failed():
            resp = JSONResponse(
                {
                    "error": "Backend degraded: the data source check is failing "
                    "(SQLHANDLER_READY_DRIFT_GATE refuses new scans while the "
                    "readiness probe is in its degraded band). Retry shortly."
                },
                status_code=503,
            )
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


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


def _seed_policy_from_configmap() -> None:
    """One-time policy migration: copy the SEED file (the read-only
    ConfigMap mount, SQLHANDLER_POLICY_SEED_FILE) to the LIVE policy file
    (the writable claim, SQLHANDLER_POLICY_FILE) when the live file does
    not exist yet (values: security.policy.existingClaim +
    seedFromConfigMap).

    Claim mode without a live file = an empty policy store — every gated
    surface fails closed, so seeding on first boot is what makes the
    ConfigMap→PVC switch zero-downtime for existing deployments. Idempotent
    by construction: once the live file exists, the seed is ignored FOREVER
    (the live file is the truth from then on; ConfigMap edits stop
    applying — the documented trade of the writable-policy mode).

    Best-effort: any failure logs loudly and continues (the pod must boot;
    an operator can seed by hand via kubectl exec cp).
    """
    import shutil

    seed = os.environ.get("SQLHANDLER_POLICY_SEED_FILE", "").strip()
    live = os.environ.get("SQLHANDLER_POLICY_FILE", "").strip()
    if not seed or not live or seed == live:
        return
    if os.path.exists(live):
        return  # the live file exists — the seed's one-time job is done
    try:
        os.makedirs(os.path.dirname(os.path.abspath(live)), exist_ok=True)
        shutil.copyfile(seed, live)
        logger.info("policy seeded: %s -> %s (one-time migration; ConfigMap edits stop applying)", seed, live)
    except OSError as exc:
        logger.error("policy seeding FAILED (%s -> %s): %s — seed by hand: kubectl exec cp", seed, live, exc)


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
            # One-time policy migration BEFORE anything reads the policy
            # store (the keys store + every admin route read it).
            _seed_policy_from_configmap()
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
    """List every table in the data source — START HERE before any query.

    Returns lake table names with one-line catalog descriptions when present,
    plus any attached read-only external databases with their fully-qualified
    <db-alias>.<schema>.<table> names. When this list is long or you have
    keywords but no table name, use search_tables instead; follow with
    describe_table on a candidate.

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
        # fastrender.profile_to_markdown: the same pandas-dtype-faithful
        # rendering without the DataFrame materialization (fuzz-verified
        # byte-identical incl. the object-dtype/fobj transform rules); falls
        # back to pandas for any shape outside its proven contract.
        from .fastrender import profile_to_markdown

        body = profile_to_markdown(p["columns"])
        if body is None:
            import pandas as pd

            body = pd.DataFrame(p["columns"]).to_markdown(index=False)
        return "\n".join(header) + "\n\n" + body
    except Exception as exc:
        return f"Error profiling table: {_errors.enrich(str(exc))}"


def search_tables(query: str, *, caller=None) -> str:
    """Find tables by keyword — the fast entry point when the table list is
    long or you don't know which table holds what.

    Matches against table and column names, human-written semantic-catalog
    descriptions and aliases (business terms like 'work orders'), and column
    documentation — so business-language queries like 'customer churn' or
    'order amounts' surface the right table even when no name matches
    literally. Ranked best-first: exact/substring hits outrank fuzzy
    near-miss matches for typos. Never triggers a per-table schema fetch.
    """
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


def whoami(*, caller=None) -> str:
    """The caller's own identity + their OWN ACL view, per table.

    Returns the resolved Caller (audit-safe shape: class/subject/key_fp —
    never a raw key), which ladder rung resolved it, whether the
    identity-required gate is currently on, and ONE entry per provider
    table computed through the engine's ``_effective_rule(info, caller)``:
    visible / row_filter / masked_columns / hidden. The table list is the
    UNFILTERED provider list (engine.list_tables would already omit hidden
    tables; here the point is to SHOW the caller their own view of
    everything) — hidden tables appear with ``visible: false``. Honest for
    anonymous callers too: their default-group view, or everything when no
    policy is configured. A caller NEVER sees another user's view: the
    rules are computed for THIS caller only.
    """
    try:
        effective_caller = caller if caller is not None else _identity.ANONYMOUS
        handler = _handler()
        physical = handler._provider_tables()
        tables = list(physical) + list(handler._virtual_infos(physical))
        datasets: list[dict] = []
        for info in tables:
            try:
                rule = handler._effective_rule(info, effective_caller)
            except Exception:
                # Fail-closed honesty: an unresolvable rule must never
                # render the table as open — mark it hidden + not visible.
                datasets.append(
                    {
                        "table": info.qualified_name,
                        "visible": False,
                        "row_filter": None,
                        "masked_columns": [],
                        "hidden": True,
                    }
                )
                continue
            datasets.append(
                {
                    "table": info.qualified_name,
                    "visible": not rule.hidden,
                    "row_filter": rule.row_filter,
                    "masked_columns": sorted(rule.column_masks.keys()),
                    "hidden": bool(rule.hidden),
                }
            )
        payload = {
            "caller": effective_caller.as_audit_dict(),
            "via": effective_caller.via,
            "require_identity": _IdentityRequiredMiddleware._required(),
            "datasets": datasets,
        }
        return json.dumps(payload, indent=2, default=str)
    except Exception as exc:
        return f"Error computing whoami: {_errors.enrich(str(exc))}"


def _whoami_rest_payload(request) -> dict:
    """The REST /api/whoami body — the same contract the MCP whoami tool
    returns, as JSON for the web UI's header identity widget (D-UI, 2026-10).

    Resolution mirrors the ADMIN surface (``_admin_resolve_caller``), not
    the bare ladder: an X-API-Key / Bearer presented on the request is
    authenticated HERE (the /api routes have no key middleware), and a
    key-shaped value that matches NOTHING stays anonymous — a wrong key is
    never redeemed as the browser user behind it (the same contract
    ``require_admin`` pins). A genuinely keyless request resolves through
    the full ladder (SSO bearer JWT rung → oauth2-proxy browser rung).

    The payload previews the identity — it is NOT a grant: it answers
    "who would this request resolve as" so the UI can render the header
    chip (``Signed in as …``) and tell an SSO visitor why self-mint will
    or will not accept them (``via``). Audit-safe by construction: the
    caller renders through ``as_audit_dict`` (class/subject/fp — never a
    raw key) and the response never says whether the caller is an ADMIN
    (an admin designation must not be probeable from an unauthenticated
    page; the Access-control panel's own calls already 401/403 loudly).
    """
    presented = _admin_presented_keys(request)
    caller = _admin_resolve_caller(request, presented)
    if caller is None:
        caller = _identity.ANONYMOUS
    # The D22 sign-in hint: when anonymous AND the SSO flow is served, the
    # header chip offers "Sign in with SSO" instead of only the key modal.
    # A config read only — no identity leak (the flow is public knowledge).
    from . import oidc_sso

    return {
        "caller": caller.as_audit_dict(),
        "via": caller.via,
        "authenticated": not caller.is_anonymous,
        "require_identity": _IdentityRequiredMiddleware._required(),
        "sso_login_available": oidc_sso.sso_enabled(),
    }


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
    """Execute one SQL SELECT against the lake tables and return rows.

    The workhorse for "run this query / count / aggregate / join / top-N".
    Workflow: list_tables / search_tables -> describe_table / profile_table ->
    this. Estimate an expensive query first with explain_query; a query that
    may outlive the tool-call timeout belongs in query_submit. To chart the
    result, pass the SAME sql to a charting tool (e.g. the seaborn MCP plot
    tool's ``sql`` argument) — this server returns data, not charts.

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
        version_as_of: Optional historical snapshot for time travel: a Delta
            snapshot version (nfs/onelake) or Iceberg snapshot id, applied to
            every versionable table the query touches.
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
    """Fetch raw rows/columns from one table WITHOUT writing SQL (pyarrow scan).

    For "just give me the rows/columns of <table>" — no filters, no SQL.
    Prefer run_sql whenever filters/aggregations/joins can be pushed into the
    query (they prune the scan); use this for a bounded columnar slice to
    inspect, or to hand exact-dtype Arrow data to another tool. Unlike
    sample_rows (which adds per-column fill/null rates for filter planning),
    this is the plain row/column fetch.

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
        version_as_of: Optional historical snapshot for time travel: a Delta
            snapshot version (nfs/onelake) or Iceberg snapshot id.
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
    """Preview a table's actual rows plus per-column fill rates, in one call.

    The "what does this data look like" step before writing SQL. Use
    describe_table for types, profile_table/column_stats for full statistics —
    this shows the rows themselves. Physical
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


def _job_owner(caller) -> str | None:
    """The caller's owner scope for async query jobs (or None = unowned).

    The SAME derivation the saved-query write gate uses (saved.py):
    ``policy.owner_key(caller)`` — subject slug or key fingerprint, never a
    raw key — but only when policy enforcement is ON; enforcement off keeps
    jobs unowned and every owner-related behavior byte-identical (the
    historical shared posture). jobs.py stores what it is given and never
    derives identities itself.
    """
    if caller is not None and _policy.policy_enabled():
        return _policy.owner_key(caller)
    return None


def query_submit(
    sql: str,
    limit: int | None = None,
    params: object | None = None,
    version_as_of: int | None = None,
    *,
    caller=None,
) -> str:
    """Start a read-only SQL query as a background job; returns JSON with the job_id.

    Use INSTEAD of run_sql when the query may outlive the tool-call timeout
    (big scans, heavy joins). Then: poll query_status, fetch the finished
    result ONCE with query_result (a second fetch is refused — resubmit
    instead), and query_cancel to stop a running job.

    The D2 read-only guard runs at SUBMIT time (a DDL submission is refused
    with the same error run_sql raises, before any job starts). The job runs
    on the same engine path as run_sql — same SQLHANDLER_QUERY_TIMEOUT
    (watchdog-enforced), same SQLHANDLER_MAX_ROWS cap, same audit. The
    registry is in-memory and bounded by SQLHANDLER_MAX_JOBS (default 8).

    Under policy enforcement the job is OWNER-SCOPED: only the submitting
    caller's owner scope may poll/fetch/cancel it; anyone else sees the
    same unknown-job 404 a bogus id gets.
    """
    result = _jobs.api_job_submit(
        _handler(),
        {"sql": sql, "limit": limit, "params": params, "version_as_of": version_as_of},
        caller=caller,
        owner=_job_owner(caller),
    )
    if result.get("error"):
        raise JobError(result["error"], status=result.get("status", 429))
    return json.dumps({k: result[k] for k in ("job_id", "state", "note") if k in result})


def query_status(job_id: str, *, caller=None) -> str:
    """Poll one query job: state, elapsed, error, columns/n_rows (no rows).

    Owner-scoped under policy enforcement (a foreign caller's job reads as
    unknown — see query_submit).
    """
    return json.dumps(_jobs.api_job_status(job_id, owner=_job_owner(caller)), default=str)


def query_result(job_id: str, output_format: str = "markdown", *, caller=None) -> str:
    """Fetch a finished job's result ONCE (markdown/json/csv); then freed.

    A second fetch is refused — the result is released from the registry's
    memory right after the first hand-over. Poll query_status first; a
    running job returns an error telling you to do exactly that.
    Owner-scoped under policy enforcement (see query_submit).
    """
    arrow = _jobs.api_job_result(job_id, owner=_job_owner(caller))
    return _arrow_to_output(arrow, max_rows=None, fmt=_validate_output_format(output_format))


def query_cancel(job_id: str, *, caller=None) -> str:
    """Cancel a running query job (DuckDB interrupt, like the web async API).

    Owner-scoped under policy enforcement (see query_submit).
    """
    return json.dumps(_jobs.api_job_cancel(job_id, owner=_job_owner(caller)), default=str)


# NOTE on query_list_jobs: jobs.api_job_list() (the owner-scoped listing over
# the registry) exists and is ready to expose, but the MCP tool is
# deliberately NOT added yet: test_dispatch_arg_contract.py pins the exact
# _TOOLS name→required-args map as the enforced contract, and advertising a
# new tool requires updating that pinned expectation in the same change.
# Add together when wanted: the tool function, the dispatch elif arm, the
# _TOOLS Tool(...) and the pinned-map entry.


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
    """Turn a plain-language data question into a SQL plan — never executes.

    For "how many work orders are overdue?" / "answer this question about
    the data": composes the existing discovery pieces (search_tables,
    describe_table, optional profile_table) and drafts ONE candidate SQL —
    WITHOUT executing anything. ``execute`` is accepted for call-site
    symmetry with run_sql but deliberately has no effect in this slice
    (execution stays a separate, confirmable ``run_sql`` call — the fleet's
    plan→apply separation); the footer says exactly that. If the answer
    should be a chart, run the draft, then hand the same SQL to a charting
    tool (e.g. the seaborn MCP plot tool's ``sql`` argument).

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
    """Render a pyarrow Table as a compact markdown table for an LLM.

    Bench follow-up (2026-09): the pure-Arrow renderer (fastrender) renders
    from Arrow chunks — no pandas DataFrame materialization, no per-cell
    boxing — and is byte-identical to the pandas path for every type in its
    proven contract (tests/test_fastrender.py pins the contract per-type
    against the real pandas/tabulate stack, plus a seeded differential fuzz).
    When the table falls outside the contract (nested/binary/decimal types,
    multiline cells, tabulate internals moved) fastrender returns None and
    the historical pandas renderer below runs — pandas stays the source of
    truth for every shape the pure renderer has not proven. One pinned
    divergence: naive timestamps render ISO instead of pandas 3.x's
    sci-notation epoch floats (an LLM-facing defect; see fastrender docstring).
    """
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
        # Boundary rule (unchanged): only a POSITIVE max_rows may reach
        # .head()/the renderer. 0 means unlimited (no head at all), and a
        # negative count would silently drop the last row — never allowed.
        if max_rows is not None and max_rows <= 0:
            max_rows = None
        from .fastrender import arrow_to_markdown_fast

        fast = arrow_to_markdown_fast(arrow, max_rows=max_rows)
        if fast is not None:
            return fast
        df = arrow.to_pandas()
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
        # fastrender.dumps: orjson when available (3-8x faster than stdlib on
        # this row-dict-heavy payload), parse-identical, stdlib fallback.
        from .fastrender import dumps as _fast_json

        return _fast_json(payload, default=str)
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


# ---------------------------------------------------------------------------
# Admin API (route cores + MCP twins) — admin-designation gated
# ---------------------------------------------------------------------------

#: The 403 body every admin surface returns to an authenticated non-admin
#: (one shape everywhere: REST JSON body, MCP tool text, the UI renders it
#: verbatim).
_ADMIN_FORBIDDEN = "admin access required"


class AdminHTTPError(Exception):
    """An admin operation's refusal, carrying its HTTP status + JSON body.

    Raised by the SHARED admin cores (the REST routes and the MCP twins run
    the same code); the route wrappers translate it to a JSONResponse (via
    ``.body``, + ``.headers``), the MCP twins let it PROPAGATE to
    _dispatch_tool's catch-all — which renders ``str(this)`` as an isError
    tool result, the structured 403-shaped error the MCP contract requires
    (never a bare traceback). ``status`` is one of 400/401/403/404/409/503 —
    never 500.
    """

    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status = status
        self.body: dict = {"error": error, **extra}

    @property
    def headers(self) -> dict:
        """RFC 7235: a 401 carries WWW-Authenticate (the credential
        schemas this surface accepts — the same Bearer/X-API-Key pair the
        /mcp gate names)."""
        if self.status == 401:
            return {"WWW-Authenticate": "Bearer"}
        return {}

    def __str__(self) -> str:
        """The MCP-facing text: the human message, plus — where the CALLER
        can act on it — the machine-parseable fix_hints tail (the REST body
        stays the bare message via .body; str() exists for the tool path)."""
        message = self.args[0] if self.args else "admin error"
        if self.status == 403:
            return message + _errors.structured(
                _errors.E_PARAM_INVALID,
                [
                    "This surface is restricted to policy-designated admins (the policy document's 'admins' list).",
                    "Check whoami — an authenticated key that is not designated gets this refusal, not a retry.",
                ],
            )
        if self.status == 400:
            return message + _errors.structured(
                _errors.E_PARAM_INVALID,
                ["Fix the document and retry — the previous policy is untouched and still enforcing."],
            )
        if self.status == 404:
            return message + _errors.structured(
                _errors.E_PARAM_INVALID,
                ["List the revocable keys (and their exact fingerprints) with admin_grants."],
            )
        if self.status == 409:
            return message + _errors.structured(
                _errors.E_PARAM_INVALID,
                ["Secret-managed keys rotate via kubectl (the Secret's lifecycle) — this store cannot revoke them."],
            )
        if self.status == 503:
            return message + _errors.structured(
                _errors.E_PARAM_INVALID,
                [
                    (
                        "Configuration problem — check SQLHANDLER_ADMIN_KEYS_FILE (the keys store) and "
                        "SQLHANDLER_POLICY_FILE (the policy path) point at writable files."
                    ),
                ],
            )
        return message


def _admin_gate(caller) -> None:
    """The MCP twins' tool-level admin gate: raise (→ isError result) when
    the caller is anonymous or not designated. The REST routes run the same
    check through require_admin (which adds the 401/403 distinction)."""
    if caller is None or not _admin_keys.is_admin(caller):
        raise AdminHTTPError(403, _ADMIN_FORBIDDEN)


def _admin_presented_keys(request) -> dict[str, list[str]]:
    """ALL credential candidates on an admin route request, GROUPED BY KIND
    — because the kinds have different fallback semantics (see
    :func:`_admin_resolve_caller`):

    ``bearer``  — Authorization: Bearer tokens, plus the D21 forwarded-token
                  envelope (X-Auth-Request-Access-Token, appended AFTER any
                  explicit Bearer — ambient never outranks presented). Each
                  is AMBIGUOUS by nature: could be a fleet/minted key OR an
                  OIDC SSO token (oauth2-proxy forwards one on every
                  authenticated browser request since the parity flip).
    ``api_key`` — X-API-Key / X-API-Token. An EXPLICIT key claim: the caller
                  is asserting 'this is a key'; a wrong key claim is a hard
                  refusal, never a fallback to the browser session behind it.

    Live-seen (G2 2026-10-01): picking ONE header as 'the' credential let
    the ambient forwarded SSO token shadow a perfectly valid explicit
    X-API-Key — the panel 401'd with the correct fleet key in the field.
    """
    if request is None:
        return {"bearer": [], "api_key": []}
    try:
        headers = request.headers
    except Exception:
        return {"bearer": [], "api_key": []}
    out: dict[str, list[str]] = {"bearer": [], "api_key": []}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            out["bearer"].append(token)
    # The D21 forwarded-token envelope (oauth2-proxy pass-access-token):
    # AMBIENT, never explicit — collected as a bearer so it gets the same
    # verified-or-decline treatment, but AFTER any Authorization Bearer
    # (explicit-over-ambient, D19) and always behind an explicit key claim
    # (the api_key branch returns before bearer candidates are consulted).
    forwarded = (headers.get("x-auth-request-access-token") or "").strip()
    if forwarded[:7].lower() == "bearer ":
        forwarded = forwarded[7:].strip()
    if forwarded and forwarded not in out["bearer"]:
        out["bearer"].append(forwarded)
    for header in ("x-api-key", "x-api-token"):
        value = (headers.get(header) or "").strip()
        if value and value not in out["api_key"]:
            out["api_key"].append(value)
    return out


def _admin_presented_key(request) -> str:
    """The FIRST credential candidate (compat shim for the single-value
    call sites/tests) — see :func:`_admin_presented_keys` for the real
    multi-credential resolution."""
    candidates = _admin_presented_keys(request)
    # The dict's two lists in order (bearer first, then api_key); a plain
    # concatenation keeps the candidate order mypy can't see behind the
    # dict[str, list[str]] index.
    ordered = candidates["bearer"] + candidates["api_key"]
    return ordered[0] if ordered else ""


def _admin_resolve_caller(request, presented: dict[str, list[str]]):
    """Resolve the Caller for an admin request, or None (anonymous).

    Credential resolution, in order (first hit wins):

    1. A presented STATIC key (MCP_API_KEYS / SQLHANDLER_API_KEYS,
       constant-time) → Caller(cls=key, key_fp=sha256-of-presentation).
    2. A minted store key via ``admin_keys.match_presentation`` (the
       presented key hashed, compared constant-time against each stored
       ``key_sha256``) → Caller(cls=key, key_fp=the stored fp).
    3. The FULL identity ladder (identity.caller_from_request_state) —
       gateway-relay attribution, the OIDC bearer-JWT rung, the D22 SSO
       session-cookie rung, and the oauth2-proxy browser rung. This is what
       lets an SSO subject in the policy's ``admins`` list administer from
       the browser with no key at all (the DECISIONS "admins: subjects
       and/or fingerprints" contract). The relay rung self-guards
       (attribution-never-authorization: it only resolves over a key-valid
       request), so step 3 can never ELEVATE an unauthenticated caller —
       it can only name an already-authenticated one.

    A key-shaped presented value that matches nothing in steps 1-2 → None
    (401), NOT a fallback to step 3 with a fabricated identity: a wrong key
    must never be redeemed as 'the browser user behind it'. A genuinely
    keyless request (SSO browser session) skips 1-2 (nothing presented) and
    resolves at step 3.

    (/api/* has NO key middleware — pinned by tests/test_require_identity.py,
    the key gate is a /mcp concept — hence the admin surface authenticates
    the presented credential itself.)
    """
    keys_env = _McpApiKeyMiddleware._keys()

    # 1) Explicit KEY claims first (X-API-Key / X-API-Token): each is tried
    #    against the static env keys and the minted store. The FIRST
    #    RECOGNIZED key wins. An UNRECOGNIZED key claim is a hard refusal —
    #    it never falls through to the identity ladder (a wrong key is never
    #    redeemed as the browser user behind it; the test pinning this is
    #    test_unrecognized_key_still_401_not_redeemed_as_browser).
    for candidate in presented["api_key"]:
        static = _identity.match_api_key(candidate, keys_env)
        if static is not None:
            return _identity.Caller(
                cls=_identity.CALLER_CLASS_KEY, subject=None, key_fp=_identity.key_fp(static), via="key"
            )
        try:
            entry = _admin_keys.match_presentation(candidate)
        except Exception:
            entry = None
        if entry is not None:
            return _identity.Caller(cls=_identity.CALLER_CLASS_KEY, subject=None, key_fp=entry.get("fp"), via="key")
    if presented["api_key"]:
        return None  # a wrong explicit key claim — refuse, full stop

    # 2) Bearer tokens: AMBIGUOUS — fleet/minted key OR an OIDC SSO token.
    #    Try the key interpretation first (steps identical to above); if no
    #    key claims it, try the identity ladder's JWT rung (which validates
    #    iss/aud/exp via JWKS and declines silently on failure).
    for candidate in presented["bearer"]:
        static = _identity.match_api_key(candidate, keys_env)
        if static is not None:
            return _identity.Caller(
                cls=_identity.CALLER_CLASS_KEY, subject=None, key_fp=_identity.key_fp(static), via="key"
            )
        try:
            entry = _admin_keys.match_presentation(candidate)
        except Exception:
            entry = None
        if entry is not None:
            return _identity.Caller(cls=_identity.CALLER_CLASS_KEY, subject=None, key_fp=entry.get("fp"), via="key")
    if presented["bearer"]:
        caller = _identity.caller_from_request_state(request)
        if not getattr(caller, "is_anonymous", True):
            return caller
        return None  # a Bearer that is neither a key nor a valid token

    # 3) Nothing presented — the plain SSO-browser case: the full ladder
    #    (D22 session-cookie rung → browser rung). ANONYMOUS → None (the
    #    401 contract: a credential-less caller must never probe the admins
    #    list via 403).
    caller = _identity.caller_from_request_state(request)
    if getattr(caller, "is_anonymous", True):
        return None
    return caller


def require_admin(request) -> object:
    """The D1 admin gate: authenticate first (401), then authorize (403).

    Returns the resolved Caller on success; raises :class:`AdminHTTPError`
    otherwise. Applied to EVERY admin surface (the four REST routes via the
    webui wrappers, the four MCP twins at tool level).

    * anonymous (no credential, or one that matches nothing configured) →
      401 {"error": "unauthorized: ..."} — authentication precedes
      authorization so an anonymous caller can never probe the admins list;
    * authenticated but NOT in the policy's ``admins`` list (subjects and/or
      ``sha256:<12hex>`` fingerprints, read through
      ``admin_keys.is_admin`` — hot-reloaded with the policy file) →
      403 {"error": "admin access required"}.
    """
    presented = _admin_presented_keys(request)
    caller = _admin_resolve_caller(request, presented)
    if caller is None:
        # The message leads with "identity required" (the UI's Access-control
        # panel renders the refusal verbatim, and an anonymous admin-route
        # request IS an identity-required situation) + names the accepted
        # credential headers. WWW-Authenticate: Bearer rides .headers.
        raise AdminHTTPError(
            401,
            "identity required: admin access requires a valid API key "
            "(X-API-Key / Authorization: Bearer) designated in the policy's admins list",
        )
    if not _admin_keys.is_admin(caller):
        raise AdminHTTPError(403, _ADMIN_FORBIDDEN)
    return caller


def _admin_created_by(caller) -> str:
    """The minting admin's audit-safe identity (subject, else the fp)."""
    return getattr(caller, "subject", None) or getattr(caller, "key_fp", None) or "unknown"


def _policy_atomic_write(text: str) -> None:
    """Atomically replace the policy file (temp file in the SAME directory +
    ``os.replace`` — a crash never truncates the operator's policy).

    Raises AdminHTTPError(503) when no policy file is configured or the
    path is unwritable. The temp file carries 0o600: the policy document
    names identities and grants, not secrets, but there is no reason for it
    to be group/world-readable either.
    """
    path = _policy.policy_file_path()
    if not path:
        raise AdminHTTPError(
            503, "no policy file is configured (SQLHANDLER_POLICY_FILE unset) — the policy is read-only"
        )
    target = Path(path)
    try:
        directory = target.parent if str(target.parent) else Path(".")
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".policy-", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except AdminHTTPError:
        raise
    except OSError as exc:
        raise AdminHTTPError(503, f"cannot write the policy file ({path}): {exc}") from exc


def _policy_assignments_view(pol) -> dict:
    """The grants view's ``assignments`` (raw keys → their glob lists).

    The RAW document's object (the compiled vocabulary is enforcement's
    business, not the admin UI's — an operator edits the same keys they
    wrote).
    """
    doc = getattr(pol, "datasets", None)
    if not isinstance(doc, dict):
        return {}
    raw = doc.get("assignments")
    return dict(raw) if isinstance(raw, dict) else {}


def _policy_groups_view(pol) -> list:
    """The grants view's ``groups`` (names only, sorted).

    The datasets form's compiled machinery (``_acl_*``) is enforcement
    detail, NOT operator-authored content — only the hand-written form's
    groups are shown (an operator editing the policy file recognizes their
    own names; the compiled ones would be noise that invites edits that
    hot-reload would then refuse).
    """
    return sorted(g for g in (getattr(pol, "groups", {}) or {}) if not str(g).startswith("_acl_"))


def _admin_secret_keys() -> list[dict]:
    """The bootstrap Secret keys as fp-only grants entries (source "secret").

    These are the keys the deployment booted with (SQLHANDLER_API_KEYS /
    MCP_API_KEYS): the store does not own them (their lifecycle is the
    Secret's — kubectl), so they are reported READ-ONLY and NOT removable.
    Only the FINGERPRINT leaves the process — never the raw value.
    """
    out: list[dict] = []
    for raw in _McpApiKeyMiddleware._keys():
        out.append(
            {
                "fp": _identity.key_fp(raw),
                "label": "bootstrap secret key",
                "created_at": None,
                "created_by": None,
                "source": "secret",
            }
        )
    return out


def _admin_grants_payload() -> dict:
    """The whole grants view (the GET route's body AND admin_grants' text).

    ``admins`` is read DEFENSIVELY (``getattr(pol, "admins", ())``): the
    field lands with the policy-side change and older snapshots of the
    module must not break the route.

    ``policy_text`` is the WHOLE authored document (the parsed policy file
    — datasets doc AND the top-level admins list, or the hand-written
    groups form) serialized as editable YAML, so the UI's policy editor
    prefills with what is actually enforcing instead of a fragment. The
    ``datasets``-only mirror stays in the payload for the older clients and
    the programmatic twins; new code should present ``policy_text``.
    """
    pol = _policy.policy_store().get()
    entries: list[dict] = []
    for e in _admin_keys.list_keys():
        # KeyEntry shapes from the store (source "file"); a defensive copy
        # so a caller mutating the payload can never touch the cache.
        entries.append(dict(e) if isinstance(e, dict) else {"fp": str(e)})
    entries.extend(_admin_secret_keys())
    authored = getattr(pol, "authored", None)
    datasets_mirror = getattr(pol, "datasets", None)
    return {
        "admins": list(getattr(pol, "admins", ()) or ()),
        "datasets": datasets_mirror,
        # The editable truth (YAML). Defensive getattr like ``admins``:
        # a Policy snapshot from before the ``authored`` field must not
        # break the route — fall back to the datasets mirror, else empty.
        "policy_text": _policy.dump_doc(authored)
        if isinstance(authored, dict)
        else (_policy.dump_doc(datasets_mirror) if isinstance(datasets_mirror, dict) else ""),
        "assignments": _policy_assignments_view(pol),
        "blocked": list((getattr(pol, "datasets", None) or {}).get("blocked") or []),
        "groups": _policy_groups_view(pol),
        "policy_hash": getattr(pol, "hash", "") or "",
        "keys": entries,
    }


def _self_mint_key(caller, label: str, assign: list[str] | None) -> dict:
    """The SELF-MINT core: an SSO-authenticated user mints a key bound to
    their own verified subject.

    Hard rules (the security posture of the whole feature):
    * JWT RUNG ONLY — ``caller.via == "jwt"`` (an SSO bearer token the
      resolver verified against the IdP's JWKS). The relay rung is
      EXCLUDED: relay attribution is header-carried and (without the HMAC
      secret configured) spoofable — a self-mint through it would mint
      another user's identity. The browser rung is excluded too (headers
      under a trust flag, not a proof of possession).
    * The subject comes from the VERIFIED token claims (the resolver's
      output), NEVER from request input — there is no parameter that could
      name another user.
    * NO WILDCARD DEFAULT — unlike the admin mint (assign omitted =
      ["*"]), a self-mint with no explicit assign records the user's
      EXISTING subject assignments from the policy; when the policy has
      none, the mint refuses with an actionable message. A self-mint must
      never be able to CREATE privileges — only to carry them into a key.
    * REVOKE-TO-ROTATE — one active self-minted key per subject by
      default (SQLHANDLER_SELF_MINT_MAX_KEYS, default 1, re-read per
      call): minting beyond the cap revokes the OLDEST self-minted key
      for that subject first (a lost key is recovered by minting again,
      never by an admin ticket).
    * The raw key is returned ONCE (same contract as the admin mint); the
      store keeps the fingerprint + the sha256 the middleware matches.

    Raises AdminHTTPError(401/403/409/503) — same envelope as the admin
    surface so the REST wrapper needs no new error shape.
    """
    subject = getattr(caller, "subject", None)
    if getattr(caller, "via", "") != "jwt":
        # THE gate (checked before the subject presence so a non-JWT caller
        # — key class has no subject, relay is spoofable, browser is
        # trust-flag headers — can never reach the mint logic): only a
        # VERIFIED SSO bearer may mint.
        raise AdminHTTPError(
            403,
            "self-mint is available only through SSO login (OIDC bearer token) — "
            "authenticate with your SSO credentials, not an API key, to mint one",
        )
    if not subject:
        raise AdminHTTPError(401, "self-mint requires an authenticated SSO identity (OIDC bearer token)")
    if _admin_keys.keys_file_path() is None:
        raise AdminHTTPError(
            503,
            "self-mint is not available: the keys store is not configured (SQLHANDLER_ADMIN_KEYS_FILE unset)",
        )
    # Per-subject key cap (revoke-to-rotate). The env is re-read per call.
    try:
        max_keys = max(1, int(os.environ.get("SQLHANDLER_SELF_MINT_MAX_KEYS", "1").strip() or "1"))
    except ValueError:
        max_keys = 1
    # INPUT VALIDATION before any store write: the self-mint cannot accept
    # custom 'assign' globs (custom-scoped keys are the admin mint's job) —
    # refusing here leaves no orphan entry to compensate for.
    if assign:
        raise AdminHTTPError(
            403,
            "self-mint cannot accept 'assign' — a minted key carries the "
            "subject's grants by name (no policy write); use the admin mint "
            "for custom-scoped keys",
        )
    raw = secrets.token_urlsafe(32)
    from .mcp_fleet_common.audit import key_fingerprint

    fp = key_fingerprint(raw)
    try:
        entry = _admin_keys.add_key(
            raw,
            label=label or f"self-mint:{subject}",
            created_by=f"subject:{subject}",
            subject=subject,
        )
    except _admin_keys.AdminKeysError as exc:
        message = str(exc)
        status = 409 if "already exists" in message else 503
        raise AdminHTTPError(status, message) from exc
    # NO POLICY WRITE (live 2026-10-05, G2: "[Errno 30] Read-only file
    # system" — the policy file is a ConfigMap mount, read-only by
    # construction). None is needed: the minted key is SUBJECT-BOUND (the
    # store entry above carries subject=), and the identity ladder
    # resolves subject-bound keys to a subject-carrying Caller — so
    # groups_for(subject, fp) falls through the (absent) fp binding to the
    # SUBJECT binding and the key inherits the human's grants BY NAME,
    # live. Grant changes (Access-control tab / policy edit) apply to the
    # key with the same hot-reload that applies to the human — no per-key
    # policy rows to maintain, no write to a read-only volume.
    _self_mint_enforce_cap(subject, max_keys, keep_fp=fp)
    _audit_admin_event(
        "selfservice.key_mint",
        fp=fp,
        subject=subject,
        by=f"subject:{subject}",
        assign=list(assign) if assign else None,
    )
    return {
        "key": raw,  # THE ONE TIME the raw key is returned.
        "fp": fp,
        "label": entry.get("label", ""),
        "subject": subject,
        "entry": entry,
    }


def _self_mint_enforce_cap(subject: str, max_keys: int, *, keep_fp: str) -> None:
    """Keep at most max_keys self-minted keys per subject (revoke-to-rotate).

    Revokes the OLDEST self-minted keys beyond the cap (created_at order),
    never the just-minted one. Best-effort: a failed revoke (unwritable
    store) logs and continues — over-cap is a hygiene issue, not a security
    one (every key still carries only the subject's own grants).
    """
    try:
        mine = [e for e in _admin_keys.list_keys() if e.get("subject") == subject and e.get("fp") != keep_fp]
        mine.sort(key=lambda e: str(e.get("created_at", "")))
        excess = mine[: max(0, len(mine) - max_keys + 1)]
        for e in excess:
            fp = str(e.get("fp", ""))
            if fp:
                _admin_keys.remove_key(fp)
                _admin_drop_assignment(fp)
                _audit_admin_event("selfservice.key_rotated_out", fp=fp, subject=subject)
    except Exception:
        logging.getLogger("sqlhandler.server").debug("self-mint cap enforcement skipped", exc_info=True)


def _self_revoke_key(caller, fp: str) -> dict:
    """A user revokes ONE OF THEIR OWN self-minted keys by fingerprint.

    Ownership is verified against the STORE's subject binding (never the
    caller's word): a subject-bound key may only be revoked by a caller
    authenticated as the same subject (JWT rung) — or by an admin (the
    existing admin revoke surface covers those). Unknown/foreign fps get
    the same 404-shaped answer (no existence leak).
    """
    subject = getattr(caller, "subject", None)
    if not subject or getattr(caller, "via", "") != "jwt":
        raise AdminHTTPError(403, "self-service revoke requires SSO authentication (OIDC bearer token)")
    entry = next(
        (e for e in _admin_keys.list_keys() if e.get("fp") == str(fp or "").strip()),
        None,
    )
    if entry is None or entry.get("subject") != subject:
        raise AdminHTTPError(404, f"no self-minted key with fingerprint {fp}")
    _admin_keys.remove_key(str(entry["fp"]))
    _admin_drop_assignment(str(entry["fp"]))
    _audit_admin_event("selfservice.key_revoke", fp=str(entry["fp"]), subject=subject)
    return {"removed": str(entry["fp"])}


def _admin_users_payload() -> dict:
    """The Users-tab view: every subject the system knows, with grants,
    keys, and last-seen — the admin grants BY NAME from here.

    Subjects come from three unions: policy ``subject:`` assignments (the
    grants that exist), subject-bound minted keys (the users who hold a
    key), and the audit log (last-seen activity per subject). Last-seen
    scans the audit JSONL tail (bounded — the last _USERS_SCAN_LINES
    lines) for events carrying a subject; never the whole file.
    """
    pol = _policy.policy_store().get()
    datasets_doc = getattr(pol, "datasets", None) or {}
    raw_assignments = (datasets_doc.get("assignments") if isinstance(datasets_doc, dict) else None) or {}

    users: dict[str, dict] = {}

    def _ensure(name: str) -> dict:
        return users.setdefault(name, {"subject": name, "grants": [], "keys": [], "last_seen": None})

    # 1) policy assignments — the authoritative grants (name-spelled rows)
    for ident, globs in raw_assignments.items():
        ident = str(ident)
        if ident.startswith("subject:"):
            _ensure(ident[len("subject:") :])["grants"] = list(globs or [])

    # 2) subject-bound minted keys
    for e in _admin_keys.list_keys():
        s = e.get("subject")
        if not s:
            continue
        _ensure(str(s))["keys"].append(
            {
                "fp": e.get("fp"),
                "label": e.get("label", ""),
                "created_at": e.get("created_at"),
                "source": e.get("source", "file"),
            }
        )

    # 3) last-seen from the audit tail (bounded scan, best-effort)
    tail = _audit_subject_last_seen()
    for name, ts in tail.items():
        if name in users:
            users[name]["last_seen"] = ts

    return {
        "users": sorted(users.values(), key=lambda u: u["subject"]),
        "admins": list(getattr(pol, "admins", ()) or ()),
        "policy_hash": getattr(pol, "hash", "") or "",
    }


#: The audit-tail bound for the Users view (last-seen is a convenience, not
#: a query engine — a bounded read keeps the admin route O(1)-ish on any
#: log size).
_USERS_SCAN_LINES = 5000


def _audit_subject_last_seen() -> dict[str, str]:
    """subject -> last ISO ts from the audit log's tail (bounded, silent)."""
    path = observability.audit_log_path()
    if not path:
        return {}
    last: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-_USERS_SCAN_LINES:]
    except OSError:
        return {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        subject = rec.get("subject")
        ts = rec.get("ts")
        if subject and ts and isinstance(subject, str):
            prev = last.get(subject)
            if prev is None or str(ts) >= prev:
                last[subject] = str(ts)
    return last


def _admin_assign_user_grants(caller, subject: str, globs: list) -> dict:
    """Grant (or replace) one subject's dataset globs BY NAME — the
    admin-panel Users action.

    Merges ``{f"subject:{subject}": globs}`` into the policy's
    datasets.assignments, validates the WHOLE document first (a bad merge
    never lands), and hot-reloads. Empty globs DROPS the subject's row
    (revoke-all). The subject name is sanitized (no control chars) and the
    document round-trips through the SAME validation the PUT route uses.
    """
    subject = str(subject or "").strip()
    if not subject or any(c in subject for c in "\r\n\t"):
        raise AdminHTTPError(400, "provide a valid subject name")
    if not isinstance(globs, list) or any(not isinstance(g, str) for g in globs):
        raise AdminHTTPError(400, "'globs' must be a list of dataset glob strings (empty list revokes all)")
    cleaned = [g.strip() for g in globs if g.strip()]
    path = _policy.policy_file_path()
    if not path:
        raise AdminHTTPError(503, "no policy file is configured (SQLHANDLER_POLICY_FILE unset) — grants are read-only")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AdminHTTPError(503, f"cannot read the policy file ({path}): {exc}") from exc
    data = _admin_validate_policy_text(text)
    doc = data.get("datasets")
    if not isinstance(doc, dict):
        raise AdminHTTPError(503, "grants live in the datasets policy form — this deployment uses the group form")
    assignments = doc.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}
        doc["assignments"] = assignments
    if cleaned:
        assignments[f"subject:{subject}"] = cleaned
    else:
        assignments.pop(f"subject:{subject}", None)
        assignments.pop(subject, None)
    _admin_write_policy(json.dumps(data, indent=2))
    _audit_admin_event(
        "admin.user_grants",
        subject=subject,
        by=_admin_created_by(caller),
        globs=cleaned or None,
    )
    pol = _policy.policy_store().get()
    return {"subject": subject, "grants": cleaned, "policy_hash": getattr(pol, "hash", "") or ""}


def _admin_keys_list_for_subject(caller) -> dict:
    """The self-service key view: the CALLER'S OWN subject-bound keys.

    JWT-rung only (same posture as the mint). Returns the caller's keys
    (fp/label/created_at — never any key material) so the UI can offer
    revoke-to-rotate without an admin.
    """
    subject = getattr(caller, "subject", None)
    if not subject or getattr(caller, "via", "") != "jwt":
        raise AdminHTTPError(403, "self-service keys require SSO authentication (OIDC bearer token)")
    mine = [
        {
            "fp": e.get("fp"),
            "label": e.get("label", ""),
            "created_at": e.get("created_at"),
        }
        for e in _admin_keys.list_keys()
        if e.get("subject") == subject
    ]
    return {"subject": subject, "keys": sorted(mine, key=lambda k: str(k.get("created_at", "")))}


def _admin_validate_policy_text(text: str) -> dict:
    """FULLY validate a policy document WITHOUT writing it.

    ``_parse_text`` only decodes (JSON/YAML → dict); the deep validation
    (glob shapes, fp formats, mutual exclusion, admins entries) lives in
    ``load_policy`` — so validation goes through the loader on a THROWAWAY
    temp file: a PolicyError → 400 with the loader's message, the previous
    policy file untouched. Returns the parsed document.
    """
    tmp = None
    try:
        data = _policy._parse_text(text, "admin policy set")  # decode
        fd, tmp = tempfile.mkstemp(prefix=".policy-validate-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        _policy.load_policy(tmp)  # the FULL validation (globs, fps, exclusion)
    except _policy.PolicyError as exc:
        raise AdminHTTPError(400, str(exc)) from exc
    except OSError as exc:
        raise AdminHTTPError(400, f"policy document could not be validated: {exc}") from exc
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return data


def _admin_write_policy(text: str) -> dict:
    """Validate + atomically write + hot-reload the policy file.

    The shared PUT/admin_policy_set path: validation runs FIRST (a bad
    document never touches the file — PolicyError → 400, previous policy
    keeps enforcing), then the atomic replace, then the store is re-read
    (mtime hot-reload picks it up; the response returns the new hash).
    """
    _admin_validate_policy_text(text)
    _policy_atomic_write(text)
    # Force the reload (the mtime granularity on fast filesystems can hide
    # a same-second rewrite from get()'s stat signature — one explicit
    # load makes the response's hash the WRITTEN truth).
    path = _policy.policy_file_path()
    pol = _policy.load_policy(path) if path else _policy.Policy()
    store = _policy.policy_store()
    with store._lock:
        store._policy = pol
        st = os.stat(path) if path else None
        # Explicit guard rather than a one-line conditional: the tuple
        # literal would otherwise carry `path`'s Optional into a
        # tuple[str, float, int] slot.
        if st is not None and path is not None:
            store._stat = (path, st.st_mtime, st.st_size)
        else:
            store._stat = None
        store._broken_since = None
    return {"ok": True, "policy_hash": pol.hash, "admins": list(getattr(pol, "admins", ()) or ())}


def _admin_assignments_doc(pol) -> dict | None:
    """The current policy document's ``datasets`` doc, or None (groups form)."""
    doc = getattr(pol, "datasets", None)
    return doc if isinstance(doc, dict) else None


def _admin_drop_assignment(fp: str) -> bool:
    """Remove one fp's assignment from the policy document (if present).

    Atomic; returns True when an entry was dropped. A missing entry is NOT
    an error (the key may never have had one; the binding and the store
    entry are removed independently so neither orphan can block the other).
    """
    path = _policy.policy_file_path()
    if not path:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = _admin_validate_policy_text(text)
    except AdminHTTPError:
        return False
    doc = data.get("datasets")
    if not isinstance(doc, dict):
        return False
    assignments = doc.get("assignments")
    if not isinstance(assignments, dict):
        return False
    dropped = False
    for key in [k for k in assignments if _assignment_fp(k) == fp]:
        del assignments[key]
        dropped = True
    if not dropped:
        return False
    _policy_atomic_write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True


def _assignment_fp(key: str) -> str:
    """The bare fp an assignments key binds (the ``key:`` prefix normalized)."""
    return key.removeprefix("key:").strip()


def _admin_grants_text() -> str:
    """The grants view as the MCP tool text (stable JSON, no raw keys)."""
    return json.dumps(_admin_grants_payload(), indent=2, default=str)


def _audit_admin_event(event: str, **fields) -> None:
    """One audit line for an admin mutation (best-effort, never raises).

    Same JSONL trail as observability.audit_query (event/ts/pod) with the
    event named for the action and the RAW KEY NEVER included — the mint
    event carries the fingerprint only (the raw key exists in exactly one
    place: the mint response body).
    """
    path = observability.audit_log_path()
    if not path:
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "pod": os.environ.get("SQLHANDLER_POD_NAME") or os.environ.get("HOSTNAME") or None,
        **fields,
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logging.getLogger("sqlhandler.server").debug("admin audit line skipped", exc_info=True)


def admin_grants(*, caller=None) -> str:
    """MCP twin of GET /api/admin/grants (admin-gated at the tool level).

    Returns the whole grants view: the designated admins, the raw datasets
    document (or null), policy_text (the WHOLE authored policy document as
    YAML — feeds admin_policy_set back verbatim), the assignments map, the
    blocked globs, the group names, the policy hash, and every key (minted:
    full KeyEntry; Secret keys: fp-only, source "secret", not removable).

    The gate raises AdminHTTPError — _dispatch_tool's catch-all renders it
    (via __str__) as the structured 403-shaped isError result. NEVER a bare
    exception traceback, never a silent empty view.
    """
    _admin_gate(caller)
    try:
        return _admin_grants_text()
    except AdminHTTPError:
        raise
    except Exception as exc:
        return f"Error reading grants: {_errors.enrich(str(exc))}"


def admin_policy_set(policy: str, *, caller=None) -> str:
    """MCP twin of PUT /api/admin/policy (admin-gated at the tool level).

    ``policy`` is the FULL policy document as a JSON or YAML string (the
    YAML admin_grants' policy_text serves round-trips as-is). Validated
    first (an invalid document → the loader's message as an isError result,
    the previous policy untouched and enforcing); written atomically; the
    hot-reload picks it up. Returns the new policy hash.
    """
    _admin_gate(caller)
    try:
        result = _admin_write_policy(str(policy))
        _audit_admin_event("admin.policy_set", policy_hash=result["policy_hash"], by=_admin_created_by(caller))
        return json.dumps(result, indent=2)
    except AdminHTTPError:
        raise  # __str__ carries the structured fix_hints (dispatcher renders)
    except Exception as exc:
        return f"Error setting policy: {_errors.enrich(str(exc))}"


def admin_key_mint(label=None, assign=None, *, caller=None) -> str:
    """MCP twin of POST /api/admin/keys (admin-gated at the tool level).

    Mints one key; the raw key is in this response ONCE and nowhere else
    (the store keeps the fingerprint + the full sha256 the middleware
    matches against — never the raw). ``assign`` is the optional glob list
    bound into the policy's datasets.assignments (omitted = ["*"]).
    """
    _admin_gate(caller)
    try:
        result = _admin_mint_key(
            str(label) if label else "",
            [str(g).strip() for g in assign if str(g).strip()] if isinstance(assign, list) else None,
            _admin_created_by(caller),
        )
        return json.dumps(result, indent=2)
    except AdminHTTPError:
        raise
    except Exception as exc:
        return f"Error minting key: {_errors.enrich(str(exc))}"


def admin_key_revoke(fp: str, *, caller=None) -> str:
    """MCP twin of DELETE /api/admin/keys/{fp} (admin-gated at the tool level).

    Removes the store key AND its assignment entry. A Secret-managed fp is
    refused (409-shaped error text — the Secret's lifecycle owns it); an
    unknown fp is 404-shaped text.
    """
    _admin_gate(caller)
    try:
        result = _admin_revoke_key(str(fp or "").strip(), _admin_created_by(caller))
        return json.dumps(result, indent=2)
    except AdminHTTPError:
        raise
    except Exception as exc:
        return f"Error revoking key: {_errors.enrich(str(exc))}"


def _admin_mint_key(label: str, assign: list[str] | None, created_by: str) -> dict:
    """The shared mint core (POST route + admin_key_mint twin).

    secrets.token_urlsafe(32) → fingerprint via the audit layer's
    key_fingerprint (the SAME formula the auth layer matches with) →
    add_key (fp-unique; AdminKeysError → 409 dup / 503 unconfigured or
    unwritable) → the assignment merged into the policy document (datasets
    form required — refusal otherwise) → the response carries the raw key
    ONCE. The audit line carries the FINGERPRINT, never the raw.
    """
    raw = secrets.token_urlsafe(32)
    from .mcp_fleet_common.audit import key_fingerprint

    fp = key_fingerprint(raw)
    if _admin_keys.keys_file_path() is None:
        raise AdminHTTPError(
            503,
            "admin keys store not configured (SQLHANDLER_ADMIN_KEYS_FILE unset) — the key cannot be stored",
        )
    try:
        entry = _admin_keys.add_key(raw, label=label, created_by=created_by)
    except _admin_keys.AdminKeysError as exc:
        message = str(exc)
        status = 409 if "already exists" in message else 503
        raise AdminHTTPError(status, message) from exc
    # The assignment is ALWAYS recorded (omitted assign = ["*"], the
    # mint_key.py convention): a key minted with no grant is a trap for the
    # next reader. The merge may REFUSE (groups form / no policy file) —
    # the compensating remove below keeps the store from stranding a
    # minted-but-never-granted key.
    try:
        _admin_merge_assignment_with_fp(fp, assign if assign else ["*"])
    except AdminHTTPError:
        try:
            _admin_keys.remove_key(fp)
        except Exception:
            pass
        raise
    _audit_admin_event("admin.key_mint", fp=fp, label=label, by=created_by, assign=list(assign) if assign else ["*"])
    return {
        "key": raw,  # THE ONE TIME the raw key is returned — store it now.
        "fp": fp,
        "label": label,
        "assignment": {fp: list(assign) if assign else ["*"]},
        "entry": entry,
    }


def _admin_merge_assignment_with_fp(fp: str, globs: list[str]) -> dict:
    """The assignment merge for the mint path (fp in hand — no round-trip).

    Reads the policy FILE (the authored truth, not the compiled Policy),
    merges ``{fp: globs}`` into ``datasets.assignments``, and writes the
    whole document back atomically. Returns the merged assignments map.

    503 "bind via the policy form when a datasets doc is absent" when the
    file uses the hand-written groups form: silently creating a datasets
    doc would make the file carry BOTH forms and the loader refuses that
    on principle (mutual exclusion) — the operator binds by hand there.
    503 when no policy file is configured (assignments are file content).
    """
    path = _policy.policy_file_path()
    if not path:
        raise AdminHTTPError(
            503, "no policy file is configured (SQLHANDLER_POLICY_FILE unset) — cannot record the assignment"
        )
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AdminHTTPError(503, f"cannot read the policy file ({path}): {exc}") from exc
    data = _admin_validate_policy_text(text)
    doc = data.get("datasets")
    if not isinstance(doc, dict):
        raise AdminHTTPError(503, "bind via the policy form when a datasets doc is absent")
    assignments = doc.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}
        doc["assignments"] = assignments
    # One fp, one binding: an existing entry for the same fp (either
    # spelling) is REPLACED — the fresh mint defines the grant.
    normalized = {_assignment_fp(k): k for k in assignments}
    existing_key = normalized.get(fp)
    if existing_key:
        del assignments[existing_key]
    assignments[fp] = list(globs) if globs else ["*"]
    doc["assignments"] = assignments
    _policy_atomic_write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return dict(assignments)


def _admin_revoke_key(fp: str, by: str = "") -> dict:
    """The shared revoke core (DELETE route + admin_key_revoke twin).

    Secret-managed fps are refused (409 — the Secret's lifecycle owns
    them); unknown fps 404; a store hit removes the entry AND its policy
    assignment (dropping the assignment alone would leave a dead binding;
    dropping the key alone would leave an orphan grant).
    """
    if not fp:
        raise AdminHTTPError(400, "provide the key fingerprint to revoke (sha256:<12hex>)")
    secret_fps = {e["fp"] for e in _admin_secret_keys()}
    if fp in secret_fps:
        raise AdminHTTPError(409, "Secret-managed key — revoke via kubectl (the Secret's lifecycle)")
    removed = _admin_keys.remove_key(fp)
    if not removed:
        # remove_key returns False for BOTH an unknown fp and a disabled
        # store; with no store configured the whole surface is moot and the
        # fp cannot have been a file key — same 404, honest message.
        raise AdminHTTPError(404, f"no minted key with fingerprint {fp}")
    assignment_removed = _admin_drop_assignment(fp)
    _audit_admin_event("admin.key_revoke", fp=fp, by=by or None)
    return {"removed": fp, "assignment_removed": bool(assignment_removed)}


#: Process start time (wall anchor) for the /metrics uptime header.
_START_TIME = time.time()


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


class _MetricsPodIdentityMiddleware:
    """ASGI middleware: add the serving replica's identity to /metrics.

    Deliberately a header, not a body annotation: the exposition body stays
    byte-identical (the whole fleet contract on additive metrics), while an
    aggregate alert can still name the replica that answered. The header is
    also how a human correlating a scrape with kubectl output confirms
    which pod they are looking at.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "") == "/metrics":
            pod = os.environ.get("SQLHANDLER_POD_NAME") or os.environ.get("HOSTNAME") or ""

            async def send_wrapper(message):
                if message["type"] == "http.response.start" and pod:
                    headers = list(message.get("headers", []))
                    headers.append((b"x-sqlhandler-pod", pod.encode("latin-1")))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_wrapper)
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

    Keys file UNION (SQLHANDLER_ADMIN_KEYS_FILE): ``_keys()`` stays
    env-only (it feeds /metrics auth and the static-key-vs-JWT check);
    the union lives in ``__call__`` — a minted store key is authenticated
    via ``admin_keys.match_presentation`` (the presented key is hashed and
    compared constant-time against each stored ``key_sha256``; the raw key
    is never stored or logged). ENV-FIRST precedence: the env keys are
    tried first, and a value present in both is authenticated by the env
    path (a static key wins its own fingerprint). A store match records
    the minted entry's fingerprint into ``scope["state"]`` exactly like an
    env match, so the identity spine is unchanged (key-class caller, same
    fp vocabulary). The gate ARMS on the union: env keys alone (exactly as
    before) OR a keys-file-only deployment with at least one minted entry
    (a disabled store or an empty one never arms it — no bootstrap
    lockout, zero store I/O beyond one env read). When the store is
    disabled the match is a cheap no-op and behavior is byte-identical to
    the env-only days.

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

    #: The scope["state"] slot carrying a SUBJECT-BOUND key's subject
    #: (self-minted keys only — the SSO user the key acts as).
    KEY_SUBJECT_STATE = "sqlhandler.key_subject"

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
                gate_on = True
            else:
                # The union's store half can arm the gate on its own (a
                # keys-file-only deployment): configured AND non-empty.
                # A disabled store or an empty one never arms it — no
                # bootstrap lockout, and env-only deployments keep the
                # exact env-only behavior (zero store I/O: the path check
                # is one env read).
                gate_on = _admin_keys.keys_file_path() is not None and bool(_admin_keys.list_keys())
            if gate_on:
                # D19 explicit-over-ambient (parity with _admin_presented_keys
                # on /api and with MultimodalRAG's mcp_auth): collect EVERY
                # presented credential — explicit X-API-Key/X-API-Token claims
                # first, then the ambient Authorization Bearer — and resolve
                # the first RECOGNIZED one. Picking the wire-order FIRST of
                # authorization/x-api-key let a relay's own server-level
                # Bearer shadow a per-key X-API-KEY override riding the same
                # request (pcai-llm-gateway's resolve_mcp_headers emits
                # exactly that pair: the server-level auth as Bearer, then
                # the calling key's stored overrides merged last-wins under
                # their own header names) — the explicit claim was never
                # read, so every relayed call resolved as the shared server
                # identity.
                api_claims: list[str] = []
                bearer_claims: list[str] = []
                for k, v in scope.get("headers", []):
                    lk = k.lower() if isinstance(k, bytes) else k
                    if lk in (b"x-api-key", b"x-api-token"):
                        value = v.decode("latin-1").strip()
                        if value and value not in api_claims:
                            api_claims.append(value)
                    elif lk == b"authorization":
                        scheme, _, token = v.decode("latin-1").partition(" ")
                        if scheme.lower() == "bearer":
                            value = token.strip()
                            if value and value not in bearer_claims:
                                bearer_claims.append(value)
                matched: str | None = None
                store_entry: dict | None = None
                # Explicit phase: a recognized claim wins; an UNRECOGNIZED
                # explicit claim is a hard refusal — never redeemed as the
                # ambient Bearer behind it (the _admin_resolve_caller
                # posture: a wrong key is never 'the caller behind it').
                for candidate in api_claims:
                    if keys:
                        matched = _identity.match_api_key(candidate, keys)
                        if matched is not None:
                            store_entry = None
                            break
                    # Store fallback per candidate: a minted key (keys file)
                    # when no env key matched. No-op (None, zero I/O) when
                    # the store is disabled.
                    store_entry = _admin_keys.match_presentation(candidate)
                    if store_entry is not None:
                        matched = None
                        break
                if matched is None and store_entry is None and not api_claims:
                    # Ambient phase: reached only when NOTHING explicit was
                    # claimed — bearer-only and keyless requests keep their
                    # exact pre-change behavior.
                    for candidate in bearer_claims:
                        if keys:
                            matched = _identity.match_api_key(candidate, keys)
                            if matched is not None:
                                store_entry = None
                                break
                        store_entry = _admin_keys.match_presentation(candidate)
                        if store_entry is not None:
                            matched = None
                            break
                if matched is None and store_entry is None:
                    resp = JSONResponse(
                        {"error": "unauthorized: missing or invalid API key"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await resp(scope, receive, send)
                    return
                # ADDITIVE identity feed: record WHICH key matched (its
                # fingerprint, never the key) for the inner middleware. A
                # store match records the minted fingerprint the SAME way an
                # env match records the static one (identity spine unchanged:
                # key-class caller). A SUBJECT-BOUND key (self-minted by an
                # SSO user) additionally records its subject: the key rung
                # then resolves a subject-carrying Caller and policy grants
                # BY NAME apply to the human, not just to the fp.
                state = scope.setdefault("state", {})
                if store_entry is not None:
                    state[self.KEY_FP_STATE] = store_entry.get("fp")
                    if store_entry.get("subject"):
                        state[self.KEY_SUBJECT_STATE] = str(store_entry["subject"])
                else:
                    state[self.KEY_FP_STATE] = _identity.key_fp(matched)
        await self.app(scope, receive, send)


# The OIDC rung's static-key source (registered now that the middleware
# above exists): a Bearer value matching a configured static key is the KEY
# rung's credential and is NEVER parsed as a JWT. The callable re-reads the
# env per call, so key rotation needs no restart and nothing is cached here.
_identity.set_static_keys_source(_McpApiKeyMiddleware._keys)


class _CallerIdentityMiddleware:
    """ASGI middleware: resolve one Caller per request (identity spine, Stage 1).

    Added BEFORE ``add_middleware(_McpApiKeyMiddleware)`` — Starlette applies
    middleware LIFO, so this runs INSIDE the key check and sees the
    fingerprint the key middleware recorded in ``scope["state"]``.

    Pure resolution + recording: it never rejects anything (the 401 posture
    stays _McpApiKeyMiddleware's, and the anonymous-401 posture when
    ``SQLHANDLER_REQUIRE_IDENTITY`` is on stays _IdentityRequiredMiddleware's —
    the gate wraps OUTSIDE this one). The resolved Caller rides
    ``scope["state"]["sqlhandler.caller"]`` where the tool dispatch /
    webui / resource handlers read it, and is also published to
    ``identity.CALLER_CONTEXT`` so the audit writer (and any contextvar-based
    consumer inside the REQUEST's async task) sees it. The QueryJob thread
    still receives the caller EXPLICITLY — contextvars do not cross a raw
    ``threading.Thread``.

    Rungs (see sqlhandler.identity for the full ladder): relay attribution
    headers only over a key-valid request → OIDC bearer JWT (verified RS256
    against the configured JWKS, and only when the Bearer value is not a
    configured static key) → oauth2-proxy headers only under
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


class _IdentityRequiredMiddleware:
    """ASGI middleware: reject ANONYMOUS callers on the data surfaces.

    Env ``SQLHANDLER_REQUIRE_IDENTITY`` (truthy: 1/true/yes/on,
    case-insensitive, re-read PER REQUEST like every SQLhandler env) turns
    the optional-auth posture into an enforced one: a request to ``/mcp``
    or ``/api/*`` whose resolved Caller is anonymous gets

        401 {"error": "identity required: authenticate with a key
        (X-API-Key/Bearer), an OIDC bearer token, or via the gateway"}
        + WWW-Authenticate: Bearer

    NEVER gated: ``/health``, ``/ready``, ``/metrics`` — kubelet probes
    cannot carry secrets (live-learned 2026-09-18; gating them makes the
    pod NotReady and unscrapeable, an outage, not a hardening) — and the
    ``/ui`` shell, whose page loads from static bytes and fetches its data
    through the gated ``/api`` routes (webui.register_ui: every /api/*
    handler is separate from the HTML shell; the browser then authenticates
    exactly like any other client). An unset env is BYTE-IDENTICAL to the
    pre-gate behavior (the middleware is registered either way so its env
    stays per-request flippable, and the gate body returns False without
    touching anything).

    ``/api/admin/*`` is EXEMPT (Lead-approved 2026-09-30): the admin surface
    self-gates with a STRICTER check than this middleware could apply here —
    ``require_admin`` authenticates the presented key itself (the /api surface
    has no key middleware, so the generic gate's caller is anonymous there
    regardless of a presented X-API-Key) and then authorizes against the
    policy's ``admins`` list (403 non-admin). Exempting it changes NO outcome
    (a credentialed admin passes both layers; an anonymous or non-admin
    caller is refused by require_admin with 401/403) while keeping the gate's
    /api posture byte-identical for every other route. Scope is EXACTLY the
    admin prefix — never /api/*.

    Added AFTER ``_CallerIdentityMiddleware`` in the add_middleware order →
    Starlette LIFO puts it OUTSIDE the resolver (which is outside the key
    middleware) — the layering is: key gate → identity gate → caller
    resolution → routes. By the time this runs, _CallerIdentityMiddleware
    has already populated ``scope["state"]["sqlhandler.caller"]`` on the
    request's way IN (this middleware sits between the resolver and the
    routes, so the state slot is populated before BOTH the gate check and
    the app), so the gate reads the resolved Caller instead of re-resolving.
    """

    ENV_NAME = "SQLHANDLER_REQUIRE_IDENTITY"

    #: Path prefixes the gate protects (the data surfaces). /api/admin/* is
    #: subtracted below (SELF-GATED: require_admin's 401-anonymous +
    #: 403-non-admin is strictly stronger than this 401-anonymous-only gate).
    GATED_PREFIXES = ("/mcp", "/api")

    #: The self-gating admin surface (exempt from the generic gate; Lead
    #: approval 2026-09-30 — see the class docstring for the reasoning).
    SELF_GATED_PREFIX = "/api/admin/"

    #: Paths NEVER gated — kubelet probes cannot carry secrets (2026-09-18),
    #: and the UI shell is static bytes (its data comes through gated /api):
    #: the html_page mounts at "/", "/ui" and "/ui/index.html".
    #: /api/whoami joins them (D-UI, 2026-10): it is the shell's identity
    #: probe — a PREVIEW of what a gated call would resolve to (audit-safe
    #: shape, no grant, no admin answer), so gating it would blind exactly
    #: the caller it exists to tell "why am I 401-ing?" while admitting
    #: nothing (every gated route re-resolves on its own).
    #: /oauth/* joins them (D22, 2026-10): the SSO login round trip IS the
    #: way an anonymous visitor becomes authenticated — gating it would
    #: make sign-in unreachable. The callback verifies the exchanged token
    #: with the full D21 machinery before any cookie exists, so the exempt
    #: surface admits nothing by itself.
    UNGATED_EXACT = (
        "/",
        "/health",
        "/ready",
        "/metrics",
        "/api/whoami",
        "/oauth/login",
        "/oauth/oidc/callback",
        "/oauth/logout",
        "/ui",
        "/ui/index.html",
    )

    def __init__(self, app):
        self.app = app

    @classmethod
    def _required(cls) -> bool:
        return os.environ.get(cls.ENV_NAME, "").strip().lower() in ("1", "true", "yes", "on")

    @classmethod
    def _gated_path(cls, path: str) -> bool:
        if path in cls.UNGATED_EXACT or path == "/" or path.startswith(("/ui/", "/static/")):
            return False
        # /api/admin/* self-gates (require_admin: 401 anon THEN 403
        # non-admin — strictly stronger than this gate's 401-only check;
        # the exemption is the Lead-approved 2026-09-30 design note).
        if path.startswith(cls.SELF_GATED_PREFIX):
            return False
        return path.startswith(cls.GATED_PREFIXES)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._required() and self._gated_path(scope.get("path", "")):
            caller = (scope.get("state") or {}).get(_CallerIdentityMiddleware.CALLER_STATE)
            if caller is None:
                # Defense in depth: no resolver output in scope state (a
                # re-ordered stack, or a direct call). Resolve here rather
                # than trusting the slot — an absent slot must never widen
                # the gate.
                caller = _identity.caller_from_scope(scope)
            if caller is not None and caller.is_anonymous:
                resp = JSONResponse(
                    {
                        "error": "identity required: authenticate with a key "
                        "(X-API-Key/Bearer), an OIDC bearer token, or via the gateway"
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return
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

    # Event loop (bench follow-up, 2026-09): uvloop's C loop instead of the
    # default asyncio selector loop — the ASGI hop (uvicorn + Starlette
    # middleware stack) is pure-Python callback churn, and uvloop cuts that
    # overhead measurably on the warm/cache-hit path that dominates
    # agent-facing traffic. Guarded on three axes so it can never become a
    # hard dependency or a behavior change:
    #   * importable (the wheel ships in pyproject, but a stripped install
    #     without it still runs on the default loop);
    #   * opt-out (SQLHANDLER_EVENT_LOOP=asyncio) for incident triage — if a
    #     uvloop regression is ever suspected, flip the env and restart,
    #     no code change;
    #   * stdio transport untouched (asyncio.run above — uvloop adds nothing
    #     to a single-session pipe and stdio tooling expects default asyncio).
    # The engine is unaffected: DuckDB/pyarrow calls release the GIL and run
    # on worker threads regardless of which loop schedules them.
    _loop_pref = os.environ.get("SQLHANDLER_EVENT_LOOP", "uvloop").strip().lower()
    _loop_log = logging.getLogger("sqlhandler.server")
    if _loop_pref in ("", "uvloop", "auto"):
        try:
            import uvloop

            uvloop.install()
            _loop_log.info("Event loop: uvloop (SQLHANDLER_EVENT_LOOP=asyncio to opt out)")
        except ImportError:
            _loop_log.info("Event loop: asyncio (uvloop not installed — add the wheel for the fast loop)")
    else:
        _loop_log.info("Event loop: asyncio (SQLHANDLER_EVENT_LOOP=%s)", _loop_pref)
    # Engine PRE-WARM (G2 2026-10-01 lesson): fire the cold engine build in a
    # background thread the moment main() starts — BEFORE uvicorn binds — so
    # the first readiness probe never races a cold `_handler()` under
    # `_handler_lock`. Without this, a slow provider init turned the probe
    # cadence into a self-sustaining 503 cascade (wait_for cancels the WAIT,
    # not the thread; every subsequent probe blocked on the lock and timed
    # out) for as long as init took. Pre-warm idempotent: `_handler()` is
    # lock-guarded and singleton — this thread and the first probe converge
    # on one build; whoever arrives second blocks on the lock briefly and
    # gets the finished engine. Failures land in the thread's log line AND
    # surface normally on the next probe (no swallowed errors).
    import threading as _threading

    def _prewarm() -> None:
        try:
            _handler()
            _loop_log.info("Engine pre-warm complete — readiness can pass immediately")
        except Exception as exc:  # the probe path re-raises with the real error
            _loop_log.warning("Engine pre-warm FAILED (first /ready will carry the real error): %s", exc)

    _threading.Thread(target=_prewarm, name="sqlhandler-prewarm", daemon=True).start()
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
        # Backend-aware readiness: report "ready" when the configured data
        # source is actually reachable (OneLake DFS token+list, S3 list,
        # Iceberg catalog, NFS root, or Delta Sharing GET /shares). If the
        # credential/endpoint breaks, the pod drops out of the Service so
        # traffic stops reaching a dead backend and the failure becomes
        # visible. Disable with SQLHANDLER_READINESS_CHECK=0.
        #
        # DEGRADED band (HA review 2026-09): a failing check is NOT reported
        # to the kubelet until SQLHANDLER_READY_DEGRADED_GRACE (default 60s)
        # of CONTINUOUS failure has elapsed. One flaky storage blip used to
        # drain every replica of a scaled-out deployment simultaneously (a
        # correlated outage the replicas were bought to prevent); within the
        # grace the pod stays Ready, keeps serving cached/metadata paths,
        # and the /metrics gauge drops to 0.5 so alerting still sees the
        # degradation. Persistent failure still goes NotReady (the
        # historical outcome) and a healthy check resets the clock.
        if os.environ.get("SQLHANDLER_READINESS_CHECK", "1").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return JSONResponse({"status": "ready"})
        # Probe cadence is 10s with a 20s timeout (chart readiness values) —
        # the INIT budget must absorb a cold engine build (env + provider
        # construction) without racing it: a 2s budget turned every early
        # probe into a silent timeout CASCADE (asyncio.wait_for cancels the
        # wait, not the to_thread — init keeps running under _handler_lock,
        # so each subsequent probe blocks on the lock and times out too,
        # pegging /ready at 503 for the whole cold-start even though the
        # process itself was healthy; live-seen on G2 2026-10-01, 13-minute
        # zero-exception 503 streaks that cleared on their own). 30s also
        # stays inside the probe's own 20s timeout? No — 30s EXCEEDS it;
        # that is deliberate: the kubelet probe timing out is FINE (it
        # retries), what must never happen is the readiness path reporting
        # a backend outage because engine init was slow. Every failure now
        # LOGS (all three branches were silent before — the reason string
        # rode only in the JSON body, which the gateway strips on
        # empty-endpoint 503s, leaving nothing to debug from).
        # Two separate budgets with DISTINCT handling (asyncio.wait_for raises
        # a bare TimeoutError either way — a combined try can't tell which
        # budget fired, and the distinction is the whole diagnostic value).
        try:
            engine = await asyncio.wait_for(asyncio.to_thread(_handler), timeout=30)
        except TimeoutError:
            logger.warning(
                "readiness: ENGINE INIT timed out after 30s — engine build still "
                "grinding in its thread (lock held); later probes will succeed once warm"
            )
            observability.drift.note_backend(False)
            return _ready_response(503, "engine init timed out")
        except Exception as exc:
            logger.warning("readiness: engine init FAILED: %s", exc)
            observability.drift.note_backend(False)
            return _ready_response(503, f"engine init failed: {exc}")
        try:
            err = await asyncio.wait_for(asyncio.to_thread(engine.provider.check_connection), timeout=15)
        except TimeoutError:
            logger.warning("readiness: backend check timed out after 15s (endpoint slow/unreachable)")
            observability.drift.note_backend(False)
            return _ready_response(503, "backend check timed out")
        except Exception as exc:
            logger.warning("readiness: backend check FAILED: %s", exc)
            observability.drift.note_backend(False)
            return _ready_response(503, str(exc))
        if err:
            logger.warning("readiness: backend check reported: %s", err)
            observability.drift.note_backend(False)
            return _ready_response(503, err)
        observability.drift.note_backend(True)
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
        response = Response(
            content=observability.metrics.render(engine),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
        # Explicit value first (set only when the operator wires it); the
        # hostname fallback (empty on stdio/local runs) renders NO header
        # at all — never a misleading "pod=unknown" label.
        pod = os.environ.get("SQLHANDLER_POD_NAME") or os.environ.get("HOSTNAME") or ""
        if pod:
            response.headers["X-Sqlhandler-Pod"] = pod
        response.headers["X-Sqlhandler-Ready-Seconds"] = f"{time.time() - _START_TIME:.1f}"
        return response

    app.add_route("/metrics", _metrics)

    # When SQLHANDLER_API_TOKEN is set, every /api/* call must present it
    # (Authorization: Bearer <token> or X-API-Token: <token>) — for machine
    # callers of the JSON API on deployments that are NOT behind the PCAI
    # oauth2-proxy gateway. /mcp, /ui and /health|/ready are unaffected.
    api_token = os.environ.get("SQLHANDLER_API_TOKEN", "").strip()
    if api_token:
        app.add_middleware(_ApiTokenMiddleware, token=api_token)

    # Label /metrics responses with the serving replica (HA review 2026-09):
    # aggregate alerts over per-pod series need to know WHICH replica
    # answered (pod identity is otherwise only in the k8s scrape labels).
    # ON by default for /metrics only; SQLHANDLER_METRICS_POD_LABEL=0 opts
    # out. Additive response headers — body bytes unchanged, so scrapers
    # see the same exposition.
    if os.environ.get("SQLHANDLER_METRICS_POD_LABEL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    ):
        app.add_middleware(_MetricsPodIdentityMiddleware)

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
    # Identity-required gate: registered AFTER the resolver → LIFO puts it
    # OUTSIDE the resolver but still INSIDE the key middleware — the gate
    # reads the resolver's Caller from scope state and adds the anonymous
    # 401 on /mcp + /api/* when SQLHANDLER_REQUIRE_IDENTITY is truthy.
    # Registered unconditionally so the env stays per-request flippable;
    # unset env = byte-identical pass-through inside the middleware.
    app.add_middleware(_IdentityRequiredMiddleware)
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

    # Identity-required posture (the gate + its two misconfiguration notes).
    _require_identity = os.environ.get(_IdentityRequiredMiddleware.ENV_NAME, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if _require_identity:
        _log_gate = logging.getLogger("sqlhandler.server")
        if not _mcp_keys and not _oidc_identity.oidc_enabled():
            _log_gate.warning("=" * 72)
            _log_gate.warning(
                "%s is SET but no key gate is configured (MCP_API_KEYS/SQLHANDLER_API_KEYS) "
                "and OIDC is not enabled (SQLHANDLER_OIDC_ENABLED/SQLHANDLER_OIDC_ISSUER) — "
                "the ONLY rung that can resolve an identity is the browser-header rung "
                "(%s). Every other caller gets 401 on /mcp and /api/* (probes stay open). "
                "Configure a key gate or OIDC, or enable %s deliberately.",
                _IdentityRequiredMiddleware.ENV_NAME,
                _identity.TRUST_BROWSER_HEADERS_ENV,
                _identity.TRUST_BROWSER_HEADERS_ENV,
            )
            _log_gate.warning("=" * 72)
        else:
            _log_gate.info(
                "Identity REQUIRED on /mcp and /api/* (%s) — anonymous callers get 401 "
                "(WWW-Authenticate: Bearer); /health /ready /metrics and the /ui shell stay ungated.",
                _IdentityRequiredMiddleware.ENV_NAME,
            )
    elif os.environ.get("SQLHANDLER_POLICY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        logging.getLogger("sqlhandler.server").info(
            "SQLHANDLER_POLICY_ENABLED is on but %s is unset — ACLs are ADVISORY without it: "
            "anonymous callers resolve the default group. Set %s=1 to make identity required.",
            _IdentityRequiredMiddleware.ENV_NAME,
            _IdentityRequiredMiddleware.ENV_NAME,
        )
    if _oidc_identity.warn_if_misconfigured("sqlhandler"):
        pass  # the vendored module prints the loud fail-closed banner itself

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

    # Cross-replica readiness drift gate (HA review 2026-09): while the
    # backend check is failing (the /ready degraded band), stop admitting
    # NEW SQL work so a pod in a dead-backend window answers cheap calls
    # instead of starting doomed scans. OFF by default
    # (SQLHANDLER_READY_DRIFT_GATE: kubelet-independent, byte-identical
    # behavior until enabled). /mcp is exempt — MCP responses self-describe
    # errors and an agent should still be able to ask "why is my query
    # failing".
    app.add_middleware(_DriftGateMiddleware)

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
        app.add_middleware(GZipMiddleware, minimum_size=_compression.min_size, compresslevel=3)
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

    # Request body cap + security headers — added LAST so BOTH are the
    # OUTERMOST middleware (Starlette LIFO; cross-review finding: every
    # gate that short-circuits with its own response — ProbesAuth 401,
    # DriftGate 503, IdentityRequired/ApiToken 401, CORS preflight — sits
    # OUTSIDE anything added before it, and those were exactly the
    # responses missing CSP/nosniff/frame-deny). Oversized POST/PUT bodies
    # are counted and refused as they flow off the wire; per-route bounded
    # reads stay in webui.py as the inner net.
    app.add_middleware(_BodyLimitMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)
    return app


if __name__ == "__main__":
    main()
