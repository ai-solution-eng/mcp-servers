"""Unit tests for the Prometheus MCP web UI + JSON API (no network).

The UI routes are built with a dependency-injected client/config, so tests
pass a stub client (same pattern as the MCP wire tests) and drive the
Starlette app with TestClient.
"""

from typing import ClassVar

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
                {"metric": {"__name__": "up", "pod": f"p{i}"}, "value": [1757337600, str(i)]}
                for i in range(30)
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
    assert data["result"][0]["series"] == 'up{pod=p0}'
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
    data = c.post("/api/query_range", json={"query": "m", "start": "1757337600", "end": "1757341200", "step": "1s"}).json()
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
                return {"resultType": "vector", "result": [
                    _domain_sample(GPU_HOST, 0, 0),
                    _domain_sample(GPU_HOST, 0, 1),  # same GPU claimed twice — bad data
                    _domain_sample(GPU_HOST, 1, 0),
                ]}
            return await DetectedClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    data = TestClient(Starlette(routes=webui.build_ui_routes(Dupes(), cfg))).get("/api/gpu").json()
    assert data["domains_source"] == "default"
    assert data["domains_detected"] is None
