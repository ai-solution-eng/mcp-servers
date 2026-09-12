# Benchmarks — why SQLhandler scales out

Two measurement campaigns on the **SE G2** cluster
(`pcai-se-ai-application.hst.rdlabs.hpecorp.net`, sqlhandler namespace,
MinIO-backed workload) drove the chart's scaling defaults. Raw results live in
[`../bench/`](../bench/) — every number below is reproducible from there.

## Campaign 1 — scale-out evidence (2026-09-08)

**Setup.** sqlhandler behind the PCAI gateway (`sqlhandler.pcai-se-ai-application.hst.rdlabs.hpecorp.net`),
workload = pool of 9 queries (counts, range filters, aggregations, joins over
200K–3M-row tables), 3 batches per concurrency level, harness
[`../bench/ezpresto_vs_sqlhandler.py`](../bench/ezpresto_vs_sqlhandler.py) with
persistent pooled connections. First a **1-replica** deployment
([`../bench/bench_full_comparison.{json,log}`](../bench/bench_full_comparison.log)),
then the same workload against **4 replicas** (two independent runs:
[`../bench/bench_sqlhandler_4replica.{json,log}`](../bench/bench_sqlhandler_4replica.log),
[`../bench/bench_sqlhandler_4replica_v2.{json,log}`](../bench/bench_sqlhandler_4replica_v2.log)).

**1 replica — throughput collapses as clients are added:**

| clients | qps | p50 | p95 | max |
|---:|---:|---:|---:|---:|
| 1 | 5.54 | 448 ms | 744 ms | 744 ms |
| 2 | 3.88 | 1 332 ms | 1 815 ms | 1 815 ms |
| 4 | 4.27 | 2 036 ms | 3 170 ms | 3 170 ms |
| 8 | 2.80 | 4 048 ms | 8 867 ms | 8 980 ms |
| 16 | 2.66 | 8 240 ms | **17 766 ms** | 18 303 ms |

**4 replicas — flat ~6 qps across the whole range** (two runs, A / B):

| clients | qps A | qps B | p50 @16 | p95 @16 |
|---:|---:|---:|---:|---:|
| 1 | 6.97 | 6.12 | — | — |
| 2 | 6.58 | 5.05 | — | — |
| 4 | 7.68 | 7.62 | — | — |
| 8 | 5.51 | 5.88 | — | — |
| 16 | 5.67 | 6.47 | 3 066 ms / 3 977 ms | 8.3 s / 7.0 s |

**Findings (the basis of the chart's scaling defaults):**

- The embedded DuckDB engine **serializes requests per process**, so a single
  replica halves its throughput and degrades p95 to ~18 s under 16 concurrent
  clients. **CPU/memory bumps on a single replica buy nothing** — the limits
  went unused.
- **Scale out, not up**: 4 replicas held ~6 qps flat with a fleet-wide peak of
  only **~0.24 cores / 1.3 GiB** (k8s metrics during the run) — throughput is
  replica-count-bound, not resource-bound.
- The **800m-CPU variant** ([`../bench/bench_v120_800m.{json,log}`](../bench/bench_v120_800m.log))
  shows the CPU *limit* is what bounds wide scans: at 800m (~1 DuckDB thread)
  4 replicas still held 5.2–6.7 qps, but per-scan latency suffers — raise the
  CPU **limit** for large-table scans (Omnilife runs 16 cores), keep the
  requests small.
- **HPA owns the replica count** (`autoscaling.enabled`, min 4 / max 8 on G2).
  The CPU@80% target is **runaway protection, not load-following**: steady-state
  CPU sits near ~8% of limits even at full concurrent load, so the HPA fires
  only on heavy scan bursts. For load-following scale-out add a custom metric
  (`autoscaling.extraMetrics`, needs prometheus-adapter) — e.g.
  requests-per-second per pod targeted at ~1.5, the measured per-pod saturation
  point. A PDB (`minAvailable: 2`) and slow scale-down stabilization keep
  bursts from thrashing the fleet.

## Campaign 2 — engine & caches, v1.6.0 (2026-09-12)

Methodology, full tables and the in-cluster variant:
[`../bench/BENCHMARK.md`](../bench/BENCHMARK.md). Headlines, measured with the
same harness (4 replicas × 8 vCPU/16Gi, 10-query workload 50K–3M rows):

- **Query result cache**: identical queries served from memory, keyed by
  sql/params/limits + base-snapshot versions — **1.8×–21× over cold**,
  scaling with query cost (aggregations 6.9–19.2×). Warm floor ~104–120 ms
  through the gateway (~18–25 ms in-cluster), flat from 50K to 3M rows.
- **Cold engine** (pure DuckDB): 3M-row scans 1.0–2.0 s p50 (4.3–5.1M rows/s on
  counts/projections); bare `count(*)` hits a metadata fast-path
  (~125 ms → ~0.1 ms on 20M rows).
- **Concurrency**: warm queries scale to ~75–81 qps at 8-way parallelism with
  flat p50; **all-cold bursts saturate per-pod CPU** (flat ~11–12 qps fleet
  throughput, p95 to 4.2 s) — the capacity-planning number for cold agent
  storms, and the reason the per-pod concurrency cap
  (`SQLHANDLER_MAX_CONCURRENT_QUERIES`, default 8) exists.
- **Virtual-table materialization cache** on a shared PVC materializes a
  virtual table's result once per deployment, not once per pod.

EzPresto-side environment findings recorded during the runs (locator routing
splits, pod flapping, OPA denies) are documented in
[`../bench/BENCHMARK.md`](../bench/BENCHMARK.md) §"Environment findings" — they
affect the comparison legs, not sqlhandler.

## Raw data & reproduction

| Artifact | Contents |
|---|---|
| [`../bench/bench_full_comparison.{json,log}`](../bench/bench_full_comparison.log) | 1-replica run, 2026-09-08 (latency + concurrency) |
| [`../bench/bench_sqlhandler_4replica.{json,log}`](../bench/bench_sqlhandler_4replica.log) | 4-replica run A |
| [`../bench/bench_sqlhandler_4replica_v2.{json,log}`](../bench/bench_sqlhandler_4replica_v2.log) | 4-replica run B |
| [`../bench/bench_v120_800m.{json,log}`](../bench/bench_v120_800m.log) | 4 replicas @ 800m CPU limit |
| [`../bench/workload_g2.json`](../bench/workload_g2.json) | the 10-query workload definition |
| [`../bench/ezpresto_vs_sqlhandler.py`](../bench/ezpresto_vs_sqlhandler.py) | stdlib-only harness (MCP + REST legs, warm/cold cache-busting, integrity guards) |
| [`../bench/BENCHMARK.md`](../bench/BENCHMARK.md) | full 2026-09-12 methodology + tables |

The 2026-09-08 numbers above are quoted from the raw logs in `bench/`; the
"0.24 cores / 1.3 GiB fleet peak" observation comes from the cluster metrics
captured during that run and is recorded in the chart's scaling comments
(`helm/values.yaml`, `helm/values-examples/values.g2.yaml`).
