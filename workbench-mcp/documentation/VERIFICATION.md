# Verification — workbench-mcp

After a PCAI apply (or a `helm template` render + install), verify in this
order: reachability → MCP surface → one full workspace lifecycle → the
exec guardrails. All commands are read-only against the cluster.

## 1. Reachability

```bash
# Through the PCAI gateway (the path MCP clients use):
curl -s https://workbench-mcp.<your-domain>/healthz
# -> {"status":"ok","server":"workbench-mcp"}

# In-cluster (bypasses the gateway):
curl -s http://workbench-mcp-service.<namespace>.svc.cluster.local:9103/healthz
```

## 2. MCP handshake

This server is MCP 2.0 stateless: there is no initialize handshake and no
`Mcp-Session-Id` header — any replica serves any request, and a plain
JSON-RPC `tools/list` works immediately:

```bash
curl -s https://workbench-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect a result naming the ten tools: `delete_file`, `get_env`,
`list_files`, `read_file`, `run_command`, `set_env`, `workspace_create`,
`workspace_delete`, `workspace_list`, `write_file`. If a client registers
the server as `url: https://workbench-mcp.<your-domain>/mcp` and lists those
ten, the connection is good. (If the gateway enforces SSO/bearer auth, add
`-H "Authorization: Bearer <token>"`.)

## 3. One tool test (a full lifecycle, all reversible)

Create a workspace, write and read a file, run a command, then clean up —
exactly what an agent's first minutes look like:

```bash
MCP=https://workbench-mcp.<your-domain>/mcp
H='-H Content-Type:application/json -H Accept:application/json,text/event-stream'

curl -s $MCP $H -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
  "params":{"name":"workspace_create","arguments":{"name":"smoke-test"}}}'
curl -s $MCP $H -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"write_file","arguments":{"workspace":"smoke-test","path":"hello.txt","content":"hi"}}}'
curl -s $MCP $H -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
  "params":{"name":"run_command","arguments":{"workspace":"smoke-test","command":["cat","hello.txt"]}}}'
# expect exit_code 0 and stdout "hi"
curl -s $MCP $H -d '{"jsonrpc":"2.0","id":5,"method":"tools/call",
  "params":{"name":"workspace_delete","arguments":{"name":"smoke-test","confirm":true}}}'
```

Also confirm a guardrail fires: `run_command` with
`["curl","https://example.com"]` must refuse (`'curl' is deny-listed`), and
`write_file` with `path: "../escape.txt"` must refuse
(`path escapes the workspace`). The human path: the console at
`https://workbench-mcp.<your-domain>/` shows the same workspaces, files, env
vars, run results, and audit tail.

## 4. Operator checks (optional)

Kubectl-level confirmation that the release is healthy — read-only verbs:

```bash
kubectl get deploy,po,svc,pvc -n <namespace> -l app=workbench-mcp
kubectl get pvc workbench-mcp-data -n <namespace>   # RWX bound, 10Gi default
kubectl logs deploy/workbench-mcp -n <namespace> --tail=50
# Startup log names the endpoint and the workspace root.
kubectl get configmap ezaf-root-ca -n <namespace>   # only when caCert.enabled=true
```

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `command 'x' is not in the allowlist` | argv[0] not on `workbench.execAllowlist` (no shells by design) | Add the binary to the allowlist in values, re-apply — or use an allowlisted tool |
| `'curl' is deny-listed` (or sudo/su/ssh/...) | The hard denylist wins over the allowlist | Working as designed; adjust `workbench.execDenylist` only with intent |
| `path escapes the workspace` | Absolute path, `..` traversal, or symlink escape | Use workspace-relative paths (by design) |
| `workspace 'x' does not exist` | Never created, or typo | `workspace_create` first; `workspace_list` shows what exists |
| `content is N bytes; cap is ...` | `write_file` over 8 MiB | Write in chunks or raise `workbench.maxFileBytes` |
| `[workbench] timed out after Ns` | Command exceeded the timeout bound | Raise per-call `timeout_s` (clamped by `execTimeoutMax`, default 600 s) |
| Pod CrashLoopBackOff / PVC not writable | `fsGroup` mismatch or non-RWX class with `replicaCount > 1` | Keep `securityContext` (uid/fsGroup 10001); use RWX for multi-replica or RWO + `replicaCount: 1` |
| `pip` fails TLS inside `run_command` on a proxied cluster | Proxy/CA env not wired | `hpe_proxies: true` + `caCert.enabled: true` (with the `ezaf-root-ca` ConfigMap present in the release namespace) |
| Console 404s at `/` but `/mcp` works | `webui.enabled=false` | Re-enable, re-apply |
| Client hangs or 504 on `/mcp` | Gateway route missing (`ezua.enabled=false`) or command ran past the VS timeout | Enable ezua with a literal endpoint; keep `timeout: 660s` |
