from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from html import escape
from pydantic import BaseModel, Field

from trend_model import (
    CLASSES,
    FEATURE_NAMES,
    MIN_SAMPLES_PER_GROUP,
    N_FEATURES,
    READY_MIN_DIRECTIONAL_PER_BUCKET,
    READY_MIN_LIVE,
    READY_MIN_NEAR_MISS_PER_BUCKET,
    WINDOW_SIZE,
    TrendMlEngine,
)
from trend_utils import validate_log_trend_sample_input, validate_predict_trend_input
import verification

ROOT = Path(__file__).resolve().parent
engine = TrendMlEngine(ROOT)

# Injected into "/restart" responses (this service's and its siblings') -- waits a
# couple seconds for the old process to actually die, then polls /health until the
# relaunched process answers, and redirects. %s is the page to land on. Covers both
# the dashboard's Restart link AND typing /restart in the URL bar directly, since
# both routes serve this same response.
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
app = FastAPI(title="NinjaTrader Trend ML Service", version="0.1.0")

# Separate schedule from the BB/VWAP service's 14:00 job so the two never
# retrain at the same moment and compete for CPU.
AUTO_RETRAIN_HOUR = 14
AUTO_RETRAIN_MINUTE = 5
AUTO_RETRAIN_CHECK_SECONDS = 30
auto_retrain_last_date: Optional[str] = None
auto_retrain_last_result: Optional[Dict[str, Any]] = None


class PredictTrendRequest(BaseModel):
    symbol: str = ""
    bars_period: str = Field(default="", description="e.g. 'Order Flow Delta'")
    timestamp: Optional[str] = None
    min_confidence: float = 0.65
    window: List[List[float]] = Field(default_factory=list, description=f"Rolling window of up to {WINDOW_SIZE} bars, each a row of {N_FEATURES} feature values")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LogTrendSampleRequest(BaseModel):
    symbol: str = ""
    bars_period: str = ""
    timestamp: Optional[str] = None
    label: str = Field(description="long, short, or no_trade")
    window: List[List[float]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RetrainRequest(BaseModel):
    symbol: Optional[str] = None
    bars_period: Optional[str] = None
    epochs: int = 30
    batch_size: int = 32
    learning_rate: float = 0.001


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_now() -> datetime:
    return datetime.now().astimezone()


def auto_retrain_log_path() -> Path:
    return ROOT / "data" / "auto_retrain.jsonl"


def load_auto_retrain_state() -> None:
    global auto_retrain_last_date, auto_retrain_last_result
    path = auto_retrain_log_path()
    if not path.exists():
        return
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return
        record = json.loads(lines[-1])
        if record.get("ok") is True:
            auto_retrain_last_date = str(record.get("local_date") or "")
            auto_retrain_last_result = record
    except Exception:
        return


def log_auto_retrain(record: Dict[str, Any]) -> None:
    path = auto_retrain_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine.append_jsonl(path, record)


async def auto_retrain_loop() -> None:
    global auto_retrain_last_date, auto_retrain_last_result
    while True:
        now = local_now()
        today = now.date().isoformat()
        due = (
            now.hour > AUTO_RETRAIN_HOUR
            or (now.hour == AUTO_RETRAIN_HOUR and now.minute >= AUTO_RETRAIN_MINUTE)
        )

        if due and auto_retrain_last_date != today:
            started_at = utc_now()
            try:
                result = await run_in_threadpool(engine.retrain, None, None, 30, 32, 0.001)
                auto_retrain_last_result = {
                    "ok": True, "started_at": started_at, "finished_at": utc_now(),
                    "local_date": today, "result": result,
                }
            except Exception as exc:
                auto_retrain_last_result = {
                    "ok": False, "started_at": started_at, "finished_at": utc_now(),
                    "local_date": today, "error": str(exc),
                }
            auto_retrain_last_date = today
            try:
                log_auto_retrain(auto_retrain_last_result)
            except Exception:
                pass  # a disk hiccup must not kill the retrain loop for good

        await asyncio.sleep(AUTO_RETRAIN_CHECK_SECONDS)


@app.on_event("startup")
async def startup() -> None:
    load_auto_retrain_state()
    asyncio.create_task(auto_retrain_loop())


@app.get("/health")
def health() -> Dict[str, Any]:
    groups = engine.all_group_health()
    return {
        "ok": True,
        "service": "nt_trend_ml_service",
        "groups_known": len(groups),
        "groups_ready": sum(1 for g in groups.values() if g.get("model_ready")),
        "n_features": N_FEATURES,
        "feature_names": FEATURE_NAMES,
        "classes": CLASSES,
        "min_samples_per_group": MIN_SAMPLES_PER_GROUP,
        "auto_retrain": {
            "enabled": True,
            "local_time": f"{AUTO_RETRAIN_HOUR:02d}:{AUTO_RETRAIN_MINUTE:02d}",
            "last_date": auto_retrain_last_date,
            "last_result": auto_retrain_last_result,
        },
    }


@app.post("/predict-trend")
def predict_trend(request: PredictTrendRequest) -> Dict[str, Any]:
    error = validate_predict_trend_input(request.model_dump(), N_FEATURES)
    if error:
        raise HTTPException(status_code=422, detail=error)

    result = engine.predict(request.symbol, request.bars_period, request.window, request.min_confidence)
    engine.append_jsonl(
        ROOT / "data" / "trend_predictions.jsonl",
        {
            "logged_at": utc_now(),
            "symbol": request.symbol,
            "bars_period": request.bars_period,
            "timestamp": request.timestamp,
            "window": request.window,
            "metadata": request.metadata,
            "result": result,
        },
    )
    return result


@app.post("/log-trend-sample")
def log_trend_sample(request: LogTrendSampleRequest) -> Dict[str, Any]:
    error = validate_log_trend_sample_input(request.model_dump(), N_FEATURES)
    if error:
        raise HTTPException(status_code=422, detail=error)

    engine.log_training_sample(
        {
            "logged_at": utc_now(),
            "symbol": request.symbol,
            "bars_period": request.bars_period,
            "timestamp": request.timestamp,
            "label": request.label.lower(),
            "window": request.window,
            "metadata": request.metadata,
        }
    )
    return {"ok": True, "label": request.label.lower()}


def run_retrain(
    symbol: Optional[str] = None,
    bars_period: Optional[str] = None,
    epochs: int = 30,
    batch_size: int = 32,
    learning_rate: float = 0.001,
) -> Dict[str, Any]:
    return engine.retrain(symbol=symbol, bars_period=bars_period, epochs=epochs, batch_size=batch_size, learning_rate=learning_rate)


@app.get("/retrain-trend", response_class=HTMLResponse)
def retrain_trend_from_browser(
    symbol: Optional[str] = None,
    bars_period: Optional[str] = None,
    epochs: int = 30,
    batch_size: int = 32,
    learning_rate: float = 0.001,
) -> str:
    started = local_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = run_retrain(symbol=symbol, bars_period=bars_period, epochs=epochs, batch_size=batch_size, learning_rate=learning_rate)
        title, status, detail = "Retrain complete", "ok", json.dumps(result, indent=2, default=str)
    except Exception as error:
        title, status, detail = "Retrain failed", "error", str(error)

    scope = f"{symbol or 'ALL'} / {bars_period or 'ALL SERIES'}"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;background:#111;color:#eee;}}
.box{{max-width:900px;border:1px solid #333;padding:20px;background:#1b1b1b;}}
.ok{{color:#7bd88f;}}.error{{color:#ff7b7b;}}
pre{{white-space:pre-wrap;background:#0b0b0b;padding:14px;border:1px solid #333;overflow:auto;}}
a{{color:#8ab4ff;}}</style></head>
<body><div class="box"><h1 class="{escape(status)}">{escape(title)}</h1>
<p>Started: {escape(started)}</p><p>Scope: {escape(scope)}</p>
<pre>{escape(detail)}</pre>
<p><a href="/trend-dashboard">Open dashboard</a></p></div></body></html>"""


@app.post("/retrain-trend")
def retrain_trend(request: RetrainRequest) -> Dict[str, Any]:
    return run_retrain(
        symbol=request.symbol, bars_period=request.bars_period,
        epochs=request.epochs, batch_size=request.batch_size, learning_rate=request.learning_rate,
    )


class RunCheckRequest(BaseModel):
    name: str


# A manual verification run must not fight the 14:05 auto-retrain for CPU, so we
# refuse to launch one inside a window bracketing it. Both jobs train models;
# overlapping them just makes each slower and muddies timing-sensitive results.
BLACKOUT_START = (13, 55)
BLACKOUT_END = (14, 15)


def _in_retrain_blackout() -> bool:
    now = local_now()
    cur = (now.hour, now.minute)
    return BLACKOUT_START <= cur <= BLACKOUT_END


async def _run_check_task(name: str) -> None:
    try:
        await run_in_threadpool(verification.run_check, name, engine, ROOT)
    except Exception:
        pass  # run_check records its own error record; never let the task explode


def _launch_check(name: str) -> Dict[str, Any]:
    if name not in verification.CHECKS:
        raise HTTPException(status_code=404, detail=f"unknown check '{name}'")
    if verification.CHECKS[name].get("heavy") and _in_retrain_blackout():
        raise HTTPException(status_code=409, detail="within the 14:05 auto-retrain window; try again after 14:15")
    ok, msg = verification.can_start(name, engine)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    verification._set(name, state="running", progress=0.0, message="queued", started_at=verification._now(), finished_at=None)
    asyncio.create_task(_run_check_task(name))
    return {"ok": True, "name": name, "state": "running"}


@app.post("/run-check")
async def run_check(request: RunCheckRequest) -> Dict[str, Any]:
    # async so asyncio.create_task in _launch_check has a running loop (a sync
    # route executes on a threadpool thread, where create_task raises).
    return _launch_check(request.name)


class RunAblationRequest(BaseModel):
    group: str


@app.post("/run-ablation")
async def run_ablation_route(request: RunAblationRequest) -> Dict[str, Any]:
    group = request.group.strip()
    readiness = engine.all_group_ablation_readiness()
    if group not in readiness:
        raise HTTPException(status_code=404, detail=f"unknown group '{group}'")
    if not readiness[group].get("ready"):
        raise HTTPException(status_code=409, detail=f"group '{group}' is not ready for a meaningful ablation")
    if verification.is_busy(verification.ablation_job_name(group)):
        raise HTTPException(status_code=409, detail="ablation already running for this group")
    if _in_retrain_blackout():
        raise HTTPException(status_code=409, detail="within the 14:05 auto-retrain window; try again after 14:15")

    verification._set(verification.ablation_job_name(group), state="running", progress=0.0,
                      message="queued", started_at=verification._now(), finished_at=None)

    async def _task() -> None:
        try:
            await run_in_threadpool(verification.run_ablation, group, ROOT)
        except Exception:
            pass  # run_ablation records its own failure state

    asyncio.create_task(_task())
    return {"ok": True, "group": group, "state": "running"}


@app.get("/trend-stats")
def trend_stats() -> Dict[str, Any]:
    return {
        "ok": True,
        "generated_at": local_now().isoformat(),
        "groups": engine.all_group_health(),
        "feature_names": FEATURE_NAMES,
        "classes": CLASSES,
    }


def _read_jsonl_tail(path: Path, limit: int = 50) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0


_RANGE_RE = re.compile(r"^(\d{1,2})d$")


def _normalize_range(value: str) -> str:
    value = (value or "all").strip().lower()
    return value if value == "all" or _RANGE_RE.match(value) else "all"


def _range_start_local(range_name: str) -> Optional[datetime]:
    # Same trading-day semantics as the Live (8766) and Models (8765) dashboards:
    # a "day" is anchored at the 15:00 local (California) session start, so 1d is
    # "since the most recent 3pm boundary", 3d adds the two sessions before it.
    match = _RANGE_RE.match(_normalize_range(range_name))
    if not match:
        return None
    now = datetime.now()
    anchor = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor - timedelta(days=int(match.group(1)) - 1)


def _row_time_local(row: Dict[str, Any]) -> Optional[datetime]:
    # Prediction rows carry a naive local "timestamp" (strategy bar clock, may
    # have 7-digit fractions); sample rows usually leave it empty and only have
    # the UTC-aware "logged_at". Normalize both to naive local for comparison
    # against _range_start_local.
    for field in ("timestamp", "logged_at"):
        text = str(row.get(field) or "")
        if not text:
            continue
        try:
            if "." in text:
                head, frac = text.split(".", 1)
                tz_tail = ""
                for sep in ("+", "-", "Z"):
                    idx = frac.find(sep)
                    if idx != -1:
                        tz_tail = frac[idx:].replace("Z", "+00:00")
                        frac = frac[:idx]
                        break
                text = f"{head}.{frac[:6]}{tz_tail}"
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    return None


def _filter_rows_by_range(rows: List[Dict[str, Any]], range_name: str) -> List[Dict[str, Any]]:
    start = _range_start_local(range_name)
    if start is None:
        return rows
    filtered = []
    for row in rows:
        row_time = _row_time_local(row)
        if row_time is not None and row_time >= start:
            filtered.append(row)
    return filtered


def _all_training_sample_rows() -> List[Dict[str, Any]]:
    path = ROOT / "data" / "trend_samples.jsonl"
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _label_counts_by_group(rows: List[Dict[str, Any]]) -> Dict[str, Counter]:
    counts: Dict[str, Counter] = {}
    for row in rows:
        key = str(row.get("symbol") or "").upper() + "_" + str(row.get("bars_period") or "").upper().replace(" ", "")
        label = str(row.get("label") or "").lower()
        if not key.strip("_") or label not in CLASSES:
            continue
        counts.setdefault(key, Counter())[label] += 1
    return counts


def _pct_cell(value: Any) -> str:
    return "" if value is None else f"{float(value) * 100:.1f}%"


def _prob_cell(probs: Dict[str, Any], name: str) -> str:
    value = probs.get(name)
    return "" if value is None else f"{float(value) * 100:.1f}%"


def _row_group_key(row: Dict[str, Any]) -> str:
    result = row.get("result") or {}
    group = result.get("group")
    if group:
        return str(group)
    return str(row.get("symbol") or "").upper() + "_" + str(row.get("bars_period") or "").upper().replace(" ", "")


def _order_flow_delta_value(row: Dict[str, Any]) -> Optional[float]:
    try:
        index = FEATURE_NAMES.index("order_flow_delta")
    except ValueError:
        return None
    window = row.get("window") or []
    if not window:
        return None
    last_bar = window[-1]
    if not isinstance(last_bar, list) or len(last_bar) <= index:
        return None
    try:
        return float(last_bar[index])
    except (TypeError, ValueError):
        return None


def _order_flow_delta_cell(row: Dict[str, Any]) -> str:
    value = _order_flow_delta_value(row)
    return "" if value is None else f"{value:.6f}"


def _bar_cell(value: int, max_value: int) -> str:
    pct = 0.0 if max_value <= 0 else max(0.0, min(100.0, value / max_value * 100.0))
    return f"<div class='bar'><span style='width:{pct:.1f}%'></span></div>"


def _training_history_rows(limit: int = 12) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted((ROOT / "weights").glob("*_trend_history.jsonl")):
        group = path.name.replace("_trend_history.jsonl", "")
        for row in _read_jsonl_tail(path, limit):
            row["group"] = group
            rows.append(row)
    rows.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return rows[:limit]


def _recall_cell(per_class: Optional[Dict[str, Any]], cls: str) -> str:
    if not per_class or cls not in per_class:
        return "<span class='muted'>--</span>"
    entry = per_class[cls]
    support = entry.get("support") or 0
    recall = entry.get("recall")
    if support == 0:
        return f"<span class='muted'>no examples (n=0)</span>"
    if recall is None:
        return f"<span class='muted'>n/a (n={support})</span>"
    css = "warn" if recall < 0.5 else ""
    return f"<span class='{css}'>{recall * 100:.0f}% (n={support})</span>"


def _status_pill(css_class: str, text: str, tip: str) -> str:
    return f"<span class='status-pill {css_class} th-tip' data-tip=\"{escape(tip)}\">{escape(text)}</span>"


def _status_cell(group: Dict[str, Any]) -> str:
    # Delegates every threshold decision to TrendMlEngine.classify_direction_status --
    # the same function predict()'s live ML gate uses -- so this pill can never say
    # a direction is fine while the strategy is actually blocked from trading it (or
    # vice versa). This function only owns the display text/tooltips.
    model_ready = bool(group.get("model_ready"))
    val_acc = group.get("val_acc")
    test_acc = group.get("test_acc")
    per_class = group.get("holdout_per_class") or {}

    for cls in ("long", "short"):
        status = TrendMlEngine.classify_direction_status(model_ready, val_acc, test_acc, per_class, cls)
        if status == "warming_up":
            return _status_pill(
                "status-warming", "WARMING UP",
                "Fewer than 100 labeled samples for this group -- not enough data to train yet.",
            )
        if status == "no_direction_tests":
            return _status_pill(
                "status-bad", f"NO {cls.upper()} TESTS",
                f"No {cls} examples landed in the holdout set, so recall can't be measured -- treated as unproven and blocked from live use.",
            )
        if status == "low_direction_recall":
            return _status_pill(
                "status-bad", f"LOW {cls.upper()} RECALL",
                f"Under 50% of actual {cls} examples in the holdout set were correctly called {cls} -- blocked regardless of overall accuracy.",
            )

    status = TrendMlEngine.classify_direction_status(model_ready, val_acc, test_acc, per_class, "")
    if status == "do_not_use":
        return _status_pill(
            "status-bad", "DO NOT USE",
            "Validation accuracy is below 50% -- worse than random guessing among 3 classes, not fit for live use.",
        )
    if status == "overfitting":
        return _status_pill(
            "status-bad", "OVERFITTING",
            "Validation and test accuracy differ by more than 10 points -- the model learned the training data, not a generalizable pattern.",
        )
    if status == "caution":
        return _status_pill(
            "status-caution", "CAUTION",
            "Validation accuracy is between 50% and 65% -- better than chance but not reliable enough to trust alone.",
        )

    return _status_pill(
        "status-good", "GOOD TO USE",
        "Validation accuracy is 65%+, test accuracy tracks it within 10 points, and Long/Short recall are both healthy -- passes the quality gate for live use.",
    )


# --- Dashboard self-monitoring: surface what a manual glance used to catch ---
# Liveness comes from the strategy's own heartbeat files, not from ML traffic.
# OpenTradeStatusExporter rewrites TrendTcn_<ticker>_<account>_open_trades.tsv on
# every OnBarUpdate (header-only while flat), so its mtime answers the question
# the banner is actually asking: is this instance running and is its data series
# ticking? ML samples/predictions only post on a gate trigger or near-miss, so a
# live-but-quiet instrument can go hours without one -- keying the banner on
# those made an ordinary thin session read as "market likely closed".
HEARTBEAT_PREFIX = "TrendTcn_"
HEARTBEAT_GLOB = HEARTBEAT_PREFIX + "*_open_trades.tsv"
SYSTEM_ACTIVE_MINUTES = 20
# Staleness is judged per instrument against that instrument's OWN measured
# cadence, because no single number can work here. Each series has its own
# TrendDeltaValue (NQ 400, ES 500, BTC 20, ...) calibrated to its own delta
# distribution -- raw order-flow delta doesn't scale between instruments -- so
# the thresholds aren't comparable and neither are the resulting bar rates.
# Measured on 2026-07-20: 6E writes every ~3-6 min while NQ ran 281 min between
# writes with everything perfectly healthy. A flat threshold is therefore either
# deaf to a fast instrument dying or noisy on a slow one; the first flat value
# tried (120) fired a false alarm on NQ within hours.
CADENCE_PATH = ROOT / "data" / "heartbeat_cadence.json"
# Warn at a multiple of the instrument's p95 observed gap, floored so a very
# chatty instrument doesn't get a hair-trigger.
CADENCE_MULTIPLE = 3.0
STALE_FLOOR_MINUTES = 120
# Until an instrument has enough measured gaps to characterise it, use a
# deliberately generous ceiling rather than guessing -- a missed warning during
# the learning window is cheaper than crying wolf and training the eye to
# ignore the banner.
CADENCE_MIN_GAPS = 8
STALE_LEARNING_MINUTES = 480
CADENCE_MAX_GAPS = 200
# Retired accounts and dropped instruments leave heartbeat files behind forever
# (there are ones here from July 9). Anything untouched this long isn't part of
# the current roster and must never raise a warning.
HEARTBEAT_ROSTER_HOURS = 24


def _heartbeat_mtimes() -> Dict[str, datetime]:
    """Most recent heartbeat write per symbol (the same ticker can run under
    more than one account, so the newest file wins)."""
    mtimes: Dict[str, datetime] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HEARTBEAT_ROSTER_HOURS)
    for path in ROOT.parent.glob(HEARTBEAT_GLOB):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        symbol = path.name[len(HEARTBEAT_PREFIX):].partition("_")[0].upper()
        if symbol and (symbol not in mtimes or mtime > mtimes[symbol]):
            mtimes[symbol] = mtime
    return mtimes


def _percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * pct)))
    return ordered[idx]


def _update_cadence(mtimes: Dict[str, datetime]) -> Dict[str, Any]:
    """Learn each instrument's normal gap between heartbeats by remembering the
    last mtime seen and recording the delta whenever it advances. Fail-soft: a
    corrupt or unwritable store degrades to the learning-window ceiling rather
    than taking the dashboard down."""
    try:
        store = json.loads(CADENCE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    symbols = store.get("symbols")
    if not isinstance(symbols, dict):
        symbols = {}

    changed = False
    for symbol, mtime in mtimes.items():
        entry = symbols.setdefault(symbol, {"last_mtime": None, "gaps": []})
        previous = entry.get("last_mtime")
        iso = mtime.isoformat()
        if previous == iso:
            continue
        if previous:
            try:
                gap = (mtime - datetime.fromisoformat(previous)).total_seconds() / 60.0
            except ValueError:
                gap = None
            if gap is not None and gap > 0:
                entry["gaps"] = ([round(gap, 2)] + list(entry.get("gaps") or []))[:CADENCE_MAX_GAPS]
        entry["last_mtime"] = iso
        changed = True

    if changed:
        try:
            CADENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CADENCE_PATH.write_text(json.dumps({"symbols": symbols}), encoding="utf-8")
        except OSError:
            pass
    return symbols


def _stale_threshold(entry: Dict[str, Any]) -> Tuple[float, bool]:
    """Returns (threshold_minutes, is_learned)."""
    gaps = [g for g in (entry.get("gaps") or []) if isinstance(g, (int, float))]
    if len(gaps) < CADENCE_MIN_GAPS:
        return STALE_LEARNING_MINUTES, False
    return max(STALE_FLOOR_MINUTES, CADENCE_MULTIPLE * _percentile(gaps, 0.95)), True


def _assess_staleness() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    mtimes = _heartbeat_mtimes()
    cadence = _update_cadence(mtimes)

    heartbeat_ages = {s: (now - m).total_seconds() / 60.0 for s, m in mtimes.items()}
    freshest_age = min(heartbeat_ages.values()) if heartbeat_ages else None
    system_active = freshest_age is not None and freshest_age <= SYSTEM_ACTIVE_MINUTES

    # Only flag an instrument that has gone quiet while at least one peer is
    # still writing -- that's what separates a single dead instance from
    # NinjaTrader being closed, where every heartbeat stops together.
    stale = []
    thresholds: Dict[str, Tuple[float, bool]] = {}
    for symbol, age in sorted(heartbeat_ages.items()):
        threshold, learned = _stale_threshold(cadence.get(symbol) or {})
        thresholds[symbol] = (threshold, learned)
        if system_active and age > threshold:
            stale.append((symbol, age, threshold, learned))

    return {"system_active": system_active, "freshest_age": freshest_age,
            "heartbeat_ages": heartbeat_ages, "thresholds": thresholds,
            "stale_warnings": stale}


def _assess_feature_anomalies(prediction_tail: List[Dict[str, Any]]) -> List[str]:
    """Guards specifically against the class of bug just fixed (one instrument's
    order_flow_delta standing in for another's) plus a per-group feed that has
    frozen -- both invisible to the accuracy/recall columns."""
    latest_row_by_group: Dict[str, Dict[str, Any]] = {}
    for row in reversed(prediction_tail):
        key = _row_group_key(row)
        if key and key not in latest_row_by_group:
            latest_row_by_group[key] = row

    warnings: List[str] = []

    # Cross-instrument identical: the exact signature of the CL-for-everyone bug.
    # Non-zero guard avoids the trivial "both happen to be 0" coincidence.
    value_to_groups: Dict[float, List[str]] = {}
    for key, row in latest_row_by_group.items():
        value = _order_flow_delta_value(row)
        if value is None or value == 0.0:
            continue
        value_to_groups.setdefault(round(value, 8), []).append(key)
    for value, keys in value_to_groups.items():
        if len(keys) >= 2:
            symbols = ", ".join(sorted(k.partition("_")[0] for k in keys))
            warnings.append(
                f"order_flow_delta identical ({value:.6f}) across {symbols} "
                "-- possible cross-instrument feed bug"
            )

    # Per-group frozen column: a full live window of one repeated value means the
    # feature isn't updating for that instrument (e.g. the accessor is throwing
    # and returning 0 every bar).
    try:
        idx = FEATURE_NAMES.index("order_flow_delta")
    except ValueError:
        idx = None
    if idx is not None:
        for key, row in latest_row_by_group.items():
            column = []
            for bar in row.get("window") or []:
                if isinstance(bar, list) and len(bar) > idx:
                    try:
                        column.append(round(float(bar[idx]), 8))
                    except (TypeError, ValueError):
                        pass
            if len(column) >= WINDOW_SIZE and len(set(column)) == 1:
                warnings.append(
                    f"order_flow_delta frozen (={column[0]:.6f}) for "
                    f"{key.partition('_')[0]} -- feature not updating"
                )
    return warnings


def _render_health_banner(staleness: Dict[str, Any], anomalies: List[str]) -> Tuple[str, str]:
    items: List[str] = []
    for symbol, age, threshold, learned in staleness["stale_warnings"]:
        basis = (f"its own normal gap puts the limit at {threshold:.0f} min"
                 if learned else
                 f"still learning this instrument's cadence, provisional limit {threshold:.0f} min")
        items.append(
            f"<li><strong>{escape(symbol)}</strong>: no heartbeat in {age:.0f} min "
            f"while other instruments are still writing ({basis}) -- if unexpected, "
            "check the strategy is enabled and compiling in NinjaTrader.</li>"
        )
    for message in anomalies:
        items.append(f"<li>{escape(message)}</li>")

    if items:
        level = "warn"
        count = len(items)
        title = f"{count} health warning" + ("" if count == 1 else "s")
        body = "<ul class='health-list'>" + "".join(items) + "</ul>"
    elif staleness["system_active"]:
        level = "ok"
        title = "Healthy"
        body = ("<p>Every running instrument is writing heartbeats and no feature "
                "anomalies were detected. Quiet instruments are normal -- the ML "
                "gate only fires on a setup or near-miss.</p>")
    else:
        level = "idle"
        title = "Idle"
        body = ("<p>No strategy instance has written a heartbeat recently -- "
                "NinjaTrader is likely closed or the strategies are disabled. "
                "Staleness checks paused; anomaly checks still active.</p>")

    return level, (f"<div class='health-banner health-{level}'>"
                   f"<div class='health-head'>{escape(title)}</div>{body}</div>")


_VERDICT_PILL = {
    "pass": ("status-good", "PASS"),
    "warn": ("status-caution", "WARN"),
    "fail": ("status-bad", "FAIL"),
    "error": ("status-bad", "ERROR"),
    "skip": ("status-muted", "SKIP"),
}


def _short_time(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except Exception:
        return str(iso)[:16]


def _build_verification_context() -> Dict[str, Any]:
    last = verification.last_results(ROOT)
    rows: List[str] = []
    passing = 0
    ready_count = 0

    for name, spec in verification.CHECKS.items():
        ready = spec["ready_fn"](engine)
        is_ready = bool(ready.get("ready"))
        if is_ready:
            ready_count += 1
        job = verification.job_state(name)
        running = job.get("state") == "running"

        rec = last.get(name)
        verdict = (rec or {}).get("verdict")
        if verdict == "pass":
            passing += 1

        # Readiness / live-state cell.
        if running:
            pct = int(round(float(job.get("progress") or 0) * 100))
            state_cell = f"<span class='status-pill status-info'>RUNNING</span> <span class='muted'>{pct}%</span>"
        elif is_ready:
            state_cell = "<span class='status-pill status-good'>READY</span>"
        else:
            state_cell = "<span class='status-pill status-muted'>NOT READY</span>"

        # Last-result cell.
        if verdict:
            css, txt = _VERDICT_PILL.get(verdict, ("status-muted", verdict.upper()))
            dur = (rec or {}).get("duration_s")
            dur_txt = f" <span class='muted'>{dur}s</span>" if dur is not None else ""
            result_cell = (f"<span class='status-pill {css}'>{escape(txt)}</span> "
                           f"<span class='muted'>{escape(_short_time((rec or {}).get('ts')))}</span>{dur_txt}")
        else:
            result_cell = "<span class='muted'>never run</span>"

        # Action cell.
        if running:
            action_cell = "<span class='muted'>running&hellip;</span>"
        elif is_ready:
            action_cell = f"<button class='run-btn' data-check='{escape(name)}'>Run</button>"
        else:
            action_cell = (f"<button class='run-btn' disabled title='{escape(str(ready.get('detail') or ''))}'>Run</button>")

        rows.append(
            "<tr>"
            f"<td title='{escape(spec.get('tip') or '')}'>{escape(spec.get('label') or name)}</td>"
            f"<td>{escape(spec.get('guards') or '')}</td>"
            f"<td>{state_cell}</td>"
            f"<td>{result_cell}</td>"
            f"<td>{action_cell}</td>"
            "</tr>"
        )

    if not rows:
        rows.append("<tr><td colspan='5' class='muted'>No checks registered</td></tr>")

    return {
        "verification_rows_html": "".join(rows),
        "verification_passing": passing,
        "verification_total": len(verification.CHECKS),
        "verification_ready_count": ready_count,
        "verification_last_sweep": _short_time(verification.last_sweep_time(ROOT)) or "--",
    }


def _build_dashboard_context(range_name: str = "all") -> Dict[str, Any]:
    range_name = _normalize_range(range_name)
    groups = engine.all_group_health()
    # Unfiltered copies feed the staleness/anomaly health checks -- system
    # liveness must not depend on which view range is picked. Everything
    # sample/prediction-derived below uses the range-filtered lists.
    sample_rows_all = _all_training_sample_rows()
    sample_rows = _filter_rows_by_range(sample_rows_all, range_name)
    label_counts = _label_counts_by_group(sample_rows)
    if range_name == "all":
        prediction_tail_all = _read_jsonl_tail(ROOT / "data" / "trend_predictions.jsonl", 500)
        prediction_tail = prediction_tail_all
    else:
        # Range active: filter over the whole file (it's small), not just the
        # tail, so total_predictions below counts the range honestly.
        full_predictions = _read_jsonl_tail(ROOT / "data" / "trend_predictions.jsonl", 10_000_000)
        prediction_tail_all = full_predictions[-500:]
        prediction_tail = _filter_rows_by_range(full_predictions, range_name)
    predictions = list(reversed(prediction_tail[-40:]))
    latest_order_flow_delta_by_group: Dict[str, str] = {}
    for row in reversed(prediction_tail):
        key = _row_group_key(row)
        if key and key not in latest_order_flow_delta_by_group:
            latest_order_flow_delta_by_group[key] = _order_flow_delta_cell(row)
    recent_signal_rows = [
        row for row in reversed(prediction_tail[-300:])
        if str((row.get("result") or {}).get("action") or "").lower() != "no_trade"
        # In-position confidence-decay polls use min_confidence 0, so their raw
        # long/short actions aren't entry signals -- keep them out of this panel.
        and str((row.get("metadata") or {}).get("purpose") or "") != "decay_check"
    ][:20]
    # Same file sample_rows came from -- take the tail of the (range-filtered)
    # in-memory list instead of re-reading the file unfiltered.
    recent_samples = list(reversed(sample_rows[-20:]))
    history_rows = _training_history_rows(12)

    model_rows = []
    label_rows = []
    symbol_cards = []

    def _plain_recall(per_class_map: Optional[Dict[str, Any]], cls: str) -> str:
        rec = ((per_class_map or {}).get(cls) or {}).get("recall")
        return "--" if rec is None else f"{rec * 100:.0f}%"

    for key in sorted(groups.keys()):
        g = groups[key]
        val_acc = "" if g.get("val_acc") is None else f"{g['val_acc'] * 100:.1f}%"
        test_acc = "" if g.get("test_acc") is None else f"{g['test_acc'] * 100:.1f}%"
        ready_cell = "Yes" if g["model_ready"] else f"No, {g['warmup_remaining']} left"
        per_class = g.get("holdout_per_class")
        long_recall = _recall_cell(per_class, "long")
        short_recall = _recall_cell(per_class, "short")
        no_trade_recall = _recall_cell(per_class, "no_trade")
        status = _status_cell(g)
        support = g.get("holdout_per_class") or {}
        counts = label_counts.get(key, Counter())
        total_labels = sum(counts.values())

        # Card-first summary of the same group: the five numbers that actually
        # gate trading (samples progress, val/test acc, long/short recall) with
        # the status pill carrying the gate reason in its tooltip. The full
        # 14-column table below stays as the detail view.
        samples = int(g["samples"])
        progress_pct = min(100.0, samples / max(1, MIN_SAMPLES_PER_GROUP) * 100.0)
        acc_line = (
            f"val {val_acc or '--'} · test {test_acc or '--'}"
            if g["model_ready"] else
            f"{samples} / {MIN_SAMPLES_PER_GROUP} samples to the training gate"
        )
        recall_line = (
            f"recall L {_plain_recall(per_class, 'long')} · S {_plain_recall(per_class, 'short')}"
            if g["model_ready"] else
            f"{g.get('live_samples', 0)} live · {g.get('near_miss_samples', 0)} near-miss"
        )
        symbol_cards.append(
            "<div class='sym-card'>"
            f"<div class='sym-head'><span class='sym-name'>{escape(str(g['symbol']))}</span>{status}</div>"
            f"<div class='bar'><span style='width:{progress_pct:.0f}%'></span></div>"
            f"<div class='sym-line'>{escape(acc_line)}</div>"
            f"<div class='sym-line muted-line'>{escape(recall_line)} · v{g['model_version']}</div>"
            "</div>"
        )
        model_rows.append(
            "<tr>"
            f"<td>{escape(str(g['symbol']))}</td>"
            f"<td class='num'>{escape(latest_order_flow_delta_by_group.get(key, ''))}</td>"
            f"<td class='num'>{g['samples']}{_bar_cell(int(g['samples']), MIN_SAMPLES_PER_GROUP)}</td>"
            f"<td class='num'>{g.get('live_samples', 0)} / {g.get('near_miss_samples', 0)}</td>"
            f"<td>{escape(ready_cell)}</td>"
            f"<td class='num'>{g['model_version']}</td>"
            f"<td>{escape(str(g.get('last_trained') or ''))}</td>"
            f"<td class='num'>{val_acc}</td>"
            f"<td class='num'>{test_acc}</td>"
            f"<td class='num'>{long_recall}</td>"
            f"<td class='num'>{short_recall}</td>"
            f"<td class='num'>{no_trade_recall}</td>"
            f"<td class='num'>{support.get('long', {}).get('support') or 0} / {support.get('short', {}).get('support') or 0} / {support.get('no_trade', {}).get('support') or 0}</td>"
            f"<td>{status}</td>"
            "</tr>"
        )
        label_rows.append(
            "<tr>"
            f"<td>{escape(str(g['symbol']))}</td>"
            f"<td class='num'>{escape(latest_order_flow_delta_by_group.get(key, ''))}</td>"
            f"<td class='num'>{counts.get('long', 0)}{_bar_cell(counts.get('long', 0), max(1, total_labels))}</td>"
            f"<td class='num'>{counts.get('short', 0)}{_bar_cell(counts.get('short', 0), max(1, total_labels))}</td>"
            f"<td class='num'>{counts.get('no_trade', 0)}{_bar_cell(counts.get('no_trade', 0), max(1, total_labels))}</td>"
            f"<td class='num'>{total_labels}</td>"
            "</tr>"
        )
    if not model_rows:
        model_rows.append("<tr><td colspan='14' class='muted'>No trend groups yet</td></tr>")
    if not label_rows:
        label_rows.append("<tr><td colspan='6' class='muted'>No label data yet</td></tr>")

    readiness = engine.all_group_ablation_readiness()
    ablation_last_runs = verification.last_ablation_runs(ROOT)
    ablation_rows = []
    for key in sorted(readiness.keys()):
        r = readiness[key]
        symbol_part = key.partition("_")[0]

        def bucket_cell(mc: int) -> str:
            b = r["buckets"][mc]
            pill_class = "status-good" if b["ready"] else "status-caution"
            return (
                f"<td class='num'>{b['count']}/{READY_MIN_NEAR_MISS_PER_BUCKET}"
                f"{_bar_cell(b['count'], READY_MIN_NEAR_MISS_PER_BUCKET)}"
                f"<span class='status-pill pill-xs {pill_class}'>{b['directional']}/{READY_MIN_DIRECTIONAL_PER_BUCKET} L/S</span></td>"
            )

        status_pill = (
            "<span class='status-pill status-good'>READY</span>" if r["ready"]
            else "<span class='status-pill status-caution'>NOT READY</span>"
        )

        job = verification.job_state(verification.ablation_job_name(key))
        last_run = ablation_last_runs.get(key)
        if job.get("state") == "running":
            action_cell = "<span class='status-pill status-info'>RUNNING</span>"
        elif r["ready"]:
            action_cell = f"<button class='run-btn' data-ablation='{escape(key)}'>Run</button>"
        else:
            action_cell = "<button class='run-btn' disabled title='Not enough data yet'>Run</button>"
        if last_run:
            ok = last_run.get("ok")
            run_pill = "status-good" if ok else "status-bad"
            run_txt = "OK" if ok else "FAILED"
            action_cell += (f" <span class='status-pill pill-xs {run_pill}'>{run_txt}</span>"
                            f" <span class='muted'>{escape(_short_time(last_run.get('ts')))}</span>")

        ablation_rows.append(
            "<tr>"
            f"<td>{escape(symbol_part)}</td>"
            f"<td class='num'>{r['live_count']}/{READY_MIN_LIVE}{_bar_cell(r['live_count'], READY_MIN_LIVE)}</td>"
            f"{bucket_cell(5)}{bucket_cell(4)}{bucket_cell(3)}"
            f"<td>{status_pill}</td>"
            f"<td>{action_cell}</td>"
            "</tr>"
        )
    if not ablation_rows:
        ablation_rows.append("<tr><td colspan='7' class='muted'>No trend groups yet</td></tr>")
    ablation_ready_count = sum(1 for r in readiness.values() if r["ready"])

    # Latest ablation output (most recent run across groups) for the card's
    # collapsible results block.
    ablation_output_html = ""
    if ablation_last_runs:
        latest = max(ablation_last_runs.values(), key=lambda rec: str(rec.get("ts") or ""))
        body = latest.get("output") or latest.get("stderr") or "(no output captured)"
        ablation_output_html = (
            f"<div class='muted' style='margin:8px 0 4px'>Latest run: <strong>{escape(str(latest.get('group')))}</strong> "
            f"at {escape(_short_time(latest.get('ts')))} ({latest.get('duration_s')}s, "
            f"{'ok' if latest.get('ok') else 'FAILED'})</div>"
            f"<pre style='white-space:pre-wrap;font-size:11.5px;max-height:340px;overflow:auto'>{escape(body)}</pre>"
        )

    prediction_rows = []
    for row in predictions:
        result = row.get("result") or {}
        probs = result.get("probabilities") or {}
        meta = row.get("metadata") or {}
        prediction_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('logged_at') or ''))}</td>"
            f"<td>{escape(str(row.get('timestamp') or ''))}</td>"
            f"<td>{escape(str(row.get('symbol') or ''))}</td>"
            f"<td class='num'>{escape(_order_flow_delta_cell(row))}</td>"
            f"<td>{escape(str(meta.get('gate_direction') or ''))}</td>"
            f"<td>{escape(str(result.get('action') or ''))}</td>"
            f"<td>{escape(str(result.get('raw_action') or ''))}</td>"
            f"<td class='num'>{_pct_cell(result.get('confidence'))}</td>"
            f"<td class='num'>{_prob_cell(probs, 'long')}</td>"
            f"<td class='num'>{_prob_cell(probs, 'short')}</td>"
            f"<td class='num'>{_prob_cell(probs, 'no_trade')}</td>"
            f"<td>{'Yes' if result.get('model_ready') else 'No'}</td>"
            f"<td>{escape(str(result.get('reason') or ''))}</td>"
            "</tr>"
        )
    if not prediction_rows:
        prediction_rows.append("<tr><td colspan='13' class='muted'>No predictions logged yet</td></tr>")

    signal_rows = []
    for row in recent_signal_rows:
        result = row.get("result") or {}
        meta = row.get("metadata") or {}
        signal_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('logged_at') or ''))}</td>"
            f"<td>{escape(str(row.get('symbol') or ''))}</td>"
            f"<td class='num'>{escape(_order_flow_delta_cell(row))}</td>"
            f"<td>{escape(str(meta.get('gate_direction') or ''))}</td>"
            f"<td>{escape(str(result.get('action') or ''))}</td>"
            f"<td class='num'>{_pct_cell(result.get('confidence'))}</td>"
            f"<td class='num'>{result.get('model_version') or 0}</td>"
            "</tr>"
        )
    if not signal_rows:
        signal_rows.append("<tr><td colspan='7' class='muted'>No long/short TrendTCN signals in the recent prediction log.</td></tr>")

    sample_table_rows = []
    for row in recent_samples:
        sample_table_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('logged_at') or ''))}</td>"
            f"<td>{escape(str(row.get('timestamp') or ''))}</td>"
            f"<td>{escape(str(row.get('symbol') or ''))}</td>"
            f"<td class='num'>{escape(_order_flow_delta_cell(row))}</td>"
            f"<td><span class='label-pill label-{escape(str(row.get('label') or '').lower())}'>{escape(str(row.get('label') or ''))}</span></td>"
            "</tr>"
        )
    if not sample_table_rows:
        sample_table_rows.append("<tr><td colspan='5' class='muted'>No training samples logged yet</td></tr>")

    history_table_rows = []
    for row in history_rows:
        history_table_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('ts') or ''))}</td>"
            f"<td>{escape(str(row.get('group') or ''))}</td>"
            f"<td class='num'>{row.get('samples') or 0}</td>"
            f"<td class='num'>{_pct_cell(row.get('val_acc'))}</td>"
            f"<td class='num'>{_pct_cell(row.get('test_acc'))}</td>"
            "</tr>"
        )
    if not history_table_rows:
        history_table_rows.append("<tr><td colspan='5' class='muted'>No training history yet</td></tr>")

    staleness = _assess_staleness()
    anomalies = _assess_feature_anomalies(prediction_tail_all)
    health_level, health_banner_html = _render_health_banner(staleness, anomalies)
    health_warning_count = len(staleness["stale_warnings"]) + len(anomalies)

    ready_groups = sum(1 for g in groups.values() if g.get("model_ready"))
    blocked_groups = sum(1 for g in groups.values() if "status-bad" in _status_cell(g))
    total_predictions = (
        _count_jsonl_lines(ROOT / "data" / "trend_predictions.jsonl")
        if range_name == "all"
        else len(prediction_tail)
    )
    last_prediction_time = predictions[0].get("logged_at") if predictions else ""
    total_samples = len(sample_rows)
    label_totals = Counter(str(row.get("label") or "").lower() for row in sample_rows)

    verification_ctx = _build_verification_context()

    return {
        "generated_at": local_now().isoformat(),
        **verification_ctx,
        "total_samples": total_samples,
        "total_predictions": total_predictions,
        "ready_groups": ready_groups,
        "group_count": len(groups),
        "blocked_groups": blocked_groups,
        "label_long": label_totals.get("long", 0),
        "label_short": label_totals.get("short", 0),
        "label_no_trade": label_totals.get("no_trade", 0),
        "last_prediction_time": str(last_prediction_time or ""),
        "ablation_ready_groups": ablation_ready_count,
        "ablation_group_count": len(readiness),
        "symbol_cards_html": "".join(symbol_cards),
        "model_rows_html": "".join(model_rows),
        "label_rows_html": "".join(label_rows),
        "signal_rows_html": "".join(signal_rows),
        "prediction_rows_html": "".join(prediction_rows),
        "sample_rows_html": "".join(sample_table_rows),
        "history_rows_html": "".join(history_table_rows),
        "ablation_rows_html": "".join(ablation_rows),
        "ablation_output_html": ablation_output_html,
        "health_level": health_level,
        "health_banner_html": health_banner_html,
        "health_warning_count": health_warning_count,
    }


@app.get("/restart", response_class=HTMLResponse)
def restart_service() -> str:
    # Mirrors MLService/service.py's /restart: this process can't restart itself
    # in-process (new code needs a fresh interpreter), so hand off to a helper
    # script that waits for this response to flush, kills whatever holds port
    # 8767, then relaunches uvicorn.
    restart_script = ROOT / "restart_trend_service.ps1"
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return f"<html><body><h2>Restart failed to launch: {escape(str(exc))}</h2></body></html>"

    return (
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h2>Restarting Trend ML service&hellip;</h2>"
        "<p id='restartStatus'>Shutting down and relaunching uvicorn&hellip; this page "
        "will reload automatically once it's back.</p>"
        "<p><a href='/trend-dashboard'>Go to the dashboard now</a></p>"
        + (RESTART_POLL_SCRIPT % "/trend-dashboard") +
        "</body></html>"
    )


@app.get("/trend-dashboard-data")
def trend_dashboard_data(range_name: str = Query("all", alias="range")) -> Dict[str, Any]:
    return _build_dashboard_context(range_name)


@app.get("/", response_class=HTMLResponse)
@app.get("/trend-dashboard", response_class=HTMLResponse)
def trend_dashboard() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trend ML Dashboard</title>
<style>
:root{
  /* Shared token set with the Live (8766) and Models (8765) dashboards. */
  --bg:#0b0d10;
  --bg2:#0b0d10;
  --panel:#14171c;
  --panel-solid:#14171c;
  --border:rgba(235,240,245,0.10);
  --border-soft:rgba(235,240,245,0.06);
  --text:#e8eaed;
  --muted:#8f959d;
  --accent:#4c8dff;
  --accent2:#4c8dff;
  --accent3:#8f959d;
  --win:#00c46a;
  --loss:#ff5c74;
  --warn:#e8b64c;
  --radius:12px;
  --radius-sm:8px;
}
*{box-sizing:border-box;}
@media (prefers-reduced-motion: reduce){*{animation-duration:.001ms !important;transition-duration:.001ms !important;}}
html{scroll-behavior:smooth;}
body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;background:var(--bg);
  color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased;}
::-webkit-scrollbar{width:10px;height:10px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:999px;}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,0.28);}
h1,h2{font-family:inherit;}

.stickytop{position:sticky;top:0;z-index:50;}
header.top{padding:14px 22px;background:rgba(10,10,15,0.72);
  backdrop-filter:blur(14px) saturate(140%);-webkit-backdrop-filter:blur(14px) saturate(140%);
  border-bottom:1px solid var(--border);}
.top-row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;}
.brand{display:flex;align-items:center;gap:12px;}
.brand .dot{width:10px;height:10px;border-radius:50%;background:var(--loss);}
.brand .dot.ok{background:var(--win);box-shadow:0 0 0 0 rgba(52,229,143,0.6);animation:pulse 2s infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,229,143,0.55);}70%{box-shadow:0 0 0 9px rgba(52,229,143,0);}100%{box-shadow:0 0 0 0 rgba(52,229,143,0);}}
h1{margin:0;font-size:18px;font-weight:700;letter-spacing:-0.01em;color:var(--text);}
.page-switch{display:inline-flex;align-items:center;gap:2px;border:1px solid var(--border);
  border-radius:8px;background:var(--panel);padding:3px;margin-left:12px;}
.page-switch a{color:var(--muted);text-decoration:none;font-size:12.5px;font-weight:600;
  padding:4px 12px;border-radius:6px;white-space:nowrap;}
.page-switch a:hover{color:var(--text);text-decoration:none;}
.page-switch a.current{background:rgba(235,240,245,0.08);color:var(--text);}
.overflow-menu{position:relative;}
.overflow-btn{border:1px solid var(--border);background:var(--panel);color:var(--muted);
  border-radius:8px;padding:5px 11px;font:inherit;font-weight:700;cursor:pointer;line-height:1;}
.overflow-btn:hover{color:var(--text);}
.overflow-pop{display:none;position:absolute;right:0;top:calc(100% + 8px);z-index:60;min-width:230px;
  background:var(--panel-solid);border:1px solid var(--border);border-radius:10px;padding:6px;
  box-shadow:0 10px 30px rgba(0,0,0,0.5);}
.overflow-pop.open{display:block;}
.overflow-pop a{display:block;color:var(--text);font-size:13px;padding:8px 10px;border-radius:7px;text-decoration:none;}
.overflow-pop a:hover{background:rgba(235,240,245,0.06);text-decoration:none;}
.overflow-pop .danger{color:var(--loss);}
.sym-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-bottom:22px;}
.sym-card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;}
.sym-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px;}
.sym-name{font-size:17px;font-weight:700;letter-spacing:-0.01em;}
.sym-card .bar{margin:0 0 9px;}
.sym-line{font-size:12.5px;color:var(--text);margin-top:3px;font-variant-numeric:tabular-nums;}
.sym-line.muted-line{color:var(--muted);font-size:11.5px;}
.mix-chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px;}
.mix-chip{border:1px solid var(--border);background:var(--panel);color:var(--muted);border-radius:999px;
  padding:5px 13px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;}
.mix-chip:hover{color:var(--text);}
.mix-chip[data-active="true"]{background:var(--accent);border-color:transparent;color:#0b0d10;}
.meta-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:8px;}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);
  background:var(--panel);border:1px solid var(--border);padding:6px 12px;border-radius:999px;}
.chip strong{color:var(--text);font-weight:600;}
.muted{color:var(--muted);}
.hint{color:var(--muted);font-size:12px;margin-top:8px;}
a{color:var(--accent2);text-decoration:none;}
a:hover{text-decoration:underline;}

nav.pillnav{display:flex;gap:8px;overflow-x:auto;padding:10px 22px 12px;background:rgba(10,10,15,0.55);
  border-bottom:1px solid var(--border-soft);scrollbar-width:thin;}
nav.pillnav a{flex:0 0 auto;font-size:12.5px;font-weight:500;color:var(--muted);background:var(--panel);
  border:1px solid var(--border);padding:7px 13px;border-radius:999px;white-space:nowrap;transition:all .15s ease;}
nav.pillnav a:hover{color:var(--text);border-color:var(--accent);text-decoration:none;transform:translateY(-1px);}
nav.pillnav a.active{color:#0b0d10;background:var(--accent);border-color:transparent;}

main{padding:22px 22px 60px;max-width:1600px;margin:0 auto;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:22px;}
@keyframes rise{to{opacity:1;transform:translateY(0);}}
.card{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px;transition:border-color .2s ease;}
.card:hover{border-color:rgba(76,141,255,0.5);}
.card div{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;}
.card strong{display:block;font-size:26px;margin-top:6px;font-weight:700;font-variant-numeric:tabular-nums;}

section{margin-top:18px;scroll-margin-top:110px;background:var(--panel);border:1px solid var(--border-soft);
  border-radius:var(--radius);padding:4px 18px 18px;}
section h2{font-size:15px;margin:0;padding:14px 0 12px;font-weight:600;display:flex;align-items:center;gap:9px;
  cursor:pointer;user-select:none;color:var(--text);}
section h2::after{content:"\\25BE";margin-left:auto;color:var(--muted);font-size:12px;transition:transform .2s ease;}
section.collapsed h2::after{transform:rotate(-90deg);}
section .sec-body{overflow:hidden;}
section.collapsed .sec-body{display:none;}
.sec-icon{font-size:15px;}

.tablewrap{overflow-x:auto;border-radius:var(--radius-sm);}
table{border-spacing:0;width:auto;background:var(--panel-solid);border:1px solid var(--border-soft);
  border-radius:var(--radius-sm);overflow:hidden;white-space:nowrap;}
th,td{padding:7px 12px;border-bottom:1px solid var(--border-soft);text-align:left;font-size:12px;font-variant-numeric:tabular-nums;}
th{color:var(--muted);background:rgba(255,255,255,0.03);font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.02em;position:sticky;top:0;}
th.num{text-align:right;}
th[data-col]{cursor:pointer;user-select:none;}
th[data-col]:hover{color:var(--text);background:rgba(124,92,255,0.1);}
th[data-col]::after{content:"";margin-left:4px;color:var(--accent2);font-size:10px;}
th[data-col][data-sort="asc"]::after{content:"\\25B2";}
th[data-col][data-sort="desc"]::after{content:"\\25BC";}
tbody tr{transition:background .12s ease;}
tbody tr:nth-child(even){background:rgba(255,255,255,0.015);}
tbody tr:hover{background:rgba(124,92,255,0.08);}
tbody tr:last-child td{border-bottom:none;}
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}

.bar{height:8px;background:rgba(255,255,255,0.06);border-radius:4px;overflow:hidden;min-width:90px;margin-top:4px;}
.bar span{display:block;height:100%;background:rgba(235,240,245,0.35);transition:width .6s ease;}
.warn{color:var(--warn);}
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;color:#fff;
  font-weight:700;font-size:11.5px;white-space:nowrap;}
.status-pill.pill-xs{display:inline-flex;padding:1px 6px;font-size:9.5px;gap:3px;margin-top:3px;}
.status-pill.pill-xs::before{width:5px;height:5px;}
.status-pill::before{content:"";width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,.85);}
.status-warming{background:#4c4f54;}
.status-bad{background:var(--loss);}
.status-caution{background:var(--warn);color:#241a00;}
.status-good{background:var(--win);color:#00170a;}
.status-info{background:var(--accent2);color:#00161c;}
.status-muted{background:#3a3f4a;color:#c9cfda;}
.run-btn{background:var(--panel);border:1px solid var(--border);color:var(--text);font:inherit;font-size:11.5px;
  font-weight:600;padding:5px 14px;border-radius:8px;cursor:pointer;transition:border-color .15s ease,background .15s ease;}
.run-btn:hover:not(:disabled){border-color:var(--accent);background:rgba(124,92,255,0.12);}
.run-btn:disabled{opacity:.35;cursor:not-allowed;}
.label-pill{padding:2px 9px;border-radius:999px;background:#4c4f54;color:#fff;font-weight:600;font-size:12px;}
.label-long{background:var(--win);color:#00170a;}
.label-short{background:var(--loss);color:#2a0510;}
.label-no_trade{background:#4c4f54;}

.sec-desc{color:var(--muted);font-size:12.5px;line-height:1.5;margin:0 0 12px;padding:10px 12px;
  background:rgba(124,92,255,0.06);border:1px solid var(--border-soft);border-radius:var(--radius-sm);}
.sec-desc strong{color:var(--text);font-weight:600;}
[data-tip]{cursor:help;}
th.th-tip .th-tip-label{border-bottom:1px dotted var(--muted);}
/* Floating tooltip shared by column headers (th.th-tip) and, per-row, status
   pills. Appended to <body> at runtime (see the initHeaderTooltip script
   below) instead of living inside the target -- the tables sit in
   .tablewrap (overflow-x:auto), which would clip an absolutely-positioned
   bubble nested inside either. */
#thTip{position:fixed;display:none;z-index:100;max-width:240px;background:var(--panel-solid);
  border:1px solid var(--border);border-radius:var(--radius-sm);padding:9px 11px;font-size:11.5px;
  font-weight:400;text-transform:none;letter-spacing:normal;line-height:1.45;color:var(--text);
  white-space:normal;box-shadow:0 10px 28px rgba(0,0,0,0.55);pointer-events:none;}

.health-banner{border-radius:var(--radius);padding:13px 16px;margin-bottom:16px;border:1px solid var(--border);}
.health-banner .health-head{font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px;}
.health-banner .health-head::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor;flex:0 0 auto;}
.health-banner p{margin:4px 0 0;color:var(--text);font-size:12.5px;}
.health-list{margin:7px 0 0;padding-left:20px;font-size:12.5px;line-height:1.55;color:var(--text);}
.health-list li{margin:3px 0;}
.health-ok{background:rgba(52,229,143,0.08);border-color:rgba(52,229,143,0.35);color:var(--win);}
.health-warn{background:rgba(255,92,122,0.10);border-color:rgba(255,92,122,0.45);color:var(--loss);}
.health-idle{background:var(--panel);border-color:var(--border);color:var(--muted);}
footer.fab{position:fixed;right:20px;bottom:20px;z-index:60;background:var(--panel-solid);border:1px solid var(--border);
  color:var(--text);border-radius:999px;padding:10px 16px;font-size:12px;font-weight:600;
  box-shadow:0 6px 20px rgba(0,0,0,0.45);cursor:pointer;}
@media (max-width:640px){
  main{padding:12px 12px 70px;}
  header.top{padding:10px 12px;}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}
  .card strong{font-size:21px;}
  .page-switch{margin-left:0;}
  section{padding:2px 12px 12px;}
  /* Phone: tables become stacked label/value rows (labels stamped from each
     table's own thead by the m-stack script) instead of scrolling sideways. */
  .tablewrap{overflow:visible;}
  table.m-stack{white-space:normal;width:100%;}
  table.m-stack thead{display:none;}
  table.m-stack tbody tr{display:block;padding:8px 0;border-bottom:1px solid var(--border-soft);}
  table.m-stack tbody tr:last-child{border-bottom:none;}
  table.m-stack tbody td{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:3px 4px;border-bottom:none;text-align:right;white-space:normal;}
  table.m-stack tbody td::before{content:attr(data-label);color:var(--muted);font-size:10.5px;
    text-transform:uppercase;letter-spacing:.04em;flex:none;text-align:left;}
}
</style></head>
<body>
<div class="stickytop">
<header class="top">
  <div class="top-row">
    <div class="brand">
      <span id="liveDot" class="dot"></span>
      <h1>Trend ML</h1>
      <nav class="page-switch" aria-label="Dashboards">
        <a data-port="8766" data-path="/">Live</a>
        <a data-port="8765" data-path="/dashboard">Models</a>
        <a data-port="8765" data-path="/ops">Ops</a>
        <a class="current" href="/">Trend</a>
      </nav>
      <nav class="page-switch" id="rangeSwitch" aria-label="Data range" title="Filters sample- and prediction-derived panels (KPI counts, label mix, predictions, signals, recent samples). Model health and the health banner always show current state. Days are trading days starting 3pm California time.">
        <a href="#" data-range="1d">1D</a>
        <a href="#" data-range="2d">2D</a>
        <a href="#" data-range="3d">3D</a>
        <a href="#" data-range="5d">5D</a>
        <a href="#" data-range="all" class="current">All</a>
      </nav>
    </div>
    <div class="meta-row">
      <span class="chip"><span id="liveText">Connecting</span></span>
      <span class="chip">Updated <strong id="lastUpdate">--</strong></span>
      <div class="overflow-menu">
        <button type="button" class="overflow-btn" id="overflowBtn" aria-label="More actions">&#8943;</button>
        <div class="overflow-pop" id="overflowPop">
          <a href="/trend-stats" target="_blank" rel="noopener">Raw JSON: /trend-stats</a>
          <a href="/retrain-trend">Manual retrain</a>
          <a href="/restart" class="danger" onclick="return confirm('Restart the trend ML service?');">&#8635; Restart trend service</a>
        </div>
      </div>
    </div>
  </div>
</header>
<nav class="pillnav" id="pillNav"></nav>
</div>
<main>
<div id="healthBanner"></div>
<div class="sym-cards" id="symbolCards"></div>
<div class="grid">
  <div class="card"><div>Training Samples</div><strong id="totalSamples">0</strong></div>
  <div class="card"><div>Predictions Logged</div><strong id="totalPredictions">0</strong></div>
  <div class="card"><div>Groups Ready</div><strong><span id="readyCount">0</span> / <span id="groupsTotal">0</span></strong></div>
  <div class="card"><div>Blocked By Validation</div><strong id="blockedCount">0</strong></div>
  <div class="card"><div>Long / Short / No Trade</div><strong><span id="labelLong">0</span> / <span id="labelShort">0</span> / <span id="labelNoTrade">0</span></strong></div>
  <div class="card"><div>Last Prediction</div><strong style="font-size:13px" id="lastPrediction">--</strong></div>
  <div class="card"><div>Ablation Ready</div><strong><span id="ablationReadyCount">0</span> / <span id="ablationGroupsTotal">0</span></strong></div>
  <div class="card"><div>Checks Passing</div><strong><span id="verificationPassing">0</span> / <span id="verificationTotal">0</span></strong></div>
</div>

<section class="collapsed"><h2>Model Health &mdash; Full Table</h2>
<p class="sec-desc">The detail view behind the symbol cards above. Predicts trade direction (<strong>long / short / no_trade</strong>) per symbol from order-flow-delta windows. A group sits at <strong>WARMING UP</strong> until it collects <strong>100 labeled samples</strong>. Because <code>no_trade</code> is the majority class, overall accuracy can look good even when the model never correctly calls a Long or Short -- so the gate also requires healthy <strong>Long/Short Recall</strong>, not just <strong>65%+ Val Acc</strong> with less than a <strong>10-point</strong> gap to Test Acc. Hover any column header for details.</p>
<div class="tablewrap"><table><thead><tr>
<th data-col="0" class="th-tip" data-tip="Instrument this model trains on, e.g. ES, NQ, RTY. Trend models train per-symbol, not per data series."><span class="th-tip-label">Symbol</span></th>
<th data-col="1" class="num th-tip" data-tip="The model's core input signal -- cumulative order flow delta at the time of the most recent prediction for this symbol."><span class="th-tip-label">Order Flow Delta</span></th>
<th data-col="2" class="num th-tip" data-tip="Labeled training examples collected so far for this group. Training isn't even attempted until this hits 100."><span class="th-tip-label">Samples</span></th>
<th data-col="3" class="num th-tip" data-tip="Real fills vs. near-miss (paper-traded) samples that make up the Samples count."><span class="th-tip-label">Live / Near-Miss</span></th>
<th data-col="4" class="th-tip" data-tip="Whether this group has passed the 100-sample minimum and has a trained model saved to disk."><span class="th-tip-label">Ready</span></th>
<th data-col="5" class="num th-tip" data-tip="How many times this group's model has been retrained from scratch."><span class="th-tip-label">Version</span></th>
<th data-col="6" class="th-tip" data-tip="Timestamp of the most recent training run for this group."><span class="th-tip-label">Last Trained</span></th>
<th data-col="7" class="num th-tip" data-tip="Accuracy on the held-out validation split. Dominated by no_trade (the majority class), so check Long/Short Recall too before trusting this number."><span class="th-tip-label">Val Acc</span></th>
<th data-col="8" class="num th-tip" data-tip="Accuracy on a second held-out split, never seen during training. Compared against Val Acc to catch overfitting -- a gap over 10 points flags OVERFITTING."><span class="th-tip-label">Test Acc</span></th>
<th data-col="9" class="num th-tip" data-tip="Of the actual LONG examples in the holdout set, the percent the model correctly called LONG. Zero long test examples or recall under 50% blocks the model regardless of overall accuracy."><span class="th-tip-label">Long Recall</span></th>
<th data-col="10" class="num th-tip" data-tip="Of the actual SHORT examples in the holdout set, the percent the model correctly called SHORT. Zero short test examples or recall under 50% blocks the model regardless of overall accuracy."><span class="th-tip-label">Short Recall</span></th>
<th data-col="11" class="num th-tip" data-tip="Of the actual NO_TRADE examples in the holdout set, the percent the model correctly called NO_TRADE. Usually high since it's the majority class."><span class="th-tip-label">No Trade Recall</span></th>
<th data-col="12" class="num th-tip" data-tip="How many Long / Short / No Trade examples landed in the holdout set used to compute the recall columns."><span class="th-tip-label">Holdout L/S/NT</span></th>
<th data-col="13" class="th-tip" data-tip="Overall quality gate for this group. Hover the status badge itself for what that specific result means."><span class="th-tip-label">Status</span></th>
</tr></thead>
<tbody id="modelRows"></tbody></table></div></section>

<section><h2>Ablation Readiness</h2>
<p class="muted">Progress toward having enough real + near-miss data to run <code>tools/ablate_near_miss_weights.py</code> meaningfully for each group.</p>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Symbol</th><th data-col="1" class="num">Live</th><th data-col="2" class="num">5/6 Match</th>
<th data-col="3" class="num">4/6 Match</th><th data-col="4" class="num">3/6 Match</th><th data-col="5">Status</th>
<th data-col="6">Action</th>
</tr></thead>
<tbody id="ablationRows"></tbody></table></div>
<div id="ablationOutput"></div></section>

<section><h2>Verification Suite</h2>
<p class="sec-desc">Integrity and leakage checks that run alongside the ablation. Each check has a <strong>Readiness</strong> gate (enough data to run) and a last <strong>PASS / FAIL</strong> verdict. Click <strong>Run</strong> when a check is <strong>READY</strong>; heavy checks train models in the background and refuse to launch inside the 14:05 auto-retrain window. <strong>Permutation</strong>: retrains on shuffled labels -- validation accuracy must fall back to the base rate, or the pipeline is leaking.</p>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Check</th><th data-col="1">Guards</th><th data-col="2">Readiness</th>
<th data-col="3">Last Result</th><th data-col="4">Action</th>
</tr></thead>
<tbody id="verificationRows"></tbody></table></div></section>

<section><h2>Label Balance</h2>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Symbol</th><th data-col="1" class="num">Latest Order Flow Delta</th><th data-col="2" class="num">Long</th>
<th data-col="3" class="num">Short</th><th data-col="4" class="num">No Trade</th><th data-col="5" class="num">Total</th>
</tr></thead>
<tbody id="labelRows"></tbody></table></div></section>

<section><h2>Recent Non-No-Trade Signals</h2>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Logged</th><th data-col="1">Symbol</th><th data-col="2" class="num">Order Flow Delta</th><th data-col="3">Gate</th>
<th data-col="4">Action</th><th data-col="5" class="num">Confidence</th><th data-col="6" class="num">Version</th>
</tr></thead>
<tbody id="signalRows"></tbody></table></div></section>

<section class="collapsed"><h2>Recent Predictions</h2>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Logged</th><th data-col="1">Bar Time</th><th data-col="2">Symbol</th><th data-col="3" class="num">Order Flow Delta</th>
<th data-col="4">Gate</th><th data-col="5">Action</th><th data-col="6">Raw</th><th data-col="7" class="num">Confidence</th>
<th data-col="8" class="num">Long</th><th data-col="9" class="num">Short</th><th data-col="10" class="num">No Trade</th><th data-col="11">Ready</th><th data-col="12">Reason</th>
</tr></thead>
<tbody id="predictionRows"></tbody></table></div></section>

<section><h2>Recent Training Samples</h2>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Logged</th><th data-col="1">Bar Time</th><th data-col="2">Symbol</th><th data-col="3" class="num">Order Flow Delta</th><th data-col="4">Label</th>
</tr></thead>
<tbody id="sampleRows"></tbody></table></div></section>

<section class="collapsed"><h2>Training History</h2>
<div class="tablewrap"><table><thead><tr>
<th data-col="0">Trained At</th><th data-col="1">Group</th><th data-col="2" class="num">Samples</th><th data-col="3" class="num">Val Acc</th><th data-col="4" class="num">Test Acc</th>
</tr></thead>
<tbody id="historyRows"></tbody></table></div></section>
</main>
<button class="fab" id="topBtn" title="Back to top">↑ Top</button>
<script>
// --- Activity: merge the four "Recent ..." sections into one tabbed feed.
// Runs BEFORE the section wrapper below so the merged section gets normal
// collapse handling and a single nav pill. tbody ids are moved, not
// recreated, so the 7s refresh keeps writing into them unchanged. ---
(function() {
  var parts = [
    ['Recent Non-No-Trade Signals', 'Signals'],
    ['Recent Predictions', 'Predictions'],
    ['Recent Training Samples', 'Samples'],
    ['Training History', 'Training']
  ];
  var found = [];
  document.querySelectorAll('main > section').forEach(function(sec) {
    var h2 = sec.querySelector('h2');
    if (!h2) return;
    var hit = parts.filter(function(p) { return p[0] === h2.textContent.trim(); })[0];
    if (hit) found.push({ sec: sec, label: hit[1] });
  });
  if (found.length < 2) return;
  var wrap = document.createElement('section');
  var head = document.createElement('h2');
  head.textContent = 'Activity';
  wrap.appendChild(head);
  var chipRow = document.createElement('div');
  chipRow.className = 'mix-chips';
  wrap.appendChild(chipRow);
  var panes = [];
  found.forEach(function(entry, i) {
    var pane = document.createElement('div');
    pane.className = 'mix-pane';
    var h2 = entry.sec.querySelector('h2');
    while (h2.nextSibling) pane.appendChild(h2.nextSibling);
    pane.style.display = i === 0 ? '' : 'none';
    panes.push(pane);
    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'mix-chip';
    chip.textContent = entry.label;
    chip.dataset.active = i === 0 ? 'true' : 'false';
    chip.addEventListener('click', function() {
      panes.forEach(function(p) { p.style.display = p === pane ? '' : 'none'; });
      chipRow.querySelectorAll('.mix-chip').forEach(function(c) { c.dataset.active = c === chip ? 'true' : 'false'; });
    });
    chipRow.appendChild(chip);
    wrap.appendChild(pane);
  });
  found[0].sec.parentNode.insertBefore(wrap, found[0].sec);
  found.forEach(function(entry) { entry.sec.parentNode.removeChild(entry.sec); });
})();

(function() {
  var sections = Array.prototype.slice.call(document.querySelectorAll('main > section'));
  var nav = document.getElementById('pillNav');

  sections.forEach(function(sec, i) {
    var h2 = sec.querySelector('h2');
    if (!h2) return;
    var title = h2.textContent.trim();
    var slug = 'sec-' + title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
    if (!sec.id) sec.id = slug;

    var body = document.createElement('div');
    body.className = 'sec-body';
    while (h2.nextSibling) { body.appendChild(h2.nextSibling); }
    sec.appendChild(body);

    var saved = localStorage.getItem('trenddash-collapsed-' + sec.id);
    var collapsed = saved !== null ? saved === '1' : sec.classList.contains('collapsed');
    sec.classList.toggle('collapsed', collapsed);

    h2.addEventListener('click', function() {
      sec.classList.toggle('collapsed');
      localStorage.setItem('trenddash-collapsed-' + sec.id, sec.classList.contains('collapsed') ? '1' : '0');
    });

    var pill = document.createElement('a');
    pill.href = '#' + sec.id;
    pill.textContent = title;
    nav.appendChild(pill);
  });

  var pills = Array.prototype.slice.call(nav.querySelectorAll('a'));
  var activePill = null;
  window.addEventListener('scroll', function() {
    var pos = window.scrollY + 130;
    var current = sections[0] && sections[0].id;
    // getBoundingClientRect, not offsetTop -- offsetTop is relative to the
    // nearest positioned ancestor, not the document, so it silently gives the
    // wrong scroll threshold for any section that isn't a direct static-positioned
    // child.
    sections.forEach(function(sec) {
      if (sec.getBoundingClientRect().top + window.scrollY <= pos) current = sec.id;
    });
    var next = null;
    pills.forEach(function(p) {
      var isActive = p.getAttribute('href') === '#' + current;
      p.classList.toggle('active', isActive);
      if (isActive) next = p;
    });
    // nav.pillnav scrolls horizontally (overflow-x: auto) once there are more
    // pills than fit the header width, so the active pill can end up off-screen
    // with no visible cue -- keep it in view as the page section changes.
    // behavior:"instant" (not "smooth"/default): smooth scrollIntoView is a
    // rAF-driven animation that can silently stall and never move the nav's
    // scroll position at all; instant always lands correctly.
    if (next && next !== activePill) {
      activePill = next;
      next.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
    }
  });

  var topBtn = document.getElementById('topBtn');
  topBtn.style.display = 'none';
  window.addEventListener('scroll', function() {
    topBtn.style.display = window.scrollY > 400 ? 'block' : 'none';
  });
  topBtn.addEventListener('click', function() { window.scrollTo({ top: 0, behavior: 'smooth' }); });
})();

// --- sortable tables, sort survives the 7s live refresh ---
var tableSortState = {};
function sortTableBody(tbody, col, dir) {
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  rows.sort(function(a, b) {
    var av = (a.children[col] && a.children[col].textContent.trim()) || '';
    var bv = (b.children[col] && b.children[col].textContent.trim()) || '';
    var an = parseFloat(av.replace(/[^-0-9.]/g, '')), bn = parseFloat(bv.replace(/[^-0-9.]/g, ''));
    var cmp = (!isNaN(an) && !isNaN(bn) && /^-?[\\d.]/.test(av) && /^-?[\\d.]/.test(bv)) ? (an - bn) : av.localeCompare(bv);
    return cmp * dir;
  });
  rows.forEach(function(tr) { tbody.appendChild(tr); });
}
document.querySelectorAll('table thead th[data-col]').forEach(function(th) {
  th.addEventListener('click', function() {
    var table = th.closest('table');
    var tbody = table.querySelector('tbody');
    var col = Number(th.getAttribute('data-col'));
    var prev = tableSortState[tbody.id];
    var dir = (prev && prev.col === col) ? -prev.dir : 1;
    tableSortState[tbody.id] = { col: col, dir: dir };
    sortTableBody(tbody, col, dir);
    table.querySelectorAll('th[data-col]').forEach(function(h) { h.removeAttribute('data-sort'); });
    th.setAttribute('data-sort', dir === 1 ? 'asc' : 'desc');
  });
});
function reapplySort(tbodyId) {
  var state = tableSortState[tbodyId];
  if (!state) return;
  sortTableBody(document.getElementById(tbodyId), state.col, state.dir);
}

// Single floating tooltip shared by every [data-tip] element on the page --
// column headers (th.th-tip) and, per-row, status pills. Appended to <body>
// so it isn't clipped by .tablewrap's overflow-x:auto, and driven by event
// delegation so it keeps working regardless of table rebuilds.
(function initHeaderTooltip() {
  var tip = document.createElement('div');
  tip.id = 'thTip';
  document.body.appendChild(tip);
  var activeTh = null;

  function showTip(th) {
    tip.textContent = th.getAttribute('data-tip');
    tip.style.display = 'block';
    var r = th.getBoundingClientRect();
    var tw = tip.offsetWidth, tht = tip.offsetHeight, pad = 8;
    var left = Math.max(pad, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - pad));
    var top = r.bottom + pad;
    if (top + tht > window.innerHeight - pad) top = r.top - tht - pad;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
    activeTh = th;
  }
  function hideTip() {
    tip.style.display = 'none';
    activeTh = null;
  }

  // Desktop: real hover shows/hides it as the mouse moves.
  document.addEventListener('mouseover', function(e) {
    var th = e.target.closest && e.target.closest('[data-tip]');
    if (!th) return;
    showTip(th);
  });
  document.addEventListener('mouseout', function(e) {
    var th = e.target.closest && e.target.closest('[data-tip]');
    if (!th) return;
    var to = e.relatedTarget;
    if (to && to.closest && to.closest('[data-tip]') === th) return;
    hideTip();
  });

  // Touch devices never fire a real "leave" event, so a tap would open the
  // tip via the synthetic mouseover above but nothing would ever close it.
  // Handle taps explicitly: tapping a tooltip target toggles its tip,
  // tapping anywhere else dismisses whatever's open.
  document.addEventListener('touchstart', function(e) {
    var th = e.target.closest && e.target.closest('[data-tip]');
    if (th) {
      if (activeTh === th) hideTip(); else showTip(th);
      return;
    }
    if (activeTh) hideTip();
  }, { passive: true });
})();

const fmt = new Intl.NumberFormat();
const byId = id => document.getElementById(id);
const setText = (id, value) => { byId(id).textContent = value; };

const animatingElements = new WeakMap();
function animateNumber(id, targetValue) {
  const el = byId(id);
  // Hidden tab: requestAnimationFrame never fires, so a tween would leave the
  // value frozen at whatever it showed when the tab was backgrounded. Set the
  // exact value directly instead -- nobody sees the animation anyway.
  if (document.hidden) {
    if (animatingElements.get(el)) {
      cancelAnimationFrame(animatingElements.get(el));
      animatingElements.delete(el);
    }
    el.textContent = fmt.format(targetValue);
    return;
  }
  const previous = parseFloat((el.textContent || "0").replace(/,/g, "")) || 0;
  if (previous === targetValue) { el.textContent = fmt.format(targetValue); return; }
  if (animatingElements.get(el)) cancelAnimationFrame(animatingElements.get(el));
  const duration = 450;
  const start = performance.now();
  function tick(now) {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = previous + (targetValue - previous) * eased;
    el.textContent = fmt.format(Math.round(current));
    if (progress < 1) {
      animatingElements.set(el, requestAnimationFrame(tick));
    } else {
      animatingElements.delete(el);
    }
  }
  animatingElements.set(el, requestAnimationFrame(tick));
}

let selectedRange = "all";

async function refresh() {
  try {
    const response = await fetch("/trend-dashboard-data?range=" + encodeURIComponent(selectedRange), { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();

    setText("lastUpdate", data.generated_at);
    byId("liveDot").classList.add("ok");
    setText("liveText", "Live");

    byId("healthBanner").innerHTML = data.health_banner_html || "";
    byId("symbolCards").innerHTML = data.symbol_cards_html || "";

    animateNumber("totalSamples", data.total_samples);
    animateNumber("totalPredictions", data.total_predictions);
    animateNumber("readyCount", data.ready_groups);
    setText("groupsTotal", data.group_count);
    animateNumber("blockedCount", data.blocked_groups);
    animateNumber("labelLong", data.label_long);
    animateNumber("labelShort", data.label_short);
    animateNumber("labelNoTrade", data.label_no_trade);
    setText("lastPrediction", data.last_prediction_time || "--");
    animateNumber("ablationReadyCount", data.ablation_ready_groups);
    setText("ablationGroupsTotal", data.ablation_group_count);
    animateNumber("verificationPassing", data.verification_passing);
    setText("verificationTotal", data.verification_total);

    byId("modelRows").innerHTML = data.model_rows_html;
    byId("labelRows").innerHTML = data.label_rows_html;
    byId("signalRows").innerHTML = data.signal_rows_html;
    byId("predictionRows").innerHTML = data.prediction_rows_html;
    byId("sampleRows").innerHTML = data.sample_rows_html;
    byId("historyRows").innerHTML = data.history_rows_html;
    byId("ablationRows").innerHTML = data.ablation_rows_html;
    byId("ablationOutput").innerHTML = data.ablation_output_html || "";
    byId("verificationRows").innerHTML = data.verification_rows_html;

    ["modelRows", "labelRows", "signalRows", "predictionRows", "sampleRows", "historyRows", "ablationRows"].forEach(reapplySort);
  } catch (error) {
    byId("liveDot").classList.remove("ok");
    setText("liveText", "Offline: " + error.message);
  }
}

// Run buttons are re-rendered every refresh, so delegate off the table body
// (which is stable) rather than binding each button.
document.addEventListener("click", function(ev) {
  var btn = ev.target.closest ? ev.target.closest(".run-btn") : null;
  if (!btn || btn.disabled) return;
  var name = btn.getAttribute("data-check");
  var ablationGroup = btn.getAttribute("data-ablation");
  if (!name && !ablationGroup) return;
  var url = name ? "/run-check" : "/run-ablation";
  var payload = name ? { name: name } : { group: ablationGroup };
  btn.disabled = true;
  btn.textContent = "Starting...";
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(function(r) {
    return r.json().then(function(body) { return { ok: r.ok, body: body }; });
  }).then(function(res) {
    if (!res.ok) {
      btn.textContent = "Run";
      btn.disabled = false;
      alert("Could not start check: " + ((res.body && res.body.detail) || "unknown error"));
    }
    refresh();
  }).catch(function(err) {
    btn.textContent = "Run";
    btn.disabled = false;
    alert("Could not start check: " + err.message);
  });
});

// --- Range picker: refetch immediately on click; the 7s poll keeps using
// the selected range after that. ---
(function() {
  document.querySelectorAll('#rangeSwitch a[data-range]').forEach(function(a) {
    a.addEventListener('click', function(event) {
      event.preventDefault();
      selectedRange = a.dataset.range;
      document.querySelectorAll('#rangeSwitch a[data-range]').forEach(function(b) {
        b.classList.toggle('current', b === a);
      });
      refresh();
    });
  });
})();

// --- Header chrome: cross-dashboard links + overflow menu ---
(function() {
  function portUrl(port, path) {
    var host = location.hostname || 'localhost';
    var proto = location.protocol === 'https:' ? 'https:' : 'http:';
    return proto + '//' + host + ':' + port + (path || '/');
  }
  document.querySelectorAll('a[data-port]').forEach(function(a) {
    a.href = portUrl(a.dataset.port, a.dataset.path);
  });
  var overflowBtn = document.getElementById('overflowBtn');
  var overflowPop = document.getElementById('overflowPop');
  overflowBtn.addEventListener('click', function(event) {
    event.stopPropagation();
    overflowPop.classList.toggle('open');
  });
  document.addEventListener('click', function() { overflowPop.classList.remove('open'); });
})();

// --- Phone table stacker: stamp each td with its column header so the
// m-stack CSS lays rows out as label/value lines. Labels come from each
// table's live thead; the observer restamps after every 7s refresh rewrite.
// Attribute writes don't retrigger a childList observer, so no loop. ---
(function() {
  function stampLabels() {
    document.querySelectorAll('main table').forEach(function(table) {
      var ths = table.querySelectorAll('thead th');
      if (!ths.length) return;
      var labels = Array.prototype.map.call(ths, function(th) { return th.textContent.trim(); });
      table.querySelectorAll('tbody tr').forEach(function(tr) {
        Array.prototype.forEach.call(tr.children, function(td, i) {
          td.setAttribute('data-label', labels[i] || '');
        });
      });
      table.classList.add('m-stack');
    });
  }
  stampLabels();
  var stampTimer = null;
  new MutationObserver(function() {
    clearTimeout(stampTimer);
    stampTimer = setTimeout(stampLabels, 300);
  }).observe(document.querySelector('main'), { childList: true, subtree: true });
})();

refresh();
setInterval(refresh, 7000);
</script>
</body></html>"""


@app.get("/schema")
def schema_summary() -> Dict[str, Any]:
    return {
        "window_shape": [WINDOW_SIZE, len(FEATURE_NAMES)],
        "feature_names": FEATURE_NAMES,
        "classes": CLASSES,
        "min_samples_per_group": MIN_SAMPLES_PER_GROUP,
        "predict_example": {
            "symbol": "NQ",
            "bars_period": "Order Flow Delta",
            "min_confidence": 0.65,
            "window": [[0.0 for _ in FEATURE_NAMES] for _ in range(2)],
            "metadata": {},
        },
    }
