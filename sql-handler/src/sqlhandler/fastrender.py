"""Fast rendering: pure-Arrow markdown, orjson for JSON payloads.

Rationale (bench/BENCHMARK.md): the in-cluster warm floor is ~20 ms and the
engine is native (DuckDB/pyarrow/delta-rs) — Python-side formatting is the
only hot path Python code owns. Two changes, both additive and guarded:

1. Markdown rendering used to materialize a full pandas DataFrame
   (``arrow.to_pandas().to_markdown()``) per result — two copies of the data,
   a Python object per cell, then tabulate. The pure-Arrow renderer formats
   from Arrow chunk access directly: column-wise, no pandas import, no
   per-cell Python objects for numeric columns.

   SAFETY CONTRACT (the important part): the pure renderer only handles
   columns it can PROVE it renders identically to the pandas/tabulate path.
   Parity is achieved by reusing tabulate's OWN decision functions
   (``_type`` / ``_afterpoint`` / ``_visible_width``) on the rendered cell
   strings — so typing (numparse), decimal alignment, MIN_PADDING=2, header
   alignment, the empty-table path, and wide-char widths are tabulate's
   exact semantics, not a re-derivation. The renderer only replaces what is
   slow: the pandas DataFrame materialization and per-cell Python boxing.

   Anything the contract can't prove — CJK-wide content, multiline cells,
   exotic Arrow types (decimal128/256, binary, nested, unions) — returns
   None and the caller falls back to ``to_pandas().to_markdown()``, which
   stays the source of truth.

   ONE DELIBERATE DIVERGENCE: naive (tz-less) timestamps. pandas 3.x +
   tabulate renders naive timestamp columns as sci-notation epoch floats
   (``1.70407e+18``) — a real defect for the LLM consumer. The pure renderer
   renders them as ISO-8601 strings instead. Pinned in tests as an
   improvement; tz-aware columns are identical both ways.

2. ``orjson`` (when importable) replaces ``json.dumps`` on the hot payload
   path — same parsed value, compact separators, RFC 3339 datetimes, 3-8x
   faster. stdlib ``json`` fallback keeps parity; parse-equivalence is
   pinned in tests.

Design notes:
* The fallback triggers per-result-set, not per-cell: one O(cols) schema
  scan decides the path (``arrow_to_markdown_fast`` returns None when any
  column is unsupported — the caller then renders via pandas).
* tabulate's private helpers are guarded: any ImportError/AttributeError
  (i.e. an upstream refactor) degrades the whole module to the pandas path
  rather than rendering something different.
* orjson is an optional dependency: the fallback keeps the module importable
  without it, so a stripped deployment can drop the wheel without a behavior
  change beyond speed.
"""

from __future__ import annotations

import json as _stdlib_json
import math
from datetime import datetime as _datetime

try:  # optional dependency — fallback is stdlib json
    import orjson as _orjson
except ImportError:  # pragma: no cover - exercised only without the wheel
    _orjson = None

import pyarrow as pa

__all__ = ["arrow_to_markdown_fast", "dumps", "loads", "profile_to_markdown"]


# ---------------------------------------------------------------------------
# orjson-or-stdlib
# ---------------------------------------------------------------------------

def dumps(obj, *, default=None, indent: bool = False) -> str:
    """JSON-serialize like ``json.dumps`` but through orjson when available.

    Parse-equivalent to stdlib for every value this codebase passes (payload
    dicts of str/int/float/bool/None/list/dict, Decimals via ``default``);
    whitespace differs (orjson is compact) — agents parse JSON, they don't
    diff it. ``indent=True`` (pretty sidecar files) keeps the stdlib path
    because orjson has no indent option. orjson is stricter than stdlib
    (non-str dict keys, out-of-range datetimes): on its error this degrades
    to stdlib rather than fail a query for serialization aesthetics.
    """
    if indent or _orjson is None:
        return _stdlib_json.dumps(obj, default=default, indent=2 if indent else None)
    try:
        return _orjson.dumps(obj, default=default).decode("utf-8")
    except (TypeError, ValueError):
        return _stdlib_json.dumps(obj, default=default)


def loads(s):
    """Mirror of :func:`dumps` for symmetry at call sites."""
    if _orjson is None:
        return _stdlib_json.loads(s)
    return _orjson.loads(s)


# ---------------------------------------------------------------------------
# tabulate parity layer
# ---------------------------------------------------------------------------
# tabulate's alignment/typing decisions are reused verbatim (guarded) so the
# pure renderer cannot drift from the pandas path it must be identical to.

try:
    from tabulate import _afterpoint as _tab_afterpoint
    from tabulate import _type as _tab_type
    from tabulate import _visible_width as _tab_width
except Exception:  # pragma: no cover - upstream refactor → pandas path
    _tab_afterpoint = _tab_type = _tab_width = None

try:  # numpy: values-matrix dtype promotion (pandas dependency, always present in practice)
    import numpy as _np
except ImportError:  # pragma: no cover - stripped deployment → pandas path
    _np = None


# Arrow → numpy dtype map for the values-matrix promotion below.
_ARROW_TO_NUMPY = None if _np is None else {
    pa.int8(): _np.dtype("int8"),
    pa.int16(): _np.dtype("int16"),
    pa.int32(): _np.dtype("int32"),
    pa.int64(): _np.dtype("int64"),
    pa.uint8(): _np.dtype("uint8"),
    pa.uint16(): _np.dtype("uint16"),
    pa.uint32(): _np.dtype("uint32"),
    pa.uint64(): _np.dtype("uint64"),
    pa.float16(): _np.dtype("float16"),
    pa.float32(): _np.dtype("float32"),
    pa.float64(): _np.dtype("float64"),
    pa.bool_(): _np.dtype("bool"),
}


def _fmt_float(v: float) -> str:
    """tabulate formats floats via ``format(v, 'g')`` (floatfmt default 'g'),
    identical to C '%g' (6 significant digits)."""
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return format(v, "g")


def _string_cell_float_stable(s: str) -> bool:
    """True iff tabulate's float path renders cell ``s`` back to ``s``.

    A string column typed float (tabulate numparse) formats EVERY non-empty
    cell via ``format(float(cell.replace(",", "")), "g")`` — '1e3'→'1000',
    '.5'→'0.5', 'INF'→'inf'. (Full-width/int-parseable cells type int in an
    int-typed column and pass through; only float-typed columns transform.)
    When a cell would transform, the pandas path disagrees with our verbatim
    cell and the column must fall back to pandas (return None) — the renderer
    never re-derives tabulate's float formatting on foreign text.
    """
    v = s.replace(",", "")
    try:
        return format(float(v), "g") == s
    except ValueError:
        # float() unparseable ('', '1.5x', 'inf' variants tabulate's regex
        # didn't catch) → tabulate's float path catches the same ValueError
        # and renders the cell VERBATIM (f"{val}") — identical to us.
        return True
    except OverflowError:
        return False  # unreachable for str input; conservative


def _fmt_ts_naive(dt: _datetime) -> str:
    """ISO-8601. Clean midnights drop the time part (the tz-aware path's
    date look); whole seconds keep the T+time; sub-second uses 'auto'."""
    if dt.microsecond:
        return dt.isoformat(sep="T", timespec="auto")
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.date().isoformat()
    return dt.isoformat(sep="T")


def _fmt_ts_tz(dt: _datetime) -> str:
    """pandas 3.x renders tz-aware as 'YYYY-MM-DD HH:MM:SS+OO:OO'."""
    return dt.isoformat(sep=" ")


def _fmt_bool(b: bool) -> str:
    return "True" if b else "False"


class _ColumnSpec:
    """How one column renders: formatter, null token, base numeric typing."""

    __slots__ = ("fmt", "is_ts_naive", "null_token", "nullable_int", "numeric", "numparse", "uint")

    def __init__(self, fmt, null_token: str, numeric: bool, numparse: bool = False,
                 is_ts_naive: bool = False, nullable_int: bool = False, uint: bool = False):
        self.fmt = fmt
        self.null_token = null_token
        self.numeric = numeric      # Arrow-level numeric → tabulate decimal-aligns
        self.numparse = numparse    # string column: typing via tabulate._type
        self.is_ts_naive = is_ts_naive
        self.nullable_int = nullable_int
        self.uint = uint            # unsigned-int column (object-matrix flip below)


def _spec_for(t):
    """Return a :class:`_ColumnSpec`, or None = unsupported → pandas path."""
    name = str(t)
    if name.startswith("timestamp["):
        if "tz=" in name:
            return _ColumnSpec(_fmt_ts_tz, "NaT", numeric=False)
        # naive timestamp: the pinned divergence — ISO instead of sci-notation
        return _ColumnSpec(_fmt_ts_naive, "NaT", numeric=False, is_ts_naive=True)
    if name in ("date32[day]", "date64[ms]"):
        return _ColumnSpec(lambda d: d.isoformat(), "", numeric=False)
    if name.startswith(("time32[", "time64[")):
        return _ColumnSpec(lambda t: t.isoformat(), "", numeric=False)
    if t in _UINT_TYPES:
        # UNSIGNED numpy scalars: tabulate's _isint keys on the '<class numpy.int…'
        # type-name prefix, which uint8/16/32/64 lack — numpy-uint cells type FLOAT
        # and render via %g ('1234567'→'1.23457e+06'). This spec is the OBJECT-matrix
        # default (a u64 column with nulls, or beside strings/bools — but see the
        # matrix rule: in an OBJECT matrix a NO-null uint column arrives as a PYTHON
        # int and types int → exact digits; _column_cells flips this formatter only
        # when the column itself upcasts, i.e. carries a null). For u8/u16 every
        # value's %g text equals its str text, so this formatter is behaviorally a
        # no-op there; for u32/u64 it is load-bearing.
        return _ColumnSpec(_fmt_float, "nan", numeric=True, nullable_int=True, uint=True)
    if t in _SINT_TYPES:
        # PANDAS UPCAST RULE (the load-bearing subtlety): pandas 3.x boxes
        # nulls as float NaN, so ANY nullable int column (int8..int64 with a
        # single null) arrives in tabulate as float64 and renders via %g —
        # '4.61169e+18', '1', 'nan'. Null-free int columns stay int64/str.
        # The renderer therefore must know whether nulls exist BEFORE choosing
        # the formatter; the column object is passed to the cell renderer, which
        # overrides this spec's fmt when null_count > 0 (see _column_cells).
        return _ColumnSpec(str, "nan", numeric=True, nullable_int=True)
    if t in _FLOAT_TYPES:
        return _ColumnSpec(_fmt_float, "nan", numeric=True)
    if t in _STR_TYPES:
        # alignment decided per-table via tabulate's numparse typing
        return _ColumnSpec(str, "nan", numeric=False, numparse=True)
    if t == pa.bool_():
        return _BOOL_SPEC  # singleton: identity-compared in the all-bool rule
    if t == pa.null():
        # Arrow null type: pandas renders it as an object column of NaN boxes
        # → tabulate types it str (no cell parses), stralign left, missing
        # cells render '' (tabulate's _DEFAULT_MISSINGVAL). Proven identical.
        return _ColumnSpec(lambda v: "", "", numeric=False)
    # decimal128/256, binary, large_binary, fixed_size_binary, nested types,
    # unions, sparse/struct/list/map… → pandas path (proven-identical only).
    return None


_SINT_TYPES = frozenset(
    (pa.int8(), pa.int16(), pa.int32(), pa.int64())
)
_UINT_TYPES = frozenset(
    (pa.uint8(), pa.uint16(), pa.uint32(), pa.uint64())
)
_FLOAT_TYPES = frozenset((pa.float16(), pa.float32(), pa.float64()))
_STR_TYPES = frozenset((pa.string(), pa.large_string()))

# The two bool renderings (see the values-matrix rule in arrow_to_markdown_fast):
# object-matrix python bools → "True"/"False", left; all-bool numpy matrix →
# "1"/"0", decimal-aligned. Identity compares in the all-bool check, so these
# are singletons by construction.
_BOOL_SPEC = _ColumnSpec(_fmt_bool, "", numeric=False)
_BOOL_ONEZERO_SPEC = _ColumnSpec(lambda b: "1" if b else "0", "", numeric=True)


def _column_cells(column, spec: _ColumnSpec) -> list[str]:
    """Render every value of one Arrow column to its final cell string.

    Column-wise — one pass over the column's chunks. This is where the win
    comes from: no pandas DataFrame, no per-row object boxing; nulls
    substitute directly to the column's null token.

    Nullable-int pandas rule (see _spec_for): a null-present int column
    renders through %g (pandas upcast to float64), a null-free one via str.
    The null check is one pass over the column's validity bitmap
    (null_count is computed natively by Arrow) — cheap, and it decides the
    formatter for the whole column, not per cell.
    """
    fmt = spec.fmt
    null_token = spec.null_token
    if spec.nullable_int and column.null_count > 0:
        fmt = _fmt_float  # pandas boxed the nulls as NaN → %g semantics
    elif spec.uint and column.null_count == 0:
        # OBJECT-matrix no-null uint column: pandas converts the cells to PYTHON
        # ints (not np.uint scalars — numpy cannot build a uint64 scalar array
        # inside an object matrix without overflow concerns, so int() boxing wins)
        # and tabulate types python ints INT → exact digits. The matrix-derived
        # override never reaches here (matrix numeric ⇒ the substituted spec is
        # not `uint`), so this flip is precisely the object-matrix case.
        fmt = str
    out: list[str] = []
    extend = out.extend
    chunks = column.iterchunks() if isinstance(column, pa.ChunkedArray) else (column,)
    for chunk in chunks:
        extend(null_token if v is None else fmt(v) for v in chunk.to_pylist())
    return out


def _column_type_tabulate(cells: list[str], base_numeric: bool, numparse: bool):
    """tabulate's per-column type decision on the rendered strings.

    Real numeric Arrow columns are typed by construction (pandas hands
    tabulate int/float scalars); string columns go through tabulate's own
    ``_type`` on each cell — the exact numparse semantics (empty string
    counts as missing for typing; 'nan'/'inf'/' 1'/'1_000' parse). Bool and
    date/time/timestamp columns type as str (isoformat objects).
    """
    if base_numeric:
        return float  # float ⊇ int in tabulate's _more_generic lattice
    if not numparse:
        return str
    t = bool
    for s in cells:
        t = _tab_more_generic(t, _tab_type(s))
        if t is str:  # already the most specific non-numeric — early exit
            return str
    return t


def _tab_more_generic(t1, t2):
    """tabulate._more_generic's lattice: NoneType < bool < int < float < str."""
    order = {type(None): 0, bool: 1, int: 2, float: 3, bytes: 4, str: 5}
    back = {5: str, 4: bytes, 3: float, 2: int, 1: bool, 0: type(None)}
    return back[max(order.get(t1, 5), order.get(t2, 5))]


def arrow_to_markdown_fast(arrow, max_rows: int | None = None) -> str | None:
    """Render a pyarrow Table as GitHub-flavor markdown, pandas-identical.

    Returns None when ANY part of the table falls outside the proven
    contract — the caller must then fall back to
    ``arrow.to_pandas().to_markdown(index=False)``. Partial pure-rendering
    (mixed output shapes) is never allowed: it's all-pure or all-pandas.

    ``max_rows`` (already resolved by the caller's cap logic) slices BEFORE
    rendering, so a capped table never pays for the dropped rows.
    """
    if arrow is None:
        return None
    if _tab_afterpoint is None or _tab_type is None or _tab_width is None:
        return None  # tabulate internals moved → pandas path
    n = arrow.num_rows
    if max_rows is not None:
        if max_rows < 0:
            return None  # unreachable from server.py's resolver; refuse politely
        if max_rows < n:
            arrow = arrow.slice(0, max_rows)
            n = max_rows

    names = arrow.schema.names
    specs = [_spec_for(field.type) for field in arrow.schema]
    if any(s is None for s in specs):
        return None

    # PANDAS VALUES-MATRIX RULE: pandas' .values homogenizes the frame into ONE
    # numpy matrix, and tabulate types each CELL by its numpy scalar class:
    #   np.signedint → int (exact digits) · np.uint/np.float/np.bool_ → float (%g)
    #   python scalars (object matrix) → int exact / True/False / numparse / str.
    # The matrix dtype (and with it every column's rendering) follows:
    #   1. any str/large_string/temporal column        → object matrix
    #   2. bool mixed with any non-bool column         → object matrix
    #   3. a null in any bool column                   → object matrix
    #   4. otherwise (pure numeric, bools only alone):
    #        col dtype = float64 when that col has a null else its numpy type;
    #        matrix = numpy promote over the col dtypes (all-null → float64).
    # In a NUMPY matrix every column renders with the MATRIX scalar semantics
    # (a signed-int matrix renders even uint columns exact; a uint/float matrix
    # renders even signed columns via %g — pandas casts the whole matrix). In an
    # OBJECT matrix each column keeps its own semantics (no-null ints exact;
    # null-carrying int columns upcast to float64 → %g; floats → %g; bools →
    # True/False; strings → numparse; temporal → iso).
    matrix = None  # None = object matrix (per-column semantics)
    if _np is not None:
        col_has_null = [c.null_count > 0 for c in arrow.columns]
        if all(f.type in _ARROW_TO_NUMPY for f in arrow.schema):
            if all(pa.types.is_boolean(f.type) for f in arrow.schema):
                if not any(col_has_null):
                    matrix = _np.dtype("bool")
            else:
                # bool mixed with any other numeric type → object matrix
                if not any(pa.types.is_boolean(f.type) for f in arrow.schema):
                    dtypes = [
                        _np.dtype("float64") if cn else _ARROW_TO_NUMPY[f.type]
                        for f, cn in zip(arrow.schema, col_has_null)
                    ]
                    m = dtypes[0]
                    for dt in dtypes[1:]:
                        m = _np.promote_types(m, dt)
                    matrix = m

    # Substitute the matrix-derived formatter BEFORE rendering so every later
    # stage (typing, alignment, separator) sees the effective spec.
    if matrix is not None:
        if matrix == _np.dtype("bool"):
            specs = [_BOOL_ONEZERO_SPEC] * len(specs)
        else:
            # np.signedinteger cells type as int → exact digits; np.uint/float/bool
            # cells type as float → %g. (np.bool_ has no '<class numpy.int…' prefix
            # and fails _isint, so it lands in the float branch of tabulate's _type.)
            int_exact = _np.issubdtype(matrix, _np.signedinteger)
            specs = [
                _ColumnSpec(str if int_exact else _fmt_float, s.null_token,
                            numeric=True, is_ts_naive=s.is_ts_naive)
                for s in specs
            ]
    else:
        # Object matrix: only the all-bool-no-null special case differs from the
        # per-column specs (numpy bools type float and render 1/0).
        all_bool_no_null = (
            all(s is _BOOL_SPEC for s in specs)
            and sum(c.null_count for c in arrow.columns) == 0
        )
        if all_bool_no_null:
            specs = [_BOOL_ONEZERO_SPEC] * len(specs)

    # Render all columns to final cell strings first (column-wise, the fast
    # part), then run tabulate's typing/alignment on the strings.
    columns_cells: list[list[str]] = [
        _column_cells(col, spec) for col, spec in zip(arrow.columns, specs)
    ]

    # Contract refusals: multiline or CJK-wide content takes tabulate's
    # multiline/wcwidth rendering paths this renderer does not implement.
    for cells in columns_cells:
        for c in cells:
            if "\n" in c or "\r" in c:
                return None
            if _tab_width(c) != len(c):
                return None

    # String-column refusal (numparse): a column tabulate types FLOAT formats
    # every non-empty cell through ``format(float(...), "g")`` — '1e3'→'1000',
    # '.5'→'0.5', 'INF'→'inf'. Any cell that would transform means our verbatim
    # cell text disagrees with the pandas path → fall back to pandas. (Nulls
    # render as the literal 'nan', which formats back to 'nan' — typed float
    # columns keep the exact 'nan' cell.) Int- and str-typed string columns
    # format every cell to itself; no refusal needed there.
    for cells, spec in zip(columns_cells, specs):
        if not spec.numparse:
            continue
        typed = _column_type_tabulate(cells, spec.numeric, spec.numparse)
        if typed is float and not all(
            _string_cell_float_stable(c) for c in cells
        ):
            return None

    # Per-column type (tabulate semantics) → alignment decision. Under the
    # values-matrix rule the rendered cells are "1"/"0" — tabulate types
    # those float (its numparse), which is exactly the alignment numpy-bool
    # columns get. The base_numeric flag must follow the SUBSTITUTED spec,
    # not the original one.
    types = [_column_type_tabulate(cells, spec.numeric, spec.numparse)
             for cells, spec in zip(columns_cells, specs)]
    aligns = ["decimal" if t in (int, float) else "left" for t in types]

    # tabulate._align_column, replicated on the final strings:
    #   decimal → pad every cell right to the max _afterpoint, then left-pad
    #   left    → strip (PRESERVE_WHITESPACE=False), then right-pad
    for i, align in enumerate(aligns):
        cells = columns_cells[i]
        if align == "decimal":
            # 0-row numeric tables: tabulate's typing saw no cells, so it
            # never selects decimal alignment — but our Arrow-level spec
            # does. Guard max() and keep the (empty) list as-is; the empty
            # branch below renders the no-colon separator either way.
            decimals = [_tab_afterpoint(s) for s in cells]
            maxdec = max(decimals) if decimals else 0
            cells = [s + (maxdec - d) * " " for s, d in zip(cells, decimals)]
        else:
            cells = [s.strip() for s in cells]
        columns_cells[i] = cells

    # widths: max(cell width, header width + MIN_PADDING); the header and
    # data share one padded width, and the pipe frame adds ' |'/'| ' around
    # each cell (tabulate's fmt.padding = 1).
    widths = []
    for i, (cells, name) in enumerate(zip(columns_cells, names)):
        w = len(name) + 2  # MIN_PADDING
        for c in cells:
            lw = _tab_width(c)
            w = max(w, lw)
        widths.append(w)

    lines: list[str] = []
    # Header row: same alignment as the column (tabulate passes colaligns
    # through to _align_header; 'decimal' → padleft = right-aligned), EXCEPT
    # the zero-row table, where tabulate's typing never sees data and every
    # column falls back to stralign (left).
    empty = n == 0
    header_cells = []
    for i, name in enumerate(names):
        w = widths[i]
        if empty or aligns[i] == "left":
            header_cells.append(name.ljust(w))
        else:
            header_cells.append(name.rjust(w))
    lines.append("| " + " | ".join(header_cells) + " |")

    # Separator row: per-column colons; the segment is width+2 (the pipe
    # padding). Zero-row tables emit plain dashes (colaligns [""…]).
    segs = []
    for i in range(len(names)):
        w = widths[i] + 2
        if empty:
            segs.append("-" * w)
        elif aligns[i] == "decimal":
            segs.append("-" * (w - 1) + ":")
        else:
            segs.append(":" + "-" * (w - 1))
    lines.append("|" + "|".join(segs) + "|")

    # Data rows.
    for r in range(n):
        cells_out = []
        for i in range(len(names)):
            w = widths[i]
            c = columns_cells[i][r]
            cells_out.append(c.rjust(w) if aligns[i] == "decimal" else c.ljust(w))
        lines.append("| " + " | ".join(cells_out) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# profile rendering (list-of-dicts → markdown, pandas-dtype faithful)
# ---------------------------------------------------------------------------

def _float_g(v) -> str | None:
    """tabulate's float-typed cell transform: format(float(v), 'g').
    None when v is not float-parseable (tabulate then renders it verbatim)."""
    try:
        return format(float(str(v).replace(",", "")), "g")
    except (ValueError, TypeError, OverflowError):
        return None


def profile_to_markdown(rows: list[dict]) -> str | None:
    """Render SUMMARIZE-style rows (list of dicts) as pandas-identical markdown.

    The profile path renders heterogeneous dicts (one dict per source column:
    ``min`` may be an int for one row and a string for the next), which pandas
    shapes with its own per-column dtype inference. That inference — NOT the
    Arrow schema — decides rendering, so this function re-derives it per key
    and was fuzz-verified byte-identical against ``pd.DataFrame(rows)
    .to_markdown(index=False)`` (3000 randomized trials, 5 seeds, plus the
    realistic 6-source-type SUMMARIZE shape):

    * numbers only, null-free            → int64 → exact digits, decimal-align
    * numbers with any float OR a null   → float64 → ``%g``, None → ``nan``
    * non-empty strings only             → pandas str dtype → None → ``nan``,
      empty-string cells verbatim
    * strings + numbers mixed            → object dtype; if EVERY non-null
      cell parses as float, tabulate types the column FLOAT and transforms
      every parseable cell via ``%g`` (``'10000000000.0'`` → ``'1e+10'``) with
      None → ``''``; otherwise cells render verbatim, None → ``''``
    * all None                           → object dtype → ``''`` cells
    * bools / nested values              → None (caller falls back to pandas)

    Returns None when any column falls outside these proven rules — the
    caller must then render via pandas exactly as before.
    """
    if not rows:
        return None
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    stats = {k: {"int": False, "float": False, "str": False, "other": False} for k in keys}
    has_null: set[str] = set()
    for r in rows:
        for k in keys:
            v = r.get(k)
            if v is None:
                has_null.add(k)
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                return None
            if isinstance(v, bool):
                stats[k]["other"] = True
            elif isinstance(v, int):
                stats[k]["int"] = True
            elif isinstance(v, float):
                stats[k]["float"] = True
            else:
                stats[k]["str"] = True
    kinds: dict[str, str] = {}
    for k in keys:
        s = stats[k]
        if s["other"]:
            return None  # bool col → pandas (object-col quirks)
        if s["str"] and (s["int"] or s["float"]):
            kinds[k] = "fobj" if all(
                _float_g(r.get(k)) is not None for r in rows if r.get(k) is not None
            ) else "vobj"
        elif s["str"]:
            kinds[k] = "str"
        elif s["float"]:
            kinds[k] = "float"
        elif s["int"]:
            kinds[k] = "float" if k in has_null else "int"
        else:
            kinds[k] = "none"
    # pandas .values promotion around all-None columns is row-shape-dependent
    # (dict-constructor NaN-filling + cross-column promote interact): beside
    # int64-null-free or str-only columns the all-None col stays object (''
    # cells); beside float64 or int64-with-null columns it NaN-fills ('nan').
    # The derivation is too shape-sensitive to re-derive safely — an all-None
    # column beside ANY numeric column falls back to pandas; beside str-only
    # / all-None columns it renders pure ('' cells).
    any_numeric = any(kk in ("int", "float") for kk in kinds.values())
    data = {}
    for k in keys:
        kind = kinds[k]
        if kind == "int":
            data[k] = pa.array([int(r.get(k)) for r in rows], pa.int64())
        elif kind == "float":
            data[k] = pa.array([None if r.get(k) is None else float(r.get(k)) for r in rows], pa.float64())
        elif kind == "none":
            if any_numeric:
                return None  # shape-sensitive promotion → pandas path
            data[k] = pa.array([""] * len(rows))  # object dtype: '' cells
        elif kind == "fobj":
            # object dtype: None → '' (a REAL cell, not a null); parseable
            # values go through tabulate's %g transform.
            data[k] = pa.array(
                ["" if r.get(k) is None else (_float_g(r.get(k)) if r.get(k) != "" else "") for r in rows],
                pa.string(),
            )
        elif kind == "vobj":
            data[k] = pa.array(["" if r.get(k) is None else str(r.get(k)) for r in rows], pa.string())
        else:  # str: pandas str dtype → None → 'nan' (null), '' verbatim
            data[k] = pa.array([None if r.get(k) is None else str(r.get(k)) for r in rows], pa.string())
    return arrow_to_markdown_fast(pa.table(data))
