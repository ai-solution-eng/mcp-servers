# Aha idea — SQLhandler as PCAI's agent-facing SQL MCP

## What solution do you suggest?

Ship **SQLhandler** — already deployed and benchmarked on PCAI G2, behind the ezaf gateway + oauth2-proxy — as the default **agent-facing** SQL MCP (EzPresto stays for BI/heavy SQL). It's an MCP server built for agents: DuckDB over columnar scans, measured in-cluster MCP-to-MCP vs. ezpresto-mcp at **5.8×–32× faster warm (10/10 queries), 2×–23× cold (9/10), 31× concurrent throughput** (185 vs 6 qps) — reproducible from `bench/BENCHMARK.md`. Timeouts stop being a failure mode structurally: a result cache keyed by SQL + data-snapshot versions answers repeats in ~20 ms and invalidates instantly on ETL; long queries become async jobs (submit → `job_id` → poll/cancel) instead of blocking the tool call; overload fails fast with a clean error. Two features Presto's MCP will never grow: **virtual tables** (a catalog entry with a SQL `definition` becomes a first-class queryable table — pushdown preserved, materialized once per deployment per data change on a shared PVC, zero DDL, zero writes to the lake) and a **semantic catalog** (human table/column docs merged into list/describe/resources, hot-reloaded, browser-editable). Ask in one line: adopt SQLhandler for the agent path, and make semantic catalog + virtual tables the platform pattern for all PCAI MCP data services.

## What is the problem that the user cannot solve with today's features?

Agents that need lake data go through the EzPresto MCP, and every call pays full query cost — no result cache (measured warm ≈ cold, 132–532 ms per call in-cluster), no async path, and at 4-way concurrent load the fleet serves ~6 qps, so a handful of agents queue behind each other and tool calls block until MCP client timeouts. There is also no way to tell the model what the data *means*: tools return bare names and dtypes, so agents guess at coded columns (`woh_ord_typ`) and burn round trips on trial-and-error SQL — and on the G2 deployment `SHOW TABLES` doesn't even list the tables.

## How do they work around and/or solve the problem? (If they can)

Patience and prompt-stuffing: agents retry through slow scans, split queries to stay under timeouts, and re-derive the same joins wrong 20% of the time and slow 100% of the time; developers hand-copy table/column meanings into each app's system prompt and re-do it per agent. Nothing caches, nothing documents the data, nothing fails fast — so every new agent re-pays the full cost, and the workaround doesn't scale beyond a couple of concurrent sessions.
