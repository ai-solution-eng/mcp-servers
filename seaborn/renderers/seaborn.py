from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mpld3
import numpy as np
import pandas as pd
import seaborn as sns
from schema import PlotRequest


def _common_sns_kwargs(req: PlotRequest, df: pd.DataFrame) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"data": df}

    for key in ["x", "y", "hue", "size", "style"]:
        value = getattr(req, key)
        if value:
            kwargs[key] = value

    return kwargs


def _prepare_pie_data(
    df: pd.DataFrame,
    labels_col: str | None,
    values_col: str | None = None,
) -> pd.DataFrame:
    """
    Prepare category/value data for a pie chart.

    If values_col is provided, values are summed by labels_col.
    If values_col is not provided, rows are counted by labels_col.
    """
    if not labels_col:
        raise ValueError("Pie chart requires x as the label/category column.")

    if labels_col not in df.columns:
        raise ValueError(f"Pie label column '{labels_col}' not found.")

    if values_col:
        if values_col not in df.columns:
            raise ValueError(f"Pie value column '{values_col}' not found.")
        if not pd.api.types.is_numeric_dtype(df[values_col]):
            raise ValueError(f"Pie value column '{values_col}' must be numeric.")

        pie_df = (
            df.groupby(labels_col, dropna=False)[values_col]
            .sum()
            .reset_index(name="value")
        )
    else:
        pie_df = df.groupby(labels_col, dropna=False).size().reset_index(name="value")

    pie_df = pie_df.rename(columns={labels_col: "label"})
    pie_df["label"] = pie_df["label"].astype(str)
    pie_df = pie_df[pie_df["value"].notna()]
    pie_df = pie_df[pie_df["value"] > 0]
    pie_df = pie_df.sort_values("value", ascending=False)

    if pie_df.empty:
        raise ValueError("Pie chart has no positive values to plot.")

    total = float(pie_df["value"].sum())
    pie_df["percentage"] = pie_df["value"] / total * 100.0

    return pie_df


def _draw_seaborn_plot(req: PlotRequest, df: pd.DataFrame) -> Any:
    kwargs = _common_sns_kwargs(req, df)

    if req.kind == "scatter":
        return sns.scatterplot(**kwargs)

    if req.kind == "line":
        return sns.lineplot(**kwargs)

    if req.kind == "histogram":
        kwargs.pop("y", None)
        if req.bins:
            kwargs["bins"] = req.bins
        return sns.histplot(**kwargs)

    if req.kind == "kde":
        return sns.kdeplot(**kwargs)

    if req.kind == "box":
        kwargs.pop("size", None)
        kwargs.pop("style", None)
        return sns.boxplot(**kwargs)

    if req.kind == "violin":
        kwargs.pop("size", None)
        kwargs.pop("style", None)
        return sns.violinplot(**kwargs)

    if req.kind == "bar":
        kwargs.pop("size", None)
        kwargs.pop("style", None)
        return sns.barplot(**kwargs)

    if req.kind == "count":
        kwargs.pop("y", None)
        kwargs.pop("size", None)
        kwargs.pop("style", None)
        return sns.countplot(**kwargs)

    if req.kind == "regression":
        allowed = {k: v for k, v in kwargs.items() if k in {"data", "x", "y"}}
        return sns.regplot(**allowed)

    if req.kind == "residual":
        allowed = {k: v for k, v in kwargs.items() if k in {"data", "x", "y"}}
        return sns.residplot(**allowed)

    if req.kind == "correlation_heatmap":
        corr = df.select_dtypes(include="number").corr()
        return sns.heatmap(corr, annot=True, fmt=".2f", center=0)

    if req.kind == "pie":
        pie_df = _prepare_pie_data(df, req.x, req.y)
        ax = plt.gca()
        ax.pie(
            pie_df["value"],
            labels=pie_df["label"],
            autopct="%1.1f%%",
            startangle=90,
        )
        ax.axis("equal")
        return ax

    raise ValueError(f"Unsupported Seaborn plot kind: {req.kind}")


def _render_seaborn_html_mpld3(req: PlotRequest, df: pd.DataFrame) -> str | None:
    if req.kind == "pair":
        grid = sns.pairplot(df, hue=req.hue if req.hue else None)
        if req.title:
            grid.figure.suptitle(req.title)
            grid.figure.tight_layout()
        fig = grid.figure

    elif req.kind == "facet":
        return None

    else:
        fig = plt.figure(figsize=(10, 6))
        _draw_seaborn_plot(req, df)

        if req.title:
            plt.title(req.title)

    return mpld3.fig_to_html(fig)