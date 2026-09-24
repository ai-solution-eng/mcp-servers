"""Shared SQL statement guard (the read-only parser gate).

One implementation of the read-only rule, used by every SQL entry point:

* the web UI / JSON API (``webui.assert_readonly`` — always on),
* the MCP ``run_sql`` tool (``server.run_sql`` — decision D2, default on,
  ``SQLHANDLER_MCP_READONLY=0`` opts out),
* every query that can see an external attached catalog (``engine.QueryJob``
  — unconditional; attached databases are a source, never a sink, so the
  opt-out does not reach them).

The rule is enforced with DuckDB's OWN parser (``duckdb.extract_statements``):
string literals, comments and multi-statement text are handled by the real
grammar, unlike keyword-prefix checks. Each parsed statement also carries its
exact source span (``.query``), which replaces the old character-scanning
``_split_statements`` heuristic (quotes/semicolons inside literals were
miscounted there).
"""

from __future__ import annotations

import os
import re

# Env values that turn the MCP read-only mode OFF (everything else,
# including an unset variable, keeps it ON — default-on per decision D2).
_READONLY_OFF_VALUES = ("0", "false", "off", "no")

# DuckDB REWRITES some statements before they reach the parser's span:
# ``PRAGMA table_info(t)`` becomes ``"SELECT * FROM pragma_table_info('t');"``.
# Any statement invoking a ``pragma_*`` table function is therefore treated
# as a PRAGMA (the read-only surfaces have no use for them — and a
# table-valued pragma is exactly what the keyword form compiles into).
_PRAGMA_FUNCTION_RE = re.compile(r"\bpragma_[a-z0-9_]*\s*\(", re.IGNORECASE)

_MCP_READONLY_ENV = "SQLHANDLER_MCP_READONLY"

_MCP_OPT_OUT_NOTE = (
    f" Set {_MCP_READONLY_ENV}=0 to restore multi-statement/DDL execution for trusted callers."
)


def extract_statement_spans(sql: str) -> list[tuple[str, str]]:
    """Parse ``sql`` with DuckDB's real parser; return one (type, text) per statement.

    The text is the statement's exact source span (parser-provided, so
    semicolons inside string literals and comments never split a statement).
    Raises ValueError when the text does not parse at all.
    """
    import duckdb

    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:
        raise ValueError(f"Could not parse SQL: {exc}") from exc
    return [(str(s.type).split(".")[-1], s.query) for s in statements]


def _explain_inner_sql(statement: str) -> str:
    """Strip a leading ``EXPLAIN [ANALYZE] [VERBOSE]`` and return the rest."""
    rest = statement
    for keyword in ("EXPLAIN", "ANALYZE", "VERBOSE"):
        rest = rest.lstrip(" \t\r\n(")
        parts = rest.split(None, 1)
        if parts and parts[0].upper() == keyword:
            rest = parts[1] if len(parts) > 1 else ""
        else:
            break
    return rest


def assert_readonly(sql: str, *, context: str = "Read-only", env_note: str = "") -> str:
    """Return the trimmed SQL if it is a read-only statement, else raise ValueError.

    Every parsed statement must be a plain SELECT (DuckDB parses WITH/VALUES/
    SHOW/DESCRIBE/SUMMARIZE as SELECT too). EXPLAIN is allowed only when the
    explained statement is itself a SELECT — ``EXPLAIN ANALYZE INSERT``
    actually executes the insert, so it is rejected. PRAGMA/SET, COPY, and
    every write statement are rejected regardless of position.

    ``context`` names the surface in the error (e.g. "Read-only UI",
    "Read-only MCP (SQLHANDLER_MCP_READONLY)"); ``env_note`` appends the
    escape-hatch pointer where one exists.
    """
    text = sql.strip()
    if not text:
        raise ValueError("Empty SQL statement.")
    spans = extract_statement_spans(text)
    for stmt_type, statement in spans:
        # Belt and braces: PRAGMA is a SET alias in DuckDB, and some
        # table-valued pragmas even parse as SELECT — reject the keyword
        # itself and the ``pragma_*()`` function form the parser rewrites
        # it into (no read-only surface has a use for either).
        head = statement.lstrip(" \t\r\n(\"'")
        if head.upper().startswith("PRAGMA") or _PRAGMA_FUNCTION_RE.search(statement):
            raise ValueError(
                f"{context}: PRAGMA statements are not allowed. "
                "Only SELECT / WITH / VALUES / EXPLAIN SELECT queries are permitted."
                f"{env_note}"
            )
        if stmt_type == "EXPLAIN":
            inner = _explain_inner_sql(statement)
            for inner_type, _inner_span in extract_statement_spans(inner):
                if inner_type != "SELECT":
                    raise ValueError(
                        f"{context}: EXPLAIN of a {inner_type} statement is not allowed "
                        "(EXPLAIN ANALYZE would execute it). Only SELECT queries are permitted."
                        f"{env_note}"
                    )
        elif stmt_type != "SELECT":
            raise ValueError(
                f"{context}: {stmt_type} statements are not allowed. "
                "Only SELECT / WITH / VALUES / EXPLAIN SELECT queries are permitted."
                f"{env_note}"
            )
    return text


def mcp_readonly_enabled(environ: dict[str, str] | None = None) -> bool:
    """True (default) when MCP ``run_sql`` must stay SELECT-only (decision D2).

    ``SQLHANDLER_MCP_READONLY`` is re-read per call, like the API-key envs,
    so flipping it reaches a running process without a restart.
    """
    env = os.environ if environ is None else environ
    return env.get(_MCP_READONLY_ENV, "1").strip().lower() not in _READONLY_OFF_VALUES


def assert_mcp_readonly(sql: str) -> str:
    """The MCP ``run_sql`` guard (decision D2): SELECT-only by default.

    The error message names the env so an agent (or operator reading the
    transcript) can see both the rule and its escape hatch.
    """
    return assert_readonly(
        sql,
        context=f"Read-only MCP ({_MCP_READONLY_ENV})",
        env_note=_MCP_OPT_OUT_NOTE,
    )


def assert_attached_readonly(sql: str) -> str:
    """Unconditional SELECT-only guard for queries that touch attached catalogs.

    Attached external databases are documented as strictly read-only (see
    sqlhandler/external.py: "the database is a source, never a sink"); this
    is what makes an attached catalog unreachable as an exfiltration sink —
    ``INSERT INTO <sink>.t SELECT * FROM <attached>.…`` is refused here
    BEFORE any scanner extension is loaded on the query connection, and
    ``SQLHANDLER_MCP_READONLY=0`` does NOT lift it.
    """
    return assert_readonly(
        sql,
        context="Read-only (attached external databases)",
        env_note=(
            f" {_MCP_READONLY_ENV}=0 does not apply here: attached catalogs are read-only by design."
        ),
    )
