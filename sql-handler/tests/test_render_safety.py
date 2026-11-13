"""Render-safety regressions (audit 2026-11-12).

Markdown: a pipe or newline inside a cell must not shift columns.
CSV: a formula-looking TEXT cell must be neutralized (spreadsheet injection).
"""

import pyarrow as pa

from sqlhandler import server


def _table():
    return pa.table(
        {
            "id": pa.array([1, 2], type=pa.int64()),
            "note": pa.array(["a|b", "line1\nline2"]),
            "amount": pa.array([-5.0, 7.5]),
        }
    )


def test_markdown_escapes_pipes_and_newlines():
    out = server._arrow_to_markdown(_table(), max_rows=None)
    # the pipe inside the VALUE is escaped, so the row keeps 3 cells
    assert "a\\|b" in out
    assert "line1<br>line2" in out
    data_rows = [ln for ln in out.splitlines() if ln.startswith("|") and "---" not in ln][1:]
    for row in data_rows:
        # count unescaped pipes: exactly the 4 delimiters of a 3-column table
        unescaped = row.replace("\\|", "").count("|")
        assert unescaped == 4, f"cell content shifted columns: {row!r}"


def test_csv_neutralizes_formula_prefixes():
    out = server._arrow_to_output(
        pa.table({"name": pa.array(["=cmd|calc", "+1", "@x", "-3", "plain"]), "n": pa.array([1, 2, 3, 4, 5])}),
        max_rows=None,
        fmt="csv",
    )
    assert "'=cmd|calc" in out
    assert "'+1" in out
    assert "'@x" in out
    assert "'-3" in out  # a TEXT cell starting with '-' is neutralized
    assert "plain" in out


def test_csv_leaves_numeric_negatives_alone():
    out = server._arrow_to_output(pa.table({"n": pa.array([-3.5, 2.0])}), max_rows=None, fmt="csv")
    assert "-3.5" in out and "'-3.5" not in out


def test_timestamp_tables_take_the_pandas_path():
    """A parity break found while wiring fastrender (2026-11-12): its naive-
    timestamp formatter renders a clean midnight as "1970-01-01" while the
    pandas/numpy path renders "1970-01-01T00:00:00.000000". Timestamp tables
    therefore bypass the fast renderer; this test pins the exclusion so a
    future fix is deliberate."""
    import pyarrow as pa

    from sqlhandler import server

    t = pa.table({"t": pa.array([0, 86400], type=pa.timestamp("us"))})
    assert server._has_timestamp_column(t) is True
    assert server._arrow_to_markdown(t, max_rows=None) == t.to_pandas().to_markdown(index=False)
    ints = pa.table({"a": pa.array([1, 2], type=pa.int64())})
    assert server._has_timestamp_column(ints) is False
