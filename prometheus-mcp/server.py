"""
Prometheus MCP Server

An MCP 2.0 server for read-only querying of a Prometheus instance — the
*time-series* half of cluster observability that a Kubernetes API cannot
answer (the K8s MCP sees state: pods, events, logs, instantaneous `top`;
this server sees behavior over time: rates, trends, quantiles, alert state).

Tools (all read-only against Prometheus):
  prom_query         instant PromQL query (what is the value NOW / at time T)
  prom_query_range   range query (how did it behave between two times)
  prom_series        which series exist for a selector (label discovery)
  prom_label_values  values of one label (e.g. list pods / namespaces)
  prom_alerts        currently firing / pending alerts
  prom_rules         alerting & recording rules, filterable by state

Plus a small saved-query store around those same query paths (Wave-5 F4):
  query_save         name a query (+ optional run params) for later reuse
  query_list         what is saved
  query_delete       remove one saved query
  query_saved        RUN a saved query through the existing instant/range
                     paths (clamps/caps/validation all apply unchanged)

The streamable-http transport ALSO serves an HPE-branded, read-only web
UI at / (dashboard, PromQL query, alerts & rules, tool catalog) plus a
JSON API under /api/* — a human front-end over the same client and caps
that back these tools (see webui.py). The query editor supports
shareable deep links: the expression + time range live in the URL
fragment (#q=…&range=…), restored on load.

Configuration (environment variables):
  PROM_URL                  Prometheus base URL (default: the kube-prometheus-
                            stack service: http://kubeprom-prometheus.
                            prometheus.svc.cluster.local:9090)
  PROM_TIMEOUT              HTTP timeout seconds (default 30)
  PROM_MAX_SERIES           Max series per response (default 20)
  PROM_MAX_POINTS           Max points per range series, shape-preserving
                            downsample (default 60)
  PROM_MAX_LABEL_VALUES     Max label values per response (default 200)
  PROM_BEARER_TOKEN_ENV     Env-var NAME holding a bearer token (optional;
                            the token value itself never appears in config)
  PROMETHEUS_MIN_STEP_SECONDS  Range-query step floor in seconds (Wave-4
                            D14, default 15): a caller-supplied step below
                            the floor is clamped up to it, with an honest
                            notice line in the result. 0 disables clamping.
  PROMETHEUS_OVERVIEW_CACHE_TTL  Short-TTL cache for the /api/overview
                            dashboard payload, seconds (Wave-4 D14, default
                            20; 0 disables): a UI refresh within the TTL is
                            served instantly and marked cached=true with
                            cache_age_seconds. Concurrent identical
                            overviews share one computation (single-flight).
  PROMETHEUS_SAVED_QUERIES_PATH  JSON file for the saved-query store
                            (Wave-5 F4). Unset (default) → in-memory only
                            for the session (tool results say so); set →
                            durable, written atomically (tmp+rename).
  PROMETHEUS_METRICS_ENABLED  Serve the server's own /metrics counters
                            (Wave-3 C3; the chart gates this — default off).
"""

import argparse
import os
import sys
import traceback

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations

import mcp_metrics
import saved_queries
from prom_client import (
    PrometheusClient,
    PrometheusError,
    format_value,
    load_config,
    parse_timestamp,
    query_hints,
    resolve_step,
    shape_instant,
    shape_range,
)

config = load_config()
client = PrometheusClient(config)

mcp = MCPServer("prometheus-mcp")

print("Prometheus MCP Server initialized:", file=sys.stderr)
print(f"  PROM_URL: {config.base_url}", file=sys.stderr)
print(
    f"  caps: max_series={config.max_series} max_points={config.max_points}",
    file=sys.stderr,
)

_mcp_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _err(context: str, exc: Exception) -> str:
    if isinstance(exc, PrometheusError):
        return f"Error: {exc}"
    traceback.print_exc(file=sys.stderr)
    return f"Error: {context} failed: {type(exc).__name__}: {exc}"


# ----------------------------------------------------------- shared run paths
# The instant/range query bodies live in these helpers so prom_query,
# prom_query_range AND query_saved execute the EXACT same code — the saved
# path cannot drift from the direct path (clamps, caps, validation, hints).
# Both append the advisory hints block when hints fire and the caller has
# not suppressed them; hint-free queries return byte-identical results.

_HINTS_HEADER = "Hints (advisory — pattern-matched from the query, may not apply):"


def _with_hints(text: str, query: str, include_hints: bool) -> str:
    if not include_hints:
        return text
    hints = query_hints(query)
    if not hints:
        return text
    return text + "\n\n" + _HINTS_HEADER + "".join(f"\n  - {h}" for h in hints)


async def _instant_text(query: str, time: str = "", include_hints: bool = True) -> str:
    """One instant query through the client, shaped + hints (the prom_query body)."""
    ts = parse_timestamp(time or None)
    data = await client.instant_query(query, ts)
    out = shape_instant(data, config.max_series)
    return _with_hints(out, query, include_hints)


async def _range_text(
    query: str, start: str = "now-1h", end: str = "now", step: str = "", include_hints: bool = True
) -> tuple[str, str]:
    """One range query through the client, shaped + D14 clamp + hints
    (the prom_query_range body). Returns ``(text, effective_step)``."""
    s = parse_timestamp(start) or parse_timestamp("now-1h") or ""
    e = parse_timestamp(end) or parse_timestamp("now") or ""
    eff_step, clamp_notice = resolve_step(step, s, e)
    data = await client.range_query(query, s, e, eff_step)
    out = shape_range(data, config.max_series, config.max_points)
    header = f"(start={s} end={e} step={eff_step})"
    if clamp_notice:
        header += f"\n{clamp_notice}"
    return _with_hints(f"{header}\n{out}", query, include_hints), eff_step


@mcp.tool(
    title="Prometheus Instant Query",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def prom_query(query: str, ctx: Context, time: str = "", include_hints: bool = True) -> str:
    """Evaluate one PromQL expression at a single point in time (now, or a
    given timestamp). Use this for current values: request rates, error
    percentages, memory usage, queue depth, throttle ratios.

    Tip: rate windows need at least 2x the scrape interval — for a 30s
    scrape use rate(...[2m]) or wider.

    Args:
        query: PromQL expression, e.g. 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (pod)'.
        time: Optional timestamp: 'now' (default), relative 'now-6h'/'now-30m',
            unix seconds, or RFC3339.
        include_hints: Optional (default true). Set false to suppress the
            advisory hints block (pattern-matched from the query text) —
            the result is then byte-identical to a plain shaped answer.
        ctx: MCP context for logging.
    """
    try:
        out = await _instant_text(query, time, include_hints)
        await ctx.info(f"prom_query ok ({out.count(chr(10)) + 1} lines)")
        return out
    except Exception as e:
        return _err("instant query", e)


@mcp.tool(
    title="Prometheus Range Query",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def prom_query_range(
    query: str,
    ctx: Context,
    start: str = "now-1h",
    end: str = "now",
    step: str = "",
    include_hints: bool = True,
) -> str:
    """Evaluate a PromQL expression over a time range — the tool for
    trends, spikes, leaks, and sawtooth/plateau patterns. Use before and
    after instant queries when debugging: 'pod restarts' wants the memory
    CURVE, not one sample.

    Results are downsampled to a bounded number of evenly-spaced points per
    series (shape preserved) and the series count is capped, so answers stay
    readable. Narrow the label selectors rather than paging. A step below
    PROMETHEUS_MIN_STEP_SECONDS (default 15s) is clamped up to the floor,
    with a notice line in the result when that happens.

    Args:
        query: PromQL expression, e.g. 'container_memory_working_set_bytes{pod=~"myapp.*", container!=""}'.
        start: Range start: 'now-6h' (default 'now-1h'), unix seconds, or RFC3339.
        end: Range end: 'now' (default), unix seconds, or RFC3339.
        step: Query resolution (e.g. '15s', '5m'). Default: span/240 points,
            floored at PROMETHEUS_MIN_STEP_SECONDS.
        include_hints: Optional (default true). Set false to suppress the
            advisory hints block (pattern-matched from the query text) —
            the result is then byte-identical to a plain shaped answer.
        ctx: MCP context for logging.
    """
    try:
        out, eff_step = await _range_text(query, start, end, step, include_hints)
        await ctx.info(f"prom_query_range ok (step={eff_step})")
        return out
    except Exception as e:
        return _err("range query", e)


@mcp.tool(
    title="Prometheus Series Discovery",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def prom_series(match: str, ctx: Context, start: str = "now-1h", end: str = "now") -> str:
    """List the series matching a selector with their label sets — use it to
    discover what labels exist before writing an exact query (e.g. which
    pods export http_requests_total, what 'namespace' values look like).

    Args:
        match: Series selector, e.g. 'http_requests_total' or
            'http_requests_total{namespace="prod"}'.
        start: Lookback start (default 'now-1h').
        end: Lookback end (default 'now').
        ctx: MCP context for logging.
    """
    try:
        s = parse_timestamp(start) or parse_timestamp("now-1h")
        e = parse_timestamp(end) or parse_timestamp("now")
        result = await client.series(match, s, e)
        if not result:
            return f"No series match {match!r} in that window (wrong name, or no data in range)."
        cap = config.max_series
        lines = [f"{len(result)} series (showing up to {cap}):"]
        for series in result[:cap]:
            lines.append(f"  {_metric_str_safe(series)}")
        if len(result) > cap:
            lines.append(f"  … {len(result) - cap} more (narrow the selector)")
        return "\n".join(lines)
    except Exception as e:
        return _err("series lookup", e)


@mcp.tool(
    title="Prometheus Label Values",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def prom_label_values(label: str, ctx: Context, match: str = "", start: str = "", end: str = "") -> str:
    """List the values of one label (e.g. label='pod' with
    match='container_memory_working_set_bytes' lists pods that report
    memory; label='namespace' lists namespaces). Useful for building
    selectors without guessing.

    Args:
        label: The label name to list values for (must be a valid Prometheus
            label name — letters/digits/underscores; invalid names are
            rejected with a clear error before the request is made).
        match: Optional series selector to restrict the values (recommended
            — without it the values span ALL metrics).
        start: Optional lookback start.
        end: Optional lookback end.
        ctx: MCP context for logging.
    """
    try:
        s = parse_timestamp(start or None)
        e = parse_timestamp(end or None)
        values = await client.label_values(label, match or None)
        if (s or e) and match:
            # /label/<n>/values ignores start/end server-side; with a window
            # requested, use series() semantics instead for correctness.
            result = await client.series(match, s, e)
            values = sorted({str(m.get(label, "")) for m in result if m.get(label)})
        if not values:
            return f"No values for label {label!r} (with that selector, if any)."
        shown = values[: config.max_label_values]
        lines = [f"{len(values)} values for {label!r} (showing up to {config.max_label_values}):"]
        lines += [f"  {v}" for v in shown]
        if len(values) > len(shown):
            lines.append(f"  … {len(values) - len(shown)} more")
        return "\n".join(lines)
    except Exception as e:
        return _err("label values", e)


@mcp.tool(
    title="Prometheus Alerts",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def prom_alerts(ctx: Context) -> str:
    """List the alerts currently firing or pending on this Prometheus, with
    severity, labels, annotations (summary/description) and how long each
    has been active. The natural first call when a user asks 'is anything
    wrong?' or when a K8s MCP investigation needs the alert context.

    Args:
        ctx: MCP context for logging.
    """
    try:
        alerts = await client.alerts()
        if not alerts:
            return "No alerts firing or pending."
        order = {"firing": 0, "pending": 1}
        alerts = sorted(alerts, key=lambda a: (order.get(a.get("state", ""), 2), str(a.get("activeAt", ""))))
        lines = [f"{len(alerts)} alert(s):"]
        for a in alerts:
            labels = a.get("labels", {})
            ann = a.get("annotations", {})
            lines.append(
                f"  [{a.get('state', '?').upper()}] {labels.get('alertname', '?')}"
                f" — severity={labels.get('severity', '?')}"
                f" active={a.get('activeAt', '?')}"
            )
            labels_s = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "alertname")
            if labels_s:
                lines.append(f"      labels: {labels_s}")
            for key in ("summary", "description"):
                if ann.get(key):
                    lines.append(f"      {key}: {ann[key]}")
            val = format_value(str(a.get("value", "")) if a.get("value") is not None else None)
            if val is not None and val != "None":
                lines.append(f"      value: {val}")
        return "\n".join(lines)
    except Exception as e:
        return _err("alerts", e)


@mcp.tool(
    title="Prometheus Rules",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def prom_rules(ctx: Context, state: str = "", search: str = "") -> str:
    """List alerting/recording rules, optionally filtered. Use it to learn
    WHAT is monitored and WHY an alert fires (each rule shows its expr):
    the bridge between 'something is firing' and 'here is the exact
    condition and threshold'.

    Args:
        state: Optional filter: 'firing', 'pending', or 'inactive'.
        search: Optional case-insensitive substring match on rule/alert names.
        ctx: MCP context for logging.
    """
    try:
        state_l = (state or "").strip().lower()
        if state_l and state_l not in ("firing", "pending", "inactive"):
            return "Error: state must be one of firing, pending, inactive."
        search_l = (search or "").strip().lower()
        groups = await client.rules()
        lines = []
        total = 0
        for g in groups:
            for r in g.get("rules", []):
                r_state = r.get("state", "")
                name = r.get("name", "")
                if state_l and r_state != state_l:
                    continue
                if search_l and search_l not in name.lower() and search_l not in str(r.get("query", "")).lower():
                    continue
                total += 1
                kind = "alert" if r.get("type") == "alerting" else "record"
                duration = r.get("duration", 0)
                dur_s = f" for {int(duration)}s" if duration else ""
                lines.append(f"  [{r_state or 'n/a'}] {kind}: {name}{dur_s} (health={r.get('health', '?')})")
                lines.append(f"      expr: {r.get('query', '')}")
                if r.get("labels", {}).get("severity"):
                    lines.append(f"      severity: {r['labels']['severity']}")
                if r.get("annotations", {}).get("summary"):
                    lines.append(f"      summary: {r['annotations']['summary']}")
        if not lines:
            return "No rules match the given filters."
        header = f"{total} rule(s):"
        return "\n".join([header, *lines])
    except Exception as e:
        return _err("rules", e)


def _metric_str_safe(metric: dict) -> str:
    from prom_client import _metric_str

    return _metric_str(dict(metric))


# ---------------------------------------------------------------------------
# Saved queries (Wave-5 F4 — additive; see saved_queries.py)
# ---------------------------------------------------------------------------


def _store_note() -> str:
    """One honest line about where the store lives right now."""
    path = saved_queries.saved_queries_path()
    if path:
        return f"durable: {path}"
    return "in-memory only for this session (set PROMETHEUS_SAVED_QUERIES_PATH to persist across restarts)"


def _fmt_params(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""


@mcp.tool(
    title="Save a Query",
    annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False, idempotent_hint=True),
)
async def query_save(name: str, query: str, ctx: Context, params: dict | None = None) -> str:
    """Save a PromQL expression under a name for later reuse (Wave-5 F4).

    Names are sanitized (letters/digits/space/._- ; everything else becomes
    '_'); saving under an existing name overwrites it. ``params`` optionally
    records run defaults for :func:`query_saved` — the same arguments the
    query tools take: ``mode`` (instant|range), ``time`` (instant) or
    ``start``/``end``/``step`` (range). The query is stored verbatim and
    validated when it RUNS, not when it is saved.

    Args:
        name: Short name to save under (sanitized; e.g. 'cpu by pod').
        query: The PromQL expression to store.
        params: Optional run defaults: mode/time/start/end/step.
        ctx: MCP context for logging.
    """
    try:
        entry = saved_queries.get_store().save(name, query, params)
        suffix = f" (runs as {entry['params']['mode']})" if entry["params"].get("mode") else ""
        return (
            f"Saved {entry['name']!r} — {entry['query']}"
            f"{'; params: ' + _fmt_params(entry['params']) if entry['params'] else ''}"
            f"{suffix}\nStore: {_store_note()}"
        )
    except Exception as e:
        return _err("save query", e)


@mcp.tool(
    title="List Saved Queries",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def query_list(ctx: Context) -> str:
    """List the saved queries (name, params, expression), newest first
    (Wave-5 F4). Cheap — no Prometheus traffic.

    Args:
        ctx: MCP context for logging.
    """
    try:
        entries = saved_queries.get_store().items()
        if not entries:
            return f"No saved queries yet. Save one with query_save.\nStore: {_store_note()}"
        lines = [f"{len(entries)} saved query(ies) — newest first. Store: {_store_note()}:"]
        for entry in entries:
            params = _fmt_params(entry["params"])
            line = f"  {entry['name']}  —  {entry['query']}"
            if params:
                line += f"   [{params}]"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return _err("list saved queries", e)


@mcp.tool(
    title="Delete a Saved Query",
    annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False, destructive_hint=True),
)
async def query_delete(name: str, ctx: Context) -> str:
    """Delete one saved query by name (Wave-5 F4). The name is sanitized
    the same way query_save sanitizes it, so an approximate spelling still
    matches what was stored.

    Args:
        name: The saved query's name.
        ctx: MCP context for logging.
    """
    try:
        store = saved_queries.get_store()
        if store.delete(name):
            return f"Deleted {saved_queries.sanitize_name(name)!r}. {len(store)} remaining.\nStore: {_store_note()}"
        return f"Error: no saved query named {name!r} (query_list shows what exists)."
    except Exception as e:
        return _err("delete saved query", e)


@mcp.tool(
    title="Run a Saved Query",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def query_saved(name: str, ctx: Context, params: dict | None = None, include_hints: bool = True) -> str:
    """RUN a saved query — through the exact same instant/range paths as
    prom_query / prom_query_range, so clamps, caps and validation all apply
    unchanged (Wave-5 F4).

    The saved ``params`` provide the run defaults (mode/time/start/end/step);
    the ``params`` argument here overrides them per call. No saved mode →
    runs as an instant query at 'now'.

    Args:
        name: The saved query's name (see query_list).
        params: Optional per-call overrides: mode/time/start/end/step.
        include_hints: Optional (default true). Set false to suppress the
            advisory hints block.
        ctx: MCP context for logging.
    """
    try:
        store = saved_queries.get_store()
        entry = store.get(name)
        if entry is None:
            return f"Error: no saved query named {name!r} (query_list shows what exists)."
        eff = dict(entry["params"])
        eff.update(saved_queries.normalize_params(params))
        mode = eff.get("mode") or "instant"
        if mode == "range":
            out, _eff_step = await _range_text(
                entry["query"],
                eff.get("start", "now-1h"),
                eff.get("end", "now"),
                eff.get("step", ""),
                include_hints,
            )
            return f"(saved query {entry['name']!r})\n{out}"
        return f"(saved query {entry['name']!r})\n" + await _instant_text(
            entry["query"], eff.get("time", ""), include_hints
        )
    except Exception as e:
        return _err("run saved query", e)


# ---------------------------------------------------------------------------
# self-metrics (Wave-3 C3 — additive, chart-gated DEFAULT-OFF)
# ---------------------------------------------------------------------------

# Count every MCP-protocol tool call (per-tool {ok,error} counters — see
# mcp_metrics.py). Unconditional and inert: the counters exist from import
# time, but nothing exposes them unless the /metrics route below is mounted,
# which requires the chart to set PROMETHEUS_METRICS_ENABLED (metrics.enabled,
# default false). No behavior change when metrics are off. Outcome labeling:
# this server's tools report failures as "Error: ..." strings (never raise),
# so a returned error string counts as outcome="error" too. These are the
# MCP server's OWN self-metrics (its request volume / failure mix) — not the
# upstream Prometheus's series, which stay one client call away per tool.
mcp_metrics.instrument(mcp, error_result=lambda result: isinstance(result, str) and result.startswith("Error"))


def _metrics_enabled() -> bool:
    """Serve the /metrics endpoint? (PROMETHEUS_METRICS_ENABLED, default off).

    The chart renders this env — and the ServiceMonitor — ONLY when
    ``metrics.enabled: true`` (values), so a default deployment has no
    /metrics route at all. Read per call (env re-read, the fleet pattern)
    so tests can flip it without reimporting.
    """
    raw = os.environ.get("PROMETHEUS_METRICS_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL: the MCP session manager needs its lifespan to run (it starts
    the task group that serves /mcp sessions). Mounting only the routes —
    without ``lifespan=http_app.router.lifespan_context`` — yields a server
    whose probes pass but where EVERY /mcp request fails with
    "RuntimeError: Task group is not initialized" (the bug the first
    v0.1.0 deployment hit: /health 200, /mcp 500).
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from webui import build_ui_routes

    async def health(_request):
        return JSONResponse({"status": "ok", "prometheus": config.base_url})

    # MCP 2.0 (protocol 2026-07-28) is natively stateless: no initialize
    # handshake, no Mcp-Session-Id header, so any replica can serve any
    # request; json_response keeps plain-HTTP clients on single responses.
    # (The session-manager lifespan wiring below is still required — it runs
    # the task group that serves requests, stateless or not.)
    http_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=_mcp_transport_security,
    )
    routes = [
        Route("/health", health),
        Route("/healthz", health),
        # HPE-branded web UI + read-only JSON API at / and /api/* — a human
        # front-end over the SAME client/config that backs the MCP tools.
        *build_ui_routes(client, config),
        *list(http_app.routes),
    ]
    if _metrics_enabled():
        # Prometheus self-metrics (Wave-3 C3, ADDITIVE, default OFF): per-tool
        # request counters for THIS server (no queries, label names, or error
        # text are exported — see mcp_metrics.py). The route exists only when
        # the chart opted in (metrics.enabled=true → PROMETHEUS_METRICS_ENABLED).
        # There is no auth middleware here (read-only surface behind the
        # gateway); /metrics carries non-sensitive counters only.
        routes.append(Route("/metrics", mcp_metrics.endpoint))
    return Starlette(
        routes=routes,
        lifespan=http_app.router.lifespan_context,
    )


def main():
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    parser = argparse.ArgumentParser(description="Prometheus MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default=["stdio"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9095)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    app = _build_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    print(f"Prometheus MCP streamable-http endpoint: http://{args.host}:{args.port}/mcp")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
