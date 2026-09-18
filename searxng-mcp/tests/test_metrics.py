"""Tests for the searxng-mcp /metrics endpoint (Wave-3 C3 — additive, default OFF).

The contract being pinned:

* DEFAULT OFF: with SEARXNG_METRICS_ENABLED unset (the chart default —
  metrics.enabled: false renders no env at all), the assembled app contains NO
  /metrics route. Nothing else about the app changes.
* ON: the route serves the Prometheus text exposition and the per-tool
  counters increment as MCP tool calls flow through mcp.call_tool — including
  the error outcome (search/fetch failures are reported as strings).
* /metrics is key-free when enabled even with API keys configured (the auth
  gate protects only the /mcp prefix) — scrapeable like the probes.

These tests build the app through server._build_http_app — the same assembly
main() uses (extracted Wave-3 C3, the fleet convention) — and drive it with
starlette's TestClient, the searxng suite's existing pattern (runs under the
conda fleet env, where httpx + prometheus_client are both available; the
assertions stay backend-agnostic regardless).
"""

import asyncio
import re
from contextlib import contextmanager

from starlette.testclient import TestClient

import mcp_metrics
import server


@contextmanager
def running(monkeypatch, *, enabled, api_key=None):
    monkeypatch.delenv("SEARXNG_METRICS_ENABLED", raising=False)
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    monkeypatch.delenv(server.SEARXNG_API_KEYS_ENV, raising=False)
    if enabled:
        monkeypatch.setenv("SEARXNG_METRICS_ENABLED", "1")
    if api_key:
        monkeypatch.setenv(server.SEARXNG_API_KEYS_ENV, api_key)
    app = server._build_http_app({"streamable-http"})
    with TestClient(app) as client:

        def metrics_text() -> str:
            r = client.get("/metrics")
            assert r.status_code == 200
            return r.text

        yield metrics_text


def call_tool(name, arguments):
    """Drive one tool call through the REAL MCP dispatch (the seam the
    metrics wrapper instruments) via the SDK's in-memory transport — the
    searxng suite's existing end-to-end pattern; a real session supplies a
    real Context, unlike a bare mcp.call_tool call."""
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    async def run():
        async with InMemoryTransport(server.mcp) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)

    return asyncio.run(run())


def metric_value(body: str, name: str, **labels):
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        found = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if all(found.get(k) == v for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


class _StubSearcher:
    """Replaces the network-backed SearXNGClient (offline smoke).

    The tool body itself reads ``resp.results`` (for ctx.info) before handing
    the response to format_search_response — so the stub returns a namespace
    with .results and the formatter seam is patched at the call site.
    """

    async def search(self, *args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(results=[{"title": "t", "url": "https://x"}])


def test_default_off_no_metrics_route(monkeypatch):
    monkeypatch.delenv("SEARXNG_METRICS_ENABLED", raising=False)
    app = server._build_http_app({"streamable-http"})
    assert all(getattr(r, "path", None) != "/metrics" for r in app.routes)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404


def test_enabled_serves_exposition(monkeypatch):
    with running(monkeypatch, enabled=True) as metrics_text:
        body = metrics_text()
        assert f"# TYPE {mcp_metrics.TOOL_REQUESTS} counter" in body


def test_counters_increment_search_outcomes(monkeypatch):
    with running(monkeypatch, enabled=True) as metrics_text:
        real_searcher, real_formatter = server.searcher, server.format_search_response
        server.searcher = _StubSearcher()
        server.format_search_response = lambda resp, limit: "stub-search-ok"
        try:
            call_tool("search", {"query": "metrics smoke"})
        finally:
            server.searcher, server.format_search_response = real_searcher, real_formatter
        body = metrics_text()
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="search", outcome="ok") == 1.0


def test_search_failure_counts_as_error_outcome(monkeypatch):
    # An empty query fails validation inside the tool body ("Search failed:
    # ...") — the outcome label must see it, not just exceptions.
    with running(monkeypatch, enabled=True) as metrics_text:
        call_tool("search", {"query": "   "})
        body = metrics_text()
        assert metric_value(body, mcp_metrics.TOOL_REQUESTS, tool="search", outcome="error") == 1.0


def test_metrics_key_free_while_mcp_stays_gated(monkeypatch):
    with running(monkeypatch, enabled=True, api_key="scrape-not-needed") as metrics_text:
        metrics_text()  # key-free — the gate protects ONLY /mcp
        app = server._build_http_app({"streamable-http"})
        with TestClient(app) as client:
            assert (
                client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                    headers={"Accept": "application/json, text/event-stream"},
                ).status_code
                == 401
            )


def test_counters_export_no_query_or_url_text(monkeypatch):
    with running(monkeypatch, enabled=True) as metrics_text:
        real_searcher = server.searcher
        server.searcher = _StubSearcher()
        try:
            call_tool("search", {"query": "super-secret-query"})
        finally:
            server.searcher = real_searcher
        body = metrics_text()
        assert "super-secret-query" not in body
