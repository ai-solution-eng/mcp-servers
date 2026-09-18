# Deploying SQLhandler on PCAI

Full deployment guide for **PCAI (HPE Private Cloud AI / HPE Ezmeral Unified
Analytics)**. Field reference for every key: [`../helm/values.yaml`](../helm/values.yaml) ·
paste-ready examples: [`../helm/values-examples/`](../helm/values-examples/) ·
post-deploy checks: [VERIFICATION.md](VERIFICATION.md).

SQLhandler ships as a **PCAI MCP 2.0** server — built on the `mcp>=2.0` SDK's
low-level `Server`, serving the standard MCP protocol (initialize handshake)
over stateless streamable-http at `/mcp`, so any standard MCP client can
connect (DSH, official Python/TS SDKs, MCP Inspector, OWUI).

## 1. How deployment works on PCAI

PCAI is a Kubernetes wrapper: **users never run `helm install` or
`kubectl apply`**. The whole deployment is driven by the chart's values:

1. **Import** the packaged `sqlhandler` chart (`.tar.gz`) into PCAI once.
2. Open the deployment's **Helm Values** editor (or use the PCAI API).
3. **Paste a full values document** (start from
   [`helm/values-examples/`](../helm/values-examples/)) and adjust the
   `# SITE:` lines.
4. **Apply.** PCAI resolves `${DOMAIN_NAME}` before rendering.

There is one chart; the "variant" is the **data-source backend** you select
(`backend:` + its credential block). Everything else — service, Istio, probes,
security profile — is shared.

To change anything later, **edit the same values document and re-apply** — that
is the upgrade (see §9).

## 2. Credentials first (out-of-band Secret)

The chart defaults to `credentialsSecret.create: false`: you create the
Kubernetes Secret yourself so credentials never pass through values files or
the Helm release secret (which is readable with `helm get values <release>
-a`). In PCAI, create the Secret in the deployment's namespace out-of-band, or
— for throwaway test clusters only — set `create: true` and let the chart
render it from values.

```bash
# S3 / MinIO (keys must match s3.credentialsSecret.accessKeyKey/secretKeyKey)
kubectl -n <namespace> create secret generic s3-credentials \
  --from-literal=access-key=<S3_ACCESS_KEY> \
  --from-literal=secret-key=<S3_SECRET_KEY>

# OneLake / Fabric service principal
kubectl -n <namespace> create secret generic fabric-credentials \
  --from-literal=tenant-id=<tenant-id> \
  --from-literal=client-id=<client-id> \
  --from-literal=client-secret=<client-secret>
```

Read back / rotate:

```bash
kubectl -n <namespace> get secret s3-credentials -o jsonpath='{.data.access-key}' | base64 -d; echo
```

The Deployment template carries `checksum/secret` + `checksum/config`
annotations, so a rotated Secret rolls the pod on the next re-apply. Full
convention: `helm/local/README.md` (local, not packaged).

## 3. Required values

These are the keys a working deployment cannot do without; everything else has
a safe chart default. The image tag is packaged with the chart
(`image.tag: v1.6.1` for the 1.6.1 chart) — set it only to track a newer
released image.

```yaml
# Data source — pick ONE backend (credential wiring is gated on the choice):
backend: s3            # s3 | minio | onelake | iceberg | nfs
s3:                    # when backend: s3
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"
  bucket: "<bucket>"           # required
  prefix: ""                   # optional sub-tree
  credentialsSecret:
    create: false              # Secret created out-of-band (§2)
    name: s3-credentials

# PCAI exposure — required while ezua.enabled (default true):
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "sqlhandler.${DOMAIN_NAME}"     # MCP clients: https://<endpoint>/mcp
    istioGateway: "istio-system/ezaf-gateway"
```

When using `backend: onelake` you **must set it explicitly** — the chart
default is `s3` and the per-backend credential wiring (and Secret creation) is
gated on the backend.

## 4. Optional values

```yaml
# Scale out (see documentation/BENCHMARKS.md — one replica collapses under
# concurrency; the HPA owns the replica count):
replicaCount: 4              # advisory while the HPA is on
autoscaling:
  enabled: true              # HPA 4-8 replicas, CPU@80% — burst/runaway protection
podDisruptionBudget:
  enabled: true              # keep 2 pods through node drains
topologySpread:
  enabled: true              # spread replicas across nodes

# Resources (chart default below; raise LIMITS — not requests — for wide
# tables / large joins; DuckDB's memory budget is duckdbMemoryFraction × the
# memory limit):
resources:
  requests: { cpu: 250m, memory: 1Gi }
  limits:   { cpu: "4",  memory: 16Gi }

# Gateway authn: oauth2-proxy AuthorizationPolicy (enabled by default; keep ON
# wherever external callers reach the endpoint through the PCAI gateway):
ezua:
  authorizationPolicy:
    enabled: true
    namespace: istio-system
    providerName: oauth2-proxy

# Semantic catalog (see §6) + metadata/query caches:
semanticCatalog:
  enabled: true
  tables:
    <schema/name>:
      description: ...
cache:
  ttl: 3600
  prewarmTables: "table_a,table_b"

# Security posture (default: hardened profile, ingress NetworkPolicy OFF so
# in-cluster callers work with zero friction):
security:
  networkPolicy:
    enabled: true                 # for production / real data
    allowedNamespaces: ["<client-namespace>"]
```

Set `ezua.enabled: false` to drop the PCAI integration (VirtualService,
AuthorizationPolicy, Kyverno) and deploy as a plain MCP server.

## 5. Data-source setup

All backends share the same SQL engine, caches, MCP tools, and a backend-aware
readiness probe; only the `DataProvider` behind them differs.

### 5.1 S3 / MinIO (`backend: s3` — chart default)

Every Parquet file or folder of Parquet files under `bucket` (+ optional
`prefix`) becomes a table; Hive-partition folders fold into the table. Folders
carrying a Delta `_delta_log` are auto-detected as Delta tables
(`s3.format: auto` → `S3_FORMAT`), so one bucket can mix formats with time
travel where a Delta log exists.

```yaml
backend: s3
s3:
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"   # or AWS/any S3
  bucket: "<bucket>"
  prefix: "<optional/subtree>"
  anonymous: false             # true reads a public bucket without a Secret
  credentialsSecret:
    name: s3-credentials
    create: false
```

### 5.2 OneLake / Fabric (`backend: onelake`)

Delta Lake over ABFS, authenticated with an Entra service principal. Table
discovery uses the OneLake DFS REST API; tables live under
`Tables/<schema>/<table>`.

```yaml
backend: onelake
fabric:
  credentialsSecret:
    name: fabric-credentials
    create: false              # preferred; create: true is bootstrap-only
  lakehouseAbfssUrl: "abfss://<workspace-guid>@onelake.dfs.fabric.microsoft.com/<lakehouse-guid>"
  # or workspaceId + lakehouseId instead of the full URL
```

The chart wires `FABRIC_TENANT_ID` / `FABRIC_CLIENT_ID` / `FABRIC_CLIENT_SECRET`
from the `fabric-credentials` Secret.

### 5.3 Iceberg (`backend: iceberg`)

Tables are discovered through an Iceberg REST (default) or SQL catalog; the
Parquet data files live in your object store.

```yaml
backend: iceberg
iceberg:
  catalogType: rest
  catalogUri: "http://rest-catalog:8181"
  credentialsSecret:
    name: iceberg-credentials
    create: false              # REST token + S3 access/secret keys
  storage:
    endpointUrl: "http://minio:9000"
```

### 5.4 NFS / mounted directory (`backend: nfs`)

Reads Delta **and** Parquet from a mounted directory. No credentials.

```yaml
backend: nfs
nfs:
  rootDir: /data
  mount:
    enabled: true
    pvcName: my-data-pvc       # mounted read-only at /data
```

The readiness probe verifies the mount is present.

### 5.5 Federated multi-source (`sources:`)

Several buckets/sources (any mix of backends) behind one endpoint —
cross-source `JOIN`s work in a single `run_sql`. When non-empty, `sources:`
overrides `backend:`.

```yaml
sources:
  - name: sales
    backend: s3
    endpointUrl: "http://minio.minio.svc.cluster.local:9000"
    bucket: sales-bucket
  - name: inventory
    backend: s3
    endpointUrl: "http://minio.minio.svc.cluster.local:9000"
    bucket: inventory-bucket
    prefix: raw
```

Tables are source-qualified (`sales_orders`, `inventory_raw_customers`); bare
names resolve only when unique. Source labels must be unique and
DuckDB-identifier-safe. Per-source credentials in `sources:` land in the
ConfigMap as JSON (`SQLHANDLER_SOURCES`) — for production, mount that env var
from a Secret instead of embedding keys in values.

### 5.6 External databases (read-only attach)

External database servers — Postgres, MySQL/MariaDB, SQLite files, SQL Server —
whose tables become queryable — and join-able with lake tables in **one** query
— as `<name>.<schema>.<table>`. Read-only is enforced by DuckDB itself
(`ATTACH ... READ_ONLY`).

```yaml
databases:
  - name: ops
    type: postgres        # postgres | mariadb | mysql | sqlite | sqlserver
                          # (omit `type` to default to postgres)
    host: postgresql.postgresql.svc.cluster.local
    port: 5432
    database: opsdb
    user: ro_user
    password: ""          # "" → manage the <release>-db-credentials Secret out-of-band
    params:               # driver options (TLS etc.) merged into the DSN
      sslmode: verify-full
      sslrootcert: /etc/sqlhandler/certs/ca.crt
```

Valid types are `postgres`, `mariadb`, `mysql`, `sqlite`, `sqlserver`
(`postgresql` is not accepted). The per-entry `params` object carries driver
options — notably TLS, which many production endpoints require — merged over
each type's built-in DSN keys: an existing key is overridden in place, a new
key is appended, no duplicate key is ever emitted. Keys that would carry
secrets or duplicate the entry's own fields (`password`, `user`, `host`,
`port`, `database`, `server`, …) are rejected loudly, and values are restricted
to an injection-safe charset. MySQL/MariaDB TLS rides the libmariadb keys
(`ssl_mode` — default `preferred` — plus `ssl_ca`, `ssl_cert`, `ssl_key`). Note
that the mysql/mariadb scanner takes DSN values bare (whitespace-split, no
quote handling): for those two types the entry fields (`host`/`database`/`user`)
and `params` values must not contain whitespace — a resolved password with
whitespace is rejected at attach time; postgres (libpq-quoted) and sqlserver
(semicolon-delimited) accept spaces.

The generated `SQLHANDLER_ATTACH` config carries env-var **names** only
(`SQLHANDLER_DB_PW_<NAME>`); password values come from the Secret. A database
that is down does not fail the lake.

`type: sqlite` attaches a **file**, not a server: `database` is the sqlite FILE
PATH and there is no host/port/user (`password_env` may be omitted; `params`
are not accepted). The file is read at query time through the extension's own
bundled sqlite3 library — outside DuckDB's filesystem lockdown — but the path
is operator-configured in `SQLHANDLER_ATTACH` (never model-chosen), the same
trust model as the nfs backend. `READ_ONLY` is still enforced.

`type: sqlserver` rides DuckDB's community **`mssql`** extension, baked into
the image (the old ODBC-based `sqlserver` extension does not exist on DuckDB
1.5.x). It speaks native TDS 7.4 — no unixODBC and no Microsoft ODBC driver
are needed. An explicit `user`/`user_env` is required (there is no `sa`
default). TLS is ON by default: `Encrypt=yes;TrustServerCertificate=yes` are
lenient defaults that `params` can override — in the mssql extension the two
keys are synonyms, so set only one of them; see the
[mssql extension docs](https://duckdb.org/community_extensions/extensions/mssql)
for TLS options.

> **DuckDB file access is locked down** on every query connection: local-file
> reads (`read_csv`, `COPY ... TO`) and URL fetches fail closed; all
> object-store IO is done by pyarrow outside DuckDB. (`SQLHANDLER_DUCKDB_FILE_ACCESS=1`
> re-enables it when a query genuinely needs it.)

## 6. Semantic catalog

The semantic catalog gives agents business meaning — table/column descriptions
merged into `list_tables` / `describe_table` output and the MCP resources.
Spec and data-owner guide: [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md).
Three attach routes (any backend):

1. **In values (most auditable).** Paste the catalog as **native YAML** under
   `semanticCatalog.tables:` (no stringification) — or as an inline JSON/YAML
   string (`semanticCatalog.json` / `semanticCatalog.yaml`, mutually
   exclusive) for machine-generated catalogs. The chart renders it to a
   ConfigMap mounted read-only and points `SQLHANDLER_CATALOG` at it.

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

   Entries with a `definition:` (a single read-only `SELECT`/`WITH`) become
   **virtual tables**. The engine **hot-reloads** the file on change, so a
   catalog edit + re-apply updates the docs **without a pod restart**. A
   broken catalog never breaks queries — it degrades to empty.

2. **Existing ConfigMap.** `semanticCatalog.existingConfigMap: <name>` mounts a
   ConfigMap you own that carries a `semantic-catalog.json` key.

3. **Upload from the UI / API (no file access needed).** Once deployed, anyone
   with UI access can enable it from the **Semantic catalog** panel of the
   data explorer (`/ui`): upload a `.json`/`.yaml` file or paste it — effective
   immediately, no restart. Same operations as API endpoints
   (`POST/GET/DELETE /api/semantic-catalog`, per-table upsert via
   `/api/semantic-catalog/table`).

   By default uploads are **per-replica and pod-local** (emptyDir). Set
   `semanticCatalog.store.enabled: true` to back uploads with a shared RWX PVC
   (1Gi default, `<release>-catalog`): an upload from any replica reaches every
   replica and survives rescheduling, still hot-reloaded. **ReadWriteMany is
   required whenever replicas can exceed 1** — the render fails loudly on an
   RWO store for a scale-out deployment. The upload API is a mutating endpoint
   covered by gateway auth / `SQLHANDLER_API_TOKEN`; `SQLHANDLER_CATALOG_UPLOAD=0`
   disables applying entirely.

## 7. What the chart renders on PCAI

When `ezua.enabled: true` (default), the chart renders:

- **Deployment + Service** (ClusterIP, port 9097, MCP at `/mcp`) — liveness
  probe on `/health` and a **backend-aware readiness probe** on `/ready` that
  performs a cheap connectivity check on the selected backend and returns 503
  when the data source is unreachable, so broken-credential pods are drained
  from the Service instead of serving errors. A startup probe gates both while
  the server warms up.
- **Istio VirtualService** routing `/mcp` and `/api` to the service with a
  **long timeout** (`ezua.virtualService.longTimeout`, default 3600s) for agent
  loops / streaming, short timeout for the rest.
- **Istio AuthorizationPolicy** (when `ezua.authorizationPolicy.enabled`) —
  `action: CUSTOM`, provider `oauth2-proxy`, applied at the `ezaf-gateway`
  ingressgateway so external callers authenticate at the PCAI gateway.
- **Kyverno ClusterPolicy** (when `ezua.kyverno.enabled`) — post-install hook
  tagging the workload `hpe-ezua/type: vendor-service` + `hpe-ezua/app:
  sqlhandler` for PCAI discovery/monitoring. Disable on clusters without the
  `kyverno.io` CRD — the hook would otherwise fail the whole release (the same
  labels are also set directly by the chart helpers).
- **Scale-out objects** when enabled: `HorizontalPodAutoscaler` (autoscaling/v2;
  needs metrics-server), `PodDisruptionBudget`, topology-spread constraints, a
  90s termination grace period for in-flight MCP tool calls, and — with
  `semanticCatalog.store.enabled` — the shared catalog PVC.
- **Hardened workload profile** (`security.hardened: true`, the default):
  non-root (1000), read-only root filesystem (+ `/tmp` emptyDir), no
  ServiceAccount token, seccomp `RuntimeDefault`, dropped capabilities.

**Network posture:** the ingress NetworkPolicy is **off by default** — any pod
in the cluster can call `http://<service>.<ns>.svc.cluster.local:9097/mcp`
directly, and the ClusterIP Service exposes nothing outside the cluster (right
posture for short-lived trials). For production / real data, enable
`security.networkPolicy.enabled` and allowlist direct callers via
`allowedNamespaces`; egress is never restricted (the server must reach
AAD/OneLake/S3).

## 8. Connect an MCP client

```json
{
  "mcpServers": {
    "sqlhandler": {
      "url": "https://sqlhandler.<your-domain>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Through the PCAI gateway the bearer token comes from PCAI auth (oauth2-proxy);
in-cluster callers need no header. Humans get the same data at
`https://sqlhandler.<your-domain>/ui` behind the same gateway.

## 9. Upgrading

- **Values change** (the normal path): edit the deployment's values document in
  the PCAI Helm Values editor → apply. PCAI re-renders and rolls the pods.
- **Semantic catalog edits** hot-reload on file change — a re-apply updates
  list/describe output without a pod restart; in-flight tool calls are not
  disrupted.
- **Credential rotation**: update the out-of-band Secret, then re-apply the
  deployment (the checksum annotations force the rollout).
- **New chart/image version**: import the new packaged chart and re-apply with
  your values document; the packaged `image.tag` moves with the chart.

## 10. Reference

| What | Where |
|---|---|
| Every values key, with comments | [`../helm/values.yaml`](../helm/values.yaml) |
| Paste-ready site examples | [`../helm/values-examples/`](../helm/values-examples/) |
| Secret convention (local, not packaged) | `helm/local/README.md` |
| Semantic catalog spec | [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md) |
| Post-deploy verification & troubleshooting | [VERIFICATION.md](VERIFICATION.md) |
| Scaling evidence | [BENCHMARKS.md](BENCHMARKS.md) |
