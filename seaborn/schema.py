"""Shared request models for the statistical-visualization MCP server (v0.1).

v0.1 redesign: data arrives BY REFERENCE (a SQL query executed via the
sqlhandler MCP server, or an https:// URL returning JSON/CSV) or inline
for tiny datasets. The model never re-transmits large row payloads.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

PlotKind = Literal[
    "scatter",
    "line",
    "histogram",
    "kde",
    "box",
    "violin",
    "bar",
    "count",
    "regression",
    "residual",
    "correlation_heatmap",
    "pair",
    "facet",
    "pie",
]

FacetPlotKind = Literal[
    "scatter",
    "line",
    "histogram",
    "box",
    "violin",
    "bar",
    "count",
]

# Inline data is tolerated for convenience but discouraged past this size.
MAX_INLINE_ROWS = 200
# Hard cap on rows pulled from sqlhandler / URL fetch (bounding memory +
# render cost server-side, independent of the query the model writes).
MAX_FETCH_ROWS = 20_000
# Response budget: PNG bytes returned to the caller.
MAX_PNG_BYTES = 600_000


class PlotRequest(BaseModel):
    """One plotting request. Provide ONE of: ``sql``, ``data_url``, ``data``."""

    # --- data source (exactly one) ---
    sql: str | None = Field(
        default=None,
        description=(
            "Read-only SQL SELECT executed via the sqlhandler MCP server "
            "(SQLHANDLER_MCP_URL). Preferred source: the model never carries "
            "row data through the tool call."
        ),
    )
    data_url: str | None = Field(
        default=None,
        description=(
            "https:// URL returning JSON records or CSV. Internal/loopback/"
            "metadata targets are refused (SSRF guard)."
        ),
    )
    data: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            f"Inline records. Discouraged: use sql/data_url for anything over "
            f"{MAX_INLINE_ROWS} rows."
        ),
    )

    # --- chart ---
    kind: PlotKind = Field(..., description="Type of plot to create.")
    x: str | None = Field(default=None, description="Column for the x-axis.")
    y: str | None = Field(default=None, description="Column for the y-axis.")
    hue: str | None = Field(default=None, description="Column to color/group by.")
    size: str | None = Field(default=None, description="Column to size marks where supported.")
    style: str | None = Field(default=None, description="Column to style marks where supported.")
    row: str | None = Field(default=None, description="Row facet variable.")
    col: str | None = Field(default=None, description="Column facet variable.")
    title: str | None = Field(default=None, description="Optional chart title.")
    bins: int | None = Field(default=None, description="Bin count for histogram-like charts.")

    # --- response shaping ---
    include_html: bool = Field(
        default=False,
        description=(
            "Return interactive HTML (plotly + seaborn/mpld3) for UI callers. "
            "Default OFF: HTML dominates the payload and is useless to an LLM."
        ),
    )
    include_png: bool = Field(
        default=False,
        description="Return a base64 PNG of the chart (LLM/vision-friendly, size-capped).",
    )
    sample_rows: int = Field(
        default=10,
        ge=0,
        le=100,
        description="How many sample rows to echo back (for the model's sanity check).",
    )


class DescribeRequest(BaseModel):
    """Profiling request. Same data-source selection as PlotRequest."""

    sql: str | None = Field(default=None, description="SELECT run via sqlhandler.")
    data_url: str | None = Field(default=None, description="https:// JSON/CSV URL.")
    data: list[dict[str, Any]] | None = Field(
        default=None,
        description=f"Inline records (discouraged over {MAX_INLINE_ROWS} rows).",
    )
    sample_rows: int = Field(default=10, ge=0, le=100)
