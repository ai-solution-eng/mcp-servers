# SearXNG MCP Server

MCP 2.0 server for web search backed by a **self-hosted SearXNG** instance, bundled as a sidecar container. Drop-in replacement for `ddgs-lite`: same two tools (`search`, `fetch_content`), same output layout — but the metasearch part runs on SearXNG's stable JSON API instead of client-side scraping.

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

**Note:** TLS fingerprint impersonation is back for *fetching* — via `curl_cffi` (a maintained libcurl-impersonate binding), used as an escalation when a site 403s plain clients. Unlike ddgs, it impersonates only the TLS handshake for page fetches; search itself never scrapes — it rides SearXNG's JSON API, so no scraping library is in the search path.

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
- Output mirrors ddgs-lite (`Found N search results:` + numbered title/URL/Summary) plus `Answer:`, `Related searches:`, and a `Note: some engines did not respond: …` line when engines fail — so an agent can distinguish "no such thing" from "engines are down/suspended".

### `fetch_content`

```python
fetch_content(
    url: str,
    start_index: int = 0,      # pagination offset
    max_length: int = 8000,    # characters per call
    backend: str = "auto",     # auto|trafilatura|bs4 ('httpx' alias)|curl|wikipedia
    render: str = "auto",      # auto|always|never — headless-browser usage
    include_screenshot: bool = False,  # PNG data URL, first page only, size-capped
) -> str
```

Same output contract as ddgs-lite, including the `[Content info: Showing characters X-Y of Z total. Use start_index=… to see more (via …)]` pagination footer.

Fetch escalation: `auto`/`trafilatura`/`bs4` fetch with plain httpx first and automatically retry through **curl_cffi impersonating Chrome** when the site rejects plain python clients (TLS-fingerprint 403s — wikipedia does this from some egress paths). `curl` always uses the impersonated fetch; `wikipedia` goes straight to the Wikipedia API. Failures are self-describing — the error includes the
last attempt's HTTP status or exception.

**Headless-browser escalation** (when the sidecar is deployed — see the section below): `render="auto"` further escalates to a real Chromium render when the plain ladder fails outright or returns a JS-stub shell (near-empty text, `<noscript>` markers, challenge interstitials); the rendered DOM flows through the same trafilatura → bs4 extraction chain and the `(via headless-browser+…)` footer says
so. `render="always"` skips plain HTTP entirely; `render="never"` keeps the old plain-only behavior. `include_screenshot=True` forces a browser render and appends the page screenshot as a base64 PNG data URL — pass it to a vision tool as-is. Every browser failure degrades gracefully to the plain result with the reason logged; the sidecar is never a hard dependency.

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
| `SEARXNG_FETCH_ALLOW_HOSTS` | — | SSRF-guard escape (D6): hosts/CIDRs allowed despite resolving internally |
| `SEARXNG_FETCH_DENY_EXTRA` | — | SSRF-guard: extra denied hosts/CIDRs (deny wins over allow) |
| `SEARXNG_FETCH_CACHE_TTL` | `300` | Fetch result cache TTL seconds (`0` disables) |
| `SEARXNG_FETCH_MAX_BODY_BYTES` | `5000000` | Response-body cap on the plain path |
| `SEARXNG_FETCH_MAX_SCREENSHOT_KB` | `512` | Screenshot data-URL cap (KB) |
| `SEARXNG_FETCH_MAX_REDIRECTS` | `5` | Redirect-hop cap (each hop re-validated) |
| `BROWSER_CDP_URL` | `http://127.0.0.1:9222` | Headless-browser sidecar CDP endpoint |
| `BROWSER_NAV_TIMEOUT_MS` etc. | see `browser_client.py` | Nav timeout, settle wait, max pages, resource blocking |

The search client deliberately ignores environment proxies (`trust_env=False`): the sidecar is reached over `localhost`, and nothing must intercept that.

## fetch_content SSRF guard (default-ON — fleet decision D6)

`fetch_content` and the headless-browser rung sit behind a shared URL policy
(`url_policy.py`, adapted from the fleet reference implementation in
`MultimodalRAG/src/multimodal_rag/utils/url_policy.py`):

- **Scheme allowlist** — only `http://` and `https://`.
- **Resolved-IP denylist** — checked on the addresses a host resolves to,
  not just its name: loopback (`127/8`, `::1`), unspecified (`0.0.0.0/8`, `::`),
  RFC1918 (`10/8`, `172.16/12`, `192.168/16`), link-local (`169.254/16` —
  including the cloud-metadata endpoint `169.254.169.254` — and `fe80::/10`),
  unique-local (`fc00::/7`), CGNAT/benchmark/TEST-NET/multicast/reserved, and
  IPv4-mapped IPv6 after unwrapping. Name forms of the same targets are
  blocked too: `localhost`, cloud-metadata hostnames (`metadata.google.internal`,
  `metadata`, …) and cluster-local suffixes (`*.svc`, `*.cluster.local`).
- **Unconditional core** — loopback, pod-local and metadata targets are
  refused in every configuration. The allowlist below cannot unlock them;
  the browser sidecar shares the pod's network namespace, so navigating it
  at loopback would reach the MCP container and the unauthenticated CDP
  endpoint itself.
- **Per-hop redirect re-validation** — redirects are never followed blindly:
  the fetcher runs a manual hop loop (cap `SEARXNG_FETCH_MAX_REDIRECTS`) and
  re-runs the full policy on every `Location` before requesting it.
- **DNS-rebinding pin** — with no proxy env set, each hop connects to the
  IP address that was validated (original hostname preserved via `Host` +
  TLS SNI), so check-time ≠ fetch-time rebinding cannot reroute the request.
  When `HTTP(S)_PROXY` is configured the request keeps the original hostname
  (the corporate proxy performs egress DNS; the denylist still ran on the
  check-time resolution) — that residual is documented and bounded to
  proxied deployments.
- **CDP route filter** — the browser validates its top-level URL before
  `page.goto`, and its route handler aborts any in-render request
  (redirects, iframes, subresources) toward loopback/pod-local/metadata/
  cluster-internal targets.
- **Escapes** — `SEARXNG_FETCH_ALLOW_HOSTS` (comma-separated hostnames,
  `.suffix` forms, IP literals, CIDRs) adds legitimate internal targets;
  additive, never authoritative (public fetching keeps working).
  `SEARXNG_FETCH_DENY_EXTRA` denies on top; deny wins over allow.
  Errors name the governing env var.

**Before → after for agents:** public-web search/fetch behaves exactly as
before; fetching internal/loopback/metadata URLs used to succeed and now
returns a policy error naming `SEARXNG_FETCH_ALLOW_HOSTS`.

Quick wins in the same area: successful fetches are memoized for
`SEARXNG_FETCH_CACHE_TTL` seconds with single-flight coalescing (concurrent
identical requests share one upstream run); the plain-path response body is
capped (`SEARXNG_FETCH_MAX_BODY_BYTES`) with a truncation notice in the
output; the screenshot data URL is size-capped
(`SEARXNG_FETCH_MAX_SCREENSHOT_KB`) and ships with the first page
(`start_index=0`) only — it used to be appended, uncapped, after every
`max_length` slice (a response-size bypass); bs4 extraction prefers the
`lxml` parser when it is importable.

In-cluster operators set these via the chart's `fetch:` values (wired to the
env names above; empty = built-in defaults) or the generic `env:` list.

## Prometheus self-metrics (GET /metrics — default OFF)

The MCP server exports its own request counters at `GET /metrics`: one
counter family, `searxng_mcp_tool_requests_total{tool,outcome}` — per-tool
call counts with outcome ok|error for `search` and `fetch_content` (SSRF
policy refusals and engine failures surface as fetch/search errors). No
queries, URLs, or error text are exported. Chart-gated default-OFF:
`metrics.enabled: false` (the default) renders no env and no ServiceMonitor
and the server serves no `/metrics` route — the default pod is unchanged.
Set `metrics.enabled: true` (values) to enable; `/metrics` rides the MCP
container's port, is key-free (the auth gate protects only `/mcp`), and the
ServiceMonitor template (templates/servicemonitor.yaml) scrapes it via the
Prometheus Operator.

## Headless browser rendering (optional sidecar)

JS-only pages (SPAs, challenge interstitials) are invisible to plain HTTP fetching. The optional `browser` container — Playwright's Chromium headless shell — closes that gap. All containers in a pod share a network namespace, so the MCP server connects over `http://127.0.0.1:9222` with **no service, port, or policy changes** — and because CDP binds loopback only, nothing outside the pod can reach
it (CDP is unauthenticated code-execution-as-browser-user; on a cluster without NetworkPolicies this binding is not negotiable — never add `--remote-debugging-address=0.0.0.0`).

Build & push (separate image; the MCP image stays slim — only the ~40 MB playwright *client* package is added to it). The Playwright version is baked into the Dockerfile as the `ARG PLAYWRIGHT_VERSION` default, so no build-arg is needed; pass `--build-arg PLAYWRIGHT_VERSION=<ver>` only to override it without editing the file (e.g., once a version with official Debian-13 support lands):

```bash
docker buildx build \
  -t ghcr.io/ai-solution-eng/searxng-mcp-browser:v1.1.0 \
  mcp_servers/searxng_mcp/browser --push
```

Then enable per site: `browser.enabled: true` (+ resources) in the site values. The chart adds the container with CDP `json/version` exec probes, turns `proxy.https` into Chromium's `--proxy-server` (Chromium ignores `*_PROXY` env vars), and — with `browser.caCert.enabled` — mounts the corporate MITM CA ConfigMap into the container trust store at startup (Chromium honors neither `SSL_CERT_FILE`
nor `NODE_EXTRA_CA_CERTS`). Without the sidecar the server behaves exactly as before: escalation is simply unavailable and self-describes in tool output.

## Local development

```bash
uv venv --python 3.12 .venv          # or any python ≥3.10
uv pip install -e . pytest           # or: uv pip install -r <(sed ...) — see pyproject
.venv/bin/python -m pytest tests/ -v # unit tests (fully mocked, no network; the
                                     # optional 'browser' extra — playwright — is
                                     # needed only for the browser-rung tests)

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
# Build & push the MCP image (playwright client included via the browser extra)
docker buildx build -t ghcr.io/ai-solution-eng/searxng-mcp:v1.1.0 -f Dockerfile . --push

# Optional: build & push the headless-browser sidecar (see the section above)
docker buildx build -t ghcr.io/ai-solution-eng/searxng-mcp-browser:v1.1.0 \
    mcp_servers/searxng_mcp/browser --push

# Render & inspect, then install (per-site values from helm/local/)
helm template searxng-mcp helm/ -f helm/local/values.<site>.yaml
helm upgrade --install searxng-mcp helm/ -n searxng-mcp --create-namespace \
    -f helm/local/values.<site>.yaml
```

### API-key auth — OPTIONAL (fleet pattern)

By default `/mcp` runs open (the chart wires no key env) and relies on the
gateway / network position. Because the `fetch_content` tools can reach
in-cluster URLs, enabling the key gate is **recommended** beyond a lab. The
chart never inlines a key — pre-deploy the Secret, then point the values at
it (no pod restart needed; the server re-reads the env per request):

```bash
kubectl -n searxng-mcp create secret generic searxng-mcp-apikey \
  --from-literal="api-keys=$(openssl rand -hex 32)"
helm upgrade searxng-mcp helm/ -n searxng-mcp --reuse-values \
  --set apiKey.existingSecret=searxng-mcp-apikey
```

Keys are a comma-separated list (`api-keys=new,old`) — that is the rotation
mechanism (append → move clients → drop the old). The fleet-universal
`MCP_API_KEYS` env is honored too (either var works).

Chart contents:

- `deployment.yaml` — one pod: an init container that copies the settings ConfigMap into an `emptyDir` (SearXNG's entrypoint writes to `/etc/searxng`), the SearXNG sidecar (8080), the MCP server (9090), and — when `browser.enabled` — the headless-browser sidecar (loopback CDP).
- `configmap.yaml` — renders `settings.yml` from values: JSON format enabled, limiter off (no valkey needed), optional disabled-engine list (default: none), optional `outgoing.proxies` for the corporate proxy.
- `service.yaml` — ports `mcp` (9090) and `searxng` (8080).
- `virtualservice.yaml` — `/mcp` → 9090 (MCP), `/` → 8080 (**the normal SearXNG web page**), behind the EZAF gateway.
- `kyverno.yaml` — vendor label policy (same as ddgs-lite).

On HPE-network clusters set `hpe_proxies: true` — this wires the corporate proxy into both SearXNG's engine requests (settings.yml) and the MCP container's `fetch_content` egress (env), matching the ddgs-lite convention.

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
3. Flip the MCP client entry `mcp-duck-duck-go` (ddgs-lite) to the `mcp-searxng` entry above — tool names and signatures are unchanged, so agents keep working; only `backend` values change meaning (engine allowlist instead of ddgs backend names).
4. Decommission the ddgs-lite release when confident.

## SearXNG settings notes

- `search.formats` **must** include `json` — the MCP server uses `format=json`, and SearXNG returns 403 otherwise (the client surfaces a targeted hint when that happens).
- **Engine overrides go at the TOP LEVEL of settings.yml** (`engines:`), not under `search.engines:`. SearXNG's settings loader (`searx/settings_loader.py`) reads `user_settings['engines']` only — a `search.engines` key is silently ignored. (The pre-existing `searxng` namespace deployment made exactly this mistake: its ConfigMap "disabled" brave/duckduckgo/startpage, but `/config` showed them
  enabled and failing for 12 days.)
- **Sidecar image pinning.** The chart pins SearXNG to the dated tag that Docker Hub's `latest` resolved to when the chart was last touched (never the mutable `latest` tag itself — pods rescheduled later would silently get different engines/settings). To refresh the pin: look up the newest dated tag on Docker Hub, bump `searxng.image.tag` in values, and upgrade. For a fully immutable reference,
  use the digest form (`image: searxng/searxng@sha256:…`). The MCP server side is decoupled from SearXNG releases — it speaks the JSON API, whose schema has been stable for years — so sidecar upgrades are low-risk; validate with `GET /config` and one search call after upgrading.
- `server.limiter: false` keeps the limiter off for internal API use — no valkey/redis required. Add valkey only if you ever expose this publicly.
- **Default: no engines are disabled.** All default SearXNG engines stay on, including ddg/brave/startpage. SearXNG auto-suspends engines that fail repeatedly (transient, self-recovers) and the search tool reports `some engines did not respond: …` per query — so an engine being flaky costs a little latency, never correctness. If one proves *consistently* dead on a given network (startpage showed a
  hard CAPTCHA wall on the HPE proxy during testing), add it to `searxng.disabledEngines` in values and `helm upgrade`; verify with `GET /config` afterwards.

## Files

```
searxng_mcp/
├── server.py               # MCP 2.0 MCPServer + tools (search, fetch_content)
├── searxng_client.py       # SearXNG JSON API client + formatting
├── fetcher.py              # trafilatura → bs4/html2text → Wikipedia fetcher
│                           #   (+ headless-browser escalation rung, redirect
│                           #   hop loop, TTL cache/single-flight, body caps)
├── url_policy.py           # SSRF guard: scheme allowlist, resolved-IP
│                           #   denylist, DNS pin, allowlist/deny escapes
├── browser_client.py       # Playwright-over-CDP client for the browser sidecar
│                           #   (+ CDP route filter)
├── browser/                # headless-browser sidecar image (Chromium headless
│   ├── Dockerfile          #   shell, loopback CDP, CA/proxy entrypoint)
│   └── entrypoint.sh
├── pyproject.toml          # package: searxng-mcp, entry point: searxng-mcp
├── Dockerfile
├── searxng/settings.yml    # reference settings (bare docker / docs)
├── helm/                   # chart: configmap, sidecar deployment, service, VS
│   └── local/              # per-site values (never committed)
└── tests/
    ├── test_searxng_mcp.py # unit tests (mocked transport, in-memory MCP)
    ├── test_browser_render.py # escalation tests (stubbed browser client)
    ├── test_url_policy.py  # SSRF guard tests (denylist, hops, pin, cache, caps)
    └── live_check.py       # end-to-end check vs a real instance (+ sidecar)
```
