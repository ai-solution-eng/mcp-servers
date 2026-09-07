from typing import Literal

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


class PlotRequest(BaseModel):
    data: list[dict[str, object]] = Field(
        ...,
        description="Tabular data as a list of JSON records.",
    )
    kind: PlotKind = Field(
        ...,
        description="Type of plot to create.",
    )
    x: str | None = Field(
        default=None,
        description="Column to use for the x-axis.",
    )
    y: str | None = Field(
        default=None,
        description="Column to use for the y-axis.",
    )
    hue: str | None = Field(
        default=None,
        description="Column used to color or group observations.",
    )
    size: str | None = Field(
        default=None,
        description="Column used to size marks where supported.",
    )
    style: str | None = Field(
        default=None,
        description="Column used to style marks where supported.",
    )
    row: str | None = Field(
        default=None,
        description="Column used as the row facet variable.",
    )
    col: str | None = Field(
        default=None,
        description="Column used as the column facet variable.",
    )
    title: str | None = Field(
        default=None,
        description="Optional chart title.",
    )
    bins: int | None = Field(
        default=None,
        description="Number of bins for histogram-like charts.",
    )
    max_return_rows: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of sampled data rows returned in the JSON response.",
    )