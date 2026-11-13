# k8s-mcp

A **read-only Kubernetes ops MCP server** for HPE Private Cloud AI (PCAI /
Ezmeral Unified Analytics), shipped as two Helm charts — `k8s-mcp` 1.3.0 for
trusted operators and `k8s-mcp-customer` 1.3.0-customer, a structurally locked
customer distribution. It gives LLM agents and humans one governed endpoint to
inspect a cluster, and it deliberately cannot change it.

## What it is

An **MCP server** — a Model Context Protocol endpoint that exposes typed tools
to LLM agents over JSON-RPC — speaking MCP 2.0 (protocol `2026-07-28`,
stateless streamable-HTTP at `/mcp`, server name `k8s-ops-server`). It
publishes 19 read-only tools (20 with exec enabled) over pods, workloads,
events, logs, services, Istio VirtualServices, ConfigMaps, Secret *names*,
PVCs, CRDs and RBAC, plus a read-verb-only `kubectl` escape hatch, an opt-in
hardened `exec_in_pod`, and a built-in HPE ops console at `/ui/`. Every
request passes an API-key middleware, a namespace policy and a read-verb
allowlist before anything touches the cluster.

## The problem it solves

- **"Is anything wrong in the cluster?" needs a kubeconfig today.** One
  endpoint answers it — `triage` returns an attention-first namespace summary
  in a single call, and the drill-down tools go from pod to logs to events
  without handing anyone credentials.
- **Read-only by convention is not read-only.** kubectl runs as an argv list
  (no shell), a deny-by-default **read-verb allowlist** rejects every write
  path, connection-redirection and impersonation flags are refused, and the
  RBAC role has no Secret/RBAC/write verbs — so `get secret -o yaml` fails
  with 403 **from the API server**, not from tool formatting.
- **A cluster-wide read tool is an exfiltration tool if it can see
  everything.** Namespace allow/block policy (globs, blocklist always wins)
  filters cluster-wide results, rejects denied namespaces, and rewrites
  `-A` per allowed namespace — while RBAC stays the true boundary.
- **Debugging power is sometimes worth the risk — but only where it is
  earned.** `exec_in_pod` is opt-in, needs four independent gates to agree,
  and audits every allow *and* deny.
- **Two audiences, one image, opposite trust levels.** Internal operators
  want every knob as a value; a customer catalog must not be able to
  configure its way out of the security posture. Hence two charts, not a
  `lockdown` flag.

## How it fits in PCAI

```
agent / ops console ──▶ PCAI Istio gateway (ezaf-gateway)
                          │  https://<endpoint>/mcp   (API key)
                          ▼
                        k8s-mcp pod  :9090   /mcp + /ui/
                          │  namespace policy · read-verb allowlist · RBAC
                          ▼
                        Kubernetes API (read-only ClusterRole; no Secrets)
```

| Depends on | What it needs |
|---|---|
| Kubernetes API | the pod's ServiceAccount; a read-only ClusterRole (chart) or Role (namespace clamp) |
| PCAI Istio gateway | `ezua.enabled: true` + a unique `ezua.virtualService.endpoint` |
| Kyverno | the chart's pre-install `ClusterPolicy` stamps the `hpe-ezua/*` vendor labels PCAI expects |
| Operator Secret | the API-key Secret, created out of band |

**Depended on by** the ops agents: prometheus-mcp says *what* is degraded,
this server says *where* and *why*, and applygate (a separate server) is the
only sanctioned way to change anything.

## Capabilities, by theme

Each bullet is a design decision, not just a feature. Depth:
[documentation/FEATURES.md](documentation/FEATURES.md).

**Read surface — the cluster, honestly.**
- **19 always-on tools / 20 with exec**: `triage`, `cluster_health`, pod /
  workload / service / PVC / namespace listings, `get_pod_logs`,
  `get_events`, `get_resource` / `describe_resource`, API-resource and CRD
  discovery, `get_custom_resource`, `get_configmap`, `list_secrets` (names
  only), `list_virtual_services`, `check_rbac`, `run_kubectl`, and the
  opt-in `exec_in_pod`. The full table with parameters is in
  [FEATURES §1](documentation/FEATURES.md#1-the-tool-surface).
- **`triage(namespace, app="")`** answers "what needs attention here?" in one
  composite call, assembled **only** from this server's own governed read
  paths — so the refusal for a denied namespace is byte-identical to
  `list_pods`', and one triage call can never flood the context (bounded by
  `K8S_MCP_TRIAGE_MAX_LINES`, default 150). Attention first, so the cap can
  never hide the findings. [Why it is built that way](documentation/FEATURES.md#3-fleet-triage-one-call-one-governed-path)
- **`namespace` is required on `get_pod_logs`** — the old silent `default`
  fallback was removed because reading the wrong pod is worse than an error.
- **Listings are capped and say so**: `K8S_MCP_MAX_LIST_ITEMS` (100) and
  `K8S_MCP_MAX_EVENTS` (100) end output with a marker naming the env rather
  than silently truncating. [Performance model](documentation/FEATURES.md#7-reliability-performance-and-cost-control)

**Security — deny by default, at every layer.**
- **No shell, ever.** kubectl is invoked with `asyncio.create_subprocess_exec`
  and an argv list, so `&&`, `;`, backticks, `$()` and newlines arrive as
  inert tokens.
- **Read-verb allowlist** (`get, describe, logs, top, explain, api-resources,
  api-versions, cluster-info, version, auth, events`). The denylist era
  missed `run`, `exec`, `attach`, `cp`, `port-forward`, `proxy`, `debug`,
  `set`; an allowlist only has to be right once.
- **Least-privilege RBAC** — a custom read-only role, never `cluster-admin`;
  Secrets and RBAC objects excluded by design; CRD apiGroups extendable per
  operator and **never `["*"]`** (refused at render, because it would
  re-grant Secret reads).
- **API key on every request** (`Authorization: Bearer` or `X-API-Key`),
  constant-time compared, 401 before the MCP app. The key lives in a Secret
  you create — the chart never creates or inlines it.
- **Optional gateway identity** — an Istio `AuthorizationPolicy` (oauth2-proxy)
  adds PCAI tokens **on top of** the API key. A shared key authenticates the
  *key*, never a user.
  [Full security model](documentation/FEATURES.md#2-read-only-by-construction)

**Exec — power where it is earned.** `exec_in_pod` is not registered unless
`exec.enabled: true`, and then four gates must agree: exec namespace list →
general namespace policy → namespaced RoleBinding → per-pod label
`k8s-mcp.io/exec: "true"`. The binary allowlist is read-only introspection
(no shells, interpreters, `env`, or `curl`), the server's own pod is
off-limits, output is truncated, and every decision is audited.
[The eight layers](documentation/FEATURES.md#5-hardened-container-exec-opt-in)

**Governance — capabilities travel with the credential.**
- **Namespace policy**: `namespaces.allowed` / `namespaces.blocked`
  (comma-separated fnmatch globs, blocklist wins). The allowlist is the
  recommended posture; an empty allowlist is a blacklist-only policy that the
  server warns about at startup.
- **Per-user keys**: `clients.value` / `clients.existingSecret` maps keys to
  assignments (`name:key[:exec-ns-patterns];…`), so a user cannot widen their
  own scope. The shared key remains the deployment-wide ceiling, and the
  `X-Exec-Namespaces` request header can only narrow.
  [Governance detail](documentation/FEATURES.md#4-namespace-governance)

**Operations — the console and the metrics.**
- **Built-in ops console at `/ui/`** — a universal MCP client with curated
  screens. The shell is inert (no data, no key to load it) and every data
  request is a browser call to the same `/mcp` with the same key and the same
  guards, so **the console cannot do anything a client cannot**.
  [Console](documentation/FEATURES.md#10-built-in-ops-console)
- **Opt-in `/metrics`** (counters only, nothing cluster-derived) plus a
  Prometheus Operator `ServiceMonitor`; default off, so the default render is
  byte-identical to the pre-metrics baseline.
  [Observability](documentation/FEATURES.md#9-observability)
- **Optional network zone** — a default-off ingress allowlist that stops
  random in-cluster access while the browser path stays open.
  [Enable it](documentation/DEPLOYMENT.md#enabling-the-network-zone)

**Two charts, one image.** `helm/` exposes every knob as a value for trusted
operators. `helm-customer/` is the **structurally locked** twin: exec,
namespace policy, per-user keys and RBAC scope **do not exist in its values**
and its templates never read them, so pasting `exec: {enabled: true}` into the
PCAI frontend is inert. A values-controlled `lockdown` flag existed in chart
v0.2.0 and was removed in v0.2.2 — the customer owned the flag, so the guard
was advisory. [Which chart, and why](documentation/DEPLOYMENT.md#two-charts-one-image)

## Deploy on PCAI (one screen)

1. **Create the API-key Secret** in the target namespace (out of band — the
   chart never creates it):
   `kubectl -n <ns> create secret generic k8s-mcp-apikey --from-literal="api-key=$(openssl rand -hex 32)"`
2. **Import the packaged chart** — `k8s-mcp-1.3.0.tgz`, or
   `k8s-mcp-customer-1.3.0-customer.tgz` for a customer catalog — into PCAI
   once per chart version.
3. **Create the deployment from the chart** in the target namespace.
4. **Set values** in the chart's **Helm Values** editor: at minimum
   `ezua.virtualService.endpoint` (unique per release on the shared gateway).
   Paste a shipped example from [`helm/values-examples/`](helm/values-examples/README.md)
   or [`helm-customer/values-examples/`](helm-customer/values-examples/README.md)
   and adjust the `# SITE:` lines.
5. **Apply, then verify** — unauthenticated `/mcp` must return 401 and an
   authenticated `tools/list` must return 19 tools (20 with exec).
   Checklist: [documentation/VERIFICATION.md](documentation/VERIFICATION.md).

Full procedure, values walkthrough and failure modes:
**[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md)**.

## Prerequisites at a glance

Full purpose / failure-mode / verify detail is in
[DEPLOYMENT §2](documentation/DEPLOYMENT.md#2-prerequisites).

| Prerequisite | Kind | Why it exists | Fails how without it |
|---|---|---|---|
| `k8s-mcp-apikey` Secret, key `api-key` (default name; `apiKey.existingSecret` overrides) | Secret you create | Every request must authenticate; the chart never creates or inlines the key | Pod `CreateContainerConfigError` (`couldn't find key api-key in Secret`); no endpoint at all |
| `ezua.virtualService.endpoint` | Value (required when `ezua.enabled`) | Public host **and** `MCP_HOSTNAME` — the DNS-rebinding pin; must be unique per release | Render aborts (`Valid .Values.ezua.virtualService.endpoint is required !`); two releases on one host split traffic |
| `clients.existingSecret` (optional) | Secret you create | Per-user keys and per-user exec assignments | Unset = only the shared key works; a wrong key name fails the pod |
| `audit.persistence.existingClaim` (optional) | PVC you create | The exec audit trail must outlive the pod | Default `emptyDir`: the trail is lost on pod rescheduling |
| Kyverno CRDs | Cluster capability | The chart's pre-install `ClusterPolicy` stamps `hpe-ezua/*` vendor labels | Pre-install hook fails → the release does not install |
| Prometheus Operator CRDs (only if `metrics.enabled`) | Cluster capability | Renders the `ServiceMonitor` | Apply fails on an unknown kind `monitoring.coreos.com/v1` |
| Exec RBAC (only if `exec.enabled`) | Cluster object | `pods/exec` is granted only by namespaced RoleBindings | `exec_in_pod` answers 403 — check the startup provisioning summary |

## Day-2

- **Upgrade**: import the new chart version, keep the values document,
  re-apply. Exec RBAC re-provisions at startup.
- **Health check**: `kubectl -n <ns> rollout status deploy/<name>`; the logs
  print `Loaded In-Cluster Service Account Config` and no API-key warning.
- **Logs and audit**: `kubectl -n <ns> logs deploy/<name>`; every exec
  allow/deny and RBAC provisioning action is an `AUDIT` line
  (`| grep AUDIT`).
- **Rotate the key**: update the Secret, then `kubectl rollout restart`.
  [Procedure](documentation/DEPLOYMENT.md#api-key-rotation--audit-trail)
- **Change posture** (namespace policy, exec, per-user keys): values re-apply
  on the trusted chart; on the locked customer chart these are `kubectl set env`
  / manual RBAC actions, printed by the chart's `NOTES.txt`.
- **Uninstall**: delete the PCAI deployment; the API-key Secret and any audit
  PVC are yours and are not removed. Delete leftover exec RoleBindings if exec
  was enabled.
- **Troubleshooting**: [DEPLOYMENT §6](documentation/DEPLOYMENT.md#6-troubleshooting)
  — 401s, `CreateContainerConfigError`, 421 through the gateway, 403 on
  `list_virtual_services`, and every exec refusal.

## Documentation map

| Document | The question it answers |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy this on PCAI, what must exist first, and what breaks if it does not? |
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is it built this way? (tools, security model, triage, exec, console, performance) |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | Is my deployment actually serving MCP? |
| [helm/values-examples/](helm/values-examples/README.md) | Paste-ready values for the trusted chart (G2 lab, hardened G2) |
| [helm-customer/values-examples/](helm-customer/values-examples/README.md) | Paste-ready values for the locked customer chart (hosted trial) |
| [helm/values.yaml](helm/values.yaml) · [helm-customer/values.yaml](helm-customer/values.yaml) | The authoritative list of every knob |
| [../README.md](../README.md) | The fleet this server belongs to, and the shared pattern |

## Developing locally

**Not the deployment path.** PCAI deploys the packaged chart through its
values editor; the commands below are for working on the server itself.

```bash
export K8S_MCP_API_KEY=dev-secret        # unset = open endpoint + loud warning
python3 -m k8s_mcp.server                # streamable-http on :9090
python3 -m pytest -q                     # the whole tree, no cluster required
python3 smoke_console.py                 # real ASGI wiring, HTTP-level
python3 audit_forms.py                   # schema <-> console-form contract
```

Requires `mcp>=2.0.0,<3` and `kubernetes>=31.0.0` (see
[`requirements.txt`](requirements.txt)). To build the image:
`docker buildx build -t ghcr.io/ai-solution-eng/k8s-mcp:v<ver> . --push`.
Release tooling: `./automation.sh <version>` bumps both charts and their
`image.tag`, builds/pushes, packages and prunes.
Test and file inventory: [FEATURES §12](documentation/FEATURES.md#12-verification)
and [§15](documentation/FEATURES.md#15-files).
