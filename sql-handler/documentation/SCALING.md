# Scaling: one process or multiple uvicorn workers per pod?

Question: should the container run N uvicorn worker processes per pod
(`uvicorn.run(..., workers=N)`) instead of today's single process? Answer:
**no — scale by replicas, not workers.** Evidence below from
`src/sqlhandler/{resources,engine,l2cache,server,fastrender}.py`,
`helm/values.yaml`, `bench/BENCHMARK.md`, `documentation/BENCHMARKS.md`,
`Dockerfile`.

## 1. What saturates under load, and what N workers do to the budget

Cold queries saturate DuckDB CPU: bench/BENCHMARK.md's "Cold qps is flat
(~11–12) at every level" across L1→L8 — 4 replicas × 8 vCPU could not go
faster because every call was real compute (p95 4.2 s); documentation/
BENCHMARKS.md says it from the other side: CPU/memory bumps on one replica
"buy nothing (limits went unused)". The DuckDB budget is derived **per
process from the pod's cgroup**, and every uvicorn worker would re-derive
the identical budget:

    budget["memory_limit"] = _memory_budget_string(int(memory * fraction))   # resources.py:185
    budget["threads"] = max(1, int(cpu))                                     # resources.py:187

`memory` is the POD's cgroup `memory.max` (`container_memory_bytes()`) and
`cpu` the POD's cgroup quota (`container_cpu_count()`) — no worker-count
division exists anywhere in the module. With N workers:

- **Memory multiplies.** Each worker grants DuckDB 0.6 × the memory limit
  (`duckdbMemoryFraction: "0.6"`) — 9.6 GiB each at 16Gi; two workers =
  19.2 GiB of engine allowance inside a 16 GiB cgroup. The 0.6 exists so
  ONE engine spills to `temp_directory` instead of being OOMKilled (which
  "wipes every in-process cache", resources.py docstring); N workers turn
  the safety fraction into `0.6 × N`.
- **CPU does not multiply.** The cgroup quota is shared by all container
  processes: N × `threads = floor(cpu)` DuckDB threads just oversubscribe
  the same 4 vCPU (chart default) or 8 vCPU (bench env) — cold qps per pod
  stays ~flat; wide scans gain no silicon, only scheduler contention.
- Dividing instead (`memory_limit = fraction × limit / N`,
  `threads = max(1, floor(cpu / N))`) removes the OOM risk and the thing
  that makes big scans fast: a lone 3M-row aggregate drops from T threads
  to T/N — per-query cold latency multiplies ~N for no throughput gain.

Net: workers>1 either multiplies the OOM-risky knob or divides the
throughput knob. Cold capacity scales with the pod's CPU limit or the
replica count, not with process count.

## 2. The L1 result cache is per-process — dilution WITHIN a pod

`SqlEngine` is a process-wide singleton (`_handler()`, server.py), so the
memory L1 (`_result_cache`) is per process. bench/BENCHMARK.md documents
the multi-POD version of this pathology ("Per-replica cache physics"):
warm p95 outliers 1.0–3.2 s vs ~105 ms p50, because "4 replicas each hold
their own cache, and a connection that lands on a replica that hasn't seen
that query pays the cold cost once." N workers per pod reproduces that
INSIDE one pod — and worse: uvicorn's multiprocess mode shares one
listening socket and the kernel distributes CONNECTIONS across workers,
while MCP clients pool connections (the bench's own methodology fix: "one
pooled HTTP(S) connection per thread"). A repeated identical query lands
on an arbitrary worker whose L1 hasn't seen it.

The warm ceiling is cache-hit-rate-bound, not glue/CPU-bound: warm
157.5 qps at L4 across 4 replicas (2026-09-23) is only ~39 qps/replica, yet
the single-replica NFS leg hit **142.8 qps, warm p50 38.9 ms, in ONE
process** — the 4-replica leg's lower per-replica number is the L1 lottery,
not process capacity. More processes per pod = lower hit rate per process =
lower warm qps.

The shared L2 (`l2cache.py`) mitigates only part of this: OFF by default
(`cache.l2.enabled: false`; needs an RWX PVC), and results smaller than
`SQLHANDLER_L2_MIN_BYTES` (256 KiB) deliberately skip it — "PVC IO
round-trips small results slower than recomputing them" (l2cache.py). The
hits that matter most (the ~18–25 ms in-cluster warm floor; small results)
are exactly the ones L2 would not serve.

## 3. The GIL story — the heavy path is already parallel

Each query runs on its own raw `threading.Thread` (`QueryJob.__init__`,
engine.py:273) against its own `duckdb.connect()` (engine.py:280; per job,
closed in the job's `finally` — 8 further call sites in engine.py for the
describe/profile paths plus policy.py's filter validation, each applying
the same budget via `_apply_memory_budget`). `threads=` is never set ON
the connect call; it is applied post-connect via `SET threads=`
(engine.py:561–564). DuckDB/pyarrow release the GIL during execution and
the Arrow fetch (`rel.arrow()`, engine.py:318), so the heavy path
parallelizes within one process; the server offloads whole tool dispatches
with `asyncio.to_thread(_dispatch_tool, ...)` (server.py:298), keeping the
event loop responsive for other sessions and probes.

The GIL-bound remainder is Python-bytecode glue — markdown/JSON rendering,
transport framing — and it is shrinking: `fastrender.py` replaces the
pandas DataFrame materialization with a pure-Arrow renderer and `orjson`
(3–8× faster) on hot JSON payloads. Nothing measured shows GIL contention
capping a single process today.

## 4. When N workers WOULD help (the honest case)

- **Executor starvation / blast radius.** `asyncio.to_thread` uses the
  default executor: min(32, cpu+4) threads — 8 slots at the chart's 4-CPU
  limit. `SQLHANDLER_MAX_CONCURRENT_QUERIES` is also 8, and each dispatch
  blocks a slot in `job.wait()` up to the 600 s query timeout. A full query
  storm can occupy every executor slot, so /ready's own `to_thread` checks
  (server.py:2341–2342) queue behind 600 s queries and the pod flaps
  degraded. N workers = N independent pools = 1/N blast radius. (A
  dedicated probe executor fixes this more cheaply.)
- **One stuck request.** A wedged non-query request (huge render, slow
  client) degrades one worker, not the pod; the engine's thread + timeout +
  `interrupt()` design already contains the query-shaped version.
- **Transport/glue-bound fleets.** Many concurrent tiny cached calls would
  gain loop/socket parallelism — but the measured warm ceiling is
  cache-hit-rate-bound (§2): one process already served 142.8 warm qps.

So workers>1 buys isolation and loop headroom, not query throughput; both
real problems above have cheaper fixes.

## 5. Verdict and conditions

**Default: keep one uvicorn process per pod (status quo); scale by replicas
(HPA).** That is the configuration the benchmark evidence supports, and it
keeps the per-process artifacts (L1 hit rate, job registry, DuckDB budget)
coherent. The code cannot opt in via env anyway: `main()` passes the app
OBJECT (`uvicorn.run(app, ...)`, server.py:2287), and uvicorn refuses
`workers > 1` without an import string ("You must pass the application as
an import string to enable 'reload' or 'workers'", verified on
uvicorn 0.52.4); the Dockerfile CMD (`python -m sqlhandler.server`, no
ENTRYPOINT) launches exactly one process.

If workers>1 is ever chosen, it is a deliberate code change: (1)
`server.py main()` — expose an importable app and run
`uvicorn.run("sqlhandler.server:app", ..., workers=N)`; (2) `resources.py`
— divide the budget per worker. uvicorn provides no per-worker identity, so
a supervisor-set `SQLHANDLER_WORKER_TOTAL/_INDEX` (or equivalent) is
required, then `memory_limit = fraction × limit / N` and
`threads = max(1, floor(cpu / N))` — without this, OOM exposure (§1); (3)
shared-or-divided state: `cache.l2` becomes required (RWX PVC), yet small
results still bypass it (§2); the async job registry needs `query.jobsDir`
on an RWX PVC or `query_status`/`query_result` fail across workers (the
chart documents the identical cross-replica issue); saved queries and the
catalog store are files — safe, racing benignly (last write wins); (4)
re-bench warm qps per pod — the 2026-09-23 tables are the baseline, and
pooled MCP connections must be counted per worker, not per pod.

Conditions under which workers=2–4 is defensible: a fleet of short-lived
MCP clients (a connection per call, defeating socket stickiness), traffic
dominated by small cached calls, L2 on an RWX PVC, budgets divided per
worker. For the measured workload — pooled connections, mixed cold/warm,
big scans — single process per pod + replicas is what the evidence supports.
