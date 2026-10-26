# Semantic catalog — the JSON/YAML spec

*Shareable one-pager for data owners: how to describe your tables so SQLhandler agents see business meaning, not just column dtypes.*

The **semantic catalog** is a small file of human-written documentation that SQLhandler merges into `list_tables` / `describe_table` output, the MCP resources, and the Data Explorer. Agents use it to pick the right table, write correct filters on the first try, and decode coded values.

---

## 1. The file

One object with a `tables` mapping. Each key identifies a table; each value describes it.

**JSON:**

```json
{
  "tables": {
    "workorder/work_order": {
      "description": "Maintenance work order headers, one row per order",
      "aliases": ["work orders", "WO headers"],
      "columns": {
        "amount": "Order total in USD",
        "kind": "Order class: a=planned, b=unplanned"
      }
    }
  }
}
```

**The exact same document in YAML** (also accepted everywhere; friendlier to hand-edit):

```yaml
tables:
  workorder/work_order:
    description: Maintenance work order headers, one row per order
    aliases: [work orders, WO headers]
    columns:
      amount: Order total in USD
      kind: Order class: a=planned, b=unplanned
```

### Fields (all optional except the table key)

| Field | Where | What |
|---|---|---|
| `description` | table | One to three sentences: what one row is, grain, refresh cadence, gotchas |
| `aliases` | table | List of alternative names people/agents might search for |
| `columns` | table | Map of column name → one-line description. Only documented columns are annotated; undated columns stay untouched |
| `definition` | table | SQL (a single `SELECT`/`WITH` statement) that makes the entry a **virtual table** — see below |

### Table keys — three forms that all work

| Key form | Example | Use when |
|---|---|---|
| Logical path | `workorder/work_order` | The table lives in a folder/schema (`<schema>/<name>`) — most precise |
| Source-qualified | `warehouse_orders` | Federated multi-source deployments (`<source>_<table>`) |
| Bare name | `work_order` | Quick start; matches whatever path the table actually has |

The engine tries **path → qualified → bare name**, so a bare name keeps working even if the table moves. Start with bare names; promote to paths if two tables share a name.

### Virtual tables — entries with a `definition`

An entry whose key is a clean bare identifier (`^[A-Za-z_][A-Za-z0-9_]*$`) and that carries a non-empty
`definition` becomes a **virtual table**: it shows up in `list_tables` (marked `VIRTUAL`), `describe_table`,
`search_tables` and the Data Explorer like any other table, but nothing is stored behind it — SQLhandler
constructs it **on the fly, at query time**, as a view over the base tables the definition names. Filters and
projections from the user's query push down through it into the physical scans, and it composes with joins,
CTEs and other virtual tables like any real table.

```yaml
tables:
  vw_big_sales:
    description: Sales over threshold, with the store name resolved
    aliases: [big sales]
    definition: |
      SELECT s.id, s.amount, store.name
      FROM sales s JOIN dim_store store USING (store_id)
      WHERE s.amount > 100
```

Rules of the road:

- **Dialect**: definitions run on the engine's DuckDB. `GROUP BY ALL`, `GREATEST`, `::INT` casts and CTE column
  lists work as in Snowflake, and the engine bridges the two constructs DuckDB lacks: an `iff(c, t, e)` compat
  macro, and `ARRAY_CONSTRUCT_COMPACT(...)` rewritten to `list_filter([...], x -> x IS NOT NULL)` at
  registration time (string-literal-safe, any arity). Snowflake-style definitions using those two run verbatim;
  anything more exotic needs porting (see [`examples/omnilife-catalog.yaml`](examples/omnilife-catalog.yaml)
  for a worked example).
- **Validation**: a definition must be exactly one read-only statement and must parse; anything else is dropped
  with a warning (a broken entry must never break the catalog). Definitions execute on the same locked-down,
  read-only connections as every other query — they add no new capability, only curated, reusable query shapes.
- **Storage wins**: a virtual entry whose name collides with a physical table is ignored — the catalog can
  never shadow real data.
- **Base-table scope**: definitions resolve base tables among the lake's tables and other virtual tables;
  attached-database tables (SQLHANDLER_ATTACH) are not pulled in automatically.
- **Costs are query-time costs — paid once**: the first query touching a virtual table materializes its full
  result to parquet; every later query (UI previews, filtered reads, aggregates) reads that file — milliseconds
  instead of re-running the pipeline — until the definition, any base table's snapshot version, or
  `SQLHANDLER_VIRTUAL_CACHE_TTL` (default 3600; `0` disables) changes. Unfiltered previews cannot short-circuit
  a definition with blocking aggregates, which is exactly why the materialization exists. Profiling and its row
  count run the definition (bounded by `SQLHANDLER_PROFILE_MAX_ROWS` for the sample); `scan_table` works via the
  SQL path but its pyarrow `filters` argument is refused (use `run_sql`). Time-travel queries bypass the cache.
- `virtual: true` **without** a `definition` is just a documentation marker — no table appears.

### Authoring tips

- Describe the **grain** ("one row per order", "one row per order-line") — the single biggest accuracy win for aggregations.
- Decode **coded columns** (`kind: a=planned, b=unplanned`) — agents cannot guess these.
- Mention **units and time zones** in the column doc, not the table doc.
- Keep it honest and short; every word lands in the agent's context window.

---

## 2. Attaching it

Any **one** of these routes; they all hot-reload (no restart, in-flight calls unaffected):

**A. Chart values (durable, all replicas — the operator route).** In the PCAI *Helm Values* editor / a `helm/local/values.<site>.yaml` — paste the catalog as **native YAML** (no stringification):

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

The chart renders the structure to canonical JSON into a read-only ConfigMap on every pod. Alternative: `semanticCatalog.json:` takes the same document as an inline JSON **string** (machine-generated catalogs; mutually exclusive with `tables` — setting both fails the render).

**B. Upload from the Data Explorer (self-service, both formats).** Open `/ui` → **Semantic catalog** panel → *Upload JSON/YAML…* (`.json`, `.yaml`, `.yml`) or paste the text → **Apply**. Takes effect immediately.

**C. API (scripts / CI):**

```bash
# upload (body = raw file contents, JSON or YAML)
curl --data-binary @catalog.yaml -H "X-API-Token: $TOKEN" \
  https://sqlhandler.<domain>/api/semantic-catalog

# status: which catalog is live, where it came from, how many tables
curl -H "X-API-Token: $TOKEN" https://sqlhandler.<domain>/api/semantic-catalog

# remove the uploaded catalog (falls back to the configured file)
curl -X DELETE -H "X-API-Token: $TOKEN" https://sqlhandler.<domain>/api/semantic-catalog
```

**D. Out-of-band ConfigMap** (platform teams): set `semanticCatalog.existingConfigMap: <name>` to a ConfigMap you own carrying a `semantic-catalog.json` key; manage it with your own pipeline.

### Which catalog wins?

Uploaded catalog (B/C) → configured file (A/D) → nothing. An upload overrides the configured file **until it is deleted** (or the pod's ephemeral store disappears). With route A seeded and route B used for live edits, the values file remains the auditable baseline.

---

## 3. Persistence of uploads

| Store | Scope | Set via |
|---|---|---|
| tmp emptyDir *(default)* | one pod, gone on reschedule | nothing |
| Shared PVC **1Gi RWX** | all replicas, survives rescheduling | `semanticCatalog.store.enabled: true` (values) |

```yaml
semanticCatalog:
  store:
    enabled: true
    size: 1Gi            # catalogs are KBs of text
    accessModes: [ReadWriteMany]   # RWX required whenever replicas can exceed 1
```

RWX (ReadWriteMany) is mandatory for multi-replica deployments — the chart refuses to render an RWO store for a scale-out deployment (RWO attaches on one node only and strands the other pods Pending). The store mount is read-write; the values/ConfigMap mount is read-only.

---

## 4. Behavior & limits

- **Hot reload** — every route: edits appear in `list`/`describe` without a restart (file mtime is watched; cached describes are invalidated).
- **Fail-open** — a broken/unreadable catalog degrades to *no documentation*; queries never break. (Uploads are the exception: invalid content is rejected with a precise error, before anything is written.)
- **Validation** — top-level object with a `tables` mapping; each table entry a mapping; ≤ 1 MB; UTF-8. YAML needs the `pyyaml` package (bundled since v1.4.0).
- **Multi-replica convergence** — shared-PVC/ConfigMap updates propagate to each pod via the kubelet (typically seconds, worst case ~1 minute); replicas can briefly disagree on wording, then converge.
- **Upload is gated** — same auth as the rest of `/api/*` (`SQLHANDLER_API_TOKEN` / PCAI gateway); disable entirely with `SQLHANDLER_CATALOG_UPLOAD=0`.

## 5. Troubleshooting

| Symptom | Check |
|---|---|
| Descriptions don't show | `GET /api/semantic-catalog` → `active_source` / `active_tables`; or the UI panel status line |
| Upload rejected "not valid JSON or YAML" | Lint the file locally (`python -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" catalog.yaml`) |
| Upload rejected "PyYAML is not installed" | YAML route needs image ≥ v1.4.0 (or convert to JSON) |
| One pod shows old wording | Someone uploaded to that pod (store wins); `DELETE /api/semantic-catalog` or let it reschedule |
| PVC stuck Pending | StorageClass lacks RWX — pick an NFS-backed class or run single-replica with `accessModes: [ReadWriteOnce]` |
