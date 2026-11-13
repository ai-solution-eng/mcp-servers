# Deploying SQLhandler on PCAI

Full deployment guide for **PCAI (HPE Private Cloud AI / HPE Ezmeral Unified
Analytics)**, chart `sqlhandler` **4.0.0**. Field reference for every key:
[`../helm/values.yaml`](../helm/values.yaml) · paste-ready examples:
[`../helm/values-examples/`](../helm/values-examples/) · post-deploy checks:
[VERIFICATION.md](VERIFICATION.md) · every environment variable:
[CONFIGURATION.md](CONFIGURATION.md).

SQLhandler ships as a **PCAI MCP 2.0** server: built on the `mcp>=2.0` SDK's
low-level `Server`, speaking the standard MCP protocol (initialize handshake)
over stateless streamable-http at `/mcp`, so any standard MCP client connects
(DSH, the official Python/TS SDKs, MCP Inspector, OWUI).

---

## 1. The deployment contract

**Deployment is a values problem.** The chart is packaged and imported into PCAI
**once**; after that the operator's only surface is the deployment's **Helm
Values editor**. Every site-specific decision is a value. There are no template
edits, no post-render patching, no editing of rendered manifests — and no
`helm install` or `kubectl apply` of this chart's resources, because PCAI is a
Kubernetes wrapper that owns the release.

What PCAI does for you, on every apply:

- uploads the packaged chart to its **ChartMuseum** (the platform's chart
  registry) and installs it through its own controller;
- substitutes `${DOMAIN_NAME}` in the values you paste, so the endpoint is
  `sqlhandler.${DOMAIN_NAME}` without you knowing the domain;
- creates the namespace and renders the `ezua` block: the Istio
  **VirtualService** (the gateway route) and the `hpe-ezua` vendor-service
  labels PCAI uses for discovery and monitoring;
- keeps the release's values as the record of the deployment.

What **you** must do out-of-band, because it cannot be a value: **create
Secrets** (§2.1) and **satisfy cluster prerequisites** (§2.2). A Kubernetes
Secret cannot be a chart value without putting the credential in the values
document and the release record; a StorageClass, a CRD or a gateway provider
cannot be created by a chart that is scoped to one namespace.

**Upgrade is the same act as install**: import the new chart version, keep the
values document, apply.

The developer path — `helm template`, `helm install`, `docker run` — exists for
people working on the chart, and is documented at the end (§5.3), clearly
separated. It is **not** how a PCAI deployment is created or changed.

## 2. Prerequisites

Ordered. Each item states its **purpose**, its **failure mode** and how to
**verify** it. Nothing here is optional unless it says so.

### 2.1 Secrets the operator creates

All Secrets live in the deployment's namespace. The chart **never** creates
these (an `envsubst` apply once emptied a live key Secret — the K8S-MCP
lesson), with one deliberate exception noted below.

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `<fullname>-apikey` (default `sqlhandler-apikey`) | `api-keys` — a **comma-separated** key list | `MCP_API_KEYS` env → the `/mcp` API-key gate | SQLhandler reads the **whole data lake**; the chart wires this Secret **unconditionally**, the same posture as applygate/k8s-mcp. Values: `security.apiKey.existingSecret` / `existingSecretKey` | Pod never starts: **`CreateContainerConfigError`** (the `secretKeyRef` cannot resolve). This is the intended bootstrap order, not a bug | `kubectl -n <ns> create secret generic sqlhandler-apikey --from-literal="api-keys=$(openssl rand -hex 32)"` (or `--dry-run=client -o yaml \| kubectl apply -f -`) |
| `s3-credentials` | `access-key`, `secret-key` | `S3_ACCESS_KEY` / `S3_SECRET_KEY` — only when `backend: s3` and `s3.anonymous: false` | Object-store credentials for the Parquet/Delta backend. Values: `s3.credentialsSecret.name` / `accessKeyKey` / `secretKeyKey` | Pod never starts: **`CreateContainerConfigError`**. Wrong keys instead: `/ready` returns 503, `list_tables` errors with 403 / `InvalidAccessKeyId` | `kubectl -n <ns> create secret generic s3-credentials --from-literal=access-key=… --from-literal=secret-key=…` |
| `fabric-credentials` | `tenant-id`, `client-id`, `client-secret` | `FABRIC_TENANT_ID` / `FABRIC_CLIENT_ID` / `FABRIC_CLIENT_SECRET` — only when `backend: onelake` | Entra service-principal credential for OneLake. Values: `fabric.credentialsSecret.*` | Pod never starts: **`CreateContainerConfigError`**. Wrong or expired credential instead: the AAD token request fails, `/ready` 503, `list_tables` reports an auth error | `kubectl -n <ns> create secret generic fabric-credentials --from-literal=tenant-id=… --from-literal=client-id=… --from-literal=client-secret=…` |
| `iceberg-credentials` | `token`, `access-key`, `secret-key` | `ICEBERG_CATALOG_TOKEN` + S3 keys — only when `backend: iceberg` | REST-catalog token and the data-file keys. Values: `iceberg.credentialsSecret.*` | Pod never starts if the chart wires the key: **`CreateContainerConfigError`**. **Known defect:** the token env is only wired when `iceberg.credentialsSecret.values.token` is non-empty, so an out-of-band token alone leaves the catalog unauthenticated — see §6 | `kubectl -n <ns> create secret generic iceberg-credentials --from-literal=token=… --from-literal=access-key=… --from-literal=secret-key=…` |
| `<fullname>-db-credentials` (default `sqlhandler-db-credentials`) | `SQLHANDLER_DB_PW_<NAME>` per attached database | `databases:` entries with a non-empty `password` | Read-only Postgres/MySQL passwords. The generated attach config carries env-var **names** only | Pod starts but the affected database is reported unavailable in `list_tables`; queries touching it fail with an auth error. Lake-only queries are unaffected (by design) | `kubectl -n <ns> create secret generic sqlhandler-db-credentials --from-literal=SQLHANDLER_DB_PW_OPS=…` |

Read back and rotate:

```bash
kubectl -n <ns> get secret sqlhandler-apikey -o jsonpath='{.data.api-keys}' | base64 -d; echo
kubectl -n <ns> get secret s3-credentials -o jsonpath='{.data.access-key}' | base64 -d; echo
```

The Deployment carries `checksum/secret` and `checksum/config` annotations, so a
rotated Secret rolls the pods on the next re-apply. **A `secretKeyRef` env var is
frozen at container start** — the server re-reads its env per request, but
Kubernetes does not update the value in a running pod, so a key rotation takes
effect only after the rollout that the annotation triggers.

**Local-dev escape (never for a reachable deployment):**
`security.apiKey.openMode: true` leaves the env unwired and `/mcp` runs open,
with a loud startup warning. It exists for a laptop, not a cluster.

### 2.2 Patches and cluster objects

Things that are not values. Each is either created by PCAI/the chart (and only
needs to *exist* in the cluster) or must be prepared once per site.

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| **Namespace** + `hpe-ezua/*` labels | PCAI discovers and monitors vendor services by label; the chart's helpers set them directly, and a Kyverno post-install hook re-applies them | The app runs but is invisible to PCAI's service catalogue and monitoring. If Kyverno is missing, the **hook fails the whole release** — set `ezua.kyverno.enabled: false` on clusters without the `kyverno.io` CRD (the labels are still set by the chart) | `kubectl -n <ns> get deploy sqlhandler -o jsonpath='{.metadata.labels}'` |
| **StorageClass supporting `ReadWriteMany`** | Shared volumes for catalog uploads (`semanticCatalog.store`), the policy file (`security.policy.claim`, chart-owned by default) and scratch writes (`writes.scratch.pvc`). Replicas must read the *same* file | An RWO class on a multi-replica deployment strands the extra pods `Pending` — and a policy store on per-pod volumes lets replicas disagree about the ACL. The chart **fails the render** when the access mode is not RWX for a scale-out deployment, but it cannot check what the class actually provides | `kubectl get storageclass` → a class with `ReadWriteMany`; `kubectl -n <ns> get pvc` → all `Bound` |
| **Policy ConfigMap** `sqlhandler-policy` (only when `security.policy.enabled: true` **and** `seedFromConfigMap: true`) | Seeds the writable policy file on first boot (`SQLHANDLER_POLICY_SEED_FILE`); it also carries the `admins` list — who may administer (§4.1). A policy that exists only in the ConfigMap is read-only and every admin edit fails | Pods sit in **`ContainerCreating`** with `FailedMount: configmap "sqlhandler-policy" not found`. With no policy at all the server starts **permissive** and logs "POLICY IS NOT RESTRICTING ANYTHING" | `kubectl -n <ns> get configmap sqlhandler-policy`; `kubectl -n <ns> get pvc sqlhandler-policy` (claim mode) |
| **oauth2-proxy provider in the mesh** | The chart renders an Istio `AuthorizationPolicy` with `action: CUSTOM`, provider `oauth2-proxy`, at the `ezaf-gateway` ingressgateway. Values: `ezua.authorizationPolicy.*` | The gateway has no authenticator to delegate to, so **every request to the endpoint is denied (403)** — and no caller identity reaches the app <!-- VERIFY: 403-vs-config-rejection behaviour of a CUSTOM policy with an unregistered provider is inferred from Istio semantics, not observed on this cluster --> | `kubectl -n istio-system get configmap oauth2-proxy` (or your provider's registration); then `curl -I https://sqlhandler.<domain>/ui` → 302 to the IdP, not 403 |
| **S3 bucket (and prefix)** | The chart points at a bucket; it never creates one. `s3.bucket` | `/ready` 503; `list_tables` fails with `NoSuchBucket`; the pod stays out of the Service | read `S3_BUCKET` from the running pod and list it from inside the cluster (§2.3) |
| **NFS PVC** (only when `backend: nfs`) | `nfs.mount.pvcName` is mounted read-only at `nfs.mount.mountPath` | Pods `Pending` on `FailedMount` / `persistentvolumeclaim not found` — with no actionable message at install time | `kubectl -n <ns> get pvc <name>` → `Bound`; `kubectl -n <ns> exec deploy/sqlhandler -- ls /data` |
| **metrics-server** (only when `autoscaling.enabled`) | The HPA is `autoscaling/v2` and needs resource metrics | The HPA reports `unknown` targets and never scales; the deployment stays at `replicaCount` | `kubectl top pods -n <ns>`; `kubectl -n <ns> get hpa` → targets not `<unknown>` |
| **Ingress NetworkPolicy** (only when `security.networkPolicy.enabled: true`) | Default-deny ingress, then allow the pod's namespace, the gateway namespace and `allowedNamespaces` | **In-cluster callers not on the list are cut off** — flipping this on is exactly what re-breaks a working direct caller | `kubectl -n <ns> get networkpolicy`; call the service from an allowed namespace |
| **Egress NetworkPolicy** (only when `security.networkPolicy.egress.enabled: true`) | Default-deny egress, then allow DNS plus the CIDRs/peers you list | The pod holds the Fabric/S3 credentials; with no egress rule it can reach any host, including the cloud metadata endpoint. Plain NetworkPolicy has no FQDN support — list CIDRs (or use Cilium for FQDNs) | `kubectl -n <ns> describe networkpolicy`; a blocked backend shows as a connect timeout in `/ready` |

### 2.3 External dependencies — prove them from the cluster

Every one of these is reachable from your laptop and *not* necessarily from the
pod. Test from inside the cluster, in the deployment's namespace, because that
is where the network policy, the DNS search path and the service account live:

```bash
kubectl -n <ns> exec deploy/sqlhandler -- python - <<'PY'
import socket
for host, port in [("minio.minio.svc.cluster.local", 9000),
                   ("rest-catalog", 8181),
                   ("postgresql.postgresql.svc.cluster.local", 5432)]:
    try:
        socket.create_connection((host, port), timeout=5).close(); print("OK  ", host, port)
    except Exception as e: print("FAIL", host, port, e)
PY
```

| Dependency | Proof | Common cause of failure |
|---|---|---|
| S3 / MinIO | `GET /ready` returns 200 and `list_tables` lists the bucket | Bucket does not exist; keys from a different account; TLS mismatch (`s3.useSsl`) |
| OneLake / Fabric | `/ready` 200; the AAD token request succeeds | Expired client secret; the service principal is not a workspace member; wrong lakehouse GUID |
| Iceberg REST catalog | `/ready` 200; `list_tables` returns namespaces | Catalog unreachable; token not wired (see the §2.1 defect) |
| Attached Postgres/MySQL | `list_tables` shows the database as available | Password Secret key mismatch; server not reachable under the pod's network policy; wrong port |
| NFS mount | `kubectl exec … ls /data` lists tables | PVC not bound, or exported to a different node pool |

The readiness probe already performs the first three checks on every probe
interval, which is why a broken credential shows up as a pod drained from the
Service rather than as errors served to agents.

## 3. Values

Paste a **full values document** into the Helm Values editor. Start from
[`../helm/values-examples/`](../helm/values-examples/) — `values.g2.yaml` for a
MinIO-backed deployment, `values.hosted-trial.yaml` for a minimal one,
`values-hardened-g2.yaml` for the production posture. Unknown keys are silently
ignored by Helm, so a typo'd key is a value that does nothing: check the key
against [`../helm/values.yaml`](../helm/values.yaml), which is the field
reference with a comment per key.

### 3.1 Required

```yaml
# Data source — pick ONE backend. Credential wiring (and Secret creation) is
# gated on this choice, so the backend must be explicit.
backend: s3            # s3 | minio | onelake | iceberg | nfs

# PCAI exposure — required while ezua.enabled (default true):
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "sqlhandler.${DOMAIN_NAME}"          # MCP clients: https://<endpoint>/mcp
    istioGateway: "istio-system/ezaf-gateway"
```

**The chart default is `s3`; the image default is `onelake`.** A Fabric
deployment must set `backend: onelake` explicitly or the Fabric credential is
never wired.

### 3.2 Optional, by group

Each group is one decision. Defaults are safe; nothing here is needed to boot.

```yaml
# Scale-out — one replica collapses under concurrency; the HPA is burst
# protection, not load-following (numbers: BENCHMARKS.md).
replicaCount: 4
autoscaling:   { enabled: true }        # HPA 4-8, CPU@80%, needs metrics-server
podDisruptionBudget: { enabled: true }  # keep 2 pods through drains
topologySpread:      { enabled: true }  # spread replicas across nodes

# Resources — raise LIMITS (not requests) for wide tables / large joins.
# DuckDB's in-query budget is duckdbMemoryFraction x the memory LIMIT.
resources:
  requests: { cpu: 250m, memory: 1Gi }
  limits:   { cpu: "4",  memory: 16Gi }

# Gateway authn — keep ON wherever external callers reach the endpoint.
ezua:
  authorizationPolicy: { enabled: true, namespace: istio-system, providerName: oauth2-proxy }

# /mcp API-key auth — mandatory; point at the Secret you created in §2.1.
security:
  apiKey: { existingSecret: "", existingSecretKey: api-keys }   # "" = <fullname>-apikey

# Metadata + query caches (see FEATURES.md §10).
cache:
  ttl: 3600
  prewarmTables: "work_order_header,work_order_note_recent"

# Security posture — hardened profile is the default; the ingress
# NetworkPolicy is OFF so in-cluster callers work with zero friction.
security:
  networkPolicy: { enabled: true, allowedNamespaces: ["<client-namespace>"] }
```

Set `ezua.enabled: false` to drop the PCAI integration (VirtualService,
AuthorizationPolicy, Kyverno) and deploy as a plain MCP server.

### 3.3 Data-source blocks, backend by backend

**S3 / MinIO (`backend: s3`, the chart default).** Every Parquet file or folder
under `bucket` (plus `prefix`) becomes a table; Hive-partition folders fold in;
folders carrying a Delta `_delta_log` are auto-detected as Delta. Credentials
come from `s3-credentials` (§2.1).

```yaml
backend: s3
s3:
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"   # or AWS/any S3
  bucket: "<bucket>"
  prefix: "<optional/subtree>"
  anonymous: false             # true = public bucket, no Secret needed
  credentialsSecret: { name: s3-credentials, create: false }
```

**OneLake / Fabric (`backend: onelake`).** Delta Lake over ABFS, authenticated by
the service principal in `fabric-credentials`; discovery uses the OneLake DFS
REST API and tables live under `Tables/<schema>/<table>`.

```yaml
backend: onelake
fabric:
  credentialsSecret: { name: fabric-credentials, create: false }
  lakehouseAbfssUrl: "abfss://<workspace-guid>@onelake.dfs.fabric.microsoft.com/<lakehouse-guid>"
  # or workspaceId + lakehouseId instead of the full URL
```

**Iceberg (`backend: iceberg`).** Tables are discovered through a REST (default)
or SQL catalog; the Parquet data files live in your object store.

```yaml
backend: iceberg
iceberg:
  catalogType: rest
  catalogUri: "http://rest-catalog:8181"
  credentialsSecret: { name: iceberg-credentials, create: false }
  storage: { endpointUrl: "http://minio:9000" }
```

**NFS / mounted directory (`backend: nfs`).** Delta **and** Parquet from one
mounted directory; no credentials.

```yaml
backend: nfs
nfs:
  rootDir: /data
  mount: { enabled: true, pvcName: my-data-pvc }   # mounted read-only at /data
```

**Federated multi-source (`sources:`).** Several buckets or sources of any
backend mix behind one endpoint; when non-empty it **overrides `backend:`**.
Tables are source-qualified (`sales_orders`), and bare names resolve only when
unique. Per-source credentials land in the ConfigMap as JSON — for production,
mount `SQLHANDLER_SOURCES` from a Secret instead of embedding keys in values.

```yaml
sources:
  - { name: sales,     backend: s3, endpointUrl: "http://minio:9000", bucket: sales-bucket }
  - { name: inventory, backend: s3, endpointUrl: "http://minio:9000", bucket: inventory-bucket, prefix: raw }
```

**External databases, read-only (`databases:`).** Postgres/MySQL tables become
queryable, and joinable with lake tables in one query, as
`<name>.<schema>.<table>`. Read-only is enforced by DuckDB (`ATTACH … READ_ONLY`).

```yaml
databases:
  - name: ops
    type: postgres        # postgres | mysql
    host: postgresql.postgresql.svc.cluster.local
    port: 5432
    database: opsdb
    user: ro_user
    password: ""          # "" = manage <fullname>-db-credentials out-of-band
```

> **DuckDB file access is locked down** on every query connection: local-file
> reads (`read_csv`, `COPY … TO`) and URL fetches fail closed, while the
> registered pyarrow datasets scan normally. Re-enable with
> `SQLHANDLER_DUCKDB_FILE_ACCESS=1` only if a query genuinely needs it.

### 3.4 Semantic catalog

The catalog gives agents business meaning — table/column descriptions merged
into `list_tables`, `describe_table` and the MCP resources. Three attach
routes, all optional: **in values** (most auditable — paste native YAML under
`semanticCatalog.tables`, rendered to a read-only ConfigMap);
**`semanticCatalog.existingConfigMap`** (a ConfigMap you own, key
`semantic-catalog.json`); or **upload from the UI/API** after deploy. The engine
hot-reloads the file on mtime, so a catalog edit plus a re-apply updates the
docs **without a pod restart**, and a broken catalog degrades to empty rather
than failing queries.

```yaml
semanticCatalog:
  enabled: true
  tables:
    workorder/work_order:
      description: Maintenance work order headers, one row per order
      aliases: [work orders]
      columns:
        amount: Order total in USD
```

Uploads are per-replica on an emptyDir unless
`semanticCatalog.store.enabled: true` (a 1Gi **RWX** PVC, `<release>-catalog`);
that same claim also carries saved queries (`savedQueriesPath`) and can carry
the admin-keys store. Full spec, key forms and virtual tables:
[`../docs/semantic-catalog.md`](../docs/semantic-catalog.md).

## 4. Import and install

1. **Create the Secrets** for your backend (§2.1) in the deployment namespace.
   Nothing starts until they exist.
2. **Satisfy the cluster prerequisites** (§2.2): RWX StorageClass, the policy
   ConfigMap if policy-as-code is on, the gateway's oauth2-proxy provider.
3. **Import the chart** into PCAI: upload `sqlhandler-4.0.0.tar.gz` (AI
   Essentials / EzAppConfig import). PCAI stores it in ChartMuseum.
4. **Create the deployment** and open its **Helm Values** editor. Paste your
   values document (§3), leaving ${DOMAIN_NAME} for PCAI to substitute.
5. **Apply.** PCAI renders and installs; the endpoint becomes
   `https://sqlhandler.<domain>` (`/mcp` for agents, `/ui` for humans).

Then verify, in this order:

```bash
kubectl -n <ns> get pods                 # Running, probes green, no restarts
kubectl -n <ns> logs deploy/sqlhandler | grep -i "API-key auth"   # → ENABLED
curl -s https://sqlhandler.<domain>/ready | head -c 200           # backend reachable
curl -s -H "X-API-Key: <key>" -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     https://sqlhandler.<domain>/mcp | head -c 300                # 23 tools
```

`/health` is the process, `/ready` is the data source, and the log line is the
auth posture — a pod can be Running and still be drained from the Service if
`/ready` is failing. Full check list, including a sample query:
[VERIFICATION.md](VERIFICATION.md).

**Connect an MCP client** (any standard client; the key is the `/mcp` gate):

```json
{ "mcpServers": { "sqlhandler": {
    "url": "https://sqlhandler.<domain>/mcp",
    "headers": { "X-API-Key": "<key>" } } } }
```

Through the PCAI gateway the browser token comes from oauth2-proxy; in-cluster
callers still need a credential for `/api/*` and `/mcp` because the chart sets
`SQLHANDLER_REQUIRE_IDENTITY=1` by default (audit 2026-11-12, P1-6) — pass
`X-API-Key`, or opt out with `security.identity.requireIdentity: false` for a
gateway-only posture. Per-client examples:
[MCP.md §5](MCP.md#5-connecting-an-mcp-client).

### 4.1 First admin — the two objects that must agree

The credential and the policy are **independent objects**, and creating one
does not create the other:

| Object | Wired to | Answers | Refusal |
|---|---|---|---|
| Secret `<fullname>-apikey` (`api-keys`) | `MCP_API_KEYS` | *who are you* — authentication | `401` |
| ConfigMap `sqlhandler-policy` (`policy.json`) | the live policy file | *what may you do* — ACL **and** `admins` | `403` |

The only link between them is a **fingerprint**: `sha256:` + the first 12 hex
characters of `sha256(raw key)`, written into the policy's `admins` list.
Nothing derives it automatically, so a fingerprint that disagrees with the
Secret locks the panel out — the old key `401`s (not configured) and the new
one `403`s (not designated).

**One command that gets it right** (it computes the fingerprint with the
server's own function and emits both objects from a single key):

```bash
python scripts/bootstrap_admin.py --subject <your-sso-name>            # print the YAML
python scripts/bootstrap_admin.py --subject <your-sso-name> --apply    # apply it
```

Then deploy with `security.policy.enabled: true` and
`security.policy.existingConfigMap: sqlhandler-policy`.

**Bootstrap fallback (2026-11-12).** When *no policy document is enforcing*
(policy disabled, or enabled with no file loaded yet) the configured API key
**is** the administrator — otherwise a fresh install has no reachable admin at
all. That state is reported on `/ready` as `bootstrap_admin` and logs one
`BOOTSTRAP ADMIN ACTIVE` warning (`/health` keeps its pinned liveness shape). Set
`security.policy.bootstrapAdmin: false` for the strict posture (nobody
administers until `admins` designates them).

The fallback deliberately does **not** cover a policy that is loaded but whose
`admins` matches nothing — that stays fail-closed, because a typo'd
fingerprint must never grant admin. The `403` body names which case you are in:

| `403` says | Meaning | Fix |
|---|---|---|
| `no policy is loaded …` | no policy file is enforcing | author one, or keep the bootstrap fallback on and present a key |
| `the loaded policy designates no admins` | the policy enforces but has no `admins` entry | add `subject:<name>` or `key:sha256:<12hex>` to `admins` |
| `key fingerprint sha256:… is not in the policy's admins list` | you are authenticated but not designated | add that fingerprint, or sign in as a designated subject |
| `the policy file is INVALID` | present-but-broken; the deployment is failing closed | fix the file (the pods stay reachable, data stays hidden) |

**Rotation trap.** `admins` holds a *fingerprint*, not the key. Rotate the
Secret and the designation dangles unless you also update `admins` — in the
**live** policy file (`/etc/sqlhandler-policy/policy.json` on the policy
claim), *not* the ConfigMap, which only seeds a fresh install. `subject:<name>`
entries survive rotation; prefer them wherever SSO is available.

## 5. Upgrade, rollback and uninstall

### 5.1 The PCAI way

- **Values change** (the normal path): edit the values document, apply. PCAI
  re-renders and rolls the pods; the `checksum/*` annotations make a Secret or
  ConfigMap change roll them too.
- **New chart or image version**: import the new packaged chart, re-apply with
  the same values document. The packaged `image.tag` moves with the chart, so
  the image is not a value you maintain.
- **Semantic-catalog edits** hot-reload on file change — a re-apply updates
  `list`/`describe` output without a restart, and in-flight tool calls are not
  disturbed.
- **Rollback**: re-apply the previous values document, and re-import the
  previous chart version if the chart itself moved. There is no `helm rollback`
  in the PCAI path — the values document *is* the state, so keep it in version
  control.
- **Uninstall**: delete the deployment in PCAI. Secrets, ConfigMaps and PVCs you
  created out-of-band are **not** removed with it (they are namespace-scoped, not
  release-scoped); delete them explicitly if the data must go too.

### 5.2 Credential rotation

Update the Secret out-of-band, then re-apply (the checksum annotation rolls the
pods). For `/mcp` keys the value is a comma-separated list: append the new key,
move clients over, drop the old, apply. A running pod keeps the old env value
until it is replaced — the server re-reads its env per request, but Kubernetes
does not update a `secretKeyRef` in a running container.

`/mcp` key rotation has a second step that is easy to miss: the policy's
`admins` designates a key by **fingerprint**. After appending the new key,
update `admins` in the **live** policy file — otherwise the old fingerprint
names a key nothing accepts and key-based admin is gone (old key `401`, new key
`403`). `subject:<name>` entries survive rotation. See §4.1.

### 5.3 Plain Helm (developers only, not the PCAI path)

For chart work on a cluster you control:

```bash
helm template sqlhandler helm/ -n sqlhandler -f helm/values-examples/values.g2.yaml   # inspect
helm upgrade --install sqlhandler helm/ -n sqlhandler -f <your-values.yaml>            # deploy
```

A PCAI deployment created this way is outside PCAI's controller: no
${DOMAIN_NAME} substitution, no vendor-service labelling, and PCAI will not manage its
upgrades. Use it to review a render, not to run a site.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `CreateContainerConfigError`, never starts | The mandatory `<fullname>-apikey` Secret (or a backend credential Secret) is missing, or has the wrong key name | Create the Secret from §2.1 with exactly the key names in the table; `kubectl -n <ns> describe pod` names the missing key |
| Pods `Pending` on `persistentvolumeclaim "…" not found` | An `existingClaim`/`pvcName` value points at a claim that was never applied, or the StorageClass cannot bind RWX | Prefer the chart-owned claim (`security.policy.claim.create`, `semanticCatalog.store.enabled`); otherwise apply the PVC first. The render cannot verify a claim you own |
| Pods `ContainerCreating` for minutes, volume attached, **no mount error** | Two volumes mounted at overlapping paths, or **one claim mounted twice** (catalog store + policy) — live-seen 2026-10-20, 8+ minutes, zero ready replicas | Keep `security.policy.mountPath` out of `semanticCatalog.mountPath` (the chart fails the render on overlap) and give the policy its own claim. Never reuse `<release>-catalog` for the policy |
| Running, but `/ready` 503 and the pod is drained from the Service | The backend is unreachable or the credential is wrong — by design the probe is a real connectivity check | Check the §2.3 table from inside the cluster; a wrong key or an expired client secret looks exactly like an outage |
| `403` on every request through the gateway | The oauth2-proxy provider the AuthorizationPolicy names does not exist in the mesh, or `ezua.authorizationPolicy` points at the wrong namespace/provider | Fix `ezua.authorizationPolicy.providerName`/`namespace`, or disable it only if something else authenticates the endpoint |
| Admin / Access-control panel returns 403 | The policy's `admins` list does not contain your credential fingerprint (`key:sha256:<12hex>`) or SSO subject — or no policy is loaded at all | The 403 body names the cause (no policy / no admins entry / not on the list). For a new site run `scripts/bootstrap_admin.py`; otherwise add the fingerprint to `admins` in the **live** file (`/etc/sqlhandler-policy/policy.json`), not the ConfigMap. See §4.1 and [ADMIN-WRITES.md](ADMIN-WRITES.md) |
| Every admin edit fails with `[Errno 30] Read-only file system` | The policy is mounted from a **ConfigMap** (read-only by construction), so writes can never persist | Use claim mode (`security.policy.claim.create: true`, the default) — see [ADMIN-WRITES.md](ADMIN-WRITES.md) |
| `Unknown job id` on `query_status` / `query_result` | The async-job registry is **per replica**; the poll landed on a different pod | Single replica for job-heavy use, or a shared `SQLHANDLER_JOBS_DIR` — not chart-wired yet ([CONFIGURATION.md §9](CONFIGURATION.md#9-what-the-chart-does-not-wire)) |
| In-cluster callers broke after enabling `security.networkPolicy` | Default-deny ingress with a caller namespace not in `allowedNamespaces` | Add the namespace (no client-side change needed) or turn the policy back off for the trial |
| Iceberg REST catalog returns unauthenticated | The token env is only wired when `iceberg.credentialsSecret.values.token` is non-empty, so an out-of-band-only token never reaches the container | Known chart defect (§2.1). Until it is fixed, keep the token in the Secret **and** set the values key (accepting that the token then lives in the values document), or use a catalog that needs no token |
| Release fails at install with a Kyverno hook error | The cluster has no `kyverno.io` CRD, so the post-install hook cannot apply | Set `ezua.kyverno.enabled: false` — the `hpe-ezua` labels are still set by the chart helpers |
| A values key seems to have no effect | Helm ignores unknown keys silently, and the chart exposes a fixed env set — several capabilities are image-only | Check the key against [`../helm/values.yaml`](../helm/values.yaml) and [CONFIGURATION.md §9](CONFIGURATION.md#9-what-the-chart-does-not-wire) |

## 7. What the chart renders

When `ezua.enabled: true` (default):

- **Deployment + Service** (ClusterIP, port 9097, MCP at `/mcp`) — liveness on
  `/health`, a **backend-aware readiness probe** on `/ready` (503 when the data
  source is unreachable, so broken-credential pods are drained instead of
  serving errors), and a startup probe gating both while the server warms up.
- **Istio VirtualService** routing `/mcp` and `/api` with a **long timeout**
  (`ezua.virtualService.longTimeout`, default 3600s) for agent loops and
  streaming, a short timeout elsewhere.
- **Istio AuthorizationPolicy** (when `ezua.authorizationPolicy.enabled`) —
  `action: CUSTOM`, provider `oauth2-proxy`, applied at the `ezaf-gateway`
  ingressgateway.
- **Kyverno ClusterPolicy** (when `ezua.kyverno.enabled`) — post-install hook
  tagging the workload `hpe-ezua/type: vendor-service` and
  `hpe-ezua/app: sqlhandler`.
- **Scale-out objects** when enabled: HPA (autoscaling/v2, needs metrics-server),
  PodDisruptionBudget, topology spread, a 90 s termination grace period for
  in-flight MCP tool calls, and the shared PVCs described in §2.2.
- **A PodMonitor** when `metrics.podMonitor.enabled` (default true). **Set
  `metrics.podMonitor.additionalLabels` to your Prometheus's
  `podMonitorSelector` labels or the target never appears** in the scrape
  config — a silent no-op is the usual failure here.
- **Hardened workload profile** (`security.hardened: true`, the default):
  non-root (1000), read-only root filesystem with a writable `/tmp` emptyDir,
  no ServiceAccount token, seccomp `RuntimeDefault`, dropped capabilities.
- **Optional routing**: `routing.affinity.enabled` renders a DestinationRule
  that hashes a header (`routing.affinity.headerKey`, e.g. an agent session id)
  for session affinity. Behaviour changes only for callers that send the header.

## 8. Reference

| What | Where |
|---|---|
| Every values key, with comments | [`../helm/values.yaml`](../helm/values.yaml) |
| Paste-ready site examples | [`../helm/values-examples/`](../helm/values-examples/) |
| Secret convention and local values | [`../helm/local/README.md`](../helm/local/README.md) |
| Every environment variable | [CONFIGURATION.md](CONFIGURATION.md) |
| Feature reference and design rationale | [FEATURES.md](FEATURES.md) |
| MCP endpoint, tools, clients | [MCP.md](MCP.md) |
| Semantic catalog spec | [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md) |
| Admin writes and the policy file | [ADMIN-WRITES.md](ADMIN-WRITES.md) |
| Post-deploy verification | [VERIFICATION.md](VERIFICATION.md) |
| Scaling evidence | [BENCHMARKS.md](BENCHMARKS.md) |
