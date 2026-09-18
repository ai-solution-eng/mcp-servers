# SQLhandler — Features

Fast, direct SQL access to columnar lake data — OneLake/Delta Lake, S3/MinIO Parquet, Delta-on-S3, Iceberg catalogs, and NFS/local directories — exposed as an **MCP server** (MCP 2.0, stateless streamable-http at `/mcp`) with a bundled read-only web UI and JSON API. This document describes the features of the **current stack** (chart/image **1.6.1**; the 0.9.0→1.0.0 feature wave plus the subsequent 1.1–1.6 additions unless marked otherwise).

---

## Backends & data sources

One engine, four backends (selected by `SQLHANDLER_BACKEND` / the chart's `backend:` value) — they share the same SQL engine, caches, MCP tools, and backend-aware readiness probe; only the `DataProvider` differs:

| Backend | Reads |
|---|---|
| `onelake` | Microsoft Fabric OneLake — Delta Lake over ABFS, Entra service principal |
| `s3` / `minio` | S3-compatible object storage (MinIO/AWS) — Parquet, plus Delta tables via `S3_FORMAT=auto` (`_delta_log` detection, time travel) |
| `iceberg` | Apache Iceberg tables through a REST or SQL catalog, Parquet data files |
| `nfs` / `file` | Mounted directory (NFS/PVC/hostPath) — Delta **and** Parquet |

Plus two composition mechanisms:

- **Federated multi-source** — `SQLHANDLER_SOURCES` / the chart's `sources:` list federates several buckets/sources (any mix of backends) behind one endpoint: source-qualified tables, one shared cache, cross-source `JOIN`s in a single `run_sql`.
- **External databases (read-only attach)** — `SQLHANDLER_ATTACH` / the chart's `databases:` list attaches external servers read-only (`ATTACH ... READ_ONLY` — writes are rejected by DuckDB itself) — Postgres (plus postgres wire-compatible servers), MySQL/MariaDB, SQLite files, and SQL Server via the native-TDS `mssql` community extension; their tables join with lake tables in the same query as `<db-alias>.<schema>.<table>`. TLS and other driver options flow through a per-entry `params` object.

---

## Current stack

| Layer | Technology |
|---|---|
| Language | Python ≥ 3.11, fully type-checked (mypy) and linted (ruff) |
| MCP | `mcp>=2.0` SDK — low-level `MCPServer`, stateless streamable-HTTP at `/mcp` |
| SQL engine | DuckDB ≥ 1.0 (per-process connection; aggregations/joins push down to the lake) |
| Data access | pyarrow ≥ 17 (S3/Azure filesystems + dataset engine), `deltalake` ≥ 1.0 (native Delta reader incl. OneLake ABFS), optional `pyiceberg[pyarrow]` (+ SQLAlchemy for SQL catalogs) |
| HTTP | uvicorn + Starlette ASGI app hosting `/mcp`, the JSON API, `/ui`, `/health`, `/ready`, `/metrics` |
| Web UI | one self-contained `index.html` — no build step, no CDN, no framework |
| Deploy | Helm chart for Kubernetes / PCAI (oauth2-proxy gateway, non-root hardened image, health/startup probes, HPA + PDB + topology spread for scale-out, optional shared catalog PVC) |
| Ops | zero extra dependencies — Prometheus text exposition and JSONL audit log are hand-rolled |

The `DataProvider` interface is the only extension point for a new source: each backend (onelake, s3, iceberg, nfs/file) is a subclass plus one `make_provider` branch.

---

## 1. Agent ergonomics (MCP surface)

Features that make LLM agents effective against the lake on the first try.

### Tools
- `list_tables` — enumerate tables (any backend), annotated with catalog descriptions when configured; source-qualified names in federated mode.
- `search_tables` — keyword search over table/column names **and** semantic catalog docs, ranked best-first (exact/substring scoring, then a fuzzy layer so typo'd names still match); each hit carries its `matched_on` reasons. For when the table list is long.
- `describe_table` — columns/types/URI + catalog docs.
- `profile_table` — column-level statistics **before** writing SQL: min/max, approx distinct count, null %, avg/std, q25/q50/q75, exact row count from Parquet/Delta metadata. Optional comma-separated `columns` subset. Scans a bounded sample (`SQLHANDLER_PROFILE_MAX_ROWS`, default 1M; 0 = full) and is cached like describe.
- `column_stats` — the same statistics for ONE column, plus top values with counts: distinct count, null count/%, min/max, q25/q50/q75, top-5 values — over the same bounded sample (never a full-table scan beyond the profile cap).
- `run_sql` — DuckDB SQL with `output_format` (**markdown** default / json / csv), bind `params`, and `version_as_of` time travel.
- `scan_table` — pyarrow column/row pull with row limit; same formats and time travel.
- `query_submit` / `query_status` / `query_result` / `query_cancel` — **async query jobs**: submit returns a `job_id` immediately (read-only guard applied at SUBMIT), status polls state/columns/n_rows, the result is handed over **once** and then freed from memory, cancel interrupts via DuckDB. Same engine path as `run_sql` (timeout watchdog-enforced, `SQLHANDLER_MAX_ROWS`-bounded, audit-logged); in-memory registry capped by `SQLHANDLER_MAX_JOBS` (default 8; a restart clears it). REST twins under `/api/jobs/*`.
- `query_save` / `query_list` / `query_delete` / `query_saved` — **saved parameterized queries**: a name → SQL + default bind params store (`SQLHANDLER_SAVED_QUERIES_PATH` JSON file; atomic writes; a corrupt file degrades to empty). Save-time validation parses the SQL and applies the read-only guard, re-applied at run time; call-time params override stored ones as **bind** parameters (never string-interpolated). Writes are auth-gated: with `SQLHANDLER_API_TOKEN` / `MCP_API_KEYS` / `SQLHANDLER_API_KEYS` configured, an unauthenticated save/delete is refused; with none configured (single-user-local mode) writes are open with a loud startup note. REST twins under `/api/saved-queries/*`.

### Resources & prompts (the other MCP primitives)
- `sqlhandler://catalog` — every table + its business description (data dictionary).
- `sqlhandler://table/<name>/schema` — one table's schema + column docs (also exposed as the resource template `sqlhandler://table/{table}/schema`).
- `sqlhandler://query-memory` — the last 50 query outcomes (`SQLHANDLER_QUERY_MEMORY_SIZE`), so later agent sessions reuse proven SQL patterns instead of rediscovering them.
- Prompts: `explore-data` (list → catalog → profile → SQL) and `analyze-table` (profile-first deep dive on one table).

### Semantic catalog
`SQLHANDLER_CATALOG=<file.json|file.yaml>` merges human-written table/column documentation into `list_tables` / `describe_table` / resources. Hot-reloaded on change (cached describes invalidated); a missing/broken file never breaks queries.

- **Virtual tables** — a catalog entry keyed by a clean bare identifier with a
  `definition` (a single read-only `SELECT`/`WITH`) becomes a queryable virtual
  table: listed with a `VIRTUAL` badge, schema derived from the definition,
  user filters still push down into the physical scans, definitions compose.
  Materialized results are clustered and reused across queries and replicas
  (see Performance). Spec: [`../docs/semantic-catalog.md`](../docs/semantic-catalog.md).
- **Browser editing (1.4.0)** — a global **Edit YAML…** editor in the lower-left *Semantic catalog* panel (whole live catalog as YAML, JSON toggle) plus a per-dataset **Semantic** tab on every table (that table's breakout; skeletons prefilled from the real column list; per-entry upsert/remove). Both write through the same validated upload store — YAML default, JSON swap, `pygmentize -g`-style server-side highlighting (graceful plain-text fallback without pygments), `SQLHANDLER_CATALOG_UPLOAD=0` disables applying.

### Self-correction loops
- **Did-you-mean errors** — a bad table name in SQL returns the nearest real table names, so the agent self-corrects in one round-trip.
- **Usage-driven prewarm** — with `SQLHANDLER_PREWARM_TABLES` unset, the busiest tables of the previous run (usage counts persisted with the disk-warm cache) are prewarmed on restart.

---

## 2. Query engine capabilities

- **Parameterized queries** — `run_sql` accepts bind `params`: an object for named `$placeholders` or an array for positional `?`. Reusable templates stay injection-safe — and the saved-query tools (`query_save`/`query_saved`) store those templates with their default bind params so they can be rerun by name, values still bound (never interpolated).
- **Time travel** — `version_as_of` on `run_sql` / `scan_table`: a Delta snapshot version (nfs / onelake / Delta-on-S3) or an Iceberg snapshot id. Applies to every versionable table the query touches; plain-Parquet tables in the same query are a clear error. Historical datasets are cached per version (a snapshot never changes).
- **Query timeout** — `SQLHANDLER_QUERY_TIMEOUT` (seconds; **default 600** — decision D5: a runaway query used to hold a concurrency slot forever; `0` = off) interrupts a query inside DuckDB: no leaked threads, clean error to the caller. Applies to MCP tools and the web API alike.
- **Concurrency cap** — `SQLHANDLER_MAX_CONCURRENT_QUERIES` (default 8, 0 = unlimited) bounds simultaneous DuckDB queries per pod; excess queries queue up to `SQLHANDLER_QUEUE_TIMEOUT` (default 30 s) then fail with a clear error instead of piling up on the container.
- **Output formats** — every result-returning tool/endpoint renders markdown (human/LLM-friendly), compact JSON (`{columns, rows}`), or CSV.
- **Delta tables on S3** — `S3_FORMAT=auto|parquet|delta` on the s3 backend: `auto` (default) detects Delta tables by their `_delta_log` and reads the rest as plain Parquet, so one bucket can mix formats with time travel where a Delta log exists.

---

## 3. Web API & UI (read-only data explorer)

Same engine, caches, and read-only guard as the MCP tools — no extra deployment.

### Async query jobs (long-running queries)
| Endpoint | Purpose |
|---|---|
| `POST /api/query/async` | validate + start; returns `{"query_id", "state"}` |
| `GET /api/query/{id}` | status: `running`/`done`/`error`/`cancelled`, columns, n_rows |
| `GET /api/query/{id}/rows?offset=&limit=` | paginated rows (page cap 1000) |
| `DELETE /api/query/{id}` | cancel a running job (DuckDB interrupt) |
| `POST /api/jobs` | the MCP `query_submit` twin: submit-time read-only guard, `SQLHANDLER_MAX_JOBS` cap, watchdog-enforced timeout |
| `GET /api/jobs/{id}` | job status (+ `result_fetched`) |
| `GET /api/jobs/{id}/result` | the result, handed over **once** then freed (second fetch → 409) |
| `DELETE /api/jobs/{id}` | cancel a running job |

The MCP tools (`query_submit`/`query_status`/`query_result`/`query_cancel`) share the `/api/jobs` registry.

Finished jobs are kept for `SQLHANDLER_ASYNC_JOB_TTL` seconds (default 900), up to 100 tracked jobs; the registry refuses with HTTP 429 when full of running jobs.

Jobs, the synchronous query path, and the query timeout all run on the **same `QueryJob` primitive** (own thread, DuckDB `con.interrupt()` for cancellation) — one implementation serving the MCP tools, the JSON API, and the async registry, so cancel/timeout semantics cannot drift between surfaces.

### Profile & export endpoints
- `POST /api/profile` — column statistics (min/max, null %, distinct, quartiles).
- `POST /api/export` — download a query or table as **CSV or Parquet** (attachment), capped by `SQLHANDLER_EXPORT_MAX_ROWS` (default 100 000; 0 = hard 1M ceiling).

### UI features
- Searchable table list with format badges and `source/schema/name` labels in federated mode; per-table schema view.
- **Column stats panel** — lazy "Profile table" button per table.
- SQL editor (Ctrl+Enter) with row limit; results table with timing and copy-CSV.
- **Export buttons** — CSV and Parquet download of the current query.
- **Charts** — one-click inline SVG bar chart of the first numeric column (dependency-free).
- **Saved queries + history** — per-browser (localStorage): save/reload/delete queries; history keeps the last 30 successful runs.
- Light/dark theme (OS-aware, persisted) with HPE branding.
- **Read-only guarantee** — every statement is parsed with DuckDB's own grammar; only plain `SELECT` (plus `EXPLAIN SELECT`) is accepted. Writes, `PRAGMA`/`SET`, and `COPY` are rejected. Results are clamped to `SQLHANDLER_MAX_ROWS` (default 1000).

---

## 4. Observability & ops

- **Prometheus metrics** — `GET /metrics` renders the text exposition (0.0.4) with no extra dependency: `sqlhandler_queries_total{outcome}` (ok/error/timeout/cancelled), `sqlhandler_query_duration_seconds` histogram, `sqlhandler_query_rows_total`, `sqlhandler_cache_{hits,misses}_total{cache}` (describe/profile/dataset), and gauges for table count, process RSS, and the container memory limit.
- **Audit log** — `SQLHANDLER_AUDIT_LOG=/path/audit.jsonl` appends one JSON line per query outcome (`ts`, `event`, `sql`, `state`, `duration_ms`, `n_rows`, `error`) — compliance-grade, SIEM-friendly. Best-effort writes never break a query.
- **API token** — `SQLHANDLER_API_TOKEN` requires `Authorization: Bearer` or `X-API-Token` (constant-time compared) on every `/api/*` request, for deployments not already behind the oauth2-proxy gateway. `/mcp`, `/ui`, `/health`, `/ready` are unaffected (and `/metrics` + `/ready` too, unless `SQLHANDLER_METRICS_AUTH=1`).
- **Optional `/mcp` API-key gate** — `SQLHANDLER_API_KEYS` (or the fleet-universal `MCP_API_KEYS`): when either is set, every `/mcp` request needs `X-API-Key` or `Authorization: Bearer` (constant-time compared); unset → `/mcp` runs open exactly as before. Env re-read per request, so rotation needs no restart.
- **Resilience (carried from 0.8.0)** — cgroup-proportional DuckDB memory budgets with spill-to-disk, disk-warm metadata cache, and health/readiness/startup probes.

### Deployment note (partial chart gap)

The semantic-catalog file is chart-configurable (`semanticCatalog.*` values — ConfigMap + read-only mount + `SQLHANDLER_CATALOG`, validated + hot-reloaded; see README "Semantic catalog") — and, new in 1.4.0, catalogs can also be **uploaded as JSON or YAML straight from the web UI / `POST /api/semantic-catalog`** (engine accepts both formats; `pyyaml` is now a base dependency), with an optional
shared PVC store (`semanticCatalog.store.enabled`) making uploads durable and cross-pod. The remaining fixed-env gaps: the audit log needs a writable volume (emptyDir, or PVC if it must survive restarts), and `/metrics` needs optional scrape annotations. Until a chart follow-up lands, configure those via `docker run -e` — an image-only rollout is **default-safe** (timeout, audit, and token off;
`S3_FORMAT=auto`; export cap 100k; the one new *active* default is the concurrency cap of 8).

---

## 5. Performance (measured on G2, 2026-09-12)

Measured with the repo's own harness (`bench/ezpresto_vs_sqlhandler.py`,
stdlib-only, persistent-connection transport with per-thread pooling) against
the G2 deployment — v1.6.0, 4 replicas × 8 vCPU/16Gi, MinIO-backed workload of
10 queries from 50K to 3M rows. Full methodology, tables, and the in-cluster
variant: [`../bench/BENCHMARK.md`](../bench/BENCHMARK.md), with the scale-out
(HPA) campaign summarized in [BENCHMARKS.md](BENCHMARKS.md).

- **Query result cache** (new) — identical queries served from memory, keyed
  by sql/params/limits + base-snapshot versions (ETL commits invalidate
  instantly; TTL backstop). Measured speedup **1.8×–21× over cold execution**,
  scaling with query cost: counts/filters 1.8–2.5×, aggregations
  **6.9–19.2×**. The Snowflake result-cache analog, and the honest
  agent-facing experience — agents retry, loop, and re-ask.
- **Disk block cache for object stores** (opt-in) — parquet footers and
  column chunks are fetched once into pod-local disk and re-served on every
  later read; the OneLake/Delta path routes its data files through it with
  snapshot-version-scoped keys (Delta-log IO stays inside delta-rs). Wins
  repeat/filtered/preview-style scans — the agent-shaped access pattern.
- **Virtual-table materialization cache** (new) — a virtual table's full
  result is written to parquet once and reused across queries and replicas
  (`cache.virtualCacheDir` on a shared PVC = one materialization per
  deployment per data change). Closes the multi-second cost of unfiltered
  previews on definitions with blocking aggregations (LIMIT cannot
  short-circuit a DISTINCT/GROUP BY pipeline).
- **count(\*) metadata fast-path** (new) — bare `SELECT COUNT(*) FROM t`
  reads parquet/Delta metadata instead of scanning: ~125 ms → ~0.1 ms on a
  20M-row table; visible in-cluster as 3M-row counts in ~23 ms.
- **Cold engine numbers** (result cache busted — pure DuckDB): 3M-row scans
  1.0–2.0 s p50 (4.3–5.1M rows/s counts/projections), 200–350K queries
  0.19–2.0 s. Warm floor ~104–120 ms through the gateway for every query size
  (cache hit + round-trip), ~18–25 ms in-cluster.
- **Concurrency**: warm queries scale to ~75–81 qps at L8 with flat p50;
  all-cold bursts saturate per-pod CPU (flat ~11–12 qps, p95 to 4.2 s) — the
  capacity-planning number for cold agent storms.
- **Clustered virtual materializations** (new) — materialized results are
  auto-sorted by their lowest-cardinality columns so parquet row-group
  statistics prune filtered reads.

### Environment findings during the benchmark (upstream/ops, not sqlhandler)

Recorded with repro evidence in `bench/BENCHMARK.md` §"Environment findings":
the EzPresto locator routes `nextUri` GETs to its web app (404 "Query not
found", 10/10 repro), the EzPresto MCP pod flaps between MCP and health-echo
states (4 restarts, dev image), EzPresto's OPA policy denies table access for
non-platform principals (`AccessDeniedException`), and the ezaf-gateway tier
alternates MCP routes on a minutes cadence for external clients. In-cluster
traffic is unaffected on all counts.

---

## Feature → code → tests map

| Feature | Module(s) | Tests |
|---|---|---|
| `profile_table` / `search_tables` tools, params, time travel, timeout, concurrency gate, query memory | `engine.py`, `server.py` | `test_engine.py` |
| `column_stats` tool, fuzzy `search_tables` ranking | `engine.py`, `server.py` | `test_column_stats.py`, `test_search_fuzzy.py` |
| Async MCP query jobs (`query_submit`/`status`/`result`/`cancel`, `/api/jobs/*`) | `jobs.py`, `engine.py`, `server.py`, `webui.py` | `test_query_jobs.py` |
| Saved parameterized queries (store, bind params, auth gating) | `saved.py`, `server.py`, `webui.py` | `test_saved_queries.py` |
| MCP resources + prompts | `mcp_resources.py`, `server.py` | `test_mcp_resources.py` |
| Semantic catalog merge/hot-reload | `engine.py` | `test_engine.py` |
| Output formats (markdown/json/csv) | `engine.py`, `webui.py` | `test_tools_output.py` |
| Async query jobs (submit/poll/rows/cancel) | `webui.py`, `engine.py` | `test_async_query.py`, `test_webui.py` |
| `/api/profile`, `/api/export` | `webui.py` | `test_webui.py` |
| Delta on S3 (`S3_FORMAT`) | `s3.py`, `config.py` | `test_s3_delta.py` |
| Metrics, audit log, API token | `observability.py`, `server.py` | `test_ops.py` |
| UI: stats panel, charts, export, saved queries | `ui/index.html` | `test_webui.py` |

All tests in the files above pass on the current tree (run with `pytest tests/ -p no:cacheprovider`; the optional s3/iceberg integration files need their documented local setup — see their module docstrings).

## Key configuration knobs

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_CATALOG` | — | Semantic catalog JSON file (hot-reloaded) |
| `SQLHANDLER_QUERY_MEMORY_SIZE` | 50 | Query-memory ring size behind `sqlhandler://query-memory` |
| `SQLHANDLER_PROFILE_MAX_ROWS` | 1000000 | Sample cap for profiling (0 = full table) |
| `SQLHANDLER_MCP_READONLY` | `1` | MCP `run_sql` is SELECT-only (decision D2); `0` restores multi-statement/DDL for trusted callers. Attached external catalogs stay read-only in both modes |
| `SQLHANDLER_ALLOWED_ORIGINS` | none (same-origin) | Extra browser origins allowed on `/api/*`, `/ui`, `/mcp` (CORS + Origin validation, decision D3) |
| `SQLHANDLER_METRICS_AUTH` | off | `1` gates `/metrics` + `/ready` behind the API token / MCP API keys (default off = today's behavior) |
| `SQLHANDLER_QUERY_TIMEOUT` | 600 (decision D5) | DuckDB interrupt after N seconds (0 = off) |
| `SQLHANDLER_MAX_CONCURRENT_QUERIES` | 8 | Per-pod concurrency cap (0 = unlimited) |
| `SQLHANDLER_QUEUE_TIMEOUT` | 30 | Seconds a query may wait for a slot |
| `SQLHANDLER_ASYNC_JOB_TTL` | 900 | Seconds finished async jobs are kept (shared by the web registry and the MCP/jobs registry) |
| `SQLHANDLER_MAX_JOBS` | 8 | Cap on tracked async query jobs (`query_submit` + `/api/jobs`); beyond it submits are refused; garbage/non-positive values fall back to the default |
| `SQLHANDLER_SAVED_QUERIES_PATH` | next to the cache dir | JSON file for the saved-parameterized-queries store (`query_save`/`query_list`/`query_delete`/`query_saved`; writes are auth-gated when a credential env is configured) |
| `SQLHANDLER_EXPORT_MAX_ROWS` | 100000 | Row cap for CSV/Parquet exports (0 = 1M ceiling) |
| `SQLHANDLER_AUDIT_LOG` | — | JSONL audit file path |
| `SQLHANDLER_API_TOKEN` | — | Bearer/X-API-Token gate for `/api/*` |
| `S3_FORMAT` | auto | `auto` \| `parquet` \| `delta` for the s3 backend |
| `SQLHANDLER_ATTACH` / `SQLHANDLER_ATTACH_FILE` | — | Read-only external-database attach config (JSON; `password_env` names only) — `postgres` \| `mariadb` \| `mysql` \| `sqlite` \| `sqlserver`; per-entry `params` for driver options (TLS) |
| `SQLHANDLER_BLOCK_CACHE` / `_DIR` / `_BLOCK_SIZE` / `_MAX_BYTES` / `_INCLUDE_LOCAL` | off | Disk block cache for object-store parquet reads (s3 / iceberg / onelake backends; OneLake Delta data files are read through it snapshot-version-scoped, so a new ETL commit or a time-travel read never serves another snapshot's cached bytes) |
| `SQLHANDLER_RESULT_CACHE_TTL` / `_MAX_BYTES` | 3600 / 256MiB | In-memory result cache for identical queries (snapshot-version-keyed) |
| `SQLHANDLER_VIRTUAL_CACHE_TTL` / `_DIR` / `_MAX_BYTES` / `_SORT` | 3600 / cacheDir / 2GiB / on | Virtual-table materialization cache (point `_DIR` at an RWX PVC to share across replicas) |

### Scale-out deployment (chart keys)

`autoscaling` (HPA, autoscaling/v2, needs metrics-server), `podDisruptionBudget`,
`topologySpread`, `terminationGracePeriodSeconds`, and
`semanticCatalog.store` (shared RWX PVC for uploads + virtual-table
materializations) — the G2 benchmark showed a single replica collapses under
concurrency while 4 replicas hold flat throughput; the HPA's CPU target is
burst/runaway protection, not load-following. Numbers:
[BENCHMARKS.md](BENCHMARKS.md).
