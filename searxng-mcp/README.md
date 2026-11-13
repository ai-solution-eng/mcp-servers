# searxng-mcp

Web search and page fetching for LLM agents over MCP — chart
`searxng-mcp` 1.7.1 for HPE Private Cloud AI (PCAI / Ezmeral Unified
Analytics), backed by a self-hosted SearXNG instance bundled as a sidecar
container.

## What it is

searxng-mcp is an **MCP server** — a Model Context Protocol endpoint that
exposes tools to LLM agents — with exactly two tools: `search` (web
metasearch) and `fetch_content` (read one page as clean text). It is a
drop-in replacement for the fleet's earlier `ddgs-lite`: same tool names,
same signatures, same output layout — but the metasearch part runs on
SearXNG's stable JSON API instead of client-side scraping. The pod carries
the whole stack:

```
┌────────────────────────── Pod (searxng-mcp) ─────────────────────────┐
│  ┌───────────────────┐   localhost:8080   ┌────────────────────────┐ │
│  │  searxng-mcp      │ ◄─────────────────► │  SearXNG (sidecar)     │ │
│  │  (port 9090)      │    JSON API         │  (port 8080)           │ │
│  │                   │                     │  engines ──► internet  │ │
│  │  tools: search,   │                     │  (via corporate proxy  │ │
│  │   fetch_content   │                     │   on HPE networks)     │ │
│  └───────────────────┘                     └────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
        ▲                                    ▲
        │ MCP (streamable-http)              │ normal SearXNG web page
   in-cluster: http://searxng-mcp-service.<ns>.svc.cluster.local:9090/mcp
   external:   https://searxng-mcp.<DOMAIN_NAME>/mcp
```

## The problem it solves

- **Agents need the outside world** — current docs, release notes, error
  folklore — but a harness shell has no browser. This is the fleet's
  outbound-web window; every other fleet server looks inward.
- **Client-side scraping breaks.** ddgs-lite scraped engine endpoints
  directly and died on CAPTCHAs and markup changes; SearXNG aggregates
  server-side over a documented JSON API and reports per-engine status
  (`unresponsive_engines`), so "no results" is distinguishable from
  "engines are down/suspended".
- **Half the web is invisible to plain HTTP.** JS-only pages, challenge
  interstitials, TLS-fingerprint walls: `fetch_content` escalates plain
  httpx → curl_cffi (Chrome TLS fingerprint) → a real headless-Chromium
  render (optional sidecar) → the Wikipedia API — and the footer says
  which rung answered.
- **An internal-fetch tool is an SSRF risk.** `fetch_content` runs inside
  the cluster, so its URL policy (fleet decision D6, default-ON) refuses
  loopback/pod-local/metadata/cluster-internal targets on every hop.
- **Upstream engines rate-limit shared IPs.** Result caches with
  single-flight dedupe repeated queries; the chart's default engine list
  drops the known corporate-egress casualties so dead engines stop burning
  the per-IP budget on every search.

## How it fits in PCAI

Deployed as chart `searxng-mcp` in its own namespace (e.g. `searxng-mcp`),
exposed through the PCAI Istio gateway (`ezaf-gateway`) next to the rest of
the MCP fleet. It depends on outbound egress (direct or corporate proxy),
the SearXNG sidecar (bundled), and the gateway route; nothing in the
cluster depends on it except the MCP clients that call it. In-cluster
registration (preferred — bypasses gateway auth), e.g. in the DSH profile:

```yaml
- id: mcp-searxng
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    transport: streamable-http
    serverName: searxng
    url: http://searxng-mcp-service.searxng-mcp.svc.cluster.local:9090/mcp
```

Or through the gateway (oauth2-proxy protected):
`url: https://searxng-mcp.<cluster-domain>/mcp`.

**Cutover from ddgs-lite:** deploy this chart to its own namespace, verify
the in-cluster URL answers, flip the MCP client entry `mcp-duck-duck-go` →
`mcp-searxng` (tool names and signatures unchanged, so agents keep working;
only `backend` values change meaning — engine allowlist instead of ddgs
backend names), then decommission the ddgs-lite release.

## Capabilities, by theme

**`search`** — metasearch over the SearXNG sidecar's JSON API: region codes
(ddgs-style and native), categories (general/news/images/videos/music/
files/it/science), `backend` as an engine allowlist, and honest failure
visibility — a `Note: some engines did not respond: …` line that
distinguishes "no such thing" from "engines are down". →
[FEATURES.md#search](documentation/FEATURES.md#search)

**`fetch_content` and the escalation ladder** — httpx → curl_cffi
(Chrome TLS fingerprint) → headless-Chromium render → Wikipedia API, with
the producing rung named in the footer; the browser sidecar is never a hard
dependency and every failure degrades gracefully. →
[FEATURES.md#fetch_content-and-the-escalation-ladder](documentation/FEATURES.md#fetch_content-and-the-escalation-ladder)

**Headless-browser sidecar (optional)** — Playwright Chromium bound to
loopback CDP only (unauthenticated code execution never leaves the pod),
screenshot support, corporate-proxy (`--proxy-server`) and MITM-CA (NSS DB)
wiring. →
[FEATURES.md#headless-browser-rendering-optional-sidecar](documentation/FEATURES.md#headless-browser-rendering-optional-sidecar)

**SSRF guard (fleet decision D6, default-ON)** — scheme allowlist,
resolved-IP denylist (metadata, RFC1918, cluster-local suffixes),
unconditional core, per-hop redirect re-validation, DNS-rebinding pin, CDP
route filter, and operator escapes (`SEARXNG_FETCH_ALLOW_HOSTS` /
`SEARXNG_FETCH_DENY_EXTRA`, deny wins). →
[FEATURES.md#fetch_content-ssrf-guard-default-on--fleet-decision-d6](documentation/FEATURES.md#fetch_content-ssrf-guard-default-on--fleet-decision-d6)

**Caching and the upstream budget** — search + fetch memoization with
single-flight coalescing, byte-bounded caches, body/screenshot caps; the
upstream engines CAN rate-limit a shared egress IP, and the fallback
guidance (auto-suspension → `searxng.disabledEngines` → narrow `backend` →
cache) is documented. →
[FEATURES.md#caching-caps-and-the-upstream-budget](documentation/FEATURES.md#caching-caps-and-the-upstream-budget)

**TLS/CA posture** — the search client ignores environment proxies by
design (localhost must not be intercepted); the fetch side takes a combined
CA bundle for TLS-intercepting egress, with a marked one-shot insecure
fallback as the last resort. →
[FEATURES.md#tls-and-ca-posture](documentation/FEATURES.md#tls-and-ca-posture)

**Config freeze** — which env vars re-read live (only the API-key envs) and
which need a pod restart (the connection block, the transport pair, and all
cache/cap knobs). →
[FEATURES.md#configuration-freeze--which-envs-are-read-when](documentation/FEATURES.md#configuration-freeze--which-envs-are-read-when)

**Self-metrics** — optional `searxng_mcp_tool_requests_total{tool,outcome}`
counters at `/metrics` (default OFF; outcomes only). →
[FEATURES.md#self-metrics-get-metrics--default-off](documentation/FEATURES.md#self-metrics-get-metrics--default-off)

**API-key auth** — optional by default, recommended beyond a lab (the
fetch tools can reach in-cluster URLs); comma-list rotation without
restart. →
[FEATURES.md#api-key-auth-optional-recommended-beyond-a-lab](documentation/FEATURES.md#api-key-auth-optional-recommended-beyond-a-lab)

## Deploy on PCAI (one screen)

1. **Prerequisites:** prove outbound egress from the cluster (§2.3 of
   DEPLOYMENT.md); pre-create the SearXNG session Secret (and beyond a lab
   the API-key Secret); on MITM-proxy clusters stage the CA ConfigMap(s)
   with the proxy's root.
2. **Import the chart once** into the PCAI catalog.
3. **Create the deployment** from the catalog entry and open the Helm
   Values editor.
4. **Paste values** — start from `helm/values-examples/values.g2.yaml` or
   `values.hosted-trial.yaml` (full documents), adjust the `# SITE:` lines,
   keep `${DOMAIN_NAME}` as-is.
5. **Apply and verify:** two-path liveness + MCP handshake + one search and
   one fetch per [documentation/VERIFICATION.md](documentation/VERIFICATION.md);
   after any sidecar bump also check `GET /config`.

Full procedure — Secrets, CA ConfigMaps, values, troubleshooting:
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

## Prerequisites at a glance

| Prerequisite | Purpose | What breaks without it |
|---|---|---|
| Outbound egress (direct or `proxy.*` values) | The server's entire purpose — engines and fetches both leave the cluster | Every search/fetch fails; prove from the cluster, not the laptop |
| SearXNG session Secret (`searxng.existingSecret`, key `secret`) | Seeds `SEARXNG_SECRET` (session/crypto) | Render error: `searxng.secretKey is required (SEARXNG_SECRET) — or set searxng.existingSecret` |
| Istio gateway + `ezua.virtualService.endpoint` (values) | The product path (`/mcp` + the SearXNG web UI at `/`); doubles as the `MCP_HOSTNAME` pin | VirtualService fails to render; unresolved `${DOMAIN_NAME}` matches nothing |
| API-key Secret (optional; recommended beyond a lab) | Key gate on `/mcp` | Server runs open with a loud warning (by design); referenced-but-missing Secret = `CreateContainerConfigError` |
| CA ConfigMap(s) with the proxy root (MITM-proxy clusters only) | TLS trust for fetches (MCP container) and browser renders (sidecar NSS DB) | `curl: (60) unable to get local issuer certificate` / `ERR_CERT_AUTHORITY_INVALID` |
| Upstream search engines | What `search` aggregates | Dead engines are reported per query; disable them explicitly to stop burning the shared rate budget |
| prometheus-operator CRDs (only with `metrics.enabled: true`) | ServiceMonitor scraping | Render fails on the unknown kind |

## Day-2

- **Upgrade** — edit values, re-apply (DEPLOYMENT.md §5); sidecar image
  bumps are low-risk (the JSON API schema has been stable for years) but
  validate `GET /config` + one search after.
- **Health** — `GET /health`; sidecar: `curl localhost:8080/config` from
  inside the pod shows JSON format enabled and the live engine list.
- **Refresh the SearXNG pin** — look up the newest dated tag on Docker Hub,
  bump `searxng.image.tag` in values, upgrade (never the mutable `latest`).
- **Rotate the API key** — append to `api-keys=new,old` in the Secret; env
  re-reads per request.
- **Uninstall** — delete the deployment from the PCAI UI; operator-created
  Secrets stay until deleted. No PVC exists to worry about.
- **Troubleshooting** — symptom → cause → fix table (the SearXNG
  `engines:`-at-top-level incident, the CA/NSS-DB chain, the config freeze
  and more): [DEPLOYMENT.md §6](documentation/DEPLOYMENT.md).

## Documentation map

| File | The question it answers |
|---|---|
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is each thing designed that way? |
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy, upgrade, and debug it the PCAI way — egress, Secrets, CA ConfigMaps, values? |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | How do I prove the deployment works (two-path liveness, handshake, tool tests)? |
| [helm/values-examples/README.md](helm/values-examples/README.md) | Which paste-ready values file matches my target, and what is in it? |
| [helm/values.yaml](helm/values.yaml) | Every chart key with its default and inline comments |
| [searxng/settings.yml](searxng/settings.yml) | Reference SearXNG settings (bare-docker / docs) |

## Developing locally

**Not the deployment path.** Run the package from a checkout against any
real SearXNG (or a bare-docker one):

```bash
uv venv --python 3.12 .venv          # or any python >=3.10
uv pip install -e . pytest
.venv/bin/python -m pytest tests/ -v # unit tests (fully mocked, no network;
                                     # the optional 'browser' extra is needed
                                     # only for the browser-rung tests)

# Live check against any real SearXNG:
SEARXNG_URL=https://searxng.example.com SEARXNG_VERIFY_TLS=false \
    .venv/bin/python tests/live_check.py

# stdio (Claude Desktop / inspector):
SEARXNG_URL=http://localhost:8080 .venv/bin/python -m searxng_mcp.server --transport stdio

# Bare-docker SearXNG for local testing (JSON format pre-enabled):
docker run --rm -p 8080:8080 \
    -v $PWD/searxng/settings.yml:/etc/searxng/settings.yml:ro \
    -e SEARXNG_BASE_URL=http://localhost:8080/ -e SEARXNG_SECRET=dev-secret \
    searxng/searxng:2026.9.8-3fdc6d753
```

Image builds (maintainers): the MCP image
(`ghcr.io/ai-solution-eng/searxng-mcp:v1.7.1`, playwright client included
via the browser extra) and the optional browser sidecar
(`searxng-mcp-browser:v1.7.1` from `browser/`) both carry the W7 build-time
guards — the permission neutralizer (`chmod -R a+rX`) and the import gate —
documented in DEPLOYMENT.md §7.
