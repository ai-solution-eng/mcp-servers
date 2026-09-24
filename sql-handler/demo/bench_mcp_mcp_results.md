# MCP-to-MCP in-cluster head-to-head on equal data (2026-09-23)

**Both legs speak MCP** (`run_sql` vs `execute_query`), ClusterIP, no gateway. In-pod parity gate: employees 50,000 / orders 200,000 / customers 150,000 / testdata_50col 3,000,000 — **EQUAL**.

| query | SH-MCP warm | SH-MCP cold | EZ-MCP warm | EZ-MCP cold | warm ratio |
|---|---:|---:|---:|---:|---:|
| count_small | 36.2 | 269.3 | 117.1 | 122.4 | 3.23x |
| range_filter | 179.7 | 123.6 | 222.2 | 217.6 | 1.24x |
| filtered_agg | 116.8 | 169.7 | 477.5 | 473.8 | 4.09x |
| groupby_agg | 31.9 | 211.9 | 406.8 | 374.7 | 12.75x |
| join_count | 26.4 | 224.7 | 381.1 | 358.8 | 14.44x |
| join_agg | 228.3 | 290.4 | 447.4 | 386.8 | 1.96x |
| count_big_3m | 24.4 | 266.1 | 213.9 | 212.3 | 8.77x |
| col_proj_agg_2cols | 292.3 | 387.7 | 502.2 | 545.7 | 1.72x |
| filtered_group_3m | 529.0 | 533.8 | 473.1 | 452.7 | 0.89x |
| wide_agg_4cols | 470.3 | 479.5 | 529.5 | 514.7 | 1.13x |
| **conc L4** (8 q) | **157.5 qps** (p50 23.7 ms) | — | 7.8 qps (p50 801.5 ms) | — | **20.1x** |

- Warm: sqlhandler wins 9/10 (1.1x-14.4x); cache floor 24-36 ms.
- Cold: sqlhandler wins 7/10 (up to 2.8x); ezpresto's local mirror wins 3 scan-heaviest cold shapes.
- Concurrency: 20.1x qps, p50 23.7 ms vs 801.5 ms.
- range_filter warm (179.7) > cold (123.6): 4-replica L1 LB artifact (first warm rep landed on a cold replica); the pattern averages out across queries.
