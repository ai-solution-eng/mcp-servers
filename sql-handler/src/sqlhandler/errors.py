"""Structured self-correcting errors (agent productivity pack, Wave 7 — additive).

LLM agents self-correct far faster when an error carries BOTH the human
message (primary, unchanged) AND a machine-parseable tail: a stable error
code plus ``fix_hints`` — concrete next actions ("call list_tables", "did you
mean: amount, kind") instead of three blind retries.

The mechanism is deliberately boring:

* Every call site keeps raising/returning its existing human message. When
  structured errors are ON (``SQLHANDLER_STRUCTURED_ERRORS``, default on),
  :func:`structured` / :func:`annotate` append ONE final line::

      {"error": {"code": "E_TABLE_NOT_FOUND", "fix_hints": [...]}}

  to that text. The human message stays primary and byte-identical; the
  tail is additive and optional — any reader that ignores the last line
  sees exactly the errors of today.

* Codes are STABLE identifiers (grep-able, log-aggregatable, referenced by
  future policy/write-tier surfaces). The mapping from messages/exceptions
  to codes lives in :func:`classify` and :func:`enrich` — pattern-based,
  best-effort, and never able to change the exception's text beyond the
  appended tail.

* An unrecognized error gets NO tail (not a guessy code) unless a caller
  pins a code explicitly via :func:`structured`.
"""

from __future__ import annotations

import json
import os
import re

# ---------------------------------------------------------------------------
# stable codes
# ---------------------------------------------------------------------------

E_TABLE_NOT_FOUND = "E_TABLE_NOT_FOUND"
E_COLUMN_NOT_FOUND = "E_COLUMN_NOT_FOUND"
E_READONLY = "E_READONLY"
E_TIMEOUT = "E_TIMEOUT"
E_CONCURRENCY_GATE = "E_CONCURRENCY_GATE"
E_ROWS_CAPPED = "E_ROWS_CAPPED"
E_PARAM_INVALID = "E_PARAM_INVALID"

# ---------------------------------------------------------------------------
# message → (code, fix_hints) patterns, first match wins
# ---------------------------------------------------------------------------

# ``enrich`` classifies a finished error message by its text, so every raise
# site keeps composing its message exactly as today and the mapping lives in
# ONE place. Patterns are anchored on the stable phrasings the code already
# produces (sqlguard's "not allowed", engine's timeout/gate sentences,
# _with_hints' "Did you mean", etc.).
#
# Each fix_hint is a concrete next action; entries rendered with ``{}`` get
# the first capture group of the pattern substituted in (e.g. the unknown
# table/column name), so hints can say "table 'X' was not in list_tables".
_ERROR_PATTERNS: list[tuple[re.Pattern, str, list[str]]] = [
    # Read-only refusals: sqlguard's context-prefixed ValueErrors (MCP gate,
    # attached-catalog gate, PRAGMA/COPY/write refusals on any surface).
    (
        re.compile(r"^Read-only[ :].*\bnot allowed\b", re.IGNORECASE | re.DOTALL),
        E_READONLY,
        [
            "Rewrite the request as a plain SELECT / WITH / VALUES / EXPLAIN SELECT query.",
            "Read-only is the deliberate default posture of this surface.",
        ],
    ),
    # Table resolution: engine._resolve / _resolve_catalog_target / describe.
    (
        re.compile(r"Table with name ['\"]?([\w/ .]+?)['\"]? does not exist"),
        E_TABLE_NOT_FOUND,
        [
            "Call list_tables to see the available table names.",
            (
                "Table '{}' does not exist — check for typos, or use schema/name "
                "(e.g. workorder/work_order) exactly as list_tables prints it."
            ),
        ],
    ),
    (
        re.compile(r"Table ['\"]?([\w/ .]+?)['\"]? not found in data source"),
        E_TABLE_NOT_FOUND,
        [
            "Call list_tables to see the available table names.",
            (
                "Table '{}' does not exist — check for typos, or use schema/name "
                "(e.g. workorder/work_order) exactly as list_tables prints it."
            ),
        ],
    ),
    # Column resolution: _validate_column's explicit message ...
    (
        re.compile(r"[Cc]olumn ['\"]?([\w ]+?)['\"]? does not exist on table"),
        E_COLUMN_NOT_FOUND,
        [
            (
                "Call describe_table to see the real column names (case matters only in quoted identifiers)."
            ),
            ("Column '{}' is not in the table's schema — check for typos or reordered names."),
        ],
    ),
    # ... and DuckDB's own resolution errors (_with_hints already appended
    # its "Did you mean" when it had candidates).
    (
        re.compile(r"[Cc]olumn ['\"]?([\w ]+?)['\"]? (?:does not exist|not found)"),
        E_COLUMN_NOT_FOUND,
        [
            "Use the exact column names from describe_table.",
            (
                "Column '{}' is not in the referenced table(s) — check for typos or a missing table alias."
            ),
        ],
    ),
    # Query timeout: engine.query_duckdb's watchdog sentence (verbatim).
    (
        re.compile(r"Query timed out after [\d.]+s \(SQLHANDLER_QUERY_TIMEOUT\)"),
        E_TIMEOUT,
        [
            "Raise SQLHANDLER_QUERY_TIMEOUT if the workload legitimately needs longer.",
            (
                "Trim the query: add WHERE filters / column projections, or split aggregations into steps."
            ),
            "For long-running queries use query_submit (async job) and poll query_status.",
        ],
    ),
    # Concurrency gate: _QueryGate.acquire's refusal (verbatim) and the
    # async-job registry's over-cap refusal (jobs.py, both 429 shapes).
    (
        re.compile(r"Too many concurrent queries \(limit \d+\)"),
        E_CONCURRENCY_GATE,
        [
            "Wait briefly and retry — other queries are still draining.",
            (
                "Raise SQLHANDLER_MAX_CONCURRENT_QUERIES (or the queue wait, "
                "SQLHANDLER_QUEUE_TIMEOUT) if this pod is sized for more."
            ),
        ],
    ),
    (
        re.compile(r"Too many active query jobs \(\d+"),
        E_CONCURRENCY_GATE,
        [
            (
                "Poll query_status and fetch finished results with query_result (fetch-once frees the registry slot)."
            ),
            "Raise SQLHANDLER_MAX_JOBS if this pod legitimately tracks more jobs.",
        ],
    ),
    # Parameter validation (client input errors).
    (
        re.compile(r"\bparams\b[ :].*?(?:must|need)", re.IGNORECASE),
        E_PARAM_INVALID,
        [
            (
                'Pass params as an object ({"name": value} for $placeholders) '
                "or an array (positional ?) of scalar values."
            ),
            (
                "Nested objects/arrays are not valid bind values — flatten them into the SQL instead."
            ),
        ],
    ),
    # explain_query / ask_data argument validation (agent pack): a missing
    # or empty required argument is the caller's bug — point at the shape.
    (
        re.compile(r"Provide the (?:SQL|question) to "),
        E_PARAM_INVALID,
        [
            (
                'explain_query needs {"sql": "<SELECT ...>"}; ask_data needs '
                '{"question": "<plain-language question>"}.'
            ),
            "An empty or whitespace-only value is not a valid argument.",
        ],
    ),
    (
        re.compile(r"(?:include_plan|execute) must be a boolean"),
        E_PARAM_INVALID,
        [
            "Pass a real boolean (true/false), not a string.",
            "ask_data never executes; execute is accepted for symmetry and has no effect.",
        ],
    ),
    # The guard's own empty-statement refusal (an explicitly-sent empty or
    # whitespace-only query reached the tool — dispatch's missing-argument
    # gate has already handled the absent/mis-keyed case): point at the
    # shape so the caller self-corrects instead of suspecting the server.
    (
        re.compile(r"^Empty SQL statement\.$"),
        E_PARAM_INVALID,
        [
            (
                'The query argument arrived empty — send the SQL in the "sql" key '
                '(e.g. run_sql {"sql": "SELECT ..."}; query_submit/query_save/ '
                'explain_query also take "sql", ask_data takes "question").'
            ),
            (
                "A missing or mis-keyed argument arrives as an empty string — "
                "check the tool's schema in tools/list for the exact key names."
            ),
        ],
    ),
    # Unsupported output_format (server.py _validate_output_format): the
    # tool schema's enum is the source of truth — point back at it.
    (
        re.compile(r"^Unsupported output_format "),
        E_PARAM_INVALID,
        [
            ("Pick a format from the output_format enum in the tool's schema (markdown / json / csv / arrow)."),
            ("The format name is matched case-insensitively — a typo'd name (e.g. 'parquet') is refused."),
        ],
    ),
]


def register_pattern(pattern: re.Pattern[str], code: str, hints: list[str]) -> None:
    """Add one message pattern to the classify table (module-extension hook).

    Other modules (the write tier) register their stable codes here instead
    of mutating this module's table directly — one typed entry point, first
    match wins like every pattern above.
    """
    _ERROR_PATTERNS.insert(0, (pattern, code, hints))


# fix-hint templates may reference the pattern's first capture group via '{}';
# render them eagerly so callers get plain strings.
def _render_hints(pattern_hint: list[str], match: re.Match | None) -> list[str]:
    if match is None:
        return list(pattern_hint)
    first = match.group(1) if (match.re.groups or 0) >= 1 else None
    if first is None:
        return list(pattern_hint)
    return [h.format(first) if "{}" in h else h for h in pattern_hint]


def classify(message: str) -> tuple[str, list[str]] | None:
    """Map one finished error message to (code, fix_hints); None when unknown.

    Deliberately conservative: a message that matches nothing returns None
    and gets NO tail (a wrong code is worse than no code).
    """
    for pattern, code, hints in _ERROR_PATTERNS:
        m = pattern.search(message)
        if m:
            return code, _render_hints(hints, m)
    return None


# ---------------------------------------------------------------------------
# the JSON tail line
# ---------------------------------------------------------------------------


def structured_errors_enabled(environ: dict[str, str] | None = None) -> bool:
    """True (default) when error texts carry the machine-parseable tail.

    ``SQLHANDLER_STRUCTURED_ERRORS`` is re-read per call (same posture as
    SQLHANDLER_MCP_READONLY): setting it to 0/false/off/no restores
    byte-identical, tail-free error text without a restart.
    """
    env = os.environ if environ is None else environ
    return env.get("SQLHANDLER_STRUCTURED_ERRORS", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def structured(
    code: str, fix_hints: list[str] | None = None, environ: dict[str, str] | None = None
) -> str:
    """Render the JSON tail line for one code (empty string when disabled).

    Callers append it to the human message: ``msg + structured(...)``. When
    structured errors are off — or there is nothing to add — the empty
    string keeps the message byte-identical.
    """
    if not structured_errors_enabled(environ):
        return ""
    payload: dict = {"code": code}
    if fix_hints:
        payload["fix_hints"] = list(fix_hints)
    return "\n" + json.dumps({"error": payload}, separators=(",", ":"), default=str)


def enrich(message: str, environ: dict[str, str] | None = None) -> str:
    """Append the code/fix_hints tail to a finished error message.

    The human message is never modified — the tail is ONE additional line
    (``\\n{"error": {...}}``). Unrecognized messages pass through untouched,
    and any internal failure here degrades to the plain message (best-effort
    by contract: enrichment must never break error reporting).
    """
    try:
        if not message or not structured_errors_enabled(environ):
            return message
        classified = classify(message)
        if classified is None:
            return message
        code, hints = classified
        return message + structured(code, hints, environ)
    except Exception:
        return message
