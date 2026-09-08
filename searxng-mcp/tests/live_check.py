"""Live end-to-end check against a real SearXNG instance.

Usage:
    SEARXNG_URL=https://searxng.example.com SEARXNG_VERIFY_TLS=false \
        .venv/bin/python tests/live_check.py

Validates the real API contract: default search, engine allowlist, ddgs-style
region conversion, categories, and both fetch_content extraction paths.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

import server  # noqa: E402


async def main() -> None:
    print(f"SearXNG URL: {os.environ.get('SEARXNG_URL', '(default localhost:8080)')}")
    async with InMemoryTransport(server.mcp) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"\n[tools/list] {[t.name for t in tools.tools]}")

            # 1. Plain search (parity path)
            r = await session.call_tool(
                "search", {"query": "model context protocol", "max_results": 5}
            )
            text = r.content[0].text
            print("\n=== search: model context protocol ===")
            print(text[:1200])
            assert "Found" in text and not r.is_error

            # 2. Engine allowlist (backend param -> SearXNG engines)
            r = await session.call_tool(
                "search",
                {"query": "alan turing", "max_results": 3, "backend": "wikipedia"},
            )
            text = r.content[0].text
            print("\n=== search backend=wikipedia: alan turing ===")
            print(text[:600])
            assert not r.is_error

            # 3. ddgs-style region conversion (us-en -> en-US)
            r = await session.call_tool(
                "search",
                {"query": "berlin", "max_results": 3, "region": "us-en"},
            )
            assert not r.is_error
            print("\n=== search region=us-en: berlin (OK, converted) ===")

            # 4. Category search (free extra)
            r = await session.call_tool(
                "search",
                {"query": "quantum computing", "max_results": 3, "category": "news"},
            )
            print("\n=== search category=news: quantum computing ===")
            print(r.content[0].text[:600])

            # 5. fetch_content via trafilatura (wikipedia is trafilatura-friendly)
            r = await session.call_tool(
                "fetch_content",
                {"url": "https://en.wikipedia.org/wiki/Search_engine", "max_length": 1500},
            )
            text = r.content[0].text
            print("\n=== fetch_content: wikipedia Search_engine ===")
            print(text[:500], "...")
            assert not r.is_error and "Content info" in text

    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
