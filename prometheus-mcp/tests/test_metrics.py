"""Tests for the prometheus-mcp /metrics endpoint (Wave-3 C3 — additive,
default OFF): the server's OWN self-metrics.

The contract being pinned:

* DEFAULT OFF: with PROMETHEUS_METRICS_ENABLED unset (the chart default —
  metrics.enabled: false renders no env at all), _build_http_app contains NO
  /metrics route.
* ON: the route serves the Prometheus text exposition and the per-tool
  counters increment as MCP tool calls flow through mcp.call_tool — including
  the error outcome (this server reports failures as "Error: ..." strings).

Test env note: this suite runs under the conda fleet env (it needs httpx2),
where prometheus_client IS importable — but the assertions are
backend-agnostic (rendered exposition only), so they hold under the
dependency-free fallback too.
"""

import asyncio
import re
from contextlib import contextmanager

from starlette.testclient import TestClient

import mcp_metrics
import server


@contextmanager
def running(monkeypatch, *, enabled):
    if enabled:
        monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "1")
    else:
        monkeypatch.delenv("PROMETHEUS_METRICS_ENABLED", raising=False)
    app = server._build_http_app()
    with TestClient(app) as client:

        def metrics_text() -> str:
            r = client.get("/metrics")
            assert r.status_code == 200
            return r.text

        def call_tool(name, arguments):
            return asyncio.run(server.mcp.call_tool(name, arguments))

        yield metrics_text, call_tool


def metric_value(body: str, name: str, **labels):
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        found = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if all(found.get(k) == v for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


class _OfflineClient:
    """Replaces server.client so tool calls need no live Prometheus."""

    async def rules(self):
        return []  # prom_rules renders "No rules match the given filters." → ok


def test_default_off_no_metrics_route(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_METRICS_ENABLED", raising=False)
    app = server._build_http_app()
    assert all(getattr(r, "path", None) != "/metrics" for r in app.routes)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404


def test_enabled_serves_exposition(monkeypatch):
    with running(monkeypatch, enabled=True) as (metrics_text, _):
        body = metrics_text()
        assert f"# TYPE {mcp_metrics.TOOL_REQUESTS} counter" in body


def test_counters_increment_ok_outcome(monkeypatch):
    with running(monkeypatch, enabled=True) as (metrics_text, call_tool):
        real_client = server.client
        server.client = _OfflineClient()
        try:
            call_tool("prom_rules", {})  # offline stub → ok outcome
        finally:
            server.client = real_client
        body = metrics_text()
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="prom_rules", outcome="ok") == 1.0


def test_counters_increment_error_outcome(monkeypatch):
    # A failing Prometheus client → prom_rules reports its failure as an
    # "Error: ..." string → outcome="error" (self-metrics see the failure
    # mix). Stubbed client: offline-deterministic, no live connect timeout.
    class _RaisingClient:
        async def rules(self):
            raise server.PrometheusError("connection refused (stub)")

    with running(monkeypatch, enabled=True) as (metrics_text, call_tool):
        real_client = server.client
        server.client = _RaisingClient()
        try:
            call_tool("prom_rules", {})
        finally:
            server.client = real_client
        body = metrics_text()
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="prom_rules", outcome="error") == 1.0


def test_counters_export_no_promql_text(monkeypatch):
    with running(monkeypatch, enabled=True) as (metrics_text, _):
        body = metrics_text()
        assert "up{" not in body and "rate(" not in body  # no upstream series leak
