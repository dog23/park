from __future__ import annotations

import math
import re
from typing import Any


def normalize_symbol(symbol: str) -> str:
    """Strips contract month/year. Returns 'UNKNOWN' on bad/blank input.
    Allows a single leading digit for CME FX six-codes (6E, 6J, 6B, 6A, 6C,
    6S, 6N) -- their root symbol itself starts with a digit, unlike ES/NQ/CL."""
    try:
        match = re.match(r"^([A-Z]+|\d[A-Z]+)", str(symbol).strip().upper())
        return match.group(1) if match else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def data_series_key(bars_period_text: str) -> str:
    """Turns 'Order Flow Delta' -> 'ORDERFLOWDELTA', '1000 Volume' -> '1000VOLUME'."""
    text = re.sub(r"[^A-Za-z0-9]", "", str(bars_period_text or "").upper())
    return text if text else "UNKNOWN"


def group_key(symbol: str, bars_period_text: str) -> str:
    """Combined (symbol, data_series) key used to bucket per-symbol models/files.
    Parameters (thresholds, indicator periods) are shared across all symbols by
    design -- see project notes on avoiding cross-symbol data poisoning. Only
    the symbol (and data series, if you ever test more than one) partitions
    the training data."""
    return f"{normalize_symbol(symbol)}_{data_series_key(bars_period_text)}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_window(window: Any, n_features: int) -> str | None:
    if not isinstance(window, list) or len(window) < 1:
        return "window must be a non-empty list of bars"
    for i, row in enumerate(window):
        if not isinstance(row, list) or len(row) != n_features:
            return f"window row {i} must have exactly {n_features} numbers"
        if not all(_is_number(v) for v in row):
            return f"window row {i} contains non-numeric values"
    return None


def validate_predict_trend_input(body: dict, n_features: int) -> str | None:
    error = _validate_window(body.get("window", []), n_features)
    if error:
        return error
    if not str(body.get("symbol") or "").strip():
        return "symbol is required"
    return None


def validate_log_trend_sample_input(body: dict, n_features: int) -> str | None:
    error = _validate_window(body.get("window", []), n_features)
    if error:
        return error
    label = str(body.get("label") or "").lower()
    if label not in ("long", "short", "no_trade"):
        return "label must be one of: long, short, no_trade"
    return None
