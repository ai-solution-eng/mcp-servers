"""Tests for the logsearch /metrics endpoint (Wave-3 C3 — additive, default OFF).

The contract being pinned:

* DEFAULT OFF: with LOGSEARCH_METRICS_ENABLED unset (the chart default —
  metrics.enabled: false renders no env at all), _build_http_app contains NO
  /metrics route. Nothing else about the app changes.
* ON: the route serves the Prometheus text exposition and the per-tool
  counters increment as MCP tool calls flow through mcp.call_tool — including
  the error outcome (this server reports failures as "Error: ..." strings).

Driven the same way as tests/test_webui.py: the fleet unit-test venv has no
httpx, so instead of starlette's TestClient the Route endpoints are invoked
directly exactly as the router would. Backend-agnostic: assertions go through
the rendered exposition, never through either metrics backend's internals.
"""

import asyncio
import re
from contextlib import contextmanager

import pytest

import mcp_metrics
import server


class FakeRequest:
    """Just enough of starlette.Request for the endpoint (matches test_webui)."""

    def __init__(self, params=None, body=None):
        self.query_params = params or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


def route_by_path(routes, path):
    return next(r for r in routes if getattr(r, "path", None) == path)


def call_route(routes, path, request=None):
    return asyncio.run(route_by_path(routes, path).endpoint(request or FakeRequest()))


@contextmanager
def running(monkeypatch, *, enabled):
    for var in (
        server.ENV_ALLOWED,
        server.ENV_BLOCKED,
        server.ENV_EMPTY_ALLOWS_ALL,
        "LOGSEARCH_MAX_PODS",
        "LOGSEARCH_MAX_LINES_PER_POD",
        "LOGSEARCH_MAX_TOTAL_LINES",
        "LOGSEARCH_WEBUI_ENABLED",
        "LOGSEARCH_METRICS_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(server.ENV_EMPTY_ALLOWS_ALL, "1")  # policy off-topic here
    if enabled:
        monkeypatch.setenv("LOGSEARCH_METRICS_ENABLED", "1")
    app = server._build_http_app()
    routes = app.routes

    def metrics_text() -> str:
        response = call_route(routes, "/metrics")
        assert response.media_type.startswith("text/plain")
        return response.body.decode("utf-8")

    def call_tool(name, arguments):
        return asyncio.run(server.mcp.call_tool(name, arguments))

    yield routes, metrics_text, call_tool


def metric_value(body: str, name: str, **labels):
    """Value of one labeled sample — parses either backend's exposition."""
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        found = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if all(found.get(k) == v for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


# ---------------------------------------------------------------------------
# gated presence
# ---------------------------------------------------------------------------


def test_default_off_no_metrics_route(monkeypatch):
    with running(monkeypatch, enabled=False) as (routes, _, _), pytest.raises(StopIteration):
        route_by_path(routes, "/metrics")


def test_enabled_adds_metrics_route(monkeypatch):
    with running(monkeypatch, enabled=True) as (_, metrics_text, _):
        body = metrics_text()
        assert f"# TYPE {mcp_metrics.TOOL_REQUESTS} counter" in body


# ---------------------------------------------------------------------------
# counters increment (ok AND error outcomes, via the real MCP dispatch)
# ---------------------------------------------------------------------------


def test_counters_increment_ok_and_error(monkeypatch):
    with running(monkeypatch, enabled=True) as (_, metrics_text, _):
        monkeypatch.setattr(server, "_list_pods", lambda ns, sel="": [])
        # ok: a successful discovery call through the MCP layer
        asyncio.run(server.mcp.call_tool("list_log_sources", {"namespace": "ns"}))
        # error: reported as an "Error: ..." string by the tool body
        asyncio.run(server.mcp.call_tool("get_pod_logs", {"namespace": "ns", "pod": "p", "tail_lines": 0}))
        body = metrics_text()
        name = mcp_metrics.TOOL_REQUESTS
        assert metric_value(body, name, tool="list_log_sources", outcome="ok") == 1.0
        assert metric_value(body, name, tool="get_pod_logs", outcome="error") == 1.0


def test_policy_denial_counts_as_error_outcome(monkeypatch):
    with running(monkeypatch, enabled=True) as (_, metrics_text, _):
        monkeypatch.delenv(server.ENV_ALLOWED, raising=False)
        monkeypatch.delenv(server.ENV_EMPTY_ALLOWS_ALL, raising=False)
        asyncio.run(server.mcp.call_tool("list_log_sources", {"namespace": "anything"}))
        body = metrics_text()
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="list_log_sources", outcome="error") == 1.0


def test_counters_export_no_sensitive_fields(monkeypatch):
    with running(monkeypatch, enabled=True) as (_, metrics_text, _):
        monkeypatch.setattr(server, "_list_pods", lambda ns, sel="": [])
        asyncio.run(server.mcp.call_tool("list_log_sources", {"namespace": "team-a"}))
        body = metrics_text()
        assert "team-a" not in body  # namespace names never exported


# ---------------------------------------------------------------------------
# fallback backend unit checks (run wherever prometheus_client is absent)
# ---------------------------------------------------------------------------


def test_fallback_backend_renders_prom_text_format():
    if mcp_metrics._HAVE_PROMETHEUS_CLIENT:
        pytest.skip("prometheus_client installed — fallback not active here")
    from mcp_metrics import _MiniCounter

    c = _MiniCounter("demo_total", "demo help", ("tool", "outcome"))
    c.inc(("a", "ok"))
    c.inc(("a", "ok"))
    text = c.render()
    assert 'demo_total{tool="a",outcome="ok"} 2.0' in text
