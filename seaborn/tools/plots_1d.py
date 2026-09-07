from typing import Any

from mcp_instance import mcp
from tools.core import _build_response, _df_from_records, _make_request, _to_json_string


@mcp.tool()
def histogram_plot(
    data: list[dict[str, Any]],
    x: str,
    hue: str | None = None,
    bins: int | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a histogram for inspecting the distribution of a numeric variable.

    Use this tool when:
    - Showing the distribution, spread, or skew of a numeric variable
    - Identifying common ranges, gaps, or abnormal values
    - Comparing frequency of numeric measurements across categories (with hue)

    For other distribution views:
    - Smooth density curves → kde_plot
    - Box plot for medians/spread/outliers → box_plot
    - Violin plot for full distribution shape → violin_plot
    - Category frequency counts → count_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="histogram",
            x=x,
            hue=hue,
            bins=bins,
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
def kde_plot(
    data: list[dict[str, Any]],
    x: str,
    y: str | None = None,
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a kernel density estimate plot for smooth distribution analysis.

    Use this tool when:
    - Showing a smoothed, continuous view of a variable's distribution
    - Comparing density shapes between categories (with hue)
    - Creating a 2D density plot (with x and y)

    Note: KDE plots are rendered as HTML via the seaborn mpld3 backend (Plotly HTML
    is not produced for KDE plots).

    For other distribution views:
    - Histogram (bar-based frequency) → histogram_plot
    - Box plot for medians/spread/outliers → box_plot
    - Violin plot for full distribution shape → violin_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="kde",
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
def box_plot(
    data: list[dict[str, Any]],
    x: str | None,
    y: str,
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a box plot for comparing distributions, medians, spread, and outliers.

    Use this tool when:
    - Comparing how a numeric variable differs across categories
    - Showing medians, quartiles, and outlier detection
    - Checking for differences between faulty vs normal records

    For richer distribution comparison (including shape, density, skew):
    - Violin plot → violin_plot
    - KDE plot for smooth density → kde_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="box",
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
def violin_plot(
    data: list[dict[str, Any]],
    x: str | None,
    y: str,
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a violin plot for comparing full distribution shapes across categories.

    Use this tool when:
    - Comparing richer distribution details (shape, density, skew, multimodality)
    - Seeing both the distribution shape and quartile information
    - More detailed than a box plot, fuller picture of the data

    For simpler distribution views:
    - Box plot for quartile-based comparison → box_plot
    - Histogram for frequency-based view → histogram_plot
    - KDE for smooth continuous density → kde_plot

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="violin",
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
def count_plot(
    data: list[dict[str, Any]],
    x: str,
    hue: str | None = None,
    title: str | None = None,
    max_return_rows: int = 500,
) -> str:
    """
    Create a count plot for category frequencies.

    Use this tool when:
    - Counting how many observations fall into each category
    - Showing frequency, class, fault status, region, or group counts
    - Comparing counts across categories with hue for a second variable

    For aggregated numeric values (not counts):
    - Bar plot → bar_plot
    - Pie chart for part-to-whole → pie_chart

    """
    try:
        df = _df_from_records(data)
        req = _make_request(
            data=data,
            kind="count",
            x=x,
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