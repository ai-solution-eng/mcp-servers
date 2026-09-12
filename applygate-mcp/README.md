# applygate-mcp

applygate-mcp is a governed Kubernetes **write-path** MCP (Model Context
Protocol) server: it takes a YAML manifest and applies it to the cluster with
**server-side apply** — but only after a dry-run plan, only into
**allowlisted namespaces**, only for **allowlisted namespaced kinds**, and
only on an explicit per-call confirmation, with every operation appended to a
JSONL audit trail on a persistent volume. It is the deliberate write half of
the fleet's read-only Kubernetes MCP story, exposed over MCP 2.0
(stateless streamable-HTTP at `/mcp`) through the PCAI Istio gateway.

**What problem(s) it solves**

- LLM agents cannot safely touch clusters: the fleet's K8s MCP server is
  read-only by design, so agents can look but never fix. applygate-mcp is the
  guarded write path — guardrails are the product, and every refusal is a
  self-describing message naming the knob that would have allowed the
  operation (and why it is probably still a bad idea).
- Uncontrolled `kubectl apply` from an agent is a blast-radius problem:
  default-deny namespace policy (`namespaces.allowed` empty = nothing
  writable), a kind allowlist that can only narrow the built-in namespaced
  registry, hard refusal of `Secret` and all cluster-scoped kinds regardless
  of configuration, and a hard `confirm_apply` / `confirm_delete` gate.
- Blind writes are a trust problem: `plan_apply` is ALWAYS a server-side
  dry-run (`dry_run=All`), so the agent sees the per-document verdict — and
  the exact refusal text — before anything mutates.
- Untraceable mutations are an audit problem: one JSONL line per document per
  operation (`dry-run | applied | deleted | failed | refused`) on a PVC,
  plus a strictly read-only web console at `/` (plan previews, status,
  audit tail, effective policy — there are NO apply/delete endpoints, not
  even gated ones).
- One bad document silently blocking a multi-doc apply: documents are
  planned/applied **per document**; each reports its own outcome.

## Tools

All tools return a JSON string; refusals are structured
`{"ok": false, "refused": true, "error": "<self-describing>"}`.

| Tool | Mutates | Purpose |
|---|---|---|
| `plan_apply` | never (always `dry_run=All`) | Validate a manifest and predict the server-side apply — per-doc verdicts; also answers "would this be refused, and why". `force` is accepted for symmetry but can never turn a plan into a mutation. |
| `apply_manifest` | yes, per doc | The real write: server-side apply each document (`field_manager=applygate-mcp`). Refuses unless `confirm_apply=true`; run `plan_apply` first. |
| `delete_resource` | yes | Delete one allowlisted, namespaced resource. Refuses unless `confirm_delete=true`. |
| `get_resource_status` | never | Read-only status excerpt for one resource (Deployment/StatefulSet replica readiness, Job succeeded/failed, else phase + conditions) — verify what you applied. Same namespace/kind fence as the writes. |

Intended flow: `plan_apply` → read verdicts → `apply_manifest(confirm_apply=true)`
→ `get_resource_status` to verify.

## Architecture

A single Python service (FastMCP/Starlette, MCP 2.0 stateless, JSON
responses) that talks to exactly one backend: the **Kubernetes API server**,
reached in-cluster with the pod's ServiceAccount via a dynamic client.
Server-side apply uses `application/apply-patch+yaml` with field manager
`applygate-mcp`. The chart ships a **namespaced Role + RoleBinding in the
release namespace only** (never a ClusterRole/ClusterRoleBinding), so the
release's write identity is namespace-scoped by construction; writes into
other namespaces need a one-time operator-applied bootstrap Role per target
namespace (see [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md)).
The JSONL audit trail lands on a dedicated PVC. HTTP surface: `/mcp`
(MCP streamable-HTTP), `/health` + `/healthz`, and — when `webui.enabled` —
the read-only console at `/` and `/ui` with its `/api/*` JSON endpoints
(plan, status, audit, policy).

## Deploy on PCAI (HPE Private Cloud AI)

Import the packaged chart once into PCAI, then edit the chart's values in the
PCAI **Helm Values** editor and apply — you never run `helm install` or
`kubectl apply` for the deployment itself. Every `helm --set a.b=c`
corresponds 1:1 to a values key. PCAI resolves `${DOMAIN_NAME}` in the
editor on current builds; if your build does not, substitute the literal
cluster domain (the chart refuses an un-substituted placeholder only in
prometheus-mcp, but a wrong host here means the gateway route matches
nothing).

**Required values** (the chart default is a no-op writer on purpose —
enabling namespaces is an explicit, auditable act):

```yaml
namespaces:
  allowed: "team-alpha,mcp-demo"     # SITE: writable namespaces (globs OK) — empty = EVERY write refused
  blocked: "kube-system,kube-public,kube-node-lease"   # SITE: always wins over allowed
ezua:
  enabled: true                      # SITE: expose through the PCAI Istio gateway
  domainName: <your-domain>          # SITE: literal cluster domain
  virtualService:
    endpoint: applygate-mcp.<your-domain>   # SITE: /mcp -> MCP server; / -> read-only console
    istioGateway: istio-system/ezaf-gateway
    timeout: 120s
```

**Optional values** (chart defaults are sane): `kinds.allowed` (narrow the
write surface), `webui.enabled` (read-only console), `persistence.*` (audit
PVC, default 1Gi), `image.*` (repository/tag — kept in lockstep with the
chart by release tooling), `resources`, `securityContext` /
`podSecurityContext` / `containerSecurityContext`, `serviceAccount` /
`rbac.create`, `hpe_proxies` + `proxy.*` (inert here — the k8s API is
in-cluster), `kyverno.enabled` (vendor-label ClusterPolicy). Complete
paste-ready documents: [helm/values-examples/values.g2.yaml](helm/values-examples/values.g2.yaml)
and [helm/values-examples/values.hosted-trial.yaml](helm/values-examples/values.hosted-trial.yaml).

## Connect an MCP client

Any MCP client that speaks streamable-HTTP connects to `/mcp` (stateless —
no session header needed); humans use the read-only console at `/`:

```json
{
  "mcpServers": {
    "applygate-mcp": {
      "url": "https://applygate-mcp.<your-domain>/mcp"
    }
  }
}
```

Clients that want the transport spelled out accept `"type": "http"`
(Claude Code / Claude Desktop) or `"transport": "streamable-http"` (DSH
profile, opencode). In-cluster consumers can use the service DNS instead:
`http://applygate-mcp-service.<namespace>.svc.cluster.local:9102/mcp`.
Put real gateway auth in front of a write-path MCP — this chart ships no
auth template by design; rely on the ezaf-gateway's SSO/bearer enforcement.

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), ezua/Istio gateway exposure, cross-namespace RBAC bootstrap, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake + first tool test, optional operator kubectl checks, troubleshooting |
| [helm/values-examples/README.md](helm/values-examples/README.md) | What the example values files are, how to use them (PCAI editor or `helm -f`) |
