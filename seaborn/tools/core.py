"""Core tools for the statistical-visualization MCP server (v0.1).

Three tools total (was 17): plot, describe, health_check.

Design rules:
- Data arrives by reference (sql via sqlhandler / data_url) or inline-tiny;
  the model never re-transmits large row payloads.
- Responses are COMPACT: stats + chart_json + small sample + optional PNG.
  Interactive HTML is opt-in (include_html=True) for UI callers — it is
  useless to an LLM and was ~95% of the old payload.
"""

import base64
import io
import json
from typing import Any

import pandas as pd

from mcp_instance import mcp
from schema import MAX_PNG_BYTES, DescribeRequest, PlotKind, PlotRequest
from utils import (
    _basic_stats,
    _describe_numeric,
    _jsonable_records,
    _profile_dataframe,
    _validate_columns,
)
from utils.fetch import load_rows
from renderers import (
    _render_plotly_html,
    _render_seaborn_html_mpld3,
)
from renderers.seaborn import _draw_seaborn_plot, _prepare_pie_data


def _to_json_string(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(message: str, **extra: Any) -> str:
    return _to_json_string({"status": "error", "error": message, **extra})


def _infer_vegalite_type(df: pd.DataFrame, col: str) -> str:
    if pd.api.types.is_numeric_dtype(df[col]):
        return "quantitative"
    if pd.api.types.is_datetime64_any_dtype(df[col]):
        return "temporal"
    if pd.api.types.is_bool_dtype(df[col]):
        return "nominal"
    return "nominal"


def _to_chart_json(req: PlotRequest, df: pd.DataFrame) -> dict[str, Any]:
    encoding: dict[str, Any] = {}
    for role, key in (("x", req.x), ("y", req.y), ("color", req.hue), ("size", req.size)):
        if key and key in df.columns:
            encoding[role] = {"field": key, "type": _infer_vegalite_type(df, key)}

    mark_by_kind = {
        "scatter": "point",
        "line": "line",
        "histogram": "bar",
        "kde": "area",
        "box": "boxplot",
        "violin": "density",
        "bar": "bar",
        "count": "bar",
        "regression": "point+line",
        "residual": "point",
        "correlation_heatmap": "rect",
        "pair": "matrix",
        "facet": "facet",
        "pie": "arc",
    }
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "description": req.title or f"{req.kind} chart",
        "mark": mark_by_kind.get(req.kind, "point"),
        "encoding": encoding,
        "metadata": {"source_library": "seaborn"},
    }


def _pairplot(df: pd.DataFrame, req: PlotRequest):
    import seaborn as sns

    return sns.pairplot(df, hue=req.hue if req.hue else None)


def _facet_grid(df: pd.DataFrame, req: PlotRequest, row: str | None, col: str | None):
    import seaborn as sns

    if req.kind in ("scatter", "line"):
        return sns.relplot(
            data=df, x=req.x, y=req.y, hue=req.hue, row=row, col=col,
            kind="line" if req.kind == "line" else "scatter",
        )
    cat_kind = {
        "histogram": "hist", "box": "box", "violin": "violin",
        "bar": "bar", "count": "count",
    }.get(req.kind, "box")
    return sns.catplot(data=df, x=req.x, y=req.y, hue=req.hue, row=row, col=col, kind=cat_kind)


def _render_png(req: PlotRequest, df: pd.DataFrame) -> tuple[str | None, list[str]]:
    """Base64 PNG of the chart via the matplotlib/seaborn backend (LLM- and
    vision-friendly). Returns (png_base64 | None, warnings)."""
    warnings: list[str] = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if req.kind == "pair":
            grid = _pairplot(df, req)
            if req.title:
                grid.figure.suptitle(req.title)
            fig = grid.figure
        elif req.kind == "facet":
            row, col = (req.row, req.col) if (req.row or req.col) else (req.hue, None)
            if not (row or col):
                return None, ["facet PNG needs row/col (or hue as fallback); skipped"]
            g = _facet_grid(df, req, row, col)
            if req.title:
                g.fig.suptitle(req.title)
            fig = g.fig
        else:
            fig = plt.figure(figsize=(10, 6))
            _draw_seaborn_plot(req, df)
            if req.title:
                plt.title(req.title)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        data = buf.getvalue()
        if len(data) > MAX_PNG_BYTES:
            warnings.append(f"png exceeds {MAX_PNG_BYTES} bytes; consider fewer rows/columns")
        return base64.b64encode(data).decode("ascii"), warnings
    except Exception as exc:  # noqa: BLE001 - render is best-effort
        return None, [f"png render failed: {exc}"]


def _build_response(req: PlotRequest, df: pd.DataFrame, source: str, warnings: list[str]) -> dict[str, Any]:
    missing = _validate_columns(
        df, [req.x, req.y, req.hue, req.size, req.style, req.row, req.col]
    )
    if missing:
        return {
            "status": "error",
            "kind": req.kind,
            "error": f"Missing columns: {missing}",
            "available_columns": df.columns.tolist(),
            "warnings": warnings,
        }

    stats: dict[str, Any] = _basic_stats(df, req.x, req.y, req.hue)
    if req.kind == "pie":
        try:
            pie_df = _prepare_pie_data(df, req.x, req.y)
            stats["pie_summary"] = _jsonable_records(pie_df)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"pie summary failed: {exc}")

    png_b64, png_warnings = _render_png(req, df) if req.include_png else (None, [])
    warnings.extend(png_warnings)

    response: dict[str, Any] = {
        "status": "ok",
        "kind": req.kind,
        "source": source,
        "rows": int(len(df)),
        "data_profile": _profile_dataframe(df),
        "stats": stats,
        "chart_json": _to_chart_json(req, df),
        "sample_rows": _jsonable_records(df.head(req.sample_rows)),
        "warnings": warnings,
    }
    if png_b64:
        response["png_base64"] = png_b64
    if req.include_html:
        plotly_html = None
        mpld3_html = None
        try:
            plotly_html = _render_plotly_html(req, df)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"plotly render failed: {exc}")
        try:
            mpld3_html = _render_seaborn_html_mpld3(req, df)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"mpld3 render failed: {exc}")
        response["html"] = {"plotly": plotly_html, "seaborn_mpld3": mpld3_html}
    return response


# --- tools ---------------------------------------------------------------------


@mcp.tool()
async def plot(
    kind: PlotKind,
    x: str | None = None,
    y: str | None = None,
    hue: str | None = None,
    size: str | None = None,
    style: str | None = None,
    row: str | None = None,
    col: str | None = None,
    title: str | None = None,
    bins: int | None = None,
    sql: str | None = None,
    data_url: str | None = None,
    data: list[dict[str, Any]] | None = None,
    include_html: bool = False,
    include_png: bool = False,
    sample_rows: int = 10,
) -> str:
    """Create one statistical chart from SQL, a URL, or inline rows.

    Preferred: pass `sql` (read-only SELECT run via sqlhandler) so row data
    never round-trips through the model; or `data_url` (https JSON/CSV).
    Inline `data` is fine only for tiny datasets (<200 rows).

    Kinds: scatter, line, histogram, kde, box, violin, bar, count,
    regression, residual, correlation_heatmap, pair, facet, pie.

    Returns compact JSON: stats, chart_json (vega-lite), a small row sample,
    optional base64 PNG (include_png=True — pass it to a vision tool), and
    interactive HTML only when include_html=True.

    Kind hints: relationship→scatter/line/regression; distribution→histogram/
    kde; comparison across categories→box/violin/bar/count; composition→pie;
    many-numeric overview→pair or correlation_heatmap; small multiples→facet
    (row/col).
    """
    try:
        req = PlotRequest(
            sql=sql, data_url=data_url, data=data, kind=kind,
            x=x, y=y, hue=hue, size=size, style=style, row=row, col=col,
            title=title, bins=bins,
            include_html=include_html, include_png=include_png,
            sample_rows=sample_rows,
        )
        rows, source, warnings = await load_rows(
            sql=req.sql, data_url=req.data_url, data=req.data
        )
        df = pd.DataFrame.from_records(rows)
        if df.empty:
            return _error("data source produced zero rows", source=source, warnings=warnings)
        return _to_json_string(_build_response(req, df, source, warnings))
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), kind=kind)


@mcp.tool()
async def describe(
    sql: str | None = None,
    data_url: str | None = None,
    data: list[dict[str, Any]] | None = None,
    sample_rows: int = 10,
) -> str:
    """Profile a dataset before plotting: columns, dtypes, missing values,
    cardinality, numeric summaries, and a small sample.

    Same data-source selection as `plot` (sql preferred, then data_url,
    then inline data). Use this to choose the right chart kind and columns;
    it creates no chart.
    """
    try:
        req = DescribeRequest(sql=sql, data_url=data_url, data=data, sample_rows=sample_rows)
        rows, source, warnings = await load_rows(
            sql=req.sql, data_url=req.data_url, data=req.data
        )
        df = pd.DataFrame.from_records(rows)
        if df.empty:
            return _error("data source produced zero rows", source=source, warnings=warnings)
        return _to_json_string(
            {
                "status": "ok",
                "source": source,
                "rows": int(len(df)),
                "data_profile": _profile_dataframe(df),
                "numeric_summary": _describe_numeric(df),
                "sample_rows": _jsonable_records(df.head(req.sample_rows)),
                "warnings": warnings,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc))


@mcp.tool()
def health_check() -> str:
    """Check whether the statistical visualization MCP server is alive."""
    return _to_json_string(
        {"status": "ok", "version": "0.1.0", "tools": ["plot", "describe", "health_check"]}
    )
