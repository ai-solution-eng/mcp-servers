# applygate-mcp

The fleet's **governed Kubernetes write path**, shipped as Helm chart
`applygate-mcp` 0.5.0 for HPE Private Cloud AI (PCAI / Ezmeral Unified
Analytics). It is the deliberate, guarded complement to the fleet's read-only
k8s-mcp: server-side apply and delete of manifests into **allowlisted
namespaces**, behind a dry-run plan, an explicit confirm, a kind allowlist and
a tamper-evident audit trail.

## What it is

An **MCP server** — a Model Context Protocol endpoint that exposes typed tools
to LLM agents — speaking MCP 2.0 (stateless streamable-HTTP at `/mcp`, JSON
responses) over both stdio and HTTP. It publishes six tools: `plan_apply`,
`apply_manifest`, `plan_delete`, `delete_resource`, `get_resource_status`
and `verify_audit_chain`, plus a strictly read-only web console. Nothing in
it is a general Kubernetes client: the write surface is an allowlist of
namespaced kinds, and Secrets never pass through it.

> The guardrails ARE the product. When in doubt the server refuses loudly
> with a self-describing message that names the knob which would have allowed
> the operation — and why it is probably still a bad idea.

## The problem it solves

- **An agent that can apply anything is an outage with a tool schema.** Here
  the write surface is an allowlist of namespaced kinds in allowlisted
  namespaces, and `Secret` / cluster-scoped kinds are refused even if someone
  allowlists them.
- **"It said it applied it" is not evidence.** `plan_apply` is always a
  server-side dry-run, `apply_manifest` is sha256-bound to the exact bytes
  that plan validated, and `get_resource_status` reads the result back.
- **The dangerous verb is delete.** `plan_delete` shows what would be removed
  *and* the object's current state before `delete_resource` will act.
- **After an incident, nobody can say who changed what.** Every operation —
  including every refusal — appends a hash-chained JSONL line attributed to a
  non-secret caller fingerprint, and one call verifies the chain.
- **A fresh deployment should be harmless.** An empty namespace allowlist
  means *nothing is writable*: the pod runs, probes pass, and every write is
  refused until an operator enables a namespace in values.

## How it fits in PCAI

```
agent / read-only console ──▶ PCAI Istio gateway (optional, ezua.enabled)
                                │  /mcp (API key) · / (console, read-only)
                                ▼
                            applygate-mcp pod :9102
                                │  confirm → DNS-1123 → namespace policy
                                │  → manifest hygiene → D11 plan binding
                                │  → kind allowlist
                                ▼
                            Kubernetes API (namespaced writer Role)
                                │
                                └──▶ /data/audit.jsonl (PVC, hash-chained)
```

| Depends on | What it needs |
|---|---|
| Kubernetes API | the pod's ServiceAccount and the chart's namespaced writer Role; **plus a one-time per-namespace RBAC bootstrap** to write outside the release namespace |
| PCAI Istio gateway | optional — `ezua.enabled: true` + a unique `ezua.virtualService.endpoint`; without it the server is ClusterIP-only |
| Storage | a PVC for the audit trail (chart default: 1Gi, `ReadWriteOnce`) |
| Operator Secret | the API-key Secret, created out of band |
| Prometheus Operator CRDs | only if `metrics.enabled` |

**Depended on by** the ops agents as the only sanctioned way to change a
cluster: prometheus-mcp says *what* is degraded, k8s-mcp and logsearch-mcp say
*where* and *why*, and this server is the fix.

## Capabilities, by theme

Depth: [documentation/FEATURES.md](documentation/FEATURES.md).

**Plan before mutate — the central design decision.**
- **`plan_apply` never mutates**, regardless of flags: it is a server-side
  dry-run (`dry_run=All`) that returns per-document verdicts plus
  `manifest_sha256` — the sha256 of the exact planned bytes.
  [Tool contracts](documentation/FEATURES.md#1-the-tool-surface)
- **`apply_manifest` is bound to that plan** (fleet decision D11): it needs
  `confirm_apply=True` **and** a matching recorded plan. Missing plan or
  tampered bytes refuse by default; `APPLYGATE_UNPLANNED_APPLY=warn|allow` is
  the documented migration path, and unknown values fail closed.
  [Why binding, not just confirming](documentation/FEATURES.md#3-plan-binding-decision-d11-ratified-2026-09)
- **Deletes get the same discipline**: `plan_delete` shows the current state
  and never deletes; `delete_resource` needs `confirm_delete=True`.

**A write surface that cannot be widened by accident.**
- **Namespace policy, default-deny**: empty allowlist = every write refused.
  Blocklist always wins. `fnmatch` globs supported.
- **Kind allowlist, namespaced only**: the env knob can only *narrow* the
  built-in registry, never widen it, and an allowlisted-but-unknown kind is
  refused because its scope cannot be proven.
- **Secrets are hard-refused** on every tool — manifest Secret values would
  otherwise land in audit and log surfaces.
- **Alias-bomb-safe YAML**: an alias-limited loader plus a post-parse
  expansion budget; 8 documents / 256 KiB caps; YAML quines and over-deep
  nesting refused with dedicated messages.
  [The whole stack, in order](documentation/FEATURES.md#2-the-guardrail-stack-checked-on-every-write-in-order)

**An audit trail you can verify, not just read.**
- **Hash-chained JSONL** (`prev_sha256` per entry, genesis = 64 zeros):
  modifying, truncating or reordering a line breaks every later link.
- **Caller attribution that is never authorization**: a non-secret key
  fingerprint, the client address, an optional registry `name`, and — only
  from trusted-CIDR peers — the relayed `via` claim. None of them unlock
  anything.
- **One call to verify**: the `verify_audit_chain` tool, the console's
  `/api/audit/verify`, or the same function under kubectl exec.
  [Audit design and the verification procedure](documentation/FEATURES.md#4-audit-trail-hash-chain-and-caller-identity)

**A console that cannot mutate.**
- The HPE-branded console at `/` has **no** apply/delete endpoints — not even
  gated ones. Its plan tab rides the same always-dry-run `plan_apply` path,
  and a hand-crafted body posting `dry_run: false` is ignored by construction.
  [Console and JSON API](documentation/FEATURES.md#6-the-read-only-console)

**Operability.**
- **Opt-in `/metrics`** (`applygate_tool_calls_total{tool,outcome}`, labels
  carry no resource identity) plus a ServiceMonitor; default off keeps the
  render byte-identical.
- **Fleet transport security**: `mcpHostname` pins the public FQDN for
  DNS-rebinding protection and the chart auto-allows its own service DNS for
  the gateway's relay hop.
- **Audit trail on a PVC with `helm.sh/resource-policy: keep`** — the trail
  survives upgrades and uninstalls until an operator deletes it deliberately.

## Deploy on PCAI (one screen)

1. **Create the API-key Secret** in the target namespace (out of band — the
   chart never creates it):
   `kubectl -n <ns> create secret generic applygate-mcp-apikey --from-literal="api-keys=$(openssl rand -hex 32)"`
2. **Import the packaged chart** `applygate-mcp-0.5.0.tgz` into the PCAI
   catalog (once per chart version).
3. **Create the deployment** from the chart in that namespace.
4. **Set values** in the **Helm Values** editor: at minimum
   `namespaces.allowed` (empty = nothing writable) and, if you expose it
   through the gateway, `ezua.enabled: true` + `ezua.virtualService.endpoint`.
   Paste a shipped example from [`helm/values-examples/`](helm/values-examples/README.md)
   and adjust every `# SITE:` line.
5. **Apply, then verify** — `/healthz` must report
   `"namespaces_enabled": true` for the namespaces you allowed, and a
   `plan_apply` of a ConfigMap must come back `ok`.
   Checklist: [documentation/VERIFICATION.md](documentation/VERIFICATION.md).

Full procedure, the cross-namespace RBAC bootstrap and every failure mode:
**[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md)**.

## Prerequisites at a glance

Full purpose / failure-mode / verify detail is in
[DEPLOYMENT §2](documentation/DEPLOYMENT.md#2-prerequisites).

| Prerequisite | Kind | Why it exists | Fails how without it |
|---|---|---|---|
| `applygate-mcp-apikey` Secret, key `api-keys` (`apiKey.existingSecret`) | Secret you create | `/mcp` requires a key; comma-separated values are the rotation mechanism | Pod `CreateContainerConfigError`; nothing serves. The template has **no** name fallback — leave the default or set a real name |
| Per-namespace writer Role/RoleBinding | Cluster objects you apply | The release Role only grants its **own** namespace; writes elsewhere need a one-time admin bootstrap | Tool policy passes, API answers **403** (`rbac_hint` names the manifests) |
| Audit PVC (`<deployment.name>-audit`) | Chart-rendered; needs a working default StorageClass | The audit trail must survive restarts | Pod `Pending` on an unschedulable PVC; with `persistence.enabled: false` the trail lives in `emptyDir` and is lost on restart |
| Prometheus Operator CRDs | Cluster capability, only if `metrics.enabled` | Renders the `ServiceMonitor` | Apply fails on an unknown kind `monitoring.coreos.com/v1` |
| Istio gateway `istio-system/ezaf-gateway` | Cluster capability, only if `ezua.enabled` | The VirtualService attaches to it | No route at the public host |
| Single replica | Site rule | Plan-binding session state is per-process | Multi-replica: an apply can reach a replica that never saw the plan and refuse (re-plan is the remedy) |

## Day-2

- **Upgrade**: import the new chart version, re-apply the same values, apply.
  The audit PVC persists across upgrades.
- **Health check**: `kubectl -n <ns> get deploy,po,svc,pvc -l app=applygate-mcp`;
  `curl https://<endpoint>/healthz` → `{"status":"ok",…,"namespaces_enabled":true}`.
  `false` means the allowlist is empty: probes pass, writes do not.
- **Logs**: `kubectl -n <ns> logs deploy/applygate-mcp` — the startup banner
  prints the effective allowlist/blocklist and kind list.
- **Audit**: console Audit tab, `verify_audit_chain`, or
  `kubectl exec … python -c "…verify_audit_chain('/data/audit.jsonl')"`.
  Fleet convention: mount the same PVC into the logsearch pod so the trail is
  searchable.
- **Rotate keys**: update the Secret's `api-keys` (comma-separated list); the
  server re-reads the env per request, so no restart is needed.
- **Uninstall**: delete the PCAI deployment. The audit PVC carries
  `helm.sh/resource-policy: keep` and survives — delete it deliberately when
  you truly mean to destroy the trail.
- **Troubleshooting**: [DEPLOYMENT §6](documentation/DEPLOYMENT.md#6-troubleshooting)
  — default-deny refusals, D11 binding refusals, 403 vs policy pass, 421
  through the gateway, and server-side apply field-manager conflicts.

## Documentation map

| Document | The question it answers |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy this on PCAI, what must exist first, and what breaks if it does not? |
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is each guardrail shaped that way? |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | Is my deployment serving MCP, and do the guardrails behave? |
| [helm/values-examples/](helm/values-examples/README.md) | Paste-ready values (G2, hardened G2, hosted trial) |
| [helm/values.yaml](helm/values.yaml) | The authoritative list of every knob |
| [../README.md](../README.md) | The fleet, the shared pattern, and the gateway's federated MCP surface |

## Developing locally

**Not the deployment path.** PCAI deploys the packaged chart through its
values editor; these commands are for working on the server itself.

```bash
# stdio (default; for local MCP harnesses)
applygate-mcp

# streamable-http (in-cluster shape)
applygate-mcp --transport streamable-http --host 0.0.0.0 --port 9102
#   GET /healthz  -> {"status":"ok","namespaces_enabled":false,...}
#   POST /mcp     -> MCP 2.0 JSON-RPC (stateless)

python -m pytest tests/ -v     # no kubernetes package required
python tests/smoke_check.py    # real HTTP app over uvicorn
```

`namespaces_enabled: false` is the honest default-deny signal. The test venv
needs no `kubernetes` package: only the guardrail logic is under test.
