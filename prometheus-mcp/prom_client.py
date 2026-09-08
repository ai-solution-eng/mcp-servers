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
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx

_PROM_URL_RE = re.compile(r"^https?://")


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
    """Resolve a sane step for a range query (default: span/240 points)."""
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

    def __init__(self, config: PromConfig | None = None, transport: httpx.BaseTransport | None = None):
        self.config = config or load_config()
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(self.config.timeout),
            transport=transport,  # None -> default; tests inject a mock
            headers=(
                {"Authorization": f"Bearer {self.config.bearer_token}"}
                if self.config.bearer_token
                else {}
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._client.get(path, params=params)
        except httpx.TimeoutException as e:
            raise PrometheusError(
                f"Prometheus at {self.config.base_url} timed out after "
                f"{self.config.timeout}s ({type(e).__name__})"
            ) from e
        except httpx.HTTPError as e:
            raise PrometheusError(
                f"Prometheus at {self.config.base_url} unreachable: "
                f"{type(e).__name__}: {e}"
            ) from e
        if resp.status_code != 200:
            detail = resp.text[:200]
            raise PrometheusError(f"Prometheus returned HTTP {resp.status_code}: {detail}")
        try:
            body = resp.json()
        except ValueError as e:
            raise PrometheusError(f"Prometheus returned non-JSON: {resp.text[:200]}") from e
        if body.get("status") != "success":
            raise PrometheusError(
                f"Prometheus query error: {body.get('errorType', 'unknown')}: "
                f"{body.get('error', 'no detail')}"
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
        data = await self._get(
            "/api/v1/series", {"match[]": match, "start": start, "end": end}
        )
        return data.get("result", [])

    async def label_values(self, label: str, match: str | None = None) -> list[str]:
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
            lines.append(
                f"    ({len(values)} raw points downsampled to {len(picked)}; "
                "shape preserved)"
            )
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
