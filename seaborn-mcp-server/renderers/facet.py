from typing import Any

import mpld3
import pandas as pd
import seaborn as sns
from schema import FacetPlotKind, PlotRequest


def _render_facet_grid(
    df: pd.DataFrame,
    plot_kind: FacetPlotKind,
    x: str | None,
    y: str | None,
    hue: str | None,
    row: str | None,
    col: str | None,
    title: str | None,
    bins: int | None,
) -> Any:
    if plot_kind in {"scatter", "line"}:
        if not x or not y:
            raise ValueError(f"{plot_kind} facet plot requires x and y.")

        grid = sns.relplot(
            data=df,
            x=x,
            y=y,
            hue=hue,
            row=row,
            col=col,
            kind="scatter" if plot_kind == "scatter" else "line",
        )

    elif plot_kind == "histogram":
        if not x:
            raise ValueError("Histogram facet plot requires x.")

        dis_kwargs: dict[str, Any] = {
            "data": df,
            "x": x,
            "hue": hue,
            "row": row,
            "col": col,
            "kind": "hist",
        }
        if bins is not None:
            dis_kwargs["bins"] = bins

        grid = sns.displot(**dis_kwargs)

    elif plot_kind in {"box", "violin", "bar", "count"}:
        cat_kwargs: dict[str, Any] = {
            "data": df,
            "x": x,
            "y": y,
            "hue": hue,
            "row": row,
            "col": col,
            "kind": plot_kind,
        }

        if plot_kind == "count":
            cat_kwargs["y"] = None

        cat_kwargs = {k: v for k, v in cat_kwargs.items() if v is not None}
        grid = sns.catplot(**cat_kwargs)

    else:
        raise ValueError(f"Unsupported facet plot kind: {plot_kind}")

    if title:
        grid.figure.suptitle(title)
        grid.figure.tight_layout()

    return grid


def _render_facet_html_mpld3(
    df: pd.DataFrame,
    plot_kind: FacetPlotKind,
    x: str | None,
    y: str | None,
    hue: str | None,
    row: str | None,
    col: str | None,
    title: str | None,
    bins: int | None,
) -> str | None:
    grid = _render_facet_grid(
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

    return mpld3.fig_to_html(grid.figure)