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
  GET  /api/gpu         -> NVIDIA GPU aggregate (DCGM exporter): per-GPU
                           util/memory/temp/power/NVLink, grouped into the
                           per-node NVLink domains, plus node + cluster
                           summaries — fails soft on clusters without DCGM
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

Wave-4 (D14): the dashboard aggregate ``/api/overview`` — ~14 upstream
queries per refresh — is served from a short-TTL response cache
(PROMETHEUS_OVERVIEW_CACHE_TTL, default 20s; 0 disables) keyed by the
request's actual parameter set. Cached responses are honestly marked
(``cached: true`` + ``cache_age_seconds``); fresh ones say ``cached:
false``. Failures are never memoized, and concurrent identical overviews
share a single computation (single-flight). Range steps are clamped via
prom_client.resolve_step (D14) with a ``step_notice`` when clamped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from prom_client import (
    LABEL_NAME_RE,
    PrometheusError,
    _downsample,
    _metric_str,
    format_value,
    parse_timestamp,
    resolve_step,
)

# Kept as an alias: the audit referenced webui._LABEL_RE; the rule now lives
# in prom_client (the client enforces it before URL-path interpolation).
_LABEL_RE = LABEL_NAME_RE

# /api/overview response cache (Wave-4 D14). Default TTL 20s: a browser
# refresh within the TTL is instant instead of re-running ~14 upstream
# queries (30-60s payload on a busy cluster). 0 disables the cache entirely
# (escape hatch — every refresh recomputes, exactly the pre-D14 behavior).
_OVERVIEW_CACHE_TTL_DEFAULT = 20.0


def _overview_cache_ttl(environ: dict[str, str] | None = None) -> float:
    """PROMETHEUS_OVERVIEW_CACHE_TTL in seconds (default 20; 0 disables).

    Re-read per request (the fleet env pattern). Non-numeric / negative
    values fall back to the default; fractional values are honored.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("PROMETHEUS_OVERVIEW_CACHE_TTL") or "").strip()
    if not raw:
        return _OVERVIEW_CACHE_TTL_DEFAULT
    try:
        ttl = float(raw)
    except ValueError:
        return _OVERVIEW_CACHE_TTL_DEFAULT
    if ttl < 0:
        return _OVERVIEW_CACHE_TTL_DEFAULT
    return ttl


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
    # GPU cards (DCGM exporter). Absent on clusters without DCGM -> fail soft.
    ("gpu_count", "count(DCGM_FI_DEV_GPU_UTIL)"),
    ("gpu_util_pct", "avg(DCGM_FI_DEV_GPU_UTIL)"),
    (
        "gpu_mem_pct",
        "100 * sum(DCGM_FI_DEV_FB_USED) / (sum(DCGM_FI_DEV_FB_USED) + sum(DCGM_FI_DEV_FB_FREE))",
    ),
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
    (
        "top_gpu_mem",
        'topk(8, sum by (exported_namespace, exported_pod) (DCGM_FI_DEV_FB_USED{exported_pod!=""}))',
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

# NVIDIA GPU (dcgm-exporter) canned queries for /api/gpu. Names are the
# dcgm-exporter defaults; FB_* are MiB, POWER_USAGE watts, NVLink KiB/s.
# On clusters without DCGM every block fails soft and the GPU tab shows "—".
_GPU_QUERIES = (
    ("util", "DCGM_FI_DEV_GPU_UTIL"),
    ("fb_used", "DCGM_FI_DEV_FB_USED"),
    ("fb_free", "DCGM_FI_DEV_FB_FREE"),
    ("temp", "DCGM_FI_DEV_GPU_TEMP"),
    ("power", "DCGM_FI_DEV_POWER_USAGE"),
    ("mem_copy", "DCGM_FI_DEV_MEM_COPY_UTIL"),
    ("nvlink", "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"),
    ("xid", "DCGM_FI_DEV_XID_ERRORS"),
)
# H200 NVL (and most 8-GPU NVL-class hosts) ship as two 4-GPU NVLink islands
# per node — verified live on G2: tensor-parallel workloads move NVLink
# traffic exactly on GPUs 4-7 while 0-3 idle. Indices apply identically on
# every GPU node. Override via env (JSON list of index groups), e.g.
#   PROM_UI_GPU_NVLINK_DOMAINS='[[0,1,2,3],[4,5,6,7]]'
_DEFAULT_GPU_DOMAINS = ((0, 1, 2, 3), (4, 5, 6, 7))


def _gpu_domains(environ: dict[str, str] | None = None) -> tuple[tuple[tuple[int, ...], ...], str]:
    """Parse the NVLink domain grouping (env override > default)."""
    env = os.environ if environ is None else environ
    raw = env.get("PROM_UI_GPU_NVLINK_DOMAINS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            groups = tuple(tuple(int(g) for g in group) for group in parsed)
            flat = [g for grp in groups for g in grp]
            if groups and all(groups) and len(set(flat)) == len(flat):
                return groups, "env"
        except (ValueError, TypeError):
            pass  # fall through to the default
    return _DEFAULT_GPU_DOMAINS, "default"


# Auto-detected NVLink topology: the chart's `nvlink-topology` DaemonSet runs
# `nvidia-smi topo -m` once per GPU-node boot and pushes this info metric to
# the pushgateway. Precedence in /api/gpu: explicit PROM_UI_GPU_NVLINK_DOMAINS
# (operator escape hatch) > detected islands > built-in default.
_NVLINK_DOMAIN_QUERY = "nvidia_gpu_nvlink_domain"


def _host_key(host: str) -> str:
    """Normalize a hostname for joining: first dot-label, lowercased.

    DCGM reports the fqdn (``pcai-se-scs04.hst.lab``); the topology DaemonSet
    pushes the k8s node name (``spec.nodeName`` — identical on G2, but be
    robust to either form).
    """
    return str(host).split(".", 1)[0].strip().lower()


def _nvlink_domains_from_metrics(data) -> dict[str, tuple[tuple[int, ...], ...]] | None:
    """nvidia_gpu_nvlink_domain series -> per-host island groups (or None).

    Returns None when the metric is absent or unusable so callers fall back
    to env/default — detection is strictly additive, never a hard dependency.
    """
    if not isinstance(data, dict):
        return None
    per_host: dict[str, dict[int, set[int]]] = {}
    for r in data.get("result", []):
        labels = r.get("metric") or {}
        host = (
            labels.get("hostname")
            or labels.get("Hostname")
            or labels.get("instance")
            or labels.get("exported_instance")
        )
        try:
            gpu, dom = int(labels.get("gpu", "")), int(labels.get("domain", ""))
        except (TypeError, ValueError):
            continue
        if host:
            per_host.setdefault(_host_key(host), {}).setdefault(dom, set()).add(gpu)
    out: dict[str, tuple[tuple[int, ...], ...]] = {}
    for host, doms in per_host.items():
        groups = tuple(tuple(sorted(gpus)) for _d, gpus in sorted(doms.items()))
        flat = [g for grp in groups for g in grp]
        # a GPU claimed by two domains is malformed data — skip that host
        if groups and len(set(flat)) == len(flat):
            out[host] = groups
    return out or None


def _gpu_join_key(labels: dict) -> tuple | None:
    """(Hostname, gpu-index) from a DCGM sample; falls back to device=nvidiaN."""
    host = labels.get("Hostname") or labels.get("hostname") or labels.get("instance")
    if not host:
        return None
    idx = labels.get("gpu")
    if idx is None or not str(idx).strip().lstrip("-").isdigit():
        m = re.search(r"nvidia(\d+)$", str(labels.get("device", "")))
        idx = m.group(1) if m else None
    if idx is None:
        return None
    return (str(host), int(idx))


def _gpu_block(data: dict) -> dict:
    """instant-query data -> {'series': {(host, idx): (float, labels)}, 'models': {host: model}}."""
    series: dict = {}
    models: dict = {}
    for r in data.get("result", []):
        labels = r.get("metric") or {}
        key = _gpu_join_key(labels)
        if key is None:
            continue
        try:
            value = float(r.get("value", [None, None])[1])
        except (TypeError, ValueError):
            continue
        series[key] = (value, labels)
        if labels.get("modelName"):
            models[key[0]] = str(labels["modelName"])
    return {"series": series, "models": models}


def _r(value, nd: int = 1):
    """Round for JSON, preserving None (unknown) — the UI renders '—'."""
    if value is None:
        return None
    try:
        return round(float(value), nd)
    except (TypeError, ValueError):
        return None


def _avg(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _sum_or_none(values: list) -> float | None:
    """Sum the present values; None only when NOTHING was reported.

    (A plain ``sum(...) or None`` would erase genuine zeros — an idle NVLink
    island reports 0 traffic, which the UI should show as 0, not "—".)
    """
    present = [v for v in values if v is not None]
    return _r(sum(present)) if present else None


def _gpu_snapshot(
    blocks: dict,
    default_domains: tuple[tuple[int, ...], ...],
    overrides: dict[str, tuple[tuple[int, ...], ...]],
    source: str,
) -> dict:
    """Assemble the /api/gpu payload from per-metric blocks (fail-soft).

    ``blocks`` maps each _GPU_QUERIES name to either an error dict
    (``{"error", "query"}``) or a ``_gpu_block`` result. ``default_domains``
    is the fallback index map (explicit env override or the built-in
    default); ``overrides`` carries the auto-detected islands per host
    (keyed by _host_key) and wins per host when present. The GPU tab only
    hard-fails when BOTH util and fb_used are missing — every other metric
    degrades to ``null`` fields the UI renders as "—".
    """

    def groups_for(host: str) -> tuple[tuple[int, ...], ...]:
        return overrides.get(host) or overrides.get(_host_key(host)) or default_domains

    def dom_of(host: str, idx: int) -> int | None:
        for di, grp in enumerate(groups_for(host)):
            if idx in grp:
                return di
        return None

    util_b, used_b = blocks.get("util", {}), blocks.get("fb_used", {})
    if "error" in util_b and "error" in used_b:
        first_err = next((b["error"] for b in blocks.values() if "error" in b), "no data")
        return {
            "error": f"GPU metrics unavailable ({first_err})",
            "domains_source": source,
            "domains_config": [list(grp) for grp in default_domains],
            "domains_detected": {h: [list(g) for g in v] for h, v in overrides.items()} or None,
        }

    def val(name: str, key) -> float | None:
        series = blocks.get(name, {}).get("series", {})
        return series.get(key, (None, None))[0]

    keys = sorted({*util_b.get("series", {}), *used_b.get("series", {})}, key=lambda k: (k[0], k[1]))
    gpus: list[dict] = []
    by_host: dict[str, list[dict]] = {}
    for key in keys:
        host, idx = key
        labels: dict = (util_b.get("series", {}).get(key) or used_b.get("series", {}).get(key) or (None, {}))[1]
        used, free = val("fb_used", key), val("fb_free", key)
        total = (used or 0) + (free or 0) if used is not None or free is not None else None
        xid = val("xid", key)
        rec = {
            "hostname": host,
            "gpu": idx,
            "device": labels.get("device"),
            "uuid": labels.get("UUID"),
            "model": labels.get("modelName"),
            "domain": dom_of(host, idx),
            "util_pct": _r(val("util", key)),
            "mem_copy_pct": _r(val("mem_copy", key)),
            "mem_used_mib": _r(used),
            "mem_total_mib": _r(total),
            "mem_pct": _r(100 * used / total, 1) if used is not None and total else None,
            "temp_c": _r(val("temp", key)),
            "power_w": _r(val("power", key)),
            "nvlink_kib_s": _r(val("nvlink", key)),
            "xid": int(xid) if xid else 0,
            "namespace": labels.get("exported_namespace") or labels.get("namespace"),
            "pod": labels.get("exported_pod") or labels.get("pod"),
            "container": labels.get("exported_container") or labels.get("container"),
        }
        gpus.append(rec)
        by_host.setdefault(host, []).append(rec)

    def aggregate(records: list) -> dict:
        used = [r["mem_used_mib"] for r in records if r["mem_used_mib"] is not None]
        total = [r["mem_total_mib"] for r in records if r["mem_total_mib"] is not None]
        return {
            "gpu_count": len(records),
            "util_pct": _r(_avg([r["util_pct"] for r in records])),
            "mem_used_mib": _r(sum(used)) if used else None,
            "mem_total_mib": _r(sum(total)) if total else None,
            "mem_pct": _r(100 * sum(used) / sum(total), 1) if used and total and sum(total) else None,
            "power_w": _sum_or_none([r["power_w"] for r in records]),
            "max_temp_c": _r(max((r["temp_c"] for r in records if r["temp_c"] is not None), default=None)),
            "nvlink_kib_s": _sum_or_none([r["nvlink_kib_s"] for r in records]),
            "xid_gpus": sum(1 for r in records if r["xid"]),
        }

    nodes = []
    for host in sorted(by_host):
        recs = by_host[host]
        models = {r["model"] for r in recs if r["model"]}
        agg = aggregate(recs)
        agg.update({"hostname": host, "model": models.pop() if len(models) == 1 else ("mixed" if models else None)})
        nodes.append(agg)

    domains_out = []
    for host in sorted(by_host):
        for di, grp in enumerate(groups_for(host)):
            recs = [r for r in by_host[host] if r["gpu"] in grp]
            if not recs:
                continue
            agg = aggregate(recs)
            workloads: dict = {}
            for r in recs:
                if r["pod"]:
                    workloads.setdefault((r["namespace"], r["pod"]), []).append(r["gpu"])
            agg.update(
                {
                    "hostname": host,
                    "domain": di,
                    "gpus": sorted(grp),
                    "workloads": [
                        {"namespace": ns, "pod": pod, "gpus": sorted(idx)}
                        for (ns, pod), idx in sorted(workloads.items(), key=lambda kv: -len(kv[1]))
                    ],
                }
            )
            domains_out.append(agg)

    summary = aggregate(gpus) if gpus else {}
    models = {n["model"] for n in nodes if n["model"]}
    summary.update(
        {
            "gpus": len(gpus),
            "nodes": len(by_host),
            "model": models.pop() if len(models) == 1 else ("mixed" if models else None),
        }
    )
    return {
        "summary": summary,
        "nodes": nodes,
        "domains": domains_out,
        "gpus": gpus,
        "domains_source": source,
        "domains_config": [list(grp) for grp in default_domains],
    }


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
        values = [[int(float(t)), format_value(v)] for t, v in _downsample(r.get("values", []), cap_points)]
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

    # Per-app-instance cache + single-flight state (closure-scoped: separate
    # app builds — including tests — never share cache entries).
    _overview_cache: dict[str, tuple[float, dict]] = {}
    _overview_inflight: dict[str, asyncio.Future[tuple[dict, bool]]] = {}

    def _mark_overview(base: dict, cached: bool, age: float) -> JSONResponse:
        """Honest marking (D14): every overview response says whether it came
        from the cache and how old the payload is (fresh = cached false, 0)."""
        return JSONResponse({**base, "cached": cached, "cache_age_seconds": round(age, 3)})

    async def _compute_overview() -> tuple[dict, bool]:
        """Run the dashboard's ~14 upstream queries and build the payload.

        Returns ``(payload, memoizable)``. ``memoizable`` is False when EVERY
        block failed (upstream down) — failures are never memoized (D14); the
        normal fail-soft mode (individual cards erroring on a cluster that
        lacks e.g. DCGM) is a legitimate, cacheable payload.
        """
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
                    "items": [{"label": _short_top_label(r["labels"]), "value": r["value"]} for r in rows],
                    "query": query,
                }
            except Exception as exc:
                return {"error": str(exc), "query": query}

        async def trend(trend_window):
            _, query, start = trend_window
            try:
                s = parse_timestamp(start) or parse_timestamp("now-3h")
                e = parse_timestamp("now")
                step, clamp_notice = resolve_step("", s, e)
                data = await client.range_query(query, s, e, step)
                rows, _total = _matrix_rows(data, 5, config.max_points)
                block = {"series": rows, "start": s, "end": e, "step": step, "query": query}
                if clamp_notice:
                    block["step_notice"] = clamp_notice
                return block
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
        blocks = [*cards, *tops, *trends, alerts]  # gather results, in order
        memoizable = bool(blocks) and not all("error" in b for b in blocks)
        payload = {
            "generated_at": int(time.time()),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "cards": {cq[0]: c for cq, c in zip(_OVERVIEW_CARDS, cards)},
            "top": {tq[0]: t for tq, t in zip(_OVERVIEW_TOP, tops)},
            "trends": {tw[0]: t for tw, t in zip(_OVERVIEW_TRENDS, trends)},
            "alerts": alerts,
        }
        return payload, memoizable

    async def overview(request):
        """Dashboard aggregate, short-TTL cached (D14) and honestly marked.

        Cache key = the request's actual parameter set; entries expire after
        PROMETHEUS_OVERVIEW_CACHE_TTL seconds (0 disables everything, giving
        exactly the pre-D14 recompute-every-refresh behavior). Concurrent
        identical overviews share ONE computation (single-flight) instead of
        stampeding the upstream. Failures (every block errored) are never
        memoized.
        """
        ttl = _overview_cache_ttl()
        key = repr(tuple(sorted(request.query_params.items())))
        if ttl > 0:
            entry = _overview_cache.get(key)
            if entry is not None:
                age = time.monotonic() - entry[0]
                if age < ttl:
                    return _mark_overview(entry[1], cached=True, age=age)
            fut = _overview_inflight.get(key)
            if fut is None:
                fut = _overview_inflight[key] = asyncio.ensure_future(_compute_overview())
        else:
            fut = asyncio.ensure_future(_compute_overview())
        try:
            payload, memoizable = await fut
        finally:
            if ttl > 0 and _overview_inflight.get(key) is fut:
                del _overview_inflight[key]
        if ttl > 0 and memoizable:
            _overview_cache[key] = (time.monotonic(), payload)
        return _mark_overview(payload, cached=False, age=0.0)

    async def gpu(_request):
        """NVIDIA GPU (DCGM) aggregate: per-GPU + NVLink-domain + node views.

        One instant query per metric family (plus the optional nvlink
        topology info metric), gathered concurrently, then assembled
        client-agnostically by _gpu_snapshot. Fails soft: a cluster without
        DCGM returns ``{"error": ...}`` (HTTP 200) so the tab can render an
        honest empty state instead of breaking.
        """
        started = time.perf_counter()
        env_domains, env_source = _gpu_domains()

        async def fetch(name_query):
            name, query = name_query
            try:
                return name, {"query": query, **_gpu_block(await client.instant_query(query))}
            except Exception as exc:
                return name, {"error": str(exc), "query": query}

        async def fetch_domain_info():
            try:
                return await client.instant_query(_NVLINK_DOMAIN_QUERY)
            except Exception:
                return None  # absent / unreachable -> default grouping

        fetched, domain_info = None, None
        results = await asyncio.gather(*(fetch(nq) for nq in _GPU_QUERIES), fetch_domain_info())
        fetched, domain_info = results[:-1], results[-1]
        blocks = dict(fetched)
        detected = _nvlink_domains_from_metrics(domain_info)
        # Precedence: explicit operator override wins over ground truth wins
        # over the built-in default.
        if env_source == "env":
            payload = _gpu_snapshot(blocks, env_domains, {}, "env")
        elif detected:
            payload = _gpu_snapshot(blocks, _DEFAULT_GPU_DOMAINS, detected, "detected")
        else:
            payload = _gpu_snapshot(blocks, env_domains, {}, env_source)
        payload["generated_at"] = int(time.time())
        payload["duration_ms"] = int((time.perf_counter() - started) * 1000)
        payload["queries"] = {
            **{name: query for name, query in _GPU_QUERIES},
            "nvlink_domain_info": _NVLINK_DOMAIN_QUERY,
        }
        payload["domains_detected"] = {h: [list(g) for g in grps] for h, grps in detected.items()} if detected else None
        return JSONResponse(payload)

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
            step, clamp_notice = resolve_step(str(body.get("step") or ""), s, e)
            started = time.perf_counter()
            data = await client.range_query(q, s, e, step)
            rows, total = _matrix_rows(data, config.max_series, config.max_points)
            payload = {
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
            if clamp_notice:
                payload["step_notice"] = clamp_notice
            return JSONResponse(payload)
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
        Route("/api/gpu", gpu),
        Route("/api/alerts", alerts),
        Route("/api/rules", rules),
        Route("/api/query", query, methods=["POST"]),
        Route("/api/query_range", query_range, methods=["POST"]),
        Route("/api/series", series),
        Route("/api/label_values", label_values),
    ]


def _short_top_label(metric: dict) -> str:
    """Prefer a readable workload label for top-N charts.

    Workload-attributed series (DCGM ``exported_*`` and cAdvisor) carry both
    namespace and pod — render them as ``ns/pod``; fall back to the bare
    pod/instance label, then the full series id.
    """
    ns = metric.get("exported_namespace") or metric.get("namespace")
    pod = metric.get("exported_pod") or metric.get("pod")
    if pod and ns:
        return f"{ns}/{pod}"
    for key in ("exported_pod", "pod", "instance"):
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
