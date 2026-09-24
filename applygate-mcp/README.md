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
| `plan_apply(namespace, manifest, force=False)` | **never** (always `dry_run=All`) | DNS-1123 namespace, namespace policy, kind allowlist, alias-limited manifest hygiene | Per-doc `{kind, name, ok, message}` + `summary` + **`manifest_sha256`** (the sha256 of the EXACT planned bytes — carry it to `apply_manifest`); `readOnlyHint=True`. `force` is accepted for call-site symmetry but can never turn a plan into a mutation. |
| `apply_manifest(namespace, manifest, confirm_apply=False, plan_sha256="")` | yes, per doc | confirm gate → DNS-1123 namespace → namespace policy → manifest hygiene → **plan binding (D11)** → kind allowlist | Real server-side apply per document (`field_manager=applygate-mcp`); refuses loudly unless `confirm_apply=True`. Result carries `plan_binding` (`enforced`/`warn`/`allow`) and `manifest_sha256`. `readOnlyHint=False, destructiveHint=True`. |
| `delete_resource(namespace, kind, name, confirm_delete=False)` | yes | confirm gate → DNS-1123 namespace/name → namespace policy → kind allowlist | Deletes one allowlisted resource; audit-logged. `destructiveHint=True`. |
| `get_resource_status(namespace, kind, name)` | never | DNS-1123 namespace/name, namespace policy, kind allowlist (same fence as writes) | Status excerpt: Deployment/StatefulSet ready-vs-total replicas, Job succeeded/failed, else raw phase/conditions. `readOnlyHint=True`. |

All tool results are `json.dumps` strings. Refusals are structured JSON:
`{"ok": false, "refused": true, "error": "<self-describing message>"}`.

### The intended flow

```
plan_apply(ns, manifest)        # dry-run: what WOULD happen, per doc
  └─ read the per-doc verdicts  # result carries manifest_sha256
apply_manifest(ns, manifest, confirm_apply=True,
               plan_sha256=<manifest_sha256>)   # only after a clean plan,
                                                # bound to those exact bytes
get_resource_status(ns, kind, name)             # verify what you applied
```

Multi-doc manifests are applied **per document**: a bad document never
silently blocks the good ones — every doc reports its own outcome, and every
doc (plan, applied, failed, refused) gets its own audit line.

## Guardrail stack (checked on every write, in order)

1. **DNS-1123 hygiene.** Namespaces (RFC1123 DNS *labels*, ≤63) and resource
   names (RFC1123 DNS *subdomains* — dot-separated labels, ≤253 total) are
   validated BEFORE they flow into the DynamicClient URL path. Malformed
   input (`Bad_Name`, `-lead`, `trail-`, `..`, uppercase, over-length) gets
   a clear, self-describing validation refusal instead of an odd percent-
   encoded 404 from the API server. Dotted names (`my.team.config`) remain
   valid — they are legal Kubernetes object names.
2. **Namespace policy — DEFAULT-DENY.** `APPLYGATE_ALLOWED_NAMESPACES`
   unset or empty means **nothing is writable** — the server starts, serves
   health, and refuses every write loudly. Comma-separated; `fnmatch` globs
   supported (`team-*`). `APPLYGATE_BLOCKED_NAMESPACES` **always wins** over
   the allowlist (`kube-system,kube-public,*-system`).
3. **Kind allowlist — namespaced kinds only.**
   `APPLYGATE_ALLOWED_KINDS` defaults to `ConfigMap, Service, Deployment,
   StatefulSet, Job, CronJob, Ingress, ServiceAccount, PodDisruptionBudget,
   HorizontalPodAutoscaler`. Cluster-scoped kinds (Namespace, ClusterRole,
   ClusterRoleBinding, PersistentVolume, StorageClass, …) are refused even if
   someone allowlists them. The env knob can only **narrow** the built-in
   namespaced-kind registry, never widen it — an allowlisted-but-unknown kind
   is refused because its scope cannot be proven.
4. **Secrets never flow through this server.** Kind `Secret` is hard-refused
   on every tool (plan/apply/delete/status), with a message pointing at
   out-of-band secret management (`kubectl create secret`, sealed-secrets,
   external-secrets) — manifest secret values would otherwise end up in
   audit/log surfaces.
5. **Manifest hygiene — alias-bomb-safe YAML.** Multi-doc YAML is parsed by
   an **alias-limited SafeLoader**: per-manifest caps on alias resolutions
   (256) and composed nodes (50 000), then a post-parse **expansion budget**
   (100 000 nodes) that measures the fully-expanded tree — the number the
   loader caps alone cannot bound, because aliases share one node object
   while anything that serializes the apply body expands them exponentially.
   Self-referential structures (the YAML quine `&d [*d]`) are refused with a
   dedicated message; over-deep nesting fails with a clear error instead of
   a `RecursionError`. Sane manifests — including ones that legitimately use
   anchors — parse to **exactly** what `yaml.safe_load_all` yields. Empty
   documents are rejected (a *trailing* `---` is tolerated as an
   end-of-docs marker, an empty doc *between* documents is rejected); every
   doc must carry `apiVersion`, `kind`, `metadata.name` (itself DNS-1123);
   a doc-level `metadata.namespace` must equal the tool's namespace
   parameter; caps: **8 documents**, **256 KiB**.
6. **Plan binding — decision D11 (ratified 2026-09).** See the next
   section: `apply_manifest` is sha256-bound to the exact bytes
   `plan_apply` recorded. Missing plan or tampered bytes **refuse by
   default**; `APPLYGATE_UNPLANNED_APPLY=warn|allow` is the documented
   migration path.
7. **Confirm gates.** Real mutation requires `confirm_apply=True` /
   `confirm_delete=True` — anything else refuses with instructions.
8. **Audit trail — hash-chained, caller-attributed JSONL.** Best-effort
   append to `APPLYGATE_AUDIT_FILE` (default `/data/audit.jsonl`), one line
   per document per operation, base schema
   `{"ts","tool","namespace","kind","name","dry_run","outcome"}` with
   `outcome` ∈ `dry-run | applied | deleted | failed | refused` — plus the
   additive hardening fields `prev_sha256` and `caller` (next section).
   A broken audit sink never blocks an operation but always screams on
   stderr.

## Plan binding (decision D11, ratified 2026-09)

`apply_manifest` no longer treats the plan as advisory: it is **sha256-bound
to the exact bytes `plan_apply` validated** for that namespace.

- `plan_apply` records the sha256 of the manifest bytes it planned (its
  result carries `manifest_sha256`) in **per-process session state** —
  `{namespace: sha256}`; the latest plan per namespace wins.
- `apply_manifest` then either **carries** the sha (the optional
  `plan_sha256` parameter) or **references** the recorded plan from session
  state. The call proceeds only when the manifest bytes it received hash to
  exactly the planned value.
- **Missing plan → refusal. Tampered bytes → refusal.** Both are the
  DEFAULT (`APPLYGATE_UNPLANNED_APPLY` unset = `deny`), and both messages
  name the env. Automation that skips `plan_apply` **fails by design** —
  that is the ratified point of D11.
- **Migration path (flagged default):**
  `APPLYGATE_UNPLANNED_APPLY=warn` — applies proceed but every affected
  call logs loudly on stderr AND marks its audit entries with
  `"plan_binding": "warn"`; `=allow` restores the exact pre-D11 behavior.
  Unknown values **fail closed** to deny.
- **Carried-sha rules:** a carried `plan_sha256` must match (a) the sha256
  of THIS call's manifest bytes and (b) the latest plan recorded in session
  state for the namespace — in every mode except `allow`. A self-computed
  sha for never-planned bytes proves nothing: the plan must exist on this
  server.
- **Session state is per-process/per-replica.** MCP 2.0 here is stateless,
  so plan and apply must reach the same replica; the refusal says so and
  the remedy is to re-run `plan_apply` (single-replica deployments, the
  chart default, are unaffected).
- A tool-level-refused plan (namespace policy, unparseable manifest)
  records **no** binding — the apply would die on the same gate anyway.

The chart wires the knob only when set (`planBinding.unplannedApply` in
values) — the default render is byte-identical to the Wave-0 baseline and
the strictest mode applies out of the box.

## Audit trail: hash chain + caller identity (additive fields)

Every audit line now carries two additive fields — readers of the old
7-key format stay compatible (new fields only; `webui._AUDIT_ENTRY_KEYS`
is a subset check, so old readers keep parsing new entries):

- **`prev_sha256`** — the sha256 of the **previous line's JSON text (no
  trailing newline)**. The first entry of an empty trail uses the 64-zero
  genesis constant (`"0" * 64`). The chain makes the trail
  **tamper-evident**: modifying, truncating, or reordering any line breaks
  every later link. In-process writes serialize on a lock, so concurrent
  tool calls chain correctly; the previous line is re-read from the file on
  every write (no in-memory state to lose across restarts; an unreadable
  sink degrades to genesis rather than blocking the write — best-effort by
  design).
- **`caller`** — `{"key_fp", "client"}`: a stable **non-secret fingerprint**
  of the API key that authenticated the request (`sha256:<first 12 hex>` of
  the matched key — never the key itself, so the fingerprint is safe for
  audit surfaces and joinable across the fleet) and the client `host:port`.
  Resolved at the auth layer (`_CallerAuditMiddleware` → contextvar → the
  tool bodies via `asyncio.to_thread`, which copies the request context —
  the fleet pattern of K8S-MCP's `_Caller`). Entries from the read-only
  console path and stdio use are anonymous (`null`s) by design.
- **`caller` may carry up to two MORE keys — `name` and `via` (Wave 6,
  fleet Item A1/A2) — and each appears ONLY when set**, so a deployment
  that configures neither serializes the exact pre-attribution 2-key shape:
  - **`name`** — the per-request registry name (`APPLYGATE_CLIENTS`,
    `name:key;name:key;...`, chart `clients.existingSecret` → secret key;
    re-read every request, so rotation needs no restart). The registry only
    NAMES keys the API-key middleware already matched — it can never
    authenticate anything; a malformed entry is skipped loudly and
    attribution degrades to fingerprint-only.
  - **`via`** — the caller claim relayed by a TRUSTED proxy peer in
    `X-MCP-Caller` (chart `callerPassthrough.trustedCidrs` →
    `MCP_CALLER_TRUSTED_CIDRS`; e.g. a gateway emits `X-MCP-Caller:
    <subject>@gateway`). The header is honored ONLY when the direct peer
    IP is inside the configured CIDRs — unset/empty means the header is
    ignored from EVERY peer (fail-closed; the default render). Behind an
    Istio sidecar the scope client is the sidecar's address, so tune the
    CIDR list per deployment. Values are sanitized before they enter the
    audit JSON (CR/LF and control characters stripped, capped at 200
    chars).
  - **Privacy note (deliberate):** when the gateway relays a subject
    (A3), `via` puts a HUMAN identifier into this audit PVC. That is the
    point — audit-grade attribution — and the convention keeps the value a
    non-secret account name (`project-user-*` style), never an email or
    token.
  - **Attribution-never-authorization (hard invariant, enforced by
    tests):** `name` and `via` never unlock anything — not namespaces, not
    kinds, not the D11 plan binding, not `confirm_apply`. `key_fp` remains
    the credential truth; `via` is trusted-peer metadata about who a
    trusted intermediary says is on the other end.

### Verifying the chain (the procedure)

Given the trail file (default `/data/audit.jsonl`):

1. Read the file line by line (no trailing-newline stripping beyond
   `splitlines()`).
2. Set `expected = "0" * 64` (genesis).
3. For each line, in order:
   - it must be valid JSON (a blank or injected line is a chain **gap**);
   - if it carries `prev_sha256`, it must equal `expected` — any mismatch
     means the trail was **tampered with, truncated, or reordered**;
   - if it does NOT carry `prev_sha256`, it is a pre-hardening (legacy)
     entry — treat it as a chain root;
   - set `expected = sha256(line)` (the exact line text, UTF-8).
4. `server.verify_audit_chain(path)` runs exactly this procedure and returns
   `{"ok", "entries", "legacy_entries", "first_bad_line", "error"}`:

   ```bash
   kubectl -n <ns> exec deploy/applygate-mcp -- python -c \
     "import json,sys; sys.path.insert(0,'/app'); import server; print(json.dumps(server.verify_audit_chain('/data/audit.jsonl')))"
   ```

Known limitation (documented, standard for hash chains): the **last** line
is protected only by the next entry — tampering with the final line is
detectable only until/unless a later entry is written or the tail is
externally anchored (shipping the trail to logsearch per the convention
below provides exactly that).

### Where the audit lives — the logsearch convention

The trail is a single JSONL file at `audit.file` (default
`/data/audit.jsonl`), on the release's dedicated PVC
(`persistence.enabled=true`, 1Gi) so it survives restarts. Fleet
convention (Wave 3, mirroring the workbench wording): **the audit.path is
documented for ops mount/expose** — ops mount the SAME path/PVC into the
logsearch pod (shared PVC claim or an extra volume in the logsearch chart)
and the governed write path's "who did what" becomes logsearch-searchable.
`readOnlyRootFilesystem` stays on: only that mount is writable.

## Self-metrics (`/metrics`) — additive, OFF by default

When `APPLYGATE_METRICS_ENABLED` is truthy (the chart wires it from
`values.metrics.enabled`, **default `false`** — the default render and
route table are byte-identical to the Wave-0 baseline), the HTTP app serves
`GET /metrics` on the same container port:

- with **prometheus-client installed**: standard exposition of the default
  process metrics plus `applygate_tool_calls_total{tool, outcome}`;
- without it (slim images, the fleet unit-test venv): an honest plain-text
  fallback naming the missing dependency — the import is guarded and the
  counter is a no-op, so metrics can never gate or break the write path.

Labels carry `tool`/`outcome` only — never namespaces, names, or manifest
material. The optional `ServiceMonitor` (same `values.metrics.enabled`
gate, `metrics.interval` default `30s`) requires the prometheus-operator
CRDs in-cluster.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `APPLYGATE_ALLOWED_NAMESPACES` | *(empty)* | Namespace allowlist, comma-separated, globs OK. **Empty = every write refused (default-deny).** A loud warning prints at startup when empty. |
| `APPLYGATE_BLOCKED_NAMESPACES` | *(empty)* | Namespace blocklist — always wins over the allowlist. |
| `APPLYGATE_ALLOWED_KINDS` | `ConfigMap,Service,Deployment,StatefulSet,Job,CronJob,Ingress,ServiceAccount,PodDisruptionBudget,HorizontalPodAutoscaler` | Kind allowlist (narrow-only; Secret/cluster-scoped always refused). |
| `APPLYGATE_AUDIT_FILE` | `/data/audit.jsonl` | JSONL audit sink (hash-chained; mounted volume in the Helm chart). |
| `APPLYGATE_UNPLANNED_APPLY` | `deny` | D11 plan-binding transition knob: `deny` (apply without a matching plan refuses) / `warn` (applies + logs loudly) / `allow` (pre-D11 behavior). Unknown values fail closed. |
| `APPLYGATE_METRICS_ENABLED` | *(off)* | Serve `/metrics` (prometheus-client when installed, honest fallback otherwise). Chart key: `metrics.enabled`. |
| `APPLYGATE_CLIENTS` | *(unset)* | Optional per-request caller-name registry, `name:key;name:key;...` — NAMES keys the auth middleware already matched (audit `caller.name`). Never authenticates anything. Chart key: `clients.existingSecret` (secret material — existingSecret-only). |
| `MCP_CALLER_TRUSTED_CIDRS` | *(empty)* | Comma-separated CIDRs of trusted direct peers whose `X-MCP-Caller` claim is recorded (audit `caller.via`, sanitized, ≤200 chars). **Empty = fail-closed: the header is ignored from every peer.** Chart key: `callerPassthrough.trustedCidrs`. Attribution-never-authorization: never unlocks anything. |

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
- **Plan → apply flow — enforced (D11)**: the mutating tool demands an
  explicit `confirm_apply=True` AND the sha256 of the exact planned bytes
  (carried or referenced from session state); the plan tool can never
  mutate regardless of flags.
- **No Secrets ever**: no secret values are read, written, or deleted.
- **Audit trail — tamper-evident**: every operation (including refusals)
  leaves a hash-chained JSONL line on a persistent volume, attributed to a
  non-secret caller fingerprint; verification is one command
  (`server.verify_audit_chain` / README procedure).
- **Parser hygiene**: alias bombs, YAML quines, and over-deep manifests are
  refused at parse time, before any seam call.
- **HTTP posture**: MCP 2.0 stateless (any replica serves any request, no
  session state), DNS-rebinding protection disabled per fleet convention —
  put real gateway auth in front if you expose `/mcp` through the Istio
  gateway (`ezua.enabled=true`).
- **API-key auth — MANDATORY**: `/mcp` requires a key (`X-API-Key` or
  `Authorization: Bearer`; the read-only console and probes stay public).
  **The chart never creates the key Secret — you MUST pre-deploy it in the
  target namespace before `helm install`, or the pod sits in
  `CreateContainerConfigError`:**

  ```bash
  kubectl -n <ns> create secret generic applygate-mcp-apikey \
    --from-literal="api-keys=$(openssl rand -hex 32)"
  ```

  Keys are a comma-separated list (`api-keys=new,old`) — that is the
  rotation mechanism: append the new key, move clients over, drop the old;
  the server re-reads the env per request, so no restart is needed. The
  fleet-universal `MCP_API_KEYS` env is honored too (either var works).

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

The suite (30 guardrail tests + 23 web-console tests + the Wave-3 hardening
matrices: D11 plan binding, DNS-1123 validation, alias-bomb YAML loading,
hash-chained audit + caller identity, and the metrics gate) drives the async
tools via `asyncio.run` and the UI routes via Starlette's `TestClient`, with
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

The Wave-3 files pin the new invariants: `test_plan_binding.py` (the D11
matrix — default deny names the env, valid plan applies, tampered bytes
refuse, carried-sha rules, warn logs+applies, allow = pre-D11 behavior,
fail-closed on a typo'd mode, namespace-keyed session, latest-plan-wins,
refused plans record nothing), `test_dns_validation.py` (valid ns/name
matrices incl. dotted subdomains and the 253-char ceiling;
`Bad_Name`/`-lead`/`trail-`/`..`/uppercase/254-char → clear RFC1123
refusals, seams untouched, validation precedes the policy),
`test_yaml_loader.py` (classic billion-laughs and one- and two-level quines
refused with dedicated errors, alias-count and depth caps, sane
alias-bearing manifests parse to EXACTLY the `yaml.safe_load_all` documents
and reach the seam unchanged), `test_audit_chain.py` (genesis → chained
links, concurrent-writer chaining, tamper/truncation/reorder/blank-line
detection via `verify_audit_chain`, legacy-entry roots, caller fingerprint
resolution + middleware capture, `k1`-never-in-audit), and `test_metrics.py`
(route absent by default = baseline route table, flag parsing, guarded
exposition with fallback, counter labels carry no resource identity).
`tests/smoke_check.py` additionally drives the real HTTP app (uvicorn in a
thread): `/healthz`, the stateless `/mcp` JSON-RPC round-trip, `/` HTML,
`/api/status`, `/api/plan`, the **plan → carry-sha → apply** flow over the
wire, `/api/audit` and the 400 on a path-traversal attempt.

## Helm

```bash
helm lint helm/
helm template test helm/
helm template site helm/ -f helm/local/values.example.yaml   # after filling it in
# Or render with the sanitized paste-ready examples (values-examples/README.md):
helm template g2 helm/ -f helm/values-examples/values.g2.yaml
helm template trial helm/ -f helm/values-examples/values.hosted-trial.yaml
```

`values.yaml` ships **default-deny** (`namespaces.allowed: ""`). Site
overrides go in `helm/local/values.<site>.yaml` (never committed, never
packaged); sanitized paste-ready per-target examples live in
[helm/values-examples/](helm/values-examples/README.md) (values walkthrough:
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md), verification:
[documentation/VERIFICATION.md](documentation/VERIFICATION.md)). The audit
PVC (`persistence.enabled=true`, 1Gi) keeps the trail
across restarts; with persistence disabled an `emptyDir` is mounted instead
so `readOnlyRootFilesystem` still works (trail lost on restart — labs only).

### Required values

`ezua.virtualService.endpoint` is **required whenever `ezua.enabled: true`**:
`templates/virtualservice.yaml` calls Helm's `required` on it, so an empty
endpoint aborts the render before anything is created — `Valid
.Values.ezua.virtualService.endpoint is required !` (applygate ships
`ezua.enabled: false` by default; the check only bites an operator who enables
the gateway exposure and then blanks the endpoint, or the `${DOMAIN_NAME}`
placeholder fails to resolve). With `ezua.enabled: false` no VirtualService is
rendered and the endpoint is never read — in-cluster Service access only.

```yaml
ezua:
  enabled: true                        # SITE: expose /mcp (and the webui) through the gateway
  domainName: ${DOMAIN_NAME}
  virtualService:
    endpoint: applygate-mcp.${DOMAIN_NAME}   # unique per release on the gateway
    istioGateway: istio-system/ezaf-gateway
    timeout: 120s
```

### Standard Kubernetes knobs

Defaults fit the fleet baseline; overridable per deployment.

| Key | Default | Effect |
| --- | --- | --- |
| `deployment.appName` | `applygate-mcp` | Label/selector + container name on Deployment, Service, VirtualService — not the release name (`deployment.name` is, and names the PVC/Secrets). Leave at the default; mismatched selectors break the Service wiring. |
| `resources.requests.cpu` / `resources.limits.cpu` | `100m` / `1` | CPU requests/limits (memory: `256Mi` / `512Mi`). |
| `securityContext.runAsNonRoot` / `.runAsUser` / `.runAsGroup` / `.fsGroup` | `true` / `10001` / `10001` / `10001` | **Present in values but NOT consumed by any template** — pod/container security actually renders from `podSecurityContext` (RuntimeDefault seccomp) and `containerSecurityContext` (below); the image itself runs `USER 10001`. Kept for values-shape consistency; editing it is inert. |
| `containerSecurityContext.allowPrivilegeEscalation` | `false` | Container securityContext (with `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault` via `podSecurityContext`). Keep false — part of the audited baseline; the k8s client needs only the `/tmp` emptyDir. |

Wave-3 additive keys — **both default OFF/empty, so the default render is
byte-identical to the Wave-0 baseline** (verified by render-diff):

- `planBinding.unplannedApply: ''` — renders `APPLYGATE_UNPLANNED_APPLY`
  only when set (`warn` = D11 migration path, `allow` = pre-D11 behavior;
  unset = the server's built-in default deny applies).
- `metrics.enabled: false` (+ `metrics.interval: 30s`) — renders
  `APPLYGATE_METRICS_ENABLED` and the `ServiceMonitor` only when true.
- `clients.existingSecret: ''` (Wave-6 attribution) — renders
  `APPLYGATE_CLIENTS` (from the named Secret's `clients.existingSecretKey`,
  default `clients`) only when set: the per-request caller-name registry
  (`name:key;name:key;...`) → audit `caller.name`. Omitted ⇒ fp-only
  caller audit, behavior unchanged.
- `callerPassthrough.trustedCidrs: ''` (Wave-6 attribution) — renders
  `MCP_CALLER_TRUSTED_CIDRS` only when set: CIDRs of trusted direct peers
  whose `X-MCP-Caller` claim is recorded → audit `caller.via`. Omitted ⇒
  the header is ignored from every peer (fail-closed).
- `audit.file` carries the audit-path comment block: **the audit.path is
  documented for ops mount/expose** — the fleet convention is to mount the
  same path/PVC into the logsearch pod so the trail is searchable.

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
