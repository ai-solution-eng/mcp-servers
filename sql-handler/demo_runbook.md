# Customer Demo — G2 Sample Data

## ⭐ The headline flow: ask_data — plain English in, data out

**The centerpiece of the demo is natural language.** Type a question, and the
app does the analyst work in the open: keyword-matches the question against the
catalog, surfaces candidate tables (including the virtual ones), shows the
schema and live column statistics, estimates the cost, and drafts a SQL query —
explicitly **not executing anything** until a human runs it.

**Proven phrasing** (validated end-to-end against this data):

> **"What was our quarterly revenue by region in 2024 and 2025?"**

What it produces, live:
1. **Table discovery**: shortlists `orders_line_totals` (virtual), 
   `revenue_by_region`, `vip_revenue`, `customers` — ranked, with matched
   columns and each table's catalog description ("Premium orders with a
   computed line_total…").
2. **Schema + column stats** for the best hit: 4 regions, ~100k rows, price
   range 250–500 — the planner's reasoning is visible.
3. **Cost note**: row count from metadata with an exact/approx confidence tag.
4. **A drafted query** (shown, not run): 
   `SELECT order_id, customer_id, region, product, qty, unit_price FROM orders_line_totals LIMIT 1000`
   — plus a suggested follow-up (`column_stats`) and the "run this with
   run_sql" footer.
5. **The demo beat**: run the draft as-is and the app returns
   `E_ROWS_CAPPED` — *"narrow with WHERE filters or aggregate."* That's a
   trust moment: **it refuses to dump 100k rows into a chat window.**

Then the natural follow-up, in two more sentences of English:

> "OK — aggregate that: revenue and order count per quarter per region."

which becomes the validated aggregate (36 rows, ~instant):

```sql
SELECT date_trunc('quarter', order_ts) AS quarter,
       region,
       round(sum(qty * unit_price)) AS revenue,
       count(*)                     AS orders
FROM orders_line_totals
WHERE order_ts < TIMESTAMP '2026-01-01'   -- drop the partial Jan-2026 stub
GROUP BY 1, 2
ORDER BY 1, 2
```

**Saved as `demo_quarterly_region`** (SQL fallback if you don't want to type
live): `query_saved("demo_quarterly_region")`

Sample rows to expect:

| quarter    | region |   revenue | orders |
|------------|--------|----------:|-------:|
| 2023-01-01 | NA     | 39,807,513 |  2,121 |
| 2023-01-01 | APAC   | 39,210,760 |  2,079 |
| 2023-04-01 | EU     | 39,133,096 |  2,083 |
| 2025-10-01 | EU     | 41,125,431 |  2,132 |

**Talking points while ask_data runs:**
- Discovery is *transparent* — the customer sees **why** a table was picked
  (matched columns, catalog descriptions), not a black box.
- **Virtual tables show up in NL too** — `orders_line_totals` and
  `revenue_by_region` are definitions, not stored data, and the planner
  understands them ("definitions compose, nothing is stored").
- **Draft ≠ execute**: every NL question ends in a reviewable query and an
  explicit run step. Safe-by-default is the story, not a party trick.
- If asked how accurate the draft is: it's honest keyword+schema matching over
  catalog metadata, not an LLM — set that expectation up front and it plays as
  reliability, not weakness.

**Backup NL phrasings** (also validated): "How has monthly revenue trended
from 2023 through 2025?" (lands on `revenue_by_region` — a chance to show a
materialized, TTL-cached aggregate) and "Who are our top 10 VIP customers by
revenue?" (lands on the `customers` dimension — segue into the VIP join below).

## SQL showcase #1: 3-year monthly trend with window functions

One query, one table, three "wow" features: date truncation, MoM deltas via
`lag()`, and a 3-month moving average via a window frame. Well under a second
over 200k rows.

```sql
WITH monthly AS (
    SELECT date_trunc('month', order_ts) AS month,
           sum(qty * unit_price)         AS revenue
    FROM orders
    WHERE order_ts < TIMESTAMP '2026-01-01'   -- drop the partial Jan-2026 stub
    GROUP BY 1
)
SELECT month,
       round(revenue)                                                     AS revenue,
       round(100 * (revenue / lag(revenue) OVER (ORDER BY month) - 1), 1) AS mom_pct,
       round(avg(revenue) OVER (ORDER BY month
             ROWS BETWEEN 2 PRECEDING AND CURRENT ROW))                   AS moving_avg_3m
FROM monthly
ORDER BY month
```

**Saved as `demo_monthly_revenue_trend`** — run it live with:
`query_saved("demo_monthly_revenue_trend")`

Sample output:

| month      |   revenue | mom_pct | moving_avg_3m |
|------------|----------:|--------:|--------------:|
| 2023-01-01 | 70,251,666|         |     70,251,666 |
| 2023-02-01 | 64,745,621|    -7.8 |      67,498,643 |
| 2023-03-01 | 72,375,437|    11.8 |      69,124,241 |

Talking points while it runs:
- **`lag()` + window frames** — the analyst's bread-and-butter, done in plain
  SQL, no exports to Excel.
- **36 months of history** in one result set — great for pasting straight into
  the customer's BI tool.
- Optionally follow with the one-liner region breakdown:
  `SELECT region, sum(qty*unit_price)/1e6 AS revenue_m FROM orders GROUP BY 1`
  → four regions within ~2% of each other (~625M each), i.e. a healthy global
  business.

## Follow-up: semantic-layer query (no joins required)

```sql
SELECT customer_name, country, orders, round(revenue) AS revenue
FROM vip_revenue
ORDER BY revenue DESC
LIMIT 10
```

**Saved as `demo_vip_top10`.** Shows the virtual-table / semantic-catalog
feature: the customer asks a business question ("who are our best VIP
accounts?") and the join lives in the definition, not the query.

## ⚠️ Data caveats (know these before the demo)

1. **`orders.customer_id` is a broken FK.** Only ~2,975 of 200,000 orders match
   a `customers.customer_id` (the generator drew the FKs independently). A
   plain `orders JOIN customers` silently drops 98.5% of revenue — do **not**
   demo a raw join and present the totals as "company revenue." The shipped
   `vip_revenue` view is built on exactly this tiny match, which appears to be
   by design ("who really pays the bills" = the matched slice).
2. **Jan-2026 is a 4-day stub** (data ends 2026-01-04). An unfiltered monthly
   trend shows a scary −89.2% "collapse" in the last row. The headline query
   filters it out; if asked, it's a great segue into "partial periods in
   dashboards" as a data-quality talking point.
3. All names/companies are generator fiction — safe to show on screen.

## Suggested 5-minute demo flow

1. **⭐ `ask_data`: "What was our quarterly revenue by region in 2024 and 2025?"**
   — English → transparent table discovery → drafted SQL → the E_ROWS_CAPPED
   guardrail moment → run the aggregate (36 rows, 4 regions × 12 quarters).
2. **`ask_data` follow-up**: "OK — aggregate that: revenue and order count per
   quarter per region." — shows the iterative, conversational style.
3. **`ask_data`: "How has monthly revenue trended from 2023 through 2025?"**
   — lands on `revenue_by_region` (materialized + TTL-cached — "aggregates
   here are stored, refreshed, and cached, not recomputed every time").
4. **`query_saved demo_monthly_revenue_trend`** — the SQL showcase: window
   functions over the monthly trend (⭐ for technical buyers).
5. **`query_saved demo_vip_top10`** — "same business question through the
   semantic layer" (customers × premium orders).
6. **If the customer is technical**: `explain_query` on the aggregate to show
   cost estimation and warm/cold caching, or `profile_table("orders")` to show
   column profiling before a single line of SQL is written.
7. **Close with the guardrail story**: the E_ROWS_CAPPED moment from step 1 —
   the app won't dump 100k rows into a chat; it pushes you toward aggregates.
   "Governed by default" is the pitch.
