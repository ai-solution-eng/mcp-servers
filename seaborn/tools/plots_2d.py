from typing import Any

import pandas as pd

from mcp_instance import mcp
from schema import FacetPlotKind
from tools.core import (
    _basic_stats,
    _build_response,
    _build_plot_spec,
    _df_from_records,
    _jsonable_dict,
    _jsonable_records,
    _make_request,
    _profile_dataframe,
    _to_chart_json,
    _to_json_string,
    _validate_columns,
)
from renderers.facet import _render_facet_html_mpld3, _render_facet_grid


@mcp.tool()
def scatter_plot(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    hue: str | None = None,
    size: str | None = None,
    style: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a scatter plot for analyzing the relationship between two variables.

    Use this tool when:
    - Comparing two numeric variables (e.g., fuel flow vs power output)
    - Identifying clusters or outliers in data
    - Inspecting class separation with the hue parameter

    For other relationships:
    - Time series trends → line_plot
    - Distribution shape → histogram_plot or kde_plot
    - Category comparisons → bar_plot or box_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="scatter",
            x=x,
            y=y,
            hue=hue,
            size=size,
            style=style,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def line_plot(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a line plot for trends over time, ordered x-values, or sequential measurements.

    Use this tool when:
    - Showing trends, trajectories, or changes over time/cycles
    - Comparing multiple groups over the same x-axis (with hue)
    - Displaying time series or metric evolution

    For point-by-point comparison:
    - Scatter plot → scatter_plot
    - Distribution of a single variable → histogram_plot or kde_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="line",
            x=x,
            y=y,
            hue=hue,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def bar_plot(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a bar plot for comparing an aggregated numeric value across categories.

    Use this tool when:
    - Comparing averages, totals, or metrics across categories
    - Showing category-level aggregations (sums, means)
    - Best for categorical x-axis and numeric y-axis

    For raw category frequency counts:
    - Count plot → count_plot
    - Pie chart for part-to-whole → pie_chart

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="bar",
            x=x,
            y=y,
            hue=hue,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def regression_plot(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a regression plot with a fitted trend line.

    Use this tool when:
    - Checking if two numeric variables have a linear relationship
    - Seeing if one metric increases with another
    - Visualizing an estimated trend line alongside data points

    The response includes correlation and basic numeric statistics.

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="regression",
            x=x,
            y=y,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def residual_plot(
    data: list[dict[str, Any]],
    x: str,
    y: str,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a residual plot for diagnosing a linear regression fit.

    Use this tool when:
    - Checking whether a linear trend is appropriate
    - Detecting curvature, heteroscedasticity, or outliers in residuals
    - Evaluating systematic model error in regression

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="residual",
            x=x,
            y=y,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def pie_chart(
    data: list[dict[str, Any]],
    x: str,
    y: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a pie chart for part-to-whole composition across categories.

    Use this tool when:
    - Showing share, percentage, or proportion of categories
    - Displaying composition or contribution breakdown
    - Comparing category contributions to a whole

    Use x as the category/label column. Use y as the numeric value column
    to sum by category. If y is omitted, rows are counted per category.

    For alternative views:
    - Bar plot for side-by-side comparison → bar_plot
    - Count plot for frequency counts → count_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="pie",
            x=x,
            y=y,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string({"status": "error", "error": str(exc), "warnings": []})


@mcp.tool()
def correlation_heatmap(
    data: list[dict[str, Any]],
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a correlation heatmap across all numeric columns.

    Use this tool when:
    - Identifying which numeric variables move together
    - Finding correlated features or sensors
    - Checking for predictor redundancy

    The response includes the correlation matrix as JSON alongside the visualization.

    """
    try:
        df = _df_from_records(data)
        numeric_df = df.select_dtypes(include="number")

        if numeric_df.empty:
            return _to_json_string(
                {
                    "status": "error",
                    "kind": "correlation_heatmap",
                    "error": "No numeric columns found for correlation heatmap.",
                    "available_columns": df.columns.tolist(),
                    "warnings": [],
                }
            )

        req = _make_request(
            data=data,
            kind="correlation_heatmap",
            title=title,
            max_return_rows=max_return_rows,
        )

        response = _build_response(req, df)
        response["correlation_matrix"] = _jsonable_dict(
            numeric_df.corr().round(4).to_dict()
        )
        return _to_json_string(response)

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def pair_plot(
    data: list[dict[str, Any]],
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a pair plot for exploratory analysis across multiple numeric variables.

    Use this tool when:
    - Broad exploratory analysis of relationships across many numeric columns
    - Identifying clusters, class separation, or correlations
    - Getting a quick visual overview of a dataset

    Note: This chart can be expensive for wide datasets. Consider limiting to
    the most relevant numeric columns first using summarize_dataset.

    """
    try:
        df = _df_from_records(data)

        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if len(numeric_cols) < 2:
            return _to_json_string(
                {
                    "status": "error",
                    "kind": "pair",
                    "error": "Pair plot requires at least two numeric columns.",
                    "available_columns": df.columns.tolist(),
                    "warnings": [],
                }
            )

        req = _make_request(
            data=data,
            kind="pair",
            hue=hue,
            title=title,
            max_return_rows=max_return_rows,
        )
        return _to_json_string(_build_response(req, df))

    except Exception as exc:
        return _to_json_string(
            {
                "status": "error",
                "error": str(exc),
                "warnings": [],
            }
        )


@mcp.tool()
def facet_plot(
    data: list[dict[str, Any]],
    plot_kind: FacetPlotKind,
    x: str | None = None,
    y: str | None = None,
    hue: str | None = None,
    row: str | None = None,
    col: str | None = None,
    title: str | None = None,
    bins: int | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a faceted small-multiple plot split by row and/or column categories.

    Use this tool when:
    - Showing the same chart repeated across categories
    - Comparing behavior by region, fault status, product, etc.
    - Avoiding overcrowded single charts

    Faceting creates a grid of subplots, each showing the data for a specific
    category combination of the row and column variables.

    Note: Plotly HTML is not produced for facet plots. They are rendered as HTML
    via the seaborn mpld3 backend.

    Args:
        plot_kind: The type of plot for each facet (scatter, line, histogram, box, violin, bar, count).
        row: Column to use as row facet (creates a row per category).
        col: Column to use as column facet (creates a column per category).
    """
    warnings: list[str] = []

    try:
        df = _df_from_records(data)

        missing = _validate_columns(df, [x, y, hue, row, col])
        if missing:
            return _to_json_string(
                {
                    "status": "error",
                    "kind": "facet",
                    "error": f"Missing columns: {missing}",
                    "available_columns": df.columns.tolist(),
                    "warnings": [],
                }
            )

        req = _make_request(
            data=data,
            kind="facet",
            x=x,
            y=y,
            hue=hue,
            row=row,
            col=col,
            title=title,
            bins=bins,
            max_return_rows=max_return_rows,
        )

        seaborn_mpld3_html = None

        try:
            seaborn_mpld3_html = _render_facet_html_mpld3(
                df=df,
                plot_kind=plot_kind,
                x=x,
                y=y,
                hue=hue,
                row=row,
                col=col,
                title=title,
                bins=bins,
            )
        except Exception as exc:
            warnings.append(f"Could not render facet HTML: {exc}")

        return _to_json_string(
            {
                "status": "ok",
                "kind": "facet",
                "facet_plot_kind": plot_kind,
                "plot_spec": _build_plot_spec(req) | {"facet_plot_kind": plot_kind},
                "data_profile": _profile_dataframe(df),
                "stats": _basic_stats(df, x, y, hue),
                "chart_json": _to_chart_json(req, df),
                "sampled_data": _jsonable_records(df.head(max_return_rows)),
                "html": {
                    "plotly": None,
                    "seaborn_mpld3": seaborn_mpld3_html,
                },
                "warnings": warnings,
            }
        )

    except Exception as exc:
        return _to_json_string(
            {"status": "error", "error": str(exc), "warnings": warnings}
        )