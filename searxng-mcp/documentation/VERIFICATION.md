# VERIFICATION — searxng-mcp

How to confirm a searxng-mcp deployment actually serves MCP. Replace `<endpoint>` with the value of `ezua.virtualService.endpoint` (e.g. `searxng-mcp.<your-domain>`). Remember the two paths: `/mcp` is the MCP server; the host root is the SearXNG web UI.

## 1. Endpoint is alive

```bash
# SearXNG web UI (VirtualService root) — expect 200 and an HTML page
curl -sS -o /dev/null -w '%{http_code}\n' "https://<endpoint>/"

# MCP endpoint — expect any status but 404 (405/406 for a GET against a POST-only path)
curl -sS -o /dev/null -w '%{http_code}\n' "https://<endpoint>/mcp"

# SearXNG JSON API health, straight through the pod (operator view):
#   GET /config must list enabled engines and formats including json
```

## 2. MCP handshake

This server is **MCP 2.0 (protocol `2026-07-28`) and natively stateless** — there is no `initialize`/`initialized` exchange and no `Mcp-Session-Id`; every request is self-contained. Point any MCP client at `https://<endpoint>/mcp` (see the README's connection snippet); official SDK clients send the per-request envelope automatically.

A curl-level `tools/list`, if you want to see the wire traffic — note the `_meta` envelope and the header-based routing fields:

```bash
curl -sS "https://<endpoint>/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Expect `search` and `fetch_content` definitions (JSON Schema 2020-12 input schemas). Requests without an `Mcp-Protocol-Version` header (or carrying a 2025-era version) fall through to the SDK's legacy streamable-HTTP path — `initialize` handshake plus `mcp-session-id` — so pre-2.0 clients work against the same endpoint unchanged.

## 3. One tool test

Call `search` and expect a `Found N search results:` block (plus an engine-status note if engines failed):

```bash
curl -sS "https://<endpoint>/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: search' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"HPE Private Cloud AI","max_results":3},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Then `fetch_content` with one of the returned URLs — expect markdown plus the `[Content info: … (via …)]` footer naming the fetch source. If the browser sidecar is enabled, a JS-heavy URL should show `(via headless-browser+…)`.

## 4. Operator checks (optional)

Operators with cluster access can bypass the gateway entirely:

```bash
kubectl -n <release-namespace> get pods -l app=searxng-mcp        # Ready (2/2, or 3/3 with the browser sidecar)
kubectl -n <release-namespace> logs deploy/searxng-mcp -c searxng-mcp | head
# → "SearXNG MCP Server initialized" banner (SearXNG URL, language, proxy state)

# In-cluster MCP and SearXNG UI:
curl -sS http://searxng-mcp-service.<release-namespace>.svc.cluster.local:9090/mcp -o /dev/null -w '%{http_code}\n'
curl -sS http://searxng-mcp-service.<release-namespace>.svc.cluster.local:8080/config | head -c 400
```

## 5. Memory bound (1.7.1 — anonymous-heap fix)

Before 1.7.1 the fetch result cache was bounded by entry COUNT only (64) and
TTL-expired entries were dropped only when their own key was requested again,
so a handful of large pages could pin hundreds of MB until the container hit
its limit (live: 269 MB -> 481 MB on day 10 of a 512 Mi limit; OOMKilled
2026-10-27). 1.7.1 bounds both caches by BYTES, sweeps expired entries on
insert, and refuses to cache an outcome larger than its per-entry cap (the
outcome is still returned to the caller).

| Env | Default | Meaning |
|---|---|---|
| `SEARXNG_FETCH_CACHE_MAX_BYTES` | 33554432 (32 MiB) | fetch cache payload budget |
| `SEARXNG_FETCH_CACHE_MAX_ENTRIES` | 64 | fetch cache entry cap |
| `SEARXNG_FETCH_CACHE_MAX_ENTRY_BYTES` | 2097152 (2 MiB) | largest fetch outcome cached |
| `SEARXNG_SEARCH_CACHE_MAX_BYTES` | 8388608 (8 MiB) | search cache payload budget |
| `SEARXNG_SEARCH_CACHE_MAX_ENTRIES` | 64 | search cache entry cap |
| `SEARXNG_SEARCH_CACHE_MAX_ENTRY_BYTES` | 262144 (256 KiB) | largest search response cached |

Verify the running pod: `kubectl -n <ns> get deploy searxng-mcp -o yaml`
(expect `memory: 1Gi` for the MCP container) and watch the CURVE, not one
sample:

```promql
container_memory_working_set_bytes{namespace="<ns>", container="searxng-mcp"}
```

Expected after 1.7.1: working set plateaus at roughly the ~80 MB import
baseline + at most 32 MiB of cached payload + transient parse spikes. It must
NOT keep climbing ~20 MB/day; a sustained climb means a bound is set too high,
not that the limit is too low. Unit-level proof:
`cd searxng_mcp && .venv/bin/python -m pytest tests/test_cache_bounds.py`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `curl` to `/mcp` returns 404 | Wrong endpoint host or the VirtualService was not rendered — verify `ezua.virtualService.endpoint` (unique full FQDN) and `ezua.enabled: true`. |
| `search` fails with a 403-from-SearXNG hint | SearXNG's `search.formats` lost `json` — the chart's ConfigMap always sets it; check that no custom settings override removed it. |
| Every engine "did not respond" | No internet egress from the SearXNG sidecar. On corporate-proxy networks set `proxy.http`/`proxy.https` (wires `outgoing.proxies` in `settings.yml` — `proxy: {}` keeps egress direct); verify with `GET /config` after re-apply. |
| One engine consistently fails | Expected noise if transient (SearXNG auto-suspends and recovers). If permanent (e.g. a hard CAPTCHA wall behind the corporate proxy), add it to `searxng.disabledEngines` and re-apply. |
| `fetch_content` cannot read a JS-only page and no browser footer appears | The browser sidecar is not enabled — set `browser.enabled: true` (image must exist), or use `render: "never"` consciously and accept plain-HTTP limits. |
| Browser sidecar stuck/not-ready | Probes exec a CDP `json/version` check inside the container — check `kubectl logs deploy/searxng-mcp -c browser`; a corporate-MITM network usually needs `browser.caCert.enabled: true`. |
| Screenshots come back blank/tiny | Resource blocking is on by design (images/media/fonts aborted for speed); this only affects rendering speed, but a page that IS an image may render empty. |
| SearXNG web UI at the host root shows broken links | Set `searxng.baseUrl: https://<endpoint>/` and re-apply. |
| Requests hang for very long turns | The VirtualService timeout is 660s by design; raise `ezua.virtualService.timeout` if needed. |
