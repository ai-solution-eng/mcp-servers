"""Read-only web UI + JSON API for the Prometheus MCP server.

Serves the self-contained HPE-branded explorer (``ui/index.html``) and a
small JSON API that reuses the SAME process-wide ``PrometheusClient`` (and
its config caps) that backs the MCP tools. The API is deliberately
**read-only** — it only wraps the Prometheus HTTP API's query-side
endpoints (instant/range query, series/label metadata, alerts, rules), so
the UI is a safe human front-end for exactly the data the MCP agents
query. Nothing here can write to Prometheus or the cluster.

Endpoints (all JSON unless noted):

  GET  /                -> the HTML UI (also at /ui)
  GET  /api/status      -> {"status", "prometheus", "caps": {...}}
  GET  /api/overview    -> dashboard aggregate (cards, top-N, trends, alerts)
                           each item fails SOFT: {"error": "..."} per card,
                           because metric names differ between stacks
  GET  /api/alerts      -> {"alerts": [...], "n_total", "n_shown", "counts"}
  GET  /api/rules       -> {"groups": [...], "n_total"} (state/search filters)
  POST /api/query       -> instant query  {"query", "time"?}
  POST /api/query_range -> range query    {"query", "start", "end", "step"?}
  GET  /api/series      -> {"result": [...]} for a selector (label discovery)
  GET  /api/label_values-> {"values": [...]} for one label (+optional match)

Browser payloads reuse the MCP server's LLM-safety caps: series counts are
capped and range points downsampled (shape preserved) so a busy cluster
never floods the browser either. Alerts/rules get their own caps with
accurate ``n_total`` counts, since those lists are for scanning, not for a
model's context window.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from prom_client import (
    PrometheusError,
    _downsample,
    _metric_str,
    format_value,
    parse_step,
    parse_timestamp,
)

_HTML_CANDIDATES = (
    Path(__file__).parent / "ui" / "index.html",  # source tree / editable install
    Path(__file__).parent.parent / "ui" / "index.html",
    Path("/app/ui/index.html"),  # Docker image (WORKDIR /app)
)
_MAX_ALERTS = 500
_MAX_RULES = 500

_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Dashboard canned queries. Every stack is different, so each card degrades
# to {"error": ...} instead of failing the whole dashboard. These target the
# kube-prometheus-stack metric names (node-exporter, cAdvisor,
# kube-state-metrics) present on the G2 cluster.
_OVERVIEW_CARDS = (
    ("up_targets", "count(up == 1)"),
    ("up_total", "count(up)"),
    ("node_cpu_pct", '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))'),
    (
        "node_mem_pct",
        "100 * (1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))",
    ),
    ("pods_running", 'sum(kube_pod_status_phase{phase="Running"})'),
)
_OVERVIEW_TOP = (
    (
        "top_cpu",
        'topk(8, sum by (pod) (rate(container_cpu_usage_seconds_total{container!="",image!=""}[5m])))',
    ),
    (
        "top_mem",
        'topk(8, sum by (pod) (container_memory_working_set_bytes{container!="",image!=""}))',
    ),
)
_OVERVIEW_TRENDS = (
    (
        "cpu_trend",
        'sum(rate(container_cpu_usage_seconds_total{container!="",image!=""}[5m]))',
        "now-3h",
    ),
    (
        "mem_trend",
        'sum(container_memory_working_set_bytes{container!="",image!=""})',
        "now-3h",
    ),
)


def _load_html() -> str:
    override = os.environ.get("PROM_UI_HTML", "").strip()
    candidates = ([Path(override)] if override else []) + list(_HTML_CANDIDATES)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        "<!doctype html><meta charset='utf-8'><title>Prometheus MCP</title>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>Prometheus MCP — UI asset not found</h2>"
        "<p>The <code>ui/index.html</code> file was not located next to the "
        "server module. Set <code>PROM_UI_HTML</code> to its absolute path, or "
        "rebuild the image (the Dockerfile copies <code>ui/</code> into "
        "<code>/app</code>). The MCP endpoint and JSON API are unaffected.</p>"
    )


def _err_payload(exc: Exception) -> JSONResponse:
    if isinstance(exc, PrometheusError):
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")  # noqa: TRY004 — 400 semantics, not a caller bug
    return body


def _series_id(metric: dict) -> str:
    """Stable, readable series name for charts/tables: name{k=v,…}."""
    return _metric_str(dict(metric or {}))


def _vec_rows(data: dict, cap: int) -> tuple[list[dict], int]:
    """Instant-vector result -> [{series, value, ts}] (capped, formatted)."""
    result = data.get("result", [])
    rows = []
    for r in result[:cap]:
        value = r.get("value", [None, None])
        rows.append(
            {
                "series": _series_id(r.get("metric")),
                "labels": r.get("metric", {}),
                "ts": value[0],
                "value": format_value(value[1]),
            }
        )
    return rows, len(result)


def _matrix_rows(data: dict, cap_series: int, cap_points: int) -> tuple[list[dict], int]:
    """Range-vector result -> [{series, values: [[ts, v], ...]}] (capped +
    downsampled, values formatted, ts as int epoch seconds for charting)."""
    result = data.get("result", [])
    rows = []
    for r in result[:cap_series]:
        values = [
            [int(float(t)), format_value(v)]
            for t, v in _downsample(r.get("values", []), cap_points)
        ]
        rows.append({"series": _series_id(r.get("metric")), "labels": r.get("metric", {}), "values": values})
    return rows, len(result)


def build_ui_routes(client, config) -> list[Route]:
    """Routes for the web UI + JSON API.

    ``client`` / ``config`` are the same process-wide PrometheusClient and
    PromConfig the MCP tools use (dependency-injected so tests can stub
    them; importing them from ``server`` here would be circular).
    """

    async def ui(_request):
        return HTMLResponse(_load_html())

    async def status(_request):
        return JSONResponse(
            {
                "status": "ok",
                "prometheus": config.base_url,
                "caps": {
                    "max_series": config.max_series,
                    "max_points": config.max_points,
                    "max_label_values": config.max_label_values,
                    "max_alerts": _MAX_ALERTS,
                    "max_rules": _MAX_RULES,
                },
            }
        )

    async def overview(_request):
        """Dashboard aggregate. Every block fails soft, independently."""
        started = time.perf_counter()

        async def instant(query: str):
            return await client.instant_query(query)

        async def card(card_query):
            _, query = card_query
            try:
                rows, _total = _vec_rows(await instant(query), 1)
                return {"value": rows[0]["value"] if rows else None, "query": query}
            except Exception as exc:
                return {"error": str(exc), "query": query}

        async def top(top_query):
            _, query = top_query
            try:
                rows, _total = _vec_rows(await instant(query), config.max_series)
                return {
                    "items": [
                        {"label": _short_top_label(r["labels"]), "value": r["value"]}
                        for r in rows
                    ],
                    "query": query,
                }
            except Exception as exc:
                return {"error": str(exc), "query": query}

        async def trend(trend_window):
            _, query, start = trend_window
            try:
                s = parse_timestamp(start) or parse_timestamp("now-3h")
                e = parse_timestamp("now")
                step = parse_step("", s, e)
                data = await client.range_query(query, s, e, step)
                rows, _total = _matrix_rows(data, 5, config.max_points)
                return {"series": rows, "start": s, "end": e, "step": step, "query": query}
            except Exception as exc:
                return {"error": str(exc), "query": query}

        async def alerts_block():
            try:
                alerts = await client.alerts()
                counts = {"firing": 0, "pending": 0, "inactive": 0, "critical": 0, "warning": 0, "info": 0}
                for a in alerts:
                    state = a.get("state", "")
                    if state in ("firing", "pending", "inactive"):
                        counts[state] += 1
                    sev = a.get("labels", {}).get("severity", "")
                    if sev in ("critical", "warning", "info"):
                        counts[sev] += 1
                items = _shape_alerts(alerts)[:12]
                return {"counts": counts, "n_total": len(alerts), "items": items}
            except Exception as exc:
                return {"error": str(exc)}

        cards, tops, trends, alerts = await asyncio.gather(
            asyncio.gather(*(card(cq) for cq in _OVERVIEW_CARDS)),
            asyncio.gather(*(top(tq) for tq in _OVERVIEW_TOP)),
            asyncio.gather(*(trend(tw) for tw in _OVERVIEW_TRENDS)),
            alerts_block(),
        )
        return JSONResponse(
            {
                "generated_at": int(time.time()),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "cards": {cq[0]: c for cq, c in zip(_OVERVIEW_CARDS, cards)},
                "top": {tq[0]: t for tq, t in zip(_OVERVIEW_TOP, tops)},
                "trends": {tw[0]: t for tw, t in zip(_OVERVIEW_TRENDS, trends)},
                "alerts": alerts,
            }
        )

    async def alerts(_request):
        try:
            alerts = await client.alerts()
            counts = {"firing": 0, "pending": 0, "inactive": 0}
            for a in alerts:
                state = a.get("state", "")
                if state in counts:
                    counts[state] += 1
            return JSONResponse(
                {
                    "n_total": len(alerts),
                    "n_shown": min(len(alerts), _MAX_ALERTS),
                    "counts": counts,
                    "alerts": _shape_alerts(alerts)[:_MAX_ALERTS],
                }
            )
        except Exception as exc:
            return _err_payload(exc)

    async def rules(request):
        state_f = (request.query_params.get("state") or "").strip().lower()
        search = (request.query_params.get("search") or "").strip().lower()
        if state_f and state_f not in ("firing", "pending", "inactive"):
            return JSONResponse({"error": "state must be firing|pending|inactive"}, status_code=400)
        try:
            groups = await client.rules()
        except Exception as exc:
            return _err_payload(exc)
        out_groups, n_total = [], 0
        for g in groups:
            rules_out = []
            for r in g.get("rules", []):
                r_state = r.get("state", "")
                name = r.get("name", "")
                if state_f and r_state != state_f:
                    continue
                if search and search not in name.lower() and search not in str(r.get("query", "")).lower():
                    continue
                n_total += 1
                if len(rules_out) < _MAX_RULES:
                    rules_out.append(
                        {
                            "type": r.get("type", ""),
                            "name": name,
                            "state": r_state,
                            "query": r.get("query", ""),
                            "duration": r.get("duration", 0),
                            "health": r.get("health", ""),
                            "severity": r.get("labels", {}).get("severity", ""),
                            "summary": r.get("annotations", {}).get("summary", ""),
                        }
                    )
            if rules_out:
                out_groups.append({"name": g.get("name", ""), "rules": rules_out})
        return JSONResponse({"n_total": n_total, "groups": out_groups})

    async def query(request):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        q = str(body.get("query") or "").strip()
        if not q:
            return JSONResponse({"error": "query is required"}, status_code=400)
        try:
            ts = parse_timestamp(str(body.get("time") or "") or None)
            started = time.perf_counter()
            data = await client.instant_query(q, ts)
            rows, total = _vec_rows(data, config.max_series)
            return JSONResponse(
                {
                    "resultType": "vector",
                    "result": rows,
                    "n_total": total,
                    "n_shown": len(rows),
                    "query": q,
                    "time": ts,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except PrometheusError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except Exception as exc:
            return _err_payload(exc)

    async def query_range(request):
        try:
            body = await _json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        q = str(body.get("query") or "").strip()
        if not q:
            return JSONResponse({"error": "query is required"}, status_code=400)
        try:
            s = parse_timestamp(str(body.get("start") or "") or None) or parse_timestamp("now-1h")
            e = parse_timestamp(str(body.get("end") or "") or None) or parse_timestamp("now")
            step = parse_step(str(body.get("step") or ""), s, e)
            started = time.perf_counter()
            data = await client.range_query(q, s, e, step)
            rows, total = _matrix_rows(data, config.max_series, config.max_points)
            return JSONResponse(
                {
                    "resultType": "matrix",
                    "result": rows,
                    "n_total": total,
                    "n_shown": len(rows),
                    "start": s,
                    "end": e,
                    "step": step,
                    "query": q,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except PrometheusError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except Exception as exc:
            return _err_payload(exc)

    async def series(request):
        match = (request.query_params.get("match") or "").strip()
        if not match:
            return JSONResponse({"error": "match selector is required"}, status_code=400)
        try:
            s = parse_timestamp(request.query_params.get("start") or None)
            e = parse_timestamp(request.query_params.get("end") or None)
            result = await client.series(match, s, e)
            return JSONResponse(
                {
                    "n_total": len(result),
                    "n_shown": min(len(result), config.max_series),
                    "result": result[: config.max_series],
                }
            )
        except PrometheusError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except Exception as exc:
            return _err_payload(exc)

    async def label_values(request):
        label = (request.query_params.get("label") or "").strip()
        if not _LABEL_RE.match(label):
            return JSONResponse({"error": "label must be a valid Prometheus label name"}, status_code=400)
        match = (request.query_params.get("match") or "").strip() or None
        try:
            values = await client.label_values(label, match)
            return JSONResponse(
                {
                    "label": label,
                    "n_total": len(values),
                    "n_shown": min(len(values), config.max_label_values),
                    "values": values[: config.max_label_values],
                }
            )
        except PrometheusError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except Exception as exc:
            return _err_payload(exc)

    return [
        Route("/", ui),
        Route("/ui", ui),
        Route("/api/status", status),
        Route("/api/overview", overview),
        Route("/api/alerts", alerts),
        Route("/api/rules", rules),
        Route("/api/query", query, methods=["POST"]),
        Route("/api/query_range", query_range, methods=["POST"]),
        Route("/api/series", series),
        Route("/api/label_values", label_values),
    ]


def _short_top_label(metric: dict) -> str:
    """Prefer the pod/instance label for top-N charts, fall back to series id."""
    for key in ("pod", "instance", "namespace"):
        if metric.get(key):
            return str(metric[key])
    return _series_id(metric)


def _shape_alerts(alerts: list[dict]) -> list[dict]:
    """Normalize alerts for the browser: state/severity pulled up top."""
    order = {"firing": 0, "pending": 1}
    shaped = []
    for a in alerts:
        labels = a.get("labels", {}) or {}
        ann = a.get("annotations", {}) or {}
        shaped.append(
            {
                "state": a.get("state", ""),
                "alertname": labels.get("alertname", "?"),
                "severity": labels.get("severity", ""),
                "activeAt": a.get("activeAt", ""),
                "value": format_value(str(a["value"])) if a.get("value") is not None else None,
                "summary": ann.get("summary", ""),
                "description": ann.get("description", ""),
                "labels": {k: v for k, v in labels.items() if k not in ("alertname", "severity")},
            }
        )
    shaped.sort(key=lambda a: (order.get(a["state"], 2), a["alertname"]))
    return shaped
