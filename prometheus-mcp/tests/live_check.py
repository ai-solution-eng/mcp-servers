"""Live end-to-end check against a real Prometheus.

Usage (in-cluster or with a route to the Prometheus service):
    PROM_URL=http://kubeprom-prometheus.prometheus.svc.cluster.local:9090 \
        .venv/bin/python tests/live_check.py

Validates the real API contract: instant query, range query with relative
times, series discovery, label values, alerts, rules.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

import server


async def main() -> None:
    url = server.config.base_url
    try:
        await httpx.AsyncClient(timeout=5).get(url.rstrip("/") + "/-/ready")
        print(f"Prometheus reachable at {url}")
    except Exception as e:
        print(f"Prometheus NOT reachable at {url}: {e}")
        print("Run inside the cluster (or port-forward) with PROM_URL set.")
        sys.exit(2)

    async with (
        InMemoryTransport(server.mcp) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        print(f"\n[tools/list] {[t.name for t in tools.tools]}")

        # 1. Instant query — up
        r = await session.call_tool("prom_query", {"query": "count(up == 1)"})
        text = r.content[0].text
        print("\n=== prom_query: count(up == 1) ===")
        print(text[:400])
        assert not r.is_error

        # 2. Range query with relative times
        r = await session.call_tool(
            "prom_query_range",
            {"query": "avg(rate(node_cpu_seconds_total{mode!=\"idle\"}[5m]))", "start": "now-30m", "end": "now"},
        )
        text = r.content[0].text
        print("\n=== prom_query_range: node cpu (30m) ===")
        print(text[:400])
        assert not r.is_error

        # 3. Series + label values
        r = await session.call_tool("prom_series", {"match": "up", "start": "now-15m"})
        assert not r.is_error
        print("\n=== prom_series: up (15m) ===")
        print(r.content[0].text[:400])
        r = await session.call_tool(
            "prom_label_values", {"label": "namespace", "match": "up"}
        )
        assert not r.is_error
        print("\n=== prom_label_values: namespace (via up) ===")
        print(r.content[0].text[:400])

        # 4. Alerts + rules (may legitimately be empty)
        r = await session.call_tool("prom_alerts", {})
        assert not r.is_error
        print("\n=== prom_alerts ===")
        print(r.content[0].text[:600])
        r = await session.call_tool("prom_rules", {})
        assert not r.is_error
        print("\n=== prom_rules ===")
        print(r.content[0].text[:600])

    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
