# VERIFICATION — ddgs-lite

How to confirm a ddgs-lite deployment actually serves MCP. Replace `<endpoint>` with the value of `ezua.virtualService.endpoint` (e.g. `ddgs-lite.<your-domain>`); the MCP path is always `/mcp`.

## 1. Endpoint is alive

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "https://<endpoint>/mcp"
```

Any HTTP status other than 404 (typically `405`/`406` for a GET against a POST-only MCP endpoint) proves the VirtualService routes and the pod answers. A 404 means wrong host/path — re-check `ezua.virtualService.endpoint`.

## 2. MCP handshake

Point any MCP client at `https://<endpoint>/mcp` (see the README's connection snippet). The server speaks the MCP streamable-HTTP transport at `/mcp` (with SSE also available there); official SDK clients run the protocol handshake automatically.

A curl-level handshake, if you want to see the wire traffic:

```bash
# initialize → the response carries an mcp-session-id header (streamable-http session)
curl -sS "https://<endpoint>/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Repeat the request with the returned `mcp-session-id` header plus `"method":"notifications/initialized"`, then `tools/list`. Modern MCP clients (protocol `2026-07-28`) send a self-contained per-request envelope instead — no session bookkeeping; the server serves both eras.

## 3. One tool test

Call `search` and expect a `Found N search results:` text block:

```bash
curl -sS "https://<endpoint>/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: <SESSION_ID>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"HPE Private Cloud AI","max_results":3}}}'
```

Then `fetch_content` with one of the returned URLs — expect markdown plus the `[Content info: Showing characters 0-… of … total]` footer. (Modern clients can send both calls without the session header.)

## 4. Operator checks (optional)

Operators with cluster access can bypass the gateway entirely:

```bash
kubectl -n <release-namespace> get pods -l app=ddgs-lite        # Ready 1/1
kubectl -n <release-namespace> logs deploy/ddgs-lite | head      # "DuckDuckGo MCP Server initialized" banner
curl -sS http://ddgs-lite-service.<release-namespace>.svc.cluster.local:9090/mcp -o /dev/null -w '%{http_code}\n'
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `curl` to `/mcp` returns 404 | Wrong endpoint host or the VirtualService was not rendered — verify `ezua.virtualService.endpoint` (must be a unique full FQDN) and that `ezua.enabled: true`. |
| `search` returns `Search failed: … DDGSException` on every query | No internet egress. On HPE-network clusters set `hpe_proxies: true` (and keep `proxy.*`); on open-internet clusters make sure it is `false` — a wrong proxy breaks DNS/egress. |
| `search` works but specific engines time out | Expected — engines fail individually and `auto` falls back to the others; persistent CAPTCHA walls are engine-specific, not a server fault. |
| `fetch_content` cannot read a JS-only site | By design — this server does not execute JavaScript. Use `searxng-mcp` (headless-browser escalation) for such pages. |
| Requests hang for very long turns | The VirtualService timeout is 660s by design; raise `ezua.virtualService.timeout` if agents run longer turns. |
| Pod pending / restart-looping | `kubectl describe pod -l app=ddgs-lite` — typically image pull (check `image.repository`/`tag`) or resources. |
