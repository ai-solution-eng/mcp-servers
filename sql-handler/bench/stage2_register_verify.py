#!/usr/bin/env python3
"""Register the MinIO test-parquet tables as file:// external tables in Presto
(after bench data has been copied to a shared PVC path) and verify row counts
match SQLhandler before benchmarking.

Usage: stage2_register_verify.py [dest_root]   (default /data/cache/bench-data)
"""
import sys

sys.path.insert(0, ".")
from _pp import q  # noqa: E402

DEST = sys.argv[1] if len(sys.argv) > 1 else "/data/cache/bench-data"

# table -> relative dir under DEST (dirs, because the file metastore
# requires external locations to be directories)
TABLES = [
    ("orders", "sales/orders",
     "order_id BIGINT, customer_id BIGINT, region VARCHAR, product VARCHAR, "
     "qty BIGINT, unit_price DOUBLE, order_ts TIMESTAMP"),
    ("employees", "sales/employees",
     "emp_id BIGINT, name VARCHAR, dept VARCHAR, salary DOUBLE, hire_date DATE"),
    ("customers", "customers",
     "customer_id BIGINT, customer_name VARCHAR, segment VARCHAR, country VARCHAR, "
     "sign_date DATE, annual_spend DOUBLE, is_vip BOOLEAN"),
    ("top_orders", "top_orders",
     "order_id BIGINT, customer_id BIGINT, region VARCHAR, product VARCHAR, "
     "qty BIGINT, unit_price DOUBLE, order_ts TIMESTAMP"),
    ("c", "finance/b/c",
     "id BIGINT, kind VARCHAR, value DOUBLE"),
    ("testdata_50col", "testdata_50col",
     "tenant_id INTEGER, region_id INTEGER, store_id INTEGER, category_id SMALLINT, "
     "brand_id INTEGER, supplier_id INTEGER, warehouse_id SMALLINT, channel_id TINYINT, "
     "promotion_id INTEGER, loyalty_tier TINYINT, quantity INTEGER, returns_qty SMALLINT, "
     "units_in_stock INTEGER, visit_count INTEGER, lead_time_days SMALLINT, "
     "customer_id BIGINT, batch_number BIGINT, order_number BIGINT, hash_value BIGINT, "
     "sequence_no BIGINT, unit_price DOUBLE, discount_rate DOUBLE, tax_rate DOUBLE, "
     "shipping_cost DOUBLE, weight_kg REAL, volume_cubic_m REAL, rating REAL, "
     "margin_pct DOUBLE, price_amount DECIMAL(18,2), order_amount DECIMAL(18,2), "
     "is_active BOOLEAN, in_stock BOOLEAN, is_prime_member BOOLEAN, is_fraud_flag BOOLEAN, "
     "order_date DATE, ship_date DATE, expected_delivery_date DATE, "
     "order_created_at TIMESTAMP, last_updated_at TIMESTAMP, event_ts TIMESTAMP, "
     "heartbeat_at TIMESTAMP, sku VARCHAR, product_name VARCHAR, order_status VARCHAR, "
     "country VARCHAR, region_name VARCHAR, city_code VARCHAR, payment_method VARCHAR, "
     'customer_segment VARCHAR, "comment" VARCHAR'),
]

EXPECTED = {
    "orders": 200000,
    "employees": 50000,
    "customers": 150000,
    "top_orders": 50000,
    "c": 20000,
    "testdata_50col": 3000000,
}


def main():
    ok = True
    for name, rel, cols in TABLES:
        loc = f"file:{DEST}/{rel}"
        try:
            q(f"DROP TABLE IF EXISTS minio.default.{name}")
            q(f"CREATE TABLE minio.default.{name} ({cols}) "
              f"WITH (external_location='{loc}', format='PARQUET')")
            cnt = q(f"SELECT COUNT(*) FROM minio.default.{name}")[0][0]
            if cnt == EXPECTED[name]:
                status = "OK"
            else:
                status = f"MISMATCH (SQLhandler says {EXPECTED[name]:,})"
                ok = False
            print(f"  {name:16s} rows={cnt:,}  {status}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {name:16s} FAILED: {str(exc)[:250]}")
    print("VERIFICATION", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
