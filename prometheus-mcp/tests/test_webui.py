"""Unit tests for the Prometheus MCP web UI + JSON API (no network).

The UI routes are built with a dependency-injected client/config, so tests
pass a stub client (same pattern as the MCP wire tests) and drive the
Starlette app with TestClient.
"""

import re
import shutil
import subprocess
from typing import ClassVar

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from prometheus_mcp import webui
from prometheus_mcp.prom_client import PromConfig, PrometheusError


class StubClient:
    """Successful stand-in for PrometheusClient."""

    async def instant_query(self, query, ts=None):
        return {
            "resultType": "vector",
            "result": [
                {"metric": {"__name__": "up", "pod": f"p{i}"}, "value": [1757337600, str(i)]} for i in range(30)
            ],
        }

    async def range_query(self, query, start, end, step):
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "m", "pod": "p1"},
                    "values": [[str(1757337600 + i), str(i)] for i in range(1000)],
                }
            ],
        }

    async def series(self, match, start=None, end=None):
        return [{"__name__": "up", "pod": "p1"}, {"__name__": "up", "pod": "p2"}]

    async def label_values(self, label, match=None):
        return ["ns1", "ns2", "ns3"]

    async def alerts(self):
        return [
            {
                "state": "firing",
                "activeAt": "2026-09-08T10:00:00Z",
                "value": "3.1e3",
                "labels": {"alertname": "PodCrashLooping", "severity": "critical", "pod": "x"},
                "annotations": {"summary": "Pod x is crash-looping"},
            },
            {
                "state": "pending",
                "activeAt": "2026-09-08T11:00:00Z",
                "labels": {"alertname": "Watchdog", "severity": "none"},
                "annotations": {},
            },
        ]

    async def rules(self):
        return [
            {
                "name": "k8s.rules",
                "rules": [
                    {
                        "type": "alerting",
                        "name": "PodCrashLooping",
                        "state": "firing",
                        "query": "increase(kube_pod_container_status_restarts_total[1h]) > 3",
                        "duration": 900,
                        "health": "ok",
                        "labels": {"severity": "critical"},
                        "annotations": {"summary": "restarts too fast"},
                    },
                    {
                        "type": "recording",
                        "name": "node:mem:ratio",
                        "state": "ok",
                        "query": "node_memory_MemTotal_bytes",
                        "duration": 0,
                        "health": "ok",
                        "labels": {},
                        "annotations": {},
                    },
                ],
            }
        ]


def make_client(**client_kwargs) -> TestClient:
    cfg = PromConfig(base_url="http://prom.test:9090", max_series=5, max_points=10, max_label_values=2)
    app = Starlette(routes=webui.build_ui_routes(StubClient(), cfg))
    return TestClient(app)


# ---------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------


def test_ui_serves_hpe_branding_and_tabs():
    c = make_client()
    r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Hewlett Packard Enterprise" in html
    assert "hpe-element" in html  # the green parallelogram mark
    assert "prometheus-theme" in html  # no-flash theme script + persistence
    assert "Prometheus MCP" in html
    for tab in ("Dashboard", "GPU", "Query", "Alerts", "MCP Tools"):
        assert tab in html
    # tool catalog is rendered client-side from this embedded array
    for tool in ("prom_query", "prom_query_range", "prom_series", "prom_label_values", "prom_alerts", "prom_rules"):
        assert tool in html
    assert c.get("/ui").status_code == 200
    assert c.get("/ui").text == html


def test_ui_html_fallback_when_asset_missing(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(webui, "_HTML_CANDIDATES", (Path("/nonexistent/ui/index.html"),))
    html = webui._load_html()
    assert "UI asset not found" in html
    assert "PROM_UI_HTML" in html


def test_ui_has_no_duplicate_element_ids():
    # getElementById silently resolves to the FIRST match — duplicated ids
    # would leave one of the two panels rendering "—" forever.
    import re

    ids = re.findall(r'id="([^"]+)"', webui._load_html())
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate element ids: {sorted(dupes)}"


def test_status_endpoint_reports_caps():
    c = make_client()
    data = c.get("/api/status").json()
    assert data["status"] == "ok"
    assert data["prometheus"] == "http://prom.test:9090"
    assert data["caps"]["max_series"] == 5
    assert data["caps"]["max_alerts"] == webui._MAX_ALERTS


def test_status_reports_console_page_size(monkeypatch):
    c = make_client()
    monkeypatch.setenv("PROM_UI_PAGE_SIZE", "250")
    caps = c.get("/api/status").json()["caps"]
    assert caps["ui_page_size"] == 250
    assert caps["ui_max_page_size"] == webui._UI_MAX_PAGE_SIZE
    monkeypatch.setenv("PROM_UI_PAGE_SIZE", "999999")
    assert c.get("/api/status").json()["caps"]["ui_page_size"] == webui._UI_MAX_PAGE_SIZE
    monkeypatch.setenv("PROM_UI_PAGE_SIZE", "banana")
    assert c.get("/api/status").json()["caps"]["ui_page_size"] == webui._UI_PAGE_SIZE_DEFAULT


# ---------------------------------------------------------------------------
# query endpoints (caps, validation, error mapping)
# ---------------------------------------------------------------------------


def test_query_caps_series_and_formats():
    c = make_client()
    data = c.post("/api/query", json={"query": "up"}).json()
    assert data["resultType"] == "vector"
    assert data["n_total"] == 30 and data["n_shown"] == 5  # capped by config
    # No explicit limit -> the MCP cap stays the API default (unchanged
    # behavior for callers that never page); the console asks for its own.
    assert data["offset"] == 0 and data["limit"] == 5
    assert data["result"][0]["series"] == "up{pod=p0}"
    assert data["result"][0]["value"] == "0"
    assert "duration_ms" in data


def test_query_paginates_with_offset_and_limit():
    c = make_client()
    data = c.post("/api/query", json={"query": "up", "offset": 10, "limit": 5}).json()
    assert data["n_total"] == 30 and data["n_shown"] == 5
    assert data["offset"] == 10 and data["limit"] == 5
    assert [r["series"] for r in data["result"]] == [f"up{{pod=p{i}}}" for i in range(10, 15)]


def test_query_page_params_are_validated_and_capped():
    c = make_client()
    data = c.post("/api/query", json={"query": "up", "limit": 10**9}).json()
    assert data["limit"] == webui._UI_MAX_PAGE_SIZE  # clamped, like max_series
    assert data["n_shown"] == 30  # only 30 rows exist
    for bad in ({"offset": -1}, {"limit": 0}, {"limit": "ten"}, {"offset": "x"}):
        assert c.post("/api/query", json={"query": "up", **bad}).status_code == 400


def test_query_range_paginates_series():
    class ThreeSeries(StubClient):
        async def range_query(self, query, start, end, step):
            return {
                "resultType": "matrix",
                "result": [
                    {"metric": {"__name__": "m", "pod": f"p{i}"}, "values": [["1757337600", str(i)]]} for i in range(3)
                ],
            }

    cfg = PromConfig(base_url="http://prom.test:9090", max_series=1, max_points=10)
    c = TestClient(Starlette(routes=webui.build_ui_routes(ThreeSeries(), cfg)))
    data = c.post(
        "/api/query_range",
        json={"query": "m", "start": "1757337600", "end": "1757337700", "step": "15s", "offset": 1, "limit": 1},
    ).json()
    assert data["n_total"] == 3 and data["n_shown"] == 1
    assert data["offset"] == 1 and data["limit"] == 1
    assert [r["series"] for r in data["result"]] == ["m{pod=p1}"]


def test_query_csv_exports_every_series_not_the_page():
    c = make_client()
    page = c.post("/api/query", json={"query": "up"}).json()
    assert page["n_shown"] == 5  # the JSON page is capped by max_series ...
    r = c.post("/api/query.csv", json={"query": "up", "offset": 10, "limit": 5})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="query.csv"' in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert len(lines) == 30  # ... the export is page-free: every row
    assert lines[0] == '"up{pod=p0}",0'
    assert lines[-1] == '"up{pod=p29}",29'


def test_query_range_csv_exports_every_series_downsampled():
    class ThreeSeries(StubClient):
        async def range_query(self, query, start, end, step):
            return {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "m", "pod": f"p{i}"},
                        "values": [[str(1757337600 + j), str(j)] for j in range(1000)],
                    }
                    for i in range(3)
                ],
            }

    cfg = PromConfig(base_url="http://prom.test:9090", max_series=1, max_points=10)
    c = TestClient(Starlette(routes=webui.build_ui_routes(ThreeSeries(), cfg)))
    r = c.post("/api/query_range.csv", json={"query": "m", "start": "1757337600", "end": "1757341200", "step": "1s"})
    lines = r.text.strip().split("\n")
    assert len(lines) == 3  # max_series=1 does not cap the export
    first = lines[0].split(",")
    assert first[0] == '"m{pod=p0}"'
    assert len(first) == 1 + 2 * 10  # 10 downsampled points, each ts+value
    assert first[1] == "1757337600" and first[-1] == "999"  # edges preserved


def test_query_csv_requires_a_query():
    c = make_client()
    assert c.post("/api/query.csv", json={}).status_code == 400
    assert c.post("/api/query_range.csv", json={}).status_code == 400


def test_query_requires_query_param():
    c = make_client()
    assert c.post("/api/query", json={}).status_code == 400
    assert c.post("/api/query", json={"query": "   "}).status_code == 400
    r = c.post("/api/query", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_query_range_downsamples_points():
    c = make_client()
    data = c.post(
        "/api/query_range", json={"query": "m", "start": "1757337600", "end": "1757341200", "step": "1s"}
    ).json()
    assert data["resultType"] == "matrix"
    assert data["n_shown"] == 1
    values = data["result"][0]["values"]
    assert len(values) == 10  # max_points=10 (downsampled from 1000)
    # span edges preserved (shape kept): raw data runs 1757337600..+999
    assert values[0][0] == 1757337600 and values[-1][0] == 1757337600 + 999


def test_prometheus_error_maps_to_502():
    class FailingClient(StubClient):
        async def instant_query(self, query, ts=None):
            raise PrometheusError("Prometheus at http://prom.test:9090 timed out after 30s")

    cfg = PromConfig(base_url="http://prom.test:9090")
    app = Starlette(routes=webui.build_ui_routes(FailingClient(), cfg))
    c = TestClient(app)
    r = c.post("/api/query", json={"query": "up"})
    assert r.status_code == 502
    assert "timed out" in r.json()["error"]


def test_series_and_label_values():
    c = make_client()
    data = c.get("/api/series", params={"match": "up"}).json()
    assert data["n_total"] == 2 and len(data["result"]) == 2
    assert c.get("/api/series").status_code == 400  # missing selector

    data = c.get("/api/label_values", params={"label": "namespace", "match": "up"}).json()
    assert data["values"] == ["ns1", "ns2"]  # max_label_values=2 caps it
    assert data["n_total"] == 3
    # invalid label names are rejected (label goes into the URL path)
    for bad in ("", "a-b", "x;drop", "a/b"):
        assert c.get("/api/label_values", params={"label": bad}).status_code == 400


def test_series_and_label_values_over_the_real_client_envelope():
    """The console endpoints through a REAL client, so the wire shape is
    exercised too: /api/v1/series and /api/v1/label/<n>/values both return
    their payload as a LIST in "data" (the stub above hides that layer)."""
    import httpx2

    from prometheus_mcp.prom_client import PrometheusClient

    def handler(request):
        if request.url.path == "/api/v1/series":
            return httpx2.Response(200, json={"status": "success", "data": [{"__name__": "up", "pod": "p1"}]})
        return httpx2.Response(200, json={"status": "success", "data": ["ns1", "ns2"]})

    cfg = PromConfig(base_url="http://prom.test:9090", max_series=5, max_label_values=2)
    real = PrometheusClient(cfg, transport=httpx2.MockTransport(handler))
    c = TestClient(Starlette(routes=webui.build_ui_routes(real, cfg)))

    series = c.get("/api/series", params={"match": "up"}).json()
    assert series["n_total"] == 1 and series["result"][0]["pod"] == "p1"

    labels = c.get("/api/label_values", params={"label": "namespace"}).json()
    assert labels["values"] == ["ns1", "ns2"] and labels["n_total"] == 2


# ---------------------------------------------------------------------------
# alerts / rules
# ---------------------------------------------------------------------------


def test_alerts_shaped_sorted_counted():
    c = make_client()
    data = c.get("/api/alerts").json()
    assert data["n_total"] == 2
    assert data["counts"] == {"firing": 1, "pending": 1, "inactive": 0}
    first = data["alerts"][0]
    assert first["state"] == "firing"  # firing sorts first
    assert first["alertname"] == "PodCrashLooping"
    assert first["severity"] == "critical"
    assert first["value"] == "3100"  # formatted 4-sig
    assert "alertname" not in first["labels"] and first["labels"]["pod"] == "x"


def test_rules_filters_by_state_and_search():
    c = make_client()
    data = c.get("/api/rules", params={"state": "firing"}).json()
    names = [r["name"] for g in data["groups"] for r in g["rules"]]
    assert names == ["PodCrashLooping"]
    data = c.get("/api/rules", params={"search": "memory"}).json()
    names = [r["name"] for g in data["groups"] for r in g["rules"]]
    assert names == ["node:mem:ratio"]
    assert c.get("/api/rules", params={"state": "bogus"}).status_code == 400


# ---------------------------------------------------------------------------
# dashboard overview: fail-soft aggregation
# ---------------------------------------------------------------------------


def test_overview_aggregates_all_blocks():
    c = make_client()
    data = c.get("/api/overview").json()
    assert set(data["cards"]) == {name for name, _ in webui._OVERVIEW_CARDS}
    assert data["cards"]["up_targets"]["value"] == "0"
    assert data["top"]["top_cpu"]["items"][0]["label"] == "p0"
    assert data["trends"]["cpu_trend"]["series"][0]["values"]
    assert data["alerts"]["counts"]["firing"] == 1
    assert data["duration_ms"] >= 0


def test_overview_gpu_cards_and_top_workloads():
    c = make_client()
    data = c.get("/api/overview").json()
    # The stub returns raw series (it does not evaluate count()/avg()), so
    # assert structure + the exact canned query each card runs.
    # Both cards must dedupe per physical GPU and ignore impossible samples:
    # DCGM emits several series per GPU here, and this exporter occasionally
    # reports a 0-100 gauge in the billions, so raw count()/avg() both lie.
    assert data["cards"]["gpu_count"]["query"] == "count(count by (Hostname, device) (DCGM_FI_DEV_GPU_UTIL))"
    assert data["cards"]["gpu_util_pct"]["query"] == (
        "avg(avg by (Hostname, device) (DCGM_FI_DEV_GPU_UTIL and (DCGM_FI_DEV_GPU_UTIL < 101)))"
    )
    assert "100 * sum(DCGM_FI_DEV_FB_USED)" in data["cards"]["gpu_mem_pct"]["query"]
    # top_gpu_mem aggregates by (exported_namespace, exported_pod); the stub
    # has neither, so the label falls back to the bare pod name
    assert data["top"]["top_gpu_mem"]["items"][0]["label"] == "p0"
    assert "DCGM_FI_DEV_FB_USED" in data["top"]["top_gpu_mem"]["query"]


def test_overview_fails_soft_per_card():
    class PartialClient(StubClient):
        async def instant_query(self, query, ts=None):
            if "node_" in query or "kube_pod" in query or "DCGM_" in query:
                raise PrometheusError("parse error: unknown metric")
            return await StubClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    app = Starlette(routes=webui.build_ui_routes(PartialClient(), cfg))
    data = TestClient(app).get("/api/overview").json()
    assert "error" in data["cards"]["node_cpu_pct"]
    assert "error" in data["cards"]["gpu_util_pct"]  # no DCGM on this cluster
    assert data["cards"]["up_targets"]["value"] == "0"  # healthy cards still answer
    assert data["alerts"]["counts"]["firing"] == 1


# ---------------------------------------------------------------------------
# GPU (DCGM) endpoint: domain grouping, workload attribution, fail-soft
# ---------------------------------------------------------------------------

GPU_HOST = "pcai-se-scs04.hst.lab"


def _dcgm(metric, gpu_idx, value, extra=None, host=GPU_HOST):
    labels = {
        "__name__": metric,
        "Hostname": host,
        "gpu": str(gpu_idx),
        "device": f"nvidia{gpu_idx}",
        "UUID": f"GPU-test-{gpu_idx}",
        "modelName": "NVIDIA H200 NVL",
        "container": "nvidia-dcgm-exporter",
    }
    labels.update(extra or {})
    return {"metric": labels, "value": [1757337600, str(value)]}


class GpuClient(StubClient):
    """DCGM-shaped stub: one 8-GPU H200 NVL host, islands 0-3 idle / 4-7 busy."""

    async def instant_query(self, query, ts=None):
        result = []
        for i in range(8):
            busy = i >= 4
            wl = (
                {
                    "exported_namespace": "team-x",
                    "exported_pod": "infer-predictor-abc",
                    "exported_container": "kserve-container",
                }
                if busy
                else {}
            )
            spec = {
                "DCGM_FI_DEV_GPU_UTIL": 90 if busy else 0,
                "DCGM_FI_DEV_FB_USED": 100000,
                "DCGM_FI_DEV_FB_FREE": 20000,
                "DCGM_FI_DEV_GPU_TEMP": 60 + i,
                "DCGM_FI_DEV_POWER_USAGE": 300.5,
                "DCGM_FI_DEV_MEM_COPY_UTIL": 10,
                "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL": 1000 if busy else 0,
                "DCGM_FI_DEV_XID_ERRORS": 0,
            }
            if query in spec:
                result.append(_dcgm(query, i, spec[query], wl))
            else:
                return await StubClient.instant_query(self, query, ts)
        return {"resultType": "vector", "result": result}


def _gpu_client():
    cfg = PromConfig(base_url="http://prom.test:9090")
    return TestClient(Starlette(routes=webui.build_ui_routes(GpuClient(), cfg)))


def test_gpu_endpoint_assembles_domains_and_workloads():
    data = _gpu_client().get("/api/gpu").json()
    s = data["summary"]
    assert s["gpus"] == 8 and s["nodes"] == 1
    assert s["model"] == "NVIDIA H200 NVL"
    assert s["util_pct"] == 45.0  # half idle, half at 90
    assert s["mem_used_mib"] == 800000.0 and s["mem_total_mib"] == 960000.0
    assert s["mem_pct"] == 83.3
    assert s["power_w"] == 2404.0  # 8 × 300.5
    assert s["max_temp_c"] == 67.0
    # the two 4-GPU NVLink islands, per node
    assert [(d["domain"], d["gpus"]) for d in data["domains"]] == [(0, [0, 1, 2, 3]), (1, [4, 5, 6, 7])]
    busy = data["domains"][1]
    assert busy["util_pct"] == 90.0
    assert busy["nvlink_kib_s"] == 4000.0  # 4 × 1000 KiB/s
    assert busy["workloads"] == [{"namespace": "team-x", "pod": "infer-predictor-abc", "gpus": [4, 5, 6, 7]}]
    # idle island: REAL zero NVLink must survive (not collapse to null)
    assert data["domains"][0]["nvlink_kib_s"] == 0.0
    assert data["domains"][0]["workloads"] == []
    g4 = next(g for g in data["gpus"] if g["gpu"] == 4)
    assert g4["domain"] == 1 and g4["mem_pct"] == 83.3
    assert g4["namespace"] == "team-x" and g4["device"] == "nvidia4" and g4["uuid"].startswith("GPU-")
    assert data["domains_source"] == "default"
    assert set(data["queries"]) == {name for name, _ in webui._GPU_QUERIES} | {"nvlink_domain_info"}


def test_gpu_endpoint_fails_soft_without_dcgm():
    class NoGpu(StubClient):
        async def instant_query(self, query, ts=None):
            if query.startswith("DCGM_"):
                raise PrometheusError("query error: unknown metric DCGM_FI_DEV_GPU_UTIL")
            return await StubClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    c = TestClient(Starlette(routes=webui.build_ui_routes(NoGpu(), cfg)))
    data = c.get("/api/gpu").json()
    assert c.get("/api/gpu").status_code == 200  # soft, never breaks the tab
    assert "GPU metrics unavailable" in data["error"]
    assert data["domains_config"] == [[0, 1, 2, 3], [4, 5, 6, 7]]


def test_gpu_endpoint_partial_metrics_degrade_to_nulls():
    class Partial(GpuClient):
        async def instant_query(self, query, ts=None):
            if query == "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL":
                raise PrometheusError("metric missing")
            return await GpuClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(Partial(), cfg))).get("/api/gpu").json()
    assert "error" not in data  # util + fb_used still answer -> tab works
    assert data["summary"]["nvlink_kib_s"] is None  # that metric absent
    assert data["summary"]["util_pct"] == 45.0


def test_gpu_domain_env_override(monkeypatch):
    groups, source = webui._gpu_domains({"PROM_UI_GPU_NVLINK_DOMAINS": "[[0,4],[1,5],[2,6],[3,7]]"})
    assert source == "env" and groups == ((0, 4), (1, 5), (2, 6), (3, 7))
    # invalid payloads silently fall back to the default grouping
    for bad in ("not json", "[[0,1],[1,2]]", "[[0],[0]]", "[]", '[["a"]]'):
        groups, source = webui._gpu_domains({"PROM_UI_GPU_NVLINK_DOMAINS": bad})
        assert source == "default" and groups == webui._DEFAULT_GPU_DOMAINS
    assert webui._gpu_domains({}) == (webui._DEFAULT_GPU_DOMAINS, "default")

    monkeypatch.setenv("PROM_UI_GPU_NVLINK_DOMAINS", "[[0,4],[1,5],[2,6],[3,7]]")
    data = _gpu_client().get("/api/gpu").json()
    assert data["domains_source"] == "env"
    assert [d["gpus"] for d in data["domains"]] == [[0, 4], [1, 5], [2, 6], [3, 7]]


# ---------------------------------------------------------------------------
# server wiring
# ---------------------------------------------------------------------------


def test_server_mounts_ui_routes():
    from prometheus_mcp import server

    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/ui", "/api/status", "/api/overview", "/api/gpu", "/health", "/mcp"} <= paths


# ---------------------------------------------------------------------------
# GPU: auto-detected NVLink islands (nvlink-topology DaemonSet metric)
# ---------------------------------------------------------------------------


def _domain_sample(host, gpu_idx, dom):
    return {
        "metric": {
            "__name__": "nvidia_gpu_nvlink_domain",
            "hostname": host,  # k8s node name — short, no domain suffix
            "gpu": str(gpu_idx),
            "domain": str(dom),
            "peers": "n/a",
        },
        "value": [1757337600, "1"],
    }


class DetectedClient(GpuClient):
    """GpuClient fleet + detected topology with DIFFERENT shapes per host:
    scs04 = two 4-GPU islands (H200 NVL), scs05 = one 8-way NVSwitch island.
    Detected hostnames are the short node name; DCGM reports the fqdn — the
    consumer must join them via the first dot-label.
    """

    HOST2 = "pcai-se-scs05.hst.lab"
    H2: ClassVar[dict] = {
        "DCGM_FI_DEV_GPU_UTIL": 50,
        "DCGM_FI_DEV_FB_USED": 60000,
        "DCGM_FI_DEV_FB_FREE": 80000,
        "DCGM_FI_DEV_GPU_TEMP": 50,
        "DCGM_FI_DEV_POWER_USAGE": 200.0,
        "DCGM_FI_DEV_MEM_COPY_UTIL": 5,
        "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL": 0,
        "DCGM_FI_DEV_XID_ERRORS": 0,
    }

    async def instant_query(self, query, ts=None):
        if query == webui._NVLINK_DOMAIN_QUERY:
            result = [_domain_sample(GPU_HOST, i, 1 if i >= 4 else 0) for i in range(8)]
            result += [_domain_sample(self.HOST2, i, 0) for i in range(8)]
            return {"resultType": "vector", "result": result}
        data = await GpuClient.instant_query(self, query, ts)
        if query in self.H2:
            for i in range(8):
                data["result"].append(_dcgm(query, i, self.H2[query], host=self.HOST2))
        return data


def test_gpu_detected_islands_beat_default_and_are_per_host():
    cfg = PromConfig(base_url="http://prom.test:9090")
    c = TestClient(Starlette(routes=webui.build_ui_routes(DetectedClient(), cfg)))
    data = c.get("/api/gpu").json()
    assert data["domains_source"] == "detected"
    scs04 = [d for d in data["domains"] if d["hostname"] == GPU_HOST]
    scs05 = [d for d in data["domains"] if d["hostname"] == DetectedClient.HOST2]
    assert [(d["domain"], d["gpus"]) for d in scs04] == [(0, [0, 1, 2, 3]), (1, [4, 5, 6, 7])]
    # per-host shapes: scs05 is ONE island — the default map would have split it
    assert [(d["domain"], d["gpus"]) for d in scs05] == [(0, [0, 1, 2, 3, 4, 5, 6, 7])]
    assert data["domains_detected"]["pcai-se-scs04"] == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert data["domains_detected"]["pcai-se-scs05"] == [[0, 1, 2, 3, 4, 5, 6, 7]]
    # hostname join: detected short name matched against DCGM fqdn records
    g7 = next(g for g in data["gpus"] if g["hostname"] == DetectedClient.HOST2 and g["gpu"] == 7)
    assert g7["domain"] == 0
    assert data["queries"]["nvlink_domain_info"] == "nvidia_gpu_nvlink_domain"


def test_gpu_env_override_beats_detection(monkeypatch):
    monkeypatch.setenv("PROM_UI_GPU_NVLINK_DOMAINS", "[[0,1],[2,3],[4,5],[6,7]]")
    cfg = PromConfig(base_url="http://prom.test:9090")
    c = TestClient(Starlette(routes=webui.build_ui_routes(DetectedClient(), cfg)))
    data = c.get("/api/gpu").json()
    assert data["domains_source"] == "env"
    scs04 = [d["gpus"] for d in data["domains"] if d["hostname"] == GPU_HOST]
    assert scs04 == [[0, 1], [2, 3], [4, 5], [6, 7]]


def test_gpu_malformed_detection_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("PROM_UI_GPU_NVLINK_DOMAINS", raising=False)

    class Malformed(DetectedClient):
        async def instant_query(self, query, ts=None):
            if query == webui._NVLINK_DOMAIN_QUERY:
                return {"resultType": "vector", "result": [_domain_sample(GPU_HOST, "x", 0)]}
            return await DetectedClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(Malformed(), cfg))).get("/api/gpu").json()
    assert data["domains_source"] == "default"  # unusable detection -> built-in map
    assert data["domains_detected"] is None


def test_gpu_duplicate_domain_claim_skips_host():
    class Dupes(DetectedClient):
        async def instant_query(self, query, ts=None):
            if query == webui._NVLINK_DOMAIN_QUERY:
                return {
                    "resultType": "vector",
                    "result": [
                        _domain_sample(GPU_HOST, 0, 0),
                        _domain_sample(GPU_HOST, 0, 1),  # same GPU claimed twice — bad data
                        _domain_sample(GPU_HOST, 1, 0),
                    ],
                }
            return await DetectedClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(Dupes(), cfg))).get("/api/gpu").json()
    assert data["domains_source"] == "default"
    assert data["domains_detected"] is None


# ---------------------------------------------------------------------------
# Nodes (capacity vs allocation vs usage): /api/nodes
# ---------------------------------------------------------------------------

NODE_A = "pcai-se-scs04.hst.lab"  # GPU node (nvidia allocatable present)
NODE_B = "pcai-se-ez-master01.hst.lab"  # plain node


def _node_sample(name, value, name_override=None):
    return {"metric": {"__name__": "x", "node": name}, "value": [1757337600, str(value)]}


class NodesClient(StubClient):
    """kube-prometheus-stack-shaped stub: one GPU node + one plain node.

    scs04 (GPU): 344 allocatable cores / 700 GiB, requests 172c 350GiB,
    limits 688c 1400GiB, active 86c 100GiB, 330 pods.
    master01: 4 cores / 16 GiB, requests 2c 8GiB, limits 8c 32GiB,
    active 1c 1GiB, 9 pods.
    """

    async def instant_query(self, query, ts=None):
        spec = {
            'kube_node_status_allocatable{resource="cpu"}': [
                _node_sample(NODE_A, 343.92),
                _node_sample(NODE_B, 3.92),
            ],
            'kube_node_status_allocatable{resource="memory"}': [
                _node_sample(NODE_A, 700 * 2**30),
                _node_sample(NODE_B, 16 * 2**30),
            ],
            'sum by (node) (kube_pod_container_resource_requests{resource="cpu", node!=""})': [
                _node_sample(NODE_A, 172),
                _node_sample(NODE_B, 2),
            ],
            'sum by (node) (kube_pod_container_resource_requests{resource="memory", node!=""})': [
                _node_sample(NODE_A, 350 * 2**30),
                _node_sample(NODE_B, 8 * 2**30),
            ],
            'sum by (node) (kube_pod_container_resource_limits{resource="cpu", node!=""})': [
                _node_sample(NODE_A, 688),
                _node_sample(NODE_B, 8),
            ],
            'sum by (node) (kube_pod_container_resource_limits{resource="memory", node!=""})': [
                _node_sample(NODE_A, 1400 * 2**30),
                _node_sample(NODE_B, 32 * 2**30),
            ],
            'sum by (node) (rate(container_cpu_usage_seconds_total{container!="",image!=""}[5m]))': [
                _node_sample(NODE_A, 86),
                _node_sample(NODE_B, 1),
            ],
            "sum by (node) (rate(container_cpu_usage_seconds_total{container!="
            '",image!=""}[5m]) * on(instance) group_left(node) node_uname_info)': [
                _node_sample(NODE_A, 999),  # must be IGNORED when A answers
            ],
            'sum by (node) (container_memory_working_set_bytes{container!="",image!=""})': [
                _node_sample(NODE_A, 100 * 2**30),
                _node_sample(NODE_B, 2**30),
            ],
            "sum by (node) (container_memory_working_set_bytes{container!="
            '",image!=""} * on(instance) group_left(node) node_uname_info)': [
                _node_sample(NODE_A, 888),
            ],
            'count by (node) (kube_pod_info{node!=""})': [
                _node_sample(NODE_A, 330),
                _node_sample(NODE_B, 9),
            ],
        }
        if query in spec:
            return {"resultType": "vector", "result": spec[query]}
        if query == webui._NODES_FILTER_QUERY:
            return {"resultType": "vector", "result": [_node_sample(NODE_A, 8)]}
        return await StubClient.instant_query(self, query, ts)


def _nodes_client(client_cls=NodesClient):
    cfg = PromConfig(base_url="http://prom.test:9090")
    return TestClient(Starlette(routes=webui.build_ui_routes(client_cls(), cfg)))


def test_nodes_endpoint_assembles_allocation():
    data = _nodes_client().get("/api/nodes?filter=all").json()
    by_node = {n["node"]: n for n in data["nodes"]}
    # GPU node ranks first, every column joined on the node label
    top = data["nodes"][0]
    assert top["node"] == NODE_A and top["gpu_node"] is True
    assert top["alloc_cpu"] == 343.92
    assert top["req_cpu"] == 172.0 and top["lim_cpu"] == 688.0 and top["used_cpu"] == 86.0
    assert top["req_cpu_pct"] == 50.0 and top["lim_cpu_pct"] == 200.0 and top["used_cpu_pct"] == 25.0
    assert top["pods"] == 330
    assert top["alloc_mem"] == 700 * 2**30
    assert top["req_mem_pct"] == 50.0 and top["used_mem_pct"] == pytest.approx(14.3, abs=0.1)
    # the native-cAdvisor path wins: the node_uname_info join (999/888) is ignored
    plain = by_node[NODE_B]
    assert plain["node"] == NODE_B and plain["gpu_node"] is False
    assert plain["used_cpu"] == 1.0 and plain["used_mem"] == 2**30
    assert plain["lim_cpu_pct"] == pytest.approx(204.1, abs=0.1)
    # summary = sums of the SHOWN nodes, percentages recomputed on the sums
    s = data["summary"]
    assert s["nodes"] == 2 and s["gpu_nodes"] == 1
    assert s["alloc_cpu"] == pytest.approx(347.84, abs=0.01)
    assert s["used_cpu"] == 87.0
    assert s["used_cpu_pct"] == pytest.approx(25.0, abs=0.1)
    assert set(data["queries"]) == {name for name, _ in webui._NODES_QUERIES} | {"gpu_alloc"}


def test_nodes_default_filter_selects_gpu_nodes():
    data = _nodes_client().get("/api/nodes").json()
    assert [n["node"] for n in data["nodes"]] == [NODE_A]
    assert data["node_filter_source"] == "default"
    assert data["node_filter"] is None
    assert data["gpu_nodes_detected"] == [NODE_A]
    # summary covers the SHOWN nodes only (the tab's numbers add up)
    assert data["summary"]["nodes"] == 1
    assert data["summary"]["alloc_cpu"] == 343.92


def test_nodes_filter_query_param_overrides(monkeypatch):
    # ?filter regex narrows (matches by substring, case-insensitive)
    data = _nodes_client().get("/api/nodes?filter=ez-master").json()
    assert [n["node"] for n in data["nodes"]] == [NODE_B]
    assert data["node_filter_source"] == "query"
    assert data["node_filter"] == "ez-master"
    # 'all' disables the selection entirely
    data = _nodes_client().get("/api/nodes?filter=all").json()
    assert {n["node"] for n in data["nodes"]} == {NODE_A, NODE_B}
    # a filter matching nothing falls back to all detected nodes, never []
    data = _nodes_client().get("/api/nodes?filter=nomatch-xyz").json()
    assert {n["node"] for n in data["nodes"]} == {NODE_A, NODE_B}


def test_nodes_env_filter(monkeypatch):
    monkeypatch.setenv("PROM_UI_NODE_FILTER", "scs")
    data = _nodes_client().get("/api/nodes").json()
    assert [n["node"] for n in data["nodes"]] == [NODE_A]
    assert data["node_filter_source"] == "env" and data["node_filter"] == "scs"
    # the query param still wins over the env
    data = _nodes_client().get("/api/nodes?filter=ez").json()
    assert [n["node"] for n in data["nodes"]] == [NODE_B]
    # invalid regex -> silently the default (GPU nodes)
    monkeypatch.setenv("PROM_UI_NODE_FILTER", "[unclosed")
    data = _nodes_client().get("/api/nodes").json()
    assert [n["node"] for n in data["nodes"]] == [NODE_A]
    assert data["node_filter_source"] == "default"


def test_nodes_fail_soft_per_family():
    class Partial(NodesClient):
        async def instant_query(self, query, ts=None):
            if "limits" in query or query == webui._NODES_FILTER_QUERY:
                raise PrometheusError("query error: unknown metric")
            return await NodesClient.instant_query(self, query, ts)

    data = _nodes_client(Partial).get("/api/nodes").json()
    assert "error" not in data  # tab still renders
    # GPU detection failed too -> the default selection falls back to all
    # nodes, sorted alphabetically
    by_node = {n["node"]: n for n in data["nodes"]}
    top = by_node[NODE_A]
    assert top["lim_cpu"] is None and top["lim_cpu_pct"] is None  # limits column absent
    assert top["req_cpu"] == 172.0  # everything else still answers
    # GPU detection failing -> no gpu_node badges, but node list survives
    assert all(n["gpu_node"] is False for n in data["nodes"])

    # whole-API fail soft: only the pods block survives -> still a payload
    class Bare(StubClient):
        async def instant_query(self, query, ts=None):
            if query == 'count by (node) (kube_pod_info{node!=""})':
                return {"resultType": "vector", "result": [_node_sample(NODE_B, 9)]}
            raise PrometheusError("no such metric: " + query)

    data = _nodes_client(Bare).get("/api/nodes").json()
    assert data["summary"]["nodes"] == 1
    assert data["nodes"][0]["pods"] == 9
    assert data["nodes"][0]["alloc_cpu"] is None


def test_nodes_zero_allocatable_never_divides():
    class ZeroAlloc(NodesClient):
        async def instant_query(self, query, ts=None):
            if query == 'kube_node_status_allocatable{resource="cpu"}':
                return {"resultType": "vector", "result": [_node_sample(NODE_A, 0)]}
            return await NodesClient.instant_query(self, query, ts)

    data = _nodes_client(ZeroAlloc).get("/api/nodes").json()
    top = data["nodes"][0]
    assert top["alloc_cpu"] == 0.0
    assert top["req_cpu_pct"] is None and top["used_cpu_pct"] is None  # no ZeroDivisionError


def test_ui_serves_nodes_tab_and_axis_fix():
    html = webui._load_html()
    assert 'data-tab="nodes"' in html and "/api/nodes" in html
    assert "PROM_UI_NODE_FILTER" in html  # the note documents the env override
    assert 'id="axis-core"' in html and "axisTicks" in html


def test_ui_serves_paged_results_and_page_free_csv():
    html = webui._load_html()
    assert 'id="result-page-core"' in html and "pageWindow" in html
    assert "PROM_UI_PAGE_SIZE" in html  # the page-size knob is documented in the JS
    # the copy button exports through the page-free CSV endpoints
    assert "/api/query.csv" in html and "/api/query_range.csv" in html
    assert "copy CSV" in html


def test_page_window_math():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the pure-JS block cannot be executed here")
    m = re.search(r'<script id="result-page-core">(.*?)</script>', webui._load_html(), re.DOTALL)
    assert m, "result-page-core block missing from the UI"
    harness = (
        "const a = pageWindow(654, 0, 100);\n"
        "if (a.page !== 1 || a.pages !== 7 || a.first !== 1 || a.last !== 100) throw new Error('first page: ' + JSON.stringify(a));\n"
        "const b = pageWindow(654, 600, 100);\n"
        "if (b.page !== 7 || b.first !== 601 || b.last !== 654) throw new Error('last page: ' + JSON.stringify(b));\n"
        "const c = pageWindow(0, 0, 100);\n"
        "if (c.page !== 1 || c.pages !== 1 || c.first !== 0 || c.last !== 0) throw new Error('empty: ' + JSON.stringify(c));\n"
        "const d = pageWindow(30, 10, 5);\n"
        "if (d.page !== 3 || d.pages !== 6 || d.first !== 11 || d.last !== 15) throw new Error('offset: ' + JSON.stringify(d));\n"
        "const e = pageWindow(100, 0, 100);\n"
        "if (e.pages !== 1 || e.first !== 1 || e.last !== 100) throw new Error('exact page: ' + JSON.stringify(e));\n"
        "const f = pageWindow(10, -5, 0);\n"
        "if (f.offset !== 0 || f.limit !== 1 || f.pages !== 10 || f.page !== 1 || f.first !== 1 || f.last !== 1) throw new Error('defensive: ' + JSON.stringify(f));\n"
        "console.log('PAGER-ALL-OK');\n"
    )
    proc = subprocess.run([node, "-e", m.group(1) + "\n" + harness], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    assert "PAGER-ALL-OK" in proc.stdout


def test_axis_ticks_escalate_precision_for_flat_series():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the pure-JS block cannot be executed here")
    m = re.search(r'<script id="axis-core">(.*?)</script>', webui._load_html(), re.DOTALL)
    assert m, "axis-core block missing from the UI"
    js = m.group(1)
    # The exact bug from the screenshot: three gridlines of a near-flat
    # memory series all rendering "1.4 TiB".
    harness = (
        "const t = axisTicks([1.533e12, 1.538e12, 1.5399e12], true);\n"
        "if (new Set(t.labels).size !== 3) throw new Error('flat memory axis labels collide: ' + t.labels.join(' | '));\n"
        "if (!t.labels.every((l) => /TiB$/.test(l))) throw new Error('bytes axis must stay in TiB: ' + t.labels.join(' | '));\n"
        "const cpu = axisTicks([86, 87, 88], false);\n"
        "if (new Set(cpu.labels).size !== 3) throw new Error('cpu labels collide: ' + cpu.labels.join(' | '));\n"
        "if (cpu.digits < 3) throw new Error('digits escalation skipped');\n"
        "const spaced = axisTicks([0, 500e9, 1.1e12], true);\n"
        "if (spaced.labels.join('|') !== axisTicks([0, 500e9, 1.1e12], true).labels.join('|')) throw new Error('unstable');\n"
        "if (axisTicks([0, 1e6, 2e9], true).labels.length !== 3) throw new Error('length');\n"
        "const dup = axisTicks([1.5e12, 1.5e12, 1.5e12], true);\n"
        "if (new Set(dup.labels).size !== 1) throw new Error('identical values must stay identical, honestly');\n"
        "if (axisTicks([], true).labels.length !== 0) throw new Error('empty input');\n"
        "console.log('AXIS-ALL-OK');\n"
    )
    proc = subprocess.run([node, "-e", js + "\n" + harness], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    assert "AXIS-ALL-OK" in proc.stdout


# ---- /api/gpu/demand: GPU allocation vs served demand ----------------------


class DemandClient(StubClient):
    """Allocation + per-runtime counters, shaped like the live fleet.

    alpha  two revisions of one InferenceService (must roll up), vLLM
    beta   CPU-served, counter present and genuinely 0 (must stay 0, not null)
    gamma  holds a GPU, runtime exports no counter (must be null, not 0)
    nb-0   a notebook holding a GPU, no counter at all
    """

    async def instant_query(self, query, ts=None):
        if "kube_pod_container_resource_requests" in query:
            return {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"namespace": "ns-a", "pod": "alpha-predictor-00001-deployment-7d9f-x1"},
                        "value": [1, "2"],
                    },
                    {
                        "metric": {"namespace": "ns-a", "pod": "alpha-predictor-00002-deployment-5c2b-x2"},
                        "value": [1, "1"],
                    },
                    {
                        "metric": {"namespace": "ns-b", "pod": "beta-predictor-00001-deployment-88aa-y1"},
                        "value": [1, "0"],
                    },
                    {
                        "metric": {"namespace": "ns-b", "pod": "gamma-predictor-00001-deployment-44bb-z1"},
                        "value": [1, "1"],
                    },
                    {"metric": {"namespace": "ns-c", "pod": "nb-0"}, "value": [1, "1"]},
                ],
            }
        if "vllm:request_success_total" in query:
            rev1, rev2, beta = {"7d": ("100", "40", "0"), "14d": ("250", "90", "0")}["7d" if "[7d]" in query else "14d"]
            return {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"namespace": "ns-a", "pod": "alpha-predictor-00001-deployment-7d9f-x1"},
                        "value": [1, rev1],
                    },
                    {
                        "metric": {"namespace": "ns-a", "pod": "alpha-predictor-00002-deployment-5c2b-x2"},
                        "value": [1, rev2],
                    },
                    {
                        "metric": {"namespace": "ns-b", "pod": "beta-predictor-00001-deployment-88aa-y1"},
                        "value": [1, beta],
                    },
                ],
            }
        return {"resultType": "vector", "result": []}


def _demand_client():
    cfg = PromConfig(base_url="http://prom.test:9090")
    return TestClient(Starlette(routes=webui.build_ui_routes(DemandClient(), cfg)))


def test_demand_workload_of_rolls_up_revisions_and_pod_hashes():
    assert webui._workload_of("qwen-38-27b-predictor-00003-deployment-5d577df8fc-vs9rt") == "qwen-38-27b"
    assert webui._workload_of("nv-embedqa-e5-v5-predictor-00001-deployment-cfc8f5d79-74tzw") == "nv-embedqa-e5-v5"
    # both revisions of one InferenceService collapse to the same workload name
    assert (
        webui._workload_of("alpha-predictor-00001-deployment-7d9f-x1")
        == webui._workload_of("alpha-predictor-00002-deployment-5c2b-x2")
        == "alpha"
    )
    assert webui._workload_of("nb-0") == "nb-0"


def test_demand_rolls_revisions_up_and_attributes_runtime():
    data = _demand_client().get("/api/gpu/demand").json()
    by_name = {r["workload"]: r for r in data["workloads"]}
    alpha = by_name["alpha"]
    assert alpha["gpus"] == 3  # 2 + 1 across both revisions
    assert alpha["requests"] == {"7d": 140.0, "14d": 340.0}
    assert alpha["runtime"] == "vllm" and alpha["instrumented"] is True
    # ordered by GPU count, so the GPU holders come first
    assert data["workloads"][0]["workload"] == "alpha"


def test_demand_missing_counter_is_null_never_zero():
    data = _demand_client().get("/api/gpu/demand").json()
    by_name = {r["workload"]: r for r in data["workloads"]}
    # gamma holds a GPU but its runtime exports no request counter at all
    gamma = by_name["gamma"]
    assert gamma["gpus"] == 1
    assert gamma["requests"] == {"7d": None, "14d": None}
    assert gamma["runtime"] is None and gamma["instrumented"] is False
    assert by_name["nb-0"]["requests"] == {"7d": None, "14d": None}
    # beta genuinely served zero and reported it -- that zero must survive
    assert by_name["beta"]["requests"] == {"7d": 0.0, "14d": 0.0}
    assert by_name["beta"]["instrumented"] is True
    s = data["summary"]
    assert s["gpus_total"] == 5 and s["workloads"] == 4
    assert s["uninstrumented"] == 2 and s["no_requests_7d"] == 1


def test_demand_counter_query_failure_is_null_never_zero():
    class Broken(StubClient):
        async def instant_query(self, query, ts=None):
            if "request_success_total" in query:
                raise PrometheusError("query error: timeout")
            return await DemandClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(Broken(), cfg))).get("/api/gpu/demand").json()
    by_name = {r["workload"]: r for r in data["workloads"]}
    # a failed counter query means "unknown", never "served nothing"
    assert by_name["alpha"]["requests"] == {"7d": None, "14d": None}
    assert by_name["alpha"]["instrumented"] is False
    assert "vllm:7d" in data["errors"]
    assert data["summary"]["gpus_total"] == 5  # allocation still answers


def test_demand_route_registered():
    cfg = PromConfig(base_url="http://prom.test:9090")
    paths = {r.path for r in webui.build_ui_routes(DemandClient(), cfg)}
    assert "/api/gpu/demand" in paths


def test_ui_tab_selection_persists_through_refresh():
    html = webui._load_html()
    assert 'id="tab-core"' in html
    # written on switch, restored on load, followed when the address bar changes
    assert "writeTabHash" in html and "mergeTabHash" in html and "tabFromHash" in html
    assert "hashchange" in html
    # the restore paths must not echo their own write back into the fragment
    assert "updateHash: false" in html
    # running a query rewrites the fragment, so it must carry the tab along
    assert "mergeTabHash(frag" in html


def test_tab_hash_merge_preserves_deep_links():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available - the pure-JS block cannot be executed here")
    m = re.search(r'<script id="tab-core">(.*?)</script>', webui._load_html(), re.DOTALL)
    assert m, "tab-core block missing from the UI"
    harness = (
        "if (mergeTabHash('', 'gpu') !== '#tab=gpu') throw new Error('empty: ' + mergeTabHash('', 'gpu'));\n"
        "if (mergeTabHash('#', 'gpu') !== '#tab=gpu') throw new Error('bare: ' + mergeTabHash('#', 'gpu'));\n"
        "if (mergeTabHash('#tab=nodes', 'gpu') !== '#tab=gpu') throw new Error('replace: ' + mergeTabHash('#tab=nodes', 'gpu'));\n"
        "if (mergeTabHash('#q=up&range=now', 'gpu') !== '#q=up&range=now&tab=gpu') throw new Error('preserve: ' + mergeTabHash('#q=up&range=now', 'gpu'));\n"
        "const once = mergeTabHash('#q=up&range=now-6h..now', 'gpu');\n"
        "if (once !== '#q=up&range=now-6h..now&tab=gpu') throw new Error('range: ' + once);\n"
        "if (mergeTabHash(once, 'gpu') !== once) throw new Error('must be idempotent');\n"
        "if (tabFromHash('#tab=gpu') !== 'gpu') throw new Error('read');\n"
        "if (tabFromHash('#q=up&range=now&tab=nodes') !== 'nodes') throw new Error('read mixed');\n"
        "if (tabFromHash('') !== null || tabFromHash('#q=up&range=now') !== null) throw new Error('absent');\n"
        "console.log('TAB-ALL-OK');\n"
    )
    proc = subprocess.run([node, "-e", m.group(1) + "\n" + harness], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    assert "TAB-ALL-OK" in proc.stdout


def test_demand_sglang_family_survives_the_promql_braces():
    """Regression: str.format() ate the SGLang matcher's literal braces.

    The sglang template carries {exported_endpoint=~"..."}, which str.format()
    parsed as a replacement field and raised on. The per-family try/except
    swallowed it, so every SGLang workload reported "no counter" while the
    vLLM rows looked healthy -- an idle-looking panel over a busy runtime.
    """
    seen = []

    class Recorder(StubClient):
        async def instant_query(self, query, ts=None):
            seen.append(query)
            if "sglang:http_requests_total" in query:
                return {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {
                                "namespace": "ns-a",
                                "pod": "deepseek-predictor-00001-deployment-abc-1",
                            },
                            "value": [1, "14760"],
                        }
                    ],
                }
            return {"resultType": "vector", "result": []}

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(Recorder(), cfg))).get("/api/gpu/demand").json()

    sglang_queries = [q for q in seen if "sglang:http_requests_total" in q]
    assert len(sglang_queries) == 2  # one per window
    assert not [q for q in seen if "{window}" in q]  # every template substituted
    assert any("[7d]" in q and 'exported_endpoint=~"/v1/chat/completions' in q for q in sglang_queries)
    assert not [k for k in data["errors"] if k.startswith("sglang:")]
    row = {r["workload"]: r for r in data["workloads"]}["deepseek"]
    assert row["runtime"] == "sglang"
    assert row["requests"]["7d"] == 14760.0
    assert row["instrumented"] is True


def test_demand_names_vllm_omni_rather_than_mixed():
    pod = "tts-predictor-00001-deployment-aa-1"
    alloc = {"result": [{"metric": {"namespace": "ns-a", "pod": pod}, "value": [1, "1"]}]}
    counters = {
        "vllm": {("ns-a", pod): {"7d": 0.0, "14d": 0.0}},
        "vllm_omni": {("ns-a", pod): {"7d": 81.0, "14d": 81.0}},
    }
    snap = webui._demand_snapshot(alloc, counters, ("7d", "14d"))
    row = snap["workloads"][0]
    assert row["runtime"] == "vllm_omni"  # the specific runtime, not "mixed"
    assert row["requests"]["7d"] == 81.0  # both families still sum


def test_demand_drops_rows_with_neither_gpu_nor_counter():
    alloc = {
        "result": [
            {"metric": {"namespace": "ns-a", "pod": "comfyui-abc-1"}, "value": [1, "0"]},
            {
                "metric": {"namespace": "ns-a", "pod": "real-predictor-00001-deployment-zz-9"},
                "value": [1, "1"],
            },
        ]
    }
    snap = webui._demand_snapshot(alloc, {}, ("7d", "14d"))
    assert [r["workload"] for r in snap["workloads"]] == ["real"]


def test_demand_attributes_utilization_only_where_a_gpu_is_allocated():
    gpu_pod = "alpha-predictor-00001-deployment-7d9f-x1"
    cpu_pod = "beta-predictor-00001-deployment-88aa-y1"
    alloc = {
        "result": [
            {"metric": {"namespace": "ns-a", "pod": gpu_pod}, "value": [1, "2"]},
            {"metric": {"namespace": "ns-a", "pod": cpu_pod}, "value": [1, "0"]},
        ]
    }
    # beta is CPU-served but still reports requests, so it survives the
    # "no GPU and no counter" filter -- exactly like sms-gemma live, which
    # carries a stale DCGM utilization reading despite holding zero GPUs.
    counters = {"vllm": {("ns-a", cpu_pod): {"7d": 202.0, "14d": 202.0}}}
    util = {("ns-a", gpu_pod): 41.5, ("ns-a", cpu_pod): 90.0}
    snap = webui._demand_snapshot(alloc, counters, ("7d", "14d"), util=util)
    rows = {r["workload"]: r for r in snap["workloads"]}
    assert rows["alpha"]["util_7d"] == 41.5
    # DCGM's exported_pod attribution goes stale; a 0-GPU pod must not
    # inherit a utilization reading it cannot have.
    assert rows["beta"]["util_7d"] is None


def test_demand_utilization_averages_a_workloads_gpus():
    pod_a = "twin-predictor-00001-deployment-aa-1"
    pod_b = "twin-predictor-00001-deployment-bb-2"
    alloc = {
        "result": [
            {"metric": {"namespace": "ns-a", "pod": pod_a}, "value": [1, "2"]},
            {"metric": {"namespace": "ns-a", "pod": pod_b}, "value": [1, "2"]},
        ]
    }
    util = {("ns-a", pod_a): 10.0, ("ns-a", pod_b): 50.0}
    snap = webui._demand_snapshot(alloc, {}, ("7d", "14d"), util=util)
    row = snap["workloads"][0]
    assert row["workload"] == "twin" and row["gpus"] == 4
    assert row["util_7d"] == 30.0  # mean across its two pods


def test_ui_demand_table_shows_utilization():
    html = webui._load_html()
    assert 'label: "util 7d"' in html
    assert "util_7d" in html


def test_ui_sidebar_is_collapsible_and_remembers_state():
    html = webui._load_html()
    assert 'id="sidebar-toggle"' in html
    assert 'aria-controls="sidebar"' in html and "aria-expanded" in html
    assert 'id="sidebar"' in html
    # the preference is persisted the same way the theme is
    assert "prometheus-sidebar-collapsed" in html
    assert "Ctrl+B" in html
    # collapsed must leave the tab order, not merely leave the viewport
    assert "visibility: hidden" in html
