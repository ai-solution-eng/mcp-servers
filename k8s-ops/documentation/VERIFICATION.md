# VERIFICATION — k8s-mcp

How to confirm a k8s-mcp deployment actually serves MCP. Replace `<endpoint>` with the value of `ezua.virtualService.endpoint` (e.g. `k8s-mcp.<your-domain>`) and `<API_KEY>` with the value of the out-of-band API-key Secret. Unlike the search MCP servers, **this server requires an API key on every request** (`Authorization: Bearer <key>` or `X-API-Key: <key>`).

## 1. Endpoint is alive and the gate works

```bash
# unauthenticated → 401 (the auth middleware rejects before MCP is reached)
curl -sS -o /dev/null -w '%{http_code}\n' "https://<endpoint>/mcp"

# authenticated → the MCP app answers (a 4xx other than 401 still proves routing)
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer <API_KEY>" "https://<endpoint>/mcp"
```

## 2. MCP handshake

This server is **MCP 2.0 (protocol `2026-07-28`) and stateless at the protocol layer** — no `initialize`/`initialized` exchange, no `Mcp-Session-Id`; every request carries its own envelope (`params._meta` with the protocol version and client capabilities), and header-based routing fields (`Mcp-Method`, `Mcp-Name`) must match the body. Official SDK clients do all of this automatically — point the client at `https://<endpoint>/mcp` with the auth header (see the README's connection snippet).

A curl-level `tools/list`, if you want to see the wire traffic:

```bash
curl -sS "https://<endpoint>/mcp" \
  -H "Authorization: Bearer <API_KEY>" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Expect 19 tool definitions (20 with exec enabled). `tools/list` carries `ttlMs=300000, cacheScope=public` — clients may cache it. Legacy 2025-era clients are served by the same process via the `initialize` handshake; nothing to configure.

## 3. One tool test

Call `cluster_health` — read-only, no namespace needed:

```bash
curl -sS "https://<endpoint>/mcp" \
  -H "Authorization: Bearer <API_KEY>" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: cluster_health' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"cluster_health","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Expect node/component health output. Follow with `list_pods` for a namespace you should be able to see — under an active namespace policy, a denied namespace must return the explanatory policy error (that is the governance working, not a fault). The built-in ops console (`https://<endpoint>/ui/`) is a quick interactive way to run the same calls: paste the API key in the login card and use the "Any tool" runner.

## 4. Operator checks (optional)

Operators with cluster access can verify in-cluster, bypassing the gateway:

```bash
kubectl -n <release-namespace> get pods -l app=k8s-mcp          # Ready 1/1
kubectl -n <release-namespace> logs deploy/k8s-mcp | head -5
# → "Loaded In-Cluster Service Account Config" and NO API-key warning

# the API key (operators only):
kubectl -n <release-namespace> get secret <deployment-name>-apikey \
  -o jsonpath='{.data.api-key}' | base64 -d

# audit trail — every exec allow/deny and RBAC provisioning action:
kubectl -n <release-namespace> logs deploy/k8s-mcp | grep AUDIT
```

## Troubleshooting

Quick hits — the full table (including exec-gate messages) is in [DEPLOYMENT.md](DEPLOYMENT.md#troubleshooting).

| Symptom | Cause / fix |
| --- | --- |
| 401 on every call | Missing/wrong API key header, or the client targets the wrong Secret's key. |
| Pod `CreateContainerConfigError` | The API-key Secret doesn't exist yet — create it out of band; the pod recovers on its own. |
| 421 responses through the gateway | `MCP_HOSTNAME` mismatch — the endpoint in values must equal the public host the client calls. |
| `tools/list` returns 400 `-32602` naming a `_meta` key | The client omits required envelope fields — use a current SDK client (or add the `_meta` block as in the curl above). |
| Tools return `403 Forbidden` | RBAC boundary — check `rbac.scope`, `extraResourceGroups`, and that no namespace policy filters the target. |
| Exec refused (list / label / allowlist / assignment) | The four gates must all agree — see [DEPLOYMENT.md](DEPLOYMENT.md#exec-in-certain-namespaces-opt-in); the error text names the failing gate. |
