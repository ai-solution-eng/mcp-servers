from typing import Any

import numpy as np
import pandas as pd


def _profile_dataframe(df: pd.DataFrame) -> dict[str, Any]:
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    datetime_cols = df.select_dtypes(
        include=["datetime", "datetimetz"]
    ).columns.tolist()
    boolean_cols = df.select_dtypes(include="bool").columns.tolist()

    categorical_cols = [
        c
        for c in df.columns
        if c not in numeric_cols and c not in datetime_cols and c not in boolean_cols
    ]

    return {
        "rows": int(len(df)),
        "columns": df.columns.tolist(),
        "numeric_columns": numeric_cols,
        "categorical_columns": categorical_cols,
        "datetime_columns": datetime_cols,
        "boolean_columns": boolean_cols,
        "missing_values": {col: int(df[col].isna().sum()) for col in df.columns},
        "unique_values": {col: int(df[col].nunique(dropna=True)) for col in df.columns},
        "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
    }


def _describe_numeric(df: pd.DataFrame) -> dict[str, Any]:
    numeric_df = df.select_dtypes(include="number")

    if numeric_df.empty:
        return {}

    desc = numeric_df.describe().replace({np.nan: None}).to_dict()
    return desc


def _basic_stats(
    df: pd.DataFrame,
    x: str | None = None,
    y: str | None = None,
    hue: str | None = None,
) -> dict[str, Any]:
    from utils.data import _jsonable_dict

    stats: dict[str, Any] = {
        "numeric_summary": _describe_numeric(df),
    }

    for col in [x, y]:
        if col and col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
            series = df[col].dropna()

            if len(series) > 0:
                stats[col] = {
                    "min": float(series.min()),
                    "max": float(series.max()),
                    "mean": float(series.mean()),
                    "median": float(series.median()),
                    "std": float(series.std()) if len(series) > 1 else None,
                }

    if (
        x
        and y
        and x in df.columns
        and y in df.columns
        and pd.api.types.is_numeric_dtype(df[x])
        and pd.api.types.is_numeric_dtype(df[y])
    ):
        corr_df = df[[x, y]].dropna()

        if len(corr_df) > 1:
            stats["pearson_correlation"] = float(corr_df.corr().iloc[0, 1])

    if hue and hue in df.columns:
        stats["group_counts"] = {
            str(k): int(v) for k, v in df[hue].value_counts(dropna=False).items()
        }

        if y and y in df.columns and pd.api.types.is_numeric_dtype(df[y]):
            grouped = (
                df.groupby(hue, dropna=False)[y]
                .agg(["count", "mean", "median", "std", "min", "max"])
                .replace({np.nan: None})
            )
            stats["grouped_y_summary"] = _jsonable_dict(grouped.to_dict())

    return stats