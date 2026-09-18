"""Tests for the workbench /metrics endpoint (Wave-3 C3 — additive, default OFF).

The contract being pinned:

* DEFAULT OFF: with WORKBENCH_METRICS_ENABLED unset (the chart default —
  metrics.enabled: false renders no env at all), _build_http_app contains NO
  /metrics route and a request to it 404s. The counters still increment
  internally (instrumenting is inert), but nothing exposes them.
* ON: the route serves the Prometheus text exposition and the per-tool
  counters increment as MCP tool calls flow through mcp.call_tool — both the
  ok and the error outcome.
* /metrics is key-free when enabled (ServiceMonitor-scrapeable, non-sensitive
  counters) while the rest of the API stays key-gated.
* Wave-6 G1 pilot (D16): the implementation is the shared
  mcp_fleet_common.metrics, bound in server.py — the module surface is the
  same, and unregistered tool names normalize to the "unknown" label (the
  B3-queued fix; see test_unknown_tool_names_normalize_to_unknown).

Backend-agnostic: this suite runs under the fleet unit-test venv (.venv312 —
no prometheus_client → the dependency-free fallback) AND under envs that have
prometheus_client. Assertions go through the rendered exposition, never
through either backend's internal objects.
"""

import asyncio
import re
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError  # precise B017 target
from starlette.testclient import TestClient

import server
from server import mcp_metrics  # the shared-package binding (Wave-6 G1)


def build_app(monkeypatch, tmp_path, *, enabled):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    if enabled:
        monkeypatch.setenv("WORKBENCH_METRICS_ENABLED", "1")
    else:
        monkeypatch.delenv("WORKBENCH_METRICS_ENABLED", raising=False)
    return server._build_http_app()


@contextmanager
def running(monkeypatch, tmp_path, *, enabled):
    """Built app + open TestClient + a metrics-text helper in one seam."""
    app = build_app(monkeypatch, tmp_path, enabled=enabled)
    with TestClient(app) as client:

        def metrics_text() -> str:
            r = client.get("/metrics")
            assert r.status_code == 200
            return r.text

        def call_tool(name, arguments):
            return asyncio.run(server.mcp.call_tool(name, arguments))

        yield metrics_text, call_tool


def metric_value(body: str, name: str, **labels):
    """Value of one labeled sample in the rendered exposition — parses either
    backend's output (label order differs between them; parse, don't grep)."""
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


def test_default_off_no_metrics_route(monkeypatch, tmp_path):
    app = build_app(monkeypatch, tmp_path, enabled=False)
    assert all(getattr(r, "path", None) != "/metrics" for r in app.routes)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404


def test_enabled_adds_metrics_route(monkeypatch, tmp_path):
    with running(monkeypatch, tmp_path, enabled=True) as (metrics_text, _):
        body = metrics_text()
        assert "# HELP" in body
        assert f"# TYPE {mcp_metrics.TOOL_REQUESTS} counter" in body


# ---------------------------------------------------------------------------
# counters increment (on → tool calls counted; ok AND error outcomes)
# ---------------------------------------------------------------------------


def test_counters_increment_ok_and_error(monkeypatch, tmp_path):
    with running(monkeypatch, tmp_path, enabled=True) as (metrics_text, call_tool):
        call_tool("workspace_create", {"name": "metrics-demo"})
        call_tool("workspace_list", {})
        with pytest.raises(ToolError):  # duplicate → tool error outcome
            call_tool("workspace_create", {"name": "metrics-demo"})
        body = metrics_text()
        name = mcp_metrics.TOOL_REQUESTS
        assert metric_value(body, name, tool="workspace_create", outcome="ok") == 1.0
        assert metric_value(body, name, tool="workspace_create", outcome="error") == 1.0
        assert metric_value(body, name, tool="workspace_list", outcome="ok") == 1.0


def test_unknown_tool_counts_as_error(monkeypatch, tmp_path):
    with running(monkeypatch, tmp_path, enabled=True) as (metrics_text, call_tool):
        with pytest.raises(ToolError):
            call_tool("no_such_tool", {})
        body = metrics_text()
        # Wave-6 G1 delta (the B3-queued fix): unregistered tool names do not
        # become label values — the error counts under the bounded "unknown"
        # label (see test_unknown_tool_names_normalize_to_unknown).
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="unknown", outcome="error") == 1.0


def test_unknown_tool_names_normalize_to_unknown(monkeypatch, tmp_path):
    """The Wave-6 G1 normalization delta, pinned end-to-end through the real
    app: many distinct unknown names → ONE bounded "unknown" series (bounded
    cardinality), and no probed name ever leaks into the exposition. The
    counter is process-wide, so the assertion is a DELTA over whatever the
    earlier tests already counted."""
    with running(monkeypatch, tmp_path, enabled=True) as (metrics_text, call_tool):
        before = metric_value(metrics_text(), mcp_metrics.TOOL_REQUESTS, tool="unknown", outcome="error") or 0.0
        for probed in ("no_such_tool", "no_such_tool_2", "Workspace_Create"):
            with pytest.raises(ToolError):
                call_tool(probed, {})
        body = metrics_text()
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="unknown", outcome="error") == before + 3.0
        assert "no_such_tool" not in body
        assert "Workspace_Create" not in body


def test_counters_export_no_sensitive_fields(monkeypatch, tmp_path):
    with running(monkeypatch, tmp_path, enabled=True) as (metrics_text, call_tool):
        call_tool("workspace_create", {"name": "secret-ws-name"})
        body = metrics_text()
        assert "secret-ws-name" not in body
        assert "argv" not in body


# ---------------------------------------------------------------------------
# auth posture when enabled
# ---------------------------------------------------------------------------


def test_metrics_key_free_while_api_stays_gated(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKBENCH_API_KEYS", "unused-by-metrics")
    with running(monkeypatch, tmp_path, enabled=True) as (metrics_text, _):
        metrics_text()  # 200 without a key — scrapeable like the probes
        app = build_app(monkeypatch, tmp_path, enabled=True)
        with TestClient(app) as client:
            assert client.get("/api/ws/nope").status_code == 401  # rest stays gated


def test_default_off_keeps_middleware_unchanged(monkeypatch, tmp_path):
    # metrics OFF → public_paths stays the middleware default (probes only):
    # with a key configured, /metrics is as protected as everything else —
    # and there is no route behind it anyway (404 either way).
    monkeypatch.setenv("WORKBENCH_API_KEYS", "k")
    app = build_app(monkeypatch, tmp_path, enabled=False)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code in (401, 404)


# ---------------------------------------------------------------------------
# fallback backend unit checks (run wherever prometheus_client is absent)
# ---------------------------------------------------------------------------


def test_fallback_backend_renders_prom_text_format():
    if mcp_metrics._HAVE_PROMETHEUS_CLIENT:
        pytest.skip("prometheus_client installed — fallback not active here")
    from mcp_fleet_common.metrics import _MiniCounter  # the shared backend seam

    c = _MiniCounter("demo_total", "demo help", ("tool", "outcome"))
    c.inc(("a", "ok"))
    c.inc(("a", "ok"))
    c.inc(("b", "error"))
    text = c.render()
    assert "# HELP demo_total demo help" in text
    assert "# TYPE demo_total counter" in text
    assert 'demo_total{tool="a",outcome="ok"} 2.0' in text
    assert 'demo_total{tool="b",outcome="error"} 1.0' in text
