# logsearch-mcp

Read-only, namespace-scoped Kubernetes pod-log **search** over MCP —
chart `logsearch-mcp` 0.4.0 for HPE Private Cloud AI (PCAI / Ezmeral
Unified Analytics). The logs half of cluster observability, packaged for
LLM agents.

## What it is

logsearch-mcp is an **MCP server** — a Model Context Protocol endpoint that
exposes tools to LLM agents — that turns the Kubernetes log API into five
agent-shaped tools: fan-out regex search across a namespace's pods with
time bounds, per-line provenance, and hard caps. **READ-ONLY by design:**
every tool except `export_matches` declares `readOnlyHint=True`; the only
RBAC it needs is `pods` get/list + `pods/log` get. No secrets, no exec, no
write verbs, anywhere. `export_matches` is the one honest exception
(`readOnlyHint=False`): it writes ONLY the export file into the
operator-configured `LOGSEARCH_EXPORT_ROOT` directory (opt-in — unset by
default, the tool refuses). The cluster side stays read-only.

## The problem it solves

- **You have agents that need to know what an application printed when it
  broke.** `search_logs` fans a regex out over a namespace's pods in
  parallel, tags every matching line with `podname/container: ` provenance,
  and merges results chronologically — so a distributed incident reads as
  one timeline instead of N interleaved pod dumps.
- **You have noisy namespaces.** Caps (`LOGSEARCH_MAX_PODS`,
  `MAX_LINES_PER_POD`, `MAX_TOTAL_LINES`, `MAX_LINE_CHARS`) bound what one
  call can return, and the budget is enforced DURING the fan-out — a noisy
  namespace can never blow up an agent's context window.
- **You don't know where an error is coming from.** `count_matches` returns
  per-pod counts sorted descending — the cheap "where?" before anyone
  reads full logs.
- **Your result sets are too big for a context window.** `export_matches`
  runs the exact search pipeline and writes the matches to a file the agent
  reads back (opt-in via `logsearch.exportRoot`).
- **You don't know which pods exist or which is crash-looping.**
  `list_log_sources` shows pods, containers, restart counts, age;
  `get_pod_logs(previous=true)` reads the PREVIOUS (crashed) container —
  the first move on a CrashLoopBackOff.

## How it fits in PCAI

Deployed as chart `logsearch-mcp` in its own namespace, exposed through the
PCAI Istio gateway (`ezaf-gateway`) next to the rest of the MCP fleet:

    browser / agents ──► ezaf-gateway (Istio) ──► logsearch-mcp :9101
                                                     │  pods get/list + pods/log get
                                                     ▼
                                              Kubernetes API (log reads)

- **Depends on:** the in-cluster Kubernetes API (its own service account's
  read-only grant); the API-key Secret you pre-create; PCAI's Istio gateway
  for external exposure.
- **Depends on it:** agent harnesses (the LLM gateway relays MCP calls) and
  humans via the bundled web console at `/`. It is also the SEARCH side of
  the fleet audit convention: other servers' audit JSONL becomes queryable
  here.
- **Sibling:** prometheus-mcp is the metrics half of the same story —
  rates, trends, alert state. This server is what the applications printed.

## Capabilities, by theme

**Search and discovery** — the tool surface. `list_log_sources` (discovery),
`get_pod_logs` (raw tail, previous-container reads), `search_logs`
(parallel fan-out regex with provenance + chronological merge),
`count_matches` (per-pod ranking). All namespaced; multi-namespace calls
evaluate the policy per resolved namespace and report refusals instead of
silently merging. → [FEATURES.md#tool-catalog](documentation/FEATURES.md#tool-catalog)

**Context-window discipline** — the caps are the design: a single call can
never flood the model, because the budget (`LOGSEARCH_MAX_TOTAL_LINES`,
default 300) stops the pod-fetch pull mid-fan-out, and every response says
so (`truncated`, `pods_skipped_budget`). →
[FEATURES.md#context-window-discipline-the-caps](documentation/FEATURES.md#context-window-discipline-the-caps)

**Namespace policy (fleet decision D8 — default-deny)** — an unconfigured
server answers NOTHING; `LOGSEARCH_ALLOWED_NAMESPACES` is the access
control, `LOGSEARCH_BLOCKED_NAMESPACES` always wins, and
`LOGSEARCH_EMPTY_ALLOWS_ALL` is the deliberate escape hatch for trusted
labs. Pod logs are the most leak-prone surface on a cluster; "only
in-cluster callers" is not a control. →
[FEATURES.md#the-namespace-policy-fleet-decision-d8--default-deny](documentation/FEATURES.md#the-namespace-policy-fleet-decision-d8--default-deny)

**Regex safety** — user patterns are screened at compile time against
ReDoS shapes (`(a+)+` and friends would hold the GIL and freeze every
request); the rejection teaches the rewrite. →
[FEATURES.md#regex-safety-the-redos-screen](documentation/FEATURES.md#regex-safety-the-redos-screen)

**Exports** — `export_matches`, opt-in and default-off: exact search
pipeline, bare-filename path safety, atomic bounded writes; the fleet
convention shares the export root with workbench so agents read results
back as files. →
[FEATURES.md#exports-export_matches--opt-in](documentation/FEATURES.md#exports-export_matches--opt-in)

**Web console** — the HPE-branded log console at `/` has no extra powers:
its endpoints call the MCP tool coroutines themselves, so policy, caps, and
regex handling are byte-for-byte the server's own. →
[FEATURES.md#web-console](documentation/FEATURES.md#web-console)

**Self-metrics** — optional (`metrics.enabled`, default off) per-tool
request counters at `/metrics`; outcomes only, never log content. →
[FEATURES.md#self-metrics-get-metrics--default-off](documentation/FEATURES.md#self-metrics-get-metrics--default-off)

**API-key auth (mandatory)** — `/mcp` requires a key; the chart never
creates the Secret. Rotation is a comma-separated key list, re-read per
request. → [FEATURES.md#api-key-auth-mandatory--fleet-pattern](documentation/FEATURES.md#api-key-auth-mandatory--fleet-pattern)

**Read-only RBAC posture** — the same two rules (`pods` get/list +
`pods/log` get) as a namespaced Role (default) or an opt-in ClusterRole
(`rbac.clusterWide`); secrets, configmaps, exec, and write verbs are never
granted in any mode. → [FEATURES.md#rbac-posture](documentation/FEATURES.md#rbac-posture)

## Deploy on PCAI (one screen)

1. **Prerequisites:** create the API-key Secret in the target namespace
   (§2.1 of DEPLOYMENT.md) — without it the pod sits in
   `CreateContainerConfigError`. Decide the namespace allowlist now.
2. **Import the chart once** into the PCAI catalog (AI Essentials /
   EzAppConfig import).
3. **Create the deployment** from the catalog entry and open the Helm
   Values editor.
4. **Paste values** — start from `helm/values-examples/values.g2.yaml` or
   `values.hosted-trial.yaml` (full documents), adjust every `# SITE:`
   line, keep `${DOMAIN_NAME}` as-is.
5. **Apply and verify:** `/healthz` answers; `/api/status` shows the
   effective namespace policy + caps; one MCP handshake +
   `list_log_sources` call per [documentation/VERIFICATION.md](documentation/VERIFICATION.md).

Full procedure — secrets, patches, cluster objects, troubleshooting:
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

## Prerequisites at a glance

| Prerequisite | Purpose | What breaks without it |
|---|---|---|
| API-key Secret (`logsearch-mcp-apikey`, key `api-keys`) | `/mcp` auth — must never trust network position on a log surface | Pod `CreateContainerConfigError` |
| Read-only RBAC for its ServiceAccount (chart-rendered) | `pods` get/list + `pods/log` get | Every search answers 403 from the API |
| Per-namespace bootstrap Roles (admin-applied, only for cross-namespace reads) | Selective multi-namespace search without going cluster-wide | Allowed namespaces still answer 403 |
| Istio gateway + `ezua.virtualService.endpoint` (values) | The product path for external MCP clients | No VirtualService renders (`ezua.enabled: false`) — in-cluster access only |
| Namespace allowlist values | D8 default-deny: the agent-facing access control | Server answers nothing (by design) |
| Writable volume at `logsearch.exportRoot` (only for exports) | `export_matches` destination agents read back | Tool refuses (default OFF) — see DEPLOYMENT.md §7 chart gap |

## Day-2

- **Upgrade** — import the newer chart, re-apply your values document
  (DEPLOYMENT.md §5). Image tags flow from the chart.
- **Health** — `GET /health`, `/healthz`; the console's `/api/status`
  panel shows the effective policy and caps (the exact values the tools
  enforce).
- **Logs** — `kubectl -n <ns> logs deploy/logsearch-mcp` (its own stdout
  is searchable by itself, which is a fair smoke test).
- **Rotate the API key** — append to `api-keys=new,old` in the Secret; the
  env re-reads per request, no restart.
- **Uninstall** — delete the deployment from the PCAI UI; the
  operator-created Secret and bootstrap Roles stay until you delete them.
- **Troubleshooting** — symptom → cause → fix table:
  [DEPLOYMENT.md §6](documentation/DEPLOYMENT.md).

## Documentation map

| File | The question it answers |
|---|---|
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is each thing designed that way? |
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy, upgrade, and debug it the PCAI way — values, Secrets, patches? |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | How do I prove the deployment works (MCP handshake, first tool call)? |
| [helm/values-examples/README.md](helm/values-examples/README.md) | Which paste-ready values file matches my target, and what is in it? |
| [helm/values.yaml](helm/values.yaml) | Every chart key with its default and inline rationale |

## Developing locally

**Not the deployment path.** Local development runs the server directly:

```bash
# stdio (default — for local harness use)
logsearch-mcp

# streamable-http (the in-cluster deployment mode; stateless MCP 2.0)
logsearch-mcp --transport streamable-http --host 0.0.0.0 --port 9101
```

HTTP mode serves `/mcp` (stateless — any replica serves any request) plus
`/health`, `/healthz`, and — unless `LOGSEARCH_WEBUI_ENABLED=false` — the
web console at `/` and its `/api/*` endpoints. Tests run without a cluster:
the unit-test venv has `mcp` but no `kubernetes` package, so tests
monkeypatch the seam functions `server._list_pods` / `server._read_log`
directly (the kubernetes import stays lazy inside them):

```bash
cd /home/andrew/Code/HPE/mcp_servers/logsearch_mcp && \
/home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/ -v
```

Release/chores: `./automation.sh <version>` bumps chart + package versions,
builds and pushes `ghcr.io/ai-solution-eng/logsearch-mcp:v<version>`,
packages the chart, prunes stale archives. The tree is mirrored to
`pcai-solutions/mcp-servers/logsearch-mcp/` by the hardlinker.
