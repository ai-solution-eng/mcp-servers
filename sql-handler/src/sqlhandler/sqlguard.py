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
    f" Set {_MCP_READONLY_ENV}=0 to restore multi-statement/DDL execution "
    "for trusted callers."
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


# First words that mean a parenthesized group is the STATEMENT itself
# (``EXPLAIN (SELECT 1)``) rather than an option group
# (``EXPLAIN (FORMAT JSON) SELECT 1``).
_STATEMENT_LEAD_WORDS = frozenset(
    (  # noqa: SIM905 - one compact literal beats a 29-line list literal
        "select with values from table describe summarize show explain pragma "
        "insert update delete create drop alter copy set call attach detach "
        "export import begin commit rollback truncate merge use"
    ).split()
)


def _matching_paren(text: str, open_at: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``open_at``; None if unbalanced.

    Quote-aware (a paren inside a string literal or a quoted identifier does
    not count) so an option group containing a literal is scanned correctly.
    """
    depth = 0
    i = open_at
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2  # escaped quote (SQL doubling)
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


def _explain_inner_sql(statement: str) -> str:
    """Strip ``EXPLAIN [ANALYZE] [VERBOSE]`` (and any option group) from ``statement``.

    DuckDB accepts an OPTION GROUP between EXPLAIN and the statement —
    ``EXPLAIN (FORMAT JSON) SELECT ...``, ``EXPLAIN ANALYZE (FORMAT JSON)``,
    ``EXPLAIN (FORMAT JSON, ANALYZE false)``. The previous implementation
    lstripped the "(" and handed back ``FORMAT JSON) SELECT ...``, which does
    not parse, so EVERY option-bearing EXPLAIN was refused with a misleading
    "Could not parse SQL" — even a perfectly ordinary
    ``EXPLAIN (FORMAT JSON) SELECT ...`` (reproduced).

    The group is scanned as a BALANCED, quote-aware span (not a regex), so
    nested parens and string literals inside it are handled. A group whose
    first word is a statement lead (``EXPLAIN (SELECT 1)``) is treated as the
    statement itself, not as options.
    """
    rest = statement.strip()
    for keyword in ("EXPLAIN", "ANALYZE", "VERBOSE"):
        parts = rest.split(None, 1)
        if parts and parts[0].upper() == keyword:
            rest = parts[1].strip() if len(parts) > 1 else ""
        else:
            break
    rest = rest.lstrip(" \t\r\n")
    if rest.startswith("("):
        end = _matching_paren(rest, 0)
        if end is not None:
            group = rest[1:end].strip()
            tail = rest[end + 1 :].strip()
            first = group.split(None, 1)[0].lower().rstrip(",") if group else ""
            if first in _STATEMENT_LEAD_WORDS:
                return group
            rest = tail
    return rest


def _refuse_pragma(statement: str, *, context: str, env_note: str) -> None:
    """Raise when ``statement`` is (or wraps) a PRAGMA / ``pragma_*()`` call.

    Belt and braces: PRAGMA is a SET alias in DuckDB, and some table-valued
    pragmas even parse as SELECT — so reject BOTH the keyword itself and the
    ``pragma_*()`` function form the parser rewrites it into (no read-only
    surface has a use for either). Extracted so the rule can be applied to an
    EXPLAIN's INNER statement as well as to a top-level one.
    """
    head = statement.lstrip(" \t\r\n(\"'")
    if head.upper().startswith("PRAGMA") or _PRAGMA_FUNCTION_RE.search(statement):
        raise ValueError(
            f"{context}: PRAGMA statements are not allowed. "
            "Only SELECT / WITH / VALUES / EXPLAIN SELECT queries are permitted."
            f"{env_note}"
        )


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
        _refuse_pragma(statement, context=context, env_note=env_note)
        if stmt_type == "EXPLAIN":
            inner = _explain_inner_sql(statement)
            # The PRAGMA rule applies to the EXPLAINED statement too. Without
            # this, ``EXPLAIN PRAGMA table_info(t)`` /
            # ``EXPLAIN (FORMAT JSON) PRAGMA ...`` slipped past the outer
            # check (whose statement is an EXPLAIN) and only had to satisfy
            # "inner parses as SELECT" — a gap the outer-only rule left open.
            _refuse_pragma(inner, context=context, env_note=env_note)
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
            f" {_MCP_READONLY_ENV}=0 does not apply here: attached catalogs are "
            "read-only by design."
        ),
    )
