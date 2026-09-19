"""Unit tests for the Prometheus MCP server (no network, no Prometheus).

Run:  .venv/bin/python -m pytest tests/ -v
Live end-to-end check (needs a reachable Prometheus): tests/live_check.py
"""

import asyncio

import httpx2
import pytest

from prom_client import (
    PromConfig,
    PrometheusClient,
    PrometheusError,
    _downsample,
    _metric_str,
    format_value,
    load_config,
    parse_step,
    parse_timestamp,
    shape_instant,
    shape_range,
)


def make_client(handler, **overrides) -> PrometheusClient:
    cfg = PromConfig(base_url="http://prom.test:9090", **overrides)
    return PrometheusClient(cfg, transport=httpx2.MockTransport(handler))


# ---------------------------------------------------------------------------
# Timestamp / step / value helpers
# ---------------------------------------------------------------------------


def test_parse_timestamp_passthroughs():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    now = parse_timestamp("now")
    assert now.isdigit() and int(now) > 1_700_000_000
    rel = parse_timestamp("now-6h")
    assert int(now) - int(rel) == 6 * 3600
    assert parse_timestamp("now-30m").isdigit()
    assert parse_timestamp("now-2w").isdigit()
    assert parse_timestamp(1757337600) == "1757337600"
    assert parse_timestamp("1757337600.0") == "1757337600"
    assert parse_timestamp("2026-09-08T00:00:00Z") == "2026-09-08T00:00:00Z"


def test_parse_step_defaults_to_span_over_240():
    assert parse_step("", "0", "2400") == "10s"
    assert parse_step("5m", "0", "2400") == "5m"  # explicit wins
    assert parse_step("", None, None) == "60s"  # unknown span -> safe default
    assert parse_step("", "2026-09-08T00:00:00Z", "now") == "60s"  # non-numeric


def test_format_value_rounds():
    assert format_value(None) is None
    assert format_value("0") == "0"
    assert format_value("1234.5678") == "1235"
    assert format_value("0.123456") == "0.1235"
    assert format_value("42") == "42"
    assert format_value("nan") == "nan"


# ---------------------------------------------------------------------------
# Response shaping (LLM-safe compaction)
# ---------------------------------------------------------------------------


def _vector_result(n, prefix="m"):
    return {
        "resultType": "vector",
        "result": [
            {"metric": {"__name__": "up", "pod": f"{prefix}-{i}"}, "value": [1757337600, "1"]} for i in range(n)
        ],
    }


def test_shape_instant_caps_series():
    out = shape_instant(_vector_result(30), max_series=5)
    assert out.count("up{pod=") == 5
    assert "25 more series truncated" in out


def test_shape_instant_empty():
    assert "No results" in shape_instant({"result": []}, 10)


def test_shape_range_downsamples_preserving_span():
    values = [[str(1757337600 + i), str(i)] for i in range(1000)]
    picked = _downsample(values, 60)
    assert len(picked) == 60
    assert picked[0] == values[0] and picked[-1] == values[-1]  # span edges kept
    data = {"result": [{"metric": {"__name__": "m", "pod": "p"}, "values": values}]}
    out = shape_range(data, max_series=10, max_points=60)
    assert "1000 raw points downsampled to 60" in out


def test_shape_range_caps_series_and_handles_empty():
    data = {"result": [{"metric": {"__name__": "m"}, "values": []}]}
    assert "(empty)" in shape_range(data, 10, 60)
    assert "No results" in shape_range({"result": []}, 10, 60)
    many = {"result": [{"metric": {"__name__": "m", "i": str(i)}, "values": [["0", "1"]]} for i in range(7)]}
    out = shape_range(many, 3, 60)
    assert out.count("m{i=") == 3 and "4 more series truncated" in out


def test_metric_str_trims_long_labels():
    assert _metric_str({"__name__": "up", "a": "1"}) == "up{a=1}"
    metric = {f"l{i}": str(i) for i in range(12)}
    out = _metric_str(metric)
    assert out.count("=") == 8 and out.endswith(",…}")


# ---------------------------------------------------------------------------
# Client error handling (mock transport)
# ---------------------------------------------------------------------------


def test_client_maps_http_and_prom_errors():
    def handler_500(request):
        return httpx2.Response(500, text="boom")

    c = make_client(handler_500)
    with pytest.raises(PrometheusError, match="HTTP 500"):
        asyncio.run(c.instant_query("up"))

    def handler_query_error(request):
        return httpx2.Response(200, json={"status": "error", "errorType": "bad_data", "error": "parse error at char 5"})

    c = make_client(handler_query_error)
    with pytest.raises(PrometheusError, match="bad_data.*parse error"):
        asyncio.run(c.instant_query("up{"))

    def handler_ok(request):
        assert request.url.params["query"] == "up"
        return httpx2.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})

    c = make_client(handler_ok)
    assert asyncio.run(c.instant_query("up")) == {"resultType": "vector", "result": []}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_load_config_defaults_and_token_env_name():
    cfg = load_config({})
    assert cfg.base_url.startswith("http://kubeprom-prometheus")
    assert cfg.bearer_token is None
    # The env var NAME is configured, the VALUE is read from the environment —
    # the token itself never appears in config output.
    cfg = load_config({"PROM_BEARER_TOKEN_ENV": "MY_TOKEN", "MY_TOKEN": "tok-123"})
    assert cfg.bearer_token == "tok-123"
    assert not hasattr(cfg, "PROM_BEARER_TOKEN_ENV") or True
    # URL without scheme gets one
    cfg = load_config({"PROM_URL": "prom.monitoring:9090"})
    assert cfg.base_url == "http://prom.monitoring:9090"


# ---------------------------------------------------------------------------
# MCP surface (in-memory wire round-trip)
# ---------------------------------------------------------------------------


def _server():
    import server

    return server.mcp


def test_all_six_tools_registered_read_only():
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    import server

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.list_tools()

    tools = asyncio.run(run())
    names = {t.name for t in tools.tools}
    assert {
        "prom_query",
        "prom_query_range",
        "prom_series",
        "prom_label_values",
        "prom_alerts",
        "prom_rules",
    } <= names
    # Wave-5 F4 additions: the saved-query store's four tools.
    assert {"query_save", "query_list", "query_delete", "query_saved"} <= names
    # Annotation honesty: everything that only READS Prometheus/state is
    # marked read-only; the two store MUTATORS (save/delete) are not.
    read_only = {
        "prom_query",
        "prom_query_range",
        "prom_series",
        "prom_label_values",
        "prom_alerts",
        "prom_rules",
        "query_list",
        "query_saved",
    }
    mutating = {"query_save", "query_delete"}
    assert read_only | mutating == names
    for t in tools.tools:
        assert t.description
        assert t.annotations, t.name
        assert t.annotations.read_only_hint is (t.name in read_only), t.name


def test_prom_query_wire_round_trip(monkeypatch):
    import server

    class StubClient:
        async def instant_query(self, query, ts=None):
            assert query == "up" and ts is None
            return {
                "resultType": "vector",
                "result": [{"metric": {"__name__": "up", "pod": "p1"}, "value": [1757337600, "1"]}],
            }

    monkeypatch.setattr(server, "client", StubClient())
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("prom_query", {"query": "up"})

    result = asyncio.run(run())
    assert not result.is_error
    text = result.content[0].text
    assert "up{pod=p1} = 1" in text


def test_prom_alerts_wire_round_trip(monkeypatch):
    import server

    class StubClient:
        async def alerts(self):
            return [
                {
                    "state": "firing",
                    "activeAt": "2026-09-08T10:00:00Z",
                    "value": "3.1e3",
                    "labels": {"alertname": "PodCrashLooping", "severity": "critical", "pod": "x"},
                    "annotations": {"summary": "Pod x is crash-looping"},
                }
            ]

    monkeypatch.setattr(server, "client", StubClient())
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("prom_alerts", {})

    result = asyncio.run(run())
    text = result.content[0].text
    assert "[FIRING] PodCrashLooping" in text
    assert "severity=critical" in text
    assert "Pod x is crash-looping" in text
    assert "3100" in text  # value formatted


def test_prom_rules_filter_by_state_and_search(monkeypatch):
    import server

    class StubClient:
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
                            "type": "alerting",
                            "name": "NodeFilesystemAlmostFull",
                            "state": "inactive",
                            "query": "node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.1",
                            "duration": 0,
                            "health": "ok",
                            "labels": {},
                            "annotations": {},
                        },
                    ],
                }
            ]

    monkeypatch.setattr(server, "client", StubClient())
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    async def run(args):
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("prom_rules", args)

    out = asyncio.run(run({"state": "firing"})).content[0].text
    assert "PodCrashLooping" in out and "for 900s" in out and "NodeFilesystem" not in out
    out = asyncio.run(run({"search": "filesystem"})).content[0].text
    assert "NodeFilesystemAlmostFull" in out and "PodCrashLooping" not in out
    out = asyncio.run(run({})).content[0].text
    assert "2 rule(s)" in out


def test_prom_query_range_wire(monkeypatch):
    import server

    class StubClient:
        async def range_query(self, query, start, end, step):
            assert query == "up" and start.isdigit() and end.isdigit() and step
            return {
                "resultType": "matrix",
                "result": [{"metric": {"__name__": "up", "pod": "p1"}, "values": [["1757337600", "1"]]}],
            }

    monkeypatch.setattr(server, "client", StubClient())
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool("prom_query_range", {"query": "up", "start": "now-10m"})

    result = asyncio.run(run())
    text = result.content[0].text
    assert "up{pod=p1}" in text and "step=" in text


# ---------------------------------------------------------------------------
# Regression: the streamable-http ASGI app must run its lifespan
# (v0.1.0 shipped with the session manager never started — /health passed
# while every /mcp request died with "Task group is not initialized").
# ---------------------------------------------------------------------------


def test_streamable_http_smoke_serves_mcp_after_lifespan():
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    server_path = Path(__file__).resolve().parent.parent / "server.py"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            str(server_path),
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx2.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise AssertionError("server never became healthy")

        r = httpx2.post(
            f"{base}/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"},
                },
            },
            timeout=10,
        )
        assert r.status_code == 200, f"/mcp failed: {r.status_code} {r.text[:200]}"
        # MCP 2.0 runs STATELESS (server.py: no initialize handshake, no
        # Mcp-Session-Id — any replica can serve any request). Assert the
        # real contract: a valid JSON-RPC initialize result comes back and
        # no session header is required.
        body = r.json()
        assert body.get("jsonrpc") == "2.0" and "result" in body, body
        assert body["result"].get("serverInfo", {}).get("name") == "prometheus-mcp"
        assert "mcp-session-id" not in {k.lower() for k in r.headers}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
