# ddgs-lite — DuckDuckGo Metasearch MCP Server

> **⚠️ DEPRECATED — fleet decision D17 (stage 1 of 2).** ddgs_lite is deprecated —
> use **searxng_mcp** instead (drop-in: same two tools, `search` + `fetch_content`,
> backed by a real SearXNG metasearch engine with the fleet SSRF guard and auth
> middleware). Archive (removal) is pending the D17 window; until then this
> server still works unchanged, and every tool call emits a one-time-per-process
> `DeprecationWarning` so stragglers stay visible in logs.

ddgs-lite is an MCP (Model Context Protocol) server for web search and page reading, built on the [`ddgs`](https://pypi.org/project/ddgs/) metasearch library (Dux Distributed Global Search). It aggregates results from DuckDuckGo, Bing, Brave, Google, Startpage, Yandex, Yahoo, Mojeek, and Wikipedia with automatic engine fallback — if one engine is blocked by a CAPTCHA or bot filter, the others take over — and adds a content fetcher that extracts a page's main text as clean markdown. It is deployed as the `ddgs-lite` Helm chart behind the PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) Istio gateway and speaks MCP over streamable-HTTP (and SSE) at `/mcp`, so any MCP client — agents such as DSH, Claude Desktop, or the MCP Inspector — can use it as a drop-in web-search tool.

## What problem(s) it solves

- **Fresh web knowledge for LLM agents** — LLMs only know their training data; `search` brings back current titles, URLs, and snippets for anything recent.
- **No API keys, no scraping maintenance** — results come from the `ddgs` library's multi-engine aggregation with primp TLS-fingerprint impersonation, so no search-engine API key is needed and a single blocked engine does not break searches.
- **Reading the pages behind the results** — `fetch_content` turns a search hit into clean markdown (links, headers, lists preserved) with pagination, so an agent can actually read the source instead of guessing from snippets.
- **Resilience under bot filters** — engine fallback in `search`, and a three-rung fetch ladder (ddgs TLS-impersonation extract → httpx → Wikipedia REST API) for pages that reject plain clients.
- **Self-hosted, governed deployment** — runs inside the PCAI cluster as a vendor-service workload; no data leaves to a third-party search SaaS beyond the search engines themselves.

## Tools

| Tool | Purpose |
|---|---|
| `search` | Web search across 9+ engines (`query`, `max_results` 1–20, `region` e.g. `us-en`/`wt-wt`, `backend` `auto` or a comma-delimited engine subset such as `duckduckgo,bing,google`). Available backends: duckduckgo, bing, brave, google, startpage, yandex, yahoo, mojeek, wikipedia, grokipedia. |
| `fetch_content` | Fetch a URL and extract the main text as clean markdown (`url`, `start_index`, `max_length` 8000 chars default, `backend` `auto`/`ddgs`/`httpx`). Cannot execute JavaScript; Wikipedia pages blocked by IP restrictions fall back to the Wikipedia API. |

Both tools return untrusted external text and say so in their output; both are internally rate-limited (30 req/min search, 20 req/min fetch).

## ddgs-lite vs ddgs-mcp

Two sibling DuckDuckGo MCP servers exist in this fleet — verified from source, they differ in the search path, not in the tool names:

| | **ddgs-lite** (this repo) | ddgs-mcp (`ddgs_mcp`) |
|---|---|---|
| Search engine | `ddgs` metasearch library — 9 engines with automatic fallback | DuckDuckGo only (scrapes the DuckDuckGo HTML endpoint) |
| Search output | Numbered results, engine fallback on CAPTCHA blocks | Numbered results, SafeSearch mode fixed at startup (`DDG_SAFE_SEARCH`) |
| `fetch_content` output | Clean **markdown** (links/headers preserved) via ddgs extract → httpx → Wikipedia API | **Plain text** (scripts/nav stripped) via httpx → curl_cffi on 403/Cloudflare challenges |
| Env knobs | `DDG_REGION`, `HTTP(S)_PROXY` | `DDG_REGION`, `DDG_SAFE_SEARCH` (STRICT/MODERATE/OFF), proxy env |

`searxng-mcp` supersedes both for new deployments (stable JSON-API metasearch, categories, per-engine status) while keeping the same two tool names.

## Architecture

```
MCP client ──https──► PCAI Istio gateway (ezaf-gateway)
                          │  VirtualService: ddgs-lite.<domain> → service :9090
                          ▼
              ┌────────────────────────── Pod ──────────────────────────┐
              │  ddgs-lite (uvicorn, port 9090)                          │
              │   • MCP 2.0 streamable-http + SSE, both at /mcp          │
              │   • search  → ddgs library → 9 engines (TLS imperson.)   │
              │   • fetch   → ddgs extract → httpx → Wikipedia API       │
              └──────────────────────────────────────────────────────────┘
```

Single container, no sidecars, no persistent storage. The chart ships a Deployment, a ClusterIP Service (9090), an Istio VirtualService on `istio-system/ezaf-gateway`, and a Kyverno pre-install policy that stamps the `hpe-ezua/*` vendor labels PCAI expects.

## Deployment (PCAI way)

Users never run `helm install` or `kubectl apply` to deploy on PCAI: the packaged chart is imported into the PCAI catalog once, then the deployment is created from the PCAI UI (or PCAI API), and its **Helm Values** editor is where every knob below is set. PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering.

**Required values** (everything else has a working chart default):

```yaml
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "ddgs-lite.${DOMAIN_NAME}"
```

**Optional values** — `image.tag` (pin to the tag matching the chart version), `resources`, `env` (e.g. `DDG_REGION`), `hpe_proxies` + `proxy.*` (route search/fetch egress through the HPE corporate proxy — required on HPE-network clusters that have no direct internet egress), `deployment.replicaCount`. The full annotated list lives in [`helm/values.yaml`](helm/values.yaml); paste-ready complete examples live in [`helm/values-examples/`](helm/values-examples/README.md).

## Connecting an MCP client

Through the PCAI gateway (from outside the cluster):

```json
{
  "mcpServers": {
    "ddgs-lite": {
      "url": "https://ddgs-lite.<your-domain>/mcp"
    }
  }
}
```

From a workload inside the cluster, use the in-cluster service and skip the gateway:

```json
{
  "mcpServers": {
    "ddgs-lite": {
      "url": "http://ddgs-lite-service.<namespace>.svc.cluster.local:9090/mcp"
    }
  }
}
```

The server itself performs no client authentication; when exposed through the PCAI gateway, access is governed by the gateway's auth configuration.

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), ezua/Istio wiring, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake check, one-tool test, optional operator kubectl, troubleshooting |
| [helm/values-examples/](helm/values-examples/README.md) | Paste-ready, secret-free full-values examples (G2 lab, hosted trial) |
| [helm/values.yaml](helm/values.yaml) | Chart defaults — the authoritative list of every knob |
