"""fastrender contract tests: pure-Arrow markdown + orjson JSON.

The renderer's safety contract — pandas-identical output for every supported
type, pandas fallback (None) for everything else — is pinned here against the
REAL pandas/tabulate stack (the ground truth), not against snapshots. If a
pandas/tabulate upgrade changes rendering, these tests fail loudly and the
fallback contract (fastrender returns None → caller renders via pandas)
absorbs the change until the renderer is re-proven.

House style: follow tests/test_l2cache.py (plain pytest, monkeypatch, no
fixtures beyond what conftest provides).
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from sqlhandler.fastrender import (
    _orjson,
    arrow_to_markdown_fast,
    dumps,
    loads,
    profile_to_markdown,
)

PANDAS_GROUND_TRUTH = pytest.importorskip("pandas")
pd = PANDAS_GROUND_TRUTH


def _pandas_md(tbl) -> str:
    return tbl.to_pandas().to_markdown(index=False)


def _assert_pandas_identical(tbl):
    """The core contract: pure-Arrow output is byte-identical to pandas."""
    got = arrow_to_markdown_fast(tbl)
    assert got is not None, f"renderer refused a supported table: {tbl.schema}"
    assert got == _pandas_md(tbl)


# ---------------------------------------------------------------------------
# per-type parity (the contract, one case each — plus the null variants)
# ---------------------------------------------------------------------------


def test_int_column_with_null_pandas_identical():
    # pandas 3.x upcasts nullable ints to float64 → %g rendering ('1', 'nan').
    _assert_pandas_identical(pa.table({"a": pa.array([1, None, 3], pa.int64())}))


def test_int_column_without_null_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([1, 2, 3], pa.int64())}))


def test_huge_int64_with_null_renders_like_pandas_float_upcast():
    # The nullable-int upcast visible at the extreme: pandas shows 4.61169e+18.
    tbl = pa.table({"a": pa.array([4611686018427387904, None], pa.int64())})
    got = arrow_to_markdown_fast(tbl)
    assert got is not None
    assert "4.61169e+18" in got  # %g semantics — NOT the full digits
    assert got == _pandas_md(tbl)


def test_float_column_pandas_identical():
    _assert_pandas_identical(
        pa.table(
            {
                "a": pa.array(
                    [1.5, None, float("nan"), 1e16, 0.1, 123456789.123],
                    pa.float64(),
                )
            }
        )
    )


def test_float32_and_float16_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([1.5, 2.25, None], pa.float32())}))
    _assert_pandas_identical(pa.table({"a": pa.array([1.5, 2.25, None], pa.float16())}))


def test_int_widths_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([-128, 127, None], pa.int8())}))
    _assert_pandas_identical(pa.table({"a": pa.array([1, None, 200], pa.uint8())}))


def test_bool_all_bool_table_renders_onezero():
    # pandas .values homogenization: an all-bool, null-free frame keeps a bool
    # numpy matrix → numpy bools → tabulate types them float → 1/0, decimal.
    _assert_pandas_identical(pa.table({"a": pa.array([True, False, True], pa.bool_())}))
    _assert_pandas_identical(pa.table({"b1": [True, False], "b2": [False, True]}))


def test_bool_with_null_or_mixed_renders_truefalse():
    # Any null anywhere (or any non-bool column) → object matrix → True/False.
    _assert_pandas_identical(pa.table({"a": pa.array([True, None, False], pa.bool_())}))
    _assert_pandas_identical(pa.table({"b": [True, False], "s": ["x", "y"]}))
    _assert_pandas_identical(pa.table({"b1": [True, False], "b2": [False, None]}))


def test_string_column_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array(["x", None, "y|z", "ünïcode"], pa.string())}))


def test_string_numparse_numeric_column_gets_decimal_alignment():
    # tabulate numparse: all-numeric string columns align like numbers.
    _assert_pandas_identical(pa.table({"a": pa.array(["1.5", "2", "10.25"], pa.string())}))


def test_all_null_string_column_pandas_identical():
    # 'nan' cells parse as numeric in tabulate → float typing → decimal align.
    _assert_pandas_identical(pa.table({"a": pa.array([None, None], pa.string())}))


def test_date32_date64_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([1, None, 19000], pa.date32())}))
    _assert_pandas_identical(pa.table({"a": pa.array([86400000, None], pa.date64())}))


def test_tz_aware_timestamp_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([1704067200, None, 1719792000], pa.timestamp("s", tz="UTC"))}))
    _assert_pandas_identical(pa.table({"a": pa.array([1704067200123456789, None], pa.timestamp("ns", tz="+05:30"))}))


def test_time32_time64_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([3600, None], pa.time32("s"))}))
    _assert_pandas_identical(pa.table({"a": pa.array([3661000000, None], pa.time64("us"))}))


def test_arrow_null_type_pandas_identical():
    _assert_pandas_identical(pa.table({"a": pa.array([None, None])}))


def test_empty_tables_pandas_identical():
    for t in (
        pa.table({"a": pa.array([], pa.int64())}),
        pa.table({"a": pa.array([], pa.string())}),
        pa.table({"a": pa.array([], pa.float64())}),
        pa.table({"a": pa.array([], pa.bool_())}),
        pa.table({"a": pa.array([], pa.date32())}),
    ):
        _assert_pandas_identical(t)  # no-colon separator, left header


def test_mixed_table_pandas_identical():
    _assert_pandas_identical(
        pa.table(
            {
                "i": [1, None, -5, 4611686018427387904],
                "f": [1.5, None, float("nan"), 2.25],
                "s": ["a", None, "b|c", "x"],
                "b": [True, None, False, True],
                "d": pa.array([1, None, 30, 400], pa.date32()),
            }
        )
    )


def test_wide_table_pandas_identical():
    _assert_pandas_identical(pa.table({f"c{i}": [i, None] for i in range(30)}))


def test_chunked_column_pandas_identical():
    chunked = pa.concat_tables(
        [pa.table({"a": pa.array(range(10), pa.int64())}), pa.table({"a": pa.array(range(10, 20), pa.int64())})]
    )
    _assert_pandas_identical(chunked)


def test_max_rows_slice_pandas_identical():
    tbl = pa.table({"a": pa.array(range(50), pa.int64()), "b": [f"v{i}" for i in range(50)]})
    got = arrow_to_markdown_fast(tbl, max_rows=10)
    assert got is not None
    assert got == tbl.slice(0, 10).to_pandas().to_markdown(index=False)


# ---------------------------------------------------------------------------
# the deliberate divergence: naive timestamps
# ---------------------------------------------------------------------------


def test_naive_timestamp_renders_iso_not_scinotation():
    # pandas 3.x + tabulate mangles naive timestamps as epoch floats
    # (1.70407e+18). The pure renderer renders ISO instead — pinned as an
    # improvement; this test FAILS if someone reintroduces the pandas path
    # for these columns silently.
    tbl = pa.table({"a": pa.array([1704067200], pa.timestamp("s"))})
    got = arrow_to_markdown_fast(tbl)
    assert got is not None
    assert "2024-01-01" in got
    assert "1.70407e+18" not in got
    got_ts = arrow_to_markdown_fast(pa.table({"a": pa.array([1704067200, None], pa.timestamp("s"))}))
    assert got_ts is not None
    assert "NaT" in got_ts


# ---------------------------------------------------------------------------
# refusals → the pandas fallback contract
# ---------------------------------------------------------------------------


def test_unsupported_types_return_none():
    for tbl in (
        pa.table({"a": pa.array([("x", 1)], pa.struct([("x", pa.string()), ("y", pa.int64())]))}),
        pa.table({"a": pa.array([[1, 2]], pa.list_(pa.int64()))}),
        pa.table({"a": pa.array([None], pa.decimal128(10, 2))}),
        pa.table({"a": pa.array([b"xy"], pa.binary())}),
    ):
        assert arrow_to_markdown_fast(tbl) is None, tbl.schema


def test_multiline_cell_returns_none():
    assert arrow_to_markdown_fast(pa.table({"a": ["line1\nline2"]})) is None
    assert arrow_to_markdown_fast(pa.table({"a": ["r\r1"]})) is None


def test_tabulate_internals_missing_returns_none(monkeypatch):
    # The tabulate-parity layer is guarded: if upstream renamed its private
    # helpers, the renderer must refuse (pandas path), never render
    # differently.
    import sqlhandler.fastrender as fr

    monkeypatch.setattr(fr, "_tab_afterpoint", None)
    assert arrow_to_markdown_fast(pa.table({"a": [1]})) is None


# ---------------------------------------------------------------------------
# randomized differential fuzz (bounded, seeded — CI-stable)
# ---------------------------------------------------------------------------


def test_fuzz_pandas_identical_supported_types():
    random.seed(20260928)  # deterministic; a failure prints the seed + schema
    checked = 0
    for _ in range(200):
        ncol, nrow = random.randint(1, 4), random.randint(0, 12)
        # Column values of every kind share this dict; the per-branch
        # literals are heterogeneous (int/None, float/None, str/None), so
        # the value type is widened to object rather than annotated per kind.
        data: dict[str, object] = {}
        for c in range(ncol):
            kind = random.choice(["int", "float", "str", "bool", "date", "tstz", "null"])
            if kind == "int":
                data[f"c{c}"] = [random.choice([None, random.randint(-(10**5), 10**5)]) for _ in range(nrow)]
            elif kind == "float":
                data[f"c{c}"] = [
                    random.choice([None, float("nan"), random.uniform(-1e6, 1e6), 1e-7, 1e16]) for _ in range(nrow)
                ]
            elif kind == "str":
                data[f"c{c}"] = [random.choice([None, "a", "1.5", "42", "x|y", "ü"]) for _ in range(nrow)]
            elif kind == "bool":
                data[f"c{c}"] = [random.choice([None, True, False]) for _ in range(nrow)]
            elif kind == "date":
                data[f"c{c}"] = pa.array(
                    [random.choice([None, random.randint(0, 20000)]) for _ in range(nrow)], pa.date32()
                )
            elif kind == "tstz":
                data[f"c{c}"] = pa.array(
                    [random.choice([None, random.randint(0, 2**33)]) for _ in range(nrow)],
                    pa.timestamp("s", tz="UTC"),
                )
            elif kind == "null":
                data[f"c{c}"] = pa.array([None] * nrow)
        tbl = pa.table({k: (v if isinstance(v, pa.Array) else pa.array(v)) for k, v in data.items()})
        got = arrow_to_markdown_fast(tbl)
        if got is None:
            # refusals are allowed only for genuinely unsupported shapes;
            # the fuzz vocabulary is entirely within the contract
            pytest.fail(f"renderer refused a fuzz-supported table: {tbl.schema}")
        assert got == _pandas_md(tbl), tbl.schema
        checked += 1
    assert checked == 200


# ---------------------------------------------------------------------------
# orjson layer
# ---------------------------------------------------------------------------


def test_dumps_uses_orjson_when_available():
    if _orjson is None:
        pytest.skip("orjson not installed — stdlib fallback is the shipped behavior")
    assert dumps({"a": 1}) == '{"a":1}'  # compact separators = orjson signature


def test_dumps_stdlib_fallback_parse_identical(monkeypatch):
    import sqlhandler.fastrender as fr

    monkeypatch.setattr(fr, "_orjson", None)
    payload = {"a": 1, "b": [1, 2, 3], "c": None}
    assert json.loads(dumps(payload)) == json.loads(json.dumps(payload))


def test_dumps_decimal_default_and_roundtrip():
    payload = {"amount": Decimal("12.34")}
    assert json.loads(dumps(payload, default=str)) == {"amount": "12.34"}


def test_dumps_indent_stays_stdlib_pretty():
    out = dumps({"a": 1}, indent=True)
    assert "\n" in out  # pretty-printed; orjson has no indent option


def test_loads_roundtrip():
    assert loads(dumps({"k": [1, 2.5, None, True]})) == {"k": [1, 2.5, None, True]}


# ---------------------------------------------------------------------------
# the renderer must not import pandas (the whole point)
# ---------------------------------------------------------------------------


def test_fastrender_never_imports_pandas_directly():
    """fastrender itself must not import pandas.

    The package's ``__init__`` legitimately pulls engine (which imports pandas),
    so ``import sqlhandler.fastrender`` in-process always sees pandas in
    sys.modules — that says nothing about fastrender. What the contract
    REQUIRES is that the fastrender MODULE adds no pandas import of its own
    and never touches the pandas API. Verified two ways, both in a fresh
    interpreter:

    1. subprocess: load the module FILE via importlib (bypassing the package
       init) — pandas must stay out of sys.modules.
    2. AST: no pandas import statement and no ``.to_pandas()`` call in the
       module source (docstring mentions of the fallback path are prose).
    """
    import ast
    import inspect
    import os
    import subprocess
    import sys

    import sqlhandler.fastrender as fr

    # 1. fresh subprocess, module file loaded standalone (no package init)
    code = (
        "import sys, importlib.util\n"
        "spec = importlib.util.spec_from_file_location('fastrender_standalone',\n"
        "    {src!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "assert 'pandas' not in sys.modules, 'fastrender pulled pandas in'\n"
    ).format(src=os.path.join("src", "sqlhandler", "fastrender.py"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"standalone import pulled pandas: {r.stderr}"

    # 2. AST-level scan of the module source
    tree = ast.parse(inspect.getsource(fr))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            assert not any("pandas" in m for m in mods), f"pandas import at line {node.lineno}"
        if isinstance(node, ast.Attribute):
            assert node.attr != "to_pandas", f".to_pandas() call at line {node.lineno}"


# ---------------------------------------------------------------------------
# profile_to_markdown (the profile_table render path)
# ---------------------------------------------------------------------------


def _pandas_profile(rows: list[dict[str, Any]]):
    return pd.DataFrame(rows).to_markdown(index=False)


def test_profile_numeric_and_str_columns_pandas_identical():
    rows: list[dict[str, Any]] = [
        {"name": "id", "type": "BIGINT", "min": 1, "avg": 12.5, "q25": 5, "non_null": 100},
        {"name": "who", "type": "VARCHAR", "min": "alice", "avg": None, "q25": None, "non_null": 87},
    ]
    got = profile_to_markdown(rows)
    assert got is not None
    assert got == _pandas_profile(rows)


def test_profile_object_column_transforms_numeric_strings_like_pandas():
    # '10000000000.0' in a float-typed object column renders '1e+10' — the
    # tabulate float transform, faithfully reproduced.
    rows: list[dict[str, Any]] = [{"min": "10000000000.0"}, {"min": 1.5}, {"min": None}]
    got = profile_to_markdown(rows)
    assert got is not None
    assert "1e+10" in got
    assert got == _pandas_profile(rows)


def test_profile_object_column_verbatim_when_not_all_numeric():
    rows: list[dict[str, Any]] = [{"min": 1000}, {"min": "alice"}, {"min": 0.5}]
    got = profile_to_markdown(rows)
    assert got is not None
    assert "1000" in got and "alice" in got
    assert got == _pandas_profile(rows)


def test_profile_all_null_column_renders_empty_cells():
    # All-None column beside ONLY string columns: object dtype → '' cells.
    rows = [{"a": None, "b": "x"}, {"a": None, "b": "y"}]
    got = profile_to_markdown(rows)
    assert got is not None
    assert "nan" not in got  # object dtype: '' not 'nan'
    assert got == _pandas_profile(rows)


def test_profile_all_null_beside_numeric_falls_back():
    # The all-None + numeric promotion is row-shape-sensitive in pandas
    # (dict-constructor NaN-filling interacts with cross-column promote) —
    # outside the proven contract → pandas fallback.
    rows: list[dict[str, Any]] = [{"a": None}, {"a": None}, {"b": 1}]
    assert profile_to_markdown(rows) is None


def test_profile_null_free_int_column_exact_digits():
    rows = [{"count": 4611686018427387904}, {"count": 0}]
    got = profile_to_markdown(rows)
    assert got is not None
    assert "4611686018427387904" in got  # int64 exact, NOT 4.61169e+18
    assert got == _pandas_profile(rows)


def test_profile_bool_and_nested_values_fall_back():
    assert profile_to_markdown([{"a": True}, {"a": False}]) is None
    assert profile_to_markdown([{"a": [1]}]) is None


def test_profile_empty_rows_returns_none():
    assert profile_to_markdown([]) is None


def test_profile_realistic_summarize_shape_pandas_identical():
    real = []
    for i in range(50):
        src_type = ["BIGINT", "VARCHAR", "DOUBLE", "DATE", "BOOLEAN", "TIMESTAMP"][i % 6]
        real.append(
            {
                "column_name": f"col_{i}",
                "column_type": src_type,
                "min": {
                    "BIGINT": i * 1000,
                    "VARCHAR": f"a{i}",
                    "DOUBLE": i * 0.5,
                    "DATE": "2024-01-01",
                    "BOOLEAN": "false",
                    "TIMESTAMP": "2024-01-01 00:00:00",
                }[src_type],
                "max": {
                    "BIGINT": i * 99999,
                    "VARCHAR": f"z{i}",
                    "DOUBLE": i * 99.5,
                    "DATE": "2024-12-31",
                    "BOOLEAN": "true",
                    "TIMESTAMP": "2024-12-31 23:59:59",
                }[src_type],
                "approx_unique": i * 37,
                "null_percentage": round(i * 1.7, 1) if i % 3 else None,
                "avg": i * 1.5 if src_type in ("BIGINT", "DOUBLE") else None,
                "std": i * 0.25 if src_type in ("BIGINT", "DOUBLE") else None,
                "q25": i * 10 if src_type in ("BIGINT", "DOUBLE") else None,
                "q50": i * 20 if src_type in ("BIGINT", "DOUBLE") else None,
                "q75": i * 30 if src_type in ("BIGINT", "DOUBLE") else None,
                "count": 1000 - i,
            }
        )
    got = profile_to_markdown(real)
    assert got is not None
    assert got == _pandas_profile(real)


def test_profile_fuzz_pandas_identical():
    random.seed(3)
    vocab = [None, 0, 1, -5, 1.5, 12.5, 1e10, "a", "zed", "BIGINT", "", "1.5", 4611686018427387904, 1e-7]
    checked = 0
    for _ in range(300):
        nk = random.randint(1, 6)
        keys = [f"col{i}" for i in range(nk)]
        nr = random.randint(1, 8)
        rows = [{k: random.choice(vocab) for k in keys} for _ in range(nr)]
        want = _pandas_profile(rows)
        got = profile_to_markdown(rows)
        if got is None:
            continue  # fallback = pandas output by construction
        assert got == want, rows
        checked += 1
    assert checked > 150  # the fuzz must exercise the pure path substantively
