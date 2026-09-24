# MCP Tools Reference — SQLhandler

The SQLhandler MCP surface: endpoint, authentication, every tool's purpose and input/output schema, the MCP resources and prompts, and the self-correction behaviors an agent should rely on.

> **See also:** [FEATURES.md](FEATURES.md) for the full feature reference, [DEPLOYMENT.md](DEPLOYMENT.md) for cluster deployment, [../docs/semantic-catalog.md](../docs/semantic-catalog.md) for the semantic-catalog / virtual-table spec, and the [README](../README.md) for backend configuration.

---

## 1. Server endpoint

Transport is **`streamable-http`**, stateless per-request (`stateless_http=True` + `json_response=True` — any replica serves any request, no in-memory session state), port **9097** at path **`/mcp`**.

| Access method | URL |
|---|---|
| Via cluster ingress (production) | `https://sqlhandler.<your-domain>/mcp` |
| Via `kubectl port-forward` (local) | `http://localhost:8001/mcp` (after `kubectl port-forward svc/sqlhandler 8001:9097`) |

The same process also serves the read-only web UI (`/ui`, `/`), a JSON API (`/api/*` — the REST twins of most tools), `/health`, `/ready`, and `/metrics`.

**Auth (opt-in, per surface).** MCP API keys are configured through the Helm values — `security.apiKey.existingSecret` (a Kubernetes Secret; the chart wires it via `secretKeyRef`, never values literals) — and the server accepts `X-API-Key` or `Authorization: Bearer <key>` on `/mcp`; keys are re-read per request, so rotation needs no restart (append the new key to the Secret, move clients over, drop the old). Unset → the endpoint runs open with a loud startup warning (single-user local mode). Write-capable tools (`query_save` / `query_delete`, and the write tier) are **auth-gated regardless**: with no credential configured they refuse or degrade loudly, never silently. The underlying env the chart renders (`SQLHANDLER_API_KEYS` / `SQLHANDLER_API_TOKEN` for the REST `/api/*` surface) is an implementation detail — operators set values, not envs. See the *Key configuration knobs* table in [FEATURES.md](FEATURES.md).

**Strict Host pinning.** `/mcp` rejects requests whose `Host` header does not match the configured host (DNS-rebinding defense; HTTP 421 on mismatch). Route agents through the deployed hostname or the port-forward above.

**Policy cache separation (when policy-as-code is on — the `security.policy.enabled` chart value).** Every cache key (result L1/L2, virtual materialization, describe, profile, column stats) carries `policy=<sha256>` of the caller's effective rules — masked and unmasked callers never share an entry, while two identically-restricted callers still share.

---

## 2. Available tools (18)

Grouped by role. **The recommended agent loop** is: `search_tables`/`list_tables` → `describe_table` → `profile_table` (or `column_stats`/`sample_rows`) → `explain_query` (optional) → `run_sql`. Errors carry structured codes and did-you-mean hints — a bad table name in SQL returns the nearest real table names, so an agent self-corrects in one round-trip.

### Discovery & schema

| Tool | Purpose | Returns |
|---|---|---|
| `list_tables()` | Enumerate every table in the configured data source, annotated with semantic-catalog descriptions; plus a live inventory of attached (read-only) external databases (postgres/mysql/mariadb/sqlite/sqlserver/ducklake/mongodb/bigquery — the last two need their community extensions baked into the image; see README: External databases). Source-qualified names in federated mode. | Table list (name, schema, format, rows, URI, catalog docs) |
| `search_tables(keyword)` | Keyword search over table names, column names **and** catalog docs, ranked best-first (exact/substring, then a fuzzy layer so typo'd names still match). Each hit carries its `matched_on` reasons. | Ranked matches |
| `describe_table(table)` | Columns, types, canonical URI + catalog docs for one table. Cached (`cache.ttl`); instant warm. | Column/type list + URI |
| `profile_table(table, columns?)` | Column-level statistics **before** writing SQL: min/max, approx distinct count, null %, avg/std, q25/q50/q75, exact row count from Parquet/Delta metadata. Scans a bounded sample (chart value `query.profileMaxRows`, default 1M; 0 = full). | Stats table |
| `column_stats(table, column)` | The same statistics for ONE column, plus top-5 values with counts — over the same bounded sample (never a full-table scan beyond it). Pick filter values, spot skew. | Stats + top values |
| `sample_rows(table, limit?, columns?)` | A bounded head of actual rows with per-column fill rates (fill %, null counts) — one bounded look at the DATA before writing SQL. The scan stops early. | Sample rows + fill rates |

### Querying

| Tool | Purpose | Returns |
|---|---|---|
| `run_sql(sql, params?, output_format?, limit?, version_as_of?)` | Execute a read-only SQL query. Tables by folder name (`work_order_header` or `schema/name`); attached databases as `<db-alias>.<schema>.<table>`, joinable with lake tables in one query. `output_format`: **markdown** (default) / json / csv / **arrow** (IPC, base64). `params` travel as bind parameters. `version_as_of`: Delta snapshot / Iceberg snapshot-id time travel. SELECT/EXPLAIN-SELECT only (sqlguard). | Markdown table (or chosen format) |
| `scan_table(table, columns?, limit?, filters?, version_as_of?)` | Fetch rows/columns via pyarrow columnar pull with row limit — no SQL. Prefer `run_sql` when filters/aggregations can push into the scan; use this to sample raw columns or feed a programmatic caller. | Rows (chosen format) |
| `explain_query(sql, params?, version_as_of?, include_plan?)` | **Cost estimate without execution**: referenced tables with metadata row counts and bytes-to-scan (each labeled `exact` from Delta/Iceberg metadata, `approx` from Parquet row-group totals, `none` for attached DBs), every number keyed to the snapshot version it describes, plus the **warm/cold band** (is the exact query identity already in the L1 result cache / shared L2). `include_plan` adds DuckDB's `EXPLAIN (FORMAT JSON)` summary — planning only, no row is read, a virtual table is never materialized. | Cost estimate + plan summary |
| `ask_data(question, profile?)` | **Plan a natural-language question, don't execute it**: keyword-searches tables (top 5), describes the best hit (≤20 columns + catalog docs), optionally profiles ≤6 columns, drafts ONE candidate SELECT and a suggested follow-up. Output ends in a *run this with run_sql* footer — execution is always a separate, explicit call. | Markdown plan + drafted SQL |

### Async jobs (long queries)

| Tool | Purpose | Returns |
|---|---|---|
| `query_submit(sql, params?, limit?)` | Start an async query job, return its `job_id` immediately — for queries that may outlive a tool-call timeout. Read-only guard applied at SUBMIT; same engine path, same timeout watchdog (chart value `query.timeoutSeconds`, 600s default), registry cap (chart value `query.maxJobs`, default 8). | `job_id` |
| `query_status(job_id)` | Poll: state (running/done/error/cancelled), elapsed, error, and — when done, unfetched — column names and row count. No row data. | Status object |
| `query_result(job_id, output_format?)` | Fetch a finished job's result **ONCE** (markdown default, json, csv, arrow), then the spooled result is freed — a second fetch of the same job is refused (resubmit instead). | Result in chosen format |
| `query_cancel(job_id)` | Cancel a running job (DuckDB interrupt). | Cancellation confirmation |

### Saved queries

| Tool | Purpose | Returns |
|---|---|---|
| `query_save(name, sql, params?, description?)` | Save a parameterized query for reuse. SQL is parsed and SELECT-guarded at save time; `params` are stored as **bind** parameters (`$name` / `?` — never string-interpolated). **Writes are auth-gated**: with an API token/key configured, an unauthenticated save is refused; with none (single-user local mode), allowed with a loud note. | Save confirmation |
| `query_list()` | List saved queries (name, SQL, default params, description), newest first. | Saved-query list |
| `query_delete(name)` | Delete a saved query (same auth gating as `query_save`). | Deletion confirmation |
| `query_saved(name, params?)` | Run a saved query. Call-time params override stored ones (dict merge) and travel as bind parameters. The read-only guard is re-applied at run time. `output_format`/`limit`/`version_as_of` work like `run_sql`. | Result |

---

## 3. MCP resources & prompts

Beyond tools, the server exposes two read-only **resources** and two **prompts** (MCP primitives):

| Resource / prompt | Purpose |
|---|---|
| `sqlhandler://catalog` | Every table + its business description — the data dictionary (semantic catalog merged in; can be sourced from a dbt manifest via `POST /api/semantic-catalog/import-dbt` — see [docs/semantic-catalog.md](../docs/semantic-catalog.md#importing-from-dbt)). |
| `sqlhandler://table/<name>/schema` (template) | One table's schema + column docs. |
| `sqlhandler://query-memory` | The last 50 query outcomes (chart value `query.queryMemorySize`) — later sessions reuse proven SQL patterns instead of rediscovering them. |
| Prompt `explore-data` | Guided loop: list → catalog → profile → SQL. |
| Prompt `analyze-table` | Profile-first deep dive on one table. |

---

## 4. Output formats & fidelity notes

- **markdown** (default) — compact, agent-readable; types are inferred, wide results truncate.
- **json / csv** — full fidelity for rows; csv loses dtypes on round-trip.
- **arrow** — base64-encoded Arrow IPC stream; exact dtypes (decimals, timestamps, NULLs) preserved; use for programmatic callers and wide results.
- All query tools cap rows (chart value `query.maxRows`, default 1000) and render nothing beyond it — pages are explicit (`limit`), not streamed.

> Raw-format (CSV/JSON) landing-zone tables, where enabled, behave identically at the tool surface — see the raw-format section of [FEATURES.md](FEATURES.md) for the discovery rules and honest performance notes. They are enabled via the `rawFiles.*` Helm values (discovery size-capped), badge as `RAW` in listings, and never take the count(*) metadata fast path.

---

## 5. Connecting an MCP client

### 5.1 Generic streamable-http client

```json
{
  "mcpServers": {
    "sqlhandler": {
      "url": "https://sqlhandler.<YOUR-DOMAIN>/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

All 18 tools are exposed on every connection; the client's `tools` config can hide specific tools from the model (e.g. disable `ask_data` for agents that should not plan without executing).

### 5.2 opencode

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "sqlhandler": {
      "type": "remote",
      "url": "https://sqlhandler.<YOUR-DOMAIN>/mcp",
      "headers": {
        "Authorization": "Bearer {env:SQLHANDLER_INGRESS_TOKEN}"
      }
    }
  }
}
```

In-cluster (behind oauth2-proxy/EZUA), include the bearer token header exactly as in 5.1. The Helm chart wires `mcp.apiKeys` to a Secret for key auth (see [DEPLOYMENT.md](DEPLOYMENT.md)).

### 5.3 Worked example (the agent loop)

```json
// 1. list_tables()  ->  ["work_order_header", "work_order_note_recent", ...]
// 2. describe_table("work_order_note_recent")
// 3. column_stats("work_order_note_recent", "note_data")
// 4. run_sql:
{
  "name": "run_sql",
  "arguments": {
    "sql": "SELECT DATE_TRUNC('month', h.open_date) AS month, COUNT(DISTINCT n.work_order_number) AS wos FROM work_order_note_recent n JOIN work_order_header h ON h.work_order_number = n.work_order_number WHERE (LOWER(n.note_data) LIKE '%leak%') GROUP BY 1 ORDER BY 1",
    "output_format": "markdown"
  }
}
```

On error (e.g. a typo'd table name), the response includes structured code + did-you-mean candidates; retry the same tool with the corrected name — no re-discovery needed.

### 5.4 Pattern: explore cheaply, execute deliberately

- `ask_data` when the question is vague — it plans without executing.
- `explain_query` before an expensive `run_sql` — the warm/cold band tells you whether the result cache will answer instantly.
- `query_submit` → `query_status` → `query_result` when a query may outlive the client timeout.
- `query_save` + `query_saved` for repeated analytics; bind params keep the cache key shared across param values where the engine can reuse them (same SQL text).
- Bare LIMIT previews take a first-row-group fast path (query.previewFastpath).

### 5.5 Backends: what the tools see

The tool surface is identical on every backend (values `backend:` / federated `sources:`): OneLake (Fabric Delta), S3/MinIO (Parquet/Delta), ADLS Gen2 (`adls.enabled` + `adls.account`/`adls.filesystem` — Parquet/Delta via pyarrow's AzureFileSystem, Entra client-secret or anonymous), GCS (`gcs.enabled` + `gcs.bucket` — Parquet/Delta via pyarrow's GcsFileSystem, mounted key file or anonymous), Iceberg (REST — incl. Databricks Unity Catalog — SQL, Glue, Hive-metastore, or Nessie catalogs), NFS/local (Delta/Parquet + raw landing-zone files), and Delta Sharing (`sharing.enabled` + a profile ConfigMap or endpoint/token Secret — tables address as `<share>_<schema>_<table>`). No tool changes per backend; only `list_tables`/`describe_table` output (URIs, formats, badges) reflects the source. Backend selection is deployment configuration, never a tool argument.

---

## 6. Web UI inspector tab

The Data Explorer's **Inspector** tab (next to Query/Schema/Semantic) is a browser-side MCP inspector: it lists **the same 18 tools and input schemas as this document** — `GET /api/inspector/tools` serves them from the registered tool definitions (one source of truth with `tools/list`) — builds an arguments form from each tool's `inputSchema` (required args marked, enums as selects), and shows the MCP-shaped result (`{content, isError}`): tool errors render inline with the did-you-mean hints, and a Raw toggle shows the exact bridge JSON. The `table` argument auto-fills from the selected table in the left list. Calls (`POST /api/inspector/call {name, arguments}`) dispatch through the server's own `tools/call` path, so identity, policy, caching, the audit trail and the saved-query write gate behave exactly as over `/mcp` — but auth rides the **`/api` surface** (`SQLHANDLER_API_TOKEN` / PCAI gateway), never the `/mcp` API keys. **No new env vars or config**: the tab is present wherever the web UI is.

---

*Generated from the tool registrations in `src/sqlhandler/server.py` (2.3.2 tree). When tools change, update this file — the signatures above mirror the MCP `input_schema` blocks.*
