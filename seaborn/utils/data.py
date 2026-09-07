import math
from typing import Any

import numpy as np
import pandas as pd


def _safe_json_value(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, np.integer):
        return int(value)

    if isinstance(value, np.floating):
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, pd.Timestamp):
        return value.isoformat()

    return value


def _jsonable_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = df.replace({np.nan: None}).to_dict(orient="records")
    return [
        {key: _safe_json_value(value) for key, value in row.items()} for row in records
    ]


def _jsonable_dict(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): (
            _safe_json_value(value)
            if not isinstance(value, dict)
            else _jsonable_dict(value)
        )
        for key, value in obj.items()
    }


def _df_from_records(data: list[dict[str, Any]]) -> pd.DataFrame:
    if not data:
        raise ValueError("Input data cannot be empty.")

    df = pd.DataFrame(data)

    if df.empty:
        raise ValueError("Input data produced an empty DataFrame.")

    return df


def _validate_columns(df: pd.DataFrame, columns: list[str | None]) -> list[str]:
    requested = [c for c in columns if c]
    return [c for c in requested if c not in df.columns]