# Three-way MCP head-to-head, storage equalized (2026-09-23)

Legs (all MCP, ClusterIP): **sqlhandler-NFS** (nfs backend on `bench-data-nfs` PVC — same `gl4f-filesystem` VAST NFS class as ezpresto's mirror, identical bytes from `s3://test-parquet`) · **sqlhandler-S3** (prod, MinIO, 4 replicas) · **ezpresto** (its PVC mirror).
Parity gate in-pod: employees 50,000 / orders 200,000 / customers 150,000 / testdata_50col 3,000,000 — EQUAL on all three. All rows 5/5 ok on all legs.

| query | SH-NFS warm | SH-NFS cold | SH-S3 warm | SH-S3 cold | EZ warm | EZ cold | EZ/NFS warm | EZ/NFS cold |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| count_small | 14.4 | 71.1 | 28.1 | 311.7 | 146.2 | 132.1 | **10x** | **1.9x** |
| range_filter | 5.0 | 53.4 | 221.6 | 200.4 | 232.3 | 228.3 | **46x** | **4.3x** |
| filtered_agg | 11.3 | 104.5 | 177.1 | 186.8 | 494.0 | 499.6 | **44x** | **4.8x** |
| groupby_agg | 12.3 | 118.9 | 145.6 | 209.3 | 393.7 | 398.0 | **32x** | **3.3x** |
| join_count | 13.1 | 114.4 | 221.4 | 214.9 | 361.8 | 328.3 | **28x** | **2.9x** |
| join_agg | 14.3 | 128.9 | 276.5 | 296.4 | 456.2 | 468.9 | **32x** | **3.6x** |
| count_big_3m | 14.5 | 84.4 | 16.9 | 272.0 | 245.4 | 207.8 | **17x** | **2.5x** |
| col_proj_agg_2cols | 14.5 | 81.2 | 328.5 | 385.0 | 565.9 | 495.4 | **39x** | **6.1x** |
| filtered_group_3m | 20.9 | 96.4 | 453.9 | 507.5 | 504.6 | 447.5 | **24x** | **4.6x** |
| wide_agg_4cols | 14.2 | 85.3 | 25.6 | 491.8 | 496.4 | 442.4 | **35x** | **5.2x** |
| **conc L4** (8 q) | **142.8 qps** (p50 38.9) | — | 27.3 qps | — | 9.0 qps (p50 759) | — | **15.9x** | — |

**With storage equalized (same NFS class, same files), sqlhandler wins 10/10 warm (10x-46x) AND 10/10 cold (1.9x-6.1x).** The NFS leg also proves the nfs backend end-to-end (probe, discovery of hive-partitioned + deeply nested layouts).
SH-NFS warm 5-21 ms = the true single-replica cache floor; its cold 53-129 ms shows DuckDB+local NFS vs Presto on the same storage class: engine beats engine with no storage excuse in either direction.
SH-S3 warm variance this run (vs its own cold) is the documented 4-replica L1 lottery + TTL expiry of the morning's warmth; ezpresto warm~cold as always (no cache).
