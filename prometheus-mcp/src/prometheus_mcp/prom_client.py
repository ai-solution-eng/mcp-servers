"""Async Prometheus query client for the Prometheus MCP server.

A thin, strictly read-only client over Prometheus's HTTP API (v1):
instant/range queries, series + label metadata, alert and rule state.

Design points:

* The URL points at an in-cluster Prometheus (kube-prometheus-stack's
  ``kubeprom-prometheus`` service on G2 by default). No credentials by
  default; an optional bearer token comes from an env-var NAME
  (PROM_BEARER_TOKEN_ENV — same secrets hygiene as SQLhandler's
  password_env: the token value never appears in config or tool output).
* Series data is compacted for LLM consumption: long ranges are downsampled
  to a bounded number of points per series, floats are rounded, and the
  number of series per response is capped — a range query over a busy
  cluster must never flood a model's context. Caps are env-tunable and
  reported in the output footer.
* Relative times ("now-6h", "now-30m", "now-1d") are accepted everywhere a
  timestamp is, so agents don't have to compute epoch seconds.
* Range-query resolution is clamped (Wave-4 D14): a step below
  PROMETHEUS_MIN_STEP_SECONDS (default 15s) is raised to the floor before it
  reaches the upstream server — ``step=1s`` over 24h would otherwise pull
  ~86k points per series. The clamp raises, never lowers; 0 disables it;
  and every range path that clamps says so (resolve_step returns a notice).
* Label NAMES are validated before they are interpolated into the
  ``/api/v1/label/<name>/values`` URL path (Wave-4 hardening): a weird name
  is rejected client-side with a clear error instead of reaching the server.
* Query HINTS (Wave-5 F4): a handful of static, purely-advisory patterns
  detected in the query text itself (a match-all label regex, a counter
  used without rate(), a bare range selector, an unanchored leading
  wildcard) are surfaced as a capped ``hints`` list that the query tools
  append to their results. Text analysis only — no extra HTTP, no
  results inspection, and suppressible per call.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx2

_PROM_URL_RE = re.compile(r"^https?://")

# Valid Prometheus label name (the webui handler's rule — now shared so the
# CLIENT enforces it too, before the name is interpolated into the
# /api/v1/label/<name>/values URL path).
LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class PrometheusError(Exception):
    """A Prometheus request failed (after status/parse handling)."""


@dataclass
class PromConfig:
    base_url: str
    timeout: float = 30.0
    max_series: int = 20
    max_points: int = 60
    max_label_values: int = 200
    bearer_token: str | None = None


def load_config(environ: dict[str, str] | None = None) -> PromConfig:
    """Build the client config from the environment (fail-soft defaults)."""
    env = os.environ if environ is None else environ

    def _int(name: str, default: int) -> int:
        try:
            return int(env.get(name, "") or default)
        except ValueError:
            return default

    base_url = env.get("PROM_URL", "http://kubeprom-prometheus.prometheus.svc.cluster.local:9090").strip()
    if not base_url:
        base_url = "http://localhost:9090"
    if not _PROM_URL_RE.match(base_url):
        base_url = "http://" + base_url

    token = None
    token_env = env.get("PROM_BEARER_TOKEN_ENV", "").strip()
    if token_env:
        token = env.get(token_env) or None

    return PromConfig(
        base_url=base_url.rstrip("/"),
        timeout=float(env.get("PROM_TIMEOUT", "30") or 30),
        max_series=max(1, _int("PROM_MAX_SERIES", 20)),
        max_points=max(5, _int("PROM_MAX_POINTS", 60)),
        max_label_values=max(1, _int("PROM_MAX_LABEL_VALUES", 200)),
        bearer_token=token,
    )


_RELATIVE_RE = re.compile(r"^now\s*-\s*(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_timestamp(value: str | float | None) -> str | None:
    """Normalize a Prometheus timestamp argument.

    Accepts: None (omit), "now", relative "now-6h"/"now-30m"/"now-1d"/"now-2w",
    unix seconds (int/float/numeric string), or RFC3339 passthrough
    (Prometheus parses those itself). Returns the string form for the API.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "now":
        return None if text == "" else str(int(time.time()))
    rel = _RELATIVE_RE.match(text)
    if rel:
        amount, unit = int(rel.group(1)), rel.group(2).lower()
        return str(int(time.time()) - amount * _UNIT_SECONDS[unit])
    try:
        return str(int(float(text)))  # unix seconds (incl. "1757337600.0")
    except ValueError:
        return text  # RFC3339 — passed through, Prometheus validates


def parse_step(step: str | None, start: str | None, end: str | None) -> str:
    """Resolve a sane step for a range query (default: span/240 points).

    Pure resolver — NO clamping (existing behavior, pinned by tests). Range
    call paths must use :func:`resolve_step`, which applies the D14 floor on
    top of this.
    """
    if step and step.strip():
        return step.strip()
    try:
        t0 = float(start) if start and _is_num(start) else None
        t1 = float(end) if end and _is_num(end) else None
        if t0 is not None and t1 is not None and t1 > t0:
            span = t1 - t0
            return f"{max(int(span / 240), 1)}s"
    except Exception:
        pass
    return "60s"


# ------------------------------------------------------------- step clamp (D14)
DEFAULT_MIN_STEP_SECONDS = 15

# Prometheus duration tokens: number + unit, concatenatable ("1m30s").
_STEP_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
    "y": 31536000.0,
}
_STEP_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)", re.IGNORECASE)


def min_step_seconds(environ: dict[str, str] | None = None) -> int:
    """Range-query step floor, from PROMETHEUS_MIN_STEP_SECONDS (default 15).

    ``0`` disables the clamp entirely (escape hatch — today's unclamped
    behavior). Unset / non-numeric / negative values fall back to the
    default. Re-read per call (the fleet env pattern) so operators and tests
    can flip it without reimporting.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("PROMETHEUS_MIN_STEP_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_MIN_STEP_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_MIN_STEP_SECONDS
    if value < 0:
        return DEFAULT_MIN_STEP_SECONDS
    return value


def step_seconds(step: str) -> float | None:
    """Parse a step value into seconds; None when it is not a duration.

    Accepts bare seconds ("15", "0.5") and Prometheus duration strings
    ("15s", "1m30s", "500ms"). Anything else (e.g. "banana") returns None —
    unparseable steps pass through untouched and Prometheus rejects them.
    """
    text = step.strip()
    if not text:
        return None
    try:
        return float(text)  # bare seconds form
    except ValueError:
        pass
    total, pos = 0.0, 0
    for m in _STEP_TOKEN_RE.finditer(text):
        if m.start() != pos:  # gap -> not a pure duration ("15abc")
            return None
        total += float(m.group(1)) * _STEP_UNIT_SECONDS[m.group(2).lower()]
        pos = m.end()
    return total if pos == len(text) and pos > 0 else None


def resolve_step(
    step: str | None,
    start: str | None,
    end: str | None,
    environ: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """Resolve a range-query step AND clamp it (D14) — returns (step, notice).

    The floor is PROMETHEUS_MIN_STEP_SECONDS (default 15s; 0 disables). A
    resolved step BELOW the floor is raised to it and a one-line honest
    notice is returned for the tool result, e.g.::

        step clamped to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS

    Steps at or above the floor pass through byte-unchanged with no notice;
    steps that do not parse as a duration also pass through (the upstream
    server rejects them itself). Every range-query path (MCP
    prom_query_range, /api/query_range, the overview trends) resolves its
    step here.
    """
    resolved = parse_step(step, start, end)
    floor = min_step_seconds(environ)
    if floor <= 0:
        return resolved, None
    secs = step_seconds(resolved)
    if secs is None or secs >= floor:
        return resolved, None
    clamped = f"{floor}s"
    requested = (step or "").strip() or resolved
    return clamped, (f"step clamped to {clamped} (requested {requested}) — PROMETHEUS_MIN_STEP_SECONDS")


def _is_num(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def format_value(value: str | None) -> str | None:
    """Round a Prometheus float string for LLM display (4 significant digits)."""
    if value is None:
        return None
    try:
        f = float(value)
    except ValueError:
        return value
    if f == 0:
        return "0"
    if abs(f) >= 1000 or abs(f) < 0.001:
        return f"{f:.4g}"
    return f"{round(f, 4):g}"


class PrometheusClient:
    """Read-only async client for one Prometheus server."""

    def __init__(self, config: PromConfig | None = None, transport: httpx2.AsyncBaseTransport | None = None):
        self.config = config or load_config()
        self._client = httpx2.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx2.Timeout(self.config.timeout),
            transport=transport,  # None -> default; tests inject a mock
            headers=({"Authorization": f"Bearer {self.config.bearer_token}"} if self.config.bearer_token else {}),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._client.get(path, params=params)
        except httpx2.TimeoutException as e:
            raise PrometheusError(
                f"Prometheus at {self.config.base_url} timed out after {self.config.timeout}s ({type(e).__name__})"
            ) from e
        except httpx2.HTTPError as e:
            raise PrometheusError(f"Prometheus at {self.config.base_url} unreachable: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            detail = resp.text[:200]
            raise PrometheusError(f"Prometheus returned HTTP {resp.status_code}: {detail}")
        try:
            body = resp.json()
        except ValueError as e:
            raise PrometheusError(f"Prometheus returned non-JSON: {resp.text[:200]}") from e
        if body.get("status") != "success":
            raise PrometheusError(
                f"Prometheus query error: {body.get('errorType', 'unknown')}: {body.get('error', 'no detail')}"
            )
        return body.get("data", {})

    # ------------------------------------------------------------- queries
    async def instant_query(self, query: str, ts: str | None = None) -> dict:
        """Instant query; returns {'resultType', 'result': [...]}."""
        return await self._get("/api/v1/query", {"query": query, "time": ts})

    async def range_query(self, query: str, start: str, end: str, step: str) -> dict:
        """Range query; returns {'resultType': 'matrix', 'result': [...]}."""
        return await self._get(
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )

    async def series(self, match: str, start: str | None = None, end: str | None = None) -> list[dict]:
        data = await self._get("/api/v1/series", {"match[]": match, "start": start, "end": end})
        return data.get("result", [])

    async def label_values(self, label: str, match: str | None = None) -> list[str]:
        if not LABEL_NAME_RE.match(str(label)):
            # The label NAME is interpolated into the URL path — reject
            # anything that is not a plain label name BEFORE it reaches the
            # server (Wave-4 hardening; same rule the webui handler applies).
            raise PrometheusError(
                f"invalid label name {str(label)!r}: must match "
                f"{LABEL_NAME_RE.pattern} (letters/digits/underscores, not "
                "starting with a digit)"
            )
        params = {"match[]": match} if match else {}
        data = await self._get(f"/api/v1/label/{label}/values", params)
        return data.get("result", [])

    async def alerts(self) -> list[dict]:
        data = await self._get("/api/v1/alerts", {})
        return data.get("alerts", [])

    async def rules(self) -> list[dict]:
        data = await self._get("/api/v1/rules", {})
        return data.get("groups", [])


# ------------------------------------------------------------------ shaping
def shape_instant(data: dict, max_series: int) -> str:
    """Format an instant-query result as compact lines, capped by max_series."""
    result = data.get("result", [])
    if not result:
        return "No results (the query matched no series at that instant)."
    lines = []
    for r in result[:max_series]:
        metric = _metric_str(r.get("metric", {}))
        value = r.get("value", [None, None])
        ts, val = value[0], format_value(value[1])
        lines.append(f"  {metric} = {val} (at {ts})")
    if len(result) > max_series:
        lines.append(f"  … {len(result) - max_series} more series truncated (narrow the query)")
    return "\n".join(lines)


def shape_range(data: dict, max_series: int, max_points: int) -> str:
    """Format a range query as compact per-series series with capped points.

    Downsampling picks evenly spaced samples across the full span, so the
    SHAPE of the series (ramp, spike, sawtooth, plateau) survives even when
    the raw resolution is far denser than the cap.
    """
    result = data.get("result", [])
    if not result:
        return "No results (the query matched no series in that range)."
    lines = [f"{len(result)} series (showing up to {max_series}):"]
    for r in result[:max_series]:
        metric = _metric_str(r.get("metric", {}))
        values = r.get("values", [])
        if not values:
            lines.append(f"  {metric}: (empty)")
            continue
        picked = _downsample(values, max_points)
        rendered = " ".join(f"{format_value(v)}@{int(float(t))}" for t, v in picked)
        lines.append(f"  {metric}: {rendered}")
        if len(values) > len(picked):
            lines.append(f"    ({len(values)} raw points downsampled to {len(picked)}; shape preserved)")
    if len(result) > max_series:
        lines.append(f"  … {len(result) - max_series} more series truncated (narrow the query)")
    return "\n".join(lines)


def _downsample(values: list, max_points: int) -> list:
    if len(values) <= max_points:
        return values
    step = (len(values) - 1) / (max_points - 1)
    idx = sorted({round(i * step) for i in range(max_points)})
    return [values[i] for i in idx]


def _metric_str(metric: dict) -> str:
    """Compact series identifier: name{label=value,...} (long labels trimmed)."""
    name = metric.pop("__name__", None) if isinstance(metric, dict) else None
    if not metric:
        return name or "{}"
    pairs = ",".join(f"{k}={v}" for k, v in sorted(metric.items())[:8])
    suffix = ",…" if len(metric) > 8 else ""
    return f"{name or ''}{{{pairs}{suffix}}}"


# ------------------------------------------------------- query hints (F4)
# Static, purely-advisory pattern checks over the query TEXT. They never
# inspect results, never touch the network, and never change what is sent
# upstream — the query tools append them (capped, suppressible) so an agent
# gets a nudge toward a cheaper/correcter form of the same query.

MAX_HINTS = 3

# Functions that consume range vectors (rate & the *_over_time family).
_OVER_TIME_RE = re.compile(
    r"\b(?:rate|irate|resets|changes|delta|idelta|deriv|increase|predict_linear|"
    r"holt_winters|histogram_quantile|"
    r"(?:avg|min|max|sum|count|quantile|stddev|stdvar|present|last|absent)_over_time)\s*\(",
    re.IGNORECASE,
)

# Counter naming conventions (Prometheus: counters end in _total; the
# histogram/summary components end in _count).
_COUNTER_NAME_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*(?:_total|_count))\b")

# A metric identifier (with optional {} matcher block) directly followed by
# a range-selector bracket — matched on the quote-blanked text, so matcher
# values can't confuse it.
_RANGE_SELECTOR_RE = re.compile(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\s*(?:\{[^{}]*\})?\s*\[")

# Label matchers with a quoted regex value: label=~"…" / label!~'…'.
_LABEL_MATCHER_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~)\s*(['\"])(.*?)\3")

# A regex value that (after ^/$ anchors are ignored) matches everything.
_MATCH_ALL_VALUE_RE = re.compile(r"^[\^]*\.[*+][\$]*$")
# A regex value that merely STARTS with an unbounded wildcard.
_LEADING_WILDCARD_RE = re.compile(r"^[\^]*\.[*+]")


def _outside_quotes(query: str) -> str:
    """Blank out quoted strings so label VALUES can't trip the heuristics
    (e.g. error_type="user_total" must not read as a counter)."""
    return re.sub(r'"[^"]*"|\'[^\']*\'', '""', query)


def query_hints(query: str) -> list[str]:
    """Advisory hints for one PromQL expression (at most :data:`MAX_HINTS`).

    Each hint is a single line prefixed with a short tag so agents can skim:

    * ``[cardinality]`` — a label matcher whose regex matches every value
      (``{pod=~".*"}``); suggest a narrower selector.
    * ``[counter]`` — a ``_total``/``_count`` metric used without rate(),
      increase() or any other over-time function; raw counter values only
      ever rise, so wrap it before charting.
    * ``[regex]`` — a matcher starting with an unbounded wildcard
      (``{pod=~".*myapp.*"}``); anchoring keeps matching cheap on
      high-cardinality labels.
    * ``[range-vector]`` — a bare ``metric[5m]`` selector with no over-time
      function; an instant query wants ``rate(metric[5m])`` (or the selector
      without the bracket).
    """
    text = str(query or "")
    if not text.strip():
        return []
    hints: list[str] = []
    bare = _outside_quotes(text)

    def matchers(text_src: str):
        """(label, value) for every quoted regex matcher, in order."""
        for m in _LABEL_MATCHER_RE.finditer(text_src):
            yield m.group(1), m.group(4)

    for label, value in list(matchers(text))[:16]:
        if _MATCH_ALL_VALUE_RE.match(value):
            hints.append(
                f'[cardinality] {label}=~"{value}" matches every {label} value — '
                f'narrow the selector (e.g. {label}=~"myprefix-.*") to bound the series count'
            )
            break
    if not _OVER_TIME_RE.search(bare):
        counter = _COUNTER_NAME_RE.search(bare)
        if counter:
            metric = counter.group(1)
            hints.append(
                f"[counter] {metric} looks like a counter — wrap it in rate({metric}[5m]) "
                "or increase(...) ; a raw counter only ever rises"
            )
    for label, value in list(matchers(text))[:16]:
        if not _MATCH_ALL_VALUE_RE.match(value) and _LEADING_WILDCARD_RE.match(value):
            hints.append(
                f'[regex] {label}=~"{value}" starts with a wildcard — anchor the pattern '
                f'(e.g. {label}=~"myprefix-.*") so matching stays cheap on high-cardinality labels'
            )
            break
    if not _OVER_TIME_RE.search(bare) and _RANGE_SELECTOR_RE.search(bare):
        hints.append(
            "[range-vector] the query selects a bare range (metric[5m]) — an instant query "
            "wants rate(metric[5m]) (or drop the [5m] for the raw value)"
        )
    return hints[:MAX_HINTS]
