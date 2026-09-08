# SearXNG MCP Server

MCP 2.0 server for web search backed by a **self-hosted SearXNG** instance,
bundled as a sidecar container. Drop-in replacement for `ddgs-lite`: same two
tools (`search`, `fetch_content`), same output layout — but the metasearch
part runs on SearXNG's stable JSON API instead of client-side scraping.

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
└───────────────────────────────────────────────────────────────────────┘
          ▲                                    ▲
          │ MCP (streamable-http / SSE)        │ normal SearXNG web page
   in-cluster:                                at the VirtualService root
   http://searxng-mcp-service.<ns>.svc.cluster.local:9090/mcp
          │
   external: https://searxng-mcp.<DOMAIN_NAME>/mcp   (oauth2-proxy)
```

## Why SearXNG instead of ddgs

| | ddgs-lite | searxng-mcp |
|---|---|---|
| Metasearch | Scrapes engine endpoints client-side (primp TLS impersonation); breaks on CAPTCHAs and markup changes | SearXNG aggregates server-side over a documented JSON API |
| Failure visibility | Opaque library errors | Per-engine status reported in results (`unresponsive_engines`) |
| Engine tuning | Library-defined | Own `settings.yml` (disable CAPTCHA-prone engines per network) |
| Categories | text only | general, news, images, videos, music, files, it, science, … |
| Content fetching | ddgs extract (primp TLS impersonation) → httpx → Wikipedia API | trafilatura / bs4+html2text extraction; fetching escalates plain httpx → **curl_cffi (Chrome TLS fingerprint)** → Wikipedia API |

**Note:** TLS fingerprint impersonation is back for *fetching* — via
`curl_cffi` (a maintained libcurl-impersonate binding), used as an
escalation when a site 403s plain clients. Unlike ddgs, it impersonates
only the TLS handshake for page fetches; search itself never scrapes —
it rides SearXNG's JSON API, so no scraping library is in the search path.

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

- `backend="auto"` lets SearXNG fan out over its enabled engines (default).
  Any other value becomes SearXNG's `engines` parameter, e.g.
  `backend="wikipedia"` or `backend="google,bing"`.
- `region` accepts both ddgs-style codes and native SearXNG locales —
  `us-en` → `en-US`, `de-de` → `de-DE`, `uk-en` → `en-GB`, `wt-wt` → `all`.
  If SearXNG rejects a code, the client retries once without it.
- Output mirrors ddgs-lite (`Found N search results:` + numbered
  title/URL/Summary) plus `Answer:`, `Related searches:`, and a
  `Note: some engines did not respond: …` line when engines fail — so an
  agent can distinguish "no such thing" from "engines are down/suspended".

### `fetch_content`

```python
fetch_content(
    url: str,
    start_index: int = 0,      # pagination offset
    max_length: int = 8000,    # characters per call
    backend: str = "auto",     # auto|trafilatura|bs4 ('httpx' alias)|curl|wikipedia
) -> str
```

Same output contract as ddgs-lite, including the
`[Content info: Showing characters X-Y of Z total. Use start_index=… to see
more (via …)]` pagination footer.

Fetch escalation: `auto`/`trafilatura`/`bs4` fetch with plain httpx first and
automatically retry through **curl_cffi impersonating Chrome** when the site
rejects plain python clients (TLS-fingerprint 403s — wikipedia does this from
some egress paths). `curl` always uses the impersonated fetch; `wikipedia`
goes straight to the Wikipedia API. Failures are self-describing — the error
includes the last attempt's HTTP status or exception.

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

The search client deliberately ignores environment proxies
(`trust_env=False`): the sidecar is reached over `localhost`, and nothing
must intercept that.

## Local development

```bash
uv venv --python 3.12 .venv          # or any python ≥3.10
uv pip install -e . pytest           # or: uv pip install -r <(sed ...) — see pyproject
.venv/bin/python -m pytest tests/ -v # 30 unit tests (fully mocked, no network)

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

## Deployment

```bash
# Build & push the MCP image
docker buildx build -t ghcr.io/ai-solution-eng/searxng-mcp:v0.1.0 -f Dockerfile . --push

# Render & inspect, then install (per-site values from helm/local/)
helm template searxng-mcp helm/ -f helm/local/values.<site>.yaml
helm upgrade --install searxng-mcp helm/ -n searxng-mcp --create-namespace \
    -f helm/local/values.<site>.yaml
```

Chart contents:

- `deployment.yaml` — one pod, three containers: an init container that
  copies the settings ConfigMap into an `emptyDir` (SearXNG's entrypoint
  writes to `/etc/searxng`), the SearXNG sidecar (8080), and the MCP server
  (9090).
- `configmap.yaml` — renders `settings.yml` from values: JSON format
  enabled, limiter off (no valkey needed), optional disabled-engine list
  (default: none), optional
  `outgoing.proxies` for the corporate proxy.
- `service.yaml` — ports `mcp` (9090) and `searxng` (8080).
- `virtualservice.yaml` — `/mcp` → 9090 (MCP), `/` → 8080 (**the normal
  SearXNG web page**), behind the EZAF gateway.
- `kyverno.yaml` — vendor label policy (same as ddgs-lite).

On HPE-network clusters set `hpe_proxies: true` — this wires the corporate
proxy into both SearXNG's engine requests (settings.yml) and the MCP
container's `fetch_content` egress (env), matching the ddgs-lite convention.

### MCP client registration

In-cluster (preferred — bypasses gateway auth), e.g. in the DSH profile:

```yaml
- id: mcp-searxng
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    transport: streamable-http
    serverName: searxng
    url: http://searxng-mcp-service.searxng-mcp.svc.cluster.local:9090/mcp
```

Or through the gateway (oauth2-proxy protected):

```yaml
    url: https://searxng-mcp.<cluster-domain>/mcp
```

### Cutover from ddgs-lite

1. Deploy this chart to its own namespace (e.g. `searxng-mcp`).
2. Verify: `curl -s http://searxng-mcp-service.searxng-mcp.svc.cluster.local:9090/mcp`.
3. Flip the MCP client entry `mcp-duck-duck-go` (ddgs-lite) to the
   `mcp-searxng` entry above — tool names and signatures are unchanged, so
   agents keep working; only `backend` values change meaning
   (engine allowlist instead of ddgs backend names).
4. Decommission the ddgs-lite release when confident.

## SearXNG settings notes

- `search.formats` **must** include `json` — the MCP server uses
  `format=json`, and SearXNG returns 403 otherwise (the client surfaces a
  targeted hint when that happens).
- **Engine overrides go at the TOP LEVEL of settings.yml** (`engines:`), not
  under `search.engines:`. SearXNG's settings loader
  (`searx/settings_loader.py`) reads `user_settings['engines']` only — a
  `search.engines` key is silently ignored. (The pre-existing `searxng`
  namespace deployment made exactly this mistake: its ConfigMap "disabled"
  brave/duckduckgo/startpage, but `/config` showed them enabled and failing
  for 12 days.)
- **Sidecar image pinning.** The chart pins SearXNG to the dated tag that
  Docker Hub's `latest` resolved to when the chart was last touched (never
  the mutable `latest` tag itself — pods rescheduled later would silently
  get different engines/settings). To refresh the pin: look up the newest
  dated tag on Docker Hub, bump `searxng.image.tag` in values, and upgrade.
  For a fully immutable reference, use the digest form
  (`image: searxng/searxng@sha256:…`). The MCP server side is decoupled
  from SearXNG releases — it speaks the JSON API, whose schema has been
  stable for years — so sidecar upgrades are low-risk; validate with
  `GET /config` and one search call after upgrading.
- `server.limiter: false` keeps the limiter off for internal API use — no
  valkey/redis required. Add valkey only if you ever expose this publicly.
- **Default: no engines are disabled.** All default SearXNG engines stay on,
  including ddg/brave/startpage. SearXNG auto-suspends engines that fail
  repeatedly (transient, self-recovers) and the search tool reports
  `some engines did not respond: …` per query — so an engine being flaky
  costs a little latency, never correctness. If one proves *consistently*
  dead on a given network (startpage showed a hard CAPTCHA wall on the HPE
  proxy during testing), add it to `searxng.disabledEngines` in values and
  `helm upgrade`; verify with `GET /config` afterwards.

## Files

```
searxng_mcp/
├── server.py               # MCP 2.0 MCPServer + tools (search, fetch_content)
├── searxng_client.py       # SearXNG JSON API client + formatting
├── fetcher.py              # trafilatura → bs4/html2text → Wikipedia fetcher
├── pyproject.toml          # package: searxng-mcp, entry point: searxng-mcp
├── Dockerfile
├── searxng/settings.yml    # reference settings (bare docker / docs)
├── helm/                   # chart: configmap, sidecar deployment, service, VS
│   └── local/              # per-site values (never committed)
└── tests/
    ├── test_searxng_mcp.py # 30 unit tests (mocked transport, in-memory MCP)
    └── live_check.py       # end-to-end check vs a real instance
```
