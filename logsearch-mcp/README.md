# logsearch-mcp

logsearch-mcp is a read-only, **namespace-scoped Kubernetes pod-log search**
MCP (Model Context Protocol) server: it fans a regex out across every pod in
one namespace, tails each pod's logs with per-line `pod/container: `
provenance, merges matches chronologically, and enforces hard caps on pods
and lines so a noisy namespace can never flood an agent's context window.
It is the *logs* half of cluster observability over MCP 2.0 (stateless
streamable-HTTP at `/mcp`) behind the PCAI Istio gateway — Prometheus sees
metrics (rates, trends, alert state); this server sees what applications
actually printed (tracebacks, panics, OOM kills, crash loops).

**What problem(s) it solves**

- "Where is this error coming from?" across a whole namespace without
  `kubectl logs` ping-pong: `search_logs` greps every pod in one call with
  time bounds, `count_matches` ranks pods by match count, and both respect
  the namespace policy and caps.
- CrashLoopBackOff triage: `get_pod_logs` with `previous=true` reads the
  PREVIOUS (crashed) container — the first move when a pod keeps restarting
  and the current container has nothing to say.
- Context-window protection for agent harnesses: hard caps (`maxPods` /
  `maxLinesPerPod` / `maxTotalLines`) bound both API-server load and the MCP
  response size; when the cap bites, the MOST RECENT matches are kept and a
  `truncated` flag is set.
- Blast-radius control: READ-ONLY by design (every tool is `readOnlyHint`,
  no exec path exists), the only RBAC is `pods` get/list + `pods/log` get in
  the release namespace, and the namespace policy is the knob that bounds
  what an agent can reach — pod logs routinely contain sensitive strings.
- A human front-end over the same seams: the HPE-branded log-search console
  at `/` calls the same tool coroutines, policy, and caps — the UI gets no
  powers the tools don't have.

## Tools

All tools take a `namespace` first, enforce the namespace policy, and return
a JSON string; all are read-only.

| Tool | Purpose |
|---|---|
| `list_log_sources` | Pods + containers in one namespace with restart counts and age — the discovery call before searching. |
| `get_pod_logs` | Raw tail-bounded log fetch for ONE pod (`tail_lines`, `since_seconds`, `previous=true` for the crashed container). |
| `search_logs` | Fan-out regex search: tail of each pod (timestamps on), keep matching lines with `pod/container: ` provenance, merge chronologically, cap at `max_total_lines` (keeps the most recent; sets `truncated: true`). |
| `count_matches` | Per-pod match counts over each pod's tail, `{pod: count}` sorted descending — the call before reading full logs. |

Errors are self-describing strings (`Error: ...`), never tracebacks: a denied
namespace names the policy env vars, a dead pod is reported per-pod while the
rest of the fan-out completes. Empty results are empty results, not errors.

## Architecture

A single Python service (Starlette, MCP 2.0 stateless, JSON responses) that
talks to exactly one backend: the **Kubernetes API server**, reached
in-cluster with the pod's ServiceAccount. The chart ships a minimal
namespaced Role + RoleBinding — `pods` get/list and `pods/log` get in the
**release namespace only**, never a ClusterRole, never secrets/configmaps,
never `pods/exec`. Reading logs in OTHER namespaces requires a one-time
operator-applied read-only bootstrap Role per target namespace (see
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md)). HTTP surface:
`/mcp` (MCP streamable-HTTP), `/health` + `/healthz`, and — when
`webui.enabled` — the console at `/` and `/ui` with `/api/*` endpoints
(status, sources, search, counts).

## Deploy on PCAI (HPE Private Cloud AI)

Import the packaged chart once into PCAI, then edit the chart's values in the
PCAI **Helm Values** editor and apply — you never run `helm install` or
`kubectl apply` for the deployment itself. Every `helm --set a.b=c`
corresponds 1:1 to a values key. PCAI resolves `${DOMAIN_NAME}` in the
editor on current builds; if your build does not, substitute the literal
cluster domain (an unresolved placeholder registers a gateway host that
matches nothing).

**Required values** (technically the chart boots with empty policy — but an
empty allowlist means ALL namespaces are readable, so set it deliberately):

```yaml
logsearch:
  allowedNamespaces: "team-alpha,team-beta,mcp-demo"   # SITE: "" = ALL namespaces readable — tighten!
  blockedNamespaces: "kube-system,kube-public,kube-node-lease"   # SITE: always wins
ezua:
  enabled: true                      # SITE: expose through the PCAI Istio gateway
  domainName: <your-domain>          # SITE: literal cluster domain
  virtualService:
    endpoint: logsearch-mcp.<your-domain>   # SITE: /mcp -> MCP server; / -> web console
    istioGateway: istio-system/ezaf-gateway
    timeout: 300s
```

**Optional values**: `logsearch.maxPods` / `maxLinesPerPod` /
`maxTotalLines` (caps), `webui.enabled` (console), `image.*` (kept in
lockstep with the chart by release tooling), `resources`,
`securityContext`, `rbac.create`, `hpe_proxies` + `proxy.*` (inert — the
k8s API is in-cluster). Complete paste-ready documents:
[helm/values-examples/values.g2.yaml](helm/values-examples/values.g2.yaml)
and
[helm/values-examples/values.hosted-trial.yaml](helm/values-examples/values.hosted-trial.yaml).

## Connect an MCP client

Any MCP client that speaks streamable-HTTP connects to `/mcp` (stateless —
no session header needed); humans use the log-search console at `/`:

```json
{
  "mcpServers": {
    "logsearch-mcp": {
      "url": "https://logsearch-mcp.<your-domain>/mcp"
    }
  }
}
```

Clients that want the transport spelled out accept `"type": "http"`
(Claude Code / Claude Desktop) or `"transport": "streamable-http"` (DSH
profile, opencode). In-cluster consumers can use the service DNS instead:
`http://logsearch-mcp-service.<namespace>.svc.cluster.local:9101/mcp`.

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), namespace policy, ezua/Istio gateway, cross-namespace RBAC bootstrap, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake + first tool test, optional operator kubectl checks, troubleshooting |
| [helm/values-examples/README.md](helm/values-examples/README.md) | What the example values files are, how to use them (PCAI editor or `helm -f`) |
