# Verification — applygate-mcp

After a PCAI apply (or a `helm template` render + install), verify in this
order: reachability → MCP surface → a safe tool call → the guardrails. All
commands are read-only against the cluster.

## 1. Reachability

```bash
# Through the PCAI gateway (the path MCP clients use):
curl -s https://applygate-mcp.<your-domain>/healthz
# -> {"status":"ok","server":"applygate-mcp","namespaces_enabled":true,...}

# In-cluster (bypasses the gateway):
curl -s http://applygate-mcp-service.<namespace>.svc.cluster.local:9102/healthz
```

`"namespaces_enabled": false` means the allowlist is empty: probes pass but
EVERY write is refused (default-deny) — fix `namespaces.allowed` in the Helm
Values editor and re-apply.

## 2. MCP handshake

This server is MCP 2.0 stateless: there is no initialize handshake and no
`Mcp-Session-Id` header — any replica serves any request, and a plain
JSON-RPC `tools/list` works immediately. List the tool surface:

```bash
curl -s https://applygate-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect a result naming `apply_manifest`, `delete_resource`,
`get_resource_status`, `plan_apply`. If a client registers the server as
`url: https://applygate-mcp.<your-domain>/mcp` and lists those four tools,
the connection is good. (If the gateway enforces SSO/bearer auth, add
`-H "Authorization: Bearer <token>"`.)

## 3. One tool test (safe by construction)

`plan_apply` is ALWAYS a server-side dry-run (`dry_run=All`) — nothing is
created, so it is the natural first test:

```bash
curl -s https://applygate-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0", "id": 2,
    "method": "tools/call",
    "params": {"name": "plan_apply", "arguments": {
      "namespace": "mcp-demo",
      "manifest": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: applygate-smoke\n  namespace: mcp-demo\ndata:\n  smoke: \"1\"\n"
    }}
  }'
```

Expect `"ok": true`, `dry_run: true`, and the per-doc verdict
`"dry-run passed — would be applied; nothing was changed"`. To exercise the
confirm gate, re-call as `apply_manifest` without `confirm_apply` — expect
`{"ok": false, "refused": true, "error": "confirm_apply is False — refusing
to mutate..."}`. The human path: open the read-only console at
`https://applygate-mcp.<your-domain>/`, paste the same manifest in the Plan
tab (previews are always dry-run), and check the Audit tab for the new lines.

## 4. Operator checks (optional)

Kubectl-level confirmation that the release is healthy — read-only verbs:

```bash
kubectl get deploy,po,svc,pvc -n <namespace> -l app=applygate-mcp
kubectl get role,rolebinding -n <namespace>          # namespaced writer RBAC
kubectl logs deploy/applygate-mcp -n <namespace> --tail=50
# the startup banner prints the effective allowlist/blocklist and kinds
kubectl exec -n <namespace> deploy/applygate-mcp -- cat /data/audit.jsonl 2>/dev/null | tail -5
# (exec requires k8s-mcp.io/exec="true" or RBAC your cluster may not grant —
#  the audit tail is also visible in the web console's Audit tab)
```

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `DEFAULT-DENY: APPLYGATE_ALLOWED_NAMESPACES is unset or empty` in every refusal | `namespaces.allowed` left empty (chart default) | Set the allowlist in the Helm Values editor, re-apply |
| Refusal: `namespace 'x' is not matched by APPLYGATE_ALLOWED_NAMESPACES` | Namespace not on the allowlist (or a glob misses it) | Add the namespace/pattern — the refusal names the current list |
| Refusal mentions `APPLYGATE_BLOCKED_NAMESPACES` | Blocklist always wins | Remove the pattern or pick another namespace |
| Refusal: `kind 'Secret' is hard-refused` / `cluster-scoped` | Working as designed | Manage Secrets out-of-band; cluster-scoped objects are never writable here |
| Tool policy passes but the API answers 403 | Target namespace has no writer Role for this ServiceAccount | Apply the one-time cross-namespace RBAC bootstrap (see DEPLOYMENT.md) |
| Client hangs or 504 on `/mcp` | Gateway route missing (`ezua.enabled=false`) or timeout too small | Enable ezua with a literal endpoint; `timeout: 120s` suits plan/apply |
| Console 404s at `/` but `/mcp` works | `webui.enabled=false` | Re-enable, re-apply |
| Audit lines vanish after a restart | `persistence.enabled=false` (emptyDir) | Enable the audit PVC (default 1Gi) |
