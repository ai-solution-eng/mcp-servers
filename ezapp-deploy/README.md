# ezapp-deploy

An **application-deployment MCP server** for **HPE Private Cloud AI (PCAI / Ezmeral Unified Analytics)** — the write-side companion to [k8s-ops](../k8s-ops) (read-only inspection). It runs **inside the target PCAI cluster** and exposes a deliberately tiny deploy surface over **MCP 2.0** (protocol `2026-07-28`, official MCP Python SDK v2):

1. **`upload_chart`** (or **`POST /upload`** for big charts) — receives a packaged Helm chart and pushes it to the in-cluster **ChartMuseum** via `curl` (raw `POST /api/charts`, the exact flow of HPE's official [byoa-tutorials](https://github.com/HPEEzmeral/byoa-tutorials); ChartMuseum is a ClusterIP service inside the cluster).
2. **`apply_ezappconfig`** — receives the **cluster-scoped `EzAppConfig` CR** from the agent skill, validates it, and `kubectl apply`s it. The EZUA app operator then installs the chart into the target namespace.
3. **`get_ezappconfig`** — reads one EzAppConfig back (`status.status`: `ready | error | warning | initialized`, `failureReason`, …) so the agent can wait for rollout.
4. **`delete_chart`** — removes one chart version from ChartMuseum — **only a version this server uploaded itself**.
5. **`delete_ezappconfig`** — deletes an EzAppConfig — **only one this server applied itself**.

### Large payloads: raw HTTP endpoints (charts AND EzAppConfigs)

MCP tool-call arguments get truncated by client/model caps (observed ~40-43 KB), so anything big goes over raw HTTP with the API key — the LLM only relays the small JSON receipts:

**Charts** — `POST /upload` takes the raw `.tgz` (the agent curls it straight to the server, reachable through the ezaf-gateway):

```bash
curl -sS -H "Authorization: Bearer <API_KEY>" \
     --data-binary @chart-1.0.0.tgz \
     -w '\n%{http_code}\n' \
     "https://<public-host>/upload"            # add ?force=true to overwrite
```

Responses are JSON: `{"status": "uploaded", "chart": ..., "version": ..., "detail": ...}` with HTTP `200` uploaded / `409` exists / `403` force-refused (unmanaged) / `400` invalid payload or too-large (`413`) / `502` ChartMuseum error. It runs the **identical pipeline** as `upload_chart` — sniffing, size cap, ownership gate, ledger recording — so an upload via either path is managed/trackable the same way.

**EzAppConfigs** — `POST /manifest` takes the raw CR YAML (big `spec.values`, base64 `logoImage`, …). The server **validates it at staging time** (kind/apiVersion/name/namespace-policy problems are reported immediately with 400), stages it, and returns a `manifest_id`; the agent then calls `apply_ezappconfig(manifest_id=...)`, which applies it server-side — the CR text never travels through model tokens:

```bash
curl -sS -H "Authorization: Bearer <API_KEY>" \
     --data-binary @ezappconfig.yaml \
     "https://<public-host>/manifest"          # -> {"manifest_id": "...", "name": ...}
```

Staged manifests are **single-use**, expire after 15 minutes, and are capped at 5 concurrent (keep `replicaCount: 1`, same as the chunked uploads). `apply_ezappconfig` accepts exactly one of `manifest_yaml` (small CRs) or `manifest_id`.

### Chunked MCP upload (clients with no shell)

When the agent has no shell access (chat UIs, restricted runtimes) and the chart is too big for one tool call, three tools stream it through MCP with end-to-end integrity:

1. **`upload_chart_begin(total_size, sha256, filename?)`** — measure the file with the shell (`wc -c`, `sha256sum`); the response JSON carries a `handle` and the server-mandated `chunk_bytes` (24 KB binary ≈ 32 KB base64, safely under the ~43 KB argument cap).
2. **`upload_chart_chunk(handle, seq, chunk_b64)`** — split the FILE into `chunk_bytes` binary chunks, base64 each, send sequentially (`seq` 0,1,2,…; every chunk except the last is exactly `chunk_bytes`). Progress JSON tells the agent when `complete`.
3. **`upload_chart_commit(handle, force?)`** — the server verifies the assembled size **and sha256** (a single corrupted chunk fails loud with both hashes; the session is discarded), then runs the identical sniff/ledger/ChartMuseum pipeline.

Sessions are in-memory staging with a 15-minute TTL and a 5-concurrent cap — this is the one MCP stateful feature here, so keep `replicaCount: 1` (the chart default) or chunked uploads will fail to find their session across replicas. Everything else stays stateless. The three tools can be dropped from the server entirely with `EZAPP_MCP_CHUNKED_UPLOAD_ENABLED=false` (chart: `chunkedUpload.enabled: false`).

Rule of thumb for an agent skill: **< 30 KB → `upload_chart` · ≥ 30 KB with shell → `POST /upload` · ≥ 30 KB without shell → chunked trio.**

### Ownership ledger

Every successful chart upload and EzAppConfig apply is recorded in the **ownership ledger** — a ConfigMap in the server's namespace (created empty by the chart, `helm.sh/resource-policy: keep` so upgrades never reset it; survives restarts, shared across replicas). The destructive paths are gated on it:

- `force=true` upload requires the exact `chart/version` in the ledger — an existing chart version **not** uploaded by this server is never overwritten (a plain upload of it just 409s, and the refusal happens before any network call);
- `delete_chart` requires the exact `chart/version` in the ledger;
- `delete_ezappconfig` requires the CR's name in the ledger;
- `apply_ezappconfig` refuses to overwrite an existing EzAppConfig that has no ledger entry (it may be someone else's app; re-applying your own CRs — the upgrade path — is fine).

Fail-safe direction: a missing, unreadable, or wiped ledger means **refusal**, never permission — losing it locks the destructive tools out instead of unlocking everything. Operators can adopt an externally-created EzAppConfig by seeding `ezappconfigs.json` manually; nothing is auto-adopted. Both delete tools can be dropped entirely with `EZAPP_MCP_DELETE_ENABLED=false` (chart: `delete.enabled: false`) for automation-facing deployments where decommissioning should stay human-only — unregistered tools are invisible and uncallable for clients.

## Web UI (read-only)

A small static shell is served by the server itself at **`/ui/`** (and `/` redirects there) listing what this MCP server manages:

- **EzAppConfigs table** — every CR in the ownership ledger merged with **live cluster status** (`ready | error | warning | initialized`, failure reason, chart/version/target namespace from the CR); CRs deleted out-of-band show as "not found" instead of disappearing.
- **Managed chart versions table** — every `chart/version` this server uploaded to ChartMuseum.

Hygiene (same model as the k8s-ops console): the shell is inert — it carries no data; the browser fetches `/api/managed` with the **same API key as `/mcp`** (paste it into the page; it lives in `sessionStorage` only). All cluster data renders as text, never HTML; CSP `default-src 'none'` + nosniff + no-referrer on the shell; traversal-guarded static handler; auto-refresh (15s) is a checkbox. `EZAPP_MCP_UI_ENABLED=false` (or chart `ui.enabled: false`) removes both the shell and the data endpoint.

## Reference deploy flow (PCAI / EZUA)

```
agent skill                        ezapp-deploy MCP server (in-cluster)
───────────                        ─────────────────────────────────────
helm package → app-1.0.0.tgz  ──▶  upload_chart(...)   ── curl ──▶  ChartMuseum
                                   (POST /api/charts, 201/409)
EzAppConfig (cluster-scoped,   ──▶  apply_ezappconfig(...) ─ kubectl ─▶ API server
apiVersion ezconfig.hpe.ezaf.com/v1alpha1)
                                   EZUA app operator: helm-installs the chart
                                   into spec.options.namespace, sets .status
       ◀── get_ezappconfig(...) until status.status == "ready"
```

Reference EzAppConfig (shape comes from the platform; the server only validates the fields the deploy flow depends on):

```yaml
apiVersion: ezconfig.hpe.ezaf.com/v1alpha1
kind: EzAppConfig
metadata:
  name: ezappconfig-test-app
  labels:
    hpe-ezua/imported-app: "true"
spec:
  name: test-app
  install: true            # false = register only; flip later
  releaseName: test-app
  chartVersion: 0.2.6      # must match the uploaded chart's version
  description: Test app description
  label: Test App
  category: dataScience
  options:
    namespace: test-app-ns
    create-namespace: "true"
    wait: "true"
    timeout: 15m
  values: ""               # inline helm values (string)
```

## Tools

| Tool | Args | Effect |
| --- | --- | --- |
| `upload_chart` | `chart_tgz_b64` (base64 tgz), `force: bool = false`, `filename: str = ""` | Sniffs the payload (gzip magic, tar layout, `<chart>/Chart.yaml` with name+version, size cap), then `curl --data-binary @file <CHARTMUSEUM_URL>/api/charts[?force=true]`. 201 = created (recorded in the ledger); **409 = version exists** (bump version; `force=true` only when this server uploaded it before). **Tool-call input caps (~43 KB) truncate large payloads — use `POST /upload` or the chunked trio below.** |
| `upload_chart_begin` / `upload_chart_chunk` / `upload_chart_commit` | `total_size`+`sha256` / `handle`+`seq`+`chunk_b64` / `handle`+`force?` | Chunked upload for no-shell clients: fixed 24 KB binary chunks streamed as base64, integrity proven by the agent's locally-computed `sha256` (verified before anything reaches ChartMuseum). Same ledger pipeline. In-memory sessions, 15 min TTL, 5 max — keep `replicaCount: 1`. |
| `apply_ezappconfig` | `manifest_yaml` **or** `manifest_id` (exactly one) | Validates (kind/apiVersion allowlist, DNS-1123 name, **no `metadata.namespace`** — cluster-scoped, required `spec.name`/`spec.chartVersion`, target-namespace policy on `spec.options.namespace`), refuses to overwrite an existing **unmanaged** CR, then `kubectl apply -f <file> --field-manager ezapp-deploy-mcp` and records the CR in the ledger. Large CRs: `POST /manifest` first, then pass the `manifest_id` (staged manifests are single-use, 15-min TTL). |
| `get_ezappconfig` | `name`, `output: "summary"\|"yaml"\|"json"`, `include_values: bool = false` | Default **summary** = compact JSON with the full `.status` block (`ready \| error \| warning \| initialized`, `retryCnt`, `failureReason`) plus key spec fields — large blobs (`spec.values`, `spec.logoImage`) are elided to `<elided: N bytes, sha256=…>` so the status can never be pushed past the output truncation. `yaml` elides the same way; `json` is raw/unelided. `include_values=true` puts the blob content back (truncation may apply). |
| `delete_chart` | `chart_name`, `chart_version` | Ledger-gated: only versions this server uploaded. `curl -X DELETE <CHARTMUSEUM_URL>/api/charts/<name>/<version>`; on 200/404 the ledger entry is removed. |
| `delete_ezappconfig` | `name` | Ledger-gated: only CRs this server applied. **Single call**: the DELETE is issued `--wait=false`; on acceptance the ledger entry is removed immediately and a bounded background watch polls (10s interval, 10 min cap) until the operator teardown (finalizer) actually completes — logging loudly if it appears stuck. Confirm with `get_ezappconfig` → NotFound. A terminating CR blocks re-apply of the same name with a dedicated message until it disappears. |

## Security model

This is a **write-capable** server (it deploys things). Every mutating path is bounded by construction — there is **no kubectl escape hatch and no shell**:

1. **Fixed argv subprocesses.** `curl` and `kubectl` are invoked as argv lists via `asyncio.create_subprocess_exec` (same pattern as k8s-ops); the only user-controlled tokens that reach them are regex-validated (chart name/version, CR name, apiVersion) or temp-file contents written `0600` and unlinked in `finally`. Proxy env vars are stripped from subprocesses — in-cluster ClusterIP endpoints are not reachable through egress proxies.
2. **One-kind apply.** `apply_ezappconfig` accepts ONLY `kind: EzAppConfig` (env-tunable) whose `apiVersion` is on a configurable allowlist — no Secrets, Roles, or arbitrary objects can enter the cluster through this server. `metadata.namespace` is rejected (cluster-scoped CR; the install namespace lives in `spec.options.namespace`).
3. **Target-namespace governance.** `spec.options.namespace` is checked against optional glob allow/deny lists (`EZAPP_MCP_ALLOWED_TARGET_NAMESPACES` / `EZAPP_MCP_BLOCKED_TARGET_NAMESPACES`, blocked wins). `kube-system`, `kube-public`, `kube-node-lease` are **always** denied.
4. **Upload hardening.** Payload size cap (default 30 MB, `EZAPP_MCP_MAX_CHART_MB`), gzip-magic check, tar member-sum cap (zip-bomb guard), Chart.yaml presence + parseable name/version required — garbage cannot reach ChartMuseum.
5. **Ownership ledger.** Deletes and force-overwrites are limited to objects this server itself uploaded/applied (ConfigMap-backed — survives restarts, shared across replicas, kept across helm upgrades/uninstalls). A missing or wiped ledger LOCKS the destructive tools out (fail-safe). RBAC does carry the `delete` verb on `ezappconfigs` — the ledger is the application-layer enforcement point, as with namespace governance.
6. **API-key auth.** Every HTTP request must present `EZAPP_MCP_API_KEY` as `Authorization: Bearer <key>` or `X-API-Key: <key>`; constant-time compare (`hmac.compare_digest`); 401 before the MCP app is reached. Unset = open endpoint + loud startup warning (local development only — never in-cluster).
7. **Least-privilege RBAC.** A ClusterRole granting only `get/create/update/patch/delete` on `ezappconfigs.<group>` — deliberately **no `list`/`watch`**, so the server is structurally unable to enumerate EzAppConfigs it did not deploy (the UI and tools address CRs by name, ledger-driven only); a namespaced Role granting only `get/patch` on the ONE ledger ConfigMap (`resourceNames`-pinned — no other ConfigMap is readable). A wildcard `apiGroup` is refused at render time.
8. **Pod hardening.** Non-root (uid/gid 10001), read-only root filesystem, all capabilities dropped, RuntimeDefault seccomp, `/tmp` as emptyDir.
9. **Host-header pinning.** `MCP_HOSTNAME` (wired from the chart's `ezua.virtualService.endpoint`) enables SDK DNS-rebinding protection allowlisting only the public FQDN.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `EZAPP_MCP_API_KEY` | unset (open + warning) | API key for the MCP endpoint |
| `CHARTMUSEUM_URL` | `http://chartmuseum.ez-chartmuseum-ns.svc.cluster.local:8080` | In-cluster ChartMuseum (PCAI's stock deployment) |
| `CHARTMUSEUM_USERNAME` / `CHARTMUSEUM_PASSWORD` | unset (no auth header) | Optional basic auth (stock PCAI ChartMuseum needs none) |
| `CHARTMUSEUM_TLS_INSECURE` | `false` | Skip TLS verification on the outbound ChartMuseum calls — for platform CAs missing from the container trust store. Mutually exclusive with `CHARTMUSEUM_CA_BUNDLE` (which instead pins the platform CA via `--cacert`; mount the CA file into the pod yourself). |
| `EZAPP_MCP_EZAPPCONFIG_KIND` | `EzAppConfig` | Expected manifest kind |
| `EZAPP_MCP_EZAPPCONFIG_API_VERSIONS` | `ezconfig.hpe.ezaf.com/v1alpha1` | apiVersion allowlist (comma-separated; `*` = any) |
| `EZAPP_MCP_EZAPPCONFIG_PLURAL` | `ezappconfigs` | CRD plural used by kubectl |
| `EZAPP_MCP_MAX_CHART_MB` | `30` | Upload payload cap (decoded tgz) |
| `EZAPP_MCP_ALLOWED_TARGET_NAMESPACES` | unset (all) | Whitelist for `spec.options.namespace` |
| `EZAPP_MCP_BLOCKED_TARGET_NAMESPACES` | unset | Blacklist (always wins) |
| `EZAPP_MCP_LEDGER_CONFIGMAP` | `ezapp-deploy-ledger` | Ownership-ledger ConfigMap name |
| `EZAPP_MCP_LEDGER_NAMESPACE` | pod's SA namespace | Namespace the ledger lives in |
| `EZAPP_MCP_UI_ENABLED` | `true` | Read-only web view at `/ui/` (`false` disables shell + data endpoint) |
| `EZAPP_MCP_CHUNKED_UPLOAD_ENABLED` | `true` | Chunked upload tools (`false` drops the three tools from the server) |
| `EZAPP_MCP_DELETE_ENABLED` | `true` | `delete_chart` / `delete_ezappconfig` (`false` drops both tools — decommissioning becomes a purely human action) |
| `MCP_HOSTNAME` | unset | Public FQDN for DNS-rebinding protection |

## Deploy (helm chart — `helm/`)

```bash
docker buildx build -t ghcr.io/ai-solution-eng/ezapp-deploy:v0.1.0 . --push

export NAMESPACE=ezapp-deploy          # or a project namespace
export ENDPOINT=ezapp-deploy.<your-domain>.hpecorp.net   # MUST be unique per release

# 1. API key — out-of-band, the chart never creates it
kubectl -n $NAMESPACE create secret generic ezapp-deploy-apikey \
  --from-literal="api-key=$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -

# 2. Install (endpoint + ChartMuseum URL are the knobs that matter)
helm install ezapp-deploy ezapp-deploy/helm -n $NAMESPACE \
  --set ezua.virtualService.endpoint=$ENDPOINT \
  --set chartmuseum.url=http://chartmuseum.ez-chartmuseum-ns.svc.cluster.local:8080

# 3. Verify
kubectl -n $NAMESPACE rollout status deploy/ezapp-deploy
# ChartMuseum reachability + RBAC sanity checks: see the chart's NOTES.txt
```

What gets installed: Deployment (hardened pod, native `hpe-ezua/type: vendor-service` + `hpe-ezua/app` labels on every resource — no Kyverno policy needed), ClusterIP Service, ServiceAccount + ClusterRole/Binding (`ezappconfigs` writer) + ledger Role/Binding (one name-pinned ConfigMap), the ownership-ledger ConfigMap, Istio VirtualService on `istio-system/ezaf-gateway`, and an AuthorizationPolicy template shipped **disabled** (flip `ezua.authorizationPolicy.enabled=true` to enforce gateway SSO).

MCP client config (agent skill side):

```json
{
  "mcpServers": {
    "ezapp-deploy": {
      "type": "remote",
      "url": "https://<public-host>/mcp",
      "headers": { "Authorization": "Bearer <API_KEY>" }
    }
  }
}
```

## Build & run (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export EZAPP_MCP_API_KEY=dev-secret            # unset = open, with a warning
python server.py                               # streamable-http on 0.0.0.0:9090

# policy/validation checks (no cluster needed; stubs the mcp SDK if missing)
python3 test_ezapp_deploy.py
```

Local kubectl runs use your existing kubeconfig; in-cluster it uses the pod's service-account token/CA automatically.

## Limitations (honest boundaries)

- The server trusts the cluster's EzAppConfig CRD group/version defaults (`ezconfig.hpe.ezaf.com/v1alpha1`); a cluster on a different API version needs the env/values override — validation and RBAC both key off it.
- Target-namespace governance and the ownership ledger are application-layer controls; the authoritative boundary is RBAC (which here only ever touches `ezappconfigs` + the one ledger ConfigMap — the platform operator does the actual installs with its own privileges).
- A ledger entry can go stale (upload succeeded but the ledger write failed, or an operator deleted the object out-of-band): deletes then either 404 harmlessly (stale entry removed) or refuse — both fail-safe. An operator can seed `ezappconfigs.json`/`charts.json` manually to adopt pre-existing objects.
- Chart uploads are buffered in pod memory (base64 decode + tar header scan); size the pod memory limit for your largest chart (default cap 30 MB keeps this modest).
