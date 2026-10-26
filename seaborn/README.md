# seaborn-mcp-server (statistical-visualization-mcp)

## Disclosure
This MCP server was created with the help of [MiniMax M2.7](https://huggingface.co/MiniMaxAI/MiniMax-M2.7) using Opencode in HPE Private Cloud AI, inspired by the [Claude Seaborn Statistical Visualization Skill](https://mcpmarket.com/tools/skills/seaborn-statistical-visualization-5). **v0.1 (2026-09) redesigned the tool surface around three rules: data by reference, compact responses, minimal tool count** — the old 17-tool / data-in-args / dual-HTML design made models avoid the server entirely.

## Tools (3)

| Tool | Purpose |
|---|---|
| `plot` | One chart from SQL, a URL, or inline rows. Kinds: scatter, line, histogram, kde, box, violin, bar, count, regression, residual, correlation_heatmap, pair, facet, pie. |
| `describe` | Profile a dataset (dtypes, missing, cardinality, numeric summary, sample) to choose the right chart. |
| `health_check` | Liveness. |

## Data sources (exactly one per call)

1. **`sql`** (preferred): a read-only `SELECT`/`WITH` executed through the
   sqlhandler MCP server (`SQLHANDLER_MCP_URL`, default
   `http://sqlhandler.sqlhandler.svc.cluster.local:9097/mcp`). The model
   never carries rows through the tool call.
2. **`data_url`**: `https://` URL returning JSON records or CSV. SSRF guard:
   loopback/RFC1918/metadata/`.svc`/`.internal` targets are refused.
3. **`data`**: inline records — only sensible under ~200 rows (warning
   attached above that; hard cap 20k rows).

## Response shape (compact by default)

`status, kind, source, rows, data_profile, stats, chart_json (vega-lite),
sample_rows (default 10), warnings` — ~6 KB of JSON for a typical chart,
vs ~40 KB of dual-rendered HTML before.

Optional: `include_png: true` adds a base64 PNG (vision-model friendly,
600 KB cap); `include_html: true` adds interactive plotly + mpld3 HTML for
UI callers (off by default — useless to an LLM, and 95% of the old payload).

## Stack

- **MCP 2.0 SDK** (`mcp>=2.0.0`, `MCPServer`), served **stateless**
  streamable-http (`/mcp`, `json_response=True`) — any replica serves any
  request, no session state (protocol 2026-07-28).
- **httpx2** for all outbound calls (sqlhandler client, URL fetch). No httpx.
- Optional bearer auth on `/mcp` via `SEABORN_API_KEYS` (comma-separated;
  unset = open mode with a loud startup warning).

## Env

| Var | Default | Purpose |
|---|---|---|
| `SQLHANDLER_MCP_URL` | `http://sqlhandler.sqlhandler.svc.cluster.local:9097/mcp` | sql data source |
| `SEABORN_MCP_PORT` | `9092` | HTTP bind port |
| `SEABORN_API_KEYS` | unset | bearer keys gating `/mcp` |
| `SQL_MCP_TIMEOUT_S` | `120` | sqlhandler round-trip timeout |
| `SEABORN_FETCH_TIMEOUT_S` | `30` | URL fetch timeout |
| `MPLCONFIGDIR` | unset | set to a writable dir in containers (font cache) |

## Run

```bash
pip install -r requirements.txt
python server.py                      # stateless /mcp on :9092
```

Gateway federation (pcai-llm `seed_registry.py`): server label `seaborn`,
URL `http://mcp-seaborn-server.mcp-seaborn-server.svc.cluster.local:9092/mcp`.

## E2E tests

The suite spawns the server plus a stub sqlhandler, drives every path
through a real MCP 2.0 client, and asserts **httpx is ABSENT** from the
runtime (full-migration proof):

```bash
scratch_workspace/seaborn-e2e/venv/bin/python -m pytest \
  scratch_workspace/seaborn-e2e/test_seaborn_e2e.py -v   # 9 passed
```
