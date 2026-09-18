"""/metrics gate tests — additive, OFF by default (values.metrics.enabled).

The default render and route table must stay byte-identical to the Wave-0
baseline, so /metrics exists ONLY when APPLYGATE_METRICS_ENABLED is on. The
prometheus-client import is guarded: where the lib is absent (the fleet
unit-test venv) the route serves an honest explanatory fallback and the
tool-call counter is a no-op — metrics can never gate or break the write
path. Labels carry tool/outcome only.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_metrics.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from starlette.testclient import TestClient

import server

try:
    import prometheus_client  # noqa: F401

    HAS_PROM_CLIENT = True
except ImportError:
    HAS_PROM_CLIENT = False


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.delenv(server.METRICS_ENV, raising=False)
    return server._build_http_app()


def test_metrics_route_absent_by_default(app):
    """Default (env unset): NO /metrics route — the route table matches the
    pre-metrics baseline exactly."""
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/metrics" not in paths
    assert {"/mcp", "/health", "/healthz"} <= paths


def test_metrics_flag_parsing():
    assert server.metrics_enabled({}) is False
    for off in ("", "0", "false", "no", "off"):
        assert server.metrics_enabled({server.METRICS_ENV: off}) is False, off
    for on in ("1", "true", "TRUE", "yes", "on", "enabled"):
        assert server.metrics_enabled({server.METRICS_ENV: on}) is True, on


def test_metrics_route_served_when_enabled(monkeypatch):
    monkeypatch.setenv(server.METRICS_ENV, "true")
    # Arrange: the app counter registers lazily on first audit; materialize it
    # here so this test does not depend on other test files having audited.
    assert server._metric_tool_calls() is not None
    app = server._build_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/metrics" in paths
    # NOTE: each TestClient gets a FRESH app — the mcp session manager
    # forbids two lifecycles on one instance.
    with TestClient(server._build_http_app()) as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        if HAS_PROM_CLIENT:
            assert "text/plain" in r.headers["content-type"]
            assert "applygate_tool_calls_total" in r.text
        else:
            # Import-guarded fallback: honest explanation, never a crash.
            assert "prometheus-client is not installed" in r.text
    # /healthz keeps answering next to it.
    with TestClient(server._build_http_app()) as c:
        assert c.get("/healthz").json()["status"] == "ok"


def test_tool_call_counter_increments_on_audit(monkeypatch, tmp_path):
    """The counter rides the audit choke point — best-effort, never gating."""
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    if not HAS_PROM_CLIENT:
        pytest.skip("prometheus-client not installed in this env (fallback path covered above)")
    counter = server._metric_tool_calls()
    assert counter is not None
    from prometheus_client import REGISTRY

    def _dry_run_total() -> float:
        val = REGISTRY.get_sample_value("applygate_tool_calls_total", {"tool": "plan_apply", "outcome": "dry-run"})
        return val or 0.0

    # Delta, not absolute: earlier test files audit plan_apply/dry-run too, so
    # the global registry sample may already be non-zero when this test runs.
    before = _dry_run_total()
    server._audit("plan_apply", "team-a", "ConfigMap", "x", True, "dry-run")
    server._audit("plan_apply", "team-a", "ConfigMap", "x", True, "refused")
    assert _dry_run_total() == before + 1.0


def test_metric_labels_carry_no_resource_identity(monkeypatch, tmp_path):
    """Labels are (tool, outcome) ONLY — no namespace/name/manifest material
    ever reaches a metric label."""
    if not HAS_PROM_CLIENT:
        pytest.skip("prometheus-client not installed in this env")
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    server._audit("delete_resource", "team-a", "ConfigMap", "super-secret-name", False, "deleted")
    c = server._metric_tool_calls()
    assert c is not None
    assert set(c._labelnames) == {"tool", "outcome"}
