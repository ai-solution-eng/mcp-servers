"""Tests for the fuzzy search_tables ranking (Wave 5 additive feature).

The pre-existing exact/substring behavior is pinned by test_tools_output /
test_virtual; these tests cover the ADDED fuzzy layer — typo tolerance over
table names, alias tokens and column names — plus the ranking invariants:
exact/substring hits outrank fuzzy-only hits, and a query that is neither a
substring nor close to any name still matches nothing.
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo


def _make_engine(tmp_path, catalog=None, monkeypatch=None):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2], "amount": [10.0, 20.0], "kind": ["a", "b"]}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self, **kw):
            return [
                TableInfo(name="work_order", schema="workorder", format="parquet"),
                TableInfo(name="orders_archive", schema="sales", format="parquet"),
            ]

        def table_uri(self, info):
            return f"fake://{info.path}"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    if catalog is not None:
        cat = tmp_path / "catalog.json"
        cat.write_text(json.dumps(catalog), encoding="utf-8")
        monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    # cache_ttl > 0 like production: the fuzzy column layer matches against
    # the describe cache (search deliberately never triggers a schema fetch).
    return SqlEngine(P(), cache_ttl=3600)


# ------------------------------------------------------------- fuzzy hits


def test_typo_in_table_name_still_matches(tmp_path):
    eng = _make_engine(tmp_path)
    hits = eng.search_tables("work oder")
    assert hits, "a near-miss name ('work oder') must match work_order"
    assert hits[0]["name"] == "work_order"
    assert "fuzzy-name" in hits[0]["matched_on"]


def test_typo_in_column_name_matches(tmp_path):
    eng = _make_engine(tmp_path)
    eng.describe_table("work_order")  # warm the describe cache (search never fetches)
    hits = eng.search_tables("amont")  # typo of 'amount'
    assert hits and hits[0]["name"] == "work_order"
    assert "amount" in hits[0]["matched_columns"]
    assert "fuzzy-column" in hits[0]["matched_on"]


def test_unrelated_query_still_matches_nothing(tmp_path):
    """The pinned negative: fuzzy must not invent matches out of noise."""
    eng = _make_engine(tmp_path)
    assert eng.search_tables("completely unrelated") == []
    assert eng.search_tables("zzzzzz") == []


def test_matched_on_records_the_reasons(tmp_path):
    eng = _make_engine(tmp_path)
    hits = eng.search_tables("work_order")
    # exact-name is the primary reason; the additive fuzzy layer may append
    # its own (harmless, strictly-bonus) reasons after it.
    assert hits[0]["matched_on"][0] == "exact-name"
    assert hits[0]["matched_on"] == list(dict.fromkeys(hits[0]["matched_on"]))  # deduped
    assert all(
        reason in ("exact-name", "terms-name", "fuzzy-name", "fuzzy-name-token")
        for reason in hits[0]["matched_on"]
    )


def test_fuzzy_tokens_over_aliases(tmp_path, monkeypatch):
    catalog = {
        "tables": {
            "workorder/work_order": {
                "aliases": ["maintenance tickets"],
                "description": "Maintenance work order headers",
            }
        }
    }
    eng = _make_engine(tmp_path, catalog=catalog, monkeypatch=monkeypatch)
    hits = eng.search_tables("maintenence tickets")  # typo'd alias token
    assert hits and hits[0]["name"] == "work_order"
    assert hits[0]["matched_on"]


# ------------------------------------------------------------- ranking


def test_exact_and_substring_outrank_fuzzy_only(tmp_path):
    eng = _make_engine(tmp_path)
    hits = eng.search_tables("orders")
    # both tables contain 'order' as a term; the archive's name is the exact
    # substring hit and must outrank a fuzzy-only second entry if any
    assert hits[0]["name"] == "orders_archive"
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    if len(hits) > 1:
        assert hits[0]["score"] > hits[1]["score"]


def test_results_sorted_by_score_desc_then_name(tmp_path):
    eng = _make_engine(tmp_path)
    hits = eng.search_tables("work orders archive")
    keys = [(-h["score"], h["table"]) for h in hits]
    assert keys == sorted(keys)
