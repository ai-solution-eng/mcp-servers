# Verification — logsearch-mcp

After a PCAI apply (or a `helm template` render + install), verify in this
order: reachability → MCP surface → a safe tool call → the policy. All
commands are read-only against the cluster.

## 1. Reachability

```bash
# Through the PCAI gateway (the path MCP clients use):
curl -s https://logsearch-mcp.<your-domain>/healthz
# -> {"status":"ok","server":"logsearch-mcp"}

# In-cluster (bypasses the gateway):
curl -s http://logsearch-mcp-service.<namespace>.svc.cluster.local:9101/healthz
```

## 2. MCP handshake

This server is MCP 2.0 stateless: there is no initialize handshake and no
`Mcp-Session-Id` header — any replica serves any request, and a plain
JSON-RPC `tools/list` works immediately:

```bash
curl -s https://logsearch-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect a result naming `count_matches`, `get_pod_logs`, `list_log_sources`,
`search_logs`. If a client registers the server as
`url: https://logsearch-mcp.<your-domain>/mcp` and lists those four tools,
the connection is good. (If the gateway enforces SSO/bearer auth, add
`-H "Authorization: Bearer <token>"`.)

## 3. One tool test

Start with the read-only discovery call in the release namespace — the chart
RBAC always covers it:

```bash
curl -s https://logsearch-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0", "id": 2,
    "method": "tools/call",
    "params": {"name": "list_log_sources", "arguments": {"namespace": "<release-namespace>"}}
  }'
```

Expect a JSON payload of pods with containers, restart counts, and ages. Then
exercise the fan-out: `count_matches` with a pattern that exists (e.g.
`Error`) in a namespace you are allowed to read, and confirm the result is
`{pod: count}` sorted descending with `pods_searched` set. The human path:
open the console at `https://logsearch-mcp.<your-domain>/` and run the same
search — the Sources/Search/Counts tabs hit the identical seams. If you get
`Error: namespace ... not allowed`, the namespace policy refused it; if the
policy allows it but pods come back as errors with 403-ish wording, the API
RBAC for that namespace is missing (see Troubleshooting).

## 4. Operator checks (optional)

Kubectl-level confirmation that the release is healthy — read-only verbs:

```bash
kubectl get deploy,po,svc -n <namespace> -l app=logsearch-mcp
kubectl get role,rolebinding,sa -n <namespace>   # read-only pods/pods-log RBAC
kubectl logs deploy/logsearch-mcp -n <namespace> --tail=50
# Startup output shows the effective policy knobs; /api/status shows them too:
curl -s https://logsearch-mcp.<your-domain>/api/status
```

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Error: namespace 'x' is not allowed` (names the policy env vars) | Namespace policy refused it | Adjust `logsearch.allowedNamespaces` / `blockedNamespaces` in the Helm Values editor, re-apply. Note `""` = ALL namespaces allowed. |
| Policy allows the namespace but pods fail with an API error (403) | The chart Role covers the release namespace only | Apply the one-time read-only RBAC bootstrap for the target namespace (see DEPLOYMENT.md) |
| Fan-out returns `truncated: true` | `max_total_lines` cap bit (by design — most recent matches kept) | Narrow the regex/pod filter, raise `logsearch.maxTotalLines`, or read one pod with `get_pod_logs` |
| Client hangs or 504 on `/mcp` | Gateway route missing (`ezua.enabled=false`) or timeout too small for wide fan-outs | Enable ezua with a literal endpoint; `timeout: 300s` default suits fan-outs |
| Console 404s at `/` but `/mcp` works | `webui.enabled=false` | Re-enable, re-apply |
| One pod errors while others return matches | Dead/crashed pod or missing logs — the fan-out completes per-pod by design | Retry that pod with `get_pod_logs` and `previous: true` |
| Matches look stale | Search tails each pod's CURRENT container only | Use `since_minutes` and remember crashed history lives in `previous` containers |
