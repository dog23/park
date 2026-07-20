from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

# The hidden watchdog launcher runs this via pythonw.exe (no console), which leaves
# sys.stdout/stderr as None. BaseHTTPRequestHandler.log_message writes unconditionally
# to stderr on every request -- before any response bytes are sent -- so under pythonw
# every single request crashed the handler thread with "'NoneType' object has no
# attribute 'write'" and the client saw an empty reply. Give both a real (discarded)
# stream so logging is a no-op instead of a crash.
if sys.stdout is None or sys.stderr is None:
    _null_stream = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = _null_stream
    if sys.stderr is None:
        sys.stderr = _null_stream


CODE_MEANINGS = {
    "L": "Long",
    "S": "Short",
    "LM": "Long / MidBB cross",
    "SM": "Short / MidBB cross",
    "LV": "Long / VWAP cross",
    "SV": "Short / VWAP cross",
    "LVR": "Long / VWAP rejection",
    "SVR": "Short / VWAP rejection",
    "LU": "Long / Upper-BB breakout",
    "SLB": "Short / Lower-BB breakdown",
    "ConfirmLong": "Long / ML confirmed",
    "ConfirmShort": "Short / ML confirmed",
    "ReversalLong": "Short / ML reversed long setup",
    "ReversalShort": "Long / ML reversed short setup",
}

GLOBAL_SPIKE_SIGNAL_RE = re.compile(r"^(.*)_([LS])_\d+$")
DAY_RANGE_RE = re.compile(r"^(\d{1,2})d$")
ACTIVE_WINDOW_HOURS = 24.0

BAR_TYPE_LABELS = {
    "heikenashi": "Heiken Ashi",
    "kagi": "Kagi",
    "linebreak": "Line Break",
    "minute": "Minute",
    "pointandfigure": "Point & Figure",
    "priceonvolume": "Price On Volume",
    "range": "Range",
    "renko": "Renko",
    "tick": "Tick",
    "volume": "Volume",
    "volumetric": "Volumetric",
    "delta": "Order Flow Delta",
}

ACCOUNT_SIM_PREFIX_RE = re.compile(r"^sim", re.IGNORECASE)

# Strategies that poll for a manual-exit command file (see ManualExitCommand.cs).
MANUAL_EXIT_STRATEGIES = {"TemaLimit"}
# Strategies that poll for a manual-cancel command file (see ManualCancelCommand.cs) --
# only strategies that place working limit entry orders have anything to cancel.
MANUAL_CANCEL_STRATEGIES = {"TemaLimit"}


def canonical_bar_type(raw: str) -> str:
    key = (raw or "").strip().lower()
    label = BAR_TYPE_LABELS.get(key)
    if label:
        return label
    return raw.strip() or "Unknown"


def format_account(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "Unassigned"
    stripped = ACCOUNT_SIM_PREFIX_RE.sub("", raw).strip()
    if not stripped:
        return raw
    return stripped[:1].upper() + stripped[1:] if stripped[:1].isalpha() else stripped


def safe_file_name_part(value: str) -> str:
    """Mirror OpenTradeStatusExporter.SafeFileNamePart / ManualExitCommand.SafeFileNamePart in the NinjaScript strategies."""
    if not value:
        return "Unknown"
    chars = [c if (c.isalnum() or c in "-_") else "_" for c in value]
    return "".join(chars).strip("_")


def manual_exit_command_file_name(strategy: str, ticker: str, account: str) -> str:
    return safe_file_name_part(strategy) + "_" + safe_file_name_part(ticker + "_" + account) + "_exit_command.txt"


def manual_cancel_command_file_name(strategy: str, ticker: str, account: str) -> str:
    return safe_file_name_part(strategy) + "_" + safe_file_name_part(ticker + "_" + account) + "_cancel_command.txt"


# Injected into every "/restart" response page (this server's and its siblings') --
# waits a couple seconds for the old process to actually die, then polls /health
# until the relaunched process answers, and redirects. %s is the page to land on.
# Covers both the dashboard's Restart button AND typing /restart in the URL bar
# directly, since both routes serve this same response.
RESTART_POLL_SCRIPT = """
<script>
(function() {
  function poll(attempt) {
    fetch('/health', { cache: 'no-store' })
      .then(function(r) { if (r.ok) { window.location.href = '%s'; } else { throw new Error('not ready'); } })
      .catch(function() {
        if (attempt > 45) {
          document.getElementById('restartStatus').textContent =
            'Still waiting on the server to come back. Something may have gone wrong -- reload manually once it is ready.';
          return;
        }
        setTimeout(function() { poll(attempt + 1); }, 1000);
      });
  }
  setTimeout(function() { poll(0); }, 2000);
})();
</script>
"""

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_LAYOUTS_PATH = BASE_DIR / "dashboard_layouts.json"
DASHBOARD_LAYOUT_SLOTS_PATH = BASE_DIR / "dashboard_layout_slots.json"
NT_DIR = Path(os.environ.get("NT_USER_DATA_DIR", r"C:\Users\<user>\Documents\NinjaTrader 8"))
NQ_WEIGHTS = Path(os.environ.get("NQ_WEIGHTS_PATH", NT_DIR / "NQOnlineMLP_multi_weights.txt"))
NQ_TRADES = Path(os.environ.get("NQ_TRADES_PATH", NT_DIR / "NQOnlineMLP_trades.tsv"))
GLOBAL_SPIKE_TRADES = Path(os.environ.get("GLOBAL_SPIKE_TRADES_PATH", NT_DIR / "GlobalSpike_completed_trades.tsv"))
TEMA_LIMIT_TRADES = Path(os.environ.get("TEMA_LIMIT_TRADES_PATH", NT_DIR / "TemaLimit_completed_trades.tsv"))
TEMA_LIMIT_NOFILL = Path(os.environ.get("TEMA_LIMIT_NOFILL_PATH", NT_DIR / "TemaLimit_nofill_log.tsv"))
TEMA_LIMIT_GATEBLOCK = Path(os.environ.get("TEMA_LIMIT_GATEBLOCK_PATH", NT_DIR / "TemaLimit_gateblock_log.tsv"))
TEMA_LIMIT_EXPIRE = Path(os.environ.get("TEMA_LIMIT_EXPIRE_PATH", NT_DIR / "TemaLimit_expire_log.tsv"))
TEMA_LIMIT_SLIPPAGE = Path(os.environ.get("TEMA_LIMIT_SLIPPAGE_PATH", NT_DIR / "TemaLimit_slippage_log.tsv"))
TEMALIMIT_CS = Path(os.environ.get("TEMALIMIT_CS_PATH", NT_DIR / "bin" / "Custom" / "Strategies" / "temalimit.cs"))
# Structured record of every edit auto_apply_sizing.py has actually applied to temalimit.cs (see
# that file's HISTORY_PATH comment) -- read here to populate the "Last Applied" column on the
# No-Fill Log / Sizing Reassess / Entry Gate Reassess dashboard tables.
AUTO_APPLY_HISTORY_PATH = Path(os.environ.get("AUTO_APPLY_HISTORY_PATH", BASE_DIR / "auto_apply_history.json"))
TEMA_LIMIT_PULLBACK_STATE = Path(os.environ.get("TEMA_LIMIT_PULLBACK_STATE_PATH", NT_DIR / "TemaLimit_pullback_state.tsv"))
TEMA_LIMIT_TEMPLATE_LIVE_SAMPLES = Path(os.environ.get("TEMA_LIMIT_TEMPLATE_LIVE_SAMPLES_PATH", NT_DIR / "MLService" / "data" / "template_live_samples.csv"))
TEMA_MARKET_TRADES = Path(os.environ.get("TEMA_MARKET_TRADES_PATH", NT_DIR / "TemaMarket_completed_trades.tsv"))
MARKET_MULTI_TICKER_TRADES = Path(os.environ.get("MARKET_MULTI_TICKER_TRADES_PATH", NT_DIR / "MarketMultiTicker_completed_trades.tsv"))
CERAVE_TRADES = Path(os.environ.get("CERAVE_TRADES_PATH", NT_DIR / "Cerave_completed_trades.tsv"))
MULTI_DATA_SERIES_TRADES = Path(os.environ.get("MULTI_DATA_SERIES_TRADES_PATH", NT_DIR / "MultiDataSeries_completed_trades.tsv"))
TREND_TCN_TRADES = Path(os.environ.get("TREND_TCN_TRADES_PATH", NT_DIR / "TrendTcn_completed_trades.tsv"))
FULL_TWENTIES_TRADES = Path(os.environ.get("FULL_TWENTIES_TRADES_PATH", NT_DIR / "fulltwenties" / "trade_log.csv"))
TWENTY_FOUR_SEVEN_TRADES = Path(os.environ.get("TWENTY_FOUR_SEVEN_TRADES_PATH", NT_DIR / "TwentyFourSevenBot" / "trade_log.csv"))

CHART_REQUEST_DIR = NT_DIR / "ChartRequests"
CHART_BARS_PAD_BEFORE = timedelta(minutes=30)
CHART_BARS_PAD_AFTER = timedelta(minutes=15)
CHART_REQUEST_STALE_SECONDS = 20.0  # re-issue a request if the AddOn hasn't answered in this long
CHART_ERROR_CACHE_SECONDS = 60.0  # how long a cached failure blocks a retry -- success responses
# cache forever (historical bars never change), but a transient error (e.g. "no active data
# connection") must not permanently stick to this trade once the underlying problem clears

KNOWN_TRADE_FILES = {
    NQ_TRADES, GLOBAL_SPIKE_TRADES, TEMA_LIMIT_TRADES, TEMA_MARKET_TRADES,
    MARKET_MULTI_TICKER_TRADES, CERAVE_TRADES, MULTI_DATA_SERIES_TRADES, TREND_TCN_TRADES,
}
KNOWN_TRADE_LOG_DIRS = {FULL_TWENTIES_TRADES.parent, TWENTY_FOUR_SEVEN_TRADES.parent}
OPEN_TRADES_GLOB = "*_open_trades.tsv"
PENDING_TRADES_GLOB = "*_pending_trades.tsv"
# Strategies rewrite these files from OnBarUpdate while connected (WriteOpenTradeStatus /
# WritePendingTradeStatus), but update cadence varies a lot by strategy: TemaLimit uses
# Calculate.OnEachTick (sub-second), while TrendTcn uses Calculate.OnBarClose, so its file
# can legitimately go 1-50+ minutes between writes with no disconnect involved. A real
# disconnect leaves a file stale for hours (confirmed empirically: stopped-strategy files
# sit at 45h-8d old), so keep this generous -- it only needs to catch "genuinely dead,"
# not "just updates infrequently."
LIVE_STATUS_STALE_SECONDS = 3600.0


def file_info(path: Path) -> dict:
    try:
        stat = path.stat()
        return {
            "path": str(path),
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mtimeText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        }
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "size": 0, "mtime": 0, "mtimeText": "missing"}


_read_text_cache: dict[Path, tuple[float, int, str]] = {}


def read_text(path: Path) -> str:
    # Every parser in this file funnels through here, and build_status() calls
    # several of them (collect_all_trade_entries, parse_tema_limit, template
    # usage/coverage, etc.) against the *same* trade files on every single
    # request -- switching the range filter re-triggers the whole thing, but
    # filter_rows_by_range() only trims the in-memory list after everything
    # has already been read+parsed, so the disk read/decode cost was paid in
    # full regardless of which range was picked, and grows as trade history
    # accumulates. Cache raw file content keyed on (mtime, size) so repeated
    # reads of an unchanged file -- the common case, since these TSVs/CSVs
    # only change when a strategy actually closes a trade -- cost one stat()
    # call instead of a full read+decode.
    try:
        stat = path.stat()
    except FileNotFoundError:
        _read_text_cache.pop(path, None)
        return ""
    cached = _read_text_cache.get(path)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    for _ in range(3):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            break
        except FileNotFoundError:
            _read_text_cache.pop(path, None)
            return ""
        except OSError:
            # NinjaTrader rewrites hot files (pullback state, open trades) with an
            # exclusive lock many times per second; a poll landing mid-write used to
            # raise PermissionError here, kill the request thread, and drop the
            # connection -- the dashboard then silently froze on its last good
            # payload. Retry briefly, then serve the last-known content instead.
            time.sleep(0.02)
    else:
        return cached[2] if cached is not None else ""
    _read_text_cache[path] = (stat.st_mtime, stat.st_size, text)
    return text


def parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def parse_number(value: str, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_trade_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def hold_time_seconds(entry_time: str, exit_time: str) -> float | None:
    entry = parse_trade_time(entry_time)
    exit_ = parse_trade_time(exit_time)
    if entry is None or exit_ is None or exit_ <= entry:
        return None
    return (exit_ - entry).total_seconds()


def normalize_range(value: str) -> str:
    value = (value or "all").strip()
    lower = value.lower()
    if lower == "all":
        return "all"
    if DAY_RANGE_RE.match(lower):
        return lower
    if lower.startswith("custom:"):
        segments = value.split(":")
        if len(segments) == 3:
            try:
                datetime.strptime(segments[1], "%Y-%m-%d")
                datetime.strptime(segments[2], "%Y-%m-%d")
                return f"custom:{segments[1]}:{segments[2]}"
            except ValueError:
                pass
    return "all"


SESSION_START_HOUR = 15  # 3:00 PM local (California) -- start of the trading session


def session_anchor(now: datetime) -> datetime:
    """Most recent session-start boundary at or before `now`.

    A session runs 15:00 (California time) through 14:00 the next day. If
    `now` is before today's 15:00 anchor (including the 14:00-15:00 gap
    between sessions), the relevant/most-recent session started yesterday.
    """
    today_anchor = now.replace(hour=SESSION_START_HOUR, minute=0, second=0, microsecond=0)
    return today_anchor if now >= today_anchor else today_anchor - timedelta(days=1)


def filter_rows_by_range(rows: list[dict], range_name: str) -> list[dict]:
    range_name = normalize_range(range_name)
    if range_name == "all":
        return rows

    now = datetime.now()
    start = None
    end = None
    day_match = DAY_RANGE_RE.match(range_name)
    if day_match:
        days = int(day_match.group(1))
        start = session_anchor(now) - timedelta(days=days - 1)
    elif range_name.startswith("custom:"):
        _, start_text, end_text = range_name.split(":")
        start = datetime.strptime(start_text, "%Y-%m-%d")
        end = datetime.strptime(end_text, "%Y-%m-%d") + timedelta(days=1)

    filtered = []
    for row in rows:
        trade_time = parse_trade_time(row.get("time", ""))
        if trade_time is None:
            continue
        if start is not None and trade_time < start:
            continue
        if end is not None and trade_time >= end:
            continue
        filtered.append(row)
    return filtered


def latest_row_time(rows: list[dict]) -> datetime | None:
    latest = None
    for row in rows:
        trade_time = parse_trade_time(row.get("time", ""))
        if trade_time and (latest is None or trade_time > latest):
            latest = trade_time
    return latest


def rows_are_active(rows: list[dict], window_hours: float = ACTIVE_WINDOW_HOURS) -> bool:
    latest = latest_row_time(rows)
    return latest is not None and (datetime.now() - latest) <= timedelta(hours=window_hours)


def parse_vector(raw: str) -> list[float]:
    if "|" in raw:
        raw = raw.split("|", 1)[1]
    values: list[float] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            values.append(parse_number(part))
    return values


def summarize_values(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": 0, "max": 0, "avg": 0, "absAvg": 0, "pos": 0, "neg": 0}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "absAvg": sum(abs(v) for v in values) / len(values),
        "pos": sum(1 for v in values if v > 0),
        "neg": sum(1 for v in values if v < 0),
    }


def parse_symbol_stats(raw: str) -> list[dict]:
    rows = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        symbol, rest = entry.split(":", 1)
        row = {"ticker": symbol.strip().upper(), "bars": 0, "trades": 0, "wins": 0, "losses": 0}
        for field in rest.split(","):
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            if key.strip() in row:
                row[key.strip()] = parse_int(value)
        trades = row["trades"]
        row["winRate"] = (row["wins"] / trades * 100.0) if trades else 0.0
        rows.append(row)
    rows.sort(key=lambda item: (item["trades"], item["bars"], item["ticker"]), reverse=True)
    return rows


def summarize_trade_rows(rows: list[dict], info: dict) -> dict:
    by_ticker: dict[str, dict] = {}
    for row in rows:
        bucket = by_ticker.setdefault(row["ticker"], {
            "ticker": row["ticker"],
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "longTrades": 0,
            "shortTrades": 0,
            "reversals": 0,
            "pnl": 0.0,
        })
        bucket["trades"] += 1
        bucket["wins"] += 1 if row["pnl"] > 0 else 0
        bucket["losses"] += 1 if row["pnl"] < 0 else 0
        bucket["longTrades"] += 1 if row["direction"] == "LONG" else 0
        bucket["shortTrades"] += 1 if row["direction"] == "SHORT" else 0
        bucket["reversals"] += 1 if row.get("reversal") else 0
        bucket["pnl"] += row["pnl"]
    for bucket in by_ticker.values():
        bucket["winRate"] = (bucket["wins"] / bucket["trades"] * 100.0) if bucket["trades"] else 0.0

    wins = sum(1 for row in rows if row["pnl"] > 0)
    losses = sum(1 for row in rows if row["pnl"] < 0)
    return {
        "file": info,
        # 300, not 50: the Trades by Instrument/Direction "Reversals" counts (see
        # build_instrument_summary/build_direction_summary) are computed over full,
        # untruncated history, but the dashboard's trade-filter drawer can only jump
        # to a trade that's actually present in this "rows" payload. 50 was too
        # tight -- a reversal from earlier in the session routinely fell outside the
        # last 50 rows for a strategy, so clicking its count in the summary card
        # found nothing. 300 comfortably covers a full trading session per strategy.
        "rows": rows[-300:][::-1],
        "tickers": sorted(by_ticker.values(), key=lambda item: (item["trades"], item["pnl"], item["ticker"]), reverse=True),
        "totals": {
            "tickers": len(by_ticker),
            "trades": len(rows),
            "wins": wins,
            "losses": losses,
            "winRate": (wins / len(rows) * 100.0) if rows else 0.0,
            "pnl": sum(row["pnl"] for row in rows),
        },
    }


def series_label(bars_type: str, bars_value: str) -> str:
    bars_type = (bars_type or "").strip()
    bars_value = (bars_value or "").strip()
    if not bars_type:
        return "Unknown"
    return f"{bars_type} {bars_value}".strip()


def strip_prefix(code: str, prefix: str) -> str:
    code = code.strip().upper()
    prefix = (prefix or "").strip().upper() + "_"
    return code[len(prefix):] if code.startswith(prefix) else code


def compute_series_time_order(entries: list[dict]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for entry in entries:
        trade_time = parse_trade_time(entry["time"])
        if trade_time is None:
            continue
        minutes = trade_time.hour * 60 + trade_time.minute
        buckets.setdefault(entry["dataSeries"], []).append(minutes)
    return {series: sum(values) / len(values) for series, values in buckets.items() if values}


def build_signal_breakdown(entries: list[dict], range_name: str = "all", series_order: dict[str, float] | None = None) -> list[dict]:
    entries = filter_rows_by_range(entries, range_name)
    grouped: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        code = entry["code"]
        if not code:
            continue
        key = (entry["ticker"], entry["dataSeries"], code)
        bucket = grouped.setdefault(key, {
            "ticker": entry["ticker"],
            "dataSeries": entry["dataSeries"],
            "code": code,
            "meaning": CODE_MEANINGS.get(code, code),
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
        })
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        bucket["pnl"] += entry["pnl"]
    rows = list(grouped.values())
    for row in rows:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
    if series_order:
        rows.sort(key=lambda item: (item["ticker"], series_order.get(item["dataSeries"], 1e9), item["dataSeries"], item["code"]))
    else:
        series_trades: dict[str, int] = {}
        for row in rows:
            series_trades[row["dataSeries"]] = series_trades.get(row["dataSeries"], 0) + row["trades"]
        rows.sort(key=lambda item: (
            -series_trades[item["dataSeries"]],
            item["dataSeries"],
            -item["trades"],
            item["ticker"],
            item["code"],
        ))
    return rows


def extract_tema_signal_entries(path: Path) -> list[dict]:
    entries = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        ticker = parts[1].strip().upper()
        pnl = parse_number(parts[6])
        raw_code = parts[9].strip().upper() if len(parts) > 9 else ""
        code = strip_prefix(raw_code, ticker)
        if len(parts) >= 15:
            data_series = series_label(parts[13], parts[14])
        elif len(parts) >= 12:
            data_series = series_label(parts[10], parts[11])
        else:
            data_series = "Unknown"
        entry_time, account = trailing_entry_time_and_account(parts)
        entries.append({
            "time": parts[0],
            "ticker": ticker,
            "direction": parts[2].strip().upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "dataSeries": data_series,
            "code": code,
            "pnl": pnl,
            "outcome": parts[7].upper() if parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "exitSignal": parts[8],
            "account": format_account(account),
            "entryTime": entry_time,
            "reversal": trailing_reversal_flag(parts),
        })
    return entries


def extract_ticker_prefixed_signal_entries(path: Path) -> list[dict]:
    entries = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        ticker = parts[1].strip().upper()
        pnl = parse_number(parts[6])
        code = strip_prefix(parts[9], ticker)
        data_series = series_label(parts[10], parts[11])
        entry_time, account = trailing_entry_time_and_account(parts)
        entries.append({
            "time": parts[0],
            "ticker": ticker,
            "direction": parts[2].strip().upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "dataSeries": data_series,
            "code": code,
            "pnl": pnl,
            "outcome": parts[7].upper() if parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "exitSignal": parts[8],
            "account": format_account(account),
            "entryTime": entry_time,
            "reversal": trailing_reversal_flag(parts),
        })
    return entries


def extract_multi_data_series_signal_entries(path: Path) -> list[dict]:
    entries = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 13:
            continue
        ticker = parts[1].strip().upper()
        pnl = parse_number(parts[6])
        series_prefix = parts[12].strip().upper()
        code = strip_prefix(parts[9], series_prefix)
        data_series = series_label(parts[10], parts[11])
        entry_time, account = trailing_entry_time_and_account(parts)
        entries.append({
            "time": parts[0],
            "ticker": ticker,
            "direction": parts[2].strip().upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "dataSeries": data_series,
            "code": code,
            "pnl": pnl,
            "outcome": parts[7].upper() if parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "exitSignal": parts[8],
            "account": format_account(account),
            "entryTime": entry_time,
            "reversal": trailing_reversal_flag(parts),
        })
    return entries


def extract_global_spike_signal_entries(path: Path) -> list[dict]:
    entries = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        ticker = parts[1].strip().upper()
        pnl = parse_number(parts[6])
        match = GLOBAL_SPIKE_SIGNAL_RE.match(parts[9].strip().upper())
        if not match:
            continue
        data_series, code = match.group(1), match.group(2)
        entries.append({"time": parts[0], "ticker": ticker, "dataSeries": data_series, "code": code, "pnl": pnl})
    return entries


def trailing_entry_time_and_account(parts: list[str]) -> tuple[str, str]:
    """The last two columns of every completed-trade row are entryTime and account,
    appended after every other (per-strategy-varying) column. Older rows written
    before this was added won't have them, so validate parts[-2] actually parses as
    a timestamp before trusting it -- otherwise treat both as absent."""
    if len(parts) >= 2 and parse_trade_time(parts[-2]) is not None:
        return parts[-2], parts[-1]
    return "", ""


def trailing_reversal_flag(parts: list[str]) -> bool | None:
    """TemaLimit rows carry a "reversal" column just before entryTime/account
    (true/false -- whether the ML entry flipped the default TEMA setup
    direction). Other strategies' logs don't have this column, so only trust
    parts[-3] when it's literally "true"/"false" -- otherwise treat as unknown."""
    if len(parts) >= 3 and parts[-3].strip().lower() in ("true", "false"):
        return parts[-3].strip().lower() == "true"
    return None


def parse_nq_trade_log(range_name: str = "all") -> dict:
    info = file_info(NQ_TRADES)
    rows = []
    text = read_text(NQ_TRADES)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        pnl = parse_number(parts[6])
        entry_time, account = trailing_entry_time_and_account(parts)
        rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "pnl": pnl,
            "outcome": parts[7].upper() if parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "prediction": parse_number(parts[8]),
            "entryTime": entry_time,
            "account": format_account(account),
        })
    parsed = summarize_trade_rows(filter_rows_by_range(rows, range_name), info)
    last_time = latest_row_time(rows)
    parsed["active"] = last_time is not None and (datetime.now() - last_time) <= timedelta(hours=ACTIVE_WINDOW_HOURS)
    parsed["lastTradeTime"] = last_time.isoformat() if last_time else None
    return parsed


def parse_simple_trade_log(path: Path, range_name: str = "all", value_idx: int | None = None, template_idx: int | None = None, type_idx: int | None = None, prediction_idx: int | None = None) -> dict:
    info = file_info(path)
    rows = []
    text = read_text(path)
    # Only temalimit.cs's log (the one with a templateNumber column) has a matching
    # excursion sample to join against -- skip the CSV parse entirely for other logs.
    excursion_index = build_trade_excursion_index() if template_idx is not None else {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        pnl = parse_number(parts[6])
        entry_time, account = trailing_entry_time_and_account(parts)
        series_value = parse_int(parts[value_idx], 0) if value_idx is not None and len(parts) > value_idx else None
        # barsPeriodType (e.g. "Renko") lives one column before barsPeriodValue in
        # the TEMA Limit/Market TSVs -- combine into "Renko 9" etc. (same
        # series_label() used by extract_tema_signal_entries for the data-series
        # donut, so this matches the donut's legend keys exactly) so the trade-
        # filter drawer can show which data series a trade actually ran on instead
        # of the strategy name (which is the same for every row in that drawer and
        # tells the user nothing they don't already know from the drawer's title).
        data_series_label = series_label(parts[type_idx], parts[value_idx]) if type_idx is not None and value_idx is not None and len(parts) > max(type_idx, value_idx) else None
        template_number = parse_int(parts[template_idx], 0) if template_idx is not None and len(parts) > template_idx else None
        excursion = excursion_index.get((parts[1].upper(), template_number, parts[0])) if template_number else None
        # "prediction" is raw model output ("long"/"short") only when the ML gate
        # actually confirmed/reversed the entry; the plain-signal fallback path
        # logs "strategy_long"/"strategy_short" instead. See SubmitMlDirectedEntry
        # / LogMlTradeOutcome in temalimit.cs.
        prediction = parts[prediction_idx] if prediction_idx is not None and len(parts) > prediction_idx else ""
        rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "pnl": pnl,
            "outcome": parts[7].upper() if parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "exitSignal": parts[8],
            "entryTime": entry_time,
            "account": format_account(account),
            "seriesValue": series_value,
            "dataSeriesLabel": data_series_label,
            "reversal": trailing_reversal_flag(parts),
            "templateNumber": template_number,
            "mfePoints": excursion.get("mfePoints") if excursion else None,
            "maePoints": excursion.get("maePoints") if excursion else None,
            "mlGated": prediction in ("long", "short"),
        })
    parsed = summarize_trade_rows(filter_rows_by_range(rows, range_name), info)
    last_time = latest_row_time(rows)
    parsed["active"] = last_time is not None and (datetime.now() - last_time) <= timedelta(hours=ACTIVE_WINDOW_HOURS)
    parsed["lastTradeTime"] = last_time.isoformat() if last_time else None
    return parsed


def parse_tema_limit(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(TEMA_LIMIT_TRADES, range_name, value_idx=14, template_idx=12, type_idx=13, prediction_idx=10)
    return {
        "name": "TEMA Limit",
        "dataSeries": "Primary chart series",
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "signalBreakdown": build_signal_breakdown(extract_tema_signal_entries(TEMA_LIMIT_TRADES), range_name),
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


def parse_tema_market(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(TEMA_MARKET_TRADES, range_name, value_idx=14, template_idx=12, type_idx=13, prediction_idx=10)
    return {
        "name": "TEMA Market",
        "dataSeries": "Primary chart series",
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "signalBreakdown": build_signal_breakdown(extract_tema_signal_entries(TEMA_MARKET_TRADES), range_name),
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


def parse_market_multi_ticker(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(MARKET_MULTI_TICKER_TRADES, range_name, value_idx=11)
    return {
        "name": "marketmultiticker",
        "dataSeries": "Primary chart series + ES/YM/RTY 500 Tick added series",
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "signalBreakdown": build_signal_breakdown(extract_ticker_prefixed_signal_entries(MARKET_MULTI_TICKER_TRADES), range_name),
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


def parse_cerave(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(CERAVE_TRADES, range_name, value_idx=11)
    return {
        "name": "cerave",
        "dataSeries": "Primary chart series + ES/YM/RTY 500 Tick added series",
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "signalBreakdown": build_signal_breakdown(extract_ticker_prefixed_signal_entries(CERAVE_TRADES), range_name),
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


def parse_multi_data_series(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(MULTI_DATA_SERIES_TRADES, range_name, value_idx=11)
    return {
        "name": "multidataseries",
        "dataSeries": "1000 Volume / 5 Minute / 500 Tick / 60 Range added series",
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "signalBreakdown": build_signal_breakdown(extract_multi_data_series_signal_entries(MULTI_DATA_SERIES_TRADES), range_name),
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


def parse_trend_tcn(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(TREND_TCN_TRADES, range_name)
    return {
        "name": "TrendTcnStrategy",
        "dataSeries": "Order Flow Delta",
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


def parse_execution_csv_trade_log(path: Path, name: str, data_series: str, range_name: str = "all") -> dict:
    info = file_info(path)
    rows = []
    previous_cum = None
    text = read_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("timestamputc,"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        cum = parse_number(parts[6])
        pnl = parse_number(parts[5]) if parts[5] else None
        if pnl is None:
            if previous_cum is None:
                previous_cum = cum
                continue
            pnl = cum - previous_cum
        previous_cum = cum
        if abs(pnl) < 0.0000001:
            continue
        action = parts[2].upper()
        direction = "SHORT" if "SHORT" in action or "SELL" in action else "LONG"
        rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": direction,
            "entryPrice": 0.0,
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[3]),
            "pnl": pnl,
            "outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT",
            "exitSignal": parts[2],
            "account": format_account(parts[-1] if len(parts) > 7 else ""),
        })
    parsed = summarize_trade_rows(filter_rows_by_range(rows, range_name), info)
    last_time = latest_row_time(rows)
    return {
        "name": name,
        "dataSeries": data_series,
        "file": parsed["file"],
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "active": last_time is not None and (datetime.now() - last_time) <= timedelta(hours=ACTIVE_WINDOW_HOURS),
        "lastTradeTime": last_time.isoformat() if last_time else None,
    }


def parse_full_twenties(range_name: str = "all") -> dict:
    return parse_execution_csv_trade_log(
        FULL_TWENTIES_TRADES,
        "fulltwenties",
        "NQ/BTC/GC/CL added series execution log",
        range_name,
    )


def parse_twenty_four_seven(range_name: str = "all") -> dict:
    return parse_execution_csv_trade_log(
        TWENTY_FOUR_SEVEN_TRADES,
        "TwentyFourSevenBot",
        "MNQ/MBT/MGC/MCL added series execution log",
        range_name,
    )


def parse_nq(range_name: str = "all") -> dict:
    info = file_info(NQ_WEIGHTS)
    values = parse_key_values(read_text(NQ_WEIGHTS))
    w3 = parse_vector(values.get("W3", ""))
    b3 = parse_vector(values.get("b3", ""))
    output_score = b3[0] if b3 else 0.0
    long_bias = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, output_score))))
    weight_stats = parse_symbol_stats(values.get("symbolStats", ""))
    trade_log = parse_nq_trade_log(range_name)
    trades_by_ticker = {row["ticker"]: row for row in trade_log["tickers"]}
    bars_by_ticker = {row["ticker"]: row["bars"] for row in weight_stats}
    stats = []
    for ticker in sorted(set(bars_by_ticker.keys()) | set(trades_by_ticker.keys())):
        trade_row = trades_by_ticker.get(ticker, {})
        trade_count = trade_row.get("trades", 0)
        wins = trade_row.get("wins", 0)
        losses = trade_row.get("losses", 0)
        stats.append({
            "ticker": ticker,
            "bars": bars_by_ticker.get(ticker, 0),
            "trades": trade_count,
            "wins": wins,
            "losses": losses,
            "winRate": (wins / trade_count * 100.0) if trade_count else 0.0,
            "longTrades": trade_row.get("longTrades", 0),
            "shortTrades": trade_row.get("shortTrades", 0),
            "pnl": trade_row.get("pnl", 0.0),
        })
    stats.sort(key=lambda item: (item["trades"], item["bars"], item["ticker"]), reverse=True)
    totals = {
        "tickers": len(stats),
        "bars": sum(row["bars"] for row in stats),
        "trades": trade_log["totals"]["trades"],
        "wins": trade_log["totals"]["wins"],
        "losses": trade_log["totals"]["losses"],
        "pnl": trade_log["totals"]["pnl"],
    }
    totals["winRate"] = trade_log["totals"]["winRate"]
    return {
        "name": "NQOnlineMLP",
        "file": info,
        "tradeFile": trade_log["file"],
        "version": values.get("version", ""),
        "adamT": parse_int(values.get("adamT", "0")),
        "fCount": parse_int(values.get("fCount", "0")),
        "dataSeries": "Primary chart series + Tick 1 secondary series",
        "totals": totals,
        "symbols": stats,
        "recentTrades": trade_log["rows"],
        "active": trade_log["active"],
        "lastTradeTime": trade_log["lastTradeTime"],
        "weights": {
            "output": summarize_values(w3),
            "bias": output_score,
            "longValue": long_bias,
            "shortValue": 1.0 - long_bias,
            "note": "Completed trades come from NQOnlineMLP_trades.tsv.",
        },
    }


def parse_global(range_name: str = "all") -> dict:
    parsed = parse_simple_trade_log(GLOBAL_SPIKE_TRADES, range_name)
    return {
        "name": "GlobalSpikeMLLadderStrategy",
        "file": parsed["file"],
        "dataSeries": "Primary chart series",
        "totals": parsed["totals"],
        "tickers": parsed["tickers"],
        "recentTrades": parsed["rows"],
        "signalBreakdown": build_signal_breakdown(
            extract_global_spike_signal_entries(GLOBAL_SPIKE_TRADES),
            range_name,
            compute_series_time_order(extract_global_spike_signal_entries(GLOBAL_SPIKE_TRADES)),
        ),
        "active": parsed["active"],
        "lastTradeTime": parsed["lastTradeTime"],
    }


_discover_cache: dict = {"ts": 0.0, "result": []}
_discover_lock = threading.Lock()
_DISCOVER_TTL_SECONDS = 10.0


def discover_auto_strategies() -> list[tuple[str, Path, str]]:
    """Scanning NT_DIR costs ~3000 stat() calls (glob + is_dir on every entry,
    ~140ms each on this machine), and every /api/status request triggered it
    8x (once per collect_all_trade_entries caller just to build the cache
    fingerprint, plus build_auto_strategies) -- ~1.1s of a 1.65s request.
    New strategy files appear rarely, so a 10s TTL is invisible to the user."""
    now = time.time()
    if now - _discover_cache["ts"] < _DISCOVER_TTL_SECONDS:
        return _discover_cache["result"]
    with _discover_lock:
        if now - _discover_cache["ts"] < _DISCOVER_TTL_SECONDS:
            return _discover_cache["result"]
        result = _scan_auto_strategies()
        _discover_cache["result"] = result
        _discover_cache["ts"] = time.time()
        return result


def _scan_auto_strategies() -> list[tuple[str, Path, str]]:
    found: list[tuple[str, Path, str]] = []
    known_files = {p.resolve() for p in KNOWN_TRADE_FILES}
    known_dirs = {p.resolve() for p in KNOWN_TRADE_LOG_DIRS}

    if NT_DIR.is_dir():
        for path in sorted(NT_DIR.glob("*_completed_trades.tsv")):
            if path.resolve() in known_files:
                continue
            name = path.stem
            if name.lower().endswith("_completed_trades"):
                name = name[: -len("_completed_trades")]
            found.append((name, path, "tsv"))

        for sub in sorted(NT_DIR.iterdir()):
            if not sub.is_dir() or sub.resolve() in known_dirs:
                continue
            candidate = sub / "trade_log.csv"
            if candidate.is_file():
                found.append((sub.name, candidate, "csv"))

    return found


def parse_auto_strategy(name: str, path: Path, kind: str, range_name: str = "all") -> dict:
    if kind == "csv":
        result = parse_execution_csv_trade_log(path, name, "Auto-discovered (execution log)", range_name)
    else:
        parsed = parse_simple_trade_log(path, range_name)
        result = {
            "name": name,
            "dataSeries": "Auto-discovered",
            "file": parsed["file"],
            "totals": parsed["totals"],
            "tickers": parsed["tickers"],
            "recentTrades": parsed["rows"],
            "active": parsed["active"],
            "lastTradeTime": parsed["lastTradeTime"],
        }
    result["auto"] = True
    return result


def build_auto_strategies(range_name: str = "all") -> list[dict]:
    return [parse_auto_strategy(name, path, kind, range_name) for name, path, kind in discover_auto_strategies()]


def parse_open_trades() -> dict:
    rows = []
    files = []
    if not NT_DIR.is_dir():
        return {"rows": rows, "files": files, "totals": {"count": 0, "long": 0, "short": 0, "unrealizedPnl": 0.0}}

    for path in sorted(NT_DIR.glob(OPEN_TRADES_GLOB)):
        info = file_info(path)
        info["stale"] = info["exists"] and (time.time() - info["mtime"]) > LIVE_STATUS_STALE_SECONDS
        files.append(info)
        if info["stale"]:
            continue
        text = read_text(path)
        for line in text.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("time\t"):
                continue
            parts = [part.strip() for part in line.split("\t")]
            if len(parts) < 10:
                continue
            bars_type = parts[14] if len(parts) > 14 else ""
            bars_value = parts[15] if len(parts) > 15 else ""
            rows.append({
                "time": parts[0],
                "strategy": parts[1],
                "ticker": parts[2].upper(),
                "direction": parts[3].upper(),
                "quantity": parse_int(parts[4], 1),
                "entryPrice": parse_number(parts[5]),
                "currentPrice": parse_number(parts[6]),
                "unrealizedPnl": parse_number(parts[7]),
                "barsHeld": parse_int(parts[8]),
                "entrySignal": parts[9],
                "account": format_account(parts[10] if len(parts) > 10 else ""),
                "accountRaw": parts[10] if len(parts) > 10 else "",
                "reversal": len(parts) > 11 and parts[11].strip().lower() == "true",
                "templateNumber": parse_int(parts[12]) if len(parts) > 12 else None,
                "entryTime": parts[13] if len(parts) > 13 and parts[13] else parts[0],
                "dataSeries": series_label(bars_type, bars_value) if bars_type else None,
                "file": str(path),
            })

    rows.sort(key=lambda row: parse_trade_time(row.get("time", "")) or datetime.min, reverse=True)
    return {
        "rows": rows,
        "files": files,
        "totals": {
            "count": len(rows),
            "long": sum(1 for row in rows if row["direction"] == "LONG"),
            "short": sum(1 for row in rows if row["direction"] == "SHORT"),
            "unrealizedPnl": sum(row["unrealizedPnl"] for row in rows),
        },
    }


def parse_pending_trades() -> dict:
    """Working (unfilled) limit entry orders -- written by PendingTradeStatusExporter
    while a strategy has a limit order out but no position yet, so the dashboard can
    show what's about to become a trade instead of only what already is one."""
    rows = []
    if not NT_DIR.is_dir():
        return {"rows": rows, "totals": {"count": 0, "long": 0, "short": 0}}

    for path in sorted(NT_DIR.glob(PENDING_TRADES_GLOB)):
        try:
            if (time.time() - path.stat().st_mtime) > LIVE_STATUS_STALE_SECONDS:
                continue
        except FileNotFoundError:
            continue
        text = read_text(path)
        for line in text.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("time\t"):
                continue
            parts = [part.strip() for part in line.split("\t")]
            if len(parts) < 10:
                continue
            bars_type = parts[10] if len(parts) > 10 else ""
            bars_value = parts[11] if len(parts) > 11 else ""
            rows.append({
                "time": parts[0],
                "strategy": parts[1],
                "ticker": parts[2].upper(),
                "direction": parts[3].upper(),
                "quantity": parse_int(parts[4], 1),
                "limitPrice": parse_number(parts[5]),
                "currentPrice": parse_number(parts[6]),
                "account": format_account(parts[7]),
                "accountRaw": parts[7],
                "templateNumber": parse_int(parts[8]) if parts[8] else None,
                "entryTime": parts[9] if parts[9] else parts[0],
                "dataSeries": series_label(bars_type, bars_value) if bars_type else None,
                "file": str(path),
            })

    rows.sort(key=lambda row: parse_trade_time(row.get("time", "")) or datetime.min, reverse=True)
    return {
        "rows": rows,
        "totals": {
            "count": len(rows),
            "long": sum(1 for row in rows if row["direction"] == "LONG"),
            "short": sum(1 for row in rows if row["direction"] == "SHORT"),
        },
    }


PULLBACK_STATE_TICKER_ORDER = ["NQ", "ES", "RTY", "YM"]


def parse_pullback_state() -> list[dict]:
    rows_by_key: dict[str, dict] = {}
    for line in read_text(TEMA_LIMIT_PULLBACK_STATE).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 9:
            continue
        ticker = parts[1].upper()
        data_series = series_label(parts[2], parts[3])
        ratio = parse_number(parts[6], 1.0)
        basePullbackTicks = parse_int(parts[7])
        livePullbackTicks = parse_int(parts[8])
        rows_by_key[ticker + "|" + data_series] = {
            "ticker": ticker,
            "dataSeries": data_series,
            "time": parts[0],
            "atr": parse_number(parts[4]),
            "atrAvg": parse_number(parts[5]),
            "ratio": ratio,
            "basePullbackTicks": basePullbackTicks,
            "livePullbackTicks": livePullbackTicks,
            "deltaTicks": livePullbackTicks - basePullbackTicks,
            "status": "Pullback Reduced" if ratio < 1.0 else "Pullback Increased" if ratio > 1.0 else "Normal",
        }

    def sort_key(row: dict) -> tuple:
        ticker = row["ticker"]
        order = PULLBACK_STATE_TICKER_ORDER.index(ticker) if ticker in PULLBACK_STATE_TICKER_ORDER else len(PULLBACK_STATE_TICKER_ORDER)
        return (order, ticker, row["dataSeries"])

    return sorted(rows_by_key.values(), key=sort_key)


def extract_bartype_account_rows(path: Path, type_idx: int, value_idx: int, template_idx: int | None = None, prediction_idx: int | None = None) -> list[dict]:
    entries = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) <= value_idx:
            continue
        pnl = parse_number(parts[6])
        entry_time, account = trailing_entry_time_and_account(parts)
        # "prediction" is raw model output ("long"/"short") when the ML gate actually
        # confirmed/reversed the entry; the plain-signal fallback path (gate not
        # good_to_use) logs "strategy_long"/"strategy_short" instead, so only the
        # unprefixed values mean a real ML-gated fill. See temalimit.cs's
        # SubmitMlDirectedEntry / LogMlTradeOutcome.
        prediction = parts[prediction_idx] if prediction_idx is not None and len(parts) > prediction_idx else ""
        entries.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "pnl": pnl,
            "outcome": parts[7].upper() if len(parts) > 7 and parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "dataSeries": canonical_bar_type(parts[type_idx]),
            "account": format_account(account),
            "exitSignal": parts[8] if len(parts) > 8 else "",
            "entryTime": entry_time,
            "reversal": trailing_reversal_flag(parts),
            "templateNumber": parse_int(parts[template_idx], 0) if template_idx is not None and len(parts) > template_idx else None,
            "mlGated": prediction in ("long", "short"),
        })
    return entries


def extract_fallback_account_rows(path: Path, label: str, exit_signal_idx: int | None = 8) -> list[dict]:
    entries = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        pnl = parse_number(parts[6])
        entry_time, account = trailing_entry_time_and_account(parts)
        entries.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "entryPrice": parse_number(parts[3]),
            "exitPrice": parse_number(parts[4]),
            "quantity": parse_int(parts[5]),
            "pnl": pnl,
            "outcome": parts[7].upper() if len(parts) > 7 and parts[7] else ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"),
            "dataSeries": label,
            "account": format_account(account),
            "exitSignal": parts[exit_signal_idx] if exit_signal_idx is not None and len(parts) > exit_signal_idx else "",
            "entryTime": entry_time,
        })
    return entries


def extract_execution_csv_account_rows(path: Path, label: str) -> list[dict]:
    entries = []
    previous_cum = None
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("timestamputc,"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        cum = parse_number(parts[6])
        pnl = parse_number(parts[5]) if parts[5] else None
        if pnl is None:
            if previous_cum is None:
                previous_cum = cum
                continue
            pnl = cum - previous_cum
        previous_cum = cum
        if abs(pnl) < 0.0000001:
            continue
        account = parts[8] if len(parts) > 8 else ""
        action = parts[2].upper()
        direction = "SHORT" if "SHORT" in action or "SELL" in action else "LONG"
        entries.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": direction,
            "pnl": pnl,
            "dataSeries": label,
            "account": format_account(account),
            "exitSignal": "",
            "entryTime": "",
        })
    return entries


ACCOUNT_BAR_SOURCES = [
    (TEMA_LIMIT_TRADES, 13, 14, 12, 10),
    (TEMA_MARKET_TRADES, 13, 14, 12, 10),
    (MARKET_MULTI_TICKER_TRADES, 10, 11, None, None),
    (CERAVE_TRADES, 10, 11, None, None),
    (MULTI_DATA_SERIES_TRADES, 10, 11, None, None),
]

ACCOUNT_FALLBACK_TSV_SOURCES = [
    # (path, dataSeries label, exitSignal column index -- NQ's 9th column is
    # "prediction", not exitSignal, so it has no exit-reason data)
    (NQ_TRADES, "Primary chart series + Tick 1 secondary series", None),
    (GLOBAL_SPIKE_TRADES, "Primary chart series", 8),
    (TREND_TCN_TRADES, "Order Flow Delta", 8),
]

ACCOUNT_CSV_SOURCES = [
    (FULL_TWENTIES_TRADES, "NQ/BTC/GC/CL added series execution log"),
    (TWENTY_FOUR_SEVEN_TRADES, "MNQ/MBT/MGC/MCL added series execution log"),
]


_all_trade_entries_cache: dict = {"key": None, "entries": [], "by_range": {}}
_all_trade_entries_lock = threading.Lock()


def _source_file_fingerprint(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return (str(path), None, None)


def _all_trade_entries_fingerprint(auto_strategies: list[tuple[str, Path, str]]) -> tuple:
    parts = [_source_file_fingerprint(path) for path, *_ in ACCOUNT_BAR_SOURCES]
    parts += [_source_file_fingerprint(path) for path, *_ in ACCOUNT_FALLBACK_TSV_SOURCES]
    parts += [_source_file_fingerprint(path) for path, _ in ACCOUNT_CSV_SOURCES]
    parts += [_source_file_fingerprint(path) for _, path, _ in auto_strategies]
    return tuple(parts)


def _build_all_trade_entries(auto_strategies: list[tuple[str, Path, str]]) -> list[dict]:
    entries: list[dict] = []
    for path, type_idx, value_idx, template_idx, prediction_idx in ACCOUNT_BAR_SOURCES:
        entries += extract_bartype_account_rows(path, type_idx, value_idx, template_idx, prediction_idx)
    for path, label, exit_signal_idx in ACCOUNT_FALLBACK_TSV_SOURCES:
        entries += extract_fallback_account_rows(path, label, exit_signal_idx)
    for path, label in ACCOUNT_CSV_SOURCES:
        entries += extract_execution_csv_account_rows(path, label)
    for name, path, kind in auto_strategies:
        if kind == "csv":
            entries += extract_execution_csv_account_rows(path, "Auto-discovered (execution log)")
        else:
            entries += extract_fallback_account_rows(path, "Auto-discovered")
    return entries


def collect_all_trade_entries(range_name: str = "all") -> list[dict]:
    """Every summary builder (instrument/direction/session/exit-signal/template
    usage/series-family) plus the raw accountTrades payload each call this, so
    a single /api/status request called it 7x -- and each call re-read-and-
    reparsed every trade log line from scratch (read_text() caches the raw
    file text, but not the per-line dict-building), which is what made a
    single request take ~14s. Cache the unfiltered ("all") entries keyed on
    every source file's (mtime, size), same pattern as _no_trade_rows_cache,
    so re-parsing only happens once per request cycle when a file actually
    changed, not once per summary. Lock guards it for the same reason as
    _no_trade_rows_lock: concurrent requests racing a cache miss would
    otherwise all redundantly re-parse every log in parallel."""
    auto_strategies = discover_auto_strategies()
    key = _all_trade_entries_fingerprint(auto_strategies)
    if _all_trade_entries_cache["key"] != key:
        with _all_trade_entries_lock:
            if _all_trade_entries_cache["key"] != key:
                _all_trade_entries_cache["entries"] = _build_all_trade_entries(auto_strategies)
                _all_trade_entries_cache["by_range"] = {}
                _all_trade_entries_cache["key"] = key
    # The 7 summary builders per request all ask for the same range, so cache
    # the filtered list too -- range boundaries only move when the wall-clock
    # day/hour rolls over, which the fingerprint won't catch, so bucket the
    # cache by the current hour to bound staleness.
    range_key = (range_name, int(time.time() // 3600))
    by_range = _all_trade_entries_cache["by_range"]
    if range_key not in by_range:
        if len(by_range) > 16:
            by_range.clear()
        by_range[range_key] = filter_rows_by_range(_all_trade_entries_cache["entries"], range_name)
    return by_range[range_key]


TEMA_LIMIT_NO_TRADE_LOG = Path(os.environ.get(
    "TEMA_LIMIT_NO_TRADE_LOG_PATH",
    NT_DIR / "MLService" / "data" / "training_samples.jsonl",
))

# As of 2026-07-17 the ML service reroutes windowless live-veto /log-sample
# rows here instead of training_samples.jsonl (see MLService/service.py
# /log-sample), so live veto counts must come from this file; shadow counts
# still come from TEMA_LIMIT_NO_TRADE_LOG above.
TEMA_LIMIT_VETO_LOG = Path(os.environ.get(
    "TEMA_LIMIT_VETO_LOG_PATH",
    NT_DIR / "MLService" / "data" / "vetoes.jsonl",
))


_no_trade_rows_cache: dict = {"key": None, "rows": []}
_no_trade_rows_lock = threading.Lock()
_veto_rows_cache: dict = {"key": None, "rows": []}
_veto_rows_lock = threading.Lock()
_reassess_log_cache: dict = {"key": None, "days": {}}
_reassess_log_lock = threading.Lock()


def _parse_no_trade_rows() -> list[dict]:
    """The expensive part of count_tema_limit_no_trades() -- each line in
    training_samples.jsonl carries a full ML feature window (this file runs
    ~13.6KB/line, 41MB across ~3000 lines as of 2026-07-09), so json.loads-ing
    every line on every dashboard request/poll cost ~1 second by itself,
    regardless of which range was selected (filtering happens after parsing).
    Cache the parsed (time, source) rows keyed on the file's (mtime, size) --
    same pattern as _model_health_cache below -- so re-parsing only happens
    when TemaLimit actually logs a new sample, not once per request.

    Guarded by a lock (unlike the plain dict caches elsewhere in this file)
    because ThreadingHTTPServer runs one thread per request, and this is the
    one cache expensive enough that multiple browser tabs polling at the
    instant the file changes could otherwise all redundantly re-parse the
    same 41MB file in parallel, turning a ~1s cost into several seconds of
    GIL-serialized duplicate work for whichever request lands last."""
    try:
        stat = TEMA_LIMIT_NO_TRADE_LOG.stat()
        key = (stat.st_mtime, stat.st_size)
    except FileNotFoundError:
        key = None
    if key is not None and _no_trade_rows_cache["key"] == key:
        return _no_trade_rows_cache["rows"]

    with _no_trade_rows_lock:
        if key is not None and _no_trade_rows_cache["key"] == key:
            return _no_trade_rows_cache["rows"]
        return _parse_no_trade_rows_locked(key)


def _parse_no_trade_rows_locked(key) -> list[dict]:
    rows = []
    for line in read_text(TEMA_LIMIT_NO_TRADE_LOG).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("label") != "no_trade":
            continue
        source = (record.get("metadata") or {}).get("source") or "shadow"
        rows.append({"time": record.get("timestamp") or record.get("logged_at") or "", "source": source})

    _no_trade_rows_cache["key"] = key
    _no_trade_rows_cache["rows"] = rows
    return rows


def _parse_veto_rows() -> list[dict]:
    """Same mtime/size-keyed cache pattern as _parse_no_trade_rows, applied to
    vetoes.jsonl (~41MB and growing as of 2026-07-17) so multiple browser tabs
    polling /api/status don't redundantly re-parse it in parallel."""
    try:
        stat = TEMA_LIMIT_VETO_LOG.stat()
        key = (stat.st_mtime, stat.st_size)
    except FileNotFoundError:
        key = None
    if key is not None and _veto_rows_cache["key"] == key:
        return _veto_rows_cache["rows"]

    with _veto_rows_lock:
        if key is not None and _veto_rows_cache["key"] == key:
            return _veto_rows_cache["rows"]
        return _parse_veto_rows_locked(key)


def _parse_veto_rows_locked(key) -> list[dict]:
    rows = []
    for line in read_text(TEMA_LIMIT_VETO_LOG).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("label") != "no_trade":
            continue
        source = (record.get("metadata") or {}).get("source") or "live"
        rows.append({
            "time": record.get("timestamp") or record.get("logged_at") or "",
            "source": source,
            # Always UTC (unlike "time", which prefers the NT-local bar timestamp) so
            # it can be compared unambiguously against VETO_STORM_FIX_UTC below.
            "loggedAtUtc": record.get("logged_at") or "",
        })

    _veto_rows_cache["key"] = key
    _veto_rows_cache["rows"] = rows
    return rows


AUTO_APPLY_SIZING_LOG = BASE_DIR / "auto_apply_sizing.log"

REASSESS_LOG_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] (.*)$")

# Every finding line auto_apply_sizing.py writes starts with one of these prefixes,
# grouped into the same three buckets the dashboard's existing reassess panels use
# (Entry Gate Reassess = gate + expire, Sizing Reassess = risk sizing + slippage,
# pullback/clamp reassess = pullback ratio + ATR clamp).
REASSESS_FINDING_PREFIXES = [
    ("ENTRY GATE", "entryGate"),
    ("ENTRY EXPIRE", "entryGate"),
    ("SIZING", "sizing"),
    ("SLIPPAGE RESERVE", "sizing"),
    ("PULLBACK", "pullback"),
    ("ATR CLAMP", "pullback"),
]


def _reassess_outcome_class(outcome: str | None) -> str:
    """Buckets a finding's outcome line into the classes the card's filter chips use.
    None (no continuation line at all) means the finding was only evaluated in a dry
    run -- it never had an apply attempted."""
    if not outcome:
        return "dry_run"
    head = outcome.split(None, 1)[0].rstrip(":").lower()
    if head in ("applied", "skipped", "rejected"):
        return head
    return "other"


def _parse_reassess_log_locked(key) -> dict[str, dict[str, list[dict]]]:
    days: dict[str, dict[str, list[dict]]] = {}
    pending = None
    for line in read_text(AUTO_APPLY_SIZING_LOG).splitlines():
        m = REASSESS_LOG_LINE_RE.match(line)
        if not m:
            pending = None
            continue
        date_part, time_part, rest = m.groups()
        if not rest.startswith(" "):
            pending = None
            for prefix, category in REASSESS_FINDING_PREFIXES:
                if rest.startswith(prefix):
                    event = {"time": time_part, "text": rest.strip(), "outcome": None, "outcomeClass": "dry_run", "note": None}
                    day = days.setdefault(date_part, {"entryGate": [], "sizing": [], "pullback": []})
                    day[category].append(event)
                    pending = event
                    break
        else:
            # Indented continuation line -- "  applied: X -> Y", "  SKIPPED (...)",
            # "  REJECTED: ...", "  CLAMPED: ..." or "  backup written: ...".
            stripped = rest.strip()
            if pending is None or stripped.startswith("backup written"):
                continue
            if stripped.startswith("CLAMPED"):
                # A clamp is a step-note, not a terminal outcome: the real
                # applied/SKIPPED line follows it. It must NOT claim the outcome
                # slot -- doing so is what hid 2026-07-18's one applied sizing
                # change behind a "CLAMPED ..." label (see ML_SYSTEM_GUIDE).
                if pending["note"] is None:
                    pending["note"] = stripped
                continue
            if pending["outcome"] is None:
                pending["outcome"] = stripped
                pending["outcomeClass"] = _reassess_outcome_class(stripped)
    return days


def _parse_reassess_log() -> dict[str, dict[str, list[dict]]]:
    """Same mtime/size-keyed cache pattern as _parse_veto_rows, applied to
    auto_apply_sizing.log so multiple browser tabs polling /api/status don't
    redundantly re-parse the whole (growing) log on every request."""
    try:
        stat = AUTO_APPLY_SIZING_LOG.stat()
        key = (stat.st_mtime, stat.st_size)
    except FileNotFoundError:
        key = None
    if key is not None and _reassess_log_cache["key"] == key:
        return _reassess_log_cache["days"]

    with _reassess_log_lock:
        if key is not None and _reassess_log_cache["key"] == key:
            return _reassess_log_cache["days"]
        days = _parse_reassess_log_locked(key)
        _reassess_log_cache["key"] = key
        _reassess_log_cache["days"] = days
        return days


def build_reassess_activity() -> dict:
    """Today's temalimit reassess check activity (auto_apply_sizing.log is
    temalimit-only), for the standalone Reassess Activity card: per-category
    counts plus the individual finding events behind each count, most-recent-first."""
    today = datetime.now().strftime("%Y-%m-%d")
    day = _parse_reassess_log().get(today, {"entryGate": [], "sizing": [], "pullback": []})
    events = {cat: list(reversed(day.get(cat, []))) for cat in ("entryGate", "sizing", "pullback")}
    tallies = {cat: len(events[cat]) for cat in events}
    return {"date": today, "tallies": tallies, "events": events}


# 2026-07-17: the windowless-veto rows purged from training_samples.jsonl (see
# ML_SYSTEM_GUIDE.txt changelog) had been silently training as all-zero windows
# labeled no_trade, inflating val_acc and falsely opening the entry-model gate
# for 5 groups (NQ_1MINUTE, NQ_1MINHEIKENASHI, ES_1MINUTE, NQ_500TICK,
# RTY_5RENKO). The honest retrain + service restart at this timestamp collapsed
# 4 of those 5 back to "overfitting" (gate closed) and live vetoes dropped to
# zero across the board. Everything before this line in vetoes.jsonl is that
# incident, not intentional risk filtering -- see memory ml_gate_forensics_2026_07_17.
VETO_STORM_FIX_UTC = "2026-07-17T22:52:00+00:00"


def count_tema_limit_no_trades(range_name: str = "all") -> dict[str, int]:
    """TemaLimit's ML logs one JSON line per no_trade decision. "shadow" = the
    per-template shadow evaluator replaying a signal that was never actually
    gated live, logged to training_samples.jsonl; "live" = a real signal that
    hit the ML gate (status good_to_use) and was actually vetoed, logged to
    vetoes.jsonl since 2026-07-17 (see TEMA_LIMIT_VETO_LOG). Older rows in
    training_samples.jsonl predate the "live" source split and have no
    metadata.source at all -- those are always shadow rows, so default
    unlabeled rows there to "shadow"."""
    counts = {"shadow": 0, "live": 0, "live_since_fix": 0}
    for row in filter_rows_by_range(_parse_no_trade_rows(), range_name):
        counts[row["source"]] = counts.get(row["source"], 0) + 1
    for row in filter_rows_by_range(_parse_veto_rows(), range_name):
        counts[row["source"]] = counts.get(row["source"], 0) + 1
        if row["source"] == "live" and row.get("loggedAtUtc", "") > VETO_STORM_FIX_UTC:
            counts["live_since_fix"] += 1
    return counts


def _bump_template_bucket(bucket: dict, entry: dict) -> None:
    """Tracks per-template trade counts on a summary-table bucket, same
    aggregation build_series_family_summary uses for the account cards'
    per-instrument Template column."""
    template_number = entry.get("templateNumber")
    if template_number is not None:
        templates = bucket.setdefault("_templates", {})
        templates[template_number] = templates.get(template_number, 0) + 1


def _finalize_template_bucket(row: dict) -> None:
    templates = row.pop("_templates", {})
    row["templates"] = sorted(
        ({"label": template, "count": count} for template, count in templates.items()),
        key=lambda item: item["count"],
        reverse=True,
    )


def build_instrument_summary(range_name: str = "all") -> list[dict]:
    entries = collect_all_trade_entries(range_name)
    buckets: dict[str, dict] = {}
    for entry in entries:
        bucket = buckets.setdefault(entry["ticker"], {"ticker": entry["ticker"], "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "reversals": 0, "reversalsPnl": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        if entry.get("reversal"):
            bucket["reversals"] += 1
            bucket["reversalsPnl"] += entry["pnl"]
        bucket["pnl"] += entry["pnl"]
        _bump_template_bucket(bucket, entry)
    rows = list(buckets.values())
    for row in rows:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
        _finalize_template_bucket(row)
    rows.sort(key=lambda r: r["trades"], reverse=True)
    return rows


def build_direction_summary(range_name: str = "all") -> list[dict]:
    entries = collect_all_trade_entries(range_name)
    buckets: dict[str, dict] = {}
    # ML-gated fills (temalimit's entry model actually confirmed/reversed the
    # signal, vs. just submitting the plain technical signal) get their own
    # "(ML)" rows alongside the regular LONG/SHORT rows -- same trade counted
    # in both, since this is a breakout, not a separate category.
    ml_buckets: dict[str, dict] = {}
    for entry in entries:
        direction = entry["direction"] or "UNKNOWN"
        bucket = buckets.setdefault(direction, {"direction": direction, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "reversals": 0, "reversalsPnl": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        bucket["pnl"] += entry["pnl"]
        if entry.get("reversal"):
            bucket["reversals"] += 1
            bucket["reversalsPnl"] += entry["pnl"]
        _bump_template_bucket(bucket, entry)

        if entry.get("mlGated") and direction in ("LONG", "SHORT"):
            ml_direction = f"{direction} (TEMA Limit ML entry)"
            ml_bucket = ml_buckets.setdefault(ml_direction, {"direction": ml_direction, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "reversals": 0, "reversalsPnl": 0.0})
            ml_bucket["trades"] += 1
            ml_bucket["wins"] += 1 if entry["pnl"] > 0 else 0
            ml_bucket["losses"] += 1 if entry["pnl"] < 0 else 0
            ml_bucket["pnl"] += entry["pnl"]
            if entry.get("reversal"):
                ml_bucket["reversals"] += 1
                ml_bucket["reversalsPnl"] += entry["pnl"]
            _bump_template_bucket(ml_bucket, entry)

    rows = list(buckets.values())
    for row in rows:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
        _finalize_template_bucket(row)
    rows.sort(key=lambda r: r["trades"], reverse=True)

    ml_rows = list(ml_buckets.values())
    for row in ml_rows:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
        _finalize_template_bucket(row)
    ml_rows.sort(key=lambda r: r["trades"], reverse=True)
    rows += ml_rows

    no_trade_counts = count_tema_limit_no_trades(range_name)
    if no_trade_counts.get("live"):
        since_fix = no_trade_counts.get("live_since_fix", 0)
        rows.append({
            "direction": "No Trade (TEMA Limit ML, live veto)",
            "trades": no_trade_counts["live"],
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "winRate": None,
            "reversals": 0,
            "templates": [],
            # Nearly all historical live vetoes came from the 2026-07-17 windowless-veto
            # poisoning incident (fake good_to_use status -> majority-class-collapsed
            # models vetoing everything), not intentional risk filtering -- see
            # memory ml_gate_forensics_2026_07_17. Surface the post-fix count so this
            # context isn't re-derived from scratch every time someone looks at this row.
            "note": f"{since_fix:,} since Jul 17 22:52 UTC fix (of {no_trade_counts['live']:,} total)",
        })
    if no_trade_counts.get("shadow"):
        rows.append({
            "direction": "No Trade (TEMA Limit ML, shadow eval)",
            "trades": no_trade_counts["shadow"],
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "winRate": None,
            "reversals": 0,
            "templates": [],
        })
    return rows


REGULAR_SESSION_START_HOUR = 6.5  # 6:30 AM Pacific -- CME equity index RTH open
REGULAR_SESSION_END_HOUR = 13.0  # 1:00 PM Pacific -- CME equity index RTH close


def trade_session(entry_time: str) -> str:
    """Regular = CME equity index RTH (6:30am-1:00pm Pacific, i.e. 9:30am-4:00pm
    ET); everything else (Globex overnight) is Overnight. Falls back to Overnight
    for unparseable timestamps since most trading hours are outside RTH."""
    dt = parse_trade_time(entry_time)
    if dt is None:
        return "Overnight"
    hour = dt.hour + dt.minute / 60.0
    if REGULAR_SESSION_START_HOUR <= hour < REGULAR_SESSION_END_HOUR:
        return "Regular"
    return "Overnight"


def build_session_summary(range_name: str = "all") -> list[dict]:
    entries = collect_all_trade_entries(range_name)
    buckets: dict[str, dict] = {}
    for entry in entries:
        session = trade_session(entry.get("entryTime", ""))
        bucket = buckets.setdefault(session, {"session": session, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "reversals": 0, "reversalsPnl": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        bucket["pnl"] += entry["pnl"]
        if entry.get("reversal"):
            bucket["reversals"] += 1
            bucket["reversalsPnl"] += entry["pnl"]
        _bump_template_bucket(bucket, entry)
    rows = list(buckets.values())
    for row in rows:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
        _finalize_template_bucket(row)
    rows.sort(key=lambda r: r["trades"], reverse=True)
    return rows


def build_exit_signal_summary(range_name: str = "all") -> list[dict]:
    """Groups by exitSignal (why a trade closed -- stop loss, an ML exit, a
    named template rule, etc). Only populated for sources that log a real
    exit reason (see ACCOUNT_FALLBACK_TSV_SOURCES); NQ and the execution-CSV
    strategies don't, so their trades are excluded rather than mislabeled."""
    entries = collect_all_trade_entries(range_name)
    buckets: dict[str, dict] = {}
    for entry in entries:
        exit_signal = (entry.get("exitSignal") or "").strip()
        if not exit_signal:
            continue
        bucket = buckets.setdefault(exit_signal, {"exitSignal": exit_signal, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        bucket["pnl"] += entry["pnl"]
        _bump_template_bucket(bucket, entry)
    rows = list(buckets.values())
    for row in rows:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
        _finalize_template_bucket(row)
    rows.sort(key=lambda r: r["trades"], reverse=True)
    return rows


def build_equity_curve(range_name: str = "all") -> list[dict]:
    """Cumulative realized PnL over time, all accounts/strategies pooled,
    in trade-close order. One point per completed trade -- fine at current
    volumes; a large history would want downsampling before charting."""
    entries = collect_all_trade_entries(range_name)
    timed = [e for e in entries if parse_trade_time(e["time"]) is not None]
    timed.sort(key=lambda e: parse_trade_time(e["time"]))
    points = []
    running = 0.0
    for entry in timed:
        running += entry["pnl"]
        points.append({
            "time": entry["time"],
            "pnl": entry["pnl"],
            "cumPnl": running,
            "ticker": entry["ticker"],
        })
    return points


def _model_health_sort_key(group: dict) -> tuple:
    """good_to_use groups first (so the green chips are visible without
    scrolling), then alphabetical by symbol/data-series within that bucket."""
    return (group["status"] != "good_to_use", group["symbol"], group["dataSeriesKey"])


MODEL_HEALTH_URL = os.environ.get("MODEL_HEALTH_URL", "http://localhost:8765/model-health")
MODEL_HEALTH_CACHE_SECONDS = 15.0
# Raw /model-health payload cache, shared by build_model_health() (entry_groups)
# and build_template_model_health() (template_groups) below -- both keys come
# back on the one MLService response, so fetching it twice per poll cycle
# would double the load on an endpoint already documented as 10-15s slow.
_model_health_payload_cache: dict = {"time": 0.0, "payload": None}
_model_health_cache: dict = {"time": 0.0, "data": {"available": False, "groups": []}}
_template_model_health_cache: dict = {"time": 0.0, "data": {"available": False, "groups": []}}


def _fetch_model_health_payload() -> "dict | None":
    """Fetches and caches the raw /model-health response. Returns None on
    failure (down/timeout/malformed) so callers can fall back to their own
    last-known-good group list rather than failing the whole /api/status
    response. Cached briefly since the dashboard polls every second and model
    status only changes on retrain, not tick-by-tick."""
    now = time.monotonic()
    if now - _model_health_payload_cache["time"] < MODEL_HEALTH_CACHE_SECONDS:
        return _model_health_payload_cache["payload"]

    try:
        # /model-health recomputes stats for every symbol/data-series group
        # synchronously on each call (engine.all_group_health()) -- as the number of
        # groups and their sample files has grown, this now regularly takes 10-15s.
        # A short timeout here just meant every poll timed out before MLService could
        # ever finish, permanently showing "offline" even though the service was fine.
        request = urllib.request.Request(MODEL_HEALTH_URL, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, OSError):
        _model_health_payload_cache["time"] = now
        _model_health_payload_cache["payload"] = None
        return None

    _model_health_payload_cache["time"] = now
    _model_health_payload_cache["payload"] = payload
    return payload


def _groups_from_model_health(payload_key: str, cache: dict) -> dict:
    """Shared extraction for build_model_health()/build_template_model_health():
    same status classification (warming_up/caution/overfitting/good_to_use)
    for whichever group dict key is requested from the shared payload. A
    single slow/dropped request shouldn't blank either panel -- each caller
    keeps its own last-known groups (marked available) and just skips the
    refresh until the next successful poll. Only reports "unreachable" if
    that panel has never gotten a good response at all."""
    payload = _fetch_model_health_payload()
    if payload is None:
        if not cache["data"]["groups"]:
            cache["data"] = {"available": False, "groups": []}
        return cache["data"]

    raw_groups = payload.get(payload_key) or {}
    groups = []
    for key, info in raw_groups.items():
        groups.append({
            "group": info.get("group", key),
            "symbol": info.get("symbol", ""),
            "dataSeriesKey": info.get("data_series_key", ""),
            "status": info.get("status", "warming_up"),
            "samples": info.get("samples", 0),
            "warmupRemaining": info.get("warmup_remaining", 0),
            "modelReady": bool(info.get("model_ready", False)),
            "valAcc": info.get("val_acc"),
            "testAcc": info.get("test_acc"),
        })
    groups.sort(key=_model_health_sort_key)
    cache["data"] = {"available": True, "groups": groups}
    return cache["data"]


def build_model_health() -> dict:
    """Live entry-model status per (symbol, data series) group -- same status
    classification that gates real entries in temalimit.cs, surfaced here
    instead of only in NinjaTrader's Output tab."""
    return _groups_from_model_health("entry_groups", _model_health_cache)


def build_template_model_health() -> dict:
    """Live template-selection-model status per (symbol, data series) group --
    the ML Template Selection model added 2026-07-15 (temalimit.cs
    EnableMlTemplateSelection / MLService /predict-template). Same shape and
    status classification as build_model_health(), sourced from the same
    /model-health payload's template_groups key."""
    return _groups_from_model_health("template_groups", _template_model_health_cache)


TREND_MODEL_HEALTH_URL = os.environ.get("TREND_MODEL_HEALTH_URL", "http://localhost:8767/trend-stats")
TREND_MODEL_HEALTH_CACHE_SECONDS = 15.0
_trend_model_health_cache: dict = {"time": 0.0, "data": {"available": False, "groups": []}}


def build_trend_model_health() -> dict:
    """Same shape and status classification as build_model_health() above,
    but sourced from MLService_Trend's /trend-stats (port 8767) instead of
    MLService's /model-health (port 8765) -- gates TrendTCN groups the same
    way temalimit.cs's entry groups are gated. Cached/fault-tolerant for the
    same reasons: /trend-stats recomputes health per group synchronously and
    can be slow, and a dropped request shouldn't blank the panel."""
    now = time.monotonic()
    if now - _trend_model_health_cache["time"] < TREND_MODEL_HEALTH_CACHE_SECONDS:
        return _trend_model_health_cache["data"]

    try:
        request = urllib.request.Request(TREND_MODEL_HEALTH_URL, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, OSError):
        _trend_model_health_cache["time"] = now
        if not _trend_model_health_cache["data"]["groups"]:
            _trend_model_health_cache["data"] = {"available": False, "groups": []}
        return _trend_model_health_cache["data"]

    trend_groups = payload.get("groups") or {}
    groups = []
    for key, info in trend_groups.items():
        groups.append({
            "group": info.get("group", key),
            "symbol": info.get("symbol", ""),
            "dataSeriesKey": info.get("data_series_key", ""),
            "status": info.get("status", "warming_up"),
            "samples": info.get("samples", 0),
            "warmupRemaining": info.get("warmup_remaining", 0),
            "modelReady": bool(info.get("model_ready", False)),
            "valAcc": info.get("val_acc"),
            "testAcc": info.get("test_acc"),
        })
    groups.sort(key=_model_health_sort_key)
    _trend_model_health_cache["time"] = now
    _trend_model_health_cache["data"] = {"available": True, "groups": groups}
    return _trend_model_health_cache["data"]


TEMPLATE_REFERENCE_PATH = Path(os.environ.get(
    "TEMA_LIMIT_TEMPLATE_REFERENCE_PATH",
    NT_DIR / "temalimit_template_reference.json",
))


def read_template_reference() -> dict:
    """temalimit.cs writes this once per NinjaTrader process (on first
    State.DataLoaded) with the currently-compiled per-(template, instrument)
    Risk1R/ladder/slippage table. Read fresh every call, no caching -- these
    values only change when the strategy itself is edited and reloaded, but
    when they do change we want the dashboard to reflect it immediately
    rather than serve a stale snapshot."""
    text = read_text(TEMPLATE_REFERENCE_PATH)
    if not text:
        return {"tickers": [], "templates": []}
    try:
        return json.loads(text)
    except ValueError:
        return {"tickers": [], "templates": []}


def build_template_risk_map() -> dict:
    """Risk1R/ladderDaily/slippage per (ticker, template), nested ticker ->
    template# (string) -> values, for every template whether or not it has
    ever traded -- lets the dashboard show 'what this template WOULD risk'
    even for combos with zero trades."""
    reference = read_template_reference()
    result: dict[str, dict[str, dict]] = {}
    for template_info in reference.get("templates", []):
        template_num = str(template_info.get("template"))
        risk = template_info.get("risk") or {}
        for ticker, values in risk.items():
            result.setdefault(ticker, {})[template_num] = {
                "risk1R": values.get("risk1R"),
                "ladderDaily": values.get("ladderDaily"),
                "slippage": values.get("slippage"),
            }
    return result


def build_template_pullback_map() -> dict:
    """pullbackTicksByTicker per (ticker, template), nested ticker -> template# (string) ->
    ticks -- same source/shape as build_template_risk_map, just the pullback field instead
    of risk. Lets build_nofill_stats show each bucket's live pullback distance without
    duplicating temalimit.cs's PullbackTicksForTicker logic in Python."""
    reference = read_template_reference()
    result: dict[str, dict[str, int]] = {}
    for template_info in reference.get("templates", []):
        template_num = str(template_info.get("template"))
        by_ticker = template_info.get("pullbackTicksByTicker") or {}
        for ticker, ticks in by_ticker.items():
            result.setdefault(ticker, {})[template_num] = ticks
    return result


# Standard CME point values; used to convert Risk1R dollars <-> stop distance in points for the
# ladder-trail diagnosis below. Not read from temalimit.cs -- these don't change.
FUTURES_POINT_VALUE = {"ES": 50.0, "NQ": 20.0, "RTY": 50.0, "YM": 5.0}


def build_trade_excursion_index() -> dict:
    """(ticker, templateNumber, time) -> {mfePoints, maePoints}, same exact join key as
    read_completed_trades_index() above. Lets the trade-chart modal show MFE/MAE immediately
    from temalimit.cs's own live-computed excursion (already logged here for the Risk1R
    sizing table) instead of deriving it from the chart's price bars -- which stays blank
    whenever ChartDataExporter/NinjaTrader's BarsRequest fails to come back."""
    index: dict[tuple[str, int, str], dict] = {}
    if not TEMA_LIMIT_TEMPLATE_LIVE_SAMPLES.exists():
        return index
    with open(TEMA_LIMIT_TEMPLATE_LIVE_SAMPLES, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("shadow", "").strip().lower() != "false":
                continue
            ticker = (row.get("symbol") or "").upper()
            template_num = parse_int(row.get("template_number"), 0)
            time_key = row.get("resolved_timestamp")
            if not ticker or template_num <= 0 or not time_key:
                continue
            mfe_points = parse_number(row.get("mfe_points"))
            mae_points = parse_number(row.get("mae_points"))
            if (mfe_points and mfe_points > IMPLAUSIBLE_EXCURSION_POINTS) or (mae_points and mae_points > IMPLAUSIBLE_EXCURSION_POINTS):
                continue
            index[(ticker, template_num, time_key)] = {"mfePoints": mfe_points, "maePoints": mae_points}
    return index


def read_completed_trades_index() -> dict:
    """(ticker, templateNumber, time) -> exitSignal, keyed on the exact fillTime string both
    AppendDashboardTradeOutcome (this TSV's "time" column) and LogLiveTemplateSample (the samples
    CSV's "resolved_timestamp") are given -- same call, same DateTime, same ToString("o"), so this
    is an exact join key, not a fuzzy one."""
    index: dict[tuple[str, int, str], str] = {}
    for line in read_text(TEMA_LIMIT_TRADES).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) <= 12:
            continue
        index[(parts[1].upper(), parse_int(parts[12], 0), parts[0])] = parts[8]
    return index


# Same order of magnitude as temalimit.cs's MaxPlausiblePointsExcursion clamp -- flags historical
# rows logged before that fix existed (e.g. the ES row that showed 22090.75 mae_points).
IMPLAUSIBLE_EXCURSION_POINTS = 2000.0
# A run-up under this many points is noise, not a real move -- dividing capture-ratio by it (or
# treating a loss with this little MFE as a "reversal") produces meaningless numbers.
MIN_REAL_MFE_POINTS = 10.0
MIN_SAMPLES_FOR_REASSESS = 5


def build_ladder_trail_diagnosis(sizing_since: dict | None = None) -> dict:
    """For live (non-shadow) template samples, splits into winner-capture-% and dollar-framed
    reversals (a real run-up that closed at a loss) rather than one blended average -- averaging
    a 65% win with a -785% near-zero-MFE loss produces a number that describes no real trade.
    Also checks, for samples that exited via 'Stop loss', whether the exit landed at the original
    (untrailed) stop distance or closer -- i.e. whether LadderDaily's profit-lock ladder ever
    actually trailed the stop in before the reversal caught it.

    sizing_since: optional {(ticker, role, tier) -> timestamp} last-applied cutoffs (roles
    "risk1R"/"ladderDaily", tiers "tier1"/"tier2"). auto_apply passes them because the ladder
    +/-15% suggestion is multiplicative off the CURRENT value -- with all-time evidence, one
    historical reversal re-proposes another -15% every run after every apply, compounding until
    the curve invariants finally reject it. Each role's evidence rows are filtered by that role's
    own cutoff; the trail/flagged counters stay unfiltered (display only). The dashboard omits
    this, so the card still shows all evidence.
    Returns ticker -> template# (string) -> {
        stopLossExits, trailedIn, neverTrailed,
        winnerCapturePct, winnerCount, reversals: [{giveback, peak}], flagged,
        reassessTier: "flagged" | "too_few" | "monitor" | "reassess",
    }."""
    if not TEMA_LIMIT_TEMPLATE_LIVE_SAMPLES.exists():
        return {}

    trades_index = read_completed_trades_index()
    risk_map = build_template_risk_map()
    sizing_history = _history_index(read_auto_apply_history(), "sizing", ("ticker", "role", "tier"))
    buckets: dict[tuple[str, str], dict] = {}

    with open(TEMA_LIMIT_TEMPLATE_LIVE_SAMPLES, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("shadow", "").strip().lower() != "false":
                continue
            ticker = (row.get("symbol") or "").upper()
            point_value = FUTURES_POINT_VALUE.get(ticker)
            template_num = parse_int(row.get("template_number"), 0)
            if not ticker or not point_value or template_num <= 0:
                continue

            key = (ticker, str(template_num))
            bucket = buckets.setdefault(key, {
                "ticker": ticker, "template": template_num,
                "stopLossExits": 0, "trailedIn": 0, "neverTrailed": 0,
                "flagged": False,
                "_winnerCaptures": [], "_reversals": [], "_maeValues": [], "_maeSamples": [], "_ladderSamples": [],
            })

            mfe_points = parse_number(row.get("mfe_points"))
            mae_points = parse_number(row.get("mae_points"))
            dollars = parse_number(row.get("dollars"))

            if (mfe_points and mfe_points > IMPLAUSIBLE_EXCURSION_POINTS) or (mae_points and mae_points > IMPLAUSIBLE_EXCURSION_POINTS):
                bucket["flagged"] = True
                continue

            # Per-role evidence freshness (see the sizing_since docstring above): risk evidence
            # (MAE) and ladder evidence (winners/reversals) reset independently at their own
            # last-applied timestamps.
            row_tier = sizing_tier_group(template_num)
            row_time = row.get("resolved_timestamp")
            risk_fresh = sizing_since is None or _after_cutoff(row_time, sizing_since.get((ticker, "risk1R", row_tier)))
            ladder_fresh = sizing_since is None or _after_cutoff(row_time, sizing_since.get((ticker, "ladderDaily", row_tier)))

            # MAE is meaningful for every trade regardless of how it eventually closed, not just
            # stop-loss exits -- it's the basis for the Risk1R suggestion below.
            if risk_fresh and mae_points is not None and mae_points >= 0:
                bucket["_maeValues"].append(mae_points)
                is_win = (row.get("win") or "").strip().lower() == "true"
                outcome = "win" if is_win else ("reversal" if mfe_points and mfe_points >= MIN_REAL_MFE_POINTS else "loss")
                bucket["_maeSamples"].append({
                    "time": row.get("resolved_timestamp"),
                    "direction": (row.get("setup_direction") or "").upper(),
                    "maePoints": mae_points,
                    "outcome": outcome,
                    "entryPrice": parse_number(row.get("entry_price")),
                })

            if ladder_fresh and mfe_points and mfe_points >= MIN_REAL_MFE_POINTS:
                sample_time = row.get("resolved_timestamp")
                sample_direction = (row.get("setup_direction") or "").upper()
                if dollars is not None and dollars > 0:
                    capture_pct = (dollars / (mfe_points * point_value)) * 100.0
                    bucket["_winnerCaptures"].append(capture_pct)
                    bucket["_ladderSamples"].append({
                        "time": sample_time, "direction": sample_direction, "type": "winner",
                        "capturePct": round(capture_pct, 1),
                    })
                elif dollars is not None:
                    peak_dollars = mfe_points * point_value
                    bucket["_reversals"].append({
                        "giveback": round(peak_dollars - dollars, 2),
                        "peak": round(peak_dollars, 2),
                    })
                    bucket["_ladderSamples"].append({
                        "time": sample_time, "direction": sample_direction, "type": "reversal",
                        "giveback": round(peak_dollars - dollars, 2), "peak": round(peak_dollars, 2),
                    })

            exit_signal = trades_index.get((ticker, template_num, row.get("resolved_timestamp", "")))
            if not exit_signal or "stop loss" not in exit_signal.lower():
                continue

            risk_dollars = (risk_map.get(ticker, {}).get(str(template_num)) or {}).get("risk1R")
            entry_price = parse_number(row.get("entry_price"))
            exit_price = parse_number(row.get("exit_price"))
            if not risk_dollars or entry_price is None or exit_price is None:
                continue

            original_stop_points = risk_dollars / point_value
            actual_exit_points = abs(exit_price - entry_price)
            bucket["stopLossExits"] += 1
            # 5% tolerance for tick rounding -- a stop that fires at ~full original distance never trailed.
            if actual_exit_points < original_stop_points * 0.95:
                bucket["trailedIn"] += 1
            else:
                bucket["neverTrailed"] += 1

    result: dict[str, dict[str, dict]] = {}
    for (ticker, template_key), bucket in buckets.items():
        winners = bucket.pop("_winnerCaptures")
        reversals = bucket.pop("_reversals")
        mae_values = bucket.pop("_maeValues")
        mae_samples = bucket.pop("_maeSamples")
        ladder_samples = bucket.pop("_ladderSamples")
        point_value = FUTURES_POINT_VALUE.get(ticker, 1.0)

        bucket["winnerCapturePct"] = round(sum(winners) / len(winners), 1) if winners else None
        bucket["winnerCount"] = len(winners)
        bucket["reversals"] = reversals
        bucket["ladderSamples"] = sorted(ladder_samples, key=lambda s: s["time"] or "", reverse=True)

        qualifying = len(winners) + len(reversals)
        if bucket["flagged"]:
            bucket["reassessTier"] = "flagged"
        elif qualifying >= MIN_SAMPLES_FOR_REASSESS:
            bucket["reassessTier"] = "reassess"
        elif qualifying >= 1:
            bucket["reassessTier"] = "monitor"
        else:
            bucket["reassessTier"] = "too_few"

        # LadderDaily direction: a reliable giveback pattern (any real reversal, or capture
        # consistently under half) means the ladder isn't locking in soon enough -- tighten it.
        # Consistently high capture with zero reversals means there may be room to loosen it.
        # Magnitude is a heuristic nudge (+/-15%), same caveat as the pullback-tighten side: we
        # have evidence of direction, not a measured "how much" the way MAE gives Risk1R.
        current_ladder = (risk_map.get(ticker, {}).get(template_key) or {}).get("ladderDaily")
        bucket["ladderDirection"] = None
        bucket["suggestedLadderDaily"] = None
        bucket["ladderLastApplied"] = _last_applied(sizing_history, (ticker, "ladderDaily", sizing_tier_group(int(template_key))))
        if bucket["reassessTier"] == "reassess" and current_ladder:
            if len(reversals) >= 1 or (bucket["winnerCapturePct"] is not None and bucket["winnerCapturePct"] < 50):
                bucket["ladderDirection"] = "decrease"
                bucket["suggestedLadderDaily"] = round(current_ladder * 0.85, 2)
            elif bucket["winnerCapturePct"] is not None and bucket["winnerCapturePct"] > 90 and not reversals:
                bucket["ladderDirection"] = "increase"
                bucket["suggestedLadderDaily"] = round(current_ladder * 1.15, 2)

        # Risk1R suggestion: aim for the stop to sit 30% above average MAE. Only proposes a
        # direction when that target differs from the current Risk1R by more than 10%, so it
        # doesn't flip-flop over noise near the boundary.
        current_risk = (risk_map.get(ticker, {}).get(template_key) or {}).get("risk1R")
        bucket["avgMaePoints"] = round(sum(mae_values) / len(mae_values), 1) if mae_values else None
        bucket["maeSampleCount"] = len(mae_values)
        bucket["currentRisk1R"] = current_risk
        bucket["maeSamples"] = sorted(mae_samples, key=lambda s: s["time"] or "", reverse=True)
        bucket["riskDirection"] = None
        bucket["suggestedRisk1R"] = None
        bucket["riskLastApplied"] = _last_applied(sizing_history, (ticker, "risk1R", sizing_tier_group(int(template_key))))
        if bucket["flagged"]:
            bucket["riskReassessTier"] = "flagged"
        elif len(mae_values) >= MIN_SAMPLES_FOR_REASSESS:
            bucket["riskReassessTier"] = "reassess"
            if current_risk and bucket["avgMaePoints"] is not None:
                target_risk = round(bucket["avgMaePoints"] * 1.3 * point_value, 2)
                if target_risk < current_risk * 0.9:
                    bucket["riskDirection"] = "decrease"
                    bucket["suggestedRisk1R"] = target_risk
                elif target_risk > current_risk * 1.1:
                    bucket["riskDirection"] = "increase"
                    bucket["suggestedRisk1R"] = target_risk
        elif len(mae_values) >= 1:
            bucket["riskReassessTier"] = "monitor"
        else:
            bucket["riskReassessTier"] = "too_few"

        # A tier-2 decrease whose target sits below the fixed T19 endpoint can't be fully
        # expressed by the Tier2Target automation (the curve would slope downward), so
        # auto_apply_sizing.py clamps it to the invariant floor -- flag it so the frontend
        # can say the remainder needs tier-1 evidence instead of looking silently stuck.
        if int(template_key) > SIZING_TIER1_MAX_TEMPLATE:
            t19 = risk_map.get(ticker, {}).get(str(SIZING_TIER1_MAX_TEMPLATE)) or {}
            bucket["riskTier2FloorBlocked"] = bool(
                bucket["riskDirection"] == "decrease" and bucket["suggestedRisk1R"] is not None
                and t19.get("risk1R") and bucket["suggestedRisk1R"] < t19["risk1R"])
            bucket["ladderTier2FloorBlocked"] = bool(
                bucket.get("ladderDirection") == "decrease" and bucket.get("suggestedLadderDaily") is not None
                and t19.get("ladderDaily") and bucket["suggestedLadderDaily"] < t19["ladderDaily"])

        result.setdefault(ticker, {})[template_key] = bucket
    return result


MIN_SAMPLES_FOR_PULLBACK_REASSESS = 5


def _after_cutoff(row_time, cutoff) -> bool:
    """True when the row postdates the cutoff (or there is no cutoff). Both sides normalize to a
    lexically comparable "YYYY-MM-DDTHH:MM:SS" prefix: row times are full ISO with offset, history
    timestamps are isoformat(seconds)."""
    return not cutoff or str(row_time or "")[:19] > str(cutoff)[:19]


def build_template_fill_counts(range_name: str = "all", since: dict | None = None, tier_fn=None) -> dict:
    """ticker -> template# (string) -> count of completed (filled) trades -- the "reliably fills
    with room to spare" evidence for the pullback-tighten direction below.

    since: optional {(ticker, tierGroup) -> timestamp} (tierGroup from tier_fn, default
    pullback_tier_group) -- fills at or before that (instrument, tier)'s cutoff are dropped.
    auto_apply's heuristic *increase*/tighten directions must re-earn their fill evidence after
    each applied change; counting all-time fills re-arms the same fixed step on the very next
    run (the cutoff that resets the no-fill side would otherwise guarantee it: 0 fresh no-fills
    + >=5 historical fills is exactly the increase condition)."""
    counts: dict[str, dict[str, int]] = {}
    group = tier_fn or pullback_tier_group
    for row in read_tema_limit_template_rows(range_name):
        if since and not _after_cutoff(row["time"], since.get((row["ticker"], group(row["template"])))):
            continue
        by_template = counts.setdefault(row["ticker"], {})
        key = str(row["template"])
        by_template[key] = by_template.get(key, 0) + 1
    return counts


def pullback_reassess(current_ticks, missed_values: list[float], fill_count: int) -> dict:
    """Bidirectional pullback suggestion + reassess tier for one Template x Instrument bucket.

    Decrease is data-derived from temalimit.cs's real missedByTicks measurements
    (entryOrderClosestApproachPrice vs the limit price) -- how many ticks short each no-fill
    actually missed by. Increase is a heuristic, not measured: a bucket that reliably fills with
    ~zero no-fills has no data on how much tighter it could safely be, so it only ever proposes a
    fixed conservative nudge (+15%, min 1 tick) -- weaker evidence than the decrease side, and the
    frontend should say so, not present both as equally certain.

    Returns {direction: "decrease"|"increase"|None, avgMissedByTicks, suggestedPullbackTicks, tier}
    where tier is "too_few" | "monitor" | "reassess" (never "flagged" -- no corruption concept here)."""
    # True no-fill event count for this bucket, regardless of whether missedByTicks was logged for
    # each one (older rows predate that field) -- a bucket with 27 no-fills is NOT a "zero friction,
    # safe to tighten" candidate just because none of those 27 rows happen to have measured misses.
    no_fill_count = len(missed_values)
    values = [v for v in missed_values if v is not None]
    real_sample_count = len(values)  # rows that actually HAVE a measured miss -- what avg_missed is built from
    avg_missed = round(sum(values) / len(values), 1) if values else None

    if current_ticks is None or current_ticks <= 0:
        return {"direction": None, "avgMissedByTicks": avg_missed, "suggestedPullbackTicks": None, "tier": "too_few"}

    # Gated on real_sample_count, not no_fill_count -- a bucket can have plenty of no-fill EVENTS
    # while most of them predate the missedByTicks field, leaving too few MEASURED misses to trust
    # the average. (Found 2026-07-17: a bucket with 19 no-fills but only 1 real sample passed the
    # old no_fill_count-based gate and produced a suggestion built on n=1.)
    if real_sample_count >= MIN_SAMPLES_FOR_PULLBACK_REASSESS and avg_missed and avg_missed > 0:
        suggested = max(1, current_ticks - math.ceil(avg_missed))
        return {"direction": "decrease", "avgMissedByTicks": avg_missed, "suggestedPullbackTicks": suggested, "tier": "reassess"}

    if no_fill_count <= 1 and fill_count >= MIN_SAMPLES_FOR_PULLBACK_REASSESS:
        suggested = current_ticks + max(1, round(current_ticks * 0.15))
        return {"direction": "increase", "avgMissedByTicks": avg_missed, "suggestedPullbackTicks": suggested, "tier": "reassess"}

    if no_fill_count >= 1 or fill_count >= 1:
        return {"direction": None, "avgMissedByTicks": avg_missed, "suggestedPullbackTicks": None, "tier": "monitor"}

    return {"direction": None, "avgMissedByTicks": avg_missed, "suggestedPullbackTicks": None, "tier": "too_few"}


def build_template_indicator_params() -> dict:
    """TEMA length / Bollinger length+stddev per template number (1-40) -- these come
    straight out of temalimit_template_reference.json's DerivedTemaLength/DerivedBbLength/
    DerivedBbStdDev output (temalimit.cs writes them once per NinjaTrader process; see
    read_template_reference()). Unlike risk1R, these don't vary by ticker."""
    reference = read_template_reference()
    result: dict[str, dict] = {}
    for template_info in reference.get("templates", []):
        template_num = str(template_info.get("template"))
        result[template_num] = {
            "temaLength": template_info.get("temaLength"),
            "bbLength": template_info.get("bbLength"),
            "bbStdDev": template_info.get("bbStdDev"),
        }
    return result


LADDER_ANCHORS = {
    1: 0.50, 2: 1.40, 3: 2.25, 4: 3.08, 5: 3.95, 6: 4.86, 7: 5.81, 8: 6.72,
    9: 7.65, 10: 8.60, 11: 9.57, 12: 10.56, 13: 11.57, 14: 12.60, 15: 13.65,
    16: 14.72, 17: 15.81, 18: 16.92, 19: 18.05, 20: 19.20, 21: 20.37,
    22: 21.56, 23: 22.77,
}

# Ported verbatim from "profit-ladder-split-risk.html"'s rows array/getLockedR
# so the dashboard's ladder table matches that reference tool exactly.
LADDER_ROWS = [
    ("< 0.50R", 0.25, "phase"),
    ("0.50R to < 0.75R", 0.625, "phase"),
    ("0.75R", 0.75, "early"),
    ("1.00R", 1.00, "anchor"),
    ("1.25R", 1.25, "interp"),
    ("1.50R", 1.50, "interp"),
    ("1.75R", 1.75, "interp"),
    ("2.00R", 2.00, "anchor"),
    ("2.50R", 2.50, "interp"),
    ("3.00R", 3.00, "anchor"),
    ("3.50R", 3.50, "interp"),
    ("4.00R", 4.00, "anchor"),
    ("4.50R", 4.50, "interp"),
    ("5.00R", 5.00, "anchor"),
    ("5.50R", 5.50, "interp"),
    ("6.00R", 6.00, "anchor"),
    ("6.50R", 6.50, "interp"),
    ("7.00R", 7.00, "anchor"),
    ("7.50R", 7.50, "interp"),
    ("8.00R", 8.00, "anchor"),
    ("8.50R", 8.50, "interp"),
    ("9.00R", 9.00, "anchor"),
    ("9.50R", 9.50, "interp"),
    ("10.00R", 10.00, "anchor"),
    ("11.00R", 11.00, "anchor"),
    ("12.00R", 12.00, "anchor"),
    ("13.00R", 13.00, "anchor"),
    ("14.00R", 14.00, "anchor"),
    ("15.00R", 15.00, "anchor"),
    ("16.00R", 16.00, "anchor"),
    ("17.00R", 17.00, "anchor"),
    ("18.00R", 18.00, "anchor"),
    ("19.00R", 19.00, "anchor"),
    ("20.00R", 20.00, "anchor"),
    ("21.00R", 21.00, "anchor"),
    ("22.00R", 22.00, "anchor"),
    ("23.00R", 23.00, "anchor"),
    ("24.00R+", 24.00, "exit"),
]


def ladder_locked_r(open_profit_r: float) -> float | None:
    if open_profit_r < 0.5:
        return -1.0
    if open_profit_r < 0.75:
        return 0.10 + ((open_profit_r - 0.50) / 0.25) * (0.25 - 0.10)
    if open_profit_r < 1.0:
        return 0.25 + ((open_profit_r - 0.75) / 0.25) * (0.50 - 0.25)
    if open_profit_r >= 24.0:
        return None
    if open_profit_r >= 23.0:
        return 22.77
    lower_r = math.floor(open_profit_r)
    upper_r = lower_r + 1
    frac = open_profit_r - lower_r
    low = LADDER_ANCHORS[lower_r]
    high = LADDER_ANCHORS[upper_r]
    return low + frac * (high - low)


def compute_profit_ladder(risk1r: float, ladder1r: float) -> list[dict]:
    rows = []
    for label, r, kind in LADDER_ROWS:
        if label == "< 0.50R":
            locked_r = -1.0
            open_profit = 0.25 * ladder1r
            stop = -1.0 * risk1r
            float_val = None
        else:
            locked_r = ladder_locked_r(r)
            open_profit = r * ladder1r
            stop = None if locked_r is None else locked_r * ladder1r
            float_val = None if locked_r is None else (r - locked_r) * ladder1r
        rows.append({
            "label": label,
            "kind": kind,
            "openProfit": open_profit,
            "lockedR": locked_r,
            "stop": stop,
            "float": float_val,
        })
    return rows


TEMA_LIMIT_TEMPLATE_COUNT = 40


def read_tema_limit_template_rows(range_name: str = "all") -> list[dict]:
    """Shared row source for build_template_usage and build_template_coverage.
    templateNumber is column index 12 in TEMA_LIMIT_TRADES, with ticker,
    direction, and bar type (data series) at indices 1, 2, and 13."""
    raw_rows = []
    for line in read_text(TEMA_LIMIT_TRADES).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) <= 13:
            continue
        entry_time, _ = trailing_entry_time_and_account(parts)
        raw_rows.append({
            "time": parts[0],
            "template": parse_int(parts[12], 0),
            "pnl": parse_number(parts[6]),
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "dataSeries": canonical_bar_type(parts[13]),
            "entryTime": entry_time,
        })
    return filter_rows_by_range(raw_rows, range_name)


def _new_series_stat_bucket() -> dict:
    return {
        "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
        "_holdTotal": 0.0, "_holdCount": 0,
    }


def build_template_usage(range_name: str = "all") -> list[dict]:
    """Returns all 40 template slots in numeric order, zero-filled for
    templates that haven't fired, each carrying an instrument/data-series/
    direction breakdown for the expandable detail view, plus a per-data-series
    stats map (bySeries) so the frontend can filter the list to a single
    data series without losing accurate win-rate/pnl/avg-hold numbers."""
    buckets = {
        n: {
            "template": n, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
            "instruments": {}, "dataSeries": {}, "directions": {},
            "bySeries": {},
            "_holdTotal": 0.0, "_holdCount": 0,
        }
        for n in range(1, TEMA_LIMIT_TEMPLATE_COUNT + 1)
    }
    for row in read_tema_limit_template_rows(range_name):
        bucket = buckets.get(row["template"])
        if bucket is None:
            continue
        bucket["trades"] += 1
        bucket["wins"] += 1 if row["pnl"] > 0 else 0
        bucket["losses"] += 1 if row["pnl"] < 0 else 0
        bucket["pnl"] += row["pnl"]
        bucket["instruments"][row["ticker"]] = bucket["instruments"].get(row["ticker"], 0) + 1
        bucket["dataSeries"][row["dataSeries"]] = bucket["dataSeries"].get(row["dataSeries"], 0) + 1
        bucket["directions"][row["direction"]] = bucket["directions"].get(row["direction"], 0) + 1
        hold_seconds = hold_time_seconds(row["entryTime"], row["time"])
        if hold_seconds is not None:
            bucket["_holdTotal"] += hold_seconds
            bucket["_holdCount"] += 1
        series_bucket = bucket["bySeries"].setdefault(row["dataSeries"], _new_series_stat_bucket())
        series_bucket["trades"] += 1
        series_bucket["wins"] += 1 if row["pnl"] > 0 else 0
        series_bucket["losses"] += 1 if row["pnl"] < 0 else 0
        series_bucket["pnl"] += row["pnl"]
        if hold_seconds is not None:
            series_bucket["_holdTotal"] += hold_seconds
            series_bucket["_holdCount"] += 1
    result = list(buckets.values())
    for row in result:
        row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
        row["avgHoldSeconds"] = (row["_holdTotal"] / row["_holdCount"]) if row["_holdCount"] else None
        del row["_holdTotal"]
        del row["_holdCount"]
        for key in ("instruments", "dataSeries", "directions"):
            row[key] = sorted(
                ({"label": label, "count": count} for label, count in row[key].items()),
                key=lambda item: item["count"],
                reverse=True,
            )
        for series_bucket in row["bySeries"].values():
            series_bucket["winRate"] = (series_bucket["wins"] / series_bucket["trades"] * 100.0) if series_bucket["trades"] else 0.0
            series_bucket["avgHoldSeconds"] = (series_bucket["_holdTotal"] / series_bucket["_holdCount"]) if series_bucket["_holdCount"] else None
            del series_bucket["_holdTotal"]
            del series_bucket["_holdCount"]
    result.sort(key=lambda r: r["template"])
    return result


def build_template_coverage(range_name: str = "all") -> dict:
    """Instrument x template cross-reference for TemaLimit -- answers 'did
    NQ ever use template 7' at a glance instead of expanding 40 rows one at a
    time. Sparse: only (ticker, template) pairs with at least one trade are
    included as cells; the frontend fills in the empty grid squares itself
    from the full instruments/templates lists."""
    cells: dict[tuple[str, int], dict] = {}
    instruments: set[str] = set()
    for row in read_tema_limit_template_rows(range_name):
        instruments.add(row["ticker"])
        key = (row["ticker"], row["template"])
        cell = cells.setdefault(key, {
            "ticker": row["ticker"], "template": row["template"],
            "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
            "dataSeries": {}, "directions": {},
            "_holdTotal": 0.0, "_holdCount": 0,
        })
        cell["trades"] += 1
        cell["wins"] += 1 if row["pnl"] > 0 else 0
        cell["losses"] += 1 if row["pnl"] < 0 else 0
        cell["pnl"] += row["pnl"]
        cell["dataSeries"][row["dataSeries"]] = cell["dataSeries"].get(row["dataSeries"], 0) + 1
        cell["directions"][row["direction"]] = cell["directions"].get(row["direction"], 0) + 1
        hold_seconds = hold_time_seconds(row["entryTime"], row["time"])
        if hold_seconds is not None:
            cell["_holdTotal"] += hold_seconds
            cell["_holdCount"] += 1

    result = list(cells.values())
    for cell in result:
        cell["winRate"] = (cell["wins"] / cell["trades"] * 100.0) if cell["trades"] else 0.0
        cell["avgHoldSeconds"] = (cell["_holdTotal"] / cell["_holdCount"]) if cell["_holdCount"] else None
        del cell["_holdTotal"]
        del cell["_holdCount"]
        for key in ("dataSeries", "directions"):
            cell[key] = sorted(
                ({"label": label, "count": count} for label, count in cell[key].items()),
                key=lambda item: item["count"],
                reverse=True,
            )
    result.sort(key=lambda c: (c["ticker"], c["template"]))
    return {
        "instruments": sorted(instruments),
        "templates": list(range(1, TEMA_LIMIT_TEMPLATE_COUNT + 1)),
        "cells": result,
    }


def build_series_family_summary(range_name: str = "all") -> list[dict]:
    """Strategy-card board grouped by data-series family (Renko, Tick, Minute,
    etc. -- entry["dataSeries"] here is already family-level, not the
    variant-specific "Renko 9" label build_data_series_summary uses) instead
    of account, so e.g. "1 Renko" and "5 Renko" trades from any account land
    on the same card."""
    entries = collect_all_trade_entries(range_name)
    # entries are already scoped to the requested range, so any family that
    # appears below has trades the user asked to see -- for an explicit range
    # the card must show even if the series hasn't traded in the last 24h.
    # The freshness gate only keeps its deprecation auto-hide role on "all",
    # where the range filter otherwise never drops anything.
    range_is_scoped = normalize_range(range_name) != "all"
    series: dict[str, dict] = {}
    for entry in entries:
        fam = series.setdefault(entry["dataSeries"], {"dataSeries": entry["dataSeries"], "rowsMap": {}, "lastTime": None})
        bucket = fam["rowsMap"].setdefault(entry["ticker"], {
            "ticker": entry["ticker"],
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "_templates": {},
        })
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        bucket["pnl"] += entry["pnl"]
        template_number = entry.get("templateNumber")
        if template_number is not None:
            bucket["_templates"][template_number] = bucket["_templates"].get(template_number, 0) + 1
        trade_time = parse_trade_time(entry["time"])
        if trade_time and (fam["lastTime"] is None or trade_time > fam["lastTime"]):
            fam["lastTime"] = trade_time

    result = []
    for fam in series.values():
        rows_list = list(fam["rowsMap"].values())
        for row in rows_list:
            row["winRate"] = (row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0
            row["templates"] = sorted(
                ({"label": template, "count": count} for template, count in row["_templates"].items()),
                key=lambda item: item["count"],
                reverse=True,
            )
            del row["_templates"]
        rows_list.sort(key=lambda r: r["trades"], reverse=True)
        trades = sum(r["trades"] for r in rows_list)
        wins = sum(r["wins"] for r in rows_list)
        losses = sum(r["losses"] for r in rows_list)
        pnl = sum(r["pnl"] for r in rows_list)
        instruments = len({r["ticker"] for r in rows_list})
        last_time = fam["lastTime"]
        result.append({
            "dataSeries": fam["dataSeries"],
            "totals": {
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "winRate": (wins / trades * 100.0) if trades else 0.0,
                "pnl": pnl,
                "instruments": instruments,
            },
            "rows": rows_list,
            "active": range_is_scoped or (last_time is not None and (datetime.now() - last_time) <= timedelta(hours=ACTIVE_WINDOW_HOURS)),
            "lastTradeTime": last_time.isoformat() if last_time else None,
        })
    result.sort(key=lambda a: a["totals"]["trades"], reverse=True)
    return result


DATA_SERIES_SOURCES = [
    (TEMA_LIMIT_TRADES, extract_tema_signal_entries),
    (TEMA_MARKET_TRADES, extract_tema_signal_entries),
    (MARKET_MULTI_TICKER_TRADES, extract_ticker_prefixed_signal_entries),
    (CERAVE_TRADES, extract_ticker_prefixed_signal_entries),
    (MULTI_DATA_SERIES_TRADES, extract_multi_data_series_signal_entries),
]


def collect_data_series_entries(range_name: str = "all") -> list[dict]:
    """Flat list of the individual trade rows that back build_data_series_summary,
    so the dashboard can drill from a data-series slice down to the matching
    trades.

    No rows_are_active() gate here (unlike the deprecated-strategy auto-hide used
    for the strategy CARDS): the Data Series breakdown describes the exact same
    trades as the Instrument/Direction/Session/Exit Signal tabs, which use the
    un-gated collect_all_trade_entries. Gating this one source on a 24h freshness
    window made an ACTIVE strategy's series breakdown vanish across every range
    whenever it simply hadn't traded for a day (e.g. any weekend) -- the gate
    runs before the range filter, so range=all/5D/3D all showed 0 while the
    sibling tabs still listed the same trades. The range filter alone scopes
    this now, matching those siblings."""
    entries = []
    for path, extractor in DATA_SERIES_SOURCES:
        entries += extractor(path)
    return filter_rows_by_range(entries, range_name)


def build_data_series_summary(range_name: str = "all") -> list[dict]:
    entries = collect_data_series_entries(range_name)

    series_buckets: dict[str, dict] = {}
    for entry in entries:
        series = entry["dataSeries"]
        bucket = series_buckets.setdefault(series, {
            "dataSeries": series,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "tickers": {},
            "_holdTotal": 0.0,
            "_holdCount": 0,
        })
        bucket["trades"] += 1
        bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        bucket["pnl"] += entry["pnl"]
        hold_seconds = hold_time_seconds(entry.get("entryTime", ""), entry["time"])
        if hold_seconds is not None:
            bucket["_holdTotal"] += hold_seconds
            bucket["_holdCount"] += 1
        ticker_bucket = bucket["tickers"].setdefault(entry["ticker"], {
            "ticker": entry["ticker"],
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
        })
        ticker_bucket["trades"] += 1
        ticker_bucket["wins"] += 1 if entry["pnl"] > 0 else 0
        ticker_bucket["losses"] += 1 if entry["pnl"] < 0 else 0
        ticker_bucket["pnl"] += entry["pnl"]

    rows = []
    for bucket in series_buckets.values():
        bucket["winRate"] = (bucket["wins"] / bucket["trades"] * 100.0) if bucket["trades"] else 0.0
        bucket["avgHoldSeconds"] = (bucket["_holdTotal"] / bucket["_holdCount"]) if bucket["_holdCount"] else None
        del bucket["_holdTotal"]
        del bucket["_holdCount"]
        tickers = list(bucket["tickers"].values())
        for ticker_bucket in tickers:
            ticker_bucket["winRate"] = (ticker_bucket["wins"] / ticker_bucket["trades"] * 100.0) if ticker_bucket["trades"] else 0.0
        tickers.sort(key=lambda item: item["trades"], reverse=True)
        bucket["tickers"] = tickers
        rows.append(bucket)
    rows.sort(key=lambda item: item["trades"], reverse=True)
    return rows


def read_nofill_rows(range_name: str = "all") -> list[dict]:
    """Rows from the limit-entry no-fill TSV (temalimit.cs's AppendNoFillLog) --
    one row per cancelled zero-fill entry order, context at cancel time only.
    dataSeriesType is the raw NT8 BarsPeriodType (e.g. "Renko", "HeikenAshi")
    with no bar-count suffix, matching the leading token of the combined
    labels build_data_series_summary uses (series_label() = "Renko 9" etc.),
    so the frontend can match "5 Renko" no-fill rows to any Renko-size legend
    entry without requiring the exact bar count to match."""
    raw_rows = []
    for line in read_text(TEMA_LIMIT_NOFILL).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        raw_rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "template": parse_int(parts[3], 0),
            "dataSeriesType": parts[4].strip(),
            "dataSeries": series_label(parts[4], parts[5]),
            "limitPrice": parse_number(parts[6]),
            "pullbackTicks": parse_int(parts[7], 0),
            "atr": parse_number(parts[8]),
            "waitedMinutes": parse_number(parts[9]),
            "account": format_account(parts[10]),
            "marketPriceAtPlacement": parse_number(parts[11]) if len(parts) > 11 else 0.0,
            "marketPriceAtCancel": parse_number(parts[12]) if len(parts) > 12 else 0.0,
            "missedByTicks": parse_number(parts[13]) if len(parts) > 13 else None,
            # ATR ratio pair (added 2026-07-17): raw < clamped means the AtrClampMin floor was the
            # binding constraint on the pullback distance for this no-fill. Older rows lack them.
            "atrRatioRaw": parse_number(parts[14]) if len(parts) > 14 and parts[14].strip() != "" else None,
            "atrRatioClamped": parse_number(parts[15]) if len(parts) > 15 and parts[15].strip() != "" else None,
        })
    return filter_rows_by_range(raw_rows, range_name)


def build_nofill_stats(range_name: str = "all", pullback_since: dict | None = None) -> dict:
    """pullback_since: optional {(ticker, "low"|"high") -> ISO timestamp} -- rows at or before the
    cutoff are dropped for that (instrument, tier) before bucketing. auto_apply_sizing.py passes
    the last-applied pullback timestamps from auto_apply_history.json here so its *decisions* only
    see evidence accumulated against the current ratio (pre-apply no-fills would otherwise keep
    re-implying the already-applied value forever); the dashboard's own calls omit it, so the
    displayed No-Fill Log still shows all evidence."""
    rows = read_nofill_rows(range_name)
    if pullback_since:
        rows = [row for row in rows
                if _after_cutoff(row["time"], pullback_since.get((row["ticker"], pullback_tier_group(row["template"]))))]

    buckets: dict[tuple[int, str], dict] = {}
    for row in rows:
        key = (row["template"], row["ticker"])
        bucket = buckets.setdefault(key, {
            "template": row["template"],
            "ticker": row["ticker"],
            "count": 0,
            "_waitTotal": 0.0,
            "_missedValues": [],
        })
        bucket["count"] += 1
        bucket["_waitTotal"] += row["waitedMinutes"]
        bucket["_missedValues"].append(row["missedByTicks"])

    pullback_map = build_template_pullback_map()
    # Same cutoffs applied to the fill side (see build_template_fill_counts' docstring): without
    # this, resetting the no-fill evidence after an increase apply immediately re-armed the next
    # +15% increase off all-time fills.
    fill_counts = build_template_fill_counts(range_name, since=pullback_since)
    pullback_history = _history_index(read_auto_apply_history(), "pullback", ("ticker", "tierGroup"))

    # Buckets with zero no-fills but real fill history are the "increase" candidates -- they
    # never appear in the no-fill rows above, so add them here with count=0 or they'd be invisible.
    for ticker, by_template in fill_counts.items():
        for template_key, fills in by_template.items():
            key = (int(template_key), ticker)
            if key not in buckets and fills >= 1:
                buckets[key] = {
                    "template": int(template_key), "ticker": ticker,
                    "count": 0, "_waitTotal": 0.0, "_missedValues": [],
                }

    by_template_instrument = []
    for bucket in buckets.values():
        bucket["avgWaitedMinutes"] = bucket["_waitTotal"] / bucket["count"] if bucket["count"] else 0.0
        current_ticks = pullback_map.get(bucket["ticker"], {}).get(str(bucket["template"]))
        fill_count = fill_counts.get(bucket["ticker"], {}).get(str(bucket["template"]), 0)
        reassess = pullback_reassess(current_ticks, bucket["_missedValues"], fill_count)
        bucket["currentPullbackTicks"] = current_ticks
        bucket["fillCount"] = fill_count
        bucket["avgMissedByTicks"] = reassess["avgMissedByTicks"]
        bucket["suggestedPullbackTicks"] = reassess["suggestedPullbackTicks"]
        bucket["suggestDirection"] = reassess["direction"]
        bucket["reassessTier"] = reassess["tier"]
        bucket["lastApplied"] = _last_applied(pullback_history, (bucket["ticker"], pullback_tier_group(bucket["template"])))
        del bucket["_waitTotal"]
        del bucket["_missedValues"]
        by_template_instrument.append(bucket)
    by_template_instrument.sort(key=lambda b: b["count"], reverse=True)

    template_counts: dict[int, int] = {}
    for row in rows:
        template_counts[row["template"]] = template_counts.get(row["template"], 0) + 1
    top_template = max(template_counts.items(), key=lambda kv: kv[1])[0] if template_counts else None

    recent = sorted(rows, key=lambda r: r["time"], reverse=True)[:200]
    avg_wait = (sum(r["waitedMinutes"] for r in rows) / len(rows)) if rows else 0.0

    return {
        "total": len(rows),
        "topTemplate": top_template,
        "topTemplateCount": template_counts.get(top_template, 0) if top_template is not None else 0,
        "avgWaitedMinutes": avg_wait,
        "byTemplateInstrument": by_template_instrument,
        "recent": recent,
    }


MIN_SAMPLES_FOR_GATE_REASSESS = 5
GATE_TIER1_MAX_TEMPLATE = 19  # same 1-19 / 20-40 split Risk1R sizing uses (temalimit.cs Tier1MaxTemplate)

# Maps (gate, tierGroup) to the auto-adjust constant in temalimit.cs. Widen units: MFI/RSI in
# indicator points, StochRSI in 0-1 units; expire in whole minutes.
GATE_WIDEN_CONSTANTS = {
    ("MFI", "t1_19"): "MfiGateWidenT1to19",
    ("MFI", "t20_40"): "MfiGateWidenT20to40",
    ("RSI", "t1_19"): "RsiGateWidenT1to19",
    ("RSI", "t20_40"): "RsiGateWidenT20to40",
    ("StochRSI", "t1_19"): "StochGateWidenT1to19",
    ("StochRSI", "t20_40"): "StochGateWidenT20to40",
}
EXPIRE_EXTRA_CONSTANTS = {
    "t1_19": "EntryExpireExtraMinutesT1to19",
    "t20_40": "EntryExpireExtraMinutesT20to40",
}
# Hard ceilings on the total auto-applied widen / expire-extra. Defined here (not in
# auto_apply_sizing.py, which imports them) so gate_reassess/expire_reassess can suppress a
# widen/increase suggestion once the constant is already pinned at its cap -- otherwise the
# apply layer clamps new==current and skips the same no-op finding every run forever (the
# ATR-clamp and slippage builders already self-suppress at their caps; this makes gate/expire
# consistent). auto_apply_sizing.py re-imports both for its own final clamp.
GATE_MAX_TOTAL_WIDEN = {"MFI": 15.0, "RSI": 15.0, "StochRSI": 0.15}
EXPIRE_MAX_TOTAL_EXTRA = 24  # effective expire also clamps to 30 min inside temalimit.cs
# temalimit.cs's ExpireWatchWindowMinutes: a watch that ran at least this long (small tolerance for
# 0.## rounding) observed everything a full watch could -- shorter untouched watches were truncated
# by the next expire-cancel taking the single watch slot and prove nothing about late touches.
EXPIRE_WATCH_FULL_MINUTES = 59.0

# Global (not per-tier) auto-adjust constants, parsed alongside the gate/expire ones:
# slippage reserve ratio and the ATR-bound pullback clamp band.
SCALAR_ADJUST_CONSTANTS = ("SlippageReserveRatio", "AtrClampMin", "AtrClampMax")

SIZING_TIER1_MAX_TEMPLATE = 19  # matches auto_apply_sizing.py's TIER1_MAX_TEMPLATE / temalimit.cs Tier1MaxTemplate


def sizing_tier_group(template: int) -> str:
    return "tier1" if template <= SIZING_TIER1_MAX_TEMPLATE else "tier2"


def pullback_tier_group(template: int) -> str:
    """Matches auto_apply_sizing.py's check_pullback_agreement tier split (<=17 low, else high) --
    NOT the same boundary as sizing/gates (19), since PullbackTicksForTicker's own lowTier check
    in temalimit.cs uses <=17."""
    return "low" if template <= 17 else "high"


def read_auto_apply_history() -> list[dict]:
    """Every change auto_apply_sizing.py has actually applied to temalimit.cs, most-recent-last
    (see that file's HISTORY_PATH / _append_history). Routed through read_text() so it shares the
    existing mtime+size cache instead of adding a second file-watch mechanism."""
    try:
        text = read_text(AUTO_APPLY_HISTORY_PATH)
    except OSError:
        return []
    if not text.strip():
        return []
    try:
        return json.loads(text)
    except ValueError:
        return []


def _history_index(history: list[dict], kind: str, key_fields: tuple[str, ...]) -> dict:
    """{key_tuple: most-recent entry} for one history `type`. History is append-ordered (oldest
    first), so a later entry for the same key simply overwrites the earlier one here."""
    index: dict[tuple, dict] = {}
    for entry in history:
        if entry.get("type") != kind:
            continue
        index[tuple(entry.get(f) for f in key_fields)] = entry
    return index


def _last_applied(index: dict, key: tuple) -> dict | None:
    entry = index.get(key)
    if not entry:
        return None
    # unit is only present on entries whose value isn't in the surrounding column's units
    # (e.g. ES Tier 2's ticks-per-template next to dollar columns); frontend renders it when set.
    return {"oldValue": entry.get("old"), "newValue": entry.get("new"), "timestamp": entry.get("timestamp"), "unit": entry.get("unit")}


def gate_tier_group(template: int) -> str:
    return "t1_19" if template <= GATE_TIER1_MAX_TEMPLATE else "t20_40"


def read_gate_widen_constants() -> dict:
    """Current values of the entry-gate auto-adjust constants, parsed straight out of temalimit.cs
    (same treat-the-strategy-source-as-data approach auto_apply_sizing.py uses). Returns
    {constantName: float}; missing file/constants simply yield an empty/partial dict."""
    values: dict[str, float] = {}
    try:
        text = read_text(TEMALIMIT_CS)
    except OSError:
        return values
    for name in list(GATE_WIDEN_CONSTANTS.values()) + list(EXPIRE_EXTRA_CONSTANTS.values()) + list(SCALAR_ADJUST_CONSTANTS):
        match = re.search(rf"private const (?:double|int) {name} = (-?[\d.]+);", text)
        if match:
            values[name] = parse_number(match.group(1))
    return values


def read_gateblock_rows(range_name: str = "all") -> list[dict]:
    """Rows from TemaLimit_gateblock_log.tsv (temalimit.cs's AppendGateBlockLog) -- one row per
    (bar, direction, blocking gate) where a setup trigger fired but MFI/RSI/StochRSI blocked the
    entry. gapPoints (may be empty) is the smallest threshold widen that would have passed that bar."""
    raw_rows = []
    for line in read_text(TEMA_LIMIT_GATEBLOCK).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 10:
            continue
        raw_rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "template": parse_int(parts[3], 0),
            "dataSeriesType": parts[4].strip(),
            "dataSeries": series_label(parts[4], parts[5]),
            "gate": parts[6].strip(),
            "indicatorValue": parse_number(parts[7]),
            "threshold": parse_number(parts[8]),
            "gapPoints": parse_number(parts[9]) if parts[9].strip() != "" else None,
            "account": format_account(parts[10]) if len(parts) > 10 else "Unassigned",
        })
    return filter_rows_by_range(raw_rows, range_name)


def read_expire_rows(range_name: str = "all") -> list[dict]:
    """Rows from TemaLimit_expire_log.tsv (temalimit.cs's post-cancel expire watch) -- one row per
    expired-and-cancelled entry limit, saying whether the market traded through the old limit within
    the watch window and how many minutes after submission it did."""
    raw_rows = []
    for line in read_text(TEMA_LIMIT_EXPIRE).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        raw_rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "template": parse_int(parts[3], 0),
            "dataSeriesType": parts[4].strip(),
            "dataSeries": series_label(parts[4], parts[5]),
            "limitPrice": parse_number(parts[6]),
            "expireMinutesUsed": parse_int(parts[7], 0),
            "touched": parts[8].strip() == "1",
            "minutesToTouch": parse_number(parts[9]) if parts[9].strip() != "" else None,
            # Minutes the watch actually ran. Rows written before 2026-07-18 logged the constant
            # 60-minute window here regardless of truncation, so old truncated rows read as full
            # watches -- indistinguishable, and self-correcting as new rows accumulate.
            "minutesWatched": parse_number(parts[10], 0.0),
            "account": format_account(parts[11]) if len(parts) > 11 else "Unassigned",
        })
    return filter_rows_by_range(raw_rows, range_name)


def gate_reassess(gaps: list[float], block_count: int, fill_count: int, current_widen, gate: str) -> dict:
    """Bidirectional gate-widen suggestion + reassess tier for one Ticker x TierGroup x Gate bucket,
    mirroring pullback_reassess: widen is data-derived (measured gap = smallest threshold move that
    would have passed each blocked bar), tighten is a heuristic drift back toward the designed table
    (-25% of current widen, only when the gate never blocks and only while widen > 0 -- the static
    TemplateParamsTable stays the floor).

    Returns {direction: "widen"|"tighten"|None, avgGapPoints, suggestedWidenDelta, tier, capReached}."""
    decimals = 4 if gate == "StochRSI" else 2
    measured = [g for g in gaps if g is not None]
    avg_gap = round(sum(measured) / len(measured), decimals) if measured else None
    widen = current_widen if current_widen is not None else 0.0

    if len(measured) >= MIN_SAMPLES_FOR_GATE_REASSESS and avg_gap and avg_gap > 0:
        # Blocks persist but the widen is already pinned at its ceiling -- proposing more just
        # gets clamped to current and skipped every run. Report the cap instead of a phantom
        # REASSESS NOW that can never complete (mirrors ATR clamp's current_min<0.50 guard).
        if widen >= GATE_MAX_TOTAL_WIDEN.get(gate, float("inf")):
            return {"direction": None, "avgGapPoints": avg_gap, "suggestedWidenDelta": None, "tier": "monitor", "capReached": True}
        return {"direction": "widen", "avgGapPoints": avg_gap, "suggestedWidenDelta": avg_gap, "tier": "reassess", "capReached": False}

    if block_count == 0 and fill_count >= MIN_SAMPLES_FOR_GATE_REASSESS and widen > 0:
        delta = -round(widen * 0.25, decimals)
        if delta < 0:
            return {"direction": "tighten", "avgGapPoints": avg_gap, "suggestedWidenDelta": delta, "tier": "reassess", "capReached": False}

    if block_count >= 1 or fill_count >= 1:
        return {"direction": None, "avgGapPoints": avg_gap, "suggestedWidenDelta": None, "tier": "monitor", "capReached": False}

    return {"direction": None, "avgGapPoints": avg_gap, "suggestedWidenDelta": None, "tier": "too_few", "capReached": False}


def expire_reassess(needed_extras: list[float], watch_count: int, full_watch_count: int, touched_count: int, current_extra) -> dict:
    """Expire-minutes suggestion for one Ticker x TierGroup bucket. Increase is data-derived: for
    each cancelled order that WOULD have filled inside the watch window, neededExtra is how many
    minutes past its expiry the touch came; suggest the 75th percentile (whole minutes, capped +10
    per step). Decrease is a heuristic -1 drift, only while the current extra is > 0 and a full
    sample of watches produced zero touches (waiting longer demonstrably isn't helping) -- and
    only FULL watches count toward that sample: a watch truncated after a few minutes by the next
    expire-cancel never had the chance to observe a late touch, so it can't prove one wouldn't
    have come.

    Returns {direction: "increase"|"decrease"|None, suggestedExtraDelta, touchRate, tier, capReached}."""
    touch_rate = round(touched_count / watch_count, 3) if watch_count else None
    extra = current_extra if current_extra is not None else 0

    if touched_count >= MIN_SAMPLES_FOR_GATE_REASSESS and needed_extras:
        # Touches persist but the extra is already at its ceiling -- the apply layer would clamp
        # to current and skip forever, so surface the cap instead (mirrors the gate-widen guard).
        if extra >= EXPIRE_MAX_TOTAL_EXTRA:
            return {"direction": None, "suggestedExtraDelta": None, "touchRate": touch_rate, "tier": "monitor", "capReached": True}
        ordered = sorted(needed_extras)
        p75 = ordered[int(0.75 * (len(ordered) - 1))]
        delta = max(1, min(10, math.ceil(p75)))
        return {"direction": "increase", "suggestedExtraDelta": delta, "touchRate": touch_rate, "tier": "reassess", "capReached": False}

    if full_watch_count >= MIN_SAMPLES_FOR_GATE_REASSESS and touched_count == 0 and extra > 0:
        return {"direction": "decrease", "suggestedExtraDelta": -1, "touchRate": touch_rate, "tier": "reassess", "capReached": False}

    if watch_count >= 1:
        return {"direction": None, "suggestedExtraDelta": None, "touchRate": touch_rate, "tier": "monitor", "capReached": False}

    return {"direction": None, "suggestedExtraDelta": None, "touchRate": touch_rate, "tier": "too_few", "capReached": False}


def build_tier_fill_counts(range_name: str = "all") -> dict:
    """ticker -> tierGroup -> completed-trade count; the "this tier fills fine already" evidence
    the tighten/decrease directions above need."""
    counts: dict[str, dict[str, int]] = {}
    for ticker, by_template in build_template_fill_counts(range_name).items():
        for template_key, fills in by_template.items():
            tier = gate_tier_group(int(template_key))
            by_tier = counts.setdefault(ticker, {})
            by_tier[tier] = by_tier.get(tier, 0) + fills
    return counts


def build_entry_gate_stats(range_name: str = "all", gate_since: dict | None = None, expire_since: dict | None = None) -> dict:
    """Evidence + suggestions for the entry-gate thresholds (MFI/RSI/StochRSI) and entry-order
    expire minutes, bucketed per Ticker x TierGroup (1-19 / 20-40) -- the Entry Gate Reassess card's
    data and auto_apply_sizing.py's input. Suggestions are deltas against the shared per-tier
    constants in temalimit.cs (currentWiden/currentExtra), not per-template edits.

    gate_since ({(gate, tierGroup) -> timestamp}) / expire_since ({tierGroup -> timestamp}):
    optional last-applied cutoffs (same contract as build_nofill_stats' pullback_since).
    auto_apply_sizing.py passes them so its decisions only see evidence accumulated against the
    current constants -- these suggestions are DELTAS added to the constant, so pre-apply rows
    keep implying the already-applied step forever and would ratchet the widen to its ceiling
    (or collapse the tighten drift to zero) within a few scheduled runs. The dashboard's own
    calls omit them, so the displayed card still shows all evidence in range."""
    widen_values = read_gate_widen_constants()
    tier_fills = build_tier_fill_counts(range_name)
    gate_rows = read_gateblock_rows(range_name)
    expire_rows = read_expire_rows(range_name)
    if gate_since:
        gate_rows = [row for row in gate_rows
                     if _after_cutoff(row["time"], gate_since.get((row["gate"], gate_tier_group(row["template"]))))]
    if expire_since:
        expire_rows = [row for row in expire_rows
                       if _after_cutoff(row["time"], expire_since.get(gate_tier_group(row["template"])))]
    # For the tighten drift's fill evidence: fills must also postdate that constant's cutoff,
    # or "zero fresh blocks + all-time fills" would re-fire the -25% step every run.
    fill_rows = read_tema_limit_template_rows(range_name) if gate_since else None
    history = read_auto_apply_history()
    # Shared per-tier constants (not per-ticker), so every ticker row within a (gate, tier) or
    # (tier) group shows the same last-applied change -- matches how auto_apply_sizing.py's
    # check_entry_gate_agreement() applies these.
    gate_history = _history_index(history, "gate", ("gate", "tierGroup"))
    expire_history = _history_index(history, "expire", ("tierGroup",))

    gate_buckets: dict[tuple[str, str, str], dict] = {}
    for row in gate_rows:
        key = (row["ticker"], gate_tier_group(row["template"]), row["gate"])
        bucket = gate_buckets.setdefault(key, {
            "ticker": key[0], "tierGroup": key[1], "gate": key[2],
            "blockCount": 0, "_gaps": [],
        })
        bucket["blockCount"] += 1
        bucket["_gaps"].append(row["gapPoints"])

    # Tiers that fill without ever logging a block are the tighten candidates -- seed them so
    # they're visible, same trick build_nofill_stats uses for its increase side.
    for ticker, by_tier in tier_fills.items():
        for tier, fills in by_tier.items():
            if fills < 1:
                continue
            for gate in ("MFI", "RSI", "StochRSI"):
                gate_buckets.setdefault((ticker, tier, gate), {
                    "ticker": ticker, "tierGroup": tier, "gate": gate,
                    "blockCount": 0, "_gaps": [],
                })

    gates = []
    for bucket in gate_buckets.values():
        constant = GATE_WIDEN_CONSTANTS.get((bucket["gate"], bucket["tierGroup"]))
        current_widen = widen_values.get(constant)
        fill_count = tier_fills.get(bucket["ticker"], {}).get(bucket["tierGroup"], 0)
        if gate_since:
            cutoff = gate_since.get((bucket["gate"], bucket["tierGroup"]))
            if cutoff:
                fill_count = sum(1 for r in fill_rows
                                 if r["ticker"] == bucket["ticker"]
                                 and gate_tier_group(r["template"]) == bucket["tierGroup"]
                                 and _after_cutoff(r["time"], cutoff))
        reassess = gate_reassess(bucket["_gaps"], bucket["blockCount"], fill_count, current_widen, bucket["gate"])
        bucket["fillCount"] = fill_count
        bucket["currentWiden"] = current_widen
        bucket["avgGapPoints"] = reassess["avgGapPoints"]
        bucket["suggestedWidenDelta"] = reassess["suggestedWidenDelta"]
        bucket["suggestDirection"] = reassess["direction"]
        bucket["reassessTier"] = reassess["tier"]
        bucket["capReached"] = reassess.get("capReached", False)
        bucket["lastApplied"] = _last_applied(gate_history, (bucket["gate"], bucket["tierGroup"]))
        del bucket["_gaps"]
        gates.append(bucket)
    gates.sort(key=lambda b: b["blockCount"], reverse=True)

    expire_buckets: dict[tuple[str, str], dict] = {}
    for row in expire_rows:
        key = (row["ticker"], gate_tier_group(row["template"]))
        bucket = expire_buckets.setdefault(key, {
            "ticker": key[0], "tierGroup": key[1],
            "watchCount": 0, "fullWatchCount": 0, "touchedCount": 0, "_neededExtras": [],
        })
        bucket["watchCount"] += 1
        # A touch is evidence at any watch length; an UNtouched row only proves anything if the
        # watch ran its full window (see expire_reassess and EXPIRE_WATCH_FULL_MINUTES).
        if row["touched"] or row["minutesWatched"] >= EXPIRE_WATCH_FULL_MINUTES:
            bucket["fullWatchCount"] += 1
        if row["touched"]:
            bucket["touchedCount"] += 1
            if row["minutesToTouch"] is not None:
                bucket["_neededExtras"].append(max(0.0, row["minutesToTouch"] - row["expireMinutesUsed"]))

    expire = []
    for bucket in expire_buckets.values():
        constant = EXPIRE_EXTRA_CONSTANTS.get(bucket["tierGroup"])
        current_extra = widen_values.get(constant)
        reassess = expire_reassess(bucket["_neededExtras"], bucket["watchCount"], bucket["fullWatchCount"], bucket["touchedCount"], current_extra)
        bucket["currentExtraMinutes"] = current_extra
        bucket["touchRate"] = reassess["touchRate"]
        bucket["suggestedExtraDelta"] = reassess["suggestedExtraDelta"]
        bucket["suggestDirection"] = reassess["direction"]
        bucket["reassessTier"] = reassess["tier"]
        bucket["capReached"] = reassess.get("capReached", False)
        bucket["lastApplied"] = _last_applied(expire_history, (bucket["tierGroup"],))
        del bucket["_neededExtras"]
        expire.append(bucket)
    expire.sort(key=lambda b: b["watchCount"], reverse=True)

    return {
        "gates": gates,
        "expire": expire,
        "constants": widen_values,
        "totalBlocks": len(gate_rows),
        "totalWatches": len(expire_rows),
    }


def read_slippage_rows(range_name: str = "all") -> list[dict]:
    """Rows from TemaLimit_slippage_log.tsv (temalimit.cs's AppendSlippageLog) -- one row per
    completed stop-order exit with realized slippage vs the stop level. Positive slippageTicks/
    Dollars = filled worse than the stop; negative = price improvement."""
    raw_rows = []
    for line in read_text(TEMA_LIMIT_SLIPPAGE).splitlines():
        line = line.strip()
        if not line or line.lower().startswith("time\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 13:
            continue
        raw_rows.append({
            "time": parts[0],
            "ticker": parts[1].upper(),
            "direction": parts[2].upper(),
            "template": parse_int(parts[3], 0),
            "dataSeriesType": parts[4].strip(),
            "dataSeries": series_label(parts[4], parts[5]),
            "stopPrice": parse_number(parts[6]),
            "fillPrice": parse_number(parts[7]),
            "quantity": parse_int(parts[8], 1),
            "slippageTicks": parse_number(parts[9]),
            "slippageDollars": parse_number(parts[10]),
            "ladderRiskDollars": parse_number(parts[11]),
            "reserveDollars": parse_number(parts[12]),
            "account": format_account(parts[13]) if len(parts) > 13 else "Unassigned",
        })
    return filter_rows_by_range(raw_rows, range_name)


def build_slippage_stats(range_name: str = "all") -> dict:
    """Per-instrument realized stop-exit slippage vs the SlippageReserveRatio constant (shared
    across all instruments). Suggestion is measured: 125% of the p90 realized ratio
    (slippageDollars / ladderRiskDollars, negatives floored at 0 -- the reserve exists to cover
    cost, not improvement), with a 10% dead-band against the current ratio so it doesn't flip-flop.
    Feeds the Sizing Reassess card's Slippage Reserve table and auto_apply's slippage check."""
    constants = read_gate_widen_constants()
    current_ratio = constants.get("SlippageReserveRatio")
    slippage_history = _history_index(read_auto_apply_history(), "slippage", ("param",))
    last_applied = _last_applied(slippage_history, ("ratio",))
    rows = read_slippage_rows(range_name)

    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(row["ticker"], {
            "ticker": row["ticker"], "stopExits": 0,
            "_ticks": [], "_ratios": [],
        })
        bucket["stopExits"] += 1
        bucket["_ticks"].append(row["slippageTicks"])
        if row["ladderRiskDollars"] > 0:
            bucket["_ratios"].append(max(0.0, row["slippageDollars"]) / row["ladderRiskDollars"])

    tickers = []
    for bucket in buckets.values():
        ticks = bucket.pop("_ticks")
        ratios = bucket.pop("_ratios")
        bucket["avgSlippageTicks"] = round(sum(ticks) / len(ticks), 2) if ticks else None
        bucket["p90Ratio"] = None
        bucket["suggestedRatio"] = None
        bucket["suggestDirection"] = None
        if len(ratios) >= MIN_SAMPLES_FOR_GATE_REASSESS:
            ordered = sorted(ratios)
            p90 = ordered[int(0.90 * (len(ordered) - 1))]
            bucket["p90Ratio"] = round(p90, 4)
            suggested = max(0.02, min(0.25, round(p90 * 1.25, 3)))
            bucket["suggestedRatio"] = suggested
            if current_ratio and abs(suggested - current_ratio) / current_ratio > 0.10:
                bucket["suggestDirection"] = "increase" if suggested > current_ratio else "decrease"
            bucket["reassessTier"] = "reassess" if bucket["suggestDirection"] else "monitor"
        elif bucket["stopExits"] >= 1:
            bucket["reassessTier"] = "monitor"
        else:
            bucket["reassessTier"] = "too_few"
        bucket["currentRatio"] = current_ratio
        bucket["lastApplied"] = last_applied
        tickers.append(bucket)
    tickers.sort(key=lambda b: b["stopExits"], reverse=True)

    return {"tickers": tickers, "currentRatio": current_ratio, "totalStopExits": len(rows), "lastApplied": last_applied}


def build_atr_clamp_stats(range_name: str = "all", since=None) -> dict:
    """Per-instrument evidence for the ATR-bound pullback clamp band. Only the FLOOR (AtrClampMin)
    gets a suggestion: a no-fill where the raw ratio sat below the clamped ratio was forced wider
    than volatility warranted, and missedByTicks tells whether the unclamped distance
    (pullbackTicks * raw/clamped) would actually have filled -- fully measured. The ceiling's
    saturation count is reported for manual judgment only (ceiling-bound entries FILL more easily;
    their cost is entry quality, which the no-fill log can't see).

    since: optional timestamp of the last applied AtrClampMin change (there's one shared constant,
    so one cutoff). auto_apply passes it so the +0.05 increase drift and the measured decrease both
    re-earn their sample against the current floor instead of re-stepping off pre-apply rows every
    scheduled run; the dashboard omits it."""
    constants = read_gate_widen_constants()
    current_min = constants.get("AtrClampMin")
    current_max = constants.get("AtrClampMax")
    clamp_history = _history_index(read_auto_apply_history(), "atrclamp", ("param",))
    last_applied = _last_applied(clamp_history, ("min",))
    rows = [r for r in read_nofill_rows(range_name) if r["atrRatioRaw"] is not None and r["atrRatioClamped"] is not None]
    if since:
        rows = [r for r in rows if _after_cutoff(r["time"], since)]

    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(row["ticker"], {
            "ticker": row["ticker"], "samples": 0, "floorBound": 0, "ceilBound": 0,
            "wouldHaveFilled": 0, "_wouldFillRaws": [],
        })
        bucket["samples"] += 1
        raw, clamped = row["atrRatioRaw"], row["atrRatioClamped"]
        if raw < clamped - 0.005:
            bucket["floorBound"] += 1
            if row["missedByTicks"] is not None and row["pullbackTicks"] > 0 and clamped > 0:
                unclamped_ticks = row["pullbackTicks"] * raw / clamped
                saved_ticks = row["pullbackTicks"] - unclamped_ticks
                if row["missedByTicks"] <= saved_ticks:
                    bucket["wouldHaveFilled"] += 1
                    bucket["_wouldFillRaws"].append(raw)
        elif raw > clamped + 0.005:
            bucket["ceilBound"] += 1

    tickers = []
    for bucket in buckets.values():
        would_fill_raws = bucket.pop("_wouldFillRaws")
        bucket["suggestedMin"] = None
        bucket["suggestDirection"] = None
        if len(would_fill_raws) >= MIN_SAMPLES_FOR_GATE_REASSESS:
            # p25 of the raw ratios that would have filled: set the floor near where volatility
            # actually goes when the clamp costs fills, without chasing the single lowest print.
            ordered = sorted(would_fill_raws)
            suggested = max(0.20, min(0.50, round(ordered[int(0.25 * (len(ordered) - 1))], 2)))
            if current_min is not None and suggested < current_min - 0.005:
                bucket["suggestedMin"] = suggested
                bucket["suggestDirection"] = "decrease"
        if bucket["suggestDirection"]:
            bucket["reassessTier"] = "reassess"
        elif bucket["samples"] >= MIN_SAMPLES_FOR_GATE_REASSESS and bucket["floorBound"] == 0 and current_min is not None and current_min < 0.50:
            # Heuristic drift back toward the designed 0.50 floor when a full sample shows the
            # lowered floor no longer binding -- same convention as the gate tighten side.
            bucket["suggestedMin"] = min(0.50, round(current_min + 0.05, 2))
            bucket["suggestDirection"] = "increase"
            bucket["reassessTier"] = "reassess"
        elif bucket["samples"] >= 1:
            bucket["reassessTier"] = "monitor"
        else:
            bucket["reassessTier"] = "too_few"
        bucket["currentMin"] = current_min
        bucket["currentMax"] = current_max
        bucket["lastApplied"] = last_applied
        tickers.append(bucket)
    tickers.sort(key=lambda b: b["samples"], reverse=True)

    return {"tickers": tickers, "currentMin": current_min, "currentMax": current_max, "totalSamples": len(rows), "lastApplied": last_applied}


def build_status(range_name: str = "all") -> dict:
    range_name = normalize_range(range_name)
    return {
        "serverTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "range": range_name,
        "ninjaTraderDir": str(NT_DIR),
        "nq": parse_nq(range_name),
        "temaLimit": parse_tema_limit(range_name),
        "temaMarket": parse_tema_market(range_name),
        "marketMultiTicker": parse_market_multi_ticker(range_name),
        "cerave": parse_cerave(range_name),
        "multiDataSeries": parse_multi_data_series(range_name),
        "globalSpike": parse_global(range_name),
        "trendTcn": parse_trend_tcn(range_name),
        "fullTwenties": parse_full_twenties(range_name),
        "twentyFourSeven": parse_twenty_four_seven(range_name),
        "dataSeriesSummary": build_data_series_summary(range_name),
        "accountTrades": collect_all_trade_entries(range_name),
        "dataSeriesTrades": collect_data_series_entries(range_name),
        "openTrades": parse_open_trades(),
        "pendingTrades": parse_pending_trades(),
        "autoStrategies": build_auto_strategies(range_name),
        "seriesCards": build_series_family_summary(range_name),
        "instruments": build_instrument_summary(range_name),
        "directions": build_direction_summary(range_name),
        "sessions": build_session_summary(range_name),
        "templateUsage": build_template_usage(range_name),
        "templateCoverage": build_template_coverage(range_name),
        "noFillStats": build_nofill_stats(range_name),
        "entryGateStats": build_entry_gate_stats(range_name),
        "slippageStats": build_slippage_stats(range_name),
        "atrClampStats": build_atr_clamp_stats(range_name),
        "pullbackState": parse_pullback_state(),
        "templateRisk": build_template_risk_map(),
        "ladderTrailDiagnosis": build_ladder_trail_diagnosis(),
        "reassessActivity": build_reassess_activity(),
        "templateIndicatorParams": build_template_indicator_params(),
        "exitSignals": build_exit_signal_summary(range_name),
        "equityCurve": build_equity_curve(range_name),
        "modelHealth": build_model_health(),
        "templateModelHealth": build_template_model_health(),
        "trendModelHealth": build_trend_model_health(),
    }


# Bump this when ChartDataExporter.cs's response schema changes (e.g. adding the "v"
# volume field) -- response files are cached on disk forever with no expiry, so without
# a version in the key, a trade fetched under an older AddOn build stays stuck with a
# stale/incomplete response even after the AddOn is recompiled. Bumping this orphans
# every existing cache file at once and forces a clean refetch.
CHART_CACHE_VERSION = "v3"


def trade_chart_request_id(ticker: str, entry_time: str, exit_time: str) -> str:
    key = f"{CHART_CACHE_VERSION}|{ticker.strip().upper()}|{entry_time.strip()}|{exit_time.strip()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def get_trade_chart(ticker: str, entry_time: str, exit_time: str) -> dict:
    """Bridges to ChartDataExporter.cs (a NinjaScript AddOn) over a small file-based
    protocol, same pattern as ManualExitCommand.cs's manual-exit request files:
    we drop a `<id>.request.json`, the AddOn (polling every ~2s inside NinjaTrader,
    which owns the historical-data decoder) resolves the contract, pulls 1-minute
    bars, and writes `<id>.json` back. Returns {"status": "pending"} until that
    response file shows up, so the frontend just polls this endpoint until ready."""
    entry_dt = parse_trade_time(entry_time)
    exit_dt = parse_trade_time(exit_time)
    if entry_dt is None or exit_dt is None:
        return {"status": "error", "error": "trade is missing entry/exit time"}

    request_id = trade_chart_request_id(ticker, entry_time, exit_time)
    CHART_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    response_path = CHART_REQUEST_DIR / f"{request_id}.json"

    if response_path.exists():
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            payload = None
        if payload is not None:
            if payload.get("ok"):
                return {"status": "ready", **payload}
            error_age = time.time() - response_path.stat().st_mtime
            if error_age < CHART_ERROR_CACHE_SECONDS:
                return {"status": "error", "error": payload.get("error", "chart export failed")}
            try:
                response_path.unlink()
            except OSError:
                pass

    # The AddOn renames <id>.request.json to <id>.request.json.processing the moment
    # it claims a request, and only deletes that .processing marker once the async
    # BarsRequest actually resolves (success, error, or its own stuck-fetch timeout).
    # A request is "still in flight" as long as EITHER file exists -- checking only
    # .request.json meant the instant the AddOn claimed one, it looked unclaimed again
    # here, so a BarsRequest whose callback never fired got silently re-issued every
    # poll (~1s) instead of ever surviving long enough to hit either side's timeout.
    request_path = CHART_REQUEST_DIR / f"{request_id}.request.json"
    processing_path = CHART_REQUEST_DIR / f"{request_id}.request.json.processing"
    in_flight_path = processing_path if processing_path.exists() else request_path
    needs_write = True
    if in_flight_path.exists():
        age = time.time() - in_flight_path.stat().st_mtime
        needs_write = age > CHART_REQUEST_STALE_SECONDS
    if needs_write:
        lo, hi = (entry_dt, exit_dt) if entry_dt <= exit_dt else (exit_dt, entry_dt)
        # For an open/ongoing trade, exit_dt is effectively "now", so padding it
        # forward asks BarsRequest for a window that extends past the present --
        # NinjaTrader has no bars for time that hasn't happened yet, so the request
        # just never calls back and hangs until ChartDataExporter's retry/timeout
        # gives up. Clamp to "now" so the request only ever spans real history.
        to_time = min(hi + CHART_BARS_PAD_AFTER, datetime.now())
        body = {
            "ticker": ticker.strip().upper(),
            "fromTime": (lo - CHART_BARS_PAD_BEFORE).isoformat(),
            "toTime": to_time.isoformat(),
        }
        request_path.write_text(json.dumps(body), encoding="utf-8")

    return {"status": "pending"}


_dashboard_layouts_lock = threading.Lock()

# One JSON file on disk, keyed by viewport profile ("phone-portrait",
# "tablet-landscape", "desktop-landscape", etc. -- see getViewportProfile() in
# dashboard.html), so the same profile looks the same layout up no matter which
# of your devices asks. Kept separate from localStorage, which stays as an
# instant local cache; this file is the thing that actually makes a layout
# carry over between devices.
def _load_dashboard_layouts() -> dict:
    try:
        with _dashboard_layouts_lock:
            return json.loads(DASHBOARD_LAYOUTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_dashboard_layout(profile: str, positions: dict, sizes: dict) -> None:
    with _dashboard_layouts_lock:
        try:
            data = json.loads(DASHBOARD_LAYOUTS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[profile] = {"positions": positions, "sizes": sizes}
        DASHBOARD_LAYOUTS_PATH.write_text(json.dumps(data), encoding="utf-8")


_dashboard_layout_slots_lock = threading.Lock()

# Named layout presets (the "Layout 1/2/3" pills), shared across every device
# regardless of viewport profile -- unlike the auto-positioned board layout
# above, these are user-named snapshots the user explicitly saves/renames, so
# there's one shared list rather than one per screen shape.
def _load_dashboard_layout_slots():
    try:
        with _dashboard_layout_slots_lock:
            return json.loads(DASHBOARD_LAYOUT_SLOTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_dashboard_layout_slots(data: dict) -> None:
    with _dashboard_layout_slots_lock:
        DASHBOARD_LAYOUT_SLOTS_PATH.write_text(json.dumps(data), encoding="utf-8")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/status":
            params = parse_qs(parsed.query)
            range_name = params.get("range", ["all"])[0]
            payload = json.dumps(build_status(range_name)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/template-ladder":
            params = parse_qs(parsed.query)
            ticker = (params.get("ticker", [""])[0] or "").strip().upper()
            template = (params.get("template", [""])[0] or "").strip()
            risk_map = build_template_risk_map()
            values = risk_map.get(ticker, {}).get(template)
            if values is None or values.get("risk1R") is None or values.get("ladderDaily") is None:
                payload = json.dumps({"ok": False, "error": "no risk data for that instrument/template"}).encode("utf-8")
                self.send_response(404)
            else:
                body = {
                    "ok": True,
                    "ticker": ticker,
                    "template": template,
                    "risk1R": values["risk1R"],
                    "ladderDaily": values["ladderDaily"],
                    "slippage": values.get("slippage"),
                    "rows": compute_profit_ladder(values["risk1R"], values["ladderDaily"]),
                }
                payload = json.dumps(body).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/trade-chart":
            params = parse_qs(parsed.query)
            ticker = (params.get("ticker", [""])[0] or "").strip()
            entry_time = (params.get("entryTime", [""])[0] or "").strip()
            exit_time = (params.get("exitTime", [""])[0] or "").strip()
            if not ticker or not entry_time or not exit_time:
                payload = json.dumps({"status": "error", "error": "ticker, entryTime and exitTime are required"}).encode("utf-8")
                self.send_response(400)
            else:
                payload = json.dumps(get_trade_chart(ticker, entry_time, exit_time)).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/layout":
            params = parse_qs(parsed.query)
            profile = (params.get("profile", [""])[0] or "").strip()
            entry = _load_dashboard_layouts().get(profile) if profile else None
            if entry:
                payload = json.dumps({"ok": True, "positions": entry.get("positions") or {}, "sizes": entry.get("sizes") or {}}).encode("utf-8")
            else:
                payload = json.dumps({"ok": True, "positions": {}, "sizes": {}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/layout-slots":
            data = _load_dashboard_layout_slots()
            payload = json.dumps({"ok": True, "data": data}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/restart":
            self.handle_restart_request()
            return
        if parsed.path == "/":
            self.path = "/dashboard.html"
        super().do_GET()

    def handle_restart_request(self):
        # Mirrors MLService/service.py's /restart: this process can't restart
        # itself in-process, so hand off to a helper script that waits for this
        # response to flush, kills whatever holds port 8766, then relaunches
        # live_dashboard_server.py the same way the watchdog does (pythonw.exe --
        # not python.exe -- see watchdog.py's comment on why).
        restart_script = BASE_DIR / "restart_dashboard_server.ps1"
        body = (
            "<html><body style='font-family:sans-serif;padding:2rem'>"
            "<h2>Restarting live dashboard server&hellip;</h2>"
            "<p id='restartStatus'>Shutting down and relaunching&hellip; this page will "
            "reload automatically once it's back.</p>"
            "<p><a href='/dashboard.html'>Go to the dashboard now</a></p>"
            + RESTART_POLL_SCRIPT % "/dashboard.html" +
            "</body></html>"
        ).encode("utf-8")
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
                cwd=str(BASE_DIR),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            body = f"<html><body><h2>Restart failed to launch: {exc}</h2></body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/exit":
            self.handle_exit_request()
            return
        if parsed.path == "/api/cancel":
            self.handle_cancel_request()
            return
        if parsed.path == "/api/layout":
            self.handle_layout_save_request()
            return
        if parsed.path == "/api/layout-slots":
            self.handle_layout_slots_save_request()
            return
        self.send_response(404)
        self.end_headers()

    def handle_layout_slots_save_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.respond_json(400, {"ok": False, "error": "invalid request body"})
            return

        if not isinstance(body, dict) or not isinstance(body.get("slots"), list):
            self.respond_json(400, {"ok": False, "error": "a slots array is required"})
            return

        try:
            _save_dashboard_layout_slots(body)
        except OSError as ex:
            self.respond_json(500, {"ok": False, "error": str(ex)})
            return

        self.respond_json(200, {"ok": True})

    def handle_layout_save_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.respond_json(400, {"ok": False, "error": "invalid request body"})
            return

        profile = str(body.get("profile", "")).strip()
        positions = body.get("positions")
        sizes = body.get("sizes")
        if not profile or not isinstance(positions, dict) or not isinstance(sizes, dict):
            self.respond_json(400, {"ok": False, "error": "profile, positions and sizes are required"})
            return

        try:
            _save_dashboard_layout(profile, positions, sizes)
        except OSError as ex:
            self.respond_json(500, {"ok": False, "error": str(ex)})
            return

        self.respond_json(200, {"ok": True})

    def handle_exit_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.respond_json(400, {"ok": False, "error": "invalid request body"})
            return

        strategy = str(body.get("strategy", "")).strip()
        ticker = str(body.get("ticker", "")).strip()
        account = str(body.get("account", "")).strip()

        if strategy not in MANUAL_EXIT_STRATEGIES:
            self.respond_json(400, {"ok": False, "error": "manual exit is not supported for this strategy"})
            return
        if not ticker or not account:
            self.respond_json(400, {"ok": False, "error": "ticker and account are required"})
            return
        if not NT_DIR.is_dir():
            self.respond_json(500, {"ok": False, "error": "NinjaTrader user data directory not found"})
            return

        file_name = manual_exit_command_file_name(strategy, ticker, account)
        path = NT_DIR / file_name
        try:
            path.write_text(datetime.now().isoformat(), encoding="utf-8")
        except OSError as ex:
            self.respond_json(500, {"ok": False, "error": str(ex)})
            return

        self.respond_json(200, {"ok": True})

    def handle_cancel_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.respond_json(400, {"ok": False, "error": "invalid request body"})
            return

        strategy = str(body.get("strategy", "")).strip()
        ticker = str(body.get("ticker", "")).strip()
        account = str(body.get("account", "")).strip()

        if strategy not in MANUAL_CANCEL_STRATEGIES:
            self.respond_json(400, {"ok": False, "error": "manual cancel is not supported for this strategy"})
            return
        if not ticker or not account:
            self.respond_json(400, {"ok": False, "error": "ticker and account are required"})
            return
        if not NT_DIR.is_dir():
            self.respond_json(500, {"ok": False, "error": "NinjaTrader user data directory not found"})
            return

        file_name = manual_cancel_command_file_name(strategy, ticker, account)
        path = NT_DIR / file_name
        try:
            path.write_text(datetime.now().isoformat(), encoding="utf-8")
        except OSError as ex:
            self.respond_json(500, {"ok": False, "error": str(ex)})
            return

        self.respond_json(200, {"ok": True})

    def respond_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def main() -> None:
    port = int(os.environ.get("PORT", "8766"))
    # HTTPServer sets allow_reuse_address=True; on Windows (unlike Linux) SO_REUSEADDR
    # lets a second process bind+LISTEN on the same port concurrently instead of only
    # easing TIME_WAIT rebinds, so two watchdog-spawned instances can silently coexist
    # with requests routed unpredictably between them. Disable it so a duplicate start
    # fails fast with "address already in use" instead of both listening quietly.
    ThreadingHTTPServer.allow_reuse_address = False
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Live dashboard: http://localhost:{port}/dashboard.html")
    print(f"Reading NinjaTrader files from: {NT_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()


