from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from schema import PlotRequest
from renderers.seaborn import _prepare_pie_data


def _render_plotly_html(req: PlotRequest, df: pd.DataFrame) -> str | None:
    common_kwargs: dict[str, Any] = {
        "data_frame": df,
        "title": req.title,
    }

    if req.x:
        common_kwargs["x"] = req.x
    if req.y:
        common_kwargs["y"] = req.y
    if req.hue:
        common_kwargs["color"] = req.hue
    if req.size and req.kind in {"scatter"}:
        common_kwargs["size"] = req.size

    if req.kind == "scatter":
        fig = px.scatter(**common_kwargs)

    elif req.kind == "line":
        fig = px.line(**common_kwargs)

    elif req.kind == "histogram":
        common_kwargs.pop("y", None)
        if req.bins:
            common_kwargs["nbins"] = req.bins
        fig = px.histogram(**common_kwargs)

    elif req.kind == "box":
        fig = px.box(**common_kwargs)

    elif req.kind == "violin":
        fig = px.violin(**common_kwargs)

    elif req.kind == "bar":
        fig = px.bar(**common_kwargs)

    elif req.kind == "count":
        if not req.x:
            return None
        count_df = (
            df.groupby([req.x] + ([req.hue] if req.hue else []), dropna=False)
            .size()
            .reset_index(name="count")
        )
        fig = px.bar(
            count_df,
            x=req.x,
            y="count",
            color=req.hue if req.hue else None,
            title=req.title,
        )

    elif req.kind == "regression":
        if not req.x or not req.y:
            return None

        if not (
            pd.api.types.is_numeric_dtype(df[req.x])
            and pd.api.types.is_numeric_dtype(df[req.y])
        ):
            return None

        clean = df[[req.x, req.y] + ([req.hue] if req.hue else [])].dropna()
        fig = px.scatter(
            clean,
            x=req.x,
            y=req.y,
            color=req.hue if req.hue else None,
            title=req.title,
        )

        x_values = clean[req.x].astype(float).to_numpy()
        y_values = clean[req.y].astype(float).to_numpy()

        if len(clean) > 1:
            slope, intercept = np.polyfit(x_values, y_values, 1)
            x_line = np.linspace(x_values.min(), x_values.max(), 100)
            y_line = slope * x_line + intercept
            fig.add_trace(
                go.Scatter(
                    x=x_line,
                    y=y_line,
                    mode="lines",
                    name="OLS trend",
                )
            )

    elif req.kind == "residual":
        if not req.x or not req.y:
            return None

        if not (
            pd.api.types.is_numeric_dtype(df[req.x])
            and pd.api.types.is_numeric_dtype(df[req.y])
        ):
            return None

        clean = df[[req.x, req.y] + ([req.hue] if req.hue else [])].dropna()
        x_values = clean[req.x].astype(float).to_numpy()
        y_values = clean[req.y].astype(float).to_numpy()

        if len(clean) <= 1:
            return None

        slope, intercept = np.polyfit(x_values, y_values, 1)
        clean = clean.copy()
        clean["residual"] = y_values - (slope * x_values + intercept)

        fig = px.scatter(
            clean,
            x=req.x,
            y="residual",
            color=req.hue if req.hue else None,
            title=req.title or f"Residual plot: {req.y} ~ {req.x}",
        )
        fig.add_hline(y=0)

    elif req.kind == "correlation_heatmap":
        corr = df.select_dtypes(include="number").corr()
        fig = px.imshow(
            corr,
            text_auto=True,
            title=req.title or "Correlation heatmap",
        )

    elif req.kind == "pair":
        dimensions = df.select_dtypes(include="number").columns.tolist()
        if len(dimensions) < 2:
            return None

        fig = px.scatter_matrix(
            df,
            dimensions=dimensions,
            color=req.hue if req.hue else None,
            title=req.title,
        )

    elif req.kind == "pie":
        pie_df = _prepare_pie_data(df, req.x, req.y)
        fig = px.pie(
            pie_df,
            names="label",
            values="value",
            title=req.title,
            hover_data=["percentage"],
        )

    elif req.kind in ("kde", "facet"):
        return None

    else:
        return None

    return fig.to_html(
        full_html=False,
        include_plotlyjs="cdn",
    )