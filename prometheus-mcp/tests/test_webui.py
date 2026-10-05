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

import webui
from prom_client import PromConfig, PrometheusError


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


# ---------------------------------------------------------------------------
# query endpoints (caps, validation, error mapping)
# ---------------------------------------------------------------------------


def test_query_caps_series_and_formats():
    c = make_client()
    data = c.post("/api/query", json={"query": "up"}).json()
    assert data["resultType"] == "vector"
    assert data["n_total"] == 30 and data["n_shown"] == 5  # capped by config
    assert data["result"][0]["series"] == "up{pod=p0}"
    assert data["result"][0]["value"] == "0"
    assert "duration_ms" in data


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
    assert data["cards"]["gpu_count"]["query"] == "count(DCGM_FI_DEV_GPU_UTIL)"
    assert data["cards"]["gpu_util_pct"]["query"] == "avg(DCGM_FI_DEV_GPU_UTIL)"
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
    import server

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
