"""Unit tests for the Prometheus MCP web UI + JSON API (no network).

The UI routes are built with a dependency-injected client/config, so tests
pass a stub client (same pattern as the MCP wire tests) and drive the
Starlette app with TestClient.
"""

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
    for tab in ("Dashboard", "Query", "Alerts", "MCP Tools"):
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


def test_overview_fails_soft_per_card():
    class PartialClient(StubClient):
        async def instant_query(self, query, ts=None):
            if "node_" in query or "kube_pod" in query:
                raise PrometheusError("parse error: unknown metric")
            return await StubClient.instant_query(self, query, ts)

    cfg = PromConfig(base_url="http://prom.test:9090")
    app = Starlette(routes=webui.build_ui_routes(PartialClient(), cfg))
    data = TestClient(app).get("/api/overview").json()
    assert "error" in data["cards"]["node_cpu_pct"]
    assert data["cards"]["up_targets"]["value"] == "0"  # healthy cards still answer
    assert data["alerts"]["counts"]["firing"] == 1


# ---------------------------------------------------------------------------
# server wiring
# ---------------------------------------------------------------------------


def test_server_mounts_ui_routes():
    import server

    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/ui", "/api/status", "/api/overview", "/health", "/mcp"} <= paths
