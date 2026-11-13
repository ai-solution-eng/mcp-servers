# Features — searxng-mcp

Every capability this server has, with the design decision behind it.
Written for a reader who has never heard of searxng-mcp and only knows that
PCAI runs LLM applications. Start here if the README's one-paragraph
summary is not enough.

**MCP** — Model Context Protocol: an endpoint that exposes *tools* (typed
functions) to LLM agents over HTTP, so a model can call them the way it
would call an API. This server is one member of the PCAI MCP fleet; this
one owns **web search and page fetching** — the only fleet server whose
upstream is the public internet.

**SearXNG** — a self-hosted **metasearch** engine: it aggregates results
from many upstream engines (Wikipedia, Bing, …) server-side over a stable
JSON API. This chart bundles SearXNG as a sidecar container in the same
pod, so the search engine is part of the deployment, not an external
dependency.

---

## The niche: outbound web for agents

Everything else in the MCP fleet looks inward — pods, metrics, logs, the
data lake. Agents periodically need the outside: current documentation,
release notes, error-message folklore. This server is that window. Two
tools, deliberately:

- `search(query, ...)` — metasearch via the SearXNG sidecar.
- `fetch_content(url, ...)` — read one page as clean text.

It is a **drop-in replacement for the fleet's earlier `ddgs-lite`**: same
two tool names, same signatures, same output layout — but the metasearch
part runs on SearXNG's stable JSON API instead of client-side scraping.

**Why SearXNG instead of ddgs** — scraping engine endpoints client-side
(primp TLS impersonation) breaks on CAPTCHAs and markup changes;
SearXNG aggregates server-side over a documented API, reports per-engine
status in results (`unresponsive_engines`), lets the operator tune engines
in its own `settings.yml`, and covers categories beyond text (general,
news, images, videos, music, files, it, science).

---

## search

```python
search(query, max_results=10, region="", backend="auto",
       category="general", time_range="", safesearch=0, pageno=1)
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

**Philosophy: failure visibility over fake success.** A search that
silently returned 3 of 30 results teaches the agent nothing; naming the
unresponsive engines lets it retry with a narrower `backend` instead of
concluding the answer does not exist.

---

## fetch_content and the escalation ladder

```python
fetch_content(url, start_index=0, max_length=8000, backend="auto",
              render="auto", include_screenshot=False)
```

Same output contract as ddgs-lite, including the
`[Content info: Showing characters X-Y of Z total. Use start_index=… to see
more (via …)]` pagination footer. `start_index` paginates; `max_length`
bounds each call.

**The escalation ladder** — fetching tries rungs in order and reports which
rung produced the result:

1. **Plain httpx** with trafilatura → bs4/html2text extraction (the
   `auto`/`trafilatura`/`bs4` backends).
2. **curl_cffi impersonating Chrome** — when the site rejects plain python
   clients (TLS-fingerprint 403s — wikipedia does this from some egress
   paths). `curl_cffi` is a maintained libcurl-impersonate binding; unlike
   ddgs it impersonates only the TLS handshake for page fetches — search
   itself never scrapes, it rides SearXNG's JSON API, so no scraping library
   is in the search path. `backend="curl"` always uses the impersonated
   fetch; `backend="wikipedia"` goes straight to the Wikipedia API.
3. **Headless-browser render** (only when the sidecar is deployed):
   `render="auto"` escalates to a real Chromium render when the plain
   ladder fails outright or returns a JS-stub shell (near-empty text,
   `<noscript>` markers, challenge interstitials); the rendered DOM flows
   through the same trafilatura → bs4 extraction chain and the
   `(via headless-browser+…)` footer says so. `render="always"` skips plain
   HTTP entirely; `render="never"` keeps the old plain-only behavior.
   `include_screenshot=True` forces a browser render and appends the page
   screenshot as a base64 PNG data URL — pass it to a vision tool as-is.

Failures are self-describing — the error includes the last attempt's HTTP
status or exception. **Every browser failure degrades gracefully to the
plain result with the reason logged; the sidecar is never a hard
dependency.**

**Philosophy: escalate honestly, degrade gracefully.** The footer tells
the agent which rung answered, so it can reason about reliability; a
missing sidecar removes a rung, not the tool.

---

## Headless-browser rendering (optional sidecar)

JS-only pages (SPAs, challenge interstitials) are invisible to plain HTTP
fetching. The optional `browser` container — Playwright's Chromium headless
shell — closes that gap. All containers in a pod share a network namespace,
so the MCP server connects over `http://127.0.0.1:9222` (CDP — the Chrome
DevTools Protocol) with **no service, port, or policy changes** — and
because CDP binds loopback only, nothing outside the pod can reach it.
CDP is unauthenticated code-execution-as-browser-user; on a cluster without
NetworkPolicies this binding is not negotiable — never add
`--remote-debugging-address=0.0.0.0`.

The MCP image stays slim — only the ~40 MB playwright *client* package is
added to it; the Chromium itself lives in the separate sidecar image
(`browser/Dockerfile`, Playwright version baked in as the `ARG
PLAYWRIGHT_VERSION` default).

**Corporate-proxy specifics** — the chart turns `proxy.https` into
Chromium's `--proxy-server` (Chromium ignores `*_PROXY` env vars), and with
`browser.caCert.enabled` mounts the corporate MITM CA ConfigMap into the
container trust store at startup — Chromium honors neither `SSL_CERT_FILE`
nor `NODE_EXTRA_CA_CERTS`, and Chromium ≥ 105 does not read the Debian
system bundle, so the sidecar imports the mounted PEMs into the NSS DB
`~/.pki/nssdb`. See DEPLOYMENT.md for the live-verified G2 caveats.

---

## fetch_content SSRF guard (default-ON — fleet decision D6)

**SSRF** — Server-Side Request Forgery: an attacker (or a confused agent)
makes your server fetch an internal URL it should never touch. Because
`fetch_content` runs inside the cluster and can reach in-cluster URLs, the
fetch path (and the headless-browser rung) sits behind a shared URL policy
(`url_policy.py`, adapted from the fleet reference implementation in
`MultimodalRAG`):

- **Scheme allowlist** — only `http://` and `https://`.
- **Resolved-IP denylist** — checked on the addresses a host resolves to,
  not just its name: loopback (`127/8`, `::1`), unspecified (`0.0.0.0/8`,
  `::`), RFC1918 (`10/8`, `172.16/12`, `192.168/16`), link-local
  (`169.254/16` — including the cloud-metadata endpoint `169.254.169.254` —
  and `fe80::/10`), unique-local (`fc00::/7`),
  CGNAT/benchmark/TEST-NET/multicast/reserved, and IPv4-mapped IPv6 after
  unwrapping. Name forms of the same targets are blocked too: `localhost`,
  cloud-metadata hostnames (`metadata.google.internal`, `metadata`, …) and
  cluster-local suffixes (`*.svc`, `*.cluster.local`).
- **Unconditional core** — loopback, pod-local and metadata targets are
  refused in every configuration. The allowlist below cannot unlock them;
  the browser sidecar shares the pod's network namespace, so navigating it
  at loopback would reach the MCP container and the unauthenticated CDP
  endpoint itself.
- **Per-hop redirect re-validation** — redirects are never followed blindly:
  the fetcher runs a manual hop loop (cap `SEARXNG_FETCH_MAX_REDIRECTS`,
  default 5) and re-runs the full policy on every `Location` before
  requesting it.
- **DNS-rebinding pin** — with no proxy env set, each hop connects to the
  IP address that was validated (original hostname preserved via `Host` +
  TLS SNI), so check-time ≠ fetch-time rebinding cannot reroute the
  request. When `HTTP(S)_PROXY` is configured the request keeps the
  original hostname (the corporate proxy performs egress DNS; the denylist
  still ran on the check-time resolution) — that residual is documented and
  bounded to proxied deployments.
- **CDP route filter** — the browser validates its top-level URL before
  `page.goto`, and its route handler aborts any in-render request
  (redirects, iframes, subresources) toward loopback/pod-local/metadata/
  cluster-internal targets.
- **Escapes** — `SEARXNG_FETCH_ALLOW_HOSTS` (comma-separated hostnames,
  `.suffix` forms, IP literals, CIDRs) adds legitimate internal targets;
  additive, never authoritative (public fetching keeps working).
  `SEARXNG_FETCH_DENY_EXTRA` denies on top; deny wins over allow. Errors
  name the governing env var.

**Before → after for agents:** public-web search/fetch behaves exactly as
before; fetching internal/loopback/metadata URLs used to succeed and now
returns a policy error naming `SEARXNG_FETCH_ALLOW_HOSTS`.

**Philosophy: the guard is default-ON because the blast radius is the
cluster.** A fetch tool without an SSRF guard is an internal-network
scanner with an LLM on the front; the escape hatch exists for the legitimate
internal-target case and deny always wins.

---

## Caching, caps, and the upstream budget

**Searches are memoized** for `SEARXNG_SEARCH_CACHE_TTL` seconds (default
120, `0` disables) with single-flight coalescing (concurrent identical
requests share one upstream run); **successful fetches are memoized** for
`SEARXNG_FETCH_CACHE_TTL` (default 300). On a shared multi-tenant instance
this dedupes the duplicate queries several agent sessions produce — a
direct saving of the upstream per-IP engine budget. Both caches are
byte-bounded (`SEARXNG_*_CACHE_MAX_BYTES`, plus entry-count and per-entry
caps) and an entry larger than its per-entry cap is **returned but never
cached**, so a huge page cannot balloon resident memory. The plain-path
response body is capped (`SEARXNG_FETCH_MAX_BODY_BYTES`, default 5 MB) with
a truncation notice; the screenshot data URL is size-capped
(`SEARXNG_FETCH_MAX_SCREENSHOT_KB`, default 512) and ships with the first
page only — it used to be appended, uncapped, after every `max_length`
slice (a response-size bypass). bs4 extraction prefers the `lxml` parser
when importable.

**Philosophy: the cache is an upstream-budget tool first, a latency tool
second.** SearXNG's engines rate-limit by IP; a fleet of agents sharing one
egress IP shares one budget — dedupe is the only lever the server itself
controls.

### Upstream engines can rate-limit you (fallback guidance)

This is the one fleet server whose upstream can push back. What that looks
like and what to do:

- **Symptom:** `Note: some engines did not respond: …` lines; degraded
  result counts; CAPTCHA interstitials reported per-engine.
- **Why it happens:** upstream engines rate-limit or CAPTCHA-wall
  datacenter IPs — on a shared egress IP the whole cluster's searches draw
  on one budget.
- **Fallback guidance, in order:** (1) let SearXNG auto-suspension report
  the dead engines per query — it costs quota but stays honest; (2) disable
  the known corporate-egress casualties explicitly via
  `searxng.disabledEngines` (the chart default drops ddg/brave/startpage
  variants + `wikidata` — CAPTCHA walls and SPARQL timeouts behind the
  proxy) — on a shared egress IP every search pays the dead engines'
  request/timeout budget, so this is a quota save, not a latency nicety;
  (3) narrow the agent's `backend` to engines that work from your egress
  (`backend="wikipedia"`); (4) lean on the search cache — repeated queries
  within the TTL never touch the upstream at all. Names in
  `disabledEngines` must match engine names exactly (see the `engines:`
  array in `GET /config`); a wrong name is silently ignored.

---

## TLS and CA posture

- **Search side** (`SEARXNG_VERIFY_TLS`, default true) — TLS verification
  for https:// SearXNG URLs; the in-cluster sidecar is http://localhost,
  and the search client deliberately ignores environment proxies
  (`trust_env=False`): the sidecar is reached over localhost and nothing
  must intercept that.
- **Fetch side** — `FETCH_CA_BUNDLE` is a combined CA bundle (public roots
  + the egress proxy's CA) for TLS-intercepting egress (**Zscaler et al.**
  re-sign every certificate; without the proxy root, fetch_content dies
  with `curl: (60) unable to get local issuer certificate`). The chart's
  `caCert:` block builds it from a ConfigMap; verification stays ON — the
  bundle must carry public roots + the proxy's CA. `FETCH_TLS_INSECURE_FALLBACK`
  (default false) retries a certificate-verification failure ONCE
  unverified, output marked — last-resort for CA-rotation gaps, never for
  HTTP/DNS/policy failures.

---

## Configuration reference (env defaults)

The full knob surface, with defaults; the chart wires most of these from
`fetch.*` / `search.*` / `browser.*` values (empty = built-in default):

| Variable | Default | Meaning |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG base URL (the sidecar) |
| `SEARXNG_LANGUAGE` | `en-US` | Default language/region |
| `SEARXNG_TIMEOUT` | `10` | Search request timeout (s) |
| `SEARXNG_VERIFY_TLS` | `true` | TLS verification for https:// SearXNG URLs |
| `SEARXNG_REQUESTS_PER_MINUTE` | `30` | Search rate limit |
| `SEARXNG_SEARCH_CACHE_TTL` / `_MAX_BYTES` / `_MAX_ENTRIES` / `_MAX_ENTRY_BYTES` | `120` / `8388608` / `64` / `262144` | Search cache (TTL s; total bytes; entries; per-entry cap — larger entries are returned but never cached) |
| `FETCH_REQUESTS_PER_MINUTE` | `20` | Fetch rate limit |
| `FETCH_VERIFY_TLS` | `true` | TLS verification for fetched pages |
| `FETCH_CA_BUNDLE` | — | Combined CA bundle (public roots + egress proxy CA) for TLS-intercepting egress; passed as `verify=` to both fetch rungs (chart `caCert:` block builds it) |
| `FETCH_TLS_INSECURE_FALLBACK` | `false` | Retry a certificate-verification failure ONCE unverified, output marked |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` | — | Corporate proxy for `fetch_content` |
| `SEARXNG_FETCH_ALLOW_HOSTS` / `SEARXNG_FETCH_DENY_EXTRA` | — | SSRF-guard escape (D6): allowed internal hosts/CIDRs / extra denials (deny wins) |
| `SEARXNG_FETCH_CACHE_TTL` / `_MAX_BYTES` / `_MAX_ENTRIES` / `_MAX_ENTRY_BYTES` | `300` / `33554432` / `64` / `2097152` | Fetch cache bounds (same semantics as the search cache) |
| `SEARXNG_FETCH_MAX_BODY_BYTES` | `5000000` | Response-body cap on the plain path |
| `SEARXNG_FETCH_MAX_SCREENSHOT_KB` | `512` | Screenshot data-URL cap (KB) |
| `SEARXNG_FETCH_MAX_REDIRECTS` | `5` | Redirect-hop cap (each hop re-validated) |
| `BROWSER_CDP_URL` | `http://127.0.0.1:9222` | Headless-browser sidecar CDP endpoint |
| `BROWSER_NAV_TIMEOUT_MS` etc. | see `browser_client.py` | Nav timeout, settle wait, max pages, resource blocking |
| `MCP_HOSTNAME` | — | Public FQDN clients use to reach `/mcp` (DNS-rebinding Host pin; read at startup) |
| `MCP_EXTRA_ALLOWED_HOSTS` | — | Comma-separated in-cluster Host allowlist additions (verbatim or `host:*`); the chart auto-prepends the own service DNS |

## Configuration freeze — which envs are read when

The connection block is read **once at import/startup**: `SEARXNG_URL`,
`SEARXNG_LANGUAGE`, `SEARXNG_TIMEOUT`, `SEARXNG_VERIFY_TLS`,
`SEARXNG_REQUESTS_PER_MINUTE`, `FETCH_REQUESTS_PER_MINUTE`, and
`FETCH_VERIFY_TLS` configure the long-lived `SearXNGClient` /
`WebContentFetcher` objects — changing any of them requires a pod restart,
and so do `MCP_HOSTNAME` / `MCP_EXTRA_ALLOWED_HOSTS` (resolved once at
startup) and the cache/cap knobs (`SEARXNG_SEARCH_CACHE_*`,
`SEARXNG_FETCH_CACHE_*`, `SEARXNG_FETCH_MAX_BODY_BYTES`,
`SEARXNG_FETCH_MAX_SCREENSHOT_KB`, `SEARXNG_FETCH_MAX_REDIRECTS`,
`FETCH_CA_BUNDLE`, `FETCH_TLS_INSECURE_FALLBACK` — read when the
client/fetcher objects are constructed). **Re-read live (no restart
needed):** the API-key envs (`SEARXNG_API_KEYS` / `MCP_API_KEYS` — key
rotation via the Secret is live). Aligning the connection block with the
per-request re-read pattern is a deferred work item — do not assume a
changed `SEARXNG_URL` in a running pod does anything.

---

## Self-metrics (GET /metrics — default OFF)

`metrics.enabled: true` (values) serves `GET /metrics` on the MCP
container's port: one counter family,
`searxng_mcp_tool_requests_total{tool,outcome}` — per-tool call counts with
outcome ok|error for `search` and `fetch_content` (SSRF policy refusals and
engine failures surface as fetch/search errors). No queries, URLs, or error
text are exported. Default OFF renders no env and no ServiceMonitor; when
on, `/metrics` is key-free (the auth gate protects only `/mcp`), and the
ServiceMonitor scrapes it via the Prometheus Operator.

---

## API-key auth (optional, recommended beyond a lab)

By default `/mcp` runs open (the chart wires no key env) and relies on the
gateway / network position. Because `fetch_content` can reach in-cluster
URLs (gated by the SSRF guard, but still), enabling the key gate is
**recommended** beyond a lab. The chart never inlines a key — pre-deploy
the Secret, then point the values at it (no pod restart needed; the env
re-reads per request). Keys are a comma-separated list (`api-keys=new,old`)
— the zero-downtime rotation mechanism. The fleet-universal `MCP_API_KEYS`
env is honored too. `/health`, the SearXNG UI route, and the opt-in
`/metrics` stay key-free.

---

## Egress proxy wiring (both containers)

On corporate-proxy clusters (no direct internet egress) set
`proxy.http`/`proxy.https` (and usually `proxy.noProxy`) — each key is
wired into the pod only when non-empty, configuring BOTH SearXNG's engine
requests (settings.yml `outgoing.proxies`) AND the MCP container's
`fetch_content` egress (env). `proxy: {}` (the default) renders everything
off; on direct-egress systems a wrong proxy breaks DNS/egress, so leave it
empty. (The former `hpe_proxies` boolean flag was removed — chart ≥ 1.5.0.)

**Philosophy: one proxy block, both consumers.** Engines and fetches leave
the cluster the same way; configuring them separately is how one path ends
up working while the other silently times out.
