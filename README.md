# MCP servers for PCAI

Repository to contain all MCP servers validated on PCAI.

| Server | Description |
| --- | --- |
| [k8s-ops](k8s-ops/README.md) | A read-only Kubernetes ops MCP server. |
| [seaborn](seaborn/README.md) | Provides statistical visualization capabilities through the seaborn, matplotlib, and plotly libraries. |
| [searxng-mcp](searxng-mcp/README.md) | MCP 2.0 server for web search backed by a self-hosted SearXNG instance (bundled sidecar) — metasearch over a stable JSON API with per-engine status, plus a content fetcher (trafilatura, curl_cffi TLS-fingerprint escalation, Wikipedia API). Drop-in replacement for ddgs-lite. Optionally deploys a Playwright-driven headless Chromium sidecar so `fetch_content` can render JavaScript-only pages (auto-escalation, plus screenshot capture for vision models). |
| [sql-handler](sql-handler/README.md) | Alternative to EZPresto for querying SQL in a few commonly deployed modes. Avoiding JDBC yields a 3-4x speed up with typical SQL queries, and avoids timeouts that are typical for larger datasets.|
| [text-to-image](text-to-image/README.md) | Image generation from a prompt. |
| ddgs-lite | (Development Frozen) An MCP server for web search using the ddgs metasearch library (Dux Distributed Global Search). Aggregates results from DuckDuckGo, Bing, Brave, Google, Startpage, Yandex, Yahoo, Mojeek, and Wikipedia with automatic fallback — if one engine is blocked, others take over. Also provides a content fetcher for extracting readable text from web pages. |