# FEATURES — applygate-mcp: the guardrails are the product

applygate-mcp is the fleet's governed **write** path: it applies and deletes
Kubernetes objects, and every capability below exists to make that safe
enough to hand to an agent. This file explains each guardrail, what it
prevents, and why it is shaped the way it is. The procedure lives in
[DEPLOYMENT.md](DEPLOYMENT.md); the spine is [../README.md](../README.md).

## 1. The tool surface

| Tool | Mutates | Guardrails enforced | Contract |
|---|---|---|---|
| `plan_apply(namespace, manifest, force=False)` | **never** (always `dry_run=All`) | DNS-1123 namespace, namespace policy, kind allowlist, alias-limited manifest hygiene | Per-doc `{kind, name, ok, message}` + `summary` + **`manifest_sha256`** (the sha256 of the EXACT planned bytes — carry it to `apply_manifest`); `readOnlyHint=True`. `force` is accepted for call-site symmetry but can never turn a plan into a mutation. When a DIFFERENT sha was already recorded for the namespace, the response adds `superseded_plan: true` (see §3). |
| `apply_manifest(namespace, manifest, confirm_apply=False, plan_sha256="")` | yes, per doc | confirm gate → DNS-1123 namespace → namespace policy → manifest hygiene → **plan binding (§3)** → kind allowlist | Real server-side apply per document (`field_manager=applygate-mcp`); refuses loudly unless `confirm_apply=True`. Result carries `plan_binding` (`enforced`/`warn`/`allow`) and `manifest_sha256`. A binding refusal adds `hint: "re-run plan_apply on the exact bytes you intend to apply"`. `readOnlyHint=False, destructiveHint=True`. |
| `delete_resource(namespace, kind, name, confirm_delete=False)` | yes | confirm gate → DNS-1123 namespace/name → namespace policy → kind allowlist | Deletes one allowlisted resource; audit-logged. `destructiveHint=True`. An API failure (not a refusal) returns `{"ok": false, "refused": false, "error": <same string as before>, "reason": "api_error", "status": <HTTP status>}` — plus the RBAC hint on a 403 mismatch. |
| `plan_delete(namespace, kind, name)` | **never** | DNS-1123 namespace/name, namespace policy, kind allowlist (the SAME fence as delete, minus the confirm gate — a plan never mutates) | The delete dry-run: surfaces `would_delete {kind, name, namespace}` + the object's CURRENT state (the exact view `get_resource_status` renders) or a clean `exists: false` 404 verdict. NEVER deletes — after review call `delete_resource` with `confirm_delete=true`. `readOnlyHint=True`. |
| `get_resource_status(namespace, kind, name)` | never | DNS-1123 namespace/name, namespace policy, kind allowlist (same fence as writes) | Status excerpt: Deployment/StatefulSet ready-vs-total replicas, Job succeeded/failed, else raw phase/conditions. `readOnlyHint=True`. |
| `verify_audit_chain()` | never | none needed — reads ONLY the server's configured `APPLYGATE_AUDIT_FILE`; no path parameter exists | The hash-chain verdict `{ok, file, exists, entries, legacy_entries, first_bad_line, error}` — the §4 procedure without kubectl exec. Also at `GET /api/audit/verify` (console). `readOnlyHint=True`. |

All tool results are `json.dumps` strings. Refusals are structured JSON:
`{"ok": false, "refused": true, "error": "<self-describing message>"}` — the
message names the knob that would have allowed the operation, so a refusal is
a fix instruction, not a dead end.

### The intended flow

```
plan_apply(ns, manifest)        # dry-run: what WOULD happen, per doc
  └─ read the per-doc verdicts  # result carries manifest_sha256
apply_manifest(ns, manifest, confirm_apply=True,
               plan_sha256=<manifest_sha256>)   # only after a clean plan,
                                                # bound to those exact bytes
get_resource_status(ns, kind, name)             # verify what you applied
```

Deletes get the same discipline — the most destructive verb is planned first:

```
plan_delete(ns, kind, name)     # dry-run: what WOULD be removed, current
  └─ human review of the verdict  state included; NEVER deletes anything
delete_resource(ns, kind, name,
                confirm_delete=True)            # only after the review
```

Multi-doc manifests are applied **per document**: a bad document never
silently blocks the good ones — every doc reports its own outcome, and every
doc (plan, applied, failed, refused) gets its own audit line.

## 2. The guardrail stack (checked on every write, in order)

1. **DNS-1123 hygiene.** Namespaces (RFC1123 DNS *labels*, ≤63) and resource
   names (RFC1123 DNS *subdomains* — dot-separated labels, ≤253 total) are
   validated BEFORE they flow into the DynamicClient URL path. Malformed
   input (`Bad_Name`, `-lead`, `trail-`, `..`, uppercase, over-length) gets a
   clear, self-describing validation refusal instead of an odd
   percent-encoded 404 from the API server. Dotted names
   (`my.team.config`) remain valid — they are legal Kubernetes object names.
2. **Namespace policy — DEFAULT-DENY.** `APPLYGATE_ALLOWED_NAMESPACES` unset
   or empty means **nothing is writable** — the server starts, serves health,
   and refuses every write loudly. Comma-separated; `fnmatch` globs supported
   (`team-*`). `APPLYGATE_BLOCKED_NAMESPACES` **always wins** over the
   allowlist (`kube-system,kube-public,*-system`).
3. **Kind allowlist — namespaced kinds only.** `APPLYGATE_ALLOWED_KINDS`
   defaults to `ConfigMap, Service, Deployment, StatefulSet, Job, CronJob,
   Ingress, ServiceAccount, PodDisruptionBudget, HorizontalPodAutoscaler`.
   Cluster-scoped kinds (Namespace, ClusterRole, ClusterRoleBinding,
   PersistentVolume, StorageClass, …) are refused even if someone allowlists
   them. The env knob can only **narrow** the built-in namespaced-kind
   registry, never widen it — an allowlisted-but-unknown kind is refused
   because its scope cannot be proven.
4. **Secrets never flow through this server.** Kind `Secret` is hard-refused
   on every tool (plan/apply/delete/status), with a message pointing at
   out-of-band secret management (`kubectl create secret`, sealed-secrets,
   external-secrets) — manifest secret values would otherwise end up in
   audit and log surfaces.
5. **Manifest hygiene — alias-bomb-safe YAML.** Multi-doc YAML is parsed by
   an **alias-limited SafeLoader**: per-manifest caps on alias resolutions
   (256) and composed nodes (50 000), then a post-parse **expansion budget**
   (100 000 nodes) that measures the fully-expanded tree — the number the
   loader caps alone cannot bound, because aliases share one node object
   while anything that serializes the apply body expands them exponentially.
   Self-referential structures (the YAML quine `&d [*d]`) are refused with a
   dedicated message; over-deep nesting fails with a clear error instead of a
   `RecursionError`. Sane manifests — including ones that legitimately use
   anchors — parse to **exactly** what `yaml.safe_load_all` yields. Empty
   documents are rejected (a *trailing* `---` is tolerated as an end-of-docs
   marker, an empty doc *between* documents is rejected); every doc must
   carry `apiVersion`, `kind`, `metadata.name` (itself DNS-1123); a doc-level
   `metadata.namespace` must equal the tool's namespace parameter; caps:
   **8 documents**, **256 KiB**.
6. **Plan binding — decision D11 (ratified 2026-09).** `apply_manifest` is
   sha256-bound to the exact bytes `plan_apply` recorded (§3). Missing plan
   or tampered bytes **refuse by default**;
   `APPLYGATE_UNPLANNED_APPLY=warn|allow` is the documented migration path.
7. **Confirm gates.** Real mutation requires `confirm_apply=True` /
   `confirm_delete=True` — anything else refuses with instructions.
8. **Audit trail — hash-chained, caller-attributed JSONL.** Best-effort
   append to `APPLYGATE_AUDIT_FILE` (default `/data/audit.jsonl`), one line
   per document per operation, base schema
   `{"ts","tool","namespace","kind","name","dry_run","outcome"}` with
   `outcome` ∈ `dry-run | applied | deleted | failed | refused` — plus the
   additive hardening fields `prev_sha256` and `caller` (§4). A broken audit
   sink never blocks an operation but always screams on stderr.

**Why the order matters.** Cheap, deterministic refusals come first (DNS
hygiene, namespace, kind) so a malformed request never reaches the cluster or
the parser; the expensive gates (YAML expansion, server-side dry-run) come
after. Every gate refuses with the *name of the knob* that would have allowed
it — an operator reading a refusal learns the policy, not just the verdict.

## 3. Plan binding (decision D11, ratified 2026-09)

`apply_manifest` does not treat the plan as advisory: it is **sha256-bound to
the exact bytes `plan_apply` validated** for that namespace.

- `plan_apply` records the sha256 of the manifest bytes it planned (its
  result carries `manifest_sha256`) in **per-process session state** —
  `{namespace: sha256}`; the latest plan per namespace wins.
- `apply_manifest` then either **carries** the sha (the optional
  `plan_sha256` parameter) or **references** the recorded plan from session
  state. The call proceeds only when the manifest bytes it received hash to
  exactly the planned value.
- **Missing plan → refusal. Tampered bytes → refusal.** Both are the DEFAULT
  (`APPLYGATE_UNPLANNED_APPLY` unset = `deny`), and both messages name the
  env. Automation that skips `plan_apply` **fails by design** — that is the
  ratified point of D11.
- **Migration path (flagged default):** `APPLYGATE_UNPLANNED_APPLY=warn` —
  applies proceed but every affected call logs loudly on stderr AND marks its
  audit entries with `"plan_binding": "warn"`; `=allow` restores the exact
  pre-D11 behavior. Unknown values **fail closed** to deny.
- **Carried-sha rules:** a carried `plan_sha256` must match (a) the sha256 of
  THIS call's manifest bytes and (b) the latest plan recorded in session state
  for the namespace — in every mode except `allow`. A self-computed sha for
  never-planned bytes proves nothing: the plan must exist on this server.
- **Session state is per-process/per-replica.** MCP 2.0 here is stateless, so
  plan and apply must reach the same replica; the refusal says so and the
  remedy is to re-run `plan_apply` (single-replica deployments, the chart
  default, are unaffected).
- A tool-level-refused plan (namespace policy, unparseable manifest) records
  **no** binding — the apply would die on the same gate anyway.
- **Superseded-plan visibility.** "Latest plan wins" is unchanged, but it is
  no longer silent: when a `plan_apply` records a sha DIFFERENT from the one
  already recorded for that namespace, its response carries
  `superseded_plan: true` and the note *"a newer plan_apply supersedes older
  planned bytes; apply_manifest binds to the latest plan only"* — an agent
  holding older planned bytes can see they will not bind. A binding refusal
  (tampered bytes, stale carried sha, missing plan) carries
  `hint: "re-run plan_apply on the exact bytes you intend to apply"`.

The chart wires the knob only when set (`planBinding.unplannedApply` in
values) — the default render is byte-identical to the Wave-0 baseline and the
strictest mode applies out of the box.

**The design bet:** binding the mutation to the *exact planned bytes* is what
makes "a human approved this" a statement about a specific artifact. A
namespace-scoped "you may apply" grant would be a different, weaker product.

## 4. Audit trail: hash chain and caller identity

Every audit line carries two additive fields — readers of the old 7-key
format stay compatible (new fields only; `webui._AUDIT_ENTRY_KEYS` is a
subset check, so old readers keep parsing new entries):

- **`prev_sha256`** — the sha256 of the **previous line's JSON text (no
  trailing newline)**. The first entry of an empty trail uses the 64-zero
  genesis constant (`"0" * 64`). The chain makes the trail
  **tamper-evident**: modifying, truncating, or reordering any line breaks
  every later link. In-process writes serialize on a lock, so concurrent tool
  calls chain correctly; the previous line is re-read from the file on every
  write (no in-memory state to lose across restarts; an unreadable sink
  degrades to genesis rather than blocking the write — best-effort by
  design).
- **`caller`** — `{"key_fp", "client"}`: a stable **non-secret fingerprint**
  of the API key that authenticated the request (`sha256:<first 12 hex>` of
  the matched key — never the key itself, so the fingerprint is safe for
  audit surfaces and joinable across the fleet) and the client `host:port`.
  Resolved at the auth layer (`_CallerAuditMiddleware` → contextvar → the
  tool bodies via `asyncio.to_thread`, which copies the request context —
  the fleet pattern of K8S-MCP's `_Caller`). Entries from the read-only
  console path and stdio use are anonymous (`null`s) by design.
- **`caller` may carry up to two MORE keys — `name` and `via` — and each
  appears ONLY when set**, so a deployment that configures neither serializes
  the exact pre-attribution 2-key shape:
  - **`name`** — the per-request registry name (`APPLYGATE_CLIENTS`,
    `name:key;name:key;...`, chart `clients.existingSecret` → secret key;
    re-read every request, so rotation needs no restart). The registry only
    NAMES keys the API-key middleware already matched — it can never
    authenticate anything; a malformed entry is skipped loudly and
    attribution degrades to fingerprint-only.
  - **`via`** — the caller claim relayed by a TRUSTED proxy peer in
    `X-MCP-Caller` (chart `callerPassthrough.trustedCidrs` →
    `MCP_CALLER_TRUSTED_CIDRS`; e.g. a gateway emits
    `X-MCP-Caller: <subject>@gateway`). The header is honored ONLY when the
    direct peer IP is inside the configured CIDRs — unset/empty means the
    header is ignored from EVERY peer (fail-closed; the default render).
    Behind an Istio sidecar the scope client is the sidecar's address, so
    tune the CIDR list per deployment. Values are sanitized before they enter
    the audit JSON (CR/LF and control characters stripped, capped at 200
    chars).
  - **Privacy note (deliberate):** when the gateway relays a subject, `via`
    puts a HUMAN identifier into this audit PVC. That is the point —
    audit-grade attribution — and the convention keeps the value a non-secret
    account name (`project-user-*` style), never an email or token.
  - **Attribution-never-authorization (hard invariant, enforced by tests):**
    `name` and `via` never unlock anything — not namespaces, not kinds, not
    the D11 plan binding, not `confirm_apply`. `key_fp` remains the
    credential truth; `via` is trusted-peer metadata about who a trusted
    intermediary says is on the other end.

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
   `{"ok", "entries", "legacy_entries", "first_bad_line", "error"}`.

**Three surfaces, one code path:**

- **MCP tool `verify_audit_chain()`** — parameter-free by design: it verifies
  ONLY the server's own configured `APPLYGATE_AUDIT_FILE`. A caller that
  somehow passes a path is refused with the same discipline as the
  `/api/audit` console endpoint (the path is server configuration, full
  stop). `readOnlyHint=True`.
- **Console `GET /api/audit/verify`** — the same verdict JSON over the same
  configured file, so the web UI can show "chain OK / CHAIN BROKEN" without
  exec access (the audit panel's **verify chain** button).
- **kubectl exec** — break-glass checks from a shell:

  ```bash
  kubectl -n <ns> exec deploy/applygate-mcp -- python -c \
    "import json; from applygate_mcp import server; print(json.dumps(server.verify_audit_chain('/data/audit.jsonl')))"
  ```

Known limitation (standard for hash chains): the **last** line is protected
only by the next entry — tampering with the final line is detectable only
until a later entry is written, or until the tail is externally anchored
(shipping the trail to logsearch, below, provides exactly that).

### Where the audit lives — the logsearch convention

The trail is a single JSONL file at `audit.file` (default
`/data/audit.jsonl`), on the release's dedicated PVC
(`persistence.enabled=true`, 1Gi, `helm.sh/resource-policy: keep`) so it
survives restarts and upgrades. Fleet convention: **the audit path is
documented for ops mount/expose** — ops mount the SAME path/PVC into the
logsearch pod (shared PVC claim or an extra volume in the logsearch chart) and
the governed write path's "who did what" becomes logsearch-searchable.
`readOnlyRootFilesystem` stays on: only that mount is writable.

## 5. Self-metrics

When `APPLYGATE_METRICS_ENABLED` is truthy (the chart wires it from
`values.metrics.enabled`, **default `false`** — the default render and route
table are byte-identical to the baseline), the HTTP app serves `GET /metrics`
on the same container port:

- with **prometheus-client installed**: standard exposition of the default
  process metrics plus `applygate_tool_calls_total{tool, outcome}`;
- without it (slim images, the fleet unit-test venv): an honest plain-text
  fallback naming the missing dependency — the import is guarded and the
  counter is a no-op, so metrics can never gate or break the write path.

Labels carry `tool`/`outcome` only — never namespaces, names, or manifest
material. The optional `ServiceMonitor` (same gate, `metrics.interval`
default `30s`) requires the prometheus-operator CRDs in-cluster.

## 6. The read-only console

The streamable-http server also serves a self-contained, no-build HPE-branded
console at `/` (and `/ui`) — the same visual language as the fleet's other
MCP consoles. A visible banner states the trust model: *Read-only console —
mutations happen only through the MCP tools with explicit confirm flags,
behind gateway authn.*

**THE UI CAN NEVER MUTATE THE CLUSTER.** There are NO `/api` endpoints for
apply or delete — not even gated ones — and the plan console rides the exact
`plan_apply` code path, which is always a server-side-apply dry-run
(`dry_run="All"`). Even a hand-crafted request posting `dry_run: false` /
`confirm_apply: true` to `/api/plan` cannot flip it: those fields are ignored
by construction, and the k8s seam only ever receives `dry_run=True`.

| Tab | What it shows |
| --- | --- |
| **Plan** | Paste a manifest, pick a namespace, get the per-document `{kind, name, ok, message}` verdicts the `plan_apply` tool would give (dry-run passed / refused, with the exact refusal text). |
| **Status** | `get_resource_status` for one kind/name/namespace — same namespace policy + kind gates as the write tools; Deployment/StatefulSet replica summaries, Job counters, conditions. |
| **Audit** | Tail of the JSONL audit file (last N lines, parsed): every plan, apply, delete, failure and refusal, with the configured file path — plus the **verify chain** button (hash-chain verdict over the same configured file). The endpoints read ONLY the configured `APPLYGATE_AUDIT_FILE` — client-selected paths are refused with 400. |
| **Policy** | The effective namespace allowlist/blocklist + kind allowlist (from the same config functions the tools read), the default-deny state, and the unconditional Secret / cluster-scoped refusal texts — so a human can see exactly why a plan was refused. |

### JSON API (all read-only)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Server status, `namespaces_enabled`, field manager, audit path, policy + caps |
| `GET /api/policy` | Effective allowlist/blocklist/kinds + hard-refusal texts |
| `POST /api/plan` | Plan console `{"namespace", "manifest"}` — ALWAYS a dry-run; returns the tool's per-doc verdicts |
| `GET /api/resource_status?namespace=&kind=&name=` | Status excerpt (tool-equivalent, guardrails included) |
| `GET /api/audit?lines=N` | Last N parsed audit entries — configured path only, traversal refused |
| `GET /api/audit/verify` | The hash-chain verdict (`server.verify_audit_chain` over the same configured file) — parameter-free, traversal refused |

The console is gated by `webui.enabled` (values) → `APPLYGATE_WEBUI_ENABLED`
(env; unset = on, `false|0|no|off` strips `/`, `/ui` and `/api/*` while
`/mcp` and the health endpoints keep working). The UI asset is
`ui/index.html`, copied into `/app/ui` by the Dockerfile;
`src/applygate_mcp/webui.py` also honors an `APPLYGATE_WEBUI_HTML` override.

## 7. Trust model, in one place

- **Default-deny namespaces**: a fresh deployment without an explicit
  allowlist is a no-op writer, on purpose — enabling writes is an explicit,
  auditable values-file act.
- **Kind allowlist + registry**: the write surface is explicit and
  narrow-by-default; admission is defence in depth (Secret and cluster-scoped
  refusals fire even on operator misconfiguration).
- **Plan → apply flow — enforced (D11)**: the mutating tool demands an
  explicit `confirm_apply=True` AND the sha256 of the exact planned bytes
  (carried or referenced from session state); the plan tool can never mutate
  regardless of flags.
- **No Secrets ever**: no secret values are read, written, or deleted.
- **Audit trail — tamper-evident**: every operation (including refusals)
  leaves a hash-chained JSONL line on a persistent volume, attributed to a
  non-secret caller fingerprint; verification is one call.
- **Parser hygiene**: alias bombs, YAML quines, and over-deep manifests are
  refused at parse time, before any cluster call.
- **HTTP posture**: MCP 2.0 stateless (any replica serves any request, no
  session state). DNS-rebinding protection follows the fleet-shared helper
  (`mcp_auth.transport_security_from_env`): loopback-only by default (dev
  mode) and **explicitly ON with a Host allowlist** the moment `MCP_HOSTNAME`
  / `MCP_EXTRA_ALLOWED_HOSTS` is set (chart keys `mcpHostname` /
  `extraAllowedHosts`) — set `mcpHostname` when the gateway fronts `/mcp`,
  or in-cluster svc-DNS callers get 421s after the flip. The chart
  AUTO-includes the release's own service DNS as the first
  `MCP_EXTRA_ALLOWED_HOSTS` entry when transport security is active, so sites
  only list EXTRA hosts. Real gateway auth still belongs in front
  (`ezua.enabled=true`).
- **API-key auth — MANDATORY**: `/mcp` requires a key (`X-API-Key` or
  `Authorization: Bearer`; the read-only console and probes stay public).
  The chart never creates the key Secret — pre-deploy it (§DEPLOYMENT 2.1).
  Keys are a comma-separated list (`api-keys=new,old`), which is the rotation
  mechanism; the server re-reads the env per request, so no restart is
  needed. The fleet-universal `MCP_API_KEYS` env is honored too.
- **RBAC is the authoritative boundary.** The chart ships a **namespaced**
  Role + RoleBinding in the release namespace, covering the kind allowlist's
  resource surface across the `""` (core), `apps`, `batch`,
  `networking.k8s.io`, `policy` and `autoscaling` apiGroups with
  `get/list/watch/create/patch/update/delete`. Never widen it to cluster
  scope: cross-namespace writes are a per-namespace bootstrap
  (`helm/local/rbac-bootstrap.se-g2.yaml`), applied by an admin.
- **Registry ≠ RBAC.** The kind registry admits more kinds than the Role
  grants (`PersistentVolumeClaim`, `Role`, `RoleBinding`, `Lease`,
  `Endpoints`, `ResourceQuota`, …): a kind the policy admits can still get
  an **HTTP 403 from the API server**. Extending the allowlist to such a kind
  is fine — but the write only succeeds after the RBAC manifests are extended
  too. On a 403 for a kind the shipped RBAC does not grant, the failing tool
  result carries `rbac_hint` naming the exact manifests to extend; the tool
  itself NEVER extends RBAC — that is a deliberate one-time human act.

## 8. Verification

The suite is 30 guardrail tests + 23 web-console tests + the Wave-3 hardening
matrices. It drives the async tools via `asyncio.run` and the UI routes via
Starlette's `TestClient`, with monkeypatched seam fakes — the test venv needs
**no `kubernetes` package**; only the guardrail logic is under test.

- `test_applygate.py` — default-deny matrix (empty allowlist, globs,
  blocked-wins), kind allowlist + cluster-scoped + Secret hard-refusal,
  doc/parameter namespace mismatch, multi-doc parsing incl. empty-doc
  rejection, plan-never-mutates, apply confirm gate, delete gates, audit
  JSONL contents, byte/doc caps.
- `test_plan_binding.py` — the D11 matrix: default deny names the env, valid
  plan applies, tampered bytes refuse, carried-sha rules, warn logs+applies,
  allow = pre-D11 behavior, fail-closed on a typo'd mode, namespace-keyed
  session, latest-plan-wins, refused plans record nothing.
- `test_dns_validation.py` — valid ns/name matrices incl. dotted subdomains
  and the 253-char ceiling; `Bad_Name`/`-lead`/`trail-`/`..`/uppercase/254-char
  → clear RFC1123 refusals, seams untouched, validation precedes the policy.
- `test_yaml_loader.py` — classic billion-laughs and one- and two-level
  quines refused with dedicated errors, alias-count and depth caps, sane
  alias-bearing manifests parse to EXACTLY the `yaml.safe_load_all` documents
  and reach the seam unchanged.
- `test_audit_chain.py` — genesis → chained links, concurrent-writer
  chaining, tamper/truncation/reorder/blank-line detection via
  `verify_audit_chain`, legacy-entry roots, caller fingerprint resolution +
  middleware capture, `k1`-never-in-audit.
- `test_metrics.py` — route absent by default = baseline route table, flag
  parsing, guarded exposition with fallback, counter labels carry no resource
  identity.
- `test_webui.py` — `/` renders, the plan endpoint keeps the seam at
  `dry_run=True` even against crafted bodies, refusals reuse the exact tool
  strings, the audit endpoint refuses path-traversal, the status endpoint
  honors namespace/kind gates, and `webui.enabled=false` removes the UI
  routes while `/mcp` keeps serving.
- `tests/smoke_check.py` — the real HTTP app (uvicorn in a thread):
  `/healthz`, the stateless `/mcp` JSON-RPC round-trip, `/` HTML,
  `/api/status`, `/api/plan`, the **plan → carry-sha → apply** flow over the
  wire, `/api/audit` and the 400 on a path-traversal attempt.

```bash
cd mcp_servers/applygate_mcp
python -m pytest tests/ -v
```

## 9. Files

| File | Purpose |
| --- | --- |
| `src/applygate_mcp/server.py` | The server: tools, guardrail stack, plan binding, audit writer, metrics gate, HTTP app |
| `src/applygate_mcp/webui.py` | The read-only console routes and JSON API |
| `ui/index.html` | The console asset (copied to `/app/ui` by the Dockerfile) |
| `mcp_auth.py` | The fleet-shared API-key / caller-attribution middleware (hardlinked) |
| `helm/` | The chart: `applygate-mcp` 0.5.0 |
| `helm/templates/rbac.yaml` | The namespaced writer Role + RoleBinding (read the comment block before extending) |
| `helm/values-examples/` | Secret-free paste-ready values (G2, hardened G2, hosted trial) |
| `helm/local/` | Per-site values + the cross-namespace RBAC bootstrap — gitignored, never packaged |
| [`documentation/DEPLOYMENT.md`](DEPLOYMENT.md) | The deployment procedure and every prerequisite |
| [`documentation/VERIFICATION.md`](VERIFICATION.md) | Post-deployment verification |
