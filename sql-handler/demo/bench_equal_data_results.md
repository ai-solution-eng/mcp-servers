# Equal-data in-cluster head-to-head (2026-09-23)

Parity gate at bench time: employees 50,000 / orders 200,000 / customers 150,000 / testdata_50col 3,000,000 — **EQUAL** on both engines.
Mode: sqlhandler via REST twin (ClusterIP; /mcp is API-key-gated on v2.3.2 in-cluster), ezpresto via MCP execute_query (ClusterIP). 5 warm + 5 cold reps each, all 5/5 ok.

| query | SH warm | SH cold | EZ warm | EZ cold | warm ratio | cold ratio |
|---|---:|---:|---:|---:|---:|---:|
| count_small | 13.5 | 151.4 | 138.6 | 126.0 | 10.27x | 0.83x |
| range_filter | 98.1 | 132.5 | 219.6 | 210.7 | 2.24x | 1.59x |
| filtered_agg | 154.3 | 165.6 | 482.5 | 449.7 | 3.13x | 2.72x |
| groupby_agg | 112.4 | 200.0 | 394.1 | 416.2 | 3.51x | 2.08x |
| join_count | 189.5 | 196.9 | 372.9 | 327.6 | 1.97x | 1.66x |
| join_agg | 344.6 | 482.8 | 428.2 | 388.0 | 1.24x | 0.80x |
| count_big_3m | 15.3 | 350.4 | 237.4 | 215.7 | 15.52x | 0.62x |
| col_proj_agg_2cols | 296.1 | 375.1 | 575.5 | 545.5 | 1.94x | 1.45x |
| filtered_group_3m | 470.6 | 493.7 | 539.2 | 548.2 | 1.15x | 1.11x |
| wide_agg_4cols | 361.0 | 478.6 | 551.2 | 480.6 | 1.53x | 1.00x |
| conc L4 (8 q) | **37.6 qps** (p50 24 ms) | — | 6.2 qps (p50 782 ms) | — | **6.1x** | — |

- sqlhandler warm wins 10/10 (1.1x-15.5x); cache floor 13-15 ms on counts, ~100-360 ms on aggregations.
- Cold: sqlhandler wins 7/10 (up to 2.7x); ezpresto's local-disk mirror wins the 3 scan-heaviest counts/joins (its count(*) metadata path is immune to cache-busting comments).
- ezpresto warm ~ cold everywhere (no result cache).
- Warm/cold gap is narrower than the September run because 10 distinct queries sweep the 4-replica L1 cache round-robin (realistic LB behavior; September's sweep hit the same effect).
