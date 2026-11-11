"""Per-caller concurrency limits + result-size budgets (additive).

The process-global caps are the OUTER bound, not the whole story. Before this
module, ONE caller (a single agent loop, a runaway client) could take every
concurrency slot and every async-job slot on the pod, and one wide result
could blow the caller's model context. This module adds the identity-keyed
layer and the result budget, in the repo's default-safe idiom:

* The caller identity is resolved ONCE by the identity layer
  (``sqlhandler.identity.Caller``) and travels EXPLICITLY into the engine and
  the job registry (the ``caller`` keyword — the thread-boundary rule). Nothing
  here re-parses a request or trusts a header.
* NO IDENTITY = THE GLOBAL BUCKET, so stdio / dev / unauthenticated
  deployments keep the pre-existing behavior byte-for-byte.
* The per-caller knobs DEFAULT TO THE GLOBAL CAPS: an existing deployment
  keeps its concurrency until an operator opts into a fairness bound
  (``SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER`` /
  ``SQLHANDLER_MAX_JOBS_PER_CALLER``). The global cap always stays the outer
  bound, whatever the per-caller value.
* Every number is re-read per call (the fleet env convention) and a garbage
  value degrades to the default, never to "unlimited".

Three concerns live here:

1. :func:`caller_scope` / :func:`bounded_scope` — the stable, non-secret
   bucket key and its bounded label vocabulary (one series per
   operator-provisioned identity, never a raw key, hard-capped at
   :data:`MAX_SCOPES` so a creative identity source cannot mint unbounded
   series or semaphores).
2. :class:`CallerGate` — one semaphore per caller scope (queries), plus
   :func:`max_jobs_per_caller` for the async-job registry.
3. The result budget — :func:`max_result_bytes`, :func:`fit_result` and
   :func:`truncation_payload`: a configurable byte ceiling with a
   DETERMINISTIC truncation marker that states the total, the returned count
   and the continuation offset. Rows are never dropped silently.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable

from . import policy as _policy

#: The label used when no identity was resolved (stdio / dev / no gate).
ANONYMOUS = "anonymous"

#: The overflow label: once :data:`MAX_SCOPES` distinct scopes were seen, new
#: ones share this bucket (a hard cardinality bound on metric series and
#: semaphores — the fleet rule for any label derived from a request).
OTHER = "other"

#: Maximum number of distinct caller scopes tracked with their own bucket /
#: metric series. Comfortably above any real deployment's provisioned
#: identities (policy subjects + minted keys).
MAX_SCOPES = 256

_scope_lock = threading.Lock()
_seen_scopes: set[str] = set()


def caller_scope(caller) -> str | None:
    """The caller's stable bucket key, or ``None`` when no identity is present.

    ``None`` (anonymous / stdio / no gate) is the documented fallback: the
    caller then shares the GLOBAL bucket, exactly as before this module
    existed. The key is :func:`policy.owner_key`'s derivation — subject when
    attributed, else the key fingerprint, never the raw key — so it is safe
    to use as a metric label and stable across requests.
    """
    if caller is None:
        return None
    try:
        key = _policy.owner_key(caller)
    except Exception:  # a malformed caller must never break a query
        return None
    if not key or key == ANONYMOUS:
        return None
    return key


def bounded_scope(scope: str | None) -> str:
    """The bounded metric label for a caller scope (:data:`MAX_SCOPES` cap).

    Identity is operator-provisioned (policy subjects / minted key
    fingerprints), so the cardinality is bounded in practice; the cap makes it
    bounded BY CONSTRUCTION. Past the cap new scopes collapse onto
    :data:`OTHER` — they still get budget accounting, just a shared series.
    """
    if not scope:
        return ANONYMOUS
    with _scope_lock:
        if scope in _seen_scopes:
            return scope
        if len(_seen_scopes) >= MAX_SCOPES:
            return OTHER
        _seen_scopes.add(scope)
        return scope


def gate_scope(caller) -> str | None:
    """The BOUNDED bucket key for the concurrency gate; None = the global bucket.

    :func:`caller_scope` gives the stable key; this maps it through
    :func:`bounded_scope` so the semaphore table (and the job-cap buckets) are
    capped at :data:`MAX_SCOPES` entries too — past the cap, new identities
    share the ``other`` bucket (more restrictive, never unbounded). No identity
    stays ``None`` (the global bucket: the historical behavior).
    """
    scope = caller_scope(caller)
    return None if scope is None else bounded_scope(scope)


def reset_scopes() -> None:
    """Forget every observed scope (test seam; no runtime caller needs it)."""
    with _scope_lock:
        _seen_scopes.clear()


def _env_int(name: str, default: int) -> int:
    """Read one non-negative int env var (garbage / negative -> default)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)


def max_concurrent_per_caller(global_cap: int) -> int:
    """Per-caller concurrent-query cap (SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER).

    Default: the GLOBAL cap — i.e. no additional restriction, so an existing
    deployment keeps its concurrency. Set it LOWER than the global cap for
    fairness (one caller can no longer occupy every slot). ``0`` disables the
    per-caller gate entirely (the pre-change behavior). The global gate is
    always acquired too, so the global cap remains the outer bound.
    """
    return _env_int("SQLHANDLER_MAX_CONCURRENT_QUERIES_PER_CALLER", global_cap)


def max_jobs_per_caller(global_max: int) -> int:
    """Per-caller async-job cap (SQLHANDLER_MAX_JOBS_PER_CALLER).

    Same defaulting rule as :func:`max_concurrent_per_caller`: defaults to the
    global registry cap (no additional restriction), ``0`` disables the
    per-caller check. The global registry cap always applies on top.
    """
    return _env_int("SQLHANDLER_MAX_JOBS_PER_CALLER", global_max)


class CallerGate:
    """One semaphore per caller scope — the identity-keyed concurrency bucket.

    ``scope is None`` (no identity) is a pass-through: the GLOBAL gate alone
    bounds that caller, exactly as before. Sizes are re-read per acquire (the
    fleet env convention; tests reconfigure via env), and the semaphore is
    rebuilt when the configured size changes. Acquire returns the HELD
    semaphore (or ``None``) so release never depends on a size that changed
    mid-flight — the same hardening ``_QueryGate`` already applies.
    """

    def __init__(self) -> None:
        self._sems: dict[str, threading.Semaphore] = {}
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()

    def _semaphore(self, scope: str, size: int) -> threading.Semaphore | None:
        if size <= 0:
            return None
        with self._lock:
            sem = self._sems.get(scope)
            if sem is None or self._sizes.get(scope) != size:
                sem = threading.Semaphore(size)
                self._sems[scope] = sem
                self._sizes[scope] = size
            return sem

    def acquire(self, scope: str | None, size: int, timeout: float) -> tuple[bool, threading.Semaphore | None]:
        """Try to take one slot in *scope*; ``(acquired, held_semaphore)``.

        A ``None`` scope or a non-positive *size* is always granted (no
        per-caller bucket). The caller passes the held semaphore back to
        :meth:`release`.
        """
        if scope is None:
            return True, None
        sem = self._semaphore(scope, size)
        if sem is None:
            return True, None
        if sem.acquire(timeout=timeout):
            return True, sem
        return False, None

    def release(self, held: threading.Semaphore | None) -> None:
        """Release one caller slot (a no-op when no slot was held)."""
        if held is None:
            return
        try:
            held.release()
        except ValueError:  # pragma: no cover - defensive, mirrors _QueryGate
            pass


# ---------------------------------------------------------------------------
# result-size budget + pagination
# ---------------------------------------------------------------------------

#: Default byte ceiling for ONE tool result (SQLHANDLER_MAX_RESULT_BYTES).
#: ~25k tokens of text: a single result must not be able to consume a model's
#: whole context, and the marker below always tells the agent how to page on.
DEFAULT_MAX_RESULT_BYTES = 100_000

#: Floor for a CONFIGURED byte ceiling. The truncation marker itself is a few
#: hundred bytes; a ceiling below that could never both explain the truncation
#: and honour the ceiling, so a smaller configured value is raised to this —
#: the guarantee ("the result fits the ceiling") wins over the exact number.
MIN_RESULT_BYTES = 1024

#: The machine-readable key of the truncation line. One final JSON line per
#: truncated result, exactly like errors.py's structured tail — a reader that
#: ignores the last line sees the payload; an agent that reads it knows the
#: total, what it got and how to continue.
TRUNCATION_KEY = "truncated"


def max_result_bytes() -> int:
    """Byte ceiling for one tool result (SQLHANDLER_MAX_RESULT_BYTES).

    Default :data:`DEFAULT_MAX_RESULT_BYTES` (100 000); ``0`` disables the
    ceiling (the historical unbounded behavior). A configured value below
    :data:`MIN_RESULT_BYTES` is raised to it — the truncation marker itself must
    fit, or the ceiling could not be honoured. Measured in UTF-8 bytes of the
    RENDERED text, which is what reaches the caller.
    """
    value = _env_int("SQLHANDLER_MAX_RESULT_BYTES", DEFAULT_MAX_RESULT_BYTES)
    return value if value == 0 else max(value, MIN_RESULT_BYTES)


def truncation_payload(
    *,
    tool: str,
    reason: str,
    unit: str,
    total: int | None,
    returned: int,
    offset: int,
    next_offset: int | None,
    byte_limit: int = 0,
    fix_hints: list[str] | None = None,
) -> dict:
    """The deterministic ``{"truncated": {...}}`` payload (the marker's body).

    States the TOTAL (what this result holds), the RETURNED count, the offset
    it started at and the continuation offset — plus, when paging is possible,
    the exact ``resume`` call an agent should make. ``total`` is ``None`` only
    when the producing tool genuinely cannot know it.
    """
    body: dict = {
        "tool": tool,
        "reason": reason,
        "unit": unit,
        "total": total,
        "returned": returned,
        "offset": offset,
        "next_offset": next_offset,
    }
    if byte_limit > 0:
        body["byte_limit"] = byte_limit
    if next_offset is not None and tool:
        body["resume"] = {"tool": tool, "arguments": {"offset": next_offset}}
    if fix_hints:
        body["fix_hints"] = list(fix_hints)
    return {TRUNCATION_KEY: body}


def truncation_human(payload: dict) -> str:
    """The one-line human sentence for a truncation payload (marker's header)."""
    body = payload.get(TRUNCATION_KEY) or {}
    returned = body.get("returned", 0)
    total = body.get("total")
    unit = body.get("unit", "rows")
    offset = body.get("offset", 0)
    nxt = body.get("next_offset")
    reason = body.get("reason", "budget")
    if returned == 0 and offset:
        # The page PAST the end: say so plainly (nothing is being withheld).
        have = f" (the result has {total} {unit})" if total is not None else ""
        return f"Result truncated ({reason}): no rows at offset {offset}{have}."
    shown = f"showing {returned} of {total} {unit}" if total is not None else f"showing {returned} {unit}"
    if offset:
        shown += f" from offset {offset}"
    if nxt is None:
        tail = "no further pages — narrow the request or raise the budget"
    else:
        tail = f"continue with offset={nxt}"
    return f"Result truncated ({reason}): {shown}; {tail}."


def truncation_text(payload: dict, *, comment: str = "") -> str:
    """Render a truncation payload as the trailing marker lines.

    One human line then ONE JSON line (the errors.py posture: the human text
    stays primary, the machine tail is the last line). ``comment`` prefixes
    every line for formats that need it (the arrow IPC text is
    comment-prefixed so its body stays base64-decodable).
    """
    human = truncation_human(payload)
    machine = json.dumps(payload, separators=(",", ":"), default=str)
    return f"\n{comment}{human}\n{comment}{machine}"


def has_truncation_marker(text: str) -> bool:
    """True when *text* already ends in a truncation JSON line (no double-cap)."""
    last = text.rstrip().rsplit("\n", 1)[-1].lstrip("# ").strip()
    return last.startswith('{"' + TRUNCATION_KEY + '":')


def fit_result(
    body: Callable[[int], str],
    marked: Callable[[int], str],
    requested: int,
    budget: int,
) -> tuple[str, int]:
    """The largest page of at most *requested* units that fits *budget* bytes.

    ``body(n)`` renders n units WITHOUT the truncation marker; ``marked(n)``
    renders the same page WITH it. The common case — everything fits and no
    marker is due — returns ``body(requested)`` byte-identically (the
    pre-budget output). When the body is over budget, the search runs on
    ``marked(n)`` so every candidate carries the marker (monotone: both the
    body and the marker only grow with n), which is what makes the result
    DETERMINISTIC: same input, same budget, same page.

    Returns ``(text, units_returned)``. ``units_returned`` can be 0 only when
    even the marker alone exceeds the budget — the marker is still returned, so
    a truncated result is never silent.
    """
    if budget <= 0:
        return body(requested), requested
    plain = body(requested)
    if len(plain.encode("utf-8")) <= budget:
        return plain, requested
    lo, hi = 0, requested
    best = 0
    best_text = marked(0)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = marked(mid)
        if len(candidate.encode("utf-8")) <= budget:
            best, best_text = mid, candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best_text, best


#: Fix hints shared by every row-budget truncation notice (kept SHORT: the
#: marker must stay small relative to the ceiling it explains).
ROW_BUDGET_HINTS = [
    "Rows past the budget were NOT returned — page with the offset below.",
    "Raise SQLHANDLER_MAX_OUTPUT_ROWS / SQLHANDLER_MAX_RESULT_BYTES for bigger pages.",
]

#: Fix hints for a byte-budget truncation of a non-paginated text result.
TEXT_BUDGET_HINTS = [
    "Narrow the request (filters / search_tables / fewer columns) — this tool does not paginate.",
    "Raise SQLHANDLER_MAX_RESULT_BYTES for a bigger budget.",
]


def cap_tool_text(text: str, tool: str) -> str:
    """Last-resort byte ceiling for a tool result that is not row-rendered.

    Tabular tools paginate at render time (server._arrow_to_output); this
    guard covers the rest (list_tables, profile_table, whoami, ...), where a
    large lake can still produce a listing far bigger than a model context. It
    truncates at a LINE boundary and appends the same deterministic marker.
    Two cases are deliberately left alone:

    * structured payloads (first non-space char ``{``/``[``) — cutting JSON
      mid-object yields an unparseable result, and every JSON payload the
      server emits is already bounded at render time;
    * text that already ends in a truncation marker (the tabular path already
      announced its own budget).
    """
    budget = max_result_bytes()
    if budget <= 0 or not text or len(text.encode("utf-8")) <= budget:
        return text
    if has_truncation_marker(text):
        return text
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return text
    lines = text.splitlines()

    def marked(n: int) -> str:
        payload = truncation_payload(
            tool=tool,
            reason="byte_budget",
            unit="lines",
            total=len(lines),
            returned=n,
            offset=0,
            next_offset=None,
            byte_limit=budget,
            fix_hints=TEXT_BUDGET_HINTS,
        )
        return "\n".join(lines[:n]) + truncation_text(payload)

    fitted, _n = fit_result(marked, marked, len(lines), budget)
    return fitted
