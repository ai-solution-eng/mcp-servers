# applygate-mcp

**The GOVERNED Kubernetes WRITE-path MCP server.** The fleet's existing K8s
MCP server is read-only by design — it can look, never touch. This server is
the deliberate, guarded write half of that story: server-side apply
(`application/apply-patch+yaml`) of manifests into **allowlisted namespaces**
with **plan/confirm semantics**, a **kind allowlist**, **default-deny
namespace policy**, and a **JSONL audit trail**.

> The guardrails ARE the product. When in doubt the server refuses loudly
> with a self-describing message that names the knob which would have allowed
> the operation — and why it is probably still a bad idea.

MCP 2.0 (stateless, JSON responses) on both stdio and streamable-http.

---

## Tools

| Tool | Mutates | Guardrails enforced | Contract |
|---|---|---|---|
| `plan_apply(namespace, manifest, force=False)` | **never** (always `dry_run=All`) | namespace policy, kind allowlist, manifest hygiene | Per-doc `{kind, name, ok, message}` + `summary`; `readOnlyHint=True`. `force` is accepted for call-site symmetry but can never turn a plan into a mutation. |
| `apply_manifest(namespace, manifest, confirm_apply=False)` | yes, per doc | confirm gate → namespace policy → manifest hygiene → kind allowlist | Real server-side apply per document (`field_manager=applygate-mcp`); refuses loudly unless `confirm_apply=True`. `readOnlyHint=False, destructiveHint=True`. |
| `delete_resource(namespace, kind, name, confirm_delete=False)` | yes | confirm gate → namespace policy → kind allowlist | Deletes one allowlisted resource; audit-logged. `destructiveHint=True`. |
| `get_resource_status(namespace, kind, name)` | never | namespace policy, kind allowlist (same fence as writes) | Status excerpt: Deployment/StatefulSet ready-vs-total replicas, Job succeeded/failed, else raw phase/conditions. `readOnlyHint=True`. |

All tool results are `json.dumps` strings. Refusals are structured JSON:
`{"ok": false, "refused": true, "error": "<self-describing message>"}`.

### The intended flow

```
plan_apply(ns, manifest)        # dry-run: what WOULD happen, per doc
  └─ read the per-doc verdicts
apply_manifest(ns, manifest, confirm_apply=True)   # only after a clean plan
get_resource_status(ns, kind, name)                # verify what you applied
```

Multi-doc manifests are applied **per document**: a bad document never
silently blocks the good ones — every doc reports its own outcome, and every
doc (plan, applied, failed, refused) gets its own audit line.

## Guardrail stack (checked on every write, in order)

1. **Namespace policy — DEFAULT-DENY.** `APPLYGATE_ALLOWED_NAMESPACES`
   unset or empty means **nothing is writable** — the server starts, serves
   health, and refuses every write loudly. Comma-separated; `fnmatch` globs
   supported (`team-*`). `APPLYGATE_BLOCKED_NAMESPACES` **always wins** over
   the allowlist (`kube-system,kube-public,*-system`).
2. **Kind allowlist — namespaced kinds only.**
   `APPLYGATE_ALLOWED_KINDS` defaults to `ConfigMap, Service, Deployment,
   StatefulSet, Job, CronJob, Ingress, ServiceAccount, PodDisruptionBudget,
   HorizontalPodAutoscaler`. Cluster-scoped kinds (Namespace, ClusterRole,
   ClusterRoleBinding, PersistentVolume, StorageClass, …) are refused even if
   someone allowlists them. The env knob can only **narrow** the built-in
   namespaced-kind registry, never widen it — an allowlisted-but-unknown kind
   is refused because its scope cannot be proven.
3. **Secrets never flow through this server.** Kind `Secret` is hard-refused
   on every tool (plan/apply/delete/status), with a message pointing at
   out-of-band secret management (`kubectl create secret`, sealed-secrets,
   external-secrets) — manifest secret values would otherwise end up in
   audit/log surfaces.
4. **Manifest hygiene.** Multi-doc YAML (`yaml.safe_load_all`); empty
   documents rejected (a *trailing* `---` is tolerated as an end-of-docs
   marker, an empty doc *between* documents is rejected); every doc must
   carry `apiVersion`, `kind`, `metadata.name`; a doc-level
   `metadata.namespace` must equal the tool's namespace parameter; caps: **8
   documents**, **256 KiB**.
5. **Confirm gates.** Real mutation requires `confirm_apply=True` /
   `confirm_delete=True` — anything else refuses with instructions.
6. **Audit trail.** Best-effort JSONL append to `APPLYGATE_AUDIT_FILE`
   (default `/data/audit.jsonl`), one line per document per operation:
   `{"ts","tool","namespace","kind","name","dry_run","outcome"}` with
   `outcome` ∈ `dry-run | applied | deleted | failed | refused`. A broken
   audit sink never blocks an operation but always screams on stderr.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `APPLYGATE_ALLOWED_NAMESPACES` | *(empty)* | Namespace allowlist, comma-separated, globs OK. **Empty = every write refused (default-deny).** A loud warning prints at startup when empty. |
| `APPLYGATE_BLOCKED_NAMESPACES` | *(empty)* | Namespace blocklist — always wins over the allowlist. |
| `APPLYGATE_ALLOWED_KINDS` | `ConfigMap,Service,Deployment,StatefulSet,Job,CronJob,Ingress,ServiceAccount,PodDisruptionBudget,HorizontalPodAutoscaler` | Kind allowlist (narrow-only; Secret/cluster-scoped always refused). |
| `APPLYGATE_AUDIT_FILE` | `/data/audit.jsonl` | JSONL audit sink (mounted volume in the Helm chart). |

## RBAC requirements

The Helm chart ships a **Role + RoleBinding in the release namespace** —
namespaced-scoped by construction (never ClusterRoles), covering the same
resource surface as the kind allowlist across the `""` (core), `apps`,
`batch`, `networking.k8s.io`, `policy` and `autoscaling` apiGroups with
`get/list/watch/create/patch/update/delete`. In-cluster auth uses the pod's
ServiceAccount (or kubeconfig outside the cluster). To write into more
namespaces, deploy the chart (or copy the Role) there — do **not** widen the
identity to cluster scope.

## Trust model

- **Default-deny namespaces**: a fresh deployment without an explicit
  allowlist is a no-op writer, on purpose — enabling writes is an explicit,
  auditable values-file act.
- **Kind allowlist + registry**: the write surface is explicit and
  narrow-by-default; admission is defense-in-depth (Secret and
  cluster-scoped refusals fire even on operator misconfiguration).
- **Plan → apply flow**: the mutating tool demands an explicit
  `confirm_apply=True`; the plan tool can never mutate regardless of flags.
- **No Secrets ever**: no secret values are read, written, or deleted.
- **Audit trail**: every operation (including refusals) leaves a JSONL line
  on a persistent volume.
- **HTTP posture**: MCP 2.0 stateless (any replica serves any request, no
  session state), DNS-rebinding protection disabled per fleet convention —
  put real gateway auth in front if you expose `/mcp` through the Istio
  gateway (`ezua.enabled=true`).

## Run

```bash
# stdio (default; for local MCP harnesses)
applygate-mcp

# streamable-http (in-cluster)
applygate-mcp --transport streamable-http --host 0.0.0.0 --port 9102
#   GET /healthz  -> {"status":"ok","namespaces_enabled":false,...}
#   POST /mcp     -> MCP 2.0 JSON-RPC (stateless)
```

Health endpoints report `namespaces_enabled: false` loudly when the
allowlist is empty — probes pass, writes don't.

## Web UI (strictly read-only console)

The streamable-http server also serves a self-contained, no-build HPE-branded
console at `/` (and `/ui`) — the same visual language as the fleet's other
MCP consoles (green element mark, MetricHPE wordmark, dark default + light
theme with no-flash init and `localStorage` persistence). A visible banner
states the trust model: *Read-only console — mutations happen only through
the MCP tools with explicit confirm flags, behind gateway authn.*

**THE UI CAN NEVER MUTATE THE CLUSTER.** There are NO `/api` endpoints for
apply or delete — not even gated ones — and the plan console rides the exact
`plan_apply` code path, which is always a server-side-apply dry-run
(`dry_run="All"`). Even a hand-crafted request posting `dry_run: false` /
`confirm_apply: true` to `/api/plan` cannot flip it: those fields are ignored
by construction, and the k8s seam only ever receives `dry_run=True`.

| Tab | What it shows |
| --- | --- |
| **Plan** | The plan console: paste a manifest, pick a namespace, get the per-document `{kind, name, ok, message}` verdicts the `plan_apply` tool would give (dry-run passed / refused, with the exact refusal text). |
| **Status** | `get_resource_status` for one kind/name/namespace — same namespace policy + kind gates as the write tools; Deployment/StatefulSet replica summaries, Job counters, conditions. |
| **Audit** | Tail of the JSONL audit file (last N lines, parsed): every plan, apply, delete, failure and refusal, with the configured file path. The endpoint reads ONLY the configured `APPLYGATE_AUDIT_FILE` — client-selected paths are refused with 400. |
| **Policy** | The effective namespace allowlist/blocklist + kind allowlist (from the same config functions the tools read), the default-deny state, and the unconditional Secret / cluster-scoped refusal texts — so a human can see exactly why a plan was refused. |

### JSON API (all read-only)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Server status, `namespaces_enabled`, field manager, audit path, policy + caps |
| `GET /api/policy` | Effective allowlist/blocklist/kinds + hard-refusal texts |
| `POST /api/plan` | Plan console `{"namespace", "manifest"}` — ALWAYS a dry-run; returns the tool's per-doc verdicts |
| `GET /api/resource_status?namespace=&kind=&name=` | Status excerpt (tool-equivalent, guardrails included) |
| `GET /api/audit?lines=N` | Last N parsed audit entries — configured path only, traversal refused |

The console is gated by `webui.enabled` (values) → `APPLYGATE_WEBUI_ENABLED`
(env; unset = on, `false|0|no|off` strips `/`, `/ui` and `/api/*` while `/mcp`
and the health endpoints keep working). The UI asset is `ui/index.html`,
copied into `/app/ui` by the Dockerfile; `webui.py` also honors an
`APPLYGATE_WEBUI_HTML` override.

## Test

```bash
cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp
/home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/ -v
```

The suite (30 guardrail tests + 23 web-console tests) drives the async tools
via `asyncio.run` and the UI routes via Starlette's `TestClient`, with
monkeypatched seam fakes — the test venv needs **no `kubernetes` package**;
only the guardrail logic is under test. Covers: default-deny matrix (empty
allowlist, globs, blocked-wins), kind allowlist + cluster-scoped + Secret
hard-refusal, doc/parameter namespace mismatch, multi-doc parsing incl.
empty-doc rejection, plan-never-mutates, apply confirm gate, delete gates,
audit JSONL contents, byte/doc caps — plus the console: `/` renders, the plan
endpoint keeps the seam at `dry_run=True` even against crafted bodies,
refusals reuse the exact tool strings, the audit endpoint refuses
path-traversal, the status endpoint honors namespace/kind gates, and
`webui.enabled=false` removes the UI routes while `/mcp` keeps serving.

`tests/smoke_check.py` additionally drives the real HTTP app (uvicorn in a
thread): `/healthz`, the stateless `/mcp` JSON-RPC round-trip, `/` HTML,
`/api/status`, `/api/plan`, `/api/audit` and the 400 on a path-traversal
attempt.

## Helm

```bash
helm lint helm/
helm template test helm/
helm template site helm/ -f helm/local/values.example.yaml   # after filling it in
```

`values.yaml` ships **default-deny** (`namespaces.allowed: ""`). Site
overrides go in `helm/local/values.<site>.yaml` (never committed, never
packaged). The audit PVC (`persistence.enabled=true`, 1Gi) keeps the trail
across restarts; with persistence disabled an `emptyDir` is mounted instead
so `readOnlyRootFilesystem` still works (trail lost on restart — labs only).

Fleet-convention blocks: the Owner header, `imagePullSecrets`, the gated
`hpe_proxies`/`proxy` block (default false — the k8s API is in-cluster and
covered by the standard NO_PROXY cluster-local entries; `*_PROXY` env is
wired only when `hpe_proxies=true`), an explanatory comment where `caCert`
would be (deliberately not wired — in-cluster SA CA, no MITM egress), and an
optional Kyverno vendor-label ClusterPolicy ported from searxng-mcp
(`kyverno.enabled`, default false — cluster-scoped, so it never changes the
default render). The `webui.enabled` flag (default true) ships the read-only
console and, when `ezua.enabled=true`, routes `/` alongside `/mcp` through
the gateway.

## Release

```bash
./automation.sh 0.1.1   # bump → docker buildx --push → helm package → prune
python3 hardlinker.py --config hardlink_config.json --run   # mirror to pcai-solutions
```
