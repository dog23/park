from __future__ import annotations
# --- EXIT MODEL ADDITION START ---
import math
import re
from typing import Any
SYMBOL_POINT_VALUES = {"NQ": 20.0, "ES": 50.0, "GC": 100.0, "RTY": 50.0, "YM": 5.0}
SYMBOL_TICK_SIZES = {"NQ": 0.25, "ES": 0.25, "GC": 0.10, "RTY": 0.10, "YM": 1.0}
def normalize_symbol(symbol: str) -> str:
    """Strips contract month/year. Returns 'UNKNOWN' on bad/blank input."""
    try:
        match = re.match(r"^([A-Z]+)", str(symbol).strip().upper())
        return match.group(1) if match else "UNKNOWN"
    except Exception:
        return "UNKNOWN"
def data_series_key(bars_period_text: str) -> str:
    """Turns '500 Tick' -> '500TICK', 'Minute:5' -> 'MINUTE5', etc.
    Matches the same grouping already used by grouped_outcome_stats() on the
    dashboard, just collapsed to a filesystem/dict-safe key. Falls back to
    'UNKNOWN' on blank input so ungrouped/legacy data still has a home."""
    text = re.sub(r"[^A-Za-z0-9]", "", str(bars_period_text or "").upper())
    return text if text else "UNKNOWN"
def group_key(symbol: str, bars_period_text: str) -> str:
    """Combined (symbol, data_series) key used to bucket models/files."""
    return f"{normalize_symbol(symbol)}_{data_series_key(bars_period_text)}"
def exit_group_key(symbol: str, data_series_type: str, data_series_value: Any) -> str:
    """Same group key format as the entry side, built from the exit TSV's
    separate data_series_type/data_series_value columns instead of a single
    bars_period string. '500', 'Tick' -> group_key(symbol, '500 Tick')."""
    text = f"{data_series_value} {data_series_type}".strip()
    return group_key(symbol, text)
def symbol_hash_feature(symbol: str, seed: int = 17) -> float:
    h = seed
    for c in symbol.upper():
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return max(-1.0, min(1.0, (h % 20001) / 10000.0 - 1.0))
def dollars_per_tick_feature(symbol: str) -> float:
    pv = SYMBOL_POINT_VALUES.get(symbol, 1.0)
    ts = SYMBOL_TICK_SIZES.get(symbol, 0.25)
    return max(-2.0, min(2.0, math.log10(max(1e-9, pv * ts)) - 1.0))
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
def validate_predict_exit_input(body: dict) -> str | None:
    """Returns error string if invalid, None if valid."""
    seq = body.get("sequence", [])
    ctx = body.get("context", [])
    if not seq or len(seq) < 1:
        return "sequence must have at least 1 row"
    for i, row in enumerate(seq):
        if not isinstance(row, list) or len(row) != 9:
            return f"sequence row {i} must have exactly 9 numbers"
        if not all(_is_number(v) for v in row):
            return f"sequence row {i} contains non-numeric values"
    if not isinstance(ctx, list) or len(ctx) != 8:
        return "context must have exactly 8 numbers"
    if not all(_is_number(v) for v in ctx):
        return "context contains non-numeric values"
    return None
def validate_log_exit_sample_input(body: dict) -> str | None:
    """Returns error string if invalid, None if valid."""
    features = body.get("features", [])
    label = body.get("label")
    if not isinstance(features, list) or len(features) != 9:
        return "features must have exactly 9 numbers"
    if not all(_is_number(v) for v in features):
        return "features contains non-numeric values"
    if label not in (0, 1):
        return "label must be 0 or 1"
    return None
# --- EXIT MODEL ADDITION END ---
