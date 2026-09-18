# SQLhandler

Fast, direct SQL access to columnar data, exposed as an **MCP server** — a drop-in **EzPresto / PrestoDB replacement**. It reads tabular data **directly** from the source with pyarrow and queries it with **DuckDB** over the exposed pyarrow datasets, pushing predicates / column projection into the scan.

Four backends are supported (selected by `SQLHANDLER_BACKEND`):

- **onelake** (default) — Microsoft Fabric OneLake (Delta Lake over ABFS)
- **s3 / minio** — S3-compatible object storage (Parquet files)
- **iceberg** — Apache Iceberg tables through a catalog (REST or SQL), Parquet data files
- **nfs** — a mounted directory (NFS / PVC / hostPath): Delta Lake **and** Parquet

They all share the same SQL engine, caches, MCP tools, and backend-aware readiness probe; only the `DataProvider` behind them differs. See [Backend comparison](#backend-comparison-onelake-fabric-vs-s3-minio).

## Backend comparison: OneLake (Fabric) vs S3 (MinIO)

Both backends speak the same MCP tools and SQL engine. The differences are only in *where* the data lives, how you authenticate, and how tables are discovered:

| Aspect | OneLake (`onelake`) | S3 / MinIO (`s3`) |
|---|---|---|
| Format | Delta Lake (Parquet + `_delta_log`) | Parquet (plain or Hive-partitioned) |
| Credentials | Entra **service principal** (tenant/client/secret) | S3 **access/secret key**, or anonymous for public buckets |
| Auth flow | AAD `client_credentials` → storage-scoped bearer token | AWS SigV4 (pyarrow's bundled SDK) |
| Table root | `Tables/` under the lakehouse | `S3_BUCKET` + optional `S3_PREFIX` |
| Table layout | `Tables/<schema>/<table>` | `<prefix>/<schema>/<table>`, `<prefix>/<table>`, or a single file |
| Discovery API | DFS REST (`?resource=filesystem`) — blob listing unsupported | S3 `ListObjectsV2` |
| Reader | `deltalake` → pyarrow Dataset | `pyarrow` S3 filesystem → pyarrow Dataset |
| URI shown by `describe_table` | `abfss://<ws>@.../<lh>/Tables/...` | `s3://<bucket>/<prefix>/...` |
| Auth settings | no proxy/endpoint — AAD only | `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ANONYMOUS`, `S3_USE_SSL` |

### Minimal config, side by side

**OneLake (Fabric)** — `SQLHANDLER_BACKEND=onelake` (default):

```bash
export SQLHANDLER_BACKEND=onelake
export FABRIC_TENANT_ID=<tenant-id>
export FABRIC_CLIENT_ID=<client-id>
export FABRIC_CLIENT_SECRET=<secret>            # keep in a Secret / .env
export FABRIC_LAKEHOUSE_ABFSS_URL=abfss://<ws>@onelake.dfs.fabric.microsoft.com/<lh>
# …or FABRIC_WORKSPACE_ID + FABRIC_LAKEHOUSE_ID instead of the full URL
sqlhandler --transport streamable-http --port 9097
```

**S3 / MinIO** — `SQLHANDLER_BACKEND=s3`:

```bash
export SQLHANDLER_BACKEND=s3
export S3_ENDPOINT_URL=http://127.0.0.1:9000   # MinIO; omit for AWS
export S3_REGION=us-east-1
export S3_ACCESS_KEY=<access-key>
export S3_SECRET_KEY=<secret-key>               # never in source control
export S3_BUCKET=lakehouse                      # required
export S3_PREFIX=datasets                       # optional sub-tree
sqlhandler --transport streamable-http --port 9097
```

**Which one?**

- Use **onelake** when the data already lives in Microsoft Fabric (as the Toromont lakehouse does) — zero copying, Delta's transactional metadata.
- Use **s3** when your data is Parquet in object storage (MinIO on-prem, AWS S3, GCS-interop) or you want a plain-file lake that any tool can dump into.
- Use **iceberg** when you have a real lakehouse catalog — schema/time-travel management, concurrent writers, cross-engine reads.
- Both onelake and s3 work through the same Helm chart (`backend:` value in `values.yaml`); see the deployment sections below.
## Why it's faster

| Approach | What happens on a 40M-row table |
|---|---|
| EzPresto/PrestoDB | Rows pulled over JDBC, re-serialized, full scans |
| **SQLhandler** | Delta scan reads only needed columns + filters from Parquet on OneLake |

## Architecture

```
LLM agent ──MCP──> sqlhandler (MCP server) ──DuckDB + pyarrow──> DataProvider
                       ▲  │                                    │
                       │  └─ web UI (/ui) + JSON API (/api/*)  │
                       └────────── read-only, same engine ─────┘
                                              │
                                              ├── OneLakeProvider  (ABFS, Delta Lake)
                                              ├── S3Provider       (MinIO/AWS, Parquet)
                                              ├── IcebergProvider  (catalog + Parquet)
                                              └── FileProvider     (NFS/local, Delta + Parquet)
```

Components:

- `src/sqlhandler/config.py`    — env-driven connection config + backend selection
- `src/sqlhandler/provider.py`  — `DataProvider` interface + `TableInfo`
- `src/sqlhandler/engine.py`    — shared SQL engine: DuckDB queries, scans, caches
- `src/sqlhandler/onelake.py`   — OneLake (ABFS/Delta) provider
- `src/sqlhandler/s3.py`        — S3/MinIO (Parquet) provider
- `src/sqlhandler/iceberg.py`    — Iceberg (catalog + Parquet) provider
- `src/sqlhandler/file.py`        — NFS/local filesystem (Delta + Parquet) provider
- `src/sqlhandler/webui.py`       — read-only web UI + JSON API (`/`, `/ui`, `/api/*`)
- `src/sqlhandler/server.py`    — the MCP server (`list_tables`, `describe_table`, `run_sql`, `scan_table`)

## OneLake access notes

- Table discovery uses the **DFS REST API** (`?resource=filesystem`) — OneLake does not implement the ADLS Gen2 *blob* list API that `pyarrow.AzureFileSystem` uses, so `fs.get_file_info`-style listing fails with `501 Not Implemented`.
- Tables live under schema folders: `Tables/<schema>/<table>` (e.g. `Tables/workorder/work_order_header`). `list_tables` returns a flat list keyed by `<schema>/<table>`; queries may reference the bare name or `<schema>_<name>`.
- The ABFS account is the workspace GUID; `deltalake` is configured with `account_name=onelake` + the service principal via `azure_tenant_id` / `azure_client_id` / `azure_client_secret`, plus the `onelake.dfs...` endpoints.

## S3 / MinIO (Parquet) backend

Set `SQLHANDLER_BACKEND=s3` (or `minio`) and point it at an S3-compatible store. Everything else — SQL engine, caches, MCP tools — is unchanged. To query **multiple buckets or paths** from one endpoint, see [Federated multi-source](#federated-multi-source-multiple-buckets--sources).

```bash
cd SQLhandler
SQLHANDLER_BACKEND=s3 \
  S3_ENDPOINT_URL=http://127.0.0.1:9000 \
  S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin \
  S3_BUCKET=lakehouse S3_PREFIX=datasets \
  sqlhandler --transport streamable-http --host 0.0.0.0 --port 9097
```

### How tables are discovered

Under `S3_BUCKET` (+ optional `S3_PREFIX`), every Parquet file or folder of Parquet files becomes a table:

| Layout | Table | SQL name |
|---|---|---|
| `<prefix>/orders.parquet` | `orders` | `orders` |
| `<prefix>/customers/*.parquet` | `customers` | `customers` |
| `<prefix>/sales/customers/*.parquet` | schema `sales`, table `customers` | `customers` or `sales_customers` |
| `<prefix>/hr/employees/year=2024/*.parquet` | schema `hr`, table `employees` | `employees` or `hr_employees` |

Hive partition folders (`key=value`) inside a table folder are folded into the table; hidden folders (starting with `.`) are ignored. Filtering and projection push down to the Parquet scan exactly as with the OneLake backend.

### Delta tables on S3 (`S3_FORMAT`)

Parquet buckets whose tables already carry a Delta `_delta_log` (transactions, time travel) are read directly — no copying:

- `S3_FORMAT=auto` (default) — folders containing a `_delta_log` are read as Delta tables (deltalake over the same S3 credentials); everything else stays plain Parquet. Delta data files are not re-discovered as separate tables.
- `S3_FORMAT=delta` — every discovered table is treated as Delta.
- `S3_FORMAT=parquet` — plain Parquet only (Delta logs ignored).

Delta-on-S3 tables support `version_as_of` time travel; the engine's snapshot-version check uses the `_delta_log` listing, so ETL commits are picked up without waiting out the TTL. In `SQLHANDLER_SOURCES`, each source accepts `"format": "auto" | "parquet" | "delta"`.

### MinIO specifics

- pyarrow (already a dependency) talks to S3 directly — **no extra pip dependency** (no boto3 / s3fs).
- `S3_ENDPOINT_URL` may omit the scheme; `http` is assumed unless `S3_USE_SSL=true`.
- Path-style addressing is the default (`S3_PATH_STYLE=true`), which is what MinIO uses.
- `S3_ANONYMOUS=true` serves a public bucket without keys.

### Quick local test

```bash
# Start MinIO (or any S3) once:
minio server /tmp/minio-data --address 127.0.0.1:9000

# Run the gated integration test against the live endpoint:
SQLHANDLER_TEST_MINIO=1 SQLHANDLER_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
  SQLHANDLER_TEST_S3_ACCESS_KEY=minioadmin SQLHANDLER_TEST_S3_SECRET_KEY=minioadmin \
  pytest tests/test_s3_integration.py -q
```

## Federated multi-source (multiple buckets / sources)

Need to query **several S3 buckets (or paths) at once** — or mix S3 with OneLake/NFS/Iceberg? Set `SQLHANDLER_SOURCES` (a JSON array) and SQLhandler federates every source behind **one endpoint**: same MCP tools, one shared cache, and `JOIN` across sources in a single `run_sql`. When set, it overrides `SQLHANDLER_BACKEND`.

### How it works

A `MultiProvider` wraps one `DataProvider` per source and presents them as a single logical engine:

- Every table is tagged with its **source** name.
- `describe_table`, `run_sql`, `scan_table` route to the owning source.
- The table list, describe results and open dataset handles are cached per-source in the shared in-process cache (async list refresh included).

### Configuration (environment)

```bash
export SQLHANDLER_SOURCES='[
  {"name":"sales",     "backend":"s3", "endpointUrl":"http://127.0.0.1:9000",
   "accessKey":"minioadmin","secretKey":"minioadmin","bucket":"sales-bucket"},
  {"name":"inventory", "backend":"s3", "endpointUrl":"http://127.0.0.1:9000",
   "accessKey":"minioadmin","secretKey":"minioadmin",
   "bucket":"inventory-bucket","prefix":"raw"}
]'
```

- Each entry accepts any backend (`s3`/`minio`, `onelake`, `nfs`, `iceberg`); the fields mirror that backend's env vars (`bucket`/`prefix`/`endpointUrl`/ `region`/`accessKey`/`secretKey`/`anonymous`/`useSsl`, `abfssUrl`, `rootDir`, `catalogUri`, …).
- `name` is the **source label** and must be unique; it is the top-level namespace for everything that source exposes.

### Configuration (Helm chart)

In `helm/values.yaml`, the `sources:` list (overrides the single `backend`):

```yaml
sources:
  - name: sales
    backend: s3
    endpointUrl: "http://minio.minio.svc.cluster.local:9000"
    bucket: "sales-bucket"
    prefix: ""                # optional sub-tree within the bucket
    # accessKey/secretKey can go here (non-production) or via a mounted
    # SQLHANDLER_SOURCES env var from a Secret (preferred)
  - name: inventory
    backend: s3
    endpointUrl: "http://minio.minio.svc.cluster.local:9000"
    bucket: "inventory-bucket"
    prefix: "raw"
```

### Naming: how tables are addressed

| Source + folder | SQL identifier (view) | Web UI label |
|---|---|---|
| source `sales`, folder `orders` | `sales_orders` | `sales/orders` |
| source `inventory`, folder `raw/customers` | `inventory_raw_customers` | `inventory/raw/customers` |
| source `inventory`, flat `invoices.parquet` | `inventory_invoices` | `inventory/invoices` |

- `list_tables` (MCP) lists **all** sources, source-qualified.
- `describe_table`/`run_sql` accept the qualified name (preferred) or the bare name **only when that name is unique across every source** — ambiguous bare names are deliberately not registered as DuckDB views, so they raise a clear "table not found" instead of silently returning the wrong source.

### Cross-source SQL

```sql
SELECT i.customer_name, i.tier, round(sum(s.amount), 2) AS total
FROM sales_orders s
JOIN inventory_customers i ON s.customer_id = i.cust_id
GROUP BY 1, 2
ORDER BY total DESC
```

### Caching & freshness

The federated engine shares the same caches as single-source mode, keyed per source: list refresh (async), describe and dataset handles — so the `SQLHANDLER_CACHE_TTL` / `SQLHANDLER_LIST_ASYNC_REFRESH` knobs behave exactly as documented above.

### Caveats & limitations

- **Source labels must be unique and DuckDB-identifier-safe** (letters, digits, underscores). This is validated at startup — a duplicate or unsafe label raises a `ValueError` instead of silently shadowing a source. Avoid names that collide with table names.
- **Credentials**: each source carries its own credentials. When set via `values.yaml` `sources:`, keys land in the ConfigMap as JSON — in production mount `SQLHANDLER_SOURCES` from a Secret instead.
- **Deep folder nesting** follows the S3 discovery rule: only the leaf folder (table) + its parent (schema) become the name; anything above that only lives in the storage path (`finance/b/c` → `b_c`, `finance` dropped).
- The per-source table list is cached like any other source; new files appear after the async refresh (see caching & freshness).

## External databases (read-only attach)

The engine is a *lake* engine — but some questions need the **system of record** behind the lake. Configure one or more external database servers — Postgres, MySQL/MariaDB, SQLite files, or SQL Server — and their tables become queryable (and join-able with lake tables in the SAME query) as `<db-alias>.<schema>.<table>`:

```bash
export SQLHANDLER_ATTACH='[
  {"name":"ops", "type":"postgres", "host":"pg.internal", "port":5432,
   "database":"opsdb", "user":"ro_user", "password_env":"OPS_PG_PASSWORD",
   "params": {"sslmode": "verify-full", "sslrootcert": "/etc/sqlhandler/certs/ca.crt"}},
  {"name":"crm", "type":"sqlserver", "host":"sql.crm.internal", "port":1433,
   "database":"crmdb", "user":"ro_user", "password_env":"CRM_SQL_PASSWORD"}
]'
# or: export SQLHANDLER_ATTACH_FILE=/etc/sqlhandler/attach.json
```

```sql
SELECT o.status, count(*) AS n
FROM ops.public.work_orders o            -- attached Postgres (read-only)
JOIN work_order_header w ON w.id = o.id  -- registered lake table
GROUP BY 1
```

- `list_tables` gains an "Attached databases" section with the qualified table names; `describe_table` / `profile_table` accept `<db-alias>.<schema>.<table>` directly (profile runs `SUMMARIZE` on the server, bounded by `SQLHANDLER_PROFILE_MAX_ROWS`).
- **Read-only is enforced by DuckDB itself** — every `ATTACH` carries `READ_ONLY`, so writes against the attached catalog fail at the engine level, not by convention.
- **Secrets hygiene**: the config carries env-var *names* (`password_env`); a literal `password` key is rejected at startup, and resolved passwords are scrubbed from every error message. (Trust-auth over a unix socket: `"password_env": ""`.)
- **The filesystem lockdown stays on**: attaching a database does not re-open DuckDB file reads — `read_parquet('/etc/passwd')` still fails on attached-DB connections. The scanner extensions (`postgres_scanner`, `mysql_scanner`, `sqlite_scanner`, plus the community `mssql` for SQL Server) are baked into the image at build time (two egress endpoints: `extensions.duckdb.org` + `community-extensions.duckdb.org`) and loaded explicitly; runtime extension downloads stay disabled.
- A database that is down does not fail the lake: `list_tables` reports it as unavailable, and queries touching only the lake never attach anything.
- The catalog alias must be DuckDB-identifier-safe and unique; malformed config fails loudly at startup by design.
- **`params` — driver options per entry (the TLS story)**: many production endpoints REQUIRE TLS, and a `params` object per entry carries driver options — `{key: value}` pairs merged over each type's built-in DSN keys (an existing key is overridden in place, new keys are appended; no duplicate key is ever emitted). Keys are lowercased `[a-z][a-z0-9_]*`; values are ≤256 chars from `[A-Za-z0-9_./:@=+ -]` — quotes, backslashes, semicolons and braces are refused, so no DSN/conn-string injection is possible. One per-type exception: the mysql/mariadb scanner parses its DSN as whitespace-split bare values (no quote handling), so for those two types the entry fields (`host`/`database`/`user`) and every `params` value must not contain whitespace — a resolved password containing whitespace is rejected at attach time; postgres (libpq-quoted) and sqlserver (semicolon-delimited) accept spaces. Keys that would carry secrets or duplicate the entry's own fields (`password`, `user`, `host`, `port`, `database`, `server`, …) are rejected loudly, and the literal-secret scan covers `params` too.
- **Connection lifecycle**: every query that references an attached catalog runs on a fresh connection — `LOAD` extension → `ATTACH … READ_ONLY` → filesystem lockdown, per query; there is no pooling, so busy production servers see per-query connection churn (postgres DSNs carry `connect_timeout=10`).
- **Managed IAM auth is not expressible**: `password_env` names a static environment variable, so rotating IAM database auth (GCP Cloud SQL IAM auth, AWS IAM DB auth) cannot be configured — put the Cloud SQL Auth Proxy sidecar (or equivalent) in front of the database and connect with `password_env: ""` (trust-auth) to the proxy where applicable.

### Supported types

| Config `type` | Served by (LOAD → ATTACH) | Default port | Notes |
|---|---|---|---|
| `postgres` | `postgres_scanner` → `TYPE postgres` | 5432 | Default user `postgres`. |
| `mysql` | `mysql_scanner` → `TYPE mysql` | 3306 | Default user `root`. TLS rides libmariadb keys via `params`: `ssl_mode` (default `preferred`), `ssl_ca`, `ssl_cert`, `ssl_key`. DSN values are emitted bare — no whitespace in host/database/user or params values. |
| `mariadb` | `mysql_scanner` → `TYPE mysql` | 3306 | Alias served by the mysql scanner (same wire protocol) — the ATTACH says `mysql` while the config keeps the operator's `mariadb` spelling. |
| `sqlite` | `sqlite_scanner` → `TYPE sqlite` | — | `database` is the sqlite FILE PATH; no host/port/user, `password_env` may be omitted, `params` are not accepted. The file is read at query time through the extension's own bundled sqlite3 library — outside DuckDB's filesystem lockdown — but the path is operator-configured in `SQLHANDLER_ATTACH` (never model-chosen), the same trust model as the nfs backend. READ_ONLY still enforced. |
| `sqlserver` | `mssql` (community) → `TYPE mssql` | 1433 | Native TDS 7.4 — needs no unixODBC and no Microsoft ODBC driver in the image. Requires an explicit `user`/`user_env` (no `sa` default). TLS is ON by default: `Encrypt=yes;TrustServerCertificate=yes` are lenient defaults `params` can override — in the mssql extension the two keys are synonyms, so a conflicting pair fails the ATTACH loudly; do not mix ODBC-style tightenings, see the [mssql extension docs](https://duckdb.org/community_extensions/extensions/mssql) for TLS options. |

`"postgresql"` is not a valid type — the config key is spelled `postgres`.

The postgres adapter also covers postgres wire-compatible servers — Amazon Redshift, AlloyDB, YugabyteDB, CockroachDB, TimescaleDB — with TLS where required via `params` (Redshift: `sslmode: require`, port 5439).

## Iceberg (catalog) backend

Set `SQLHANDLER_BACKEND=iceberg` to query **Apache Iceberg** tables through a catalog. Iceberg keeps its table metadata (snapshots, manifests with per-file stats) next to the Parquet data files, so a table is a first-class entity: real schemas, time travel, concurrent writers, cross-engine reads. This server reads the manifest-listed Parquet files with the same pyarrow engine.

```bash
export SQLHANDLER_BACKEND=iceberg
export ICEBERG_CATALOG_TYPE=rest          # or sql for a local SQL catalog
export ICEBERG_CATALOG_URI=http://rest-catalog:8181
# export ICEBERG_CATALOG_TOKEN=<token>
# export ICEBERG_NAMESPACE=analytics      # optional namespace filter
# S3 storage for the data files (same S3_* vars as the s3 backend):
export S3_ENDPOINT_URL=http://127.0.0.1:9000
export S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin
sqlhandler --transport streamable-http --port 9097
```

Two catalog types are supported:

- **`rest`** (default) — an Iceberg REST catalog (Dremio, Nessie, Amazon S3 Tables). `ICEBERG_CATALOG_URI` is the REST endpoint.
- **`sql`** — a SQL catalog (SQLite/Postgres) for local dev/tests: `ICEBERG_CATALOG_TYPE=sql` and `ICEBERG_CATALOG_URI=sqlite:///path/catalog.db`. Writers and readers must use the same `ICEBERG_CATALOG_NAME`.

Tables are addressed as `<namespace>/<name>` and read as Parquet; `run_sql` / `scan_table` push predicates/projections into the Parquet scan as with the other backends. Install the optional dependency first:

```bash
pip install 'sqlhandler[iceberg]'
```

### Quick local test

```bash
# create a local SQL catalog + a table with pyiceberg, then:
SQLHANDLER_TEST_ICEBERG=1 pytest tests/test_iceberg_integration.py -q
```

## NFS / local filesystem backend

Set `SQLHANDLER_BACKEND=nfs` (or `file`/`local`) to read tables from a directory mounted into the machine — NFS via a PV/PVC, a hostPath, or any volume. Backed by pyarrow's `LocalFileSystem`, it needs no credentials or endpoint, and it auto-detects both formats:

- **Delta Lake** tables (any folder containing a `_delta_log/`) — read with `deltalake`, with snapshot-version-aware cache invalidation (see caching).
- **Parquet** files/folders — same conventions as the S3 backend.

Parquet data files *inside* a Delta folder are folded into that Delta table, never exposed as separate tables; hidden (`.`) paths are skipped.

```bash
export SQLHANDLER_BACKEND=nfs
export NFS_ROOT=/data                        # mounted directory with tables
sqlhandler --transport streamable-http --port 9097
```

### How tables are discovered

| Layout | Format | Table | SQL name |
|---|---|---|---|
| `<root>/orders.parquet` | parquet | `orders` | `orders` |
| `<root>/sales/customers.parquet` | parquet | schema `sales`, `customers` | `customers` / `sales_customers` |
| `<root>/work_orders/` (+ `_delta_log/`) | **delta** | `work_orders` | `work_orders` |
| `<root>/sales/work_orders/` (+ `_delta_log/`) | **delta** | schema `sales`, `work_orders` | `work_orders` / `sales_work_orders` |

### Kubernetes / PCAI (NFS)

Mount a PVC (or hostPath) with the tables and point the chart at it. Import the `sqlhandler` chart in PCAI and set these values:

```yaml
# values.yaml — the keys PCAI renders from
backend: nfs
ezua:
  domainName: <your-domain>
nfs:
  rootDir: /data
  mount:
    enabled: true
    pvcName: my-data-pvc
```

The chart sets `NFS_ROOT`, mounts the PVC read-only at `/data`, and the readiness probe verifies the mount is present.

### Caching & freshness (NFS Delta)

Like every backend, the NFS backend reuses open Dataset handles for `SQLHANDLER_DATASET_CACHE_TTL` and pre-warms schemas via `cache.prewarmTables`. On top of that, Delta Lake tables on NFS get **snapshot-version-aware invalidation**: the engine re-checks the table's `_delta_log` version and reopens the dataset when an ETL commit bumps it, so new rows appear without a restart. The re-check is
throttled by `cache.versionCheckInterval` (values.yaml) / `SQLHANDLER_VERSION_CHECK_INTERVAL` (default `10` seconds; `0` = re-check on every query). ETL jobs writing to the mount therefore show up within a few seconds on a cached replica.
## Setup

```bash
cd SQLhandler
cp config/.env.example config/.env   # fill in FABRIC_* (do NOT commit secret)
uv sync                               # or: pip install -e .
sqlhandler --transport stdio          # run as MCP server (stdio)
```

To run as a streamable HTTP server (**MCP 2.0 SDK**, `mcp>=2.0.0`, stateless streamable-http) on a port:

```bash
sqlhandler --transport streamable-http --host 0.0.0.0 --port 9097
# MCP endpoint:  http://host:9097/mcp
# health/ready:  http://host:9097/health  http://host:9097/ready
```

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `FABRIC_TENANT_ID` | Entra tenant id |
| `FABRIC_CLIENT_ID` | Service principal client id |
| `FABRIC_CLIENT_SECRET` | Service principal secret (**keep in .env/secret, never commit**) |
| `FABRIC_LAKEHOUSE_ABFSS_URL` | Full ABFS URL to the lakehouse (preferred) |
| `FABRIC_WORKSPACE_ID` | Workspace GUID (fallback) |
| `FABRIC_LAKEHOUSE_ID` | Lakehouse GUID (fallback) |
| `FABRIC_AUTHORITY` | OneLake authority (default `onelake.dfs.fabric.microsoft.com`) |
| `SQLHANDLER_BACKEND` | Data source: `onelake` (default), `s3`/`minio`, `iceberg`, or `nfs`/`file`/`local` |
| `SQLHANDLER_SOURCES` | JSON array to federate **multiple** sources behind one engine (overrides `SQLHANDLER_BACKEND`); see [Federated multi-source](#federated-multi-source-multiple-buckets--sources) |
| `S3_ENDPOINT_URL` | S3 endpoint, e.g. `http://127.0.0.1:9000` for MinIO |
| `S3_REGION` | S3 region (default `us-east-1`) |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | S3 credentials |
| `S3_SESSION_TOKEN` | Optional STS session token |
| `S3_BUCKET` | S3 bucket that holds the tables |
| `S3_PREFIX` | Optional sub-tree within the bucket (default empty) |
| `S3_ANONYMOUS` | `true` to read a public bucket without keys |
| `S3_USE_SSL` | Force `https` when `S3_ENDPOINT_URL` has no scheme |
| `S3_PATH_STYLE` | Path-style addressing (default `true`; what MinIO uses) |
| `ICEBERG_CATALOG_TYPE` | `rest` (default) or `sql` |
| `ICEBERG_CATALOG_URI` | REST endpoint or `sqlite:///path` (sql) |
| `ICEBERG_CATALOG_TOKEN` | Optional REST bearer token |
| `ICEBERG_CATALOG_NAME` | Catalog name (SQL catalogs partition by it; default `sqlhandler`) |
| `ICEBERG_WAREHOUSE` | Optional table location root |
| `ICEBERG_NAMESPACE` | Optional namespace filter for `list_tables` |
| `NFS_ROOT` | Mounted directory with tables (nfs backend; required) |
| `SQLHANDLER_TRANSPORT` | Default MCP transport for the CLI (`stdio` default; `streamable-http`) |
| `SQLHANDLER_ENV_FILE` | Path to a `.env` file (default: auto-discovered `config/.env`) |
| `SQLHANDLER_ALLOWED_ORIGINS` | Comma-separated extra browser origins allowed on `/api/*`, `/ui` and `/mcp` (CORS + `/mcp` Origin validation). **Default: none — same-origin only** (decision D3; the old `*` default let any web page read the API). The legacy `SQLHANDLER_CORS_ORIGINS` is still honored when explicitly set. |
| `SQLHANDLER_ALLOWED_HOSTS` | Optional strict `Host`-header allowlist for `/mcp` (DNS-rebinding defense; mismatch → 421). Comma-separated; `host:*` allows any port. Unset = no Host pinning (a pod can't know its externally-visible hostnames); `/mcp` Origin validation is on regardless. Chart: `mcp.allowedHosts`. |
| `SQLHANDLER_MCP_READONLY` | **`1` (default): MCP `run_sql` is SELECT-only** — the same DuckDB-parser guard as the web API rejects multi-statement DDL, INSERT/UPDATE/DELETE, ATTACH, COPY, PRAGMA/SET (decision D2). `0` restores multi-statement/DDL execution for trusted callers. Attached external catalogs stay read-only in BOTH modes (engine-enforced). Env re-read per call — flips need no restart. Chart: `mcp.readonly`. |
| `SQLHANDLER_METRICS_AUTH` | `1` gates `/metrics` behind the API token / MCP API keys (`X-API-Key`, `X-API-Token` or `Bearer`). Default off = unauthenticated. Gate-on with no credential configured → 401 for everyone (fail closed). `/ready` is NEVER gated — kubelet readiness probes cannot carry a secret; gating it strands every pod NotReady (live-seen). Chart: `security.metricsAuth`. |
| `SQLHANDLER_DUCKDB_FILE_ACCESS` | `1` re-enables DuckDB file/table functions (`read_csv`, `COPY ... TO`, URL reads) that are locked down by default — see [Security notes](#security-notes) |
| `SQLHANDLER_CACHE_TTL` | Seconds to keep `list_tables`/`describe_table` results in memory (default `3600`, `0` disables) |
| `SQLHANDLER_LIST_ASYNC_REFRESH` | Serve `list_tables` from cache immediately and refresh in the background (plus automatically every `SQLHANDLER_CACHE_TTL`); default `true`, `false` = synchronous every call |
| `SQLHANDLER_DATASET_CACHE_TTL` | Seconds to reuse an open Dataset handle (default `3600`, `0` disables) |
| `SQLHANDLER_DATASET_CACHE_TABLES` | Max tables held in the dataset LRU (default `8`) |
| `SQLHANDLER_VERSION_CHECK_INTERVAL` | How often a Delta table's snapshot version is re-checked (default `10`s; `0` = every query) |
| `SQLHANDLER_MAX_ROWS` | Server-side row cap for `run_sql` results (default `1000`; `0` = unlimited). Also clamps `scan_table` when its `limit` is missing/negative — decision D4: `scan_table(limit=-1)` no longer materializes whole tables; pass an explicit positive limit for more rows |
| `SQLHANDLER_MAX_OUTPUT_ROWS` | Max rows rendered as markdown to a client (default `1000`) |
| `SQLHANDLER_READINESS_CHECK` | `0` to disable the backend-aware `/ready` connectivity probe |
| `SQLHANDLER_PREWARM_TABLES` | Comma-separated tables whose schemas are warmed at first request, e.g. `work_order_header,work_order_note_recent` (when unset, the busiest tables from the previous run are warmed automatically) |
| `SQLHANDLER_CATALOG` | Path to a semantic-catalog JSON/YAML file (table/column descriptions merged into list/describe; hot-reloaded) |
| `SQLHANDLER_CATALOG_STORE` | Writable path for UI/API catalog uploads (default: `<cache-dir>/semantic-catalog.json`); overrides `SQLHANDLER_CATALOG` while present |
| `SQLHANDLER_CATALOG_UPLOAD` | `0` disables the catalog mutation endpoints (upload/clear **and** the browser editor's apply/remove — reading/editing text still works, applying is refused) (default on) |
| `SQLHANDLER_VIRTUAL_CACHE_TTL` | Seconds a virtual table's materialized result is reused (keyed by definition + base-snapshot versions, so new ETL commits invalidate immediately). `0` disables (default 3600) |
| `SQLHANDLER_VIRTUAL_CACHE_DIR` | Where virtual-table results are materialized (default: the disk-warm cache dir); point at an RWX PVC shared by the replicas to materialize once per deployment |
| `SQLHANDLER_VIRTUAL_CACHE_MAX_BYTES` | Skip caching virtual results larger than this — they are served live instead (default 2GiB; `0` = unlimited) |
| `SQLHANDLER_RESULT_CACHE_TTL` | Seconds an identical query's result is served from memory (keyed by sql/params/limits + base-snapshot versions; repeated agent queries become ~0 ms). The key's SQL is whitespace-normalized outside quoted literals, so formatting-only re-runs hit; case is NOT folded and comment-bearing/dollar-quoted SQL keeps byte-exact keys. `0` disables (default 3600) |
| `SQLHANDLER_RESULT_CACHE_MAX_BYTES` | In-memory cap for cached query results, LRU-evicted (default 256MiB) |
| `SQLHANDLER_VIRTUAL_CACHE_SORT` | `0` disables clustering (auto-sorting) of materialized virtual results by their lowest-cardinality columns (default on) |
| `SQLHANDLER_BLOCK_CACHE` | `1` enables the disk block cache for object-store reads (parquet footers/column chunks cached pod-local; opt-in — cold big sequential scans pay a small Python-layer cost, repeated/filtered reads win big) (default off). Backends: S3/Iceberg filesystems and the OneLake/Delta **data files** (delta-log IO stays inside delta-rs; cached bytes are scoped by Delta snapshot version, so a new ETL commit or a time-travel read never serves another snapshot's blocks) |
| `SQLHANDLER_BLOCK_CACHE_DIR` / `_BLOCK_SIZE` / `_MAX_BYTES` / `_INCLUDE_LOCAL` | Cache location (default `<tmp>/sqlhandler-block-cache`), block size (default 8MiB), total-size cap (reset on overflow, default 4GiB), and `1` to also cache `LocalFileSystem` paths (NFS mounts — real-local disk is already covered by the page cache) |
| `SQLHANDLER_S3_OPTIONS` | JSON object merged into pyarrow's `S3FileSystem` kwargs — timeouts, retry limits, connection tuning (S3 + Iceberg backends) |
| `SQLHANDLER_ONELAKE_STORAGE_OPTIONS` | JSON object merged into delta-rs storage options (OneLake backend) |
| `SQLHANDLER_QUERY_MEMORY_SIZE` | Recent query outcomes kept for the query-memory resource (default 50; 0 disables) |
| `SQLHANDLER_PROFILE_MAX_ROWS` | Row sample cap for `profile_table` (default 1000000; 0 = full table) |
| `SQLHANDLER_QUERY_TIMEOUT` | Per-query wall-clock timeout in seconds (**default 600** — decision D5; a runaway query used to hold a concurrency slot forever; `0` = no timeout) |
| `SQLHANDLER_MAX_CONCURRENT_QUERIES` | Max simultaneous DuckDB queries (default 8; 0 = unlimited); excess queue |
| `SQLHANDLER_QUEUE_TIMEOUT` | Seconds a query may wait for a concurrency slot (default 30) |
| `SQLHANDLER_ASYNC_JOB_TTL` | Seconds a finished async-query job stays fetchable (default 900; shared by the web `/api/query/async` registry and the MCP jobs registry) |
| `SQLHANDLER_MAX_JOBS` | Max jobs tracked by the MCP/`/api/jobs` async-query registry (default **8**; garbage/non-positive values fall back to the default — the registry is bounded by design). Beyond the cap a submit is refused with a clear message until jobs are cancelled/fetched; the registry is in-memory and a restart clears it |
| `SQLHANDLER_SAVED_QUERIES_PATH` | JSON file for the saved-parameterized-queries store (default: next to the disk-warm cache / platform temp dir, like the catalog store). Each entry maps a name to a SQL template + default bind params; writes are atomic (temp+rename, chmod 600) and the store degrades to empty on a corrupt file rather than erroring |
| `SQLHANDLER_EXPORT_MAX_ROWS` | Row cap for CSV/Parquet exports (default 100000; 0 = hard 1M ceiling) |
| `SQLHANDLER_AUDIT_LOG` | Path to a JSONL audit file — one line per query outcome (unset = off) |
| `SQLHANDLER_API_TOKEN` | Require this bearer token on `/api/*` (unset = no token check) |
| `SQLHANDLER_API_KEYS` (or fleet-universal `MCP_API_KEYS`) | OPTIONAL `/mcp` API-key gate: comma-separated key list; when EITHER var is set, every `/mcp` request needs `X-API-Key` or `Authorization: Bearer` (constant-time compared). Unset → `/mcp` runs open exactly as before (loud startup warning; gateway remains the outer layer). Rotation: append the new key, move clients, drop the old — env re-read per request, no restart. Chart wiring: `security.apiKey.existingSecret` (empty default = not wired). | unset → `/mcp` open |
| `S3_FORMAT` | `auto` (detect Delta by `_delta_log`), `parquet`, or `delta` |

## Performance & caching

`list_tables` (DFS REST / S3 listing / catalog) and `describe_table` (Delta `_delta_log` or Parquet footers — backend-dependent) are the calls agents/OWUI repeat on every session, and both hit the lakehouse every time. The server now keeps both in an in-process cache:

- A single process-wide handler is reused (previously a new handler was built per tool call, silently discarding the table-list cache).
- `list_tables` is served from the cache immediately. When the cached list is stale it is refreshed on a background thread (default `SQLHANDLER_LIST_ASYNC_REFRESH=true`) and a daemon timer re-lists every `SQLHANDLER_CACHE_TTL` seconds, so a newly uploaded file appears without restarting and callers never block on the S3/DFS listing.
- `describe_table` results are cached per table for `SQLHANDLER_CACHE_TTL` seconds, so the frequently-described tables return from memory (~ms) instead of re-reading the Delta log (~8s).
- `SQLHANDLER_PREWARM_TABLES` warms those schemas in a background thread at first request, so even the first describe is a hit.
- Open Delta datasets are reused per table for `SQLHANDLER_DATASET_CACHE_TTL` (default `3600`) seconds, LRU-bounded by `SQLHANDLER_DATASET_CACHE_TABLES` (default `8`). This removes the per-query Delta `_delta_log` re-open (the serialized part of every call) so concurrent queries stop piling up on it — the rows themselves are still fetched from the source on every scan, so query results are never
  cached.
- **Delta snapshot-version-aware invalidation**: for Delta Lake tables (nfs and onelake) the engine stores the table's snapshot version with the cached handle and re-checks it on reuse (`SQLHANDLER_VERSION_CHECK_INTERVAL`, default `10`s). When an ETL commit bumps the version, the cached dataset is reopened so new rows appear without waiting out the TTL and without a restart.

The caches are **in-process**: a new replica starts fresh (and pre-warms). They only cache metadata — query results are never cached, so you always see the latest rows. Set the TTL envs to `0` to disable.

## PCAI / MCP 2.0 deployment

SQLhandler ships as a **PCAI (HPE Ezmeral Unified Analytics) MCP 2.0** server — it is built on the **`mcp>=2.0.0` SDK's low-level `Server`** and serves the **standard MCP protocol** (initialize handshake) over stateless streamable-http at `/mcp`, so any standard MCP client can connect (DSH, official Python/TS SDKs, MCP Inspector, OWUI). The `helm/` chart wires it into the PCAI platform (Istio
gateway, oauth2-proxy auth, vendor-service discovery labels) exactly like the MultimodalRAG and AgentBuilder charts.

> **PCAI is a Kubernetes wrapper — you never run `helm` or `kubectl`.** You
> import the packaged chart (`.tar.gz`) into PCAI once, then drive the whole
> deployment by setting the chart's **`values.yaml`** in the PCAI *Helm Values*
> editor (or via the PCAI API). Every `--set` below maps 1:1 to a key in
> `values.yaml`.

### Endpoint

| Access method | URL |
|---|---|
| Via PCAI gateway (production) | `https://sqlhandler.<your-domain>/mcp` |
| Web UI (data explorer) | `https://sqlhandler.<your-domain>/ui` |
| `kubectl port-forward` (local, optional) | `http://localhost:9097/mcp` · `http://localhost:9097/ui` |

### Deploy (OneLake / Fabric backend)

Import the packaged `sqlhandler` chart in PCAI, then set these values in the *Helm Values* editor:

```yaml
# values.yaml — the keys PCAI renders from
backend: onelake

ezua:
  domainName: <your-domain>
  virtualService:
    endpoint: "sqlhandler.<your-domain>"
    istioGateway: "istio-system/ezaf-gateway"

fabric:
  # Preferred: create the Secret out-of-band and keep create: false (see
  # helm/local/README.md). The chart creates it from these values only for
  # a non-production bootstrap:
  credentialsSecret:
    create: true
    values:
      tenantId: "<tenant-id>"
      clientId: "<client-id>"
      clientSecret: "<client-secret>"   # keep out of git / never in values history
  lakehouseAbfssUrl: "abfss://<workspace-guid>@onelake.dfs.fabric.microsoft.com/<lakehouse-guid>"
  workspaceId: "<workspace-guid>"
  lakehouseId: "<lakehouse-guid>"

cache:
  ttl: 3600
  prewarmTables: "work_order_header,work_order_note_recent,work_order_labour,work_order_parts"
```

> **In-cluster callers work out of the box.** The ingress NetworkPolicy is
> **disabled by default**: any pod in the cluster can call the service
> directly via `http://sqlhandler.<ns>.svc.cluster.local:9097/mcp`, and the
> ClusterIP Service exposes nothing outside the cluster — the right posture
> for short-lived test deployments.
>
> For production / real-data deployments, enable the policy and allowlist
> any direct callers from other namespaces (no client-side change needed):
>
> ```yaml
> security:
> networkPolicy:
> enabled: true
> allowedNamespaces: ["<client-namespace>"]
> ```
>
> Flipping `enabled` back to `true` is exactly what re-breaks in-cluster
> callers not on that list. See [Security notes](#security-notes) for the
> full model.

> **Toromont:** a ready-made values file with the real service principal +
> OneLake coordinates is provided (`helm/deploy-toromont-values.yaml`, local
> only — it is gitignored). Paste its values into the PCAI *Helm Values*
> editor — **do NOT commit the file itself**. Note it must set
> `backend: onelake` explicitly (the chart default is `s3`, and the fabric
> credential wiring is gated on the backend). With it, the chart creates the
> `fabric-credentials` Secret (`fabric.credentialsSecret.create=true`) and
> wires the env vars (`FABRIC_TENANT_ID` / `FABRIC_CLIENT_ID` /
> `FABRIC_CLIENT_SECRET`) into the Deployment.

### Deploy the S3 / MinIO backend

```yaml
backend: s3
ezua:
  domainName: "<your-domain>"
  virtualService:
    endpoint: "sqlhandler.<your-domain>"
s3:
  endpointUrl: "http://minio:9000"
  bucket: "lakehouse"
  prefix: "datasets"
  # Preferred: create the Secret out-of-band and keep create: false:
  #   kubectl -n <ns> create secret generic s3-credentials \
  #     --from-literal=access-key=... --from-literal=secret-key=...
  # (retrieve: kubectl -n <ns> get secret s3-credentials -o jsonpath='{.data.secret-key}' | base64 -d)
  # create: true renders the Secret FROM these values (non-production only):
  credentialsSecret:
    create: false
    values:
      accessKey: "<s3-access-key>"
      secretKey: "<s3-secret-key>"
```

The chart wires `SQLHANDLER_BACKEND=s3` and the S3 env vars, and injects `S3_ACCESS_KEY` / `S3_SECRET_KEY` from the `s3-credentials` Secret. For a public bucket set `s3.anonymous: true` (no credential Secret needed).

#### Deploy multi-source (federated)

To serve several S3 buckets (or mixed backends) from one deployment, use the `sources:` list instead of `backend:`:

```yaml
# (omit `backend:` and the single `s3:` block)
sources:
  - name: sales
    backend: s3
    endpointUrl: "http://minio:9000"
    bucket: "sales-bucket"
    accessKey: "<s3-access-key>"
    secretKey: "<s3-secret-key>"
  - name: inventory
    backend: s3
    endpointUrl: "http://minio:9000"
    bucket: "inventory-bucket"
    prefix: "raw"
```

The chart renders `SQLHANDLER_SOURCES` (JSON) into the ConfigMap. For production, mount that env var from a Secret instead of embedding keys in `values.yaml`. Everything else — service, probes, Istio, cache knobs — is unchanged.

#### Enable the semantic catalog (semantic view)

Any backend. Paste a `semanticCatalog` block into the same *Helm Values* editor values — as **native YAML, no stringification** — that is the whole deployment procedure (no `kubectl`, no out-of-band objects):

```yaml
semanticCatalog:
  enabled: true
  tables:
    workorder/work_order:
      description: Maintenance work order headers, one row per order
      aliases: [work orders]
      columns:
        amount: Order total in USD
        kind: Order class: a=planned, b=unplanned
```

The chart renders the structure to canonical JSON, stores it in a ConfigMap mounted read-only at `/etc/sqlhandler/semantic-catalog.json`, and sets `SQLHANDLER_CATALOG` to it. Because the engine hot-reloads the file on mtime change, editing the catalog and re-deploying updates `list`/`describe` output **without a pod restart** — in-flight tool calls are not disrupted. Keys match the logical
`schema/name` (source-qualified or bare names also work); see [Semantic catalog — business meaning for tables/columns](#semantic-catalog--business-meaning-for-tablescolumns) for the full schema. Two alternatives: `semanticCatalog.json` takes the same document as an inline JSON **string** (machine-generated catalogs; mutually exclusive with `tables` — setting both non-empty fails the render), and
`semanticCatalog.existingConfigMap` mounts a ConfigMap you own that carries a `semantic-catalog.json` key.

**Even simpler — no values at all:** once deployed, anyone with UI access can enable it from the **Semantic catalog** panel of the data explorer (`/ui`): upload a `.json` or `.yaml` catalog file (or paste it) and it takes effect immediately, no restart. By default that upload is per-replica and pod-local; add one values line — `semanticCatalog.store.enabled: true` (a 1Gi RWX PVC shared by all
replicas) — and uploads become durable and cross-pod while staying hot-reloaded. A catalog that must be durable/auditable *without* trusting someone's downloads folder still belongs in values. The upload API can be turned off with `SQLHANDLER_CATALOG_UPLOAD=0`.

### Deploy the Iceberg backend

```yaml
backend: iceberg
ezua:
  domainName: "<your-domain>"
  virtualService:
    endpoint: "sqlhandler.<your-domain>"
iceberg:
  catalogType: rest
  catalogUri: "http://rest-catalog:8181"
  storage:
    endpointUrl: "http://minio:9000"
  credentialsSecret:
    create: false       # create out-of-band (see helm/local/README.md)
    values:
      token: "<rest-token>"
      accessKey: "<s3-access-key>"
      secretKey: "<s3-secret-key>"
```

The chart wires `SQLHANDLER_BACKEND=iceberg` and the `ICEBERG_*` env vars, injecting the REST token and S3 storage keys from the `iceberg-credentials` Secret. `ICEBERG_CATALOG_TYPE=sql` (a local SQL catalog) needs no Secret at all when the warehouse is local. The OneLake/Fabric Secret wiring is only rendered when `backend: onelake`, so an S3-only deployment needs no Fabric credential at all. The
chart renders, when `ezua.enabled=true` (default):

- `Deployment` + `Service` (port `9097`, MCP at `/mcp`)
- Istio `VirtualService` routing `/mcp` to the service with a **long timeout** (3600s default) for agent loops / streaming, and a short timeout elsewhere
- Istio `AuthorizationPolicy` (oauth2-proxy) so PCAI clients authenticate
- a Kyverno `ClusterPolicy` that tags the workload `hpe-ezua/type: vendor-service` + `hpe-ezua/app: sqlhandler` for PCAI discovery/monitoring
- liveness probe against `/health`, and a **backend-aware readiness probe** against `/ready` — it performs a cheap connectivity check on the selected backend (OneLake DFS token+list, S3 list, Iceberg catalog, NFS root) and returns 503 when the data source is unreachable, so broken-credential pods are drained from the Service instead of serving errors

Set `ezua.enabled=false` to disable the PCAI integration (VirtualService, AuthorizationPolicy, Kyverno) and deploy as a plain MCP server.

### Connect an MCP client

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

## Web UI (read-only data explorer)

SQLhandler bundles a small, dependency-free web UI (a single self-contained `index.html` — no build step, no CDN) that gives humans the same data the MCP agents query, through the same engine and caches:

- **Table list** (searchable, shows format badges; in federated multi-source mode each entry is labeled `source/schema/name`, e.g. `sales/orders`)
- **Schema view** per table — columns + types + table URI (+ catalog docs)
- **Column stats** — lazy per-table profiling panel (min/max, distinct≈, null %, quartiles) via "Profile table"; scans a capped sample
- **SQL editor** — run read-only queries (Ctrl+Enter), with a row limit
- **Results table** — rendered with row/column counts, timing, and copy-CSV
- **Charts** — one-click inline SVG bar chart of the first numeric column
- **Export** — download the current query (or a preview) as **CSV or Parquet** (`POST /api/export`; capped by `SQLHANDLER_EXPORT_MAX_ROWS`, default 100k)
- **Saved queries + history** — stored per browser (localStorage): save a query with 💾, click to reload, one-click delete; history records the last 30 successful runs
- **Light/dark theme** — header toggle, persisted per browser, defaults to the OS preference
- **HPE branding** — Hewlett Packard Enterprise wordmark with the HPE green brand element in the header; HPE green accent throughout, in both themes

It is served by the same server process, so it needs **no extra deployment**:

| URL | What |
|---|---|
| `http://host:9097/` or `/ui` | the web UI |
| `GET  /api/status` | server version + backend |
| `GET  /api/tables` | table list |
| `POST /api/describe` | columns/types for a table (+ catalog docs) |
| `POST /api/query` | run read-only SQL, returns columns+rows JSON |
| `POST /api/query/async` | start an async query, returns a `query_id` |
| `GET  /api/query/{id}` / `/{id}/rows` / `DELETE /api/query/{id}` | async status / paginated rows / cancel |
| `POST /api/jobs` | start an async query **job** (the MCP `query_submit` twin: `SQLHANDLER_MAX_JOBS` cap, submit-time read-only guard, watchdog-enforced timeout) |
| `GET  /api/jobs/{id}` / `/{id}/result` / `DELETE /api/jobs/{id}` | job status / result handed over **once** then freed (second fetch → 409) / cancel |
| `GET    /api/saved-queries` | saved parameterized queries |
| `POST   /api/saved-queries` | save one — `{name, sql, params, description}` (**auth-gated write**: refused 401 when a credential env is configured but the request presents none) |
| `DELETE /api/saved-queries/{name}` | delete one (auth-gated write; unknown name → 404) |
| `POST   /api/saved-queries/{name}/run` | run one — call-time `params` override stored ones as **bind params** (never string-interpolated) |
| `POST /api/preview` | first N rows of a table |
| `POST /api/profile` | column statistics (min/max, null %, distinct, quartiles) |
| `POST /api/export` | download a query or table as CSV / Parquet |
| `GET  /metrics` | Prometheus metrics |

The UI is deliberately **read-only**: every statement is parsed with DuckDB's own grammar and only plain `SELECT` queries (plus `EXPLAIN SELECT`) are accepted — writes, `PRAGMA`/`SET`, and `COPY` are rejected, so it cannot modify the data source or mutate engine settings. It reuses the process-wide `SqlEngine` — the same list/describe caches and DuckDB query path the MCP tools use — and clamps
results to the same row cap (`SQLHANDLER_MAX_ROWS`, default 1000; when that is `0`/unlimited the UI still caps at 1000 to keep the JSON payload bounded).

In a PCAI deployment the UI is behind the same oauth2-proxy as `/mcp`, so it is already authenticated. The Helm chart routes `/api` with the same long timeout as `/mcp` (long-running queries), while the page itself uses the short timeout.

## Security notes

- **Read-only by default everywhere SQL runs.** The web API has always enforced SELECT-only; MCP `run_sql` now runs the **same DuckDB-parser guard by default** (decision D2): multi-statement scripts, DDL/DML, `ATTACH`, `COPY` and `PRAGMA`/`SET` are refused with an error naming `SQLHANDLER_MCP_READONLY`. Set `SQLHANDLER_MCP_READONLY=0` to restore the old arbitrary-SQL capability for trusted callers — lake data only: **attached external catalogs are SELECT-only unconditionally** (enforced on the query connection itself, before any scanner extension loads), so `INSERT INTO <sink>.t SELECT * FROM <attached>.…` cannot exfiltrate in either mode. The env is re-read per call, so a flip needs no restart. Chart: `mcp.readonly`.
- **`/mcp` transport guard is on** (DNS-rebinding defense): requests that present a browser `Origin` header are refused unless the origin is listed in `SQLHANDLER_ALLOWED_ORIGINS` (MCP clients send no Origin and are unaffected). A strict `Host`-header pin is available via `SQLHANDLER_ALLOWED_HOSTS` (mismatch → 421) — opt-in because a pod cannot know its externally-visible hostnames.
- **CORS is same-origin only by default** (decision D3): responses carry no `Access-Control-Allow-Origin` until `SQLHANDLER_ALLOWED_ORIGINS` lists origins. The legacy `SQLHANDLER_CORS_ORIGINS` env is still honored when set, but its old `*` default is gone.
- **DuckDB file access is locked down by default** on every query connection (MCP and web): `read_csv`/`read_parquet`/`COPY ... TO` of local files and extension-based URL fetches fail closed, while the registered pyarrow datasets scan normally — all object-store IO (S3/ABFS/NFS) is done by pyarrow outside DuckDB, so nothing legitimate is lost. Set `SQLHANDLER_DUCKDB_FILE_ACCESS=1` if a query
  genuinely needs DuckDB's own file/table functions.
- **Saved-query writes are credential-gated** (the semantic-catalog poisoning lesson, applied to the saved-query store): a saved query is a template other agents run, so `query_save`/`query_delete` — and their REST twins — require a valid credential whenever one is configured (`SQLHANDLER_API_TOKEN`, or `MCP_API_KEYS`/`SQLHANDLER_API_KEYS`; checked per request on REST and per call on the MCP path). With no credential configured the deployment is in the known single-user-local mode: writes work and a loud startup line says so. Saved SQL is validated at save time (parsed; SELECT-only while the read-only mode is on) and the guard is re-applied at run time, so a hand-edited store file cannot smuggle DDL past the read-only mode. Parameters are always **bind** parameters — values never enter the SQL text.
- **Table names are traversal-guarded**: `schema/name` values containing `..` or absolute paths are rejected, and the NFS backend verifies every table path stays inside `NFS_ROOT` (symlinks included).
- **Network posture** (helm chart, `security.hardened: true` default): non-root read-only container, no ServiceAccount token, and an optional pod-level AuthorizationPolicy. The ingress NetworkPolicy is **off by default** so in-cluster callers work with zero friction; the ClusterIP Service exposes nothing outside the cluster. Enable it for production / real-data deployments
  (`security.networkPolicy.enabled: true`) and allowlist direct callers via `allowedNamespaces`. External access is authenticated at the gateway by the oauth2-proxy AuthorizationPolicy — keep `ezua.authorizationPolicy.enabled: true`.
- **Credentials**: create Kubernetes Secrets out-of-band (never in values files); see `helm/local/README.md` for create/read/rotate commands. `SQLHANDLER_SOURCES` with embedded keys is for development only.
- **In-cluster callers** need nothing while the NetworkPolicy is off (default). Once it is enabled, direct calls from another namespace require that namespace in `security.networkPolicy.allowedNamespaces` — no client-side change.

## Observability & ops (0.9.0)

### Prometheus metrics

`GET /metrics` renders the Prometheus text exposition (no extra dependency): query counters by outcome (`ok`/`error`/`cancelled`), a query-duration histogram, rows returned, cache hit/miss counters per cache type, and gauges for the table count, process RSS and the container memory limit. Set `SQLHANDLER_METRICS_AUTH=1` to require a token on `/metrics` + `/ready` (see the env table; default off = open, today's behavior).

### Audit log

Set `SQLHANDLER_AUDIT_LOG=/path/audit.jsonl` and every query outcome is appended as one JSON line (`ts`, `sql`, `state`, `duration_ms`, `n_rows`, `error`) — a compliance-grade record of every SQL executed against the lake, ready for SIEM tailing. Writes are best-effort and never break a query.

### API token (non-gateway deployments)

When `SQLHANDLER_API_TOKEN` is set, every `/api/*` request must present it (`Authorization: Bearer <token>` or `X-API-Token: <token>`, constant-time compared) — for deployments where the JSON API is not already behind the PCAI oauth2-proxy gateway. `/mcp`, `/ui` and `/health` are unaffected (and `/metrics` + `/ready` too, unless `SQLHANDLER_METRICS_AUTH=1`); note the in-browser UI does not send the token, so leave it unset when the UI must work without
gateway auth.

## MCP tools

- `list_tables`       — enumerate tables in the configured source (any backend); annotated with catalog descriptions when configured
- `search_tables`     — keyword search over table/column names + catalog docs, ranked best-first (exact/substring terms, then fuzzy near-miss names for typos); each hit carries `matched_on` reasons
- `describe_table`    — columns/types/URI for a table (+ catalog docs)
- `profile_table`     — column statistics: min/max, distinct≈, null %, avg/std, q25/q50/q75, row count (cached like describe)
- `column_stats`      — statistics for ONE column: distinct count, null count/%, min/max, q25/q50/q75 and the top-5 values with counts, over the same bounded sample profile_table uses (`SQLHANDLER_PROFILE_MAX_ROWS`)
- `run_sql`           — run SELECT-only SQL via DuckDB (aggregations/joins); markdown (default), JSON or CSV output; optional bind `params` and time travel. DDL/DML is refused by default (`SQLHANDLER_MCP_READONLY`, decision D2)
- `scan_table`        — pull columns/rows via pyarrow with a row limit; same output formats and time travel
- `query_submit` / `query_status` / `query_result` / `query_cancel` — **async query jobs**: start a long query and get a `job_id` immediately, poll its status, fetch the result once (then it is freed from memory), cancel mid-flight. Same engine path as `run_sql`: the read-only guard applies at **submit** time, the `SQLHANDLER_QUERY_TIMEOUT` (600s default) is watchdog-enforced even when nobody polls, `SQLHANDLER_MAX_ROWS` bounds results. The registry is in-memory (a restart clears it — resubmit) and capped at `SQLHANDLER_MAX_JOBS` (default 8; beyond that the submit is refused with a clear message). REST twins under `/api/jobs/*`
- `query_save` / `query_list` / `query_delete` / `query_saved` — **saved parameterized queries**: name → SQL + default bind params (`$name` / `?` placeholders — never string-interpolated), stored in a JSON file (`SQLHANDLER_SAVED_QUERIES_PATH`). Save-time validation parses the SQL and applies the read-only guard (re-applied at run time, so a hand-edited store cannot smuggle DDL). **Writes are auth-gated**: when `SQLHANDLER_API_TOKEN` or `MCP_API_KEYS`/`SQLHANDLER_API_KEYS` is configured, an unauthenticated `query_save`/`query_delete` is refused; with no credential configured (single-user-local mode) writes are allowed and the posture is logged at startup. REST twins under `/api/saved-queries/*`

In federated multi-source mode, tables are source-qualified: `list_tables` returns all sources, and `describe_table` / `run_sql` take names like `sales_orders` or `inventory_raw_customers` (bare names only when unique).

## Agent ergonomics (0.9.0)

Features that make LLM agents effective against the lake on the first try:

### profile_table — statistics before SQL

`describe_table` gives names/types; `profile_table` gives *values*: min/max, approximate distinct counts, null %, avg/std and quartiles per column, plus the exact row count (from Parquet/Delta metadata). Agents use it to pick correct predicates without trial-and-error queries. Profiling scans a bounded sample (`SQLHANDLER_PROFILE_MAX_ROWS`, default 1M; 0 = full table) and is cached like describe.

### Semantic catalog — business meaning for tables/columns

> 📄 **Shareable spec for data owners:** [`docs/semantic-catalog.md`](docs/semantic-catalog.md) —
> the JSON/YAML format, table-key rules, all attach routes, and troubleshooting.

Point `SQLHANDLER_CATALOG` at a JSON **or YAML** file of human-written documentation and it is merged into `list_tables` / `describe_table` output (and MCP resources):

```json
{
  "tables": {
    "workorder/work_order": {
      "description": "Maintenance work order headers, one row per order",
      "aliases": ["work orders"],
      "columns": {
        "amount": "Order total in USD",
        "kind": "Order class: a=planned, b=unplanned"
      }
    }
  }
}
```

The same document in YAML is accepted everywhere a catalog file is read (JSON is tried first, YAML second — the extension doesn't matter):

```yaml
tables:
  workorder/work_order:
    description: Maintenance work order headers, one row per order
    aliases: [work orders]
    columns:
      amount: Order total in USD
      kind: Order class: a=planned, b=unplanned
```

Keys match the logical `schema/name` path (a bare name or the source-qualified name also work). The file is hot-reloaded on change (cached describes are invalidated so new wording appears immediately); a missing/broken file never breaks queries.

#### Virtual tables — catalog entries with a SQL `definition`

A catalog entry keyed by a clean bare identifier that carries a `definition` (a single `SELECT`/`WITH` query) becomes a **virtual table**: listed with a `VIRTUAL` badge in `list_tables` and the Data Explorer, describable (schema derived from the definition itself, docs merged on top), searchable by its aliases, and queryable like any other table — constructed on the fly, at query time, as a view over the base tables its definition names. Nothing is stored; user filters still push down into the physical scans, and definitions compose (a virtual table may build on another). Definitions must be a single read-only statement in the engine's DuckDB dialect (`iff(...)` is provided as a compat macro; see [`docs/semantic-catalog.md`](docs/semantic-catalog.md), including a full Snowflake→DuckDB port from Omnilife in [`docs/examples/omnilife-catalog.yaml`](docs/examples/omnilife-catalog.yaml)). Storage wins on name collisions; invalid definitions are dropped with a warning, never breaking the catalog.

#### Upload through the UI / API (no file access needed)

The web UI's **Semantic catalog** panel uploads a `.json`/`.yaml` file (or accepts pasted text) straight from the browser; the same operations are available as JSON API endpoints for scripts:

| Endpoint | Purpose |
|---|---|
| `GET /api/semantic-catalog` | which catalog is live, its source, table count |
| `POST /api/semantic-catalog` | body = raw JSON **or YAML** text → validated, stored, hot-reloaded |
| `DELETE /api/semantic-catalog` | remove the uploaded catalog; the configured file takes over |
| `GET /api/semantic-catalog/content?format=yaml\|json` | the live catalog as editable text (YAML default) |
| `GET /api/semantic-catalog/table?table=…&format=yaml\|json` | one table's entry (or `found: false`) |
| `POST /api/semantic-catalog/table` | `{"table", "content"}` — upsert ONE table's entry (merged + stored) |
| `DELETE /api/semantic-catalog/table?table=…` | drop one table's entry |
| `POST /api/highlight` | pygments-guessed inline-styled HTML for the editors (`pygmentize -g` semantics) |

#### Editing in the browser (global + per-dataset)

Two ways to edit the live semantic view, both writing through the same validated store as an upload:

- **Global editor** — the *Semantic catalog* panel (lower left) has an **Edit YAML…** button that opens the whole live catalog as editable YAML (JSON is a toggle; a format switch with unsaved edits asks first). Applying re-validates and hot-reloads exactly like a file upload. With no catalog attached the editor starts from a commented starter template.
- **Per-dataset breakout** — every table gets a **Semantic** tab (next to Query / Schema) showing just that table's entry. Tables with no entry yet open with a skeleton prefilled from the real column list, so documenting a dataset starts from its actual columns. *Apply* upserts only that table's entry (the server keeps the existing entry key — an edit never forks a duplicate); *Remove entry* drops it, and removing the last entry clears the store so the configured file takes back over.

The editor text is syntax-highlighted server-side with the same trick as the `rcat` CLI tool: Pygments guesses the lexer from a (virtual) filename plus content — `pygmentize -g` semantics — and returns inline-styled HTML theme-matched to the UI (dark/light). Without `pygments` installed the editors simply render plain text; highlighting is cosmetic and never gates an edit. All edit endpoints are covered by the same `SQLHANDLER_API_TOKEN` / gateway auth as the rest of `/api/*`, and `SQLHANDLER_CATALOG_UPLOAD=0` disables editing as well as uploading.

```bash
curl --data-binary @catalog.yaml -H "Content-Type: text/plain" \
  https://sqlhandler.<domain>/api/semantic-catalog
```

Uploads land in the **catalog store** (`SQLHANDLER_CATALOG_STORE`, by default next to the disk-warm cache — a writable path in every supported deployment) and take precedence over the configured `SQLHANDLER_CATALOG` file until deleted. Uploaded content is validated (JSON object with a `tables` mapping, ≤1 MB) and re-stored as canonical JSON; YAML requires the `pyyaml` package (bundled since
1.4.0). By default the store is on the pod's tmp emptyDir, so uploads are **per-replica** and last only until rescheduling — for durable, cross-pod uploads set `semanticCatalog.store.enabled: true` in the chart (1Gi RWX PVC shared by all replicas; uploads from any replica reach every replica and still hot-reload — no rollout. RWX is required whenever replicas can exceed 1, and the chart refuses to
render an RWO store for a scale-out deployment). The values/ConfigMap route remains the most auditable option; uploads are a *mutating* endpoint covered by `SQLHANDLER_API_TOKEN` / gateway auth like the rest of `/api/*`, and `SQLHANDLER_CATALOG_UPLOAD=0` disables them entirely.

### MCP resources + prompts

Resource-capable clients get read-only context without tool calls:

- `sqlhandler://catalog` — every table + its catalog description
- `sqlhandler://table/<percent-encoded path>/schema` — one table's schema + column docs
- `sqlhandler://query-memory` — recent successful queries (see below)

Prompts: `explore-data` (list → catalog → profile → SQL workflow) and `analyze-table` (deep-dive one table, profile-first).

### Self-improving loops

- **Query memory** — the engine records the last 50 query outcomes (`SQLHANDLER_QUERY_MEMORY_SIZE`); the `sqlhandler://query-memory` resource lets later agent sessions reuse proven SQL patterns instead of rediscovering them.
- **Did-you-mean errors** — a bad table name in SQL comes back with the nearest real table names attached ("Did you mean one of: workorder_work_order, …"), so the agent self-corrects in one round-trip.
- **Usage-driven prewarm** — when `SQLHANDLER_PREWARM_TABLES` is unset, the busiest tables of the previous run (usage counts persisted with the disk-warm cache) are prewarmed on restart. The server teaches itself what to warm.

## Query engine capabilities (0.9.0)

### Parameterized queries

`run_sql` accepts bind `params` — an object for named `$placeholders` or an array for positional `?` — so reusable query templates stay injection-safe:

```json
{"sql": "SELECT * FROM work_order WHERE kind = $k", "params": {"k": "a"}}
```

### Time travel (`version_as_of`)

`run_sql` and `scan_table` accept `version_as_of` (a non-negative integer) to read a historical snapshot — a **Delta snapshot version** (nfs / onelake / Delta-on-S3) or an **Iceberg snapshot id**. It applies to every versionable table the query touches; plain-Parquet tables in the same query are a clear error. Historical datasets are cached per version (a snapshot never changes).

### Query timeout

`SQLHANDLER_QUERY_TIMEOUT` (seconds; **600 by default** — decision D5, `0` = off) interrupts a query inside DuckDB when it runs too long — no leaked threads, a clean error to the caller. Applies to MCP `run_sql` and the web API alike.

### Async queries (web API)

For long-running queries the JSON API supports submit/poll/paginate/cancel:

| Endpoint | Purpose |
|---|---|
| `POST /api/query/async` | validate + start; returns `{"query_id", "state"}` |
| `GET  /api/query/{id}` | status: `running`/`done`/`error`/`cancelled`, columns, n_rows |
| `GET  /api/query/{id}/rows?offset=&limit=` | paginated rows (page cap 1000) |
| `DELETE /api/query/{id}` | cancel a running query (DuckDB interrupt) |

Finished jobs are kept for `SQLHANDLER_ASYNC_JOB_TTL` seconds (default 900), up to 100 tracked jobs; the registry refuses (HTTP 429) when full of running jobs. The same row caps and read-only guard apply.

### Concurrency cap

`SQLHANDLER_MAX_CONCURRENT_QUERIES` (default 8; 0 = unlimited) bounds the simultaneous DuckDB queries per pod; excess queries queue up to `SQLHANDLER_QUEUE_TIMEOUT` seconds (default 30) and then fail with a clear error instead of piling up on the container.

## Other data sources / roadmap

The DataProvider interface is the only thing a new source touches - new backends are a subclass plus one make_provider branch. In rough order of value for large-scale data access:

| Source | What it gives you | Cost |
|---|---|---|
| Delta Lake on S3 | Your Parquet already has a Delta _delta_log (transactions, time travel) but sits in an S3 bucket. Reuses the same deltalake reader as OneLake, just against s3:// storage. | Low - a format flag on S3Provider |
| DuckDB httpfs native | Let DuckDB read S3 Parquet directly via its bundled httpfs extension instead of through a pyarrow dataset - fewer moving parts, sometimes better pushdown. | Low - a query path variant |
| **Local / NFS directory** | **Implemented** as the `nfs` backend (Delta + Parquet) - see above. | Done |
| Azure Blob / ADLS Gen2 | ADLS/blob storage that has not been loaded into Fabric, using pyarrow's AzureFileSystem. | Low |
| GCS | Google Cloud Storage Parquet via pyarrow's GCS filesystem. | Low |
| **Iceberg tables** | **Implemented** as an `iceberg` backend (REST or SQL catalog) — see above. | Done |
| Hive / HMS catalog | If a Hive Metastore already owns the metadata, list/describe through it and read the underlying Parquet. | Medium |
| Trino / BigQuery / Snowflake | A distributed engine as the source - only needed when object-store reads alone cannot carry the concurrency or the compute already lives there. | Medium-high - new SQL bridge |

Rule of thumb: if the data is (or can be) columnar Parquet/Delta in an object store, pointing a DataProvider at it beats round-tripping rows through a JDBC/engine bridge - which is exactly why SQLhandler replaced EzPresto.
## Security note

The temporary service principal credential from Toromont was delivered in plaintext email. Keep it only in `config/.env` (git-ignored) or the deployment secret store, and **rotate it** once the integration is confirmed.

## Dev

```bash
uv pip install -e '.[dev]'
ruff check src
pytest
```