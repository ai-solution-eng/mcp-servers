# SearXNG MCP Server

SearXNG MCP Server is an MCP 2.0 (Model Context Protocol) server for web search and page reading, backed by a **self-hosted SearXNG** metasearch instance bundled as a sidecar container. It exposes the same two tools as `ddgs-lite` — `search` and `fetch_content` — with the same output layout, but the metasearch part runs on SearXNG's stable, documented JSON API instead of client-side scraping: SearXNG aggregates Google, Bing, Qwant, Mojeek, Wikipedia, and more server-side, so no single engine's CAPTCHA wall or markup change breaks it. The chart deploys the whole pod behind the PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) Istio gateway — MCP clients connect at `https://<endpoint>/mcp`, while the host root serves the normal SearXNG web UI.

```
┌────────────────────────── Pod (searxng-mcp) ──────────────────────────┐
│                                                                       │
│  ┌───────────────────┐   localhost:8080   ┌────────────────────────┐  │
│  │  searxng-mcp      │ ◄─────────────────► │  SearXNG (sidecar)     │  │
│  │  (port 9090)      │    JSON API         │  (port 8080)           │  │
│  │                   │                     │  engines ──► internet  │  │
│  │  tools:           │                     │  (via corporate proxy  │  │
│  │   • search        │                     │   on HPE networks)     │  │
│  │   • fetch_content │                     │                        │  │
│  └───────────────────┘                     └────────────────────────┘  │
│  ┌───────────────────┐                                                │
│  │  browser (option) │  headless Chromium, loopback CDP :9222         │
│  └───────────────────┘                                                │
└───────────────────────────────────────────────────────────────────────┘
          ▲                                    ▲
          │ MCP (streamable-http / SSE)        │ normal SearXNG web page
   in-cluster:                                at the VirtualService root
   http://searxng-mcp-service.<ns>.svc.cluster.local:9090/mcp
          │
    external: https://searxng-mcp.<DOMAIN_NAME>/mcp
```

## What problem(s) it solves

- **Search that does not rot.** Client-side scrapers (the ddgs approach) break when an engine changes markup or serves a CAPTCHA. SearXNG aggregates server-side over a documented JSON API; if one engine fails, the others still contribute results.
- **Failure visibility.** The `search` tool reports per query which engines did not respond (`Note: some engines did not respond: …`), so an agent can distinguish "no such thing" from "engines are down/suspended".
- **Richer search controls than plain text search** — `category` (general/news/images/videos/music/files/it/science/…), `time_range` (day/week/month/year), `safesearch` (0/1/2), `pageno` paging, and ddgs-style *and* native SearXNG region codes.
- **Drop-in replacement for ddgs-lite.** Same two tool names, same arguments where it matters, same output contract (including the `[Content info: …]` pagination footer) — point the client at the new endpoint and agents keep working; only `backend` changes meaning (engine allowlist instead of ddgs backend names).
- **JavaScript-only pages become readable.** The optional headless-browser sidecar lets `fetch_content` render SPAs and challenge interstitials in a real Chromium (auto-escalation, `render="always"`, or screenshots via `include_screenshot`) — plain HTTP fetching alone cannot see these pages.
- **Engine tuning per network.** Own `settings.yml` rendered from values (e.g. disable CAPTCHA-prone engines like startpage on the HPE proxy), corporate-proxy wiring for both SearXNG engine requests and `fetch_content` egress, and self-hosting — no third-party search SaaS in the path.

## Tools

### `search`

```python
search(
    query: str,
    max_results: int = 10,     # 1-20
    region: str = "",          # ddgs-style 'us-en'/'wt-wt' AND native 'en-US'/'en'
    backend: str = "auto",     # 'auto' or a comma-delimited engine allowlist
    category: str = "general", # general|news|images|videos|music|files|it|science
    time_range: str = "",      # ''|day|week|month|year
    safesearch: int = 0,       # 0|1|2
    pageno: int = 1,           # result page
) -> str
```

- `backend="auto"` lets SearXNG fan out over its enabled engines (default). Any other value becomes SearXNG's `engines` parameter, e.g. `backend="wikipedia"` or `backend="google,bing"`.
- `region` accepts both ddgs-style codes and native SearXNG locales — `us-en` → `en-US`, `de-de` → `de-DE`, `uk-en` → `en-GB`, `wt-wt` → `all`. If SearXNG rejects a code, the client retries once without it.
- Output mirrors ddgs-lite (`Found N search results:` + numbered title/URL/Summary) plus `Answer:`, `Related searches:`, and the `Note: some engines did not respond: …` line when engines fail.

### `fetch_content`

```python
fetch_content(
    url: str,
    start_index: int = 0,      # pagination offset
    max_length: int = 8000,    # characters per call
    backend: str = "auto",     # auto|trafilatura|bs4 ('httpx' alias)|curl|wikipedia
    render: str = "auto",      # auto|always|never — headless-browser usage
    include_screenshot: bool = False,  # append a PNG data URL of the page
) -> str
```

Same output contract as ddgs-lite, including the `[Content info: Showing characters X-Y of Z total. Use start_index=… to see more (via …)]` pagination footer.

Fetch escalation: `auto`/`trafilatura`/`bs4` fetch with plain httpx first and automatically retry through **curl_cffi impersonating Chrome** when the site rejects plain python clients (TLS-fingerprint 403s — wikipedia does this from some egress paths). `curl` always uses the impersonated fetch; `wikipedia` goes straight to the Wikipedia API. Failures are self-describing — the error includes the last attempt's HTTP status or exception.

**Headless-browser escalation** (when the sidecar is deployed): `render="auto"` further escalates to a real Chromium render when the plain ladder fails outright or returns a JS-stub shell (near-empty text, `<noscript>` markers, challenge interstitials); the rendered DOM flows through the same trafilatura → bs4 extraction chain and the `(via headless-browser+…)` footer says so. `render="always"` skips plain HTTP entirely; `render="never"` keeps the plain-only behavior. `include_screenshot=True` forces a browser render and appends the page screenshot as a base64 PNG data URL — pass it to a vision tool as-is. Every browser failure degrades gracefully to the plain result with the reason logged; the sidecar is never a hard dependency.

## Architecture

- **One pod, up to three containers.** An init container copies the rendered `settings.yml` ConfigMap into an `emptyDir` (SearXNG's entrypoint writes to `/etc/searxng`); the **SearXNG sidecar** (8080) is the metasearch engine; the **MCP server** (9090) speaks MCP 2.0 — natively stateless streamable-HTTP (`json_response`), so any replica can serve any request, plus SSE for older clients; and optionally the **browser sidecar** (Playwright Chromium headless shell) reachable only on pod-loopback `127.0.0.1:9222` — no service, port, or policy changes, and nothing outside the pod can reach the unauthenticated CDP port (never add `--remote-debugging-address=0.0.0.0`).
- **Chart objects:** Deployment (+ConfigMap), a two-port Service (`mcp` 9090, `searxng` 8080), an Istio VirtualService on `istio-system/ezaf-gateway` routing `/mcp` → 9090 and `/` → 8080 (the SearXNG web UI), and the Kyverno `hpe-ezua/*` vendor-label policy.

`search` runs inside the MCP container but deliberately ignores environment proxies for the SearXNG call (`trust_env=False` — the sidecar is reached over localhost); only `fetch_content` and SearXNG's own engine requests use the corporate proxy.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG base URL (the sidecar) |
| `SEARXNG_LANGUAGE` | `en-US` | Default language/region |
| `SEARXNG_TIMEOUT` | `10` | Search request timeout (s) |
| `SEARXNG_VERIFY_TLS` | `true` | TLS verification for https:// SearXNG URLs |
| `SEARXNG_REQUESTS_PER_MINUTE` | `30` | Search rate limit |
| `FETCH_REQUESTS_PER_MINUTE` | `20` | Fetch rate limit |
| `FETCH_VERIFY_TLS` | `true` | TLS verification for fetched pages |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` | — | Corporate proxy for `fetch_content` |
| `BROWSER_CDP_URL` | `http://127.0.0.1:9222` | Headless-browser sidecar CDP endpoint |
| `BROWSER_NAV_TIMEOUT_MS` etc. | see `browser_client.py` | Nav timeout, settle wait, max pages, resource blocking |

Only `SEARXNG_URL` is wired by the chart's default values (`env:`); the rest are underlying detail for custom deployments.

## Deployment (PCAI way)

Users never run `helm install` or `kubectl apply` to deploy on PCAI: the packaged chart is imported into the PCAI catalog once, then the deployment is created from the PCAI UI (or PCAI API), and its **Helm Values** editor is where every knob is set. PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering.

**Required values** (everything else has a working chart default):

```yaml
searxng:
  # session/crypto secret — CHANGE for any real deployment
  secretKey: "<SECRET_KEY>"
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "searxng-mcp.${DOMAIN_NAME}"
```

**Optional values** — `browser.enabled` (+ `browser.image.tag`, `browser.caCert.*` for corporate MITM CAs) to enable JS-page rendering and screenshots; `hpe_proxies: true` + `proxy.*` on HPE-network clusters (wires the proxy into BOTH SearXNG engine requests and MCP `fetch_content` egress); `searxng.baseUrl` (public UI links), `searxng.language`, `searxng.disabledEngines`; `imagePullSecrets` if the GHCR image stays private; `resources` for all three containers. The full annotated list lives in [`helm/values.yaml`](helm/values.yaml); paste-ready complete examples live in [`helm/values-examples/`](helm/values-examples/README.md).

## Connecting an MCP client

Through the PCAI gateway (from outside the cluster):

```json
{
  "mcpServers": {
    "searxng-mcp": {
      "url": "https://searxng-mcp.<your-domain>/mcp"
    }
  }
}
```

From a workload inside the cluster, use the in-cluster service and skip the gateway:

```json
{
  "mcpServers": {
    "searxng-mcp": {
      "url": "http://searxng-mcp-service.<namespace>.svc.cluster.local:9090/mcp"
    }
  }
}
```

The server performs no client authentication; when exposed through the PCAI gateway, access is governed by the gateway's auth configuration. The host **root** (`https://searxng-mcp.<your-domain>/`) is the normal SearXNG web UI — handy for manually checking engine health; `/mcp` is the MCP endpoint.

### Cutover from ddgs-lite

1. Deploy this chart to its own namespace.
2. Verify per [documentation/VERIFICATION.md](documentation/VERIFICATION.md).
3. Flip the MCP client entry from the ddgs-lite URL to the `searxng-mcp` URL above — tool names and signatures are unchanged, so agents keep working; only `backend` values change meaning (engine allowlist instead of ddgs backend names).
4. Decommission the ddgs-lite release when confident.

## Local development

```bash
uv venv --python 3.12 .venv          # or any python ≥3.10
uv pip install -e . pytest
.venv/bin/python -m pytest tests/ -v # 58 unit tests (fully mocked, no network)

# Live check against any real SearXNG:
SEARXNG_URL=https://searxng.example.com SEARXNG_VERIFY_TLS=false \
    .venv/bin/python tests/live_check.py

# Run locally (stdio for Claude Desktop / inspector):
SEARXNG_URL=http://localhost:8080 .venv/bin/python server.py --transport stdio

# Run locally (HTTP transports, like the container):
SEARXNG_URL=http://localhost:8080 \
    .venv/bin/python server.py --transport streamable-http sse --port 9090

# Bare-docker SearXNG for local testing (JSON format pre-enabled):
docker run --rm -p 8080:8080 \
    -v $PWD/searxng/settings.yml:/etc/searxng/settings.yml:ro \
    -e SEARXNG_BASE_URL=http://localhost:8080/ \
    -e SEARXNG_SECRET=dev-secret \
    searxng/searxng:2026.9.8-3fdc6d753
```

### Building images

```bash
# MCP image (playwright client included via the browser extra)
docker buildx build -t ghcr.io/ai-solution-eng/searxng-mcp:v1.2.2 -f Dockerfile . --push

# Optional headless-browser sidecar (see browser/Dockerfile header)
docker buildx build -t ghcr.io/ai-solution-eng/searxng-mcp-browser:v1.2.1 \
    mcp_servers/searxng_mcp/browser --push
```

## SearXNG settings notes

- `search.formats` **must** include `json` — the MCP server uses `format=json`, and SearXNG returns 403 otherwise (the client surfaces a targeted hint when that happens). The chart's ConfigMap always sets it.
- **Engine overrides go at the TOP LEVEL of settings.yml** (`engines:`), not under `search.engines:`. SearXNG's settings loader (`searx/settings_loader.py`) reads `user_settings['engines']` only — a `search.engines` key is silently ignored. (A pre-existing deployment made exactly this mistake.)
- **Sidecar image pinning.** The chart pins SearXNG to the dated tag that Docker Hub's `latest` resolved to when the chart was last touched (never the mutable `latest` tag itself). To refresh the pin: look up the newest dated tag on Docker Hub, bump `searxng.image.tag` in values, and re-apply. For a fully immutable reference, use the digest form. The MCP server side is decoupled from SearXNG releases — it speaks the JSON API — so sidecar upgrades are low-risk; validate with `GET /config` and one search call after upgrading.
- `server.limiter: false` keeps the limiter off for internal API use — no valkey/redis required. Add valkey only if you ever expose this publicly.
- **Default: no engines are disabled.** All default SearXNG engines stay on, including ddg/brave/startpage. SearXNG auto-suspends engines that fail repeatedly (transient, self-recovers) and the search tool reports it per query. If one proves *consistently* dead on a given network (startpage showed a hard CAPTCHA wall on the HPE proxy during testing), add it to `searxng.disabledEngines` in values and re-apply; verify with `GET /config` afterwards.

## Files

```
searxng_mcp/
├── server.py               # MCP 2.0 MCPServer + tools (search, fetch_content)
├── searxng_client.py       # SearXNG JSON API client + formatting
├── fetcher.py              # trafilatura → bs4/html2text → Wikipedia fetcher
│                           #   (+ headless-browser escalation rung)
├── browser_client.py       # Playwright-over-CDP client for the browser sidecar
├── browser/                # headless-browser sidecar image (Chromium headless
│   ├── Dockerfile          #   shell, loopback CDP, CA/proxy entrypoint)
│   └── entrypoint.sh
├── pyproject.toml          # package: searxng-mcp, entry point: searxng-mcp
├── Dockerfile
├── searxng/settings.yml    # reference settings (bare docker / docs)
├── helm/                   # chart: configmap, sidecar deployment, service, VS
│   ├── local/              # per-site values (never committed)
│   └── values-examples/    # paste-ready secret-free examples
└── tests/
    ├── test_searxng_mcp.py    # 32 unit tests (mocked transport, in-memory MCP)
    ├── test_browser_render.py # 26 escalation tests (stubbed browser client)
    └── live_check.py          # end-to-end check vs a real instance (+ sidecar)
```

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), SearXNG sidecar + browser sidecar, ezua/Istio wiring, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake check, one-tool test, optional operator kubectl, troubleshooting |
| [helm/values-examples/](helm/values-examples/README.md) | Paste-ready, secret-free full-values examples (G2 lab, hosted trial) |
| [helm/values.yaml](helm/values.yaml) | Chart defaults — the authoritative list of every knob |
