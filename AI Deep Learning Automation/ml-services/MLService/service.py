from __future__ import annotations

import anyio
import asyncio
import json
import re
import hashlib
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import Counter
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ml_model import (
    CLASSES,
    FEATURE_NAMES,
    MIN_SAMPLES_PER_GROUP,
    WINDOW_SIZE,
    MlEngine,
    assess_group_readiness,
    scan_entry_ablation_readiness,
    scan_entry_sample_counts,
    scan_entry_sample_counts_by_source,
)
from train_template import (
    TEMPLATE_COUNT,
    TemplateEngine,
    scan_template_sample_counts,
    scan_template_sample_counts_by_source,
)
import verification

# --- EXIT MODEL ADDITION START ---
# INSERT AFTER: existing ML model imports
import csv
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import torch
from fastapi import HTTPException

# Cap torch intra-op parallelism for THIS process. Uncapped, torch defaults to
# one thread per core (16) and spawns a separate OpenMP team per calling thread
# -- and FastAPI runs every sync endpoint on an anyio worker thread, so the
# teams multiply. Measured 2026-07-20: the 8765 service held 2,245 threads
# against 5 on the 8766 dashboard and 17 on 8767, i.e. ~140 calling threads x 16.
#
# 4 rather than 1-2 on purpose: this process does real training in-process, not
# just inference -- the 14:00 entry+template retrain and the heavy verification
# checks (permutation, null_feature, split_gap, seed_variance) all run here via
# run_in_threadpool. Starving them would trade a thread problem for a latency
# one. Matches EXIT_TRAIN_TORCH_THREADS, which caps the training child processes.
torch.set_num_threads(4)
try:
    # Inter-op pool can only be sized before any parallel work starts; if
    # something already touched torch, keep the default rather than crash.
    torch.set_num_interop_threads(2)
except Exception:
    pass

from exit_model import EXIT_SEQ_MAX_BARS, TradeExitModel, train_exit_model
from feature_utils import (
    data_series_key,
    exit_group_key,
    group_key,
    normalize_symbol,
    validate_log_exit_sample_input,
    validate_predict_exit_input,
)
# Do not read, modify, overwrite, or retrain any per-group entry checkpoint
# (TemaLimit_bb_vwap_tcnn_{GROUP}.pt) from the exit-model training path, and
# vice versa. Entry and exit models are fully independent per (symbol, data
# series) group; do not cross-wire their files.
# --- EXIT MODEL ADDITION END ---



ROOT = Path(__file__).resolve().parent
engine = MlEngine(ROOT)
template_engine = TemplateEngine(ROOT)

# Gate-veto telemetry (windowless /log-sample rows from temalimit's LogLiveNoTrade).
# Kept OUT of training_samples.jsonl since 2026-07-17: a veto carries no feature
# window and no outcome, so it's operational telemetry, not a labeled observation --
# 124,846 of them (92.5% of the file) had been training as all-zero windows and
# inflating entry-model val_acc to ~1.0. See ML_SYSTEM_GUIDE.txt changelog.
VETOES_PATH = ROOT / "data" / "vetoes.jsonl"

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
app = FastAPI(title="NinjaTrader BB/VWAP ML Service", version="0.1.0")
AUTO_RETRAIN_HOUR = 14
AUTO_RETRAIN_MINUTE = 0
AUTO_RETRAIN_CHECK_SECONDS = 30
auto_retrain_last_date: Optional[str] = None
auto_retrain_last_result: Optional[Dict[str, Any]] = None

# Verification Suite auto-sweep: previously Run-button-only (2026-07-18 launch),
# which meant nobody would notice a real regression unless they happened to
# click through the whole table. Runs once daily right after the retrain
# above finishes -- that's the only point in the day the training data
# actually changes, so it's the only point re-running these checks can turn
# up something new. Reuses auto_retrain_last_date as the "already did today"
# gate rather than a second date variable, since it always fires in the same
# pass as the retrain.
auto_verification_last_sweep: Optional[Dict[str, Any]] = None

# --- EXIT MODEL ADDITION START ---
# INSERT AFTER: existing auto-retrain state variables
EnableMlExitModel = False
MlExitHoldThreshold = 0.45
ExitModelWarmupMin = 500

# Retrain cadence. A flat every-200-samples trigger ignored that training cost
# scales with the group's whole history, not the 200 new rows: ES_3LINEBREAK
# crossed a boundary every few minutes while a single retrain took over an hour,
# so it re-triggered the moment it finished and sat pinned at 100% CPU.
#
# NOTE this knob alone cannot fix saturation. With interval = N/D the duty cycle
# is k*D*R (k = min/sample to train, R = samples/min arriving) -- N cancels out,
# so a bigger group buys nothing and D only picks a fixed oversubscription
# ratio. Measured on ES_3LINEBREAK at 351 samples/min: D=20 gives a 12.7 min gap
# against a 134 min retrain (10.6x oversubscribed), and even D=2 is still 1.1x.
# It is kept as cheap defense-in-depth; the real guard is the label floor below.
ExitModelRetrainMinInterval = 200
ExitModelRetrainSizeDivisor = 20

# Minimum minority-class evidence before a group may train at all. The old gate
# only required >0 of each label, which a single row satisfies -- so groups that
# are ~100% one class trained for hours and produced pure coin flips:
# ES_3LINEBREAK had 2 exit rows in 90,629 and scored val_auc exactly 0.5000.
#
# Values corrected 2026-07-20 after actually measuring this. A controlled test
# (17 features matching this model, logistic regression, 12-40 seeds) found:
#   - Imbalance RATIO does not drive learnability. Holding positives fixed and
#     sweeping 17:1 -> 2369:1 moved AUC not at all (0.963-0.981 in every cell,
#     with and without class weighting). So a percentage floor is the wrong
#     shape: at 1% it would block a group holding 200 usable positives in
#     100k rows (0.2%). Kept only as a floor against pathological trickles.
#   - Absolute POSITIVE COUNT is what governs it. At a realistic weak signal:
#     2 pos -> AUC 0.442 (stdev 0.272!), 20 -> 0.517, 50 -> 0.534 (still a coin
#     flip), 100 -> 0.561. So 50 was too low; 100 is the defensible number. The
#     huge stdev at 2-5 positives is why a lucky-high AUC off a handful of rows
#     must never be trusted -- the same failure the load gate's floor guards.
# NOTE one exit row per completed trade means 100 positives = 100 CLOSED trades
# for that group. That, not sampling, is the binding constraint (groups are at
# 1-6 today). No resampling scheme manufactures completed trades.
ExitModelMinMinorityCount = 100
ExitModelMinMinorityRatio = 0.001


def exit_retrain_interval(last_total: int) -> int:
    """Samples that must accumulate since the last retrain before the next one."""
    return max(ExitModelRetrainMinInterval, last_total // ExitModelRetrainSizeDivisor)


def exit_labels_trainable(label_counts: Dict[str, int]) -> Tuple[bool, str]:
    """Whether a group has enough minority-class evidence to be worth training.

    Returns (ok, reason); reason is "" when ok. Fails closed on empty counts.
    """
    hold = label_counts.get("hold", 0)
    exits = label_counts.get("exit", 0)
    total = hold + exits
    minority = min(hold, exits)
    if minority <= 0:
        return False, "need_both_labels"
    if minority < ExitModelMinMinorityCount:
        return False, "minority_below_count_floor"
    if total <= 0 or (minority / total) < ExitModelMinMinorityRatio:
        return False, "minority_below_ratio_floor"
    return True, ""

# Phase 3 (live ML-controlled exits) unlock thresholds. All four must hold
# before phase3_unlocked can be true for a given (symbol, data_series) group:
#   - enough completed trades to have seen real variety, not just many
#     samples from a few long-held positions
#   - trailing-average validation AUC (not a single noisy snapshot) clears
#     a "better than a coin flip by a real margin" bar
#   - test AUC doesn't diverge much from val AUC (guards against an
#     unlucky/lucky validation split)
#   - samples span enough distinct calendar weeks to have plausibly seen
#     more than one regime (trend vs. chop, high vol vs. low vol)
PHASE3_MIN_COMPLETED_TRADES = 150
PHASE3_MIN_VAL_AUC = 0.58
PHASE3_MAX_TEST_VAL_AUC_GAP = 0.03
PHASE3_MIN_WEEKS_REPRESENTED = 4
PHASE3_TRAILING_RETRAINS = 3

# Readiness floor (phase 2, "model recommending"). A model file on disk only
# means training COMPLETED, not that the model learned anything: ES_3LINEBREAK
# trained on 70,218 "hold" labels and exactly 1 "exit", scored val AUC 0.500 --
# an exact coin flip -- and was still written status "ok" (2026-07-19). Since
# exit_model_ready is what promotes a group to phase 2, without this floor those
# coin flips would start emitting exit recommendations the moment
# EnableMlExitModel is switched on. Set below PHASE3_MIN_VAL_AUC because phase 2
# only recommends (NinjaScript decides) while phase 3 closes trades on its own,
# so phase 3 stays the stricter bar on top of this one.
EXIT_MODEL_MIN_VAL_AUC = 0.55

# Companion gate to the AUC floor: how many examples of the RARER label (exit vs
# hold) the group must have before its score is believable at all. The AUC floor
# alone catches the degenerate case only because 1 exit label in 70,218 happens
# to score 0.500 -- but with a handful of minority examples a *spuriously high*
# AUC is equally likely, and 0.8 off 3 examples would sail through a 0.55 floor
# on pure luck.
#
# Set to 100 to match ExitModelMinMinorityCount (the retrain-trigger gate above),
# and for the same measured reason. An initial guess of 25 here was wrong: a
# controlled sweep (17 features matching this model, logistic regression, 12-40
# seeds) found imbalance RATIO barely matters while the ABSOLUTE positive count
# governs everything -- 2 positives -> AUC 0.442 with stdev 0.272, 5 -> 0.467,
# 20 -> 0.517, 50 -> 0.534, 100 -> 0.561. So 25 would have admitted models still
# measuring coin flips, and that stdev at low counts means the AUC floor itself
# is near-meaningless down there. This gate, not the AUC one, is load-bearing.
#
# Note the real binding constraint is completed TRADES: exit rows are one per
# closed trade, so 100 minority labels means 100 closed trades in the group
# (currently 1-6). Nothing resamples that into existence.
EXIT_MODEL_MIN_MINORITY_LABELS = 100

DATA_PATH = str(ROOT)
EXIT_TSV_HEADERS = [
    "trade_id",
    "timestamp",
    "symbol",
    "direction",
    "bars_held",
    "unrealized_r",
    "bar_duration_sec",
    "data_series_type",
    "data_series_value",
    "f0",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "label",
    "regime",
    "sample_date",
    "entry_price",
    "one_r_points",
    "template_number",
]

# All of these are keyed by "group" = "{SYMBOL}_{SERIESKEY}" (e.g. "NQ_500TICK"),
# the same key format used on the entry-model side, so exit models are now
# scoped per (symbol, data_series) rather than per symbol alone.
exit_models: Dict[str, TradeExitModel] = {}
exit_model_ready: Dict[str, bool] = {}
exit_model_training: Dict[str, bool] = {}
exit_health: Dict[str, Dict[str, Any]] = {}
exit_sample_counts: Dict[str, int] = {}
exit_label_counts: Dict[str, Dict[str, int]] = {}
exit_trade_counts: Dict[str, int] = {}
exit_weeks_represented: Dict[str, int] = {}
exit_trade_ids_seen: Dict[str, set] = {}
exit_weeks_seen: Dict[str, set] = {}
# Sample count at each group's last retrain trigger, so the next one fires on a
# delta rather than an absolute modulus (see exit_retrain_interval).
exit_last_retrain_total: Dict[str, int] = {}
exit_locks: Dict[str, threading.Lock] = {}
exit_tsv_locks: Dict[str, threading.Lock] = {}
exit_recommendations_today: Dict[str, int] = {}
actual_ml_exits_today: Dict[str, int] = {}
_health_lock = threading.Lock()

# Exit-model training is CPU-bound PyTorch work; it runs in a separate OS
# process so it can't starve the GIL that request-handling threads (e.g.
# /log-exit-sample, hit on every bar of every open position) depend on.
_exit_train_executor = ProcessPoolExecutor(max_workers=2)

# Periodic health-cache refresh (below) re-parses the entry/template sample
# files from scratch every 10s; those files are 10s-100s of MB and growing,
# so that parsing also runs in a separate process rather than an in-process
# thread for the same GIL-contention reason.
_scan_executor = ProcessPoolExecutor(max_workers=2)

exit_logger = logging.getLogger("tema_exit_model")
exit_logger.setLevel(logging.INFO)
if not exit_logger.handlers:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    exit_logger.addHandler(stream_handler)
    file_handler = logging.FileHandler(ROOT / "ml_service.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    exit_logger.addHandler(file_handler)
# --- EXIT MODEL ADDITION END ---



class PredictRequest(BaseModel):
    symbol: str = ""
    trigger: str = Field(default="", description="upper_bb, lower_bb, mid_bb, vwap, etc.")
    timestamp: Optional[str] = None
    min_confidence: float = 0.60
    window: List[List[float]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TrainingSampleRequest(BaseModel):
    symbol: str = ""
    trigger: str = ""
    timestamp: Optional[str] = None
    label: str = Field(description="long, short, or no_trade")
    window: List[List[float]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TemplateSampleRequest(BaseModel):
    symbol: str = ""
    trigger: str = ""
    setup_timestamp: Optional[str] = None
    resolved_timestamp: Optional[str] = None
    template_number: int = 0
    selectivity: float = 0.0
    setup_direction: str = ""
    r_multiple: float = 0.0
    dollars: float = 0.0
    shadow: bool = False
    bars_period: str = ""
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    mfe_points: Optional[float] = None
    mae_points: Optional[float] = None
    bars_held: Optional[int] = None
    win: Optional[bool] = None
    window: List[List[float]] = Field(default_factory=list)


class RetrainRequest(BaseModel):
    symbol: Optional[str] = None
    bars_period: Optional[str] = None
    epochs: int = 8
    batch_size: int = 64
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


def auto_verification_sweep_log_path() -> Path:
    return ROOT / "data" / "auto_verification_sweep.jsonl"


def log_auto_verification_sweep(record: Dict[str, Any]) -> None:
    path = auto_verification_sweep_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine.append_jsonl(path, record)


async def run_daily_verification_sweep(today: str) -> Dict[str, Any]:
    """Run every READY, non-heavy-during-retrain-window check once, sequentially
    (never concurrently -- heavy checks train dry-run models and would fight
    each other and the CPU-bound retrain that just finished). Records one
    summary line per sweep and lets each check's own result land in the usual
    verification_results.jsonl / dashboard row exactly as a manual Run would."""
    started_at = utc_now()
    outcomes: Dict[str, str] = {}
    for name, spec in verification.CHECKS.items():
        ok, msg = await run_in_threadpool(verification.can_start, name, engine)
        if not ok:
            outcomes[name] = f"skipped: {msg}"
            continue
        try:
            record = await run_in_threadpool(verification.run_check, name, engine, ROOT)
            outcomes[name] = record.get("verdict", "unknown")
        except Exception as exc:  # noqa: BLE001 -- one check's crash must not stop the sweep
            outcomes[name] = f"error: {exc}"
    summary = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "local_date": today,
        "outcomes": outcomes,
    }
    log_auto_verification_sweep(summary)
    return summary


async def auto_retrain_loop() -> None:
    global auto_retrain_last_date, auto_retrain_last_result, auto_verification_last_sweep

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
                # run_retrain covers both the entry engine and the template
                # engine, so the template model shares this exact schedule.
                result = await run_in_threadpool(run_retrain, None, None, 8, 64, 0.001)
                auto_retrain_last_result = {
                    "ok": True,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "local_date": today,
                    "result": result,
                }
            except Exception as exc:
                auto_retrain_last_result = {
                    "ok": False,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "local_date": today,
                    "error": str(exc),
                }

            auto_retrain_last_date = today
            log_auto_retrain(auto_retrain_last_result)

            # Sweep the Verification Suite right after -- this is the one
            # point in the day the training data actually changed, so it's
            # the only point re-running these checks can surface something
            # new. Runs regardless of whether the retrain above succeeded
            # (a failed retrain is exactly when you want fresh eyes on
            # duplicate/leakage/drift checks). Best-effort: a sweep failure
            # must not take down the retrain loop.
            try:
                auto_verification_last_sweep = await run_daily_verification_sweep(today)
            except Exception as exc:
                auto_verification_last_sweep = {
                    "local_date": today, "finished_at": utc_now(), "error": str(exc),
                }

        await asyncio.sleep(AUTO_RETRAIN_CHECK_SECONDS)



# --- EXIT MODEL ADDITION START ---
# INSERT BEFORE: first endpoint definition
def exit_tsv_path(group: str) -> Path:
    return ROOT / f"exit_samples_{group}.tsv"


def exit_model_path(group: str) -> Path:
    return ROOT / f"exit_model_{group}.pt"


def exit_metadata_path(group: str) -> Path:
    return ROOT / f"exit_model_{group}.json"


def exit_history_path(group: str) -> Path:
    return ROOT / f"exit_model_{group}_history.jsonl"


def ensure_exit_group(group: str) -> None:
    if group not in exit_locks:
        exit_locks[group] = threading.Lock()
    if group not in exit_tsv_locks:
        exit_tsv_locks[group] = threading.Lock()
    exit_model_ready.setdefault(group, False)
    exit_model_training.setdefault(group, False)
    exit_sample_counts.setdefault(group, 0)
    exit_label_counts.setdefault(group, {"hold": 0, "exit": 0})
    exit_trade_counts.setdefault(group, 0)
    exit_weeks_represented.setdefault(group, 0)
    exit_trade_ids_seen.setdefault(group, set())
    exit_weeks_seen.setdefault(group, set())
    exit_last_retrain_total.setdefault(group, 0)
    exit_recommendations_today.setdefault(group, 0)
    actual_ml_exits_today.setdefault(group, 0)
    exit_health.setdefault(group, {})


def create_exit_tsv_if_missing(group: str) -> None:
    ensure_exit_group(group)
    path = exit_tsv_path(group)
    if not path.exists():
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(EXIT_TSV_HEADERS)
        return

    # Older files predate the template_number column; upgrade the header in
    # place so the new column is labeled. Existing rows are left as-is (one
    # fewer field than the header, which csv.DictReader tolerates).
    expected_header = "\t".join(EXIT_TSV_HEADERS)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            lines = handle.readlines()
        if lines and lines[0].rstrip("\r\n") != expected_header:
            lines[0] = expected_header + "\n"
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.writelines(lines)
    except Exception:
        pass


def count_exit_tsv(group: str) -> None:
    path = exit_tsv_path(group)
    count = 0
    labels = {"hold": 0, "exit": 0}
    trade_ids: set = set()
    weeks: set = set()
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                count += 1
                trade_ids.add(str(row.get("trade_id") or ""))
                label = str(row.get("label") or "")
                if label == "1":
                    labels["hold"] += 1
                elif label == "0":
                    labels["exit"] += 1
                date_text = str(row.get("sample_date") or row.get("timestamp") or "")
                if date_text:
                    try:
                        parsed = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
                        weeks.add(parsed.isocalendar()[:2])
                    except Exception:
                        pass
    exit_sample_counts[group] = count
    exit_label_counts[group] = labels
    exit_trade_ids_seen[group] = trade_ids
    exit_weeks_seen[group] = weeks
    exit_trade_counts[group] = len(trade_ids)
    exit_weeks_represented[group] = len(weeks)
    # Baseline the retrain delta at the restored count. Left at 0, every group
    # would look overdue by its entire history and retrain on the next sample,
    # so a restart would kick off a simultaneous retrain of every large group.
    exit_last_retrain_total[group] = count


def exit_model_gate_metrics(group: str) -> Tuple[Optional[float], Optional[int]]:
    """(val_auc, minority_label_count) for this group, from its metadata file.

    Deliberately not read from exit_health: _handle_exit_train_result calls
    load_exit_model_for_group BEFORE it publishes the fresh result into
    exit_health, so that dict still holds the previous run's numbers (or
    nothing) at gate time. train_exit_model writes the metadata file before
    returning, so disk is the only source that is correct in both the startup
    path and the post-training path.

    Either element is None when it cannot be read; callers treat None as a
    failed check rather than a pass.
    """
    path = exit_metadata_path(group)
    if not path.exists():
        return None, None
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(metadata, dict):
        return None, None

    val_auc: Optional[float] = None
    raw_auc = metadata.get("val_auc")
    if raw_auc is not None:
        try:
            val_auc = float(raw_auc)
        except (TypeError, ValueError):
            val_auc = None

    minority: Optional[int] = None
    label_counts = metadata.get("label_counts")
    if isinstance(label_counts, dict):
        try:
            minority = min(int(label_counts.get("hold", 0)), int(label_counts.get("exit", 0)))
        except (TypeError, ValueError):
            minority = None

    return val_auc, minority


def load_exit_model_for_group(group: str) -> bool:
    ensure_exit_group(group)
    path = exit_model_path(group)
    if exit_trade_counts.get(group, 0) < 1 or not path.exists():
        exit_model_ready[group] = False
        return False

    # A saved model is not automatically a usable one -- see
    # EXIT_MODEL_MIN_VAL_AUC and EXIT_MODEL_MIN_MINORITY_LABELS. Both checks are
    # evaluated rather than short-circuiting on the first, so the log names every
    # reason at once: "score too low" and "score computed off 3 examples" call
    # for very different responses. Unknown score is treated as failing: a model
    # whose quality cannot be verified must not be trusted to recommend exits.
    val_auc, minority_labels = exit_model_gate_metrics(group)
    reasons: List[str] = []
    if val_auc is None:
        reasons.append("val_auc_missing")
    elif val_auc < EXIT_MODEL_MIN_VAL_AUC:
        reasons.append("val_auc_below_floor")
    if minority_labels is None:
        reasons.append("label_counts_missing")
    elif minority_labels < EXIT_MODEL_MIN_MINORITY_LABELS:
        reasons.append("minority_labels_below_floor")

    if reasons:
        exit_model_ready[group] = False
        exit_logger.info(
            "exit model held at phase 1 group=%s reasons=%s val_auc=%s auc_floor=%s "
            "minority_labels=%s minority_floor=%s",
            group,
            ",".join(reasons),
            val_auc,
            EXIT_MODEL_MIN_VAL_AUC,
            minority_labels,
            EXIT_MODEL_MIN_MINORITY_LABELS,
        )
        return False

    model = TradeExitModel()
    model.load_state_dict(torch.load(path, map_location="cpu"))
    model.eval()
    exit_models[group] = model
    exit_model_ready[group] = True
    return True


def load_exit_metadata(group: str) -> None:
    path = exit_metadata_path(group)
    if not path.exists():
        return
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(metadata, dict):
            exit_health[group] = metadata
    except Exception as exc:
        exit_health.setdefault(group, {})["last_error"] = f"metadata_load_failed: {exc}"


def recommended_exit_phase_for_group(group: str) -> int:
    """Single source of truth for phase 1 vs 2, shared by /ml-exit-phase (what
    the strategy asks on startup) and exit_health_row (what the dashboard
    shows), so they can never silently disagree. Phase 1 (collect-only) is
    the floor -- every group starts here. Phase 2 (recommendations) needs
    both warmup (>= ExitModelWarmupMin samples) AND a model that actually
    trained (exit_model_ready), not just sample count -- a group can clear
    warmup for a long time while stuck at phase 1 if training keeps failing
    need_both_labels (see exit_model_never_trained_2026_07_18)."""
    samples = exit_sample_counts.get(group, 0)
    ready = bool(exit_model_ready.get(group, False))
    return 2 if samples >= ExitModelWarmupMin and ready else 1


def phase3_unlocked_for_group(group: str) -> Dict[str, Any]:
    """Recomputes the phase3 unlock decision from first principles each time
    it's asked, rather than trusting a single stored boolean, so every
    condition is independently visible instead of being one opaque flag."""
    ensure_exit_group(group)
    completed_trades = exit_trade_counts.get(group, 0)
    weeks = exit_weeks_represented.get(group, 0)

    history_path = exit_history_path(group)
    trailing_val_aucs: List[float] = []
    latest_test_auc: Optional[float] = None
    if history_path.exists():
        lines = [l for l in history_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        recent = lines[-PHASE3_TRAILING_RETRAINS:]
        for line in recent:
            try:
                record = json.loads(line)
                if record.get("val_auc") is not None:
                    trailing_val_aucs.append(float(record["val_auc"]))
                latest_test_auc = record.get("test_auc")
            except Exception:
                continue

    trailing_val_auc = sum(trailing_val_aucs) / len(trailing_val_aucs) if trailing_val_aucs else None

    conditions = {
        "completed_trades": completed_trades >= PHASE3_MIN_COMPLETED_TRADES,
        "trailing_val_auc": trailing_val_auc is not None and trailing_val_auc >= PHASE3_MIN_VAL_AUC,
        "test_val_gap": (
            latest_test_auc is not None
            and trailing_val_auc is not None
            and latest_test_auc >= trailing_val_auc - PHASE3_MAX_TEST_VAL_AUC_GAP
        ),
        "weeks_represented": weeks >= PHASE3_MIN_WEEKS_REPRESENTED,
    }
    unlocked = all(conditions.values())

    return {
        "unlocked": unlocked,
        "conditions": conditions,
        "completed_trades": completed_trades,
        "weeks_represented": weeks,
        "trailing_val_auc": trailing_val_auc,
        "latest_test_auc": latest_test_auc,
        "thresholds": {
            "min_completed_trades": PHASE3_MIN_COMPLETED_TRADES,
            "min_val_auc": PHASE3_MIN_VAL_AUC,
            "max_test_val_gap": PHASE3_MAX_TEST_VAL_AUC_GAP,
            "min_weeks_represented": PHASE3_MIN_WEEKS_REPRESENTED,
            "trailing_retrains": PHASE3_TRAILING_RETRAINS,
        },
    }


def migrate_exit_tsv_to_per_group() -> None:
    """One-time migration: split old per-symbol exit_samples_*.tsv into per-group files."""
    old_files = list(ROOT.glob("exit_samples_[A-Z]*.tsv"))
    for old_path in old_files:
        symbol = old_path.stem.replace("exit_samples_", "")
        if not symbol or "_" in symbol:  # Skip already-migrated files (they have underscores)
            continue

        # Read old file and group rows by (symbol, data_series_type, data_series_value)
        group_rows = {}
        try:
            with old_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f, delimiter="\t")
                for row in reader:
                    ds_type = str(row.get("data_series_type") or "").strip()
                    ds_value = str(row.get("data_series_value") or "").strip()
                    group = exit_group_key(symbol, ds_type, ds_value)
                    if group not in group_rows:
                        group_rows[group] = []
                    group_rows[group].append(row)
        except Exception as exc:
            exit_logger.error("migrate_exit_tsv: failed to read %s: %s", old_path, exc)
            continue

        # Write new per-group files
        for group, rows in group_rows.items():
            new_path = exit_tsv_path(group)
            if new_path.exists():
                continue  # Don't overwrite existing per-group files
            try:
                with new_path.open("w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=EXIT_TSV_HEADERS, delimiter="\t")
                    writer.writeheader()
                    writer.writerows(rows)
                exit_logger.info("migrate_exit_tsv: wrote %s rows to %s", len(rows), new_path.name)
            except Exception as exc:
                exit_logger.error("migrate_exit_tsv: failed to write %s: %s", new_path, exc)

        # Rename old file as backup
        try:
            backup_path = old_path.with_name(old_path.name + ".bak_migrated")
            old_path.rename(backup_path)
            exit_logger.info("migrate_exit_tsv: backed up %s to %s", old_path.name, backup_path.name)
        except Exception as exc:
            exit_logger.error("migrate_exit_tsv: failed to backup %s: %s", old_path, exc)


def init_exit_models() -> None:
    migrate_exit_tsv_to_per_group()
    groups = set()
    for path in ROOT.glob("exit_samples_*.tsv"):
        name = path.stem.replace("exit_samples_", "")
        if not name.endswith(".bak_migrated"):  # Skip backups
            groups.add(name)
    for path in ROOT.glob("exit_model_*.pt"):
        name = path.stem.replace("exit_model_", "")
        if not name.endswith("_history"):
            groups.add(name)
    for path in ROOT.glob("exit_model_*.json"):
        stem = path.stem.replace("exit_model_", "")
        if not stem.endswith("_history"):
            groups.add(stem)

    for group in sorted(groups):
        ensure_exit_group(group)
        create_exit_tsv_if_missing(group)
        count_exit_tsv(group)
        load_exit_metadata(group)
        try:
            ready = load_exit_model_for_group(group)
        except Exception as exc:
            ready = False
            exit_model_ready[group] = False
            exit_health.setdefault(group, {})["last_error"] = f"model_load_failed: {exc}"
        exit_logger.info(
            "exit model group=%s samples=%s trades=%s ready=%s",
            group,
            exit_sample_counts.get(group, 0),
            exit_trade_counts.get(group, 0),
            ready,
        )


def warmup_remaining(group: str) -> int:
    return max(0, ExitModelWarmupMin - exit_sample_counts.get(group, 0))


def exit_health_row(group: str) -> Dict[str, Any]:
    metadata = dict(exit_health.get(group, {}))
    last_trained = metadata.get("last_trained")
    model_age_days = None
    if last_trained:
        try:
            trained_time = datetime.fromisoformat(str(last_trained).replace("Z", "+00:00"))
            model_age_days = (datetime.now(trained_time.tzinfo or timezone.utc) - trained_time).days
        except Exception:
            model_age_days = None

    phase3 = phase3_unlocked_for_group(group)
    symbol_part, _, series_part = group.partition("_")

    return {
        **metadata,
        "group": group,
        "symbol": symbol_part,
        "data_series_key": series_part,
        "samples": exit_sample_counts.get(group, 0),
        "completed_trades": exit_trade_counts.get(group, 0),
        "weeks_represented": exit_weeks_represented.get(group, 0),
        "label_counts": exit_label_counts.get(group, {"hold": 0, "exit": 0}),
        "model_ready": bool(exit_model_ready.get(group, False)),
        "warmup_remaining": warmup_remaining(group),
        "is_training": bool(exit_model_training.get(group, False)),
        "model_age_days": model_age_days,
        "exit_recommendations_today": exit_recommendations_today.get(group, 0),
        "actual_ml_exits_today": actual_ml_exits_today.get(group, 0),
        "last_error": metadata.get("last_error"),
        "recommended_phase": recommended_exit_phase_for_group(group),
        "phase3_unlocked": phase3["unlocked"],
        "phase3_conditions": phase3["conditions"],
        "phase3_trailing_val_auc": phase3["trailing_val_auc"],
    }


def _handle_exit_train_result(group: str, future: "Any") -> None:
    try:
        result = future.result()
        if result.get("status") == "ok":
            with exit_locks[group]:
                load_exit_model_for_group(group)
                count_exit_tsv(group)
                with _health_lock:
                    exit_health[group] = dict(result)
                    exit_health[group]["last_error"] = None
            exit_logger.info(
                "exit model trained group=%s samples=%s val_auc=%s trades=%s",
                group, result.get("samples"), result.get("val_auc"), result.get("completed_trades"),
            )
        else:
            with _health_lock:
                exit_health.setdefault(group, {})["last_error"] = result.get("error", "unknown_error")
                if "detail" in result:
                    exit_health[group]["last_error_detail"] = result["detail"]
            exit_logger.warning("exit model train skipped/failed group=%s result=%s", group, result)
    except Exception as exc:
        with _health_lock:
            exit_health.setdefault(group, {})["last_error"] = str(exc)
        exit_logger.exception("exit model background train failed group=%s", group)
    finally:
        exit_model_training[group] = False


def background_train_exit_model(group: str) -> None:
    ensure_exit_group(group)
    future = _exit_train_executor.submit(train_exit_model, group, DATA_PATH)
    future.add_done_callback(lambda f, g=group: _handle_exit_train_result(g, f))


def render_exit_model_section() -> str:
    return """
<section id="exit-model-section">
  <h2>Exit Model (per Symbol + Data Series)</h2>
  <div id="exitModelContent" class="muted">Loading exit model health...</div>
</section>
<script>
async function refreshExitModelSection() {
  const root = document.getElementById("exitModelContent");
  if (!root) return;
  try {
    const response = await fetch("/model-health", { cache: "no-store" });
    const data = await response.json();
    const enabled = !!data.exit_model_enabled;
    const groups = data.exit_groups || {};
    const banner = enabled
      ? "<div style='background:#3b82f6;color:#fff;padding:10px 16px;border-radius:6px;font-weight:bold;margin-bottom:16px;'>Exit Model: RECOMMENDATION MODE - Model recommending, NinjaScript decides</div>"
      : "<div style='background:#f59e0b;color:#1a1a1a;padding:10px 16px;border-radius:6px;font-weight:bold;margin-bottom:16px;'>Exit Model: COLLECT MODE - Logging samples only, not controlling exits</div>";
    const rows = Object.keys(groups).sort().map(group => {
      const row = groups[group] || {};
      const labels = row.label_counts || {};
      const samples = row.samples || 0;
      const progress = Math.max(0, Math.min(100, samples / 500 * 100));
      const hold = labels.hold || 0;
      const exit = labels.exit || 0;
      const totalLabels = Math.max(1, hold + exit);
      const holdPct = (hold / totalLabels * 100).toFixed(1);
      const exitPct = (exit / totalLabels * 100).toFixed(1);
      const error = row.last_error ? `<div class='loss'>${row.last_error}</div>` : "";
      const recommendedPhase = row.recommended_phase || 1;
      const phase1 = "<span class='pill long'>ACTIVE</span>";
      const phase2 = recommendedPhase >= 2 ? "<span class='pill long'>UNLOCKED</span>" : "<span class='pill no_trade'>locked</span>";
      const phase3 = row.phase3_unlocked ? "<span class='pill long'>UNLOCKED</span>" : "<span class='pill no_trade'>locked</span>";
      const trailingAuc = row.phase3_trailing_val_auc === undefined || row.phase3_trailing_val_auc === null ? "" : Number(row.phase3_trailing_val_auc).toFixed(3);
      return `<tr>
        <td>${row.symbol || group}</td>
        <td>${row.data_series_key || ""}</td>
        <td class='num'>${samples}<div class='bar'><span style='width:${progress.toFixed(1)}%'></span></div>${(row.live_samples!==undefined||row.shadow_samples!==undefined)?`<div class='subcount'>${(row.live_samples||0).toLocaleString()} live · ${(row.shadow_samples||0).toLocaleString()} shadow</div>`:''}</td>
        <td class='num'>${row.completed_trades || 0}</td>
        <td>${hold} hold (${holdPct}%) / ${exit} exit (${exitPct}%)</td>
        <td>${row.model_ready ? "Yes" : "No"}${row.model_ready ? "" : `, ${row.warmup_remaining || 0} warmup left`}</td>
        <td>${row.is_training ? "Yes" : "No"}</td>
        <td>${row.last_trained || ""}</td>
        <td class='num'>${row.val_auc === undefined || row.val_auc === null ? "" : Number(row.val_auc).toFixed(3)}</td>
        <td class='num'>${trailingAuc}</td>
        <td>${phase1}</td>
        <td>${phase2}</td>
        <td>${phase3}</td>
        <td class='num'>${row.exit_recommendations_today || 0}</td>
        <td class='num'>${row.actual_ml_exits_today || 0}${error}</td>
      </tr>`;
    }).join("");
    if (setSectionHTML(root, `${banner}<div class="tablewrap"><table><thead><tr><th data-col="0">Symbol</th><th data-col="1">Data Series</th><th data-col="2">Samples</th><th data-col="3">Trades</th><th data-col="4">Labels</th><th data-col="5">Ready</th><th data-col="6">Training</th><th data-col="7">Last Trained</th><th data-col="8">Val AUC</th><th data-col="9">Trailing Val AUC</th><th data-col="10">Phase 1</th><th data-col="11">Phase 2</th><th data-col="12">Phase 3</th><th data-col="13">Recs Today</th><th data-col="14">Actual ML Exits</th></tr></thead><tbody id="exitModelRows">${rows || "<tr><td colspan='15' class='muted'>No exit model data yet</td></tr>"}</tbody></table></div>`)) reapplySort("exitModelRows");
  } catch (error) {
    setSectionHTML(root, `<span class='loss'>Exit model health failed: ${error}</span>`);
  }
}
refreshExitModelSection();
setInterval(refreshExitModelSection, 15000);
</script>
"""


def render_entry_model_section() -> str:
    return """
<section id="entry-model-section">
  <h2>Entry Model (per Symbol + Data Series)</h2>
  <p class="sec-desc">Predicts trade direction (<strong>long / short / no_trade</strong>) for each symbol + data series combo. A group sits at <strong>WARMING UP</strong> until it collects <strong>150 labeled samples</strong> -- below that, there simply isn't enough data for the model to learn anything real. Once trained, it's only trusted live once validation accuracy is <strong>65%+</strong> and stays within <strong>10 points</strong> of test accuracy; a bigger gap means it memorized the training data instead of learning a real pattern (<strong>OVERFITTING</strong>). Hover any column header for details.</p>
  <div id="entryModelContent" class="muted">Loading entry model health...</div>
</section>
<script>
// Status string comes from the server (ml_model.py's classify_entry_model_status),
// the SAME function that gates whether temalimit.cs actually uses ML for a
// given (symbol, data series) group -- this badge can never show "good to use"
// while the strategy is treating that group differently, or vice versa.
var ENTRY_STATUS_DISPLAY = {
  warming_up:  { color: '#999',    text: '⚪ WARMING UP',  class: 'no_trade', tip: 'Fewer than 150 labeled samples for this group -- not enough data to train yet.' },
  do_not_use:  { color: '#ff6b6b', text: '🔴 DO NOT USE',  class: 'loss',     tip: 'Validation accuracy is below 50% -- worse than random guessing among 3 classes, not fit for live use.' },
  overfitting: { color: '#ff6b6b', text: '🔴 OVERFITTING', class: 'loss',     tip: 'Validation and test accuracy differ by more than 10 points -- the model learned the training data, not a generalizable pattern.' },
  caution:     { color: '#f59e0b', text: '🟡 CAUTION',     class: 'no_trade', tip: 'Validation accuracy is between 50% and 65% -- better than chance but not reliable enough to trust alone.' },
  good_to_use: { color: '#10b981', text: '🟢 GOOD TO USE', class: 'long',     tip: 'Validation accuracy is 65%+ and test accuracy tracks it within 10 points -- passes the quality gate for live use.' },
};

function getEntryModelStatus(row) {
  return ENTRY_STATUS_DISPLAY[row.status] || ENTRY_STATUS_DISPLAY.warming_up;
}

// Data-quality tripwires from /model-health (ml_model.group_health "warnings").
// Shared by the Entry and Template tables. A row with warnings gets a red ⚠
// badge whose tooltip lists them; a banner above the table names the flagged
// groups so a problem is visible without opening /model-health by hand.
function modelWarningMarker(row) {
  var warnings = (row && row.warnings) || [];
  if (!warnings.length) return "";
  var tip = warnings.join("  |  ");
  return ' <span class="pill loss" data-tip="' + escAttr(tip) + '" '
    + 'style="background-color:#ff6b6b;color:#fff;cursor:help;">⚠</span>';
}
function modelWarningBanner(groups) {
  var flagged = Object.keys(groups || {})
    .filter(function(g) { return ((groups[g] || {}).warnings || []).length; })
    .sort();
  if (!flagged.length) return "";
  return '<div style="background:#ff6b6b;color:#fff;padding:10px 16px;border-radius:6px;'
    + 'font-weight:bold;margin-bottom:12px;">⚠ Data-quality tripwire on '
    + flagged.length + ' group(s): ' + escAttr(flagged.join(", "))
    + ' -- hover the ⚠ in the Status column for details.</div>';
}

// Shared by the Entry and Template model tables below -- both use the exact
// same 10 columns, so the hover-tooltip copy only needs to live in one place.
var MODEL_HEALTH_COLS = [
  ["Symbol", "Instrument this model trains on, e.g. ES, NQ, RTY."],
  ["Data Series", "Bar type + period this model trains on, e.g. 500 TICK, 1 MINUTE. Each symbol + data series combo gets its own independent model."],
  ["Samples", "Labeled training examples collected so far for this group. Training isn't even attempted until this hits 150."],
  ["Ready", "Whether this group has passed the 150-sample minimum and has a trained model saved to disk."],
  ["Last Trained", "Timestamp of the most recent training run for this group."],
  ["Version", "How many times this group's model has been retrained from scratch."],
  ["Val Acc", "Accuracy on the held-out validation split. This is the number the quality gate checks: needs 65%+ for GOOD TO USE."],
  ["Test Acc", "Accuracy on a second held-out split, never seen during training. Compared against Val Acc to catch overfitting -- a gap over 10 points flags OVERFITTING."],
  ["Train/Val/Test Split", "How the group's samples were divided for training vs. validating vs. testing (roughly 70/15/15)."],
  ["Status", "Overall quality gate for this group. Hover the status badge itself for what that specific result means."],
];
function escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function modelHealthHeaderHtml() {
  return MODEL_HEALTH_COLS.map(function(c, i) {
    return '<th data-col="' + i + '" class="th-tip" data-tip="' + escAttr(c[1]) + '"><span class="th-tip-label">' + c[0] + '</span></th>';
  }).join("");
}

// Single floating tooltip shared by every [data-tip] element on the page --
// column headers (th.th-tip) and, per-row, status pills. Appended to <body>
// so it isn't clipped by the tables' overflow containers, and driven by
// event delegation so it keeps working after the 15s poll rebuilds rows and
// headers. Defined once here (entry section renders before the template one).
(function initHeaderTooltip() {
  if (window.__thTipInit) return;
  window.__thTipInit = true;
  function start() {
    var tip = document.createElement("div");
    tip.id = "thTip";
    document.body.appendChild(tip);
    var activeTh = null;

    function showTip(th) {
      tip.textContent = th.getAttribute("data-tip");
      tip.style.display = "block";
      var r = th.getBoundingClientRect();
      var tw = tip.offsetWidth, tht = tip.offsetHeight, pad = 8;
      var left = Math.max(pad, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - pad));
      var top = r.bottom + pad;
      if (top + tht > window.innerHeight - pad) top = r.top - tht - pad;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
      activeTh = th;
    }
    function hideTip() {
      tip.style.display = "none";
      activeTh = null;
    }

    // Desktop: real hover shows/hides it as the mouse moves.
    document.addEventListener("mouseover", function(e) {
      var th = e.target.closest && e.target.closest("[data-tip]");
      if (!th) return;
      showTip(th);
    });
    document.addEventListener("mouseout", function(e) {
      var th = e.target.closest && e.target.closest("[data-tip]");
      if (!th) return;
      var to = e.relatedTarget;
      if (to && to.closest && to.closest("[data-tip]") === th) return;
      hideTip();
    });

    // Touch devices don't fire a real "leave" event, so a tap opens the tip
    // via the synthetic mouseover above but nothing ever closes it -- that's
    // why it got stuck open on mobile. Handle taps explicitly: tapping a
    // tooltip target toggles its tip, tapping anywhere else dismisses
    // whatever's open.
    document.addEventListener("touchstart", function(e) {
      var th = e.target.closest && e.target.closest("[data-tip]");
      if (th) {
        if (activeTh === th) hideTip(); else showTip(th);
        return;
      }
      if (activeTh) hideTip();
    }, { passive: true });
  }
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();

async function refreshEntryModelSection() {
  const root = document.getElementById("entryModelContent");
  if (!root) return;
  try {
    const response = await fetch("/model-health", { cache: "no-store" });
    const data = await response.json();
    const groups = data.entry_groups || {};
    const rows = Object.keys(groups).sort().map(group => {
      const row = groups[group] || {};
      const samples = row.samples || 0;
      const progress = Math.max(0, Math.min(100, samples / 150 * 100));
      const valAcc = row.val_acc === undefined || row.val_acc === null ? "" : (Number(row.val_acc) * 100).toFixed(1) + "%";
      const testAcc = row.test_acc === undefined || row.test_acc === null ? "" : (Number(row.test_acc) * 100).toFixed(1) + "%";
      const status = getEntryModelStatus(row);
      return `<tr>
        <td>${row.symbol || group}</td>
        <td>${row.data_series_key || ""}</td>
        <td class='num'>${samples}<div class='bar'><span style='width:${progress.toFixed(1)}%'></span></div>${(row.live_samples!==undefined||row.shadow_samples!==undefined)?`<div class='subcount'>${(row.live_samples||0).toLocaleString()} live · ${(row.shadow_samples||0).toLocaleString()} shadow</div>`:''}</td>
        <td>${row.model_ready ? "Yes" : "No"}${row.model_ready ? "" : `, ${row.warmup_remaining || 0} warmup left`}</td>
        <td>${row.last_trained || ""}</td>
        <td class='num'>${row.model_version || 0}</td>
        <td class='num'>${valAcc}</td>
        <td class='num'>${testAcc}</td>
        <td class='num'>${row.train_samples || ""} / ${row.val_samples || ""} / ${row.test_samples || ""}</td>
        <td><span class='pill ${status.class}' data-tip="${escAttr(status.tip || "")}" style='background-color:${status.color};color:#fff;'>${status.text}</span>${modelWarningMarker(row)}</td>
      </tr>`;
    }).join("");
    if (setSectionHTML(root, `${modelWarningBanner(groups)}<div class="tablewrap"><table><thead><tr>${modelHealthHeaderHtml()}</tr></thead><tbody id="entryModelRows">${rows || "<tr><td colspan='10' class='muted'>No entry model data yet</td></tr>"}</tbody></table></div>`)) reapplySort("entryModelRows");
  } catch (error) {
    setSectionHTML(root, `<span class='loss'>Entry model health failed: ${error}</span>`);
  }
}
refreshEntryModelSection();
setInterval(refreshEntryModelSection, 15000);
</script>
"""


def render_template_model_section() -> str:
    # Same columns and status framework as the Entry model section; rows come
    # from /model-health's template_groups. getEntryModelStatus is defined by
    # the entry section's script, which renders earlier on the page.
    return """
<section id="template-model-section">
  <h2>Template Model (per Symbol + Data Series)</h2>
  <p class="sec-desc">Picks which of the 40 shadow-traded templates to use for each symbol + data series combo. Same gate as the Entry Model above -- <strong>150 samples</strong> minimum before training starts, <strong>65%+</strong> validation accuracy and no more than a <strong>10-point</strong> val/test gap before it's trusted live -- it just predicts a template ID instead of a trade direction. Hover any column header for details.</p>
  <div id="templateModelContent" class="muted">Loading template model health...</div>
</section>
<script>
async function refreshTemplateModelSection() {
  const root = document.getElementById("templateModelContent");
  if (!root) return;
  try {
    const response = await fetch("/model-health", { cache: "no-store" });
    const data = await response.json();
    const groups = data.template_groups || {};
    const rows = Object.keys(groups).sort().map(group => {
      const row = groups[group] || {};
      const samples = row.samples || 0;
      const progress = Math.max(0, Math.min(100, samples / 150 * 100));
      const valAcc = row.val_acc === undefined || row.val_acc === null ? "" : (Number(row.val_acc) * 100).toFixed(1) + "%";
      const testAcc = row.test_acc === undefined || row.test_acc === null ? "" : (Number(row.test_acc) * 100).toFixed(1) + "%";
      const status = getEntryModelStatus(row);
      return `<tr>
        <td>${row.symbol || group}</td>
        <td>${row.data_series_key || ""}</td>
        <td class='num'>${samples}<div class='bar'><span style='width:${progress.toFixed(1)}%'></span></div>${(row.live_samples!==undefined||row.shadow_samples!==undefined)?`<div class='subcount'>${(row.live_samples||0).toLocaleString()} live · ${(row.shadow_samples||0).toLocaleString()} shadow</div>`:''}</td>
        <td>${row.model_ready ? "Yes" : "No"}${row.model_ready ? "" : `, ${row.warmup_remaining || 0} warmup left`}</td>
        <td>${row.last_trained || ""}</td>
        <td class='num'>${row.model_version || 0}</td>
        <td class='num'>${valAcc}</td>
        <td class='num'>${testAcc}</td>
        <td class='num'>${row.train_samples || ""} / ${row.val_samples || ""} / ${row.test_samples || ""}</td>
        <td><span class='pill ${status.class}' data-tip="${escAttr(status.tip || "")}" style='background-color:${status.color};color:#fff;'>${status.text}</span>${modelWarningMarker(row)}</td>
      </tr>`;
    }).join("");
    if (setSectionHTML(root, `${modelWarningBanner(groups)}<div class="tablewrap"><table><thead><tr>${modelHealthHeaderHtml()}</tr></thead><tbody id="templateModelRows">${rows || "<tr><td colspan='10' class='muted'>No template model data yet</td></tr>"}</tbody></table></div>`)) reapplySort("templateModelRows");
  } catch (error) {
    setSectionHTML(root, `<span class='loss'>Template model health failed: ${error}</span>`);
  }
}
refreshTemplateModelSection();
setInterval(refreshTemplateModelSection, 15000);
</script>
"""


def render_active_templates_section() -> str:
    # Live rotation snapshot straight from temalimit.cs's own state files (see
    # scan_active_templates) -- distinct from the training-health table above,
    # which is per symbol+series model quality, not per running strategy instance.
    return """
<section id="active-templates-section">
  <h2>Active Templates (Live) <span id="activeTemplatesSummary" class="muted"></span></h2>
  <p class="sec-desc">Which template each strategy instance (account + instrument) is currently trading, read directly from its on-disk rotation state file. Only the newest state file per instance is shown -- older files left behind by a previous data-series config are hidden. Also limited to instances rotated today (trading day starts 15:00 California time); instances that haven't rotated since a prior trading day drop off the list. Fixed-template instances (mode 0) never rotate, so they're exempt from the today rule and always shown once their strategy has started up on the new build. Updates whenever a rotation event (no-fill timeout, trade close, or ML override) fires -- not continuously every bar.</p>
  <div id="activeTemplatesContent" class="muted">Loading active templates...</div>
</section>
<script>
async function refreshActiveTemplatesSection() {
  const root = document.getElementById("activeTemplatesContent");
  const summary = document.getElementById("activeTemplatesSummary");
  if (!root) return;
  try {
    const response = await fetch("/model-health", { cache: "no-store" });
    const data = await response.json();
    const rows = data.active_templates || {};
    const keys = Object.keys(rows).sort();
    if (summary) summary.textContent = `(${keys.length} instance${keys.length === 1 ? "" : "s"})`;
    const html = keys.map(key => {
      const row = rows[key] || {};
      const updated = row.updated_at ? new Date(row.updated_at).toLocaleString() : "";
      return `<tr>
        <td>${row.account || ""}</td>
        <td>${row.instrument || ""}</td>
        <td>${row.series || ""}</td>
        <td class='num'><strong>${row.active_template ?? ""}</strong></td>
        <td>${updated}</td>
      </tr>`;
    }).join("");
    if (setSectionHTML(root, `<div class="tablewrap"><table><thead><tr><th data-col="0">Account</th><th data-col="1">Instrument</th><th data-col="2">Data Series</th><th data-col="3">Active Template</th><th data-col="4">Last Rotation</th></tr></thead><tbody id="activeTemplatesRows">${html || "<tr><td colspan='5' class='muted'>No running instances found</td></tr>"}</tbody></table></div>`)) reapplySort("activeTemplatesRows");
  } catch (error) {
    setSectionHTML(root, `<span class='loss'>Active templates failed: ${error}</span>`);
  }
}
refreshActiveTemplatesSection();
setInterval(refreshActiveTemplatesSection, 15000);
</script>
"""


def render_ablation_readiness_section() -> str:
    return """
<section id="ablation-readiness-section">
  <h2>Shadow Weight Ablation Readiness <span id="ablationReadySummary" class="muted"></span></h2>
  <p class="muted">Progress toward having enough real + shadow data to run <code>tools/ablate_shadow_weight.py</code> meaningfully for each group. Click <b>Run</b> on a READY group to launch it in the background; <b>view</b> shows the last run's result table.</p>
  <style>
    .ablation-run-btn{background:transparent;border:1px solid #444;color:inherit;font:inherit;font-size:11.5px;
      font-weight:600;padding:4px 13px;border-radius:8px;cursor:pointer;}
    .ablation-run-btn:hover:not(:disabled){border-color:#7c5cff;background:rgba(124,92,255,0.12);}
    .ablation-run-btn:disabled{opacity:.35;cursor:not-allowed;}
    .ablation-view-btn{font-size:11px;margin-left:4px;}
  </style>
  <div id="ablationReadinessContent" class="muted">Loading ablation readiness...</div>
</section>
<script>
async function refreshAblationReadinessSection() {
  const root = document.getElementById("ablationReadinessContent");
  const summary = document.getElementById("ablationReadySummary");
  if (!root) return;
  try {
    const response = await fetch("/model-health", { cache: "no-store" });
    const data = await response.json();
    const groups = data.entry_ablation_readiness || {};
    const jobs = data.ablation_jobs || {};
    const lastRuns = data.ablation_last_runs || {};
    const keys = Object.keys(groups).sort();
    const readyCount = keys.filter(k => groups[k].ready).length;
    if (summary) summary.textContent = `(${readyCount} / ${keys.length} ready)`;
    const rows = keys.map(group => {
      const row = groups[group] || {};
      const liveCount = row.live_count || 0;
      const livePct = Math.max(0, Math.min(100, liveCount / 200 * 100));
      const shadowCount = row.shadow_count || 0;
      const shadowPct = Math.max(0, Math.min(100, shadowCount / 200 * 100));
      const shadowDirectional = row.shadow_directional || 0;
      const shadowPillClass = row.shadow_ready ? "long" : "no_trade";
      const statusPill = row.ready
        ? "<span class='pill long'>READY</span>"
        : "<span class='pill no_trade'>NOT READY</span>";
      const job = jobs[group] || {};
      let action;
      if (job.state === "running") {
        action = "<span class='pill'>RUNNING</span>";
      } else if (row.ready) {
        action = `<button class='ablation-run-btn' data-group='${group}'>Run</button>`;
      } else {
        action = "<button class='ablation-run-btn' disabled title='Not enough data yet'>Run</button>";
      }
      const last = lastRuns[group];
      if (last) {
        const cls = last.ok ? "long" : "short";
        const txt = last.ok ? "OK" : "FAILED";
        let when = "";
        try { when = new Date(last.ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); } catch (e) {}
        action += ` <span class='pill pill-xs ${cls}'>${txt}</span> <a href='#' class='ablation-view-btn muted' data-group='${group}'>${when} view</a>`;
      }
      return `<tr>
        <td>${group}</td>
        <td class='num'>${liveCount}/200<div class='bar'><span style='width:${livePct.toFixed(1)}%'></span></div></td>
        <td class='num'>${shadowCount}/200<div class='bar'><span style='width:${shadowPct.toFixed(1)}%'></span></div>
          <span class='pill pill-xs ${shadowPillClass}'>${shadowDirectional}/30 L/S</span></td>
        <td>${statusPill}</td>
        <td>${action}</td>
      </tr>`;
    }).join("");
    if (setSectionHTML(root, `<div class="tablewrap"><table><thead><tr><th data-col="0">Group</th><th data-col="1">Live</th><th data-col="2">Shadow</th><th data-col="3">Status</th><th data-col="4">Action</th></tr></thead><tbody id="ablationReadinessRows">${rows || "<tr><td colspan='5' class='muted'>No groups yet</td></tr>"}</tbody></table></div><div id="ablationRunOutput"></div>`)) reapplySort("ablationReadinessRows");
  } catch (error) {
    setSectionHTML(root, `<span class='loss'>Ablation readiness failed: ${error}</span>`);
  }
}
document.addEventListener("click", function(ev) {
  const runBtn = ev.target.closest ? ev.target.closest(".ablation-run-btn") : null;
  if (runBtn && !runBtn.disabled) {
    const group = runBtn.getAttribute("data-group");
    if (!group) return;
    runBtn.disabled = true;
    runBtn.textContent = "Starting...";
    fetch("/run-ablation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group: group })
    }).then(r => r.json().then(body => ({ ok: r.ok, body })))
      .then(res => {
        if (!res.ok) alert("Could not start ablation: " + ((res.body && res.body.detail) || "unknown error"));
        refreshAblationReadinessSection();
      })
      .catch(err => {
        alert("Could not start ablation: " + err.message);
        refreshAblationReadinessSection();
      });
    return;
  }
  const viewBtn = ev.target.closest ? ev.target.closest(".ablation-view-btn") : null;
  if (viewBtn) {
    ev.preventDefault();
    const group = viewBtn.getAttribute("data-group");
    const out = document.getElementById("ablationRunOutput");
    if (!group || !out) return;
    fetch(`/ablation-output?group=${encodeURIComponent(group)}`)
      .then(r => r.json())
      .then(rec => {
        const body = rec.output || rec.stderr || "(no output captured)";
        const status = rec.ok ? "ok" : "FAILED";
        out.innerHTML = `<div class='muted' style='margin:8px 0 4px'>Run for <strong>${group}</strong> at ${rec.ts} (${rec.duration_s}s, ${status})</div>` +
          `<pre style='white-space:pre-wrap;font-size:11.5px;max-height:340px;overflow:auto'></pre>`;
        out.querySelector("pre").textContent = body;
      })
      .catch(err => { out.innerHTML = `<span class='loss'>Could not load output: ${err.message}</span>`; });
  }
});
refreshAblationReadinessSection();
setInterval(refreshAblationReadinessSection, 15000);
</script>
"""


def render_verification_section() -> str:
    return """
<section id="verification-section">
  <h2>Verification Suite <span id="verificationSummary" class="muted"></span></h2>
  <p class="muted">Integrity and leakage checks that run alongside the shadow-weight ablation. <b>All checks now also run automatically once a day, right after the 14:00 auto-retrain</b> -- that's the only point new training data lands, so it's the only point a fresh run can turn up anything new. Click <b>Run</b> any time for an on-demand check; heavy checks train dry-run models in the background (never touching the live models) and refuse to launch around the 14:00 auto-retrain. <b>view</b> shows per-group detail of the last run -- a <b>READY</b> pill just means there's enough data to run, not that a run is due.</p>
  <div id="verificationContent" class="muted">Loading verification suite...</div>
</section>
<script>
async function refreshVerificationSection() {
  const root = document.getElementById("verificationContent");
  const summary = document.getElementById("verificationSummary");
  if (!root) return;
  try {
    const response = await fetch("/verification-data", { cache: "no-store" });
    const data = await response.json();
    const checks = data.checks || [];
    if (summary) summary.textContent = `(${data.passing} / ${data.total} passing)`;
    const pillFor = v => {
      if (v === "pass") return "<span class='pill long'>PASS</span>";
      if (v === "warn") return "<span class='pill'>WARN</span>";
      if (v === "fail" || v === "error") return "<span class='pill short'>" + v.toUpperCase() + "</span>";
      if (v === "skip") return "<span class='pill no_trade'>SKIP</span>";
      return "<span class='muted'>never run</span>";
    };
    const rows = checks.map(c => {
      let readiness;
      if (c.running) {
        const pct = Math.round((c.progress || 0) * 100);
        readiness = `<span class='pill'>RUNNING</span> <span class='muted'>${pct}%</span>`;
      } else {
        readiness = c.ready ? "<span class='pill long'>READY</span>" : "<span class='pill no_trade'>NOT READY</span>";
      }
      let action;
      if (c.running) {
        action = "<span class='muted'>running&hellip;</span>";
      } else if (c.ready) {
        action = `<button class='verif-run-btn ablation-run-btn' data-check='${c.name}'>Run</button>`;
      } else {
        action = `<button class='ablation-run-btn' disabled title='${(c.detail || "").replace(/'/g, "&#39;")}'>Run</button>`;
      }
      let result = pillFor(c.verdict);
      if (c.ts) {
        let when = "";
        try { when = new Date(c.ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); } catch (e) {}
        result += ` <span class='muted'>${when}</span> <a href='#' class='verif-view-btn muted' data-check='${c.name}'>view</a>`;
      }
      return `<tr>
        <td title='${(c.tip || "").replace(/'/g, "&#39;")}'>${c.label}</td>
        <td>${c.guards || ""}</td>
        <td>${readiness}</td>
        <td>${result}</td>
        <td>${action}</td>
      </tr>`;
    }).join("");
    if (setSectionHTML(root, `<div class="tablewrap"><table><thead><tr><th data-col="0">Check</th><th data-col="1">Guards</th><th data-col="2">Readiness</th><th data-col="3">Last Result</th><th data-col="4">Action</th></tr></thead><tbody id="verificationRows">${rows || "<tr><td colspan='5' class='muted'>No checks registered</td></tr>"}</tbody></table></div><div id="verificationOutput"></div>`)) reapplySort("verificationRows");
  } catch (error) {
    setSectionHTML(root, `<span class='loss'>Verification suite failed: ${error}</span>`);
  }
}
document.addEventListener("click", function(ev) {
  const runBtn = ev.target.closest ? ev.target.closest(".verif-run-btn") : null;
  if (runBtn && !runBtn.disabled) {
    const check = runBtn.getAttribute("data-check");
    if (!check) return;
    runBtn.disabled = true;
    runBtn.textContent = "Starting...";
    fetch("/run-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: check })
    }).then(r => r.json().then(body => ({ ok: r.ok, body })))
      .then(res => {
        if (!res.ok) alert("Could not start check: " + ((res.body && res.body.detail) || "unknown error"));
        refreshVerificationSection();
      })
      .catch(err => { alert("Could not start check: " + err.message); refreshVerificationSection(); });
    return;
  }
  const viewBtn = ev.target.closest ? ev.target.closest(".verif-view-btn") : null;
  if (viewBtn) {
    ev.preventDefault();
    const check = viewBtn.getAttribute("data-check");
    const out = document.getElementById("verificationOutput");
    if (!check || !out) return;
    fetch(`/verification-output?check=${encodeURIComponent(check)}`)
      .then(r => r.json())
      .then(rec => {
        out.innerHTML = `<div class='muted' style='margin:8px 0 4px'>Latest <strong>${check}</strong> run at ${rec.ts} (${rec.duration_s}s) -- verdict: ${rec.verdict}</div>` +
          `<pre style='white-space:pre-wrap;font-size:11.5px;max-height:340px;overflow:auto'></pre>`;
        out.querySelector("pre").textContent = JSON.stringify(rec.groups || rec, null, 1);
      })
      .catch(err => { out.innerHTML = `<span class='loss'>Could not load detail: ${err.message}</span>`; });
  }
});
refreshVerificationSection();
setInterval(refreshVerificationSection, 15000);
</script>
"""


init_exit_models()
# --- EXIT MODEL ADDITION END ---

_entry_health_cache: Dict[str, Dict[str, Any]] = {}
_entry_health_cache_lock = threading.Lock()

# /model-health used to call engine.all_group_ablation_readiness() inline on every
# request -- it iterates every group's full training rows from disk, same cost
# profile as all_group_health() above, and was the other half of why /model-health
# took 13+ seconds (long enough that the dashboard's client-side timeout gave up
# before it ever finished, permanently showing "offline" even though the service
# was up). Cached the same way, on its own key/try-except so a failure computing
# one doesn't wipe out the other's last-known-good value.
_ablation_readiness_cache: Dict[str, Dict[str, Any]] = {}
_ablation_readiness_cache_lock = threading.Lock()

# Template model health, cached on the same background cadence and for the same
# reason as the entry cache: all_group_health() rescans sample files from disk.
_template_health_cache: Dict[str, Dict[str, Any]] = {}
_template_health_cache_lock = threading.Lock()

# Currently-active template per running strategy instance, read straight from
# temalimit.cs's own state files (NinjaTrader.Core.Globals.UserDataDir, which is
# the NT8 root -- one level above this MLService folder). These are a live
# rotation snapshot, not training data, so they get their own cache/lock rather
# than piggybacking on _template_health_cache (which is per symbol+series model
# health, not per running instance).
_active_templates_cache: Dict[str, Dict[str, Any]] = {}
_active_templates_cache_lock = threading.Lock()

_TEMPLATE_STATE_FILE_RE = re.compile(r"^temalimit_template_state_(?P<account>[^_]+)_(?P<instrument>[^_]+)_(?P<series>.+)\.txt$")

_ACTIVE_TEMPLATES_TZ = ZoneInfo("America/Los_Angeles")


def _active_templates_day_cutoff_utc(now_utc: Optional[datetime] = None) -> datetime:
    # "Today" on this card means the current trading day, which the user defines
    # as starting at 15:00 California time (not midnight) -- matches when a fresh
    # rotation day actually begins for these instances. If it's currently before
    # 15:00 PT, "today" started yesterday at 15:00 PT.
    now_pt = (now_utc or datetime.now(timezone.utc)).astimezone(_ACTIVE_TEMPLATES_TZ)
    cutoff_pt = now_pt.replace(hour=15, minute=0, second=0, microsecond=0)
    if now_pt < cutoff_pt:
        cutoff_pt -= timedelta(days=1)
    return cutoff_pt.astimezone(timezone.utc)


def scan_active_templates(nt8_root: Path) -> Dict[str, Dict[str, Any]]:
    # temalimit.cs writes "activeTemplate|encodedMode|templateNumberSeed"
    # (SaveTemplateState). The middle field used to be a hardcoded 0 (legacy
    # consecutive-wins slot); it now carries TemplateMode encoded as -(mode+1)
    # (mode 0 -> -1, mode 1 -> -2, ...) so this scan can tell a fixed-template
    # (mode 0 / Manual) instance apart from a rotating one. Mode 0 never rotates,
    # so it could never satisfy the same-day-rotation filter below on its own
    # terms, and is exempted instead. The negative encoding is deliberate: files
    # written by older builds have a literal 0 there and are ALWAYS rotating
    # instances (old code never saved state for mode 0 at all) -- a plain
    # mode-in-the-slot encoding would have misread every legacy file as mode 0
    # and permanently resurrected the stale rows this card just stopped showing.
    # Any field >= 0 therefore means "legacy rotating file": day cutoff applies.
    #
    # One running instance = one account+instrument, but its state file is named
    # per data series, so re-configuring an instance onto a new series strands the
    # old series' file on disk forever. Keep only the newest file (mtime) per
    # account+instrument -- that is the instance's CURRENT template; the stranded
    # ones would otherwise show up on the card as phantom previous-template rows
    # (e.g. Simvolume's July 9 Range_60 files alongside its live Volume_1000 ones).
    #
    # On top of that, only surface rotating-mode instances whose CURRENT template
    # was rotated into today (per _active_templates_day_cutoff_utc) -- an instance
    # that hasn't rotated since a prior trading day is no longer "active" for this
    # card's purpose, even if it's technically still running unchanged.
    cutoff = _active_templates_day_cutoff_utc()
    picked: Dict[Tuple[str, str], Tuple[float, str, Dict[str, Any]]] = {}
    try:
        for path in nt8_root.glob("temalimit_template_state_*.txt"):
            match = _TEMPLATE_STATE_FILE_RE.match(path.name)
            if not match:
                continue
            try:
                parts = path.read_text(encoding="utf-8").strip().split("|")
                active_template = int(parts[0])
                encoded_mode = int(parts[1]) if len(parts) > 1 else 0
                template_mode = (-encoded_mode - 1) if encoded_mode < 0 else None
                mtime = path.stat().st_mtime
                updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
            except Exception:
                continue

            instance = (match.group("account"), match.group("instrument"))
            if instance in picked and picked[instance][0] >= mtime:
                continue
            key = f"{match.group('account')}_{match.group('instrument')}_{match.group('series')}"
            picked[instance] = (mtime, key, {
                "account": match.group("account"),
                "instrument": match.group("instrument"),
                "series": match.group("series"),
                "active_template": active_template,
                "updated_at": updated_at.isoformat(),
                "_rotated_utc": updated_at,
                "_fixed_mode": template_mode == 0,
            })
    except Exception:
        exit_logger.exception("scan_active_templates failed")
    result = {}
    for _, key, row in picked.values():
        rotated_utc = row.pop("_rotated_utc")
        fixed_mode = row.pop("_fixed_mode")
        if not fixed_mode and rotated_utc < cutoff:
            continue
        result[key] = row
    return result


async def refresh_entry_health_cache_loop() -> None:
    # engine.all_group_health() rescans every group's full TSV history from
    # disk -- expensive, and growing with sample volume. /health is polled
    # every ~minute by the watchdog and must never block on that scan (a
    # blocked /health under load looks like a dead process and gets killed),
    # so compute it here in the background and let /health just read the
    # cache.
    global _entry_health_cache, _ablation_readiness_cache, _template_health_cache, _active_templates_cache
    loop = asyncio.get_running_loop()
    while True:
        try:
            source_counts = await loop.run_in_executor(
                _scan_executor, scan_entry_sample_counts_by_source, engine.samples_path
            )
            counts = {key: bucket["total"] for key, bucket in source_counts.items()}
            keys = sorted(set(engine.known_groups()) | set(counts.keys()))
            result = {key: engine.group_health(key, counts, source_counts) for key in keys}
            with _entry_health_cache_lock:
                _entry_health_cache = result
        except Exception:
            # Deliberately don't touch _entry_health_cache on failure -- keep
            # serving the last-known-good groups instead of blanking the panel
            # out just because one refresh cycle hit a transient error (e.g. a
            # sample file mid-write).
            exit_logger.exception("entry health cache refresh failed")

        try:
            readiness = await loop.run_in_executor(_scan_executor, scan_entry_ablation_readiness, engine.samples_path)
            keys = sorted(set(engine.known_groups()) | set(readiness.keys()))
            ablation_result = {key: readiness.get(key) or assess_group_readiness([]) for key in keys}
            with _ablation_readiness_cache_lock:
                _ablation_readiness_cache = ablation_result
        except Exception:
            exit_logger.exception("ablation readiness cache refresh failed")

        try:
            template_source_counts = await loop.run_in_executor(
                _scan_executor, scan_template_sample_counts_by_source, template_engine.shadow_csv, template_engine.live_csv
            )
            template_counts = {key: bucket["total"] for key, bucket in template_source_counts.items()}
            keys = sorted(set(template_engine.known_groups()) | set(template_counts.keys()))
            template_result = {
                key: template_engine.group_health(key, template_counts, template_source_counts) for key in keys
            }
            with _template_health_cache_lock:
                _template_health_cache = template_result
        except Exception:
            exit_logger.exception("template health cache refresh failed")

        try:
            active_templates_result = await loop.run_in_executor(_scan_executor, scan_active_templates, ROOT.parent)
            with _active_templates_cache_lock:
                _active_templates_cache = active_templates_result
        except Exception:
            exit_logger.exception("active templates cache refresh failed")

        await asyncio.sleep(10)


@app.on_event("startup")
async def startup() -> None:
    # Default anyio thread-pool capacity (40) is shared by every sync endpoint
    # (log-sample, log-exit-sample, predict-exit, health, ...). A burst of
    # exit-sample POSTs from a fast-forming bar series (LineBreak/Renko) can
    # saturate that pool and starve everything else queued behind it.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 256
    load_auto_retrain_state()
    asyncio.create_task(auto_retrain_loop())
    asyncio.create_task(refresh_entry_health_cache_loop())


@app.get("/health")
def health() -> Dict[str, Any]:
    with _entry_health_cache_lock:
        entry_groups = _entry_health_cache
    return {
        "ok": True,
        "service": "nt_ml_service",
        "entry_groups_known": len(entry_groups),
        "entry_groups_ready": sum(1 for g in entry_groups.values() if g.get("model_ready")),
        "window_size": WINDOW_SIZE,
        "n_features": len(FEATURE_NAMES),
        "classes": CLASSES,
        "feature_names": FEATURE_NAMES,
        "min_samples_per_group": MIN_SAMPLES_PER_GROUP,
        "auto_retrain": {
            "enabled": True,
            "local_time": "14:00",
            "last_date": auto_retrain_last_date,
            "last_result": auto_retrain_last_result,
        },
        "auto_verification_sweep": {
            "enabled": True,
            "runs_after": "the 14:00 auto-retrain, same day",
            "last_sweep": auto_verification_last_sweep,
        },
    }


@app.get("/restart", response_class=HTMLResponse)
def restart_service() -> str:
    # The service can't restart itself in-process (new code needs a fresh
    # interpreter), so hand off to a helper script: it waits for this response
    # to flush, kills whatever holds port 8765, then relaunches uvicorn.
    # Windows child processes outlive their parent by default (no Unix-style
    # SIGHUP cascade), so no DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP needed --
    # that combination was silently failing to spawn anything when this
    # process itself has no console (it's launched with -WindowStyle Hidden).
    restart_script = ROOT / "restart_service.ps1"
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
        "<h2>Restarting ML service&hellip;</h2>"
        "<p id='restartStatus'>Shutting down and relaunching uvicorn&hellip; this page "
        "will reload automatically once it's back.</p>"
        "<p><a href='/dashboard'>Go to the dashboard now</a></p>"
        + (RESTART_POLL_SCRIPT % "/dashboard") +
        "</body></html>"
    )


@app.post("/predict")
def predict(request: PredictRequest) -> Dict[str, Any]:
    bars_period = str(request.metadata.get("bars_period") or "")
    result = engine.predict(request.symbol, bars_period, request.window, request.min_confidence)
    return result


@app.post("/log-trigger")
def log_trigger(request: PredictRequest) -> Dict[str, Any]:
    engine.log_trigger(
        {
            "kind": "trigger",
            "logged_at": utc_now(),
            "symbol": request.symbol,
            "trigger": request.trigger,
            "timestamp": request.timestamp,
            "metadata": request.metadata,
            "window": request.window,
        }
    )
    return {"ok": True}


@app.post("/log-sample")
def log_sample(request: TrainingSampleRequest) -> Dict[str, Any]:
    label = request.label.lower()
    if label not in CLASSES:
        return {"ok": False, "error": "label must be one of: " + ", ".join(CLASSES)}

    record = {
        "logged_at": utc_now(),
        "symbol": request.symbol,
        "trigger": request.trigger,
        "timestamp": request.timestamp,
        "label": label,
        "metadata": request.metadata,
        "window": request.window,
    }

    # Windowless rows are gate-veto telemetry, not training data (see VETOES_PATH
    # note above). Route them to their own file so training_samples.jsonl stays
    # 100% trainable rows; the strategy needs no change and dashboards can still
    # count vetoes via /stats' live_vetoes_excluded.
    if not request.window:
        engine.append_jsonl(VETOES_PATH, record)
        return {"ok": True, "label": label, "routed": "veto"}

    engine.log_training_sample(record)
    return {"ok": True, "label": label}


@app.post("/predict-template")
def predict_template(request: PredictRequest) -> Dict[str, Any]:
    bars_period = str(request.metadata.get("bars_period") or "")
    return template_engine.predict(request.symbol, bars_period, request.window)


@app.post("/log-template-sample")
def log_template_sample(request: TemplateSampleRequest) -> Dict[str, Any]:
    if not 1 <= request.template_number <= TEMPLATE_COUNT:
        return {"ok": False, "error": f"template_number must be 1..{TEMPLATE_COUNT}"}

    template_engine.log_template_sample(
        {
            "logged_at": utc_now(),
            "symbol": request.symbol,
            "trigger": request.trigger,
            "setup_timestamp": request.setup_timestamp,
            "resolved_timestamp": request.resolved_timestamp,
            "template_number": request.template_number,
            "selectivity": request.selectivity,
            "setup_direction": request.setup_direction,
            "r_multiple": request.r_multiple,
            "dollars": request.dollars,
            "shadow": request.shadow,
            "bars_period": request.bars_period,
            "entry_price": request.entry_price,
            "exit_price": request.exit_price,
            "mfe_points": request.mfe_points,
            "mae_points": request.mae_points,
            "bars_held": request.bars_held,
            "win": request.win,
            "window": request.window,
        }
    )
    return {"ok": True, "template_number": request.template_number, "shadow": request.shadow}


# --- EXIT MODEL ADDITION START ---
# INSERT AFTER: existing /log-sample endpoint
@app.post("/predict-exit")
def predict_exit(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        error = validate_predict_exit_input(body)
        if error:
            raise HTTPException(status_code=422, detail=error)
        symbol = normalize_symbol(str(body.get("symbol") or ""))
        if symbol == "UNKNOWN":
            raise HTTPException(status_code=400, detail="unknown symbol")
        group = exit_group_key(symbol, body.get("data_series_type", ""), body.get("data_series_value", ""))
        ensure_exit_group(group)

        ready = bool(exit_model_ready.get(group, False))
        if not ready:
            return {
                "hold_confidence": 1.0,
                "exit_confidence": 0.0,
                "recommendation": "hold",
                "model_ready": False,
                "exit_model_enabled": EnableMlExitModel,
                "storage_format": "tsv",
                "warmup_remaining": warmup_remaining(group),
                "phase3_unlocked": phase3_unlocked_for_group(group)["unlocked"],
                "symbol": symbol,
                "group": group,
            }

        started = time.perf_counter()
        # Training caps every example to the trailing EXIT_SEQ_MAX_BARS bars, so
        # inference must see the same window — NT sends the full open-trade
        # history, unbounded for trades held thousands of bars.
        seq_data = body["sequence"][-EXIT_SEQ_MAX_BARS:]
        sequence = torch.tensor(seq_data, dtype=torch.float32).unsqueeze(0)
        lengths = torch.tensor([len(seq_data)], dtype=torch.long)
        context = torch.tensor(body["context"], dtype=torch.float32).unsqueeze(0)
        with exit_locks[group]:
            model = exit_models[group]
            model.eval()
            with torch.no_grad():
                logit = model(sequence, lengths, context)
                hold_confidence = float(torch.sigmoid(logit)[0].item())
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > 500.0:
            exit_logger.warning("predict-exit slow group=%s elapsed_ms=%.1f", group, elapsed_ms)

        recommendation = "exit" if hold_confidence < MlExitHoldThreshold else "hold"
        if recommendation == "exit":
            exit_recommendations_today[group] = exit_recommendations_today.get(group, 0) + 1
        return {
            "hold_confidence": hold_confidence,
            "exit_confidence": 1.0 - hold_confidence,
            "recommendation": recommendation,
            "model_ready": True,
            "exit_model_enabled": EnableMlExitModel,
            "warmup_remaining": 0,
            "phase3_unlocked": phase3_unlocked_for_group(group)["unlocked"],
            "symbol": symbol,
            "group": group,
        }
    except HTTPException:
        raise
    except Exception as exc:
        exit_logger.exception("predict-exit failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/ml-exit-phase")
def ml_exit_phase(symbol: str = "NQ", bars_period: str = "", data_series_type: str = "", data_series_value: str = "") -> Dict[str, Any]:
    try:
        normalized = normalize_symbol(symbol)
        if normalized == "UNKNOWN":
            normalized = "NQ"
        # data_series_type/value (what temalimit sends as of 2026-07-18, same fields
        # /log-exit-sample uses) resolve the REAL exit group. The bars_period fallback is
        # kept for compatibility, but a call with neither lands in {SYMBOL}_UNKNOWN -- a
        # phantom group that always answers phase 1 (the pre-2026-07-18 strategy bug).
        if data_series_type or data_series_value:
            group = exit_group_key(normalized, data_series_type, data_series_value)
        else:
            group = group_key(normalized, bars_period)
        ensure_exit_group(group)
        samples = exit_sample_counts.get(group, 0)
        ready = bool(exit_model_ready.get(group, False))
        phase3 = phase3_unlocked_for_group(group)
        recommended_phase = recommended_exit_phase_for_group(group)
        reason = "recommendations_ready" if recommended_phase >= 2 else "collecting_exit_samples"
        return {
            "recommended_phase": recommended_phase,
            "reason": reason,
            "sample_count": samples,
            "model_ready": ready,
            "exit_model_enabled": EnableMlExitModel,
            "phase3_unlocked": phase3["unlocked"],
            "phase3_conditions": phase3["conditions"],
            "warmup_remaining": warmup_remaining(group),
            "symbol": normalized,
            "group": group,
        }
    except Exception as exc:
        exit_logger.exception("ml-exit-phase failed")
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/log-exit-sample")
def log_exit_sample(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        error = validate_log_exit_sample_input(body)
        if error:
            raise HTTPException(status_code=422, detail=error)
        symbol = normalize_symbol(str(body.get("symbol") or ""))
        if symbol == "UNKNOWN":
            raise HTTPException(status_code=400, detail="unknown symbol")
        group = exit_group_key(symbol, body.get("data_series_type", ""), body.get("data_series_value", ""))
        ensure_exit_group(group)
        create_exit_tsv_if_missing(group)

        label = int(body["label"])
        trade_id = str(body.get("trade_id") or "")
        sample_date = str(body.get("sample_date") or local_now().date().isoformat())
        row = [
            trade_id,
            str(body.get("timestamp") or utc_now()),
            symbol,
            str(body.get("direction") or ""),
            body.get("bars_held", 0),
            body.get("unrealized_r", 0),
            body.get("bar_duration_sec", 0),
            str(body.get("data_series_type") or ""),
            body.get("data_series_value", 0),
            *body["features"],
            label,
            str(body.get("regime") or ""),
            sample_date,
            body.get("entry_price", 0),
            body.get("one_r_points", 0),
            body.get("template_number", 0),
        ]
        with exit_tsv_locks[group]:
            with exit_tsv_path(group).open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle, delimiter="\t", lineterminator="\n").writerow(row)
            exit_sample_counts[group] = exit_sample_counts.get(group, 0) + 1
            labels = exit_label_counts.setdefault(group, {"hold": 0, "exit": 0})
            labels["hold" if label == 1 else "exit"] += 1
            total = exit_sample_counts[group]

            trade_ids = exit_trade_ids_seen.setdefault(group, set())
            trade_ids.add(trade_id)
            exit_trade_counts[group] = len(trade_ids)

            weeks = exit_weeks_seen.setdefault(group, set())
            try:
                parsed_date = datetime.fromisoformat(sample_date.replace("Z", "+00:00"))
                weeks.add(parsed_date.isocalendar()[:2])
                exit_weeks_represented[group] = len(weeks)
            except Exception:
                pass

        label_counts = exit_label_counts.get(group, {"hold": 0, "exit": 0})
        trainable, skip_reason = exit_labels_trainable(label_counts)
        last_total = exit_last_retrain_total.get(group, 0)
        interval = exit_retrain_interval(last_total)
        due = total - last_total >= interval and total >= ExitModelWarmupMin
        if due and trainable and not exit_model_training.get(group, False):
            exit_model_training[group] = True
            exit_last_retrain_total[group] = total
            exit_logger.info(
                "exit model retrain triggered group=%s samples=%s interval=%s",
                group, total, interval,
            )
            background_train_exit_model(group)
        elif due and not trainable:
            # Advance the baseline even when skipping, so a blocked group logs
            # once per interval instead of on every sample once it is overdue.
            exit_last_retrain_total[group] = total
            hold = label_counts.get("hold", 0)
            exits = label_counts.get("exit", 0)
            minority = min(hold, exits)
            exit_logger.info(
                "exit model retrain skipped group=%s samples=%s reason=%s hold=%s exit=%s "
                "minority=%s floor=%s ratio=%.4f%% min_ratio=%.2f%%",
                group, total, skip_reason, hold, exits, minority,
                ExitModelMinMinorityCount,
                (100.0 * minority / total) if total else 0.0,
                100.0 * ExitModelMinMinorityRatio,
            )

        return {"status": "logged", "symbol": symbol, "group": group, "total_samples": total}
    except HTTPException:
        raise
    except Exception as exc:
        exit_logger.exception("log-exit-sample failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/log-ml-exit")
def log_ml_exit(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        symbol = normalize_symbol(str(body.get("symbol") or ""))
        if symbol == "UNKNOWN":
            raise HTTPException(status_code=400, detail="unknown symbol")
        group = exit_group_key(symbol, body.get("data_series_type", ""), body.get("data_series_value", ""))
        ensure_exit_group(group)
        actual_ml_exits_today[group] = actual_ml_exits_today.get(group, 0) + 1
        exit_logger.info(
            "actual ml exit group=%s trade_id=%s hold_confidence=%s bars_held=%s unrealized_r=%s",
            group,
            body.get("trade_id"),
            body.get("hold_confidence"),
            body.get("bars_held"),
            body.get("unrealized_r"),
        )
        return {
            "status": "logged",
            "symbol": symbol,
            "group": group,
            "actual_ml_exits_today": actual_ml_exits_today[group],
        }
    except HTTPException:
        raise
    except Exception as exc:
        exit_logger.exception("log-ml-exit failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/debug-threads")
def debug_threads() -> Dict[str, Any]:
    """Diagnostic for the thread growth on this service (2026-07-20).

    OS thread count climbs ~4.3/min linearly -- 2,245 after 9.4h on the
    uncapped build, and torch.set_num_threads(4) did NOT change the rate, so
    the original "OpenMP teams per calling thread" theory was wrong. The 8766
    dashboard sits at 5 threads and 8767 at 18, so it is specific to this
    service. All leaked threads are parked (Wait/UserRequest), so CPU is
    unaffected; the cost is ~1 MB of reserved stack each.

    Thread provenance is unreadable from outside (session-0 isolation denies
    StartAddress/StartTime), hence this endpoint. The discriminator:
      - Python count flat while OS count climbs -> native (torch/MKL/BLAS)
      - Python count climbs too                 -> anyio threadpool / executor
    Read-only; safe to call any time.
    """
    try:
        threads = threading.enumerate()
        by_name: Dict[str, int] = {}
        for t in threads:
            # Strip trailing ids so pools group together (e.g. "AnyIO worker
            # thread 37" -> "AnyIO worker thread").
            base = str(t.name).rstrip("0123456789").rstrip("-_ ") or "unnamed"
            by_name[base] = by_name.get(base, 0) + 1
        return {
            "python_thread_count": threading.active_count(),
            "python_threads_by_name": dict(sorted(by_name.items(), key=lambda kv: -kv[1])),
            "daemon_count": sum(1 for t in threads if t.daemon),
            "torch_num_threads": torch.get_num_threads(),
            "torch_num_interop_threads": torch.get_num_interop_threads(),
            "exit_train_executor_workers": getattr(_exit_train_executor, "_max_workers", None),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/model-health")
def model_health() -> Dict[str, Any]:
    try:
        exit_groups = sorted(
            set(exit_sample_counts) | set(exit_health) | set(exit_models)
        )
        with _health_lock:
            exit_payload = {group: exit_health_row(group) for group in exit_groups}

        # Both read from the background-refreshed caches (see
        # refresh_entry_health_cache_loop) instead of recomputing from disk on every
        # request -- that recompute is what made this endpoint take 13+ seconds.
        with _entry_health_cache_lock:
            entry_payload = _entry_health_cache
        with _ablation_readiness_cache_lock:
            ablation_readiness = _ablation_readiness_cache
        with _template_health_cache_lock:
            template_payload = _template_health_cache
        with _active_templates_cache_lock:
            active_templates_payload = _active_templates_cache

        return {
            "exit_groups": exit_payload,
            "entry_groups": entry_payload,
            "template_groups": template_payload,
            "active_templates": active_templates_payload,
            # Back-compat alias for anything still reading the old key name.
            "symbols": exit_payload,
            "exit_model_enabled": EnableMlExitModel,
            "entry_ablation_readiness": ablation_readiness,
            "ablation_jobs": _ablation_jobs_snapshot(),
            "ablation_last_runs": _ablation_last_runs_summary(),
        }
    except Exception as exc:
        exit_logger.exception("model-health failed")
        raise HTTPException(status_code=500, detail=str(exc))
# --- EXIT MODEL ADDITION END ---


# --------------------------------------------------------------- ablation runs
# Run button on the Shadow Weight Ablation Readiness card. Launches
# tools/ablate_shadow_weight.py --group KEY as a subprocess (same pattern as
# MLService_Trend's verification.py) and records each run to
# data/ablation_runs.jsonl so the last verdict survives a restart.

_ablation_jobs: Dict[str, Dict[str, Any]] = {}
_ablation_jobs_lock = threading.Lock()
ABLATION_TIMEOUT_S = 3600


def _ablation_runs_path() -> Path:
    return ROOT / "data" / "ablation_runs.jsonl"


def _ablation_jobs_snapshot() -> Dict[str, Dict[str, Any]]:
    with _ablation_jobs_lock:
        return {k: dict(v) for k, v in _ablation_jobs.items()}


def _ablation_last_runs_summary() -> Dict[str, Dict[str, Any]]:
    """Latest record per group, output omitted (the JSON payload refreshes every
    15s; shipping full stdout each time is wasteful -- see /ablation-output)."""
    out: Dict[str, Dict[str, Any]] = {}
    path = _ablation_runs_path()
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            grp = rec.get("group")
            if grp:
                out[grp] = {"ts": rec.get("ts"), "ok": rec.get("ok"), "duration_s": rec.get("duration_s")}
    except Exception:
        return out
    return out


def _run_ablation_blocking(group: str) -> None:
    import subprocess as _sp
    import sys as _sys
    started = time.time()
    with _ablation_jobs_lock:
        _ablation_jobs[group] = {"state": "running", "started_at": utc_now()}
    try:
        proc = _sp.run(
            [_sys.executable, str(ROOT / "tools" / "ablate_shadow_weight.py"), "--group", group],
            capture_output=True, text=True, timeout=ABLATION_TIMEOUT_S, cwd=str(ROOT),
        )
        ok = proc.returncode == 0
        record = {
            "ts": utc_now(), "group": group, "ok": ok, "returncode": proc.returncode,
            "output": (proc.stdout or "")[-8000:],
            "stderr": (proc.stderr or "")[-2000:] if not ok else "",
            "duration_s": round(time.time() - started, 1),
        }
    except _sp.TimeoutExpired:
        record = {"ts": utc_now(), "group": group, "ok": False, "output": "",
                  "stderr": f"timed out after {ABLATION_TIMEOUT_S}s",
                  "duration_s": round(time.time() - started, 1)}
    except Exception as exc:
        record = {"ts": utc_now(), "group": group, "ok": False, "output": "",
                  "stderr": str(exc), "duration_s": round(time.time() - started, 1)}

    try:
        path = _ablation_runs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        exit_logger.exception("failed to record ablation run")
    with _ablation_jobs_lock:
        _ablation_jobs[group] = {
            "state": "idle" if record["ok"] else "error",
            "finished_at": utc_now(),
        }


class RunAblationRequest(BaseModel):
    group: str


@app.post("/run-ablation")
async def run_ablation_route(request: RunAblationRequest) -> Dict[str, Any]:
    group = request.group.strip()
    with _ablation_readiness_cache_lock:
        readiness = dict(_ablation_readiness_cache)
    if group not in readiness:
        raise HTTPException(status_code=404, detail=f"unknown group '{group}'")
    if not (readiness[group] or {}).get("ready"):
        raise HTTPException(status_code=409, detail=f"group '{group}' is not ready for a meaningful ablation")
    with _ablation_jobs_lock:
        if (_ablation_jobs.get(group) or {}).get("state") == "running":
            raise HTTPException(status_code=409, detail="ablation already running for this group")
    # Keep manual ablations clear of the 14:00 auto-retrain (both train models).
    now = local_now()
    if (13, 50) <= (now.hour, now.minute) <= (14, 10):
        raise HTTPException(status_code=409, detail="within the 14:00 auto-retrain window; try again after 14:10")

    with _ablation_jobs_lock:
        _ablation_jobs[group] = {"state": "running", "started_at": utc_now()}

    async def _task() -> None:
        try:
            await run_in_threadpool(_run_ablation_blocking, group)
        except Exception:
            exit_logger.exception("ablation task failed")

    asyncio.create_task(_task())
    return {"ok": True, "group": group, "state": "running"}


# --------------------------------------------------------------- verification
# Same suite as MLService_Trend (see MLService/verification.py). Readiness
# probes scan the whole samples jsonl, so they're TTL-cached here instead of
# recomputed on every 15s dashboard poll -- the same lesson /model-health
# already learned the hard way.

_verification_ready_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}
_verification_ready_lock = threading.Lock()
VERIFICATION_READY_TTL_S = 60


def _compute_verification_readiness() -> Dict[str, Dict[str, Any]]:
    by_group = verification._labels_by_group(engine)
    counts = {k: len(v) for k, v in by_group.items()}
    perm_ready = [k for k, c in counts.items() if c >= verification.MIN_SAMPLES_PER_GROUP]
    drift_ready = [k for k, c in counts.items() if c >= verification.DRIFT_MIN_SAMPLES]
    total = sum(counts.values())
    with _entry_health_cache_lock:
        health = dict(_entry_health_cache)
    trained = [k for k, g in health.items() if (g or {}).get("model_ready")]
    parity_ok = verification.PARITY_TOOL.exists()

    heavy_detail = f"{len(perm_ready)} group(s) with >= {verification.MIN_SAMPLES_PER_GROUP} samples"
    light_detail = f"{len(drift_ready)} group(s) with >= {verification.DRIFT_MIN_SAMPLES} samples"
    out = {
        "permutation": {"ready": bool(perm_ready), "detail": heavy_detail},
        "null_feature": {"ready": bool(perm_ready), "detail": heavy_detail},
        "split_gap": {"ready": bool(perm_ready), "detail": heavy_detail},
        "seed_variance": {"ready": bool(perm_ready), "detail": heavy_detail},
        "label_drift": {"ready": bool(drift_ready), "detail": light_detail},
        "dup_scan": {"ready": bool(drift_ready), "detail": light_detail},
        "feature_psi": {"ready": bool(drift_ready), "detail": light_detail},
        "empty_window": {"ready": total > 0, "detail": f"{total} samples on disk"},
        "determinism": {"ready": bool(trained), "detail": f"{len(trained)} trained model(s)"},
        "base_rate_gate": {"ready": bool(trained), "detail": f"{len(trained)} trained model(s)"},
        "context_parity": {"ready": parity_ok, "detail": "static check on temalimit.cs"},
        "cross_symbol": {"ready": verification.CROSS_SYMBOL_TOOL.exists(),
                         "detail": "cross-instrument bleed tripwire"},
    }
    return out


@app.get("/verification-data")
def verification_data() -> Dict[str, Any]:
    now_ts = time.time()
    with _verification_ready_lock:
        stale = now_ts - float(_verification_ready_cache["ts"]) > VERIFICATION_READY_TTL_S
    if stale:
        data = _compute_verification_readiness()
        with _verification_ready_lock:
            _verification_ready_cache["ts"] = now_ts
            _verification_ready_cache["data"] = data
    with _verification_ready_lock:
        readiness = dict(_verification_ready_cache["data"])

    last = verification.last_results(ROOT)
    jobs = verification.all_job_states()
    checks: List[Dict[str, Any]] = []
    for name, spec in verification.CHECKS.items():
        job = jobs.get(name) or {}
        r = readiness.get(name) or {"ready": False, "detail": "computing..."}
        rec = last.get(name) or {}
        checks.append({
            "name": name,
            "label": spec.get("label"),
            "guards": spec.get("guards"),
            "tip": spec.get("tip"),
            "heavy": bool(spec.get("heavy")),
            "ready": bool(r.get("ready")),
            "detail": r.get("detail"),
            "running": job.get("state") == "running",
            "progress": job.get("progress"),
            "verdict": rec.get("verdict"),
            "ts": rec.get("ts"),
            "duration_s": rec.get("duration_s"),
        })
    passing = sum(1 for c in checks if c["verdict"] == "pass")
    return {"checks": checks, "passing": passing, "total": len(checks)}


class RunCheckRequest(BaseModel):
    name: str


@app.post("/run-check")
async def run_check_route(request: RunCheckRequest) -> Dict[str, Any]:
    name = request.name.strip()
    spec = verification.CHECKS.get(name)
    if not spec:
        raise HTTPException(status_code=404, detail=f"unknown check '{name}'")
    if verification.is_busy(name):
        raise HTTPException(status_code=409, detail="already running")
    now = local_now()
    if spec.get("heavy") and (13, 50) <= (now.hour, now.minute) <= (14, 10):
        raise HTTPException(status_code=409, detail="within the 14:00 auto-retrain window; try again after 14:10")
    # can_start re-scans the samples file -- keep it off the event loop.
    ok, msg = await run_in_threadpool(verification.can_start, name, engine)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    verification._set(name, state="running", progress=0.0, message="queued",
                      started_at=verification._now(), finished_at=None)

    async def _task() -> None:
        try:
            await run_in_threadpool(verification.run_check, name, engine, ROOT)
        except Exception:
            exit_logger.exception("verification check task failed")

    asyncio.create_task(_task())
    return {"ok": True, "name": name, "state": "running"}


@app.get("/verification-output")
def verification_output(check: str) -> Dict[str, Any]:
    """Full per-group detail of the latest run for one check."""
    rec = verification.last_results(ROOT).get(check)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no recorded run for '{check}'")
    return rec


@app.get("/ablation-output")
def ablation_output(group: str) -> Dict[str, Any]:
    """Full stdout of the latest run for one group (fetched on demand)."""
    path = _ablation_runs_path()
    latest: Optional[Dict[str, Any]] = None
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("group") == group:
                    latest = rec
        except Exception:
            latest = None
    if latest is None:
        raise HTTPException(status_code=404, detail=f"no recorded ablation run for '{group}'")
    return latest



def run_retrain(
    symbol: Optional[str] = None,
    bars_period: Optional[str] = None,
    epochs: int = 8,
    batch_size: int = 64,
    learning_rate: float = 0.001,
) -> Dict[str, Any]:
    result = engine.retrain(
        symbol=symbol,
        bars_period=bars_period,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
    )
    # Template model rides the same manual/auto retrain lifecycle as the entry
    # model; reported additively so existing consumers of the entry result keys
    # are unaffected.
    template_result = template_engine.retrain(
        symbol=symbol,
        bars_period=bars_period,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
    )
    combined = dict(result) if isinstance(result, dict) else {"result": result}
    combined["template_result"] = template_result
    return combined


@app.get("/retrain", response_class=HTMLResponse)
def retrain_from_browser(
    symbol: Optional[str] = None,
    bars_period: Optional[str] = None,
    epochs: int = 8,
    batch_size: int = 64,
    learning_rate: float = 0.001,
) -> str:
    started = local_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = run_retrain(symbol=symbol, bars_period=bars_period, epochs=epochs, batch_size=batch_size, learning_rate=learning_rate)
        title = "Retrain complete"
        status = "ok"
        detail = json.dumps(result, indent=2, default=str)
    except Exception as error:
        result = {}
        title = "Retrain failed"
        status = "error"
        detail = str(error)

    scope = f"{symbol or 'ALL'} / {bars_period or 'ALL SERIES'}"

    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 32px; background: #111; color: #eee; }}
    .box {{ max-width: 900px; border: 1px solid #333; padding: 20px; background: #1b1b1b; }}
    .ok {{ color: #7bd88f; }}
    .error {{ color: #ff7b7b; }}
    pre {{ white-space: pre-wrap; background: #0b0b0b; padding: 14px; border: 1px solid #333; overflow: auto; }}
    a {{ color: #8ab4ff; }}
  </style>
</head>
<body>
  <div class=\"box\">
    <h1 class=\"{escape(status)}\">{escape(title)}</h1>
    <p>Started: {escape(started)}</p>
    <p>Scope: {escape(scope)}</p>
    <pre>{escape(detail)}</pre>
    <p><a href=\"/dashboard\">Open dashboard</a> | <a href=\"/stats\">Open stats</a></p>
  </div>
</body>
</html>"""


@app.post("/retrain")
def retrain(request: RetrainRequest) -> Dict[str, Any]:
    return run_retrain(
        symbol=request.symbol,
        bars_period=request.bars_period,
        epochs=request.epochs,
        batch_size=request.batch_size,
        learning_rate=request.learning_rate,
    )




def read_jsonl(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def count_field(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = str(row.get(key) or "(blank)")
        counts[value] += 1
    return dict(counts.most_common())


def count_metadata_field(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        metadata = row.get("metadata") or {}
        value = str(metadata.get(key) or "(blank)")
        counts[value] += 1
    return dict(counts.most_common())


def count_field_with_source_split(rows: List[Dict[str, Any]], key: str, from_metadata: bool = False) -> Dict[str, Dict[str, int]]:
    """Same bucketing as count_field/count_metadata_field, but each bucket also
    carries a live/shadow split (row_source(row)) -- the same breakdown the
    per-group Entry/Template model tables already show under Samples, so the
    Data Mix bar tables (Labels/Symbols/Data Series/Triggers) can show it too."""
    counts: Counter[str] = Counter()
    live: Counter[str] = Counter()
    shadow: Counter[str] = Counter()
    for row in rows:
        if from_metadata:
            value = str((row.get("metadata") or {}).get(key) or "(blank)")
        else:
            value = str(row.get(key) or "(blank)")
        counts[value] += 1
        source = row_source(row)
        if source == "live":
            live[value] += 1
        elif source == "shadow":
            shadow[value] += 1
    return {
        name: {"count": count, "live": live.get(name, 0), "shadow": shadow.get(name, 0)}
        for name, count in counts.most_common()
    }




def is_shadow_row(row: Dict[str, Any]) -> bool:
    metadata = row.get("metadata") or {}
    return bool(metadata.get("shadow")) or str(metadata.get("source") or "").lower() == "shadow"


def row_source(row: Dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    source = str(metadata.get("source") or "").lower()
    if source == "shadow" or metadata.get("shadow"):
        return "shadow"
    if source == "historical_backfill":
        return "backfill"
    return "live"


def metadata_float(row: Dict[str, Any], key: str) -> Optional[float]:
    metadata = row.get("metadata") or {}
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def outcome_from_row(row: Dict[str, Any]) -> str:
    label = str(row.get("label") or "").lower()
    metadata = row.get("metadata") or {}
    prediction = str(metadata.get("prediction") or "").lower()

    pnl = metadata_float(row, "dollars")
    if pnl is None:
        pnl = metadata_float(row, "points")
    if pnl is not None:
        if pnl > 0:
            return "win"
        if pnl < 0:
            return "loss"
        return "unknown"

    if prediction in ("long", "short"):
        return "win" if label == prediction else "loss"

    return "unknown"


def grouped_outcome_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        metadata = row.get("metadata") or {}
        if str(metadata.get("source") or "").lower() == "historical_backfill":
            continue
        # Shadow rows have their own per-template table; mixing 40 templates of
        # paper trades in here would swamp the real-fill outcome stats.
        if is_shadow_row(row):
            continue

        symbol = str(row.get("symbol") or "(blank)")
        data_series = str(metadata.get("bars_period") or "(blank)")
        key = symbol + " | " + data_series
        item = groups.setdefault(
            key,
            {
                "key": key,
                "symbol": symbol,
                "data_series": data_series,
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "unknown": 0,
                "no_trade": 0,
                "pnl_dollars": 0.0,
                "pnl_points": 0.0,
                "pnl_records": 0,
                "followed_setup": 0,
                "reversed_setup": 0,
                "reversal_wins": 0,
                "reversal_losses": 0,
                "ml_long": 0,
                "ml_short": 0,
            },
        )

        item["samples"] += 1
        label = str(row.get("label") or "").lower()
        if label == "no_trade":
            item["no_trade"] += 1
        ml_direction = str(metadata.get("ml_direction") or metadata.get("prediction") or "").lower()
        setup_direction = str(metadata.get("setup_direction") or "").lower()
        ml_reversal = metadata.get("ml_reversal")
        is_reversal = False
        if ml_direction == "long":
            item["ml_long"] += 1
        elif ml_direction == "short":
            item["ml_short"] += 1
        if setup_direction in ("long", "short") and ml_direction in ("long", "short"):
            is_reversal = bool(ml_reversal) if isinstance(ml_reversal, bool) else setup_direction != ml_direction
            if is_reversal:
                item["reversed_setup"] += 1
            else:
                item["followed_setup"] += 1

        pnl_dollars = metadata_float(row, "dollars")
        pnl_points = metadata_float(row, "points")
        has_pnl = pnl_dollars is not None or pnl_points is not None

        outcome_result = "unknown"
        if has_pnl:
            if pnl_dollars is not None:
                item["pnl_dollars"] += pnl_dollars
            if pnl_points is not None:
                item["pnl_points"] += pnl_points
            item["pnl_records"] += 1
            if (pnl_dollars if pnl_dollars is not None else pnl_points or 0.0) > 0:
                item["wins"] += 1
                outcome_result = "win"
            elif (pnl_dollars if pnl_dollars is not None else pnl_points or 0.0) < 0:
                item["losses"] += 1
                outcome_result = "loss"
            else:
                if label != "no_trade":
                    item["unknown"] += 1
        else:
            outcome = outcome_from_row(row)
            if outcome == "win":
                item["wins"] += 1
                outcome_result = "win"
            elif outcome == "loss":
                item["losses"] += 1
                outcome_result = "loss"
            else:
                if label != "no_trade":
                    item["unknown"] += 1

        if is_reversal:
            if outcome_result == "win":
                item["reversal_wins"] += 1
            elif outcome_result == "loss":
                item["reversal_losses"] += 1

    for item in groups.values():
        decided = item["wins"] + item["losses"]
        item["win_rate"] = (item["wins"] / decided * 100.0) if decided else 0.0
        reversal_decided = item["reversal_wins"] + item["reversal_losses"]
        item["reversal_win_rate"] = (item["reversal_wins"] / reversal_decided * 100.0) if reversal_decided else 0.0

    return sorted(groups.values(), key=lambda item: item["samples"], reverse=True)

def shadow_template_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-template outcome stats over shadow (paper-traded) samples only.
    This is the comparison view the shadow sweep exists to feed."""
    groups: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        if not is_shadow_row(row):
            continue
        metadata = row.get("metadata") or {}
        template = metadata.get("template_number")
        template_num = int(template) if isinstance(template, (int, float)) else -1
        symbol = str(row.get("symbol") or "(blank)")
        data_series = str(metadata.get("bars_period") or "(blank)")
        key = f"{symbol} | {data_series} | T{template_num}"
        item = groups.setdefault(
            key,
            {
                "symbol": symbol,
                "data_series": data_series,
                "template": template_num,
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "no_trade": 0,
                "pnl_points": 0.0,
                "pnl_dollars": 0.0,
            },
        )

        item["samples"] += 1
        if str(row.get("label") or "").lower() == "no_trade":
            item["no_trade"] += 1

        pnl_dollars = metadata_float(row, "dollars")
        pnl_points = metadata_float(row, "points")
        if pnl_dollars is not None:
            item["pnl_dollars"] += pnl_dollars
        if pnl_points is not None:
            item["pnl_points"] += pnl_points
        pnl = pnl_dollars if pnl_dollars is not None else pnl_points
        if pnl is not None:
            if pnl > 0:
                item["wins"] += 1
            elif pnl < 0:
                item["losses"] += 1

    for item in groups.values():
        decided = item["wins"] + item["losses"]
        item["win_rate"] = (item["wins"] / decided * 100.0) if decided else 0.0

    return sorted(groups.values(), key=lambda item: (item["symbol"], item["data_series"], item["template"]))


def count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


_SAMPLE_RANGE_RE = re.compile(r"^(\d{1,2})d$")


def normalize_sample_range(value: str) -> str:
    value = (value or "all").strip().lower()
    return value if value == "all" or _SAMPLE_RANGE_RE.match(value) else "all"


def _sample_range_start_local(range_name: str) -> Optional[datetime]:
    # Same trading-day semantics as the Live dashboard's filter_rows_by_range:
    # a "day" is anchored at 15:00 local (California) session start, so 1d means
    # "since the most recent 3pm boundary", 3d spans that plus the two sessions
    # before it. Sample timestamps are naive local time, so compare naively.
    match = _SAMPLE_RANGE_RE.match(normalize_sample_range(range_name))
    if not match:
        return None
    now = datetime.now()
    anchor = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor - timedelta(days=int(match.group(1)) - 1)


def _parse_sample_timestamp(text: str) -> Optional[datetime]:
    # Strategy-written timestamps carry 7-digit fractional seconds
    # ("2026-07-05T15:03:34.6950000"), which fromisoformat only accepts on
    # newer Pythons -- trim to microseconds before parsing.
    if not text:
        return None
    try:
        if "." in text:
            head, frac = text.split(".", 1)
            text = f"{head}.{frac[:6]}"
        parsed = datetime.fromisoformat(text)
        # Range boundaries are naive local; drop tzinfo if a row carries one.
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def filter_sample_rows_by_range(rows: List[Dict[str, Any]], range_name: str) -> List[Dict[str, Any]]:
    start = _sample_range_start_local(range_name)
    if start is None:
        return rows
    filtered = []
    for row in rows:
        ts = _parse_sample_timestamp(str(row.get("timestamp") or ""))
        if ts is not None and ts >= start:
            filtered.append(row)
    return filtered


# training_samples.jsonl is ~130MB because every row embeds its full feature
# window; sample_stats only ever reads the scalar fields + metadata, yet used to
# re-parse the whole file on every /dashboard and /stats request (~7-12s, and
# every range-picker click pays it again). Cache the parsed rows in memory keyed
# by the file's (mtime_ns, size) -- it only changes when a sample is appended or
# a retrain rewrites it -- and drop each row's "window" on the way in so the
# resident cache is ~10MB, not 130MB. Consequence: the "recent" rows in /stats
# no longer include "window" (nothing dashboard-side ever rendered it; training
# reads the file directly and is unaffected).
_sample_rows_cache_lock = threading.Lock()
# consumed = byte offset parsed so far (always ends on a line boundary, so a
# request racing a mid-append sees the half-written line left for next time).
# head_hash/head_len fingerprint the file's start: if it changes, the file was
# REWRITTEN (retrain dedupe/purge), not appended, and the cache does a full
# reparse instead of trusting the tail. boundary_hash/boundary_off do the same
# for the last 4KB before `consumed`, so "rewrote the middle but kept the first
# 64KB" can't slip a corrupt tail-parse through either.
_sample_rows_cache: Dict[str, Any] = {
    "mtime_ns": None, "consumed": 0, "rows": [],
    "head_hash": None, "head_len": 0, "boundary_hash": None, "boundary_off": 0,
}

# Same treatment for the vetoes line count (~40MB file re-read per request):
# count newlines in just the appended tail, with a head-hash guard so a rewrite
# (veto purge) still triggers a full recount.
_vetoes_count_cache: Dict[str, Any] = {"mtime_ns": None, "consumed": 0, "count": 0, "head_hash": None, "head_len": 0}


def _file_sig(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _hash_file_range(path: Path, offset: int, length: int) -> Optional[str]:
    if length <= 0:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return hashlib.sha256(handle.read(length)).hexdigest()
    except OSError:
        return None


def _parse_sample_bytes(raw: bytes) -> Tuple[List[Dict[str, Any]], int]:
    # Returns (rows, consumed_bytes). Only parses up to the last newline so a
    # concurrently-appending writer's half-written final line is deferred, not
    # half-parsed and then skipped forever.
    last_newline = raw.rfind(b"\n")
    if last_newline == -1:
        return [], 0
    consumed = last_newline + 1
    rows: List[Dict[str, Any]] = []
    for line in raw[:consumed].decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        row.pop("window", None)
        rows.append(row)
    return rows, consumed


def _load_sample_rows_cached() -> List[Dict[str, Any]]:
    path = engine.samples_path
    sig = _file_sig(path)
    if sig is None:
        return []
    mtime_ns, size = sig

    with _sample_rows_cache_lock:
        cache = dict(_sample_rows_cache)
    if cache["mtime_ns"] == mtime_ns and cache["consumed"] == size:
        return cache["rows"]

    # Append fast-path: the file grew and both fingerprints (start of file,
    # last 4KB of the already-parsed region) still match -- parse only the new
    # tail instead of all ~130MB.
    incremental = (
        cache["consumed"] > 0
        and size >= cache["consumed"]
        and cache["head_hash"] is not None
        and _hash_file_range(path, 0, cache["head_len"]) == cache["head_hash"]
        and _hash_file_range(path, cache["boundary_off"], cache["consumed"] - cache["boundary_off"]) == cache["boundary_hash"]
    )
    try:
        with path.open("rb") as handle:
            handle.seek(cache["consumed"] if incremental else 0)
            raw = handle.read()
    except OSError:
        return cache["rows"]

    new_rows, consumed_delta = _parse_sample_bytes(raw)
    if incremental:
        rows = cache["rows"] + new_rows
        consumed = cache["consumed"] + consumed_delta
    else:
        rows = new_rows
        consumed = consumed_delta

    head_len = min(65536, consumed)
    boundary_off = max(0, consumed - 4096)
    with _sample_rows_cache_lock:
        _sample_rows_cache["mtime_ns"] = mtime_ns
        _sample_rows_cache["consumed"] = consumed
        _sample_rows_cache["rows"] = rows
        _sample_rows_cache["head_len"] = head_len
        _sample_rows_cache["head_hash"] = _hash_file_range(path, 0, head_len)
        _sample_rows_cache["boundary_off"] = boundary_off
        _sample_rows_cache["boundary_hash"] = _hash_file_range(path, boundary_off, consumed - boundary_off)
    return rows


def _count_vetoes_cached() -> int:
    sig = _file_sig(VETOES_PATH)
    if sig is None:
        return 0
    mtime_ns, size = sig
    with _sample_rows_cache_lock:
        cache = dict(_vetoes_count_cache)
    if cache["mtime_ns"] == mtime_ns and cache["consumed"] == size:
        return cache["count"]

    incremental = (
        cache["consumed"] > 0
        and size >= cache["consumed"]
        and cache["head_hash"] is not None
        and _hash_file_range(VETOES_PATH, 0, cache["head_len"]) == cache["head_hash"]
    )
    try:
        with VETOES_PATH.open("rb") as handle:
            handle.seek(cache["consumed"] if incremental else 0)
            raw = handle.read()
    except OSError:
        return cache["count"]
    # Count only whole lines (up to the last newline), like the sample cache,
    # so a mid-append half-line isn't counted twice across polls.
    last_newline = raw.rfind(b"\n")
    consumed_delta = last_newline + 1 if last_newline != -1 else 0
    tail_count = sum(1 for line in raw[:consumed_delta].split(b"\n") if line.strip())
    count = (cache["count"] + tail_count) if incremental else tail_count
    consumed = (cache["consumed"] + consumed_delta) if incremental else consumed_delta

    head_len = min(65536, consumed)
    with _sample_rows_cache_lock:
        _vetoes_count_cache["mtime_ns"] = mtime_ns
        _vetoes_count_cache["consumed"] = consumed
        _vetoes_count_cache["count"] = count
        _vetoes_count_cache["head_len"] = head_len
        _vetoes_count_cache["head_hash"] = _hash_file_range(VETOES_PATH, 0, head_len)
    return count


def sample_stats(recent_limit: int = 25, range_name: str = "all") -> Dict[str, Any]:
    rows = filter_sample_rows_by_range(_load_sample_rows_cached(), range_name)
    recent = rows[-recent_limit:] if recent_limit > 0 else []

    known_groups = engine.known_groups()
    total_model_size = sum(
        engine.model_path_for_group(g).stat().st_size
        for g in known_groups
        if engine.model_path_for_group(g).exists()
    )
    trained_groups = sum(1 for g in known_groups if engine._get_or_create_state(g).trained)

    return {
        "ok": True,
        "generated_at": local_now().isoformat(),
        "entry_groups_known": len(known_groups),
        "entry_groups_trained": trained_groups,
        "entry_model_dir": str(engine.model_dir),
        "model_size_bytes": total_model_size,
        "n_features": len(FEATURE_NAMES),
        "sample_count": len(rows),
        "live_vetoes_excluded": _count_vetoes_cached(),
        "labels": count_field_with_source_split(rows, "label"),
        "symbols": count_field_with_source_split(rows, "symbol"),
        "data_series": count_field_with_source_split(rows, "bars_period", from_metadata=True),
        "triggers": count_field_with_source_split(rows, "trigger"),
        "sources": dict(Counter(row_source(row) for row in rows).most_common()),
        "symbol_data_series": grouped_outcome_stats(rows),
        "shadow_templates": shadow_template_stats(rows),
        "recent": recent,
    }


def render_bar_table(title: str, counts: Dict[str, Any]) -> str:
    # counts is either the plain {name: count} shape (used for "Sample Sources",
    # which already IS the live/shadow split -- a per-row subcount there would
    # be redundant) or the {name: {count, live, shadow}} shape from
    # count_field_with_source_split (Labels/Symbols/Data Series/Triggers),
    # which additionally shows each bucket's live/shadow subcount -- the same
    # breakdown the per-group Entry/Template model tables show under Samples.
    total = max(1, sum(v["count"] if isinstance(v, dict) else v for v in counts.values()))
    rows = []
    for name, value in counts.items():
        is_split = isinstance(value, dict)
        count = value["count"] if is_split else value
        pct = count / total * 100.0
        subcount = (
            f"<div class='subcount'>{value.get('live', 0):,} live &middot; {value.get('shadow', 0):,} shadow</div>"
            if is_split else ""
        )
        rows.append(
            "<tr>"
            f"<td>{escape(str(name))}</td>"
            f"<td class='num'>{count}{subcount}</td>"
            f"<td><div class='bar'><span style='width:{pct:.1f}%'></span></div></td>"
            f"<td class='num'>{pct:.1f}%</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='4' class='muted'>No data yet</td></tr>")

    return (
        f"<section><h2>{escape(title)}</h2>"
        "<table><thead><tr><th data-col='0'>Name</th><th data-col='1'>Count</th><th data-col='2'>Share</th><th data-col='3'>%</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )




def render_outcome_table(rows: List[Dict[str, Any]]) -> str:
    html_rows = []
    for row in rows:
        html_rows.append(
            "<tr>"
            f"<td>{escape(str(row['symbol']))}</td>"
            f"<td>{escape(str(row['data_series']))}</td>"
            f"<td class='num'>{row['samples']}</td>"
            f"<td class='num win'>{row['wins']}</td>"
            f"<td class='num loss'>{row['losses']}</td>"
            f"<td class='num'>{row['no_trade']}</td>"
            f"<td class='num'>{row['unknown']}</td>"
            f"<td class='num'>{row['win_rate']:.1f}%</td>"
            f"<td class='num'>{row['pnl_records']}</td>"
            f"<td class='num'>{row['followed_setup']}</td>"
            f"<td class='num'>{row['reversed_setup']}</td>"
            f"<td class='num'>{row['reversal_win_rate']:.1f}%</td>"
            f"<td class='num'>{row['ml_long']}</td>"
            f"<td class='num'>{row['ml_short']}</td>"
            f"<td class='num'>{row['pnl_points']:.2f}</td>"
            f"<td class='num'>{row['pnl_dollars']:.2f}</td>"
            "</tr>"
        )
    if not html_rows:
        html_rows.append("<tr><td colspan='16' class='muted'>No data yet</td></tr>")

    total_wins = sum(row["wins"] for row in rows)
    total_losses = sum(row["losses"] for row in rows)
    total_decided = total_wins + total_losses
    overall_win_rate = (total_wins / total_decided * 100.0) if total_decided else None
    winrate_attr = f" data-winrate='{overall_win_rate:.1f}'" if overall_win_rate is not None else ""

    return (
        f"<section{winrate_attr}><h2>Symbol + Data Series Outcomes</h2>"
        "<div class='tablewrap'>"
        "<table><thead><tr><th data-col='0'>Symbol</th><th data-col='1'>Data Series</th><th data-col='2'>Samples</th><th data-col='3'>Wins</th><th data-col='4'>Losses</th><th data-col='5'>No Trade</th><th data-col='6'>Unknown</th><th data-col='7'>Win %</th><th data-col='8'>P/L Records</th><th data-col='9'>Followed</th><th data-col='10'>Reversed</th><th data-col='11'>Reversal Win %</th><th data-col='12'>ML Long</th><th data-col='13'>ML Short</th><th data-col='14'>Points</th><th data-col='15'>Dollars</th></tr></thead>"
        f"<tbody>{''.join(html_rows)}</tbody></table>"
        "</div>"
        "<div class='hint'>Historical backfill rows are excluded from outcomes but still used for training. Live ML exit rows include true points/dollars going forward.</div>"
        "</section>"
    )

def render_shadow_template_table(rows: List[Dict[str, Any]]) -> str:
    html_rows = []
    for row in rows:
        html_rows.append(
            "<tr>"
            f"<td>{escape(str(row['symbol']))}</td>"
            f"<td>{escape(str(row['data_series']))}</td>"
            f"<td class='num'>{row['template']}</td>"
            f"<td class='num'>{row['samples']}</td>"
            f"<td class='num win'>{row['wins']}</td>"
            f"<td class='num loss'>{row['losses']}</td>"
            f"<td class='num'>{row['no_trade']}</td>"
            f"<td class='num'>{row['win_rate']:.1f}%</td>"
            f"<td class='num'>{row['pnl_points']:.2f}</td>"
            f"<td class='num'>{row['pnl_dollars']:.2f}</td>"
            "</tr>"
        )
    if not html_rows:
        html_rows.append("<tr><td colspan='10' class='muted'>No shadow samples yet</td></tr>")

    total_wins = sum(row["wins"] for row in rows)
    total_losses = sum(row["losses"] for row in rows)
    total_decided = total_wins + total_losses
    overall_win_rate = (total_wins / total_decided * 100.0) if total_decided else None
    winrate_attr = f" data-winrate='{overall_win_rate:.1f}'" if overall_win_rate is not None else ""

    return (
        f"<section{winrate_attr}><h2>Shadow Template Performance</h2>"
        "<div class='tablewrap'>"
        "<table><thead><tr><th data-col='0'>Symbol</th><th data-col='1'>Data Series</th><th data-col='2'>Template</th><th data-col='3'>Samples</th><th data-col='4'>Wins</th><th data-col='5'>Losses</th><th data-col='6'>No Trade</th><th data-col='7'>Win %</th><th data-col='8'>Points</th><th data-col='9'>Dollars</th></tr></thead>"
        f"<tbody>{''.join(html_rows)}</tbody></table>"
        "</div>"
        "<div class='hint'>Paper-traded template sweep with pessimistic fills (trade-through required, stop-priority bars, slippage on stops). "
        "Excluded from the real-outcome table above; trains the entry model at reduced weight.</div>"
        "</section>"
    )


TEMPLATE_REFERENCE_PATH = ROOT.parent / "temalimit_template_reference.json"


def load_template_reference() -> Optional[Dict[str, Any]]:
    # temalimit.cs writes this once per NinjaTrader process (State.DataLoaded,
    # see ExportTemplateReferenceIfNeeded) with every template's fully-computed
    # fields. Reading it live -- instead of hand-copying values into a Python
    # table -- is what keeps this dashboard from drifting out of sync with the
    # strategy whenever templates are edited.
    try:
        with open(TEMPLATE_REFERENCE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stacked_header_html(label: str) -> str:
    """Splits a camelCase/slash header label into its words and joins them
    with <br> so the header renders stacked instead of on one wide line --
    e.g. 'MfiLongMax' -> 'Mfi<br>Long<br>Max'. Keeps acronyms like 'BB'
    together (won't split 'BBLen' into 'B'/'B'/'Len', only 'BB'/'Len')."""
    words: List[str] = []
    for part in label.split("/"):
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", part)
        words.extend(w for w in spaced.split(" ") if w)
    return "<br>".join(escape(w) for w in words)


# --- Recent-bars bridge: reuses the SAME file-based protocol as the port-8766
# dashboard's /api/trade-chart (see live_dashboard_server.py get_trade_chart):
# drop a `<id>.request.json` into the shared ChartRequests dir, and the
# ChartDataExporter.cs NinjaScript AddOn (polling inside NinjaTrader) resolves
# the contract, pulls 1-minute bars, and writes `<id>.json` back. Same AddOn,
# same folder, same bar schema {t,o,h,l,c,v} -- so the BB-envelope card shows
# exactly the price the 8766 trade charts show. ---
CHART_REQUEST_DIR = ROOT.parent / "ChartRequests"
CHART_CACHE_VERSION = "v3"  # must match live_dashboard_server.CHART_CACHE_VERSION
CHART_REQUEST_STALE_SECONDS = 20.0
NQ_BARS_LOOKBACK = timedelta(hours=3)
# Round the request window to 5-minute buckets so repeated page loads within the
# same bucket reuse one cached response file instead of spamming a fresh request
# (and a fresh NinjaTrader fetch) every second.
NQ_BARS_BUCKET_SECONDS = 300
# All our request/response files start with this so cleanup only ever touches
# OUR files, never the 8766 trade-chart cache sharing this dir. The AddOn treats
# the id as an opaque filename stem (globs *.request.json, writes <id>.json), so
# the prefix carries through to the response name safely.
NQ_BARS_PREFIX = "nqbars_"
NQ_BARS_MAX_AGE_SECONDS = 24 * 3600
NQ_BARS_CLEANUP_INTERVAL_SECONDS = 600
_nq_bars_last_cleanup = 0.0


def _nq_bars_window() -> tuple:
    now = datetime.now(timezone.utc)
    bucket = now.timestamp() - (now.timestamp() % NQ_BARS_BUCKET_SECONDS)
    to_dt = datetime.fromtimestamp(bucket, tz=timezone.utc)
    return to_dt - NQ_BARS_LOOKBACK, to_dt


def _cleanup_nq_bars_files() -> None:
    """Delete our own nq-bars request/response files older than a day. Throttled
    so it globs the dir at most every 10 min, and scoped to NQ_BARS_PREFIX so it
    never removes the 8766 trade-chart files sharing ChartRequests. (The AddOn
    also nukes everything older than 6h while NinjaTrader runs; this is the
    fallback for when NinjaTrader is off and nothing else cleans up.)"""
    global _nq_bars_last_cleanup
    now = time.time()
    if now - _nq_bars_last_cleanup < NQ_BARS_CLEANUP_INTERVAL_SECONDS:
        return
    _nq_bars_last_cleanup = now
    try:
        for f in CHART_REQUEST_DIR.glob(f"{NQ_BARS_PREFIX}*"):
            try:
                if now - f.stat().st_mtime > NQ_BARS_MAX_AGE_SECONDS:
                    f.unlink()
            except OSError:
                pass  # locked/missing -- best effort
    except OSError:
        pass


def get_nq_recent_bars() -> Dict[str, Any]:
    """Returns {'status': 'ready'|'pending'|'error', 'bars': [...]} for a recent
    3h window of NQ 1-minute bars via the shared ChartDataExporter bridge."""
    from_dt, to_dt = _nq_bars_window()
    key = f"{CHART_CACHE_VERSION}|NQ|recent|{from_dt.isoformat()}|{to_dt.isoformat()}"
    request_id = NQ_BARS_PREFIX + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    try:
        CHART_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"status": "error", "error": "chart request dir unavailable"}
    _cleanup_nq_bars_files()

    response_path = CHART_REQUEST_DIR / f"{request_id}.json"
    if response_path.exists():
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            payload = None
        if payload is not None:
            if payload.get("ok"):
                return {"status": "ready", "bars": payload.get("bars") or []}
            return {"status": "error", "error": payload.get("error", "chart export failed")}

    request_path = CHART_REQUEST_DIR / f"{request_id}.request.json"
    needs_write = True
    if request_path.exists():
        try:
            needs_write = (time.time() - request_path.stat().st_mtime) > CHART_REQUEST_STALE_SECONDS
        except OSError:
            needs_write = True
    if needs_write:
        body = {"ticker": "NQ", "fromTime": from_dt.isoformat(), "toTime": to_dt.isoformat()}
        try:
            request_path.write_text(json.dumps(body), encoding="utf-8")
        except OSError:
            return {"status": "error", "error": "could not write chart request"}
    return {"status": "pending"}


@app.get("/api/nq-bars")
def api_nq_bars() -> Dict[str, Any]:
    return get_nq_recent_bars()


def render_template_risk_table() -> str:
    reference = load_template_reference()
    templates = (reference or {}).get("templates") or []
    tickers = (reference or {}).get("tickers") or ["ES", "NQ", "YM", "RTY"]

    # Pullback ticks moved to its own card (see render_pullback_by_symbol_section) --
    # not repeated here. Columns are (leaf label, group label or None, getter).
    def risk_getter(ticker: str, sub_key: str):
        return lambda row: (row.get("risk") or {}).get(ticker, {}).get(sub_key, "")

    groups: List[tuple] = [
        (None, [("Tmpl", lambda r: r.get("template", ""))]),
        (None, [("Sel", lambda r: r.get("selectivity", ""))]),
        ("Entry Filter", [
            ("MfiLongMax", lambda r: r.get("mfiLongMax", "")),
            ("MfiShortMin", lambda r: r.get("mfiShortMin", "")),
            ("RsiLongMax", lambda r: r.get("rsiLongMax", "")),
            ("RsiShortMin", lambda r: r.get("rsiShortMin", "")),
            ("StochLongMax", lambda r: r.get("stochLongMax", "")),
            ("StochShortMin", lambda r: r.get("stochShortMin", "")),
            ("MlMinConf", lambda r: r.get("mlMinConfidence", "")),
        ]),
        ("Exit Rule", [
            ("ExitHold", lambda r: r.get("exitHoldThreshold", "")),
            ("ExitMinR", lambda r: r.get("exitMinR", "")),
        ]),
        ("Indicators", [
            ("TemaLen", lambda r: r.get("temaLength", "")),
            ("BBLen", lambda r: r.get("bbLength", "")),
            ("BBStdDev", lambda r: r.get("bbStdDev", "")),
            ("MfiPeriod", lambda r: r.get("mfiPeriod", "")),
            ("StochRsiPeriod", lambda r: r.get("stochRsiPeriod", "")),
        ]),
        ("Timing", [
            ("ExitMinBars", lambda r: r.get("exitMinBars", "")),
            ("MfiPriorBars", lambda r: r.get("mfiPriorBars", "")),
            ("StochRsiLookback", lambda r: r.get("stochRsiCrossLookbackBars", "")),
            ("EntryExpireMin", lambda r: r.get("entryOrderExpireMinutes", "")),
            ("ReentryCooldown", lambda r: r.get("reentryCooldownBars", "")),
        ]),
    ]
    for t in tickers:
        groups.append((t, [
            ("Risk1R", risk_getter(t, "risk1R")),
            ("Ladder/Daily", risk_getter(t, "ladderDaily")),
            ("Slip", risk_getter(t, "slippage")),
        ]))

    columns = [(label, getter) for _, cols in groups for label, getter in cols]

    # These columns feed the ML feature window directly (AppendMlFeatureRowFor
    # in temalimit.cs reads temaNow/bb.Upper/Middle/Lower/mfiNow/stochNow, all
    # computed using TemaLen/BBLen/BBStdDev/MfiPeriod/StochRsiPeriod), so
    # hand-editing them changes what the model sees for the same market state
    # -- risks poisoning training data. Sel is deliberately NOT included --
    # it's only an upstream driver of these five, not itself a poisoning risk.
    # MfiPriorBars/StochRsiLookback/EntryExpireMin/ReentryCooldown are also
    # excluded -- they only affect entry-trigger/order-management logic, never
    # appear in the feature vector, so editing them doesn't touch what the
    # model learns.
    READONLY_LABELS = {
        "TemaLen", "BBLen", "BBStdDev", "MfiPeriod", "StochRsiPeriod",
    }

    group_header_cells = []
    leaf_header_cells = []
    group_start_cols = set()
    readonly_cols = set()
    col_group: Dict[int, str] = {}
    group_cols: Dict[str, List[int]] = {}
    col_index = 0
    for group_label, cols in groups:
        if group_label is None:
            label, _ = cols[0]
            if label in READONLY_LABELS:
                readonly_cols.add(col_index)
            group_header_cells.append(f"<th rowspan='2' data-col='{col_index}'>{escape(label)}</th>")
        else:
            group_start_cols.add(col_index)
            first_col_in_group = col_index
            for label, _ in cols:
                if label in READONLY_LABELS:
                    readonly_cols.add(col_index)
                col_group[col_index] = group_label
                group_cols.setdefault(group_label, []).append(col_index)
                leaf_header_cells.append(
                    f"<th data-col='{col_index}' data-group='{escape(group_label)}' "
                    f"class='stacked' title='{escape(label)}'>{stacked_header_html(label)}</th>"
                )
                col_index += 1
            group_header_cells.append(
                f"<th colspan='{col_index - first_col_in_group}' class='grp' "
                f"data-group='{escape(group_label)}' data-cols='{','.join(str(c) for c in range(first_col_in_group, col_index))}'>"
                f"{escape(group_label)}</th>"
            )
        if group_label is None:
            col_index += 1

    html_rows = []
    for row in templates:
        cells = "".join(
            f"<td class='num{' grp-start' if i in group_start_cols else ''}{' readonly-col' if i in readonly_cols else ''}' data-col='{i}'>{escape(str(getter(row)))}</td>"
            for i, (_, getter) in enumerate(columns)
        )
        tmpl_no = escape(str(row.get("template", "")))
        html_rows.append(f"<tr data-template='{tmpl_no}'>{cells}</tr>")

    if html_rows:
        generated_at = escape(str((reference or {}).get("generatedAtUtc") or "unknown"))
        hint = f"Live export from temalimit.cs (generated {generated_at} UTC) -- always matches the running strategy. Click a column header to sort."
    else:
        html_rows.append(
            f"<tr><td colspan='{len(columns)}' class='muted'>"
            "No live export found yet -- load temalimit.cs once in NinjaTrader to generate "
            f"{escape(str(TEMPLATE_REFERENCE_PATH))}.</td></tr>"
        )
        hint = "Waiting for temalimit.cs to export its template reference (happens once per NinjaTrader process, on strategy load)."

    group_chip_html = "".join(
        f"<button type='button' class='tref-chip' data-group='{escape(g)}'>{escape(g)}</button>"
        for g in group_cols.keys()
    )
    col_checkbox_html = "".join(
        f"<label class='tref-colcheck'><input type='checkbox' checked data-col='{i}'>{escape(label)}</label>"
        for i, (label, _) in enumerate(columns)
        if label != "Tmpl"
    )

    # Row-range groups mirror the column chips -- pick a template band to hide
    # it, pick it again to bring it back. Ranges are inclusive (lo, hi).
    ROW_RANGES = [(1, 5), (6, 10), (11, 15), (16, 21), (22, 27), (28, 32), (33, 35), (36, 40)]
    row_chip_html = "".join(
        f"<button type='button' class='tref-chip' data-lo='{lo}' data-hi='{hi}'>{lo}&ndash;{hi}</button>"
        for lo, hi in ROW_RANGES
    )
    row_checkbox_html = "".join(
        f"<label class='tref-colcheck'><input type='checkbox' checked data-template='{n}'>{n}</label>"
        for n in range(1, 41)
    )

    return f"""<section id="template-reference-section">
      <h2>Template Reference (all 40)</h2>
      <div class="tref-toolbar">
        <span class="tref-toolbar-label">Columns</span>
        <div class="tref-chips" id="trefGroupChips">{group_chip_html}</div>
        <button type='button' class='tref-chip tref-reset' id="trefResetCols">Show all columns</button>
        <button type='button' class='tref-chip tref-reset' id="trefHideCols">Hide all columns</button>
        <details class="tref-coldetails">
          <summary>Individual columns</summary>
          <div class="tref-colgrid" id="trefColChecks">{col_checkbox_html}</div>
        </details>
      </div>
      <div class="tref-toolbar">
        <span class="tref-toolbar-label">Rows</span>
        <div class="tref-chips" id="trefRowChips">{row_chip_html}</div>
        <button type='button' class='tref-chip tref-reset' id="trefResetRows">Show all rows</button>
        <button type='button' class='tref-chip tref-reset' id="trefHideRows">Hide all rows</button>
        <details class="tref-coldetails">
          <summary>Individual rows</summary>
          <div class="tref-colgrid" id="trefRowChecks">{row_checkbox_html}</div>
        </details>
      </div>
      <div class="tablewrap">
        <table class="tref">
          <thead>
            <tr>{''.join(group_header_cells)}</tr>
            <tr>{''.join(leaf_header_cells)}</tr>
          </thead>
          <tbody>{''.join(html_rows)}</tbody>
        </table>
      </div>
      <div class="hint">{hint}</div>
      <script>
      (function() {{
        var table = document.querySelector('#template-reference-section table.tref');
        if (!table) return;
        var tbody = table.querySelector('tbody');
        var headers = table.querySelectorAll('th[data-col]');
        var sortState = {{ col: null, dir: 1 }};

        // Sticky two-row header offset is handled entirely in CSS now
        // (row 1 has a fixed height, row 2's sticky `top` references the
        // same --tref-r1h variable), so there's no JS measurement to go
        // stale on collapse/expand, font load, or first scroll.

        headers.forEach(function(th) {{
          th.addEventListener('click', function() {{
            var col = Number(th.getAttribute('data-col'));
            var dir = sortState.col === col ? -sortState.dir : 1;
            sortState = {{ col: col, dir: dir }};

            var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
            rows.sort(function(a, b) {{
              var av = (a.children[col] && a.children[col].textContent.trim()) || '';
              var bv = (b.children[col] && b.children[col].textContent.trim()) || '';
              var an = parseFloat(av), bn = parseFloat(bv);
              var cmp = (!isNaN(an) && !isNaN(bn)) ? (an - bn) : av.localeCompare(bv);
              return cmp * dir;
            }});
            rows.forEach(function(tr) {{ tbody.appendChild(tr); }});

            headers.forEach(function(h) {{ h.removeAttribute('data-sort'); }});
            th.setAttribute('data-sort', dir === 1 ? 'asc' : 'desc');
          }});
        }});

        // ---- Column hide/show (per-group chips + individual checkboxes) ----
        var STORAGE_KEY = 'trefHiddenCols_v1';
        var hidden = {{}};
        try {{
          var saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}');
          if (saved && typeof saved === 'object') hidden = saved;
        }} catch (e) {{}}

        var groupChips = document.querySelectorAll('#trefGroupChips .tref-chip');
        var colChecks = document.querySelectorAll('#trefColChecks input[data-col]');
        var groupHeaderEls = table.querySelectorAll('th.grp[data-group]');

        function colsForGroup(g) {{
          var el = table.querySelector("th.grp[data-group='" + g + "']");
          if (!el) return [];
          return (el.getAttribute('data-cols') || '').split(',').filter(Boolean).map(Number);
        }}

        function persist() {{
          try {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(hidden)); }} catch (e) {{}}
        }}

        function applyVisibility() {{
          var visibleCols = 0;
          headers.forEach(function(th) {{
            var col = Number(th.getAttribute('data-col'));
            var show = !hidden[col];
            th.style.display = show ? '' : 'none';
            if (show) visibleCols++;
          }});
          table.querySelectorAll('tbody td[data-col]').forEach(function(td) {{
            var col = Number(td.getAttribute('data-col'));
            td.style.display = hidden[col] ? 'none' : '';
          }});
          groupHeaderEls.forEach(function(gth) {{
            var cols = (gth.getAttribute('data-cols') || '').split(',').filter(Boolean).map(Number);
            var visible = cols.filter(function(c) {{ return !hidden[c]; }});
            if (visible.length === 0) {{
              gth.style.display = 'none';
            }} else {{
              gth.style.display = '';
              gth.setAttribute('colspan', visible.length);
            }}
          }});

          groupChips.forEach(function(chip) {{
            var cols = colsForGroup(chip.getAttribute('data-group'));
            var allHidden = cols.length > 0 && cols.every(function(c) {{ return hidden[c]; }});
            chip.setAttribute('data-active', allHidden ? 'false' : 'true');
          }});
          colChecks.forEach(function(cb) {{
            cb.checked = !hidden[Number(cb.getAttribute('data-col'))];
          }});
        }}

        groupChips.forEach(function(chip) {{
          chip.addEventListener('click', function() {{
            var cols = colsForGroup(chip.getAttribute('data-group'));
            var allHidden = cols.length > 0 && cols.every(function(c) {{ return hidden[c]; }});
            cols.forEach(function(c) {{
              if (allHidden) delete hidden[c]; else hidden[c] = true;
            }});
            persist();
            applyVisibility();
          }});
        }});

        colChecks.forEach(function(cb) {{
          cb.addEventListener('change', function() {{
            var col = Number(cb.getAttribute('data-col'));
            if (cb.checked) delete hidden[col]; else hidden[col] = true;
            persist();
            applyVisibility();
          }});
        }});

        var resetBtn = document.getElementById('trefResetCols');
        if (resetBtn) {{
          resetBtn.addEventListener('click', function() {{
            hidden = {{}};
            persist();
            applyVisibility();
          }});
        }}

        var hideBtn = document.getElementById('trefHideCols');
        if (hideBtn) {{
          hideBtn.addEventListener('click', function() {{
            colChecks.forEach(function(cb) {{
              hidden[Number(cb.getAttribute('data-col'))] = true;
            }});
            persist();
            applyVisibility();
          }});
        }}

        applyVisibility();

        // ---- Row hide/show (per-range chips + individual checkboxes) ----
        var ROW_STORAGE_KEY = 'trefHiddenRows_v1';
        var hiddenRows = {{}};
        try {{
          var savedRows = JSON.parse(localStorage.getItem(ROW_STORAGE_KEY) || '{{}}');
          if (savedRows && typeof savedRows === 'object') hiddenRows = savedRows;
        }} catch (e) {{}}

        var rowChips = document.querySelectorAll('#trefRowChips .tref-chip');
        var rowChecks = document.querySelectorAll('#trefRowChecks input[data-template]');
        var bodyRows = table.querySelectorAll('tbody tr[data-template]');

        function templatesInRange(lo, hi) {{
          var out = [];
          for (var n = lo; n <= hi; n++) out.push(n);
          return out;
        }}

        function persistRows() {{
          try {{ localStorage.setItem(ROW_STORAGE_KEY, JSON.stringify(hiddenRows)); }} catch (e) {{}}
        }}

        function applyRowVisibility() {{
          bodyRows.forEach(function(tr) {{
            var t = Number(tr.getAttribute('data-template'));
            tr.style.display = hiddenRows[t] ? 'none' : '';
          }});
          rowChips.forEach(function(chip) {{
            var lo = Number(chip.getAttribute('data-lo'));
            var hi = Number(chip.getAttribute('data-hi'));
            var nums = templatesInRange(lo, hi);
            var allHidden = nums.length > 0 && nums.every(function(n) {{ return hiddenRows[n]; }});
            chip.setAttribute('data-active', allHidden ? 'false' : 'true');
          }});
          rowChecks.forEach(function(cb) {{
            cb.checked = !hiddenRows[Number(cb.getAttribute('data-template'))];
          }});
        }}

        rowChips.forEach(function(chip) {{
          chip.addEventListener('click', function() {{
            var lo = Number(chip.getAttribute('data-lo'));
            var hi = Number(chip.getAttribute('data-hi'));
            var nums = templatesInRange(lo, hi);
            var allHidden = nums.length > 0 && nums.every(function(n) {{ return hiddenRows[n]; }});
            nums.forEach(function(n) {{
              if (allHidden) delete hiddenRows[n]; else hiddenRows[n] = true;
            }});
            persistRows();
            applyRowVisibility();
          }});
        }});

        rowChecks.forEach(function(cb) {{
          cb.addEventListener('change', function() {{
            var t = Number(cb.getAttribute('data-template'));
            if (cb.checked) delete hiddenRows[t]; else hiddenRows[t] = true;
            persistRows();
            applyRowVisibility();
          }});
        }});

        var resetRowsBtn = document.getElementById('trefResetRows');
        if (resetRowsBtn) {{
          resetRowsBtn.addEventListener('click', function() {{
            hiddenRows = {{}};
            persistRows();
            applyRowVisibility();
          }});
        }}

        var hideRowsBtn = document.getElementById('trefHideRows');
        if (hideRowsBtn) {{
          hideRowsBtn.addEventListener('click', function() {{
            rowChecks.forEach(function(cb) {{
              hiddenRows[Number(cb.getAttribute('data-template'))] = true;
            }});
            persistRows();
            applyRowVisibility();
          }});
        }}

        applyRowVisibility();
      }})();
      </script>
    </section>"""


def render_indicator_curves_section() -> str:
    reference = load_template_reference()
    templates = (reference or {}).get("templates") or []

    if not templates:
        return (
            "<section><h2>BB Envelopes (NQ)</h2>"
            "<div class='hint'>Waiting for temalimit.cs to export its template reference "
            "(happens once per NinjaTrader process, on strategy load).</div>"
            "</section>"
        )

    # Per-template Bollinger config: length + stddev multiplier. The chart pulls
    # real recent NQ 1-min bars (same ChartDataExporter source as the 8766 trade
    # charts) and draws each template's upper/lower band over the actual price.
    tmpl_bb = [
        {
            "n": row.get("template"),
            "len": row.get("bbLength"),
            "sd": row.get("bbStdDev"),
            "temaLen": row.get("temaLength"),
        }
        for row in templates
        if row.get("template") is not None
        and row.get("bbLength") is not None
        and row.get("bbStdDev") is not None
        and row.get("temaLength") is not None
    ]

    band_chips = [("upper", "Upper", True), ("middle", "Middle", False), ("lower", "Lower", True), ("tema", "TEMA", True)]
    band_chip_html = "".join(
        f"<button type='button' class='tref-chip ind-band-chip' data-band='{b}' data-active='{'true' if on else 'false'}'>{label}</button>"
        for b, label, on in band_chips
    )

    ROW_RANGES = [(1, 5), (6, 10), (11, 15), (16, 21), (22, 27), (28, 32), (33, 35), (36, 40)]
    range_chip_html = "".join(
        f"<button type='button' class='tref-chip' data-lo='{lo}' data-hi='{hi}'>{lo}&ndash;{hi}</button>"
        for lo, hi in ROW_RANGES
    )
    tmpl_checkbox_html = "".join(
        f"<label class='tref-colcheck'><input type='checkbox' checked data-template='{n}'>{n}</label>"
        for n in range(1, 41)
    )

    tmpl_json = json.dumps(tmpl_bb)

    return f"""<section>
      <h2>BB Envelopes (NQ)</h2>
      <div class="hint" style="margin-top:0;">Each template's Bollinger band and TEMA (dotted) drawn over the last 3h of
        real NQ price, resampled to 5-minute bars (same source as the trade charts, no extra NinjaTrader load).
        Wider band = higher BBStdDev; TEMA uses each template's own TemaLen. Color runs template 1 (blue) &rarr; 40 (red).
        Drag on the chart to zoom into a rectangle, like NinjaTrader; double-click to reset.</div>
      <div class="tref-toolbar">
        <span class="tref-toolbar-label">Bands</span>
        <div class="tref-chips" id="indBandChips">{band_chip_html}</div>
        <span id="indGradLegend" style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)">
          <span>T1</span><span style="width:80px;height:8px;border-radius:4px;background:linear-gradient(90deg,hsl(210,70%,58%),hsl(150,65%,52%),hsl(45,80%,55%),hsl(0,72%,58%))"></span><span>T40</span>
        </span>
      </div>
      <div class="tref-toolbar">
        <span class="tref-toolbar-label">Templates</span>
        <div class="tref-chips" id="indRangeChips">{range_chip_html}</div>
        <button type='button' class='tref-chip tref-reset' id="indResetRows">Show all templates</button>
        <button type='button' class='tref-chip tref-reset' id="indResetZoom">Reset zoom</button>
        <details class="tref-coldetails">
          <summary>Individual templates</summary>
          <div class="tref-colgrid" id="indTmplChecks">{tmpl_checkbox_html}</div>
        </details>
      </div>
      <div class="ind-chartwrap">
        <svg id="indChart" viewBox="0 0 900 360" preserveAspectRatio="none" style="width:100%;height:360px;display:block;cursor:crosshair"></svg>
        <div id="indStatus" class="ind-status">Loading NQ price&hellip;</div>
        <div id="indTooltip" class="ind-tooltip" style="display:none"></div>
      </div>
      <script>
      (function() {{
        var templates = {tmpl_json};
        var svg = document.getElementById('indChart');
        var statusEl = document.getElementById('indStatus');
        var tooltip = document.getElementById('indTooltip');
        if (!svg) return;
        var NS = 'http://www.w3.org/2000/svg';
        var W = 900, H = 360, padL = 16, padR = 58, padT = 14, padB = 30;
        var plotW = W - padL - padR, plotH = H - padT - padB;

        var BAND_KEY = 'indBands_v1', ROW_KEY = 'indHiddenTemplates_v1';
        var hiddenBands = {{}}, hiddenRows = {{}};
        try {{ hiddenBands = JSON.parse(localStorage.getItem(BAND_KEY) || 'null') || {{ middle: true }}; }} catch (e) {{ hiddenBands = {{ middle: true }}; }}
        try {{ hiddenRows = JSON.parse(localStorage.getItem(ROW_KEY) || '{{}}') || {{}}; }} catch (e) {{}}

        var tNums = templates.map(function(t) {{ return t.n; }});
        var tMin = Math.min.apply(null, tNums), tMax = Math.max.apply(null, tNums);
        function colorFor(n) {{
          var f = tMax === tMin ? 0 : (n - tMin) / (tMax - tMin);
          return 'hsl(' + Math.round(210 - f * 210) + ',70%,58%)';
        }}

        function computeBB(closes, period, mult) {{
          var upper = [], middle = [], lower = [];
          for (var i = 0; i < closes.length; i++) {{
            if (i < period - 1) {{ upper.push(null); middle.push(null); lower.push(null); continue; }}
            var sum = 0;
            for (var j = i - period + 1; j <= i; j++) sum += closes[j];
            var mean = sum / period, varc = 0;
            for (var k = i - period + 1; k <= i; k++) varc += (closes[k] - mean) * (closes[k] - mean);
            var sd = Math.sqrt(varc / period);
            middle.push(mean); upper.push(mean + mult * sd); lower.push(mean - mult * sd);
          }}
          return {{ upper: upper, middle: middle, lower: lower }};
        }}

        // Non-seeded EMA/TEMA -- fine for a visual shape comparison, not meant
        // to bit-match NinjaTrader's warmed-up TEMA exactly.
        function computeEma(values, period) {{
          var k = 2 / (period + 1), out = [], prev = null;
          for (var i = 0; i < values.length; i++) {{
            prev = prev === null ? values[i] : values[i] * k + prev * (1 - k);
            out.push(prev);
          }}
          return out;
        }}
        function computeTema(values, period) {{
          var e1 = computeEma(values, period), e2 = computeEma(e1, period), e3 = computeEma(e2, period);
          var out = [];
          for (var i = 0; i < values.length; i++) out.push(3 * e1[i] - 3 * e2[i] + e3[i]);
          return out;
        }}

        // Aggregates the fetched 1-minute bars into N-minute buckets client-side
        // -- reuses the exact same fetch/data, so this adds zero NinjaTrader or
        // server load regardless of the bucket size chosen.
        function resampleBars(raw, bucketMin) {{
          var bucketMs = bucketMin * 60000, out = [], cur = null, curKey = null;
          raw.forEach(function(b) {{
            var ms = Date.parse(b.t);
            if (isNaN(ms)) return;
            var key = Math.floor(ms / bucketMs);
            if (key !== curKey) {{
              if (cur) out.push(cur);
              cur = {{ t: b.t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v || 0 }};
              curKey = key;
            }} else {{
              if (b.h > cur.h) cur.h = b.h;
              if (b.l < cur.l) cur.l = b.l;
              cur.c = b.c;
              cur.v = (cur.v || 0) + (b.v || 0);
            }}
          }});
          if (cur) out.push(cur);
          return out;
        }}

        var bars = null, closes = null, yMin = 0, yMax = 1;
        var viewLo = 0, viewHi = 0, manualZoom = false;
        function xForIdx(i) {{
          var span = viewHi - viewLo;
          return padL + (span <= 0 ? 0 : (i - viewLo) / span) * plotW;
        }}
        function yForPrice(p) {{ return padT + (yMax === yMin ? 0.5 : (1 - (p - yMin) / (yMax - yMin))) * plotH; }}

        function prepare() {{
          closes = bars.map(function(b) {{ return b.c; }});
          templates.forEach(function(t) {{
            t.bb = computeBB(closes, t.len, t.sd);
            t.tema = computeTema(closes, t.temaLen);
          }});
          viewLo = 0; viewHi = closes.length - 1; manualZoom = false;
        }}

        function autoYRange(loIdx, hiIdx) {{
          var lo = Infinity, hi = -Infinity;
          for (var i = loIdx; i <= hiIdx; i++) {{ if (closes[i] < lo) lo = closes[i]; if (closes[i] > hi) hi = closes[i]; }}
          templates.forEach(function(t) {{
            if (hiddenRows[t.n]) return;
            for (var i = loIdx; i <= hiIdx; i++) {{
              if (!hiddenBands.upper && t.bb.upper[i] != null && t.bb.upper[i] > hi) hi = t.bb.upper[i];
              if (!hiddenBands.lower && t.bb.lower[i] != null && t.bb.lower[i] < lo) lo = t.bb.lower[i];
              if (!hiddenBands.tema && t.tema[i] != null) {{
                if (t.tema[i] > hi) hi = t.tema[i];
                if (t.tema[i] < lo) lo = t.tema[i];
              }}
            }}
          }});
          var pad = (hi - lo) * 0.04 || 1;
          return [lo - pad, hi + pad];
        }}

        function polyFor(arr, color, width, opacity, dashed) {{
          var pts = [];
          for (var i = viewLo; i <= viewHi; i++) if (arr[i] != null) pts.push(xForIdx(i) + ',' + yForPrice(arr[i]));
          if (!pts.length) return null;
          var pl = document.createElementNS(NS, 'polyline');
          pl.setAttribute('points', pts.join(' ')); pl.setAttribute('fill', 'none');
          pl.setAttribute('stroke', color); pl.setAttribute('stroke-width', width);
          pl.setAttribute('stroke-opacity', opacity); pl.setAttribute('stroke-linejoin', 'round');
          if (dashed) pl.setAttribute('stroke-dasharray', '3,3');
          return pl;
        }}

        function draw() {{
          while (svg.firstChild) svg.removeChild(svg.firstChild);
          if (!bars) return;
          if (!manualZoom) {{ var r = autoYRange(viewLo, viewHi); yMin = r[0]; yMax = r[1]; }}
          // y gridlines + price labels, on the RIGHT
          for (var g = 0; g <= 4; g++) {{
            var p = yMin + (yMax - yMin) * g / 4, y = yForPrice(p);
            var ln = document.createElementNS(NS, 'line');
            ln.setAttribute('x1', padL); ln.setAttribute('x2', padL + plotW);
            ln.setAttribute('y1', y); ln.setAttribute('y2', y);
            ln.setAttribute('stroke', 'rgba(255,255,255,0.07)'); svg.appendChild(ln);
            var tx = document.createElementNS(NS, 'text');
            tx.setAttribute('x', padL + plotW + 8); tx.setAttribute('y', y + 3); tx.setAttribute('text-anchor', 'start');
            tx.setAttribute('fill', 'var(--muted)'); tx.setAttribute('font-size', '10');
            tx.textContent = p.toFixed(1); svg.appendChild(tx);
          }}
          // time x labels (first / mid / last of the current view window)
          [viewLo, Math.floor((viewLo + viewHi) / 2), viewHi].forEach(function(i) {{
            if (i < 0) return;
            var tx = document.createElementNS(NS, 'text');
            tx.setAttribute('x', xForIdx(i)); tx.setAttribute('y', H - 8); tx.setAttribute('text-anchor', i === viewLo ? 'start' : (i === viewHi ? 'end' : 'middle'));
            tx.setAttribute('fill', 'var(--muted)'); tx.setAttribute('font-size', '10');
            var ts = String(bars[i].t || ''); var m = ts.match(/(\\d{{2}}:\\d{{2}})/);
            tx.textContent = m ? m[1] : ts.slice(0, 16); svg.appendChild(tx);
          }});
          // per-template bands + TEMA
          templates.forEach(function(t) {{
            if (hiddenRows[t.n]) return;
            var c = colorFor(t.n);
            if (!hiddenBands.middle) {{ var m = polyFor(t.bb.middle, c, '1', '0.30', false); if (m) svg.appendChild(m); }}
            if (!hiddenBands.upper) {{ var u = polyFor(t.bb.upper, c, '1.2', '0.55', false); if (u) svg.appendChild(u); }}
            if (!hiddenBands.lower) {{ var lo2 = polyFor(t.bb.lower, c, '1.2', '0.55', false); if (lo2) svg.appendChild(lo2); }}
            if (!hiddenBands.tema) {{ var tm = polyFor(t.tema, c, '1.1', '0.75', true); if (tm) svg.appendChild(tm); }}
          }});
          // price line on top
          var price = polyFor(closes, '#eef0f7', '2', '1', false); if (price) svg.appendChild(price);
        }}

        function svgPoint(ev) {{
          var rect = svg.getBoundingClientRect();
          return {{ x: (ev.clientX - rect.left) / rect.width * W, y: (ev.clientY - rect.top) / rect.height * H }};
        }}

        function idxForClientX(clientX) {{
          var rect = svg.getBoundingClientRect();
          var xInSvg = (clientX - rect.left) / rect.width * W;
          var idx = Math.round(viewLo + (xInSvg - padL) / plotW * (viewHi - viewLo));
          return Math.max(viewLo, Math.min(viewHi, idx));
        }}

        function showTooltip(ev) {{
          var idx = idxForClientX(ev.clientX);
          var vis = templates.filter(function(t) {{ return !hiddenRows[t.n]; }});
          var widths = vis.map(function(t) {{
            var u = t.bb.upper[idx], l = t.bb.lower[idx];
            return (u != null && l != null) ? {{ n: t.n, w: u - l }} : null;
          }}).filter(Boolean).sort(function(a, b) {{ return a.w - b.w; }});
          var span = '';
          if (widths.length) {{
            var tight = widths[0], wide = widths[widths.length - 1];
            span = "<div>Tightest: T" + tight.n + " &plusmn;" + (tight.w / 2).toFixed(1) + "</div>"
                 + "<div>Widest: T" + wide.n + " &plusmn;" + (wide.w / 2).toFixed(1) + "</div>";
          }}
          var ts = String(bars[idx].t || ''); var m = ts.match(/(\\d{{2}}:\\d{{2}})/);
          tooltip.innerHTML = "<div style='font-weight:700;margin-bottom:4px'>" + (m ? m[1] : '') + "  &middot;  " + closes[idx].toFixed(1) + "</div>" + span
            + "<div style='color:var(--muted);margin-top:3px'>" + vis.length + " templates shown</div>";
          tooltip.style.display = 'block';
          var wrapRect = svg.parentNode.getBoundingClientRect();
          var lx = ev.clientX - wrapRect.left + 14;
          if (lx + 170 > wrapRect.width) lx = ev.clientX - wrapRect.left - 182;
          tooltip.style.left = lx + 'px';
          tooltip.style.top = (ev.clientY - wrapRect.top + 8) + 'px';
        }}

        // ---- Drag-rectangle zoom, like NinjaTrader: hold + drag to select a
        // box, release to zoom into it (both time and price axes); dblclick
        // or "Reset zoom" restores the full view. ----
        var dragging = false, dragStartX = 0, dragStartY = 0, selRect = null;

        svg.addEventListener('mousedown', function(ev) {{
          if (!bars) return;
          var p = svgPoint(ev);
          dragging = true; dragStartX = p.x; dragStartY = p.y;
          selRect = document.createElementNS(NS, 'rect');
          selRect.setAttribute('fill', 'rgba(124,92,255,0.15)');
          selRect.setAttribute('stroke', 'var(--accent)'); selRect.setAttribute('stroke-width', '1');
          selRect.setAttribute('stroke-dasharray', '4,3');
          selRect.setAttribute('x', p.x); selRect.setAttribute('y', p.y);
          selRect.setAttribute('width', 0); selRect.setAttribute('height', 0);
          svg.appendChild(selRect);
          tooltip.style.display = 'none';
        }});

        svg.addEventListener('mousemove', function(ev) {{
          if (!bars) return;
          if (dragging && selRect) {{
            var p = svgPoint(ev);
            selRect.setAttribute('x', Math.min(dragStartX, p.x)); selRect.setAttribute('y', Math.min(dragStartY, p.y));
            selRect.setAttribute('width', Math.abs(p.x - dragStartX)); selRect.setAttribute('height', Math.abs(p.y - dragStartY));
            return;
          }}
          showTooltip(ev);
        }});
        svg.addEventListener('mouseleave', function() {{ if (!dragging) tooltip.style.display = 'none'; }});

        window.addEventListener('mouseup', function(ev) {{
          if (!dragging) return;
          dragging = false;
          var p = svgPoint(ev);
          if (selRect) {{ svg.removeChild(selRect); selRect = null; }}
          var x0 = Math.max(padL, Math.min(dragStartX, p.x)), x1 = Math.min(padL + plotW, Math.max(dragStartX, p.x));
          var y0 = Math.max(padT, Math.min(dragStartY, p.y)), y1 = Math.min(padT + plotH, Math.max(dragStartY, p.y));
          if ((x1 - x0) < 8 || (y1 - y0) < 8) return;  // too small -- treat as a click, not a zoom
          var span = viewHi - viewLo;
          var idx0 = viewLo + (x0 - padL) / plotW * span, idx1 = viewLo + (x1 - padL) / plotW * span;
          var newLo = Math.max(0, Math.round(Math.min(idx0, idx1))), newHi = Math.min(closes.length - 1, Math.round(Math.max(idx0, idx1)));
          if (newHi - newLo < 1) return;
          var price1 = yMin + (yMax - yMin) * (1 - (y0 - padT) / plotH);
          var price0 = yMin + (yMax - yMin) * (1 - (y1 - padT) / plotH);
          viewLo = newLo; viewHi = newHi;
          yMin = Math.min(price0, price1); yMax = Math.max(price0, price1);
          manualZoom = true;
          draw();
        }});

        function resetZoom() {{
          if (!bars) return;
          viewLo = 0; viewHi = closes.length - 1; manualZoom = false;
          draw();
        }}
        svg.addEventListener('dblclick', resetZoom);
        var zoomBtn = document.getElementById('indResetZoom');
        if (zoomBtn) zoomBtn.addEventListener('click', resetZoom);

        // ---- Band toggles ----
        var bandChips = document.querySelectorAll('#indBandChips .ind-band-chip');
        function persistBands() {{ try {{ localStorage.setItem(BAND_KEY, JSON.stringify(hiddenBands)); }} catch (e) {{}} }}
        function applyBandChips() {{
          bandChips.forEach(function(c) {{ c.setAttribute('data-active', hiddenBands[c.getAttribute('data-band')] ? 'false' : 'true'); }});
        }}
        bandChips.forEach(function(chip) {{
          chip.addEventListener('click', function() {{
            var b = chip.getAttribute('data-band');
            if (hiddenBands[b]) delete hiddenBands[b]; else hiddenBands[b] = true;
            persistBands(); applyBandChips(); draw();
          }});
        }});

        // ---- Template toggles: range chips + individual + reset ----
        var rangeChips = document.querySelectorAll('#indRangeChips .tref-chip');
        var tmplChecks = document.querySelectorAll('#indTmplChecks input[data-template]');
        function persistRows() {{ try {{ localStorage.setItem(ROW_KEY, JSON.stringify(hiddenRows)); }} catch (e) {{}} }}
        function nums(lo, hi) {{ var a = []; for (var n = lo; n <= hi; n++) a.push(n); return a; }}
        function applyRowUI() {{
          rangeChips.forEach(function(chip) {{
            var ns = nums(Number(chip.getAttribute('data-lo')), Number(chip.getAttribute('data-hi')));
            chip.setAttribute('data-active', ns.every(function(n) {{ return hiddenRows[n]; }}) ? 'false' : 'true');
          }});
          tmplChecks.forEach(function(cb) {{ cb.checked = !hiddenRows[Number(cb.getAttribute('data-template'))]; }});
        }}
        rangeChips.forEach(function(chip) {{
          chip.addEventListener('click', function() {{
            var ns = nums(Number(chip.getAttribute('data-lo')), Number(chip.getAttribute('data-hi')));
            var allHidden = ns.every(function(n) {{ return hiddenRows[n]; }});
            ns.forEach(function(n) {{ if (allHidden) delete hiddenRows[n]; else hiddenRows[n] = true; }});
            persistRows(); applyRowUI(); draw();
          }});
        }});
        tmplChecks.forEach(function(cb) {{
          cb.addEventListener('change', function() {{
            var t = Number(cb.getAttribute('data-template'));
            if (cb.checked) delete hiddenRows[t]; else hiddenRows[t] = true;
            persistRows(); applyRowUI(); draw();
          }});
        }});
        var resetBtn = document.getElementById('indResetRows');
        if (resetBtn) resetBtn.addEventListener('click', function() {{ hiddenRows = {{}}; persistRows(); applyRowUI(); draw(); }});

        applyBandChips();
        applyRowUI();

        // ---- Fetch NQ bars via the shared bridge, polling until ready ----
        var attempts = 0, MAX_ATTEMPTS = 24;
        function loadBars() {{
          fetch('/api/nq-bars', {{ cache: 'no-store' }}).then(function(r) {{ return r.json(); }}).then(function(res) {{
            if (res.status === 'ready') {{
              if (!res.bars || !res.bars.length) {{ statusEl.textContent = 'No NQ bars returned for the last 3h window.'; return; }}
              var oneMin = res.bars.slice().sort(function(a, b) {{ return String(a.t).localeCompare(String(b.t)); }});
              bars = resampleBars(oneMin, 5);
              prepare(); statusEl.style.display = 'none'; draw();
            }} else if (res.status === 'error') {{
              statusEl.textContent = 'Chart data error: ' + (res.error || 'unknown') + '. Is NinjaTrader running with ChartDataExporter compiled?';
            }} else {{
              attempts++;
              if (attempts > MAX_ATTEMPTS) {{ statusEl.textContent = 'Still waiting on NinjaTrader for NQ price data. Make sure NinjaTrader is running with the ChartDataExporter AddOn.'; return; }}
              statusEl.textContent = 'Loading NQ price\\u2026 (waiting on NinjaTrader, attempt ' + attempts + ')';
              setTimeout(loadBars, 2500);
            }}
          }}).catch(function(err) {{
            statusEl.textContent = 'Failed to load NQ bars: ' + err.message;
          }});
        }}
        loadBars();
      }})();
      </script>
    </section>"""


def render_pullback_by_symbol_section() -> str:
    reference = load_template_reference()
    templates = (reference or {}).get("templates") or []
    tickers = (reference or {}).get("tickers") or ["ES", "NQ", "YM", "RTY"]

    if not templates:
        return (
            "<section><h2>Pullback by Symbol</h2>"
            "<div class='hint'>Waiting for temalimit.cs to export its template reference "
            "(happens once per NinjaTrader process, on strategy load).</div>"
            "</section>"
        )

    picker_data = [
        {
            "template": row.get("template"),
            "selectivity": row.get("selectivity"),
            "pullback": row.get("pullbackTicksByTicker") or {},
        }
        for row in templates
    ]
    default_template = picker_data[0]["template"]

    chips = "".join(
        f"<button type='button' class='tpl-chip' data-template='{escape(str(row['template']))}' "
        f"data-active='{'true' if row['template'] == default_template else 'false'}'>"
        f"#{escape(str(row['template']))}</button>"
        for row in picker_data
    )

    tile_shells = "".join(
        f"<div class='pullback-tile' data-ticker='{escape(t)}'>"
        f"<div class='sym'>{escape(t)}</div>"
        f"<div class='val'>&ndash;<span class='unit'>ticks</span></div>"
        "</div>"
        for t in tickers
    )

    data_json = json.dumps(picker_data)

    return f"""<section>
      <h2>Pullback by Symbol</h2>
      <div class="hint" style="margin-top:0;">Each symbol pulls back a different number of
        ticks off the same template. Pick a template to see what each symbol actually uses.</div>
      <div class="tpl-chips" id="pbChips">{chips}</div>
      <div class="pullback-tiles" id="pbTiles">{tile_shells}</div>
      <div class="hint">Each symbol scales this template's own base pullback by its own
        multiplier; full numeric breakdown is in Template Reference below.</div>
      <div class="hint">Static baseline formula: tableTicks = round(max(1, (38 + templateNumber)
        &times; tickerMultiplier)), tickerMultiplier: ES 0.55, RTY 0.45, YM 0.40, NQ 1.00 --
        i.e. the base pullback simply climbs 1 tick per template (T1=39 .. T40=78) before the
        per-symbol multiplier is applied.</div>
      <div class="hint">Live trading then flexes that baseline +/-50% by current volatility:
        pullbackTicks = round(max(1, tableTicks &times; clamp(ATR(14) / SMA(ATR(14), 20), 0.5,
        1.5))). See the completed-trades log's pullbackAtr / pullbackAtrAvg / pullbackAtrRatio
        columns for what was actually applied on a given trade.</div>
      <script>
      (function() {{
        var data = {data_json};
        var byTemplate = {{}};
        data.forEach(function(row) {{ byTemplate[row.template] = row; }});

        var chips = document.querySelectorAll('#pbChips .tpl-chip');
        var tiles = document.querySelectorAll('#pbTiles .pullback-tile');

        function render(templateNum) {{
          var row = byTemplate[templateNum];
          if (!row) return;
          tiles.forEach(function(tile) {{
            var ticker = tile.getAttribute('data-ticker');
            var val = row.pullback[ticker];
            tile.querySelector('.val').innerHTML = (val === undefined ? '&ndash;' : val) + "<span class='unit'>ticks</span>";
          }});
          chips.forEach(function(chip) {{
            chip.setAttribute('data-active', String(Number(chip.getAttribute('data-template')) === Number(templateNum)));
          }});
        }}

        chips.forEach(function(chip) {{
          chip.addEventListener('click', function() {{
            render(chip.getAttribute('data-template'));
          }});
        }});

        render({default_template});
      }})();
      </script>
    </section>"""


def render_recent_table(rows: List[Dict[str, Any]]) -> str:
    html_rows = []
    for row in reversed(rows):
        metadata = row.get("metadata") or {}
        setup_direction = str(metadata.get("setup_direction") or "")
        ml_direction = str(metadata.get("ml_direction") or metadata.get("prediction") or "")
        ml_signal = str(metadata.get("ml_signal") or "")
        ml_reversal_value = metadata.get("ml_reversal")
        ml_reversal = "YES" if ml_reversal_value is True else ("NO" if ml_reversal_value is False else "")
        source = row_source(row)
        source_cell = source + (f" T{metadata.get('template_number')}" if source == "shadow" and metadata.get("template_number") is not None else "")
        html_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('timestamp') or row.get('logged_at') or ''))}</td>"
            f"<td>{escape(source_cell)}</td>"
            f"<td>{escape(str(row.get('symbol') or ''))}</td>"
            f"<td>{escape(str(metadata.get('bars_period') or ''))}</td>"
            f"<td>{escape(str(metadata.get('setup_source') or row.get('trigger') or ''))}</td>"
            f"<td>{escape(setup_direction.upper())}</td>"
            f"<td>{escape(ml_direction.upper())}</td>"
            f"<td>{escape(ml_signal)}</td>"
            f"<td>{escape(ml_reversal)}</td>"
            f"<td><span class='pill {escape(str(row.get('label') or ''))}'>{escape(str(row.get('label') or ''))}</span></td>"
            f"<td class='num'>{escape(str(metadata.get('confidence') or ''))}</td>"
            "</tr>"
        )
    if not html_rows:
        html_rows.append("<tr><td colspan='11' class='muted'>No recent samples</td></tr>")

    return (
        "<section><h2>Recent Samples</h2>"
        "<table><thead><tr><th data-col='0'>Time</th><th data-col='1'>Source</th><th data-col='2'>Symbol</th><th data-col='3'>Data Series</th><th data-col='4'>Setup</th><th data-col='5'>Setup Dir</th><th data-col='6'>ML Dir</th><th data-col='7'>Signal</th><th data-col='8'>Reversal</th><th data-col='9'>Label</th><th data-col='10'>Confidence</th></tr></thead>"
        f"<tbody>{''.join(html_rows)}</tbody></table></section>"
    )

def dashboard_html(range_name: str = "all") -> str:
    range_name = normalize_sample_range(range_name)
    stats = sample_stats(30, range_name)
    # Sample-derived content honors the range picker; model-health/verification
    # sections are training-state snapshots and always show current state.
    range_options = [("1d", "1D"), ("2d", "2D"), ("3d", "3D"), ("5d", "5D"), ("all", "All")]
    range_parts = []
    for range_value, range_label in range_options:
        range_href = "/dashboard" if range_value == "all" else f"/dashboard?range={range_value}"
        range_cls = " class='current'" if range_value == range_name else ""
        range_parts.append(f"<a href='{range_href}'{range_cls}>{range_label}</a>")
    range_picker = "".join(range_parts)
    sources = stats.get("sources") or {}
    # Four KPI cards, secondary numbers demoted to subtext -- the old six
    # equal-weight cards buried the ones that matter.
    cards = [
        ("Samples", f"{stats['sample_count']:,}", f"{sources.get('live', 0):,} live · {sources.get('shadow', 0):,} shadow"),
        ("Entry Groups Trained", f"{stats['entry_groups_trained']} / {stats['entry_groups_known']}", "per symbol + data series"),
        ("Features", str(stats["n_features"]), f"window {WINDOW_SIZE} bars"),
        ("Entry Model Size", f"{stats['model_size_bytes'] / 1024.0:.1f} KB", ""),
    ]
    card_html = "".join(
        f"<div class='card'><div>{escape(str(label))}</div><strong>{escape(str(value))}</strong>"
        + (f"<span class='subcount'>{escape(str(sub))}</span>" if sub else "")
        + "</div>"
        for label, value, sub in cards
    )
    # Order: models first (the page's job is "is training healthy?"), then the
    # verification/ablation gates, then data-mix distributions (merged into one
    # tabbed section client-side), then diagnostics and the template power table.
    sections = [
        render_entry_model_section(),
        render_template_model_section(),
        render_active_templates_section(),
        render_exit_model_section(),
        render_verification_section(),
        render_ablation_readiness_section(),
        render_bar_table("Labels", stats["labels"]),
        render_bar_table("Symbols", stats["symbols"]),
        render_bar_table("Data Series", stats["data_series"]),
        render_bar_table("Triggers", stats["triggers"]),
        render_bar_table("Sample Sources", stats["sources"]),
        render_outcome_table(stats["symbol_data_series"]),
        render_shadow_template_table(stats["shadow_templates"]),
        render_recent_table(stats["recent"]),
        render_indicator_curves_section(),
        render_pullback_by_symbol_section(),
        render_template_risk_table(),
    ]

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tema Limit ML Dashboard</title>
  <style>
    :root {{
      /* Shared token set with the Live (8766) and Trend (8767) dashboards:
         flat near-black ground, hairline borders, semantic colors only
         (green/red/amber), one interactive accent. */
      --bg: #0b0d10;
      --bg2: #0b0d10;
      --panel: #14171c;
      --panel-solid: #14171c;
      --border: rgba(235,240,245,0.10);
      --border-soft: rgba(235,240,245,0.06);
      --text: #e8eaed;
      --muted: #8f959d;
      --accent: #4c8dff;
      --accent2: #4c8dff;
      --accent3: #8f959d;
      --win: #00c46a;
      --loss: #ff5c74;
      --warn: #e8b64c;
      --radius: 12px;
      --radius-sm: 8px;
    }}

    * {{ box-sizing: border-box; }}

    @media (prefers-reduced-motion: reduce) {{
      * {{ animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }}
    }}

    html {{ scroll-behavior: smooth; }}

    body {{
      margin: 0;
      font-family: 'Segoe UI', system-ui, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 999px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: rgba(255,255,255,0.28); }}

    h1, h2 {{ font-family: inherit; }}

    .stickytop {{
      position: sticky; top: 0; z-index: 50;
    }}
    header.top {{
      padding: 14px 22px;
      background: rgba(10,10,15,0.72);
      backdrop-filter: blur(14px) saturate(140%);
      -webkit-backdrop-filter: blur(14px) saturate(140%);
      border-bottom: 1px solid var(--border);
    }}
    .top-row {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
    .brand {{ display: flex; align-items: center; gap: 12px; }}
    .brand .dot {{
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--win);
      box-shadow: 0 0 0 0 rgba(52,229,143,0.6);
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(52,229,143,0.55); }}
      70% {{ box-shadow: 0 0 0 9px rgba(52,229,143,0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(52,229,143,0); }}
    }}
    h1 {{
      margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.01em;
      color: var(--text);
    }}
    .page-switch {{
      display: inline-flex; align-items: center; gap: 2px;
      border: 1px solid var(--border); border-radius: 8px;
      background: var(--panel); padding: 3px; margin-left: 12px;
    }}
    .page-switch a {{
      color: var(--muted); text-decoration: none; font-size: 12.5px; font-weight: 600;
      padding: 4px 12px; border-radius: 6px; white-space: nowrap;
    }}
    .page-switch a:hover {{ color: var(--text); text-decoration: none; }}
    .page-switch a.current {{ background: rgba(235,240,245,0.08); color: var(--text); }}
    .health-chip .chip-dot {{
      width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none;
    }}
    .health-chip.ok .chip-dot {{ background: var(--win); }}
    .health-chip.warn .chip-dot {{ background: var(--warn); }}
    .health-chip.bad .chip-dot {{ background: var(--loss); }}
    .overflow-menu {{ position: relative; }}
    .overflow-btn {{
      border: 1px solid var(--border); background: var(--panel); color: var(--muted);
      border-radius: 8px; padding: 5px 11px; font: inherit; font-weight: 700;
      cursor: pointer; line-height: 1;
    }}
    .overflow-btn:hover {{ color: var(--text); }}
    .overflow-pop {{
      display: none; position: absolute; right: 0; top: calc(100% + 8px); z-index: 60;
      min-width: 230px; background: var(--panel-solid); border: 1px solid var(--border);
      border-radius: 10px; padding: 6px; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    }}
    .overflow-pop.open {{ display: block; }}
    .overflow-pop a {{
      display: block; color: var(--text); font-size: 13px; padding: 8px 10px;
      border-radius: 7px; text-decoration: none;
    }}
    .overflow-pop a:hover {{ background: rgba(235,240,245,0.06); text-decoration: none; }}
    .overflow-pop .danger {{ color: var(--loss); }}
    .overflow-pop .menu-note {{
      padding: 6px 10px 8px; font-size: 11.5px; color: var(--muted);
      border-top: 1px solid var(--border-soft); margin-top: 4px; word-break: break-all;
    }}
    .attention {{
      margin-bottom: 14px; padding: 11px 14px; border-radius: var(--radius-sm);
      background: rgba(255,92,116,0.08); border: 1px solid rgba(255,92,116,0.4);
      border-left: 3px solid var(--loss); font-size: 13px;
    }}
    .attention a {{ color: var(--text); text-decoration: underline; }}
    .meta-row {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }}
    .chip {{
      display: inline-flex; align-items: center; gap: 6px;
      font-size: 12px; color: var(--muted);
      background: var(--panel); border: 1px solid var(--border);
      padding: 6px 12px; border-radius: 999px;
    }}
    .chip strong {{ color: var(--text); font-weight: 600; }}
    .muted {{ color: var(--muted); }}
    .hint {{ color: var(--muted); font-size: 12px; margin-top: 8px; }}
    a {{ color: var(--accent2); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    nav.pillnav {{
      display: flex; gap: 8px; overflow-x: auto; padding: 10px 22px 12px;
      background: rgba(10,10,15,0.88);
      backdrop-filter: blur(14px) saturate(140%);
      -webkit-backdrop-filter: blur(14px) saturate(140%);
      border-bottom: 1px solid var(--border-soft);
      scrollbar-width: thin;
    }}
    nav.pillnav a {{
      flex: 0 0 auto;
      font-size: 12.5px; font-weight: 500; color: var(--muted);
      background: var(--panel); border: 1px solid var(--border);
      padding: 7px 13px; border-radius: 999px; white-space: nowrap;
      transition: all .15s ease;
    }}
    nav.pillnav a:hover {{ color: var(--text); border-color: var(--accent); text-decoration: none; transform: translateY(-1px); }}
    nav.pillnav a.active {{ color: #0b0d10; background: var(--accent); border-color: transparent; }}

    main {{ padding: 22px 22px 60px; max-width: 1600px; margin: 0 auto; }}

    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 22px; }}
    .card {{
      position: relative; overflow: hidden;
      background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
      padding: 16px 18px;
      transition: border-color .2s ease;
    }}
    .card:hover {{ border-color: rgba(76,141,255,0.5); }}
    .card div {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    .card strong {{ display: block; font-size: 26px; margin-top: 6px; font-weight: 700; font-variant-numeric: tabular-nums; }}
    .subcount {{ color: var(--muted); font-size: 11px; font-weight: 400; margin-top: 3px; white-space: nowrap; font-variant-numeric: tabular-nums; }}
    @keyframes rise {{ to {{ opacity: 1; transform: translateY(0); }} }}

    section {{
      margin-top: 18px; scroll-margin-top: calc(var(--sticky-h, 110px) + 10px);
      background: var(--panel); border: 1px solid var(--border-soft); border-radius: var(--radius);
      padding: 4px 18px 18px;
    }}
    section h2 {{
      font-size: 15px; margin: 0; padding: 14px 0 12px;
      font-weight: 600; display: flex; align-items: center; gap: 9px;
      cursor: pointer; user-select: none; color: var(--text);
    }}
    section h2::after {{
      content: "▾"; margin-left: auto; color: var(--muted); font-size: 12px;
      transition: transform .2s ease;
    }}
    section.collapsed h2::after {{ transform: rotate(-90deg); }}
    section .sec-body {{ overflow: hidden; }}
    section.collapsed .sec-body {{ display: none; }}
    .sec-icon {{ font-size: 15px; }}

    table {{ width: 100%; border-collapse: collapse; background: var(--panel-solid); border: 1px solid var(--border-soft); border-radius: var(--radius-sm); overflow: hidden; }}
    th, td {{ padding: 9px 12px; border-bottom: 1px solid var(--border-soft); text-align: left; font-size: 13px; }}
    th {{ color: var(--muted); background: rgba(255,255,255,0.03); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em; position: sticky; top: 0; }}
    tbody tr {{ transition: background .12s ease; }}
    tbody tr:hover {{ background: rgba(124,92,255,0.08); }}
    .num {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}

    th[data-col] {{ cursor: pointer; user-select: none; }}
    th[data-col]:hover {{ color: var(--text); background: rgba(124,92,255,0.1); }}
    th[data-col]::after {{ content: ""; margin-left: 4px; color: var(--accent2); font-size: 10px; }}
    th[data-col][data-sort="asc"]::after {{ content: "\\25B2"; }}
    th[data-col][data-sort="desc"]::after {{ content: "\\25BC"; }}

    .bar {{ height: 8px; background: rgba(255,255,255,0.06); border-radius: 4px; overflow: hidden; min-width: 100px; margin-top: 4px; }}
    .bar span {{ display: block; height: 100%; background: rgba(235,240,245,0.35); transition: width .6s ease; }}

    .pill {{ padding: 3px 9px; border-radius: 999px; background: rgba(255,255,255,0.08); font-size: 12px; font-weight: 600; }}
    .pill.pill-xs {{ padding: 1px 6px; font-size: 9.5px; margin-top: 3px; display: inline-block; }}
    .win {{ color: var(--win); }}
    .loss {{ color: var(--loss); }}
    .pill.long {{ color: #06280f; background: var(--win); }}
    .pill.short {{ color: #2a0510; background: var(--loss); }}
    .pill.no_trade {{ color: #222; background: #ccc; }}

    .tablewrap {{ overflow: auto; max-height: 70vh; border-radius: var(--radius-sm); }}
    .tablewrap table {{ white-space: nowrap; }}
    /* The template reference gets a taller viewport than other tables so more
       of the 40 rows show at once, but it KEEPS an internal scroll (inherits
       overflow:auto from .tablewrap) -- that internal scroll is what lets the
       header row and the Tmpl column stay frozen while you scroll. Row
       filtering is the way to shrink it further. A page-flowing full-height
       table (max-height:none) cannot freeze its header, so it's not used. */
    #template-reference-section .tablewrap {{ max-height: 82vh; }}

    table.tref {{
      border-spacing: 0; border-collapse: separate; width: auto; min-width: 0;
      overflow: visible; border-radius: 0;
    }}
    table.tref th, table.tref td {{ padding: 6px 12px; font-size: 12px; text-align: right; }}
    /* Red is reserved for losses/failures on this dashboard -- structural
       styling (headers, read-only columns, row keys) stays neutral. */
    table.tref thead th {{ position: sticky; background: var(--panel-solid); color: var(--muted); }}
    table.tref td.readonly-col {{ color: var(--muted); }}
    /* Two stacked sticky header rows: row 1 (group labels) is pinned to a
       fixed height, and row 2's sticky `top` references the SAME variable,
       so the two can never drift or overlap regardless of collapse/expand,
       font load, or scroll timing -- no JS measurement involved. Row 1's
       group labels are single-line, so a fixed height never clips them. */
    :root {{ --tref-r1h: 30px; }}
    table.tref thead tr:first-child th {{ top: 0; z-index: 3; height: var(--tref-r1h); box-sizing: border-box; }}
    table.tref thead tr:last-child th {{ top: var(--tref-r1h); z-index: 2; }}
    table.tref th[data-col] {{ text-align: right; letter-spacing: 0; font-size: 10.5px; }}
    table.tref th.grp {{
      text-align: center; background: rgba(124,92,255,0.08);
      border-left: 1px solid var(--border-soft); font-size: 11px; letter-spacing: .03em;
    }}
    table.tref th[data-col] {{ cursor: pointer; user-select: none; white-space: nowrap; }}
    table.tref th[data-col]:hover {{ background: rgba(124,92,255,0.1); }}
    table.tref th.stacked {{ text-align: center; line-height: 1.25; padding-left: 6px; padding-right: 6px; }}
    table.tref th[data-col]::after {{ content: ""; margin-left: 4px; color: var(--accent2); font-size: 10px; }}
    table.tref th[data-col][data-sort="asc"]::after {{ content: "▲"; }}
    table.tref th[data-col][data-sort="desc"]::after {{ content: "▼"; }}
    table.tref tbody tr:nth-child(even) {{ background: rgba(255,255,255,0.015); }}
    table.tref tbody tr:nth-child(even) td:first-child {{ background: #17171f; }}
    table.tref td:nth-child(2) {{ border-right: 1px solid var(--border-soft); }}
    table.tref td.grp-start {{ border-left: 1px solid var(--border-soft); }}
    table.tref td:first-child, table.tref th[data-col='0'] {{ text-align: center; }}
    table.tref thead tr:first-child th:first-child, table.tref td:first-child {{
      position: sticky; left: 0; z-index: 1; background: var(--panel-solid);
      border-right: 1px solid var(--border-soft);
    }}
    table.tref thead tr:first-child th:first-child {{ z-index: 4; }}
    table.tref td:first-child {{ color: var(--text); font-weight: 600; }}

    .tpl-chips {{ display: flex; gap: 6px; overflow-x: auto; padding: 2px 2px 10px; scrollbar-width: thin; }}
    .tpl-chip {{
      flex: 0 0 auto; font: inherit; font-size: 12.5px; font-weight: 600; color: var(--muted);
      background: var(--panel); border: 1px solid var(--border); border-radius: 999px;
      padding: 7px 13px; cursor: pointer; transition: all .15s ease;
    }}
    .tpl-chip:hover {{ color: var(--text); border-color: var(--accent); }}
    .tpl-chip[data-active="true"] {{
      color: #0b0d10; background: var(--accent); border-color: transparent;
    }}
    .tref-toolbar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px; }}
    .tref-toolbar-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); min-width: 56px; }}
    .tref-chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tref-chip {{
      flex: 0 0 auto; font: inherit; font-size: 12px; font-weight: 600; color: var(--muted);
      background: var(--panel); border: 1px solid var(--border); border-radius: 999px;
      padding: 6px 12px; cursor: pointer; transition: all .15s ease;
    }}
    .tref-chip:hover {{ color: var(--text); border-color: var(--accent); }}
    .tref-chip[data-active="true"] {{
      color: #0b0d10; background: var(--accent); border-color: transparent;
    }}
    .tref-chip.tref-reset {{ color: var(--muted); background: transparent; border-style: dashed; }}
    .tref-chip.tref-reset:hover {{ color: var(--text); border-color: var(--accent); }}
    .tref-coldetails {{ width: 100%; }}
    .tref-coldetails summary {{ cursor: pointer; color: var(--muted); font-size: 12px; user-select: none; }}
    .tref-coldetails summary:hover {{ color: var(--text); }}
    .tref-colgrid {{
      display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 10px 2px 2px;
    }}
    .tref-colcheck {{
      display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); cursor: pointer;
    }}
    .tref-colcheck:hover {{ color: var(--text); }}
    .tref-colcheck input {{ cursor: pointer; }}
    .ind-chartwrap {{ position: relative; background: var(--panel-solid); border: 1px solid var(--border-soft); border-radius: var(--radius-sm); padding: 8px 6px 4px; margin-top: 4px; }}
    .ind-band-chip[data-active="false"] {{ opacity: .45; text-decoration: line-through; }}
    .ind-status {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); font-size: 12.5px; color: var(--muted); text-align: center; max-width: 80%; }}
    .ind-swatch {{ display: inline-block; width: 9px; height: 9px; border-radius: 2px; flex: 0 0 auto; }}
    .ind-tooltip {{
      position: absolute; pointer-events: none; z-index: 5; min-width: 148px;
      background: var(--panel-solid); border: 1px solid var(--border); border-radius: var(--radius-sm);
      padding: 8px 10px; font-size: 11.5px; color: var(--text); box-shadow: 0 6px 20px rgba(0,0,0,0.4);
    }}
    .pullback-tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 4px; }}
    .pullback-tile {{
      position: relative; background: var(--panel-solid); border: 1px solid var(--border-soft);
      border-radius: var(--radius-sm); padding: 14px 14px 12px;
    }}
    .pullback-tile .sym {{ font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
    .pullback-tile .val {{ font-size: 26px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }}
    .pullback-tile .unit {{ font-size: 12px; font-weight: 500; color: var(--muted); margin-left: 4px; }}

    .sec-desc {{
      color: var(--muted); font-size: 12.5px; line-height: 1.5; margin: 0 0 12px;
      padding: 10px 12px; background: rgba(124,92,255,0.06); border: 1px solid var(--border-soft);
      border-radius: var(--radius-sm);
    }}
    .sec-desc strong {{ color: var(--text); font-weight: 600; }}
    [data-tip] {{ cursor: help; }}
    th.th-tip .th-tip-label {{ border-bottom: 1px dotted var(--muted); }}
    /* Shared hover/tap tooltip for anything with a data-tip attribute --
       column headers (th.th-tip) and, per-row, the status pills. Rendered as
       a single fixed-position element appended to <body> (see the JS in the
       entry-model section) instead of a child of the target, because the
       model tables live inside .tablewrap (overflow:auto) and table
       (overflow:hidden) -- an absolutely-positioned bubble inside either
       would be clipped and never show. position:fixed + body-level parent
       escapes both clippers. */
    #thTip {{
      position: fixed; display: none; z-index: 100; max-width: 240px;
      background: var(--panel-solid); border: 1px solid var(--border);
      border-radius: var(--radius-sm); padding: 9px 11px;
      font-size: 11.5px; font-weight: 400; text-transform: none;
      letter-spacing: normal; line-height: 1.45; color: var(--text);
      white-space: normal; box-shadow: 0 10px 28px rgba(0,0,0,0.55);
      pointer-events: none;
    }}

    footer.fab {{
      position: fixed; right: 20px; bottom: 20px; z-index: 60;
      background: var(--panel-solid); border: 1px solid var(--border);
      color: var(--text); border-radius: 999px; padding: 10px 16px; font-size: 12px; font-weight: 600;
      box-shadow: 0 6px 20px rgba(0,0,0,0.45); cursor: pointer;
    }}

    @media (max-width: 640px) {{
      main {{ padding: 12px 12px 70px; }}
      header.top {{ padding: 10px 12px; }}
      nav.pillnav {{ padding: 8px 12px 10px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
      .card strong {{ font-size: 21px; }}
      section {{ padding: 2px 12px 12px; }}
      .page-switch {{ margin-left: 0; }}
      /* Phone: tables become stacked label/value rows (labels stamped from
         each table's own thead by the m-stack script) instead of scrolling
         sideways inside the card -- same pattern as the 8766 trade tables.
         The Template Reference power table keeps its frozen-header scroll. */
      .tablewrap {{ max-height: none; overflow: visible; }}
      #template-reference-section .tablewrap {{ overflow: auto; max-height: 82vh; }}
      .tablewrap table.m-stack {{ white-space: normal; }}
      table.m-stack thead {{ display: none; }}
      table.m-stack tbody tr {{ display: block; padding: 8px 0; border-bottom: 1px solid var(--border-soft); }}
      table.m-stack tbody tr:last-child {{ border-bottom: none; }}
      table.m-stack tbody td {{
        display: flex; align-items: center; justify-content: space-between; gap: 12px;
        padding: 3px 4px; border-bottom: none; text-align: right; white-space: normal;
      }}
      table.m-stack tbody td::before {{
        content: attr(data-label);
        color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em;
        flex: none; text-align: left;
      }}
    }}
  </style>
</head>
<body>
  <script>
    // --- sortable tables, click a <th data-col> to sort. Defined here (right
    // after <body>, before any section's own inline <script>) so it's already
    // global by the time the exit/entry/ablation sections' immediate
    // refresh*Section() calls run. Event-delegated so it also covers tables
    // whose rows are rebuilt every 15s poll or whose tbody didn't exist yet at
    // load time -- reapplySort(tbodyId) is called by each section's own
    // refresh function after it rewrites innerHTML, so a sort survives the
    // next poll instead of silently resetting to server order.
    var tableSortState = {{}};
    function sortTableBody(tbody, col, dir) {{
      var trs = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
      trs.sort(function(a, b) {{
        var av = (a.children[col] && a.children[col].textContent.trim()) || '';
        var bv = (b.children[col] && b.children[col].textContent.trim()) || '';
        var an = parseFloat(av.replace(/[^-0-9.]/g, '')), bn = parseFloat(bv.replace(/[^-0-9.]/g, ''));
        var cmp = (!isNaN(an) && !isNaN(bn) && /^-?[\\d.]/.test(av) && /^-?[\\d.]/.test(bv)) ? (an - bn) : av.localeCompare(bv);
        return cmp * dir;
      }});
      trs.forEach(function(tr) {{ tbody.appendChild(tr); }});
    }}
    function reapplySort(tbodyId) {{
      var state = tableSortState[tbodyId];
      var tbody = document.getElementById(tbodyId);
      if (!state || !tbody) return;
      sortTableBody(tbody, state.col, state.dir);
    }}
    // Section polls used to rewrite innerHTML every 15s even when nothing
    // changed. On phone the stacked tables are tall, so any rewrite ABOVE the
    // card being read destroyed the browser's scroll anchor and (until the
    // label stamper's 300ms debounce re-ran) briefly reverted the table to
    // its wide desktop layout -- with six staggered timers the page visibly
    // jumped to a different card every couple of seconds. Route every poll
    // through this: skip the write when the markup is byte-identical, and
    // when it isn't, restamp phone labels synchronously (no debounce window)
    // so the layout never flashes unstacked. Returns whether it wrote.
    function setSectionHTML(root, html) {{
      if (root.__lastHtml === html) return false;
      root.__lastHtml = html;
      root.innerHTML = html;
      if (window.stampPhoneLabels) window.stampPhoneLabels();
      return true;
    }}
    document.addEventListener('click', function(event) {{
      var th = event.target.closest('th[data-col]');
      if (!th) return;
      var table = th.closest('table');
      var tbody = table && table.querySelector('tbody');
      if (!tbody) return;
      if (!tbody.id) tbody.id = 'sortTbody' + Math.random().toString(36).slice(2);
      var col = Number(th.getAttribute('data-col'));
      var prev = tableSortState[tbody.id];
      var dir = (prev && prev.col === col) ? -prev.dir : 1;
      tableSortState[tbody.id] = {{ col: col, dir: dir }};
      sortTableBody(tbody, col, dir);
      table.querySelectorAll('th[data-col]').forEach(function(h) {{ h.removeAttribute('data-sort'); }});
      th.setAttribute('data-sort', dir === 1 ? 'asc' : 'desc');
    }});
  </script>
  <div class="stickytop">
  <header class="top">
    <div class="top-row">
      <div class="brand">
        <span class="dot"></span>
        <h1>Tema Limit ML</h1>
        <nav class="page-switch" aria-label="Dashboards">
          <a data-port="8766" data-path="/">Live</a>
          <a class="current" href="/dashboard">Models</a>
          <a href="/ops">Ops</a>
          <a data-port="8767" data-path="/">Trend</a>
        </nav>
        <nav class="page-switch" aria-label="Sample range" title="Filters the sample-derived sections (Samples card, Data Mix, Symbol/Series outcomes, Shadow Templates, Recent). Model health tables always show current training state. Days are trading days starting 3pm California time.">
          {range_picker}
        </nav>
      </div>
      <div class="meta-row">
        <span class="chip health-chip" id="healthChip" title="Verification suite roll-up — click the Verification section for detail"><span class="chip-dot"></span><span id="healthChipText">Checks</span></span>
        <span class="chip">Generated <strong>{escape(stats['generated_at'])}</strong></span>
        <div class="overflow-menu">
          <button type="button" class="overflow-btn" id="overflowBtn" aria-label="More actions">&#8943;</button>
          <div class="overflow-pop" id="overflowPop">
            <a href="/stats" target="_blank" rel="noopener">Raw JSON: /stats</a>
            <a href="/ops">Operations page</a>
            <a href="/retrain">Manual retrain</a>
            <a href="/restart" class="danger" onclick="return confirm('Restart the ML service?');">&#8635; Restart ML service</a>
            <div class="menu-note">Entry model dir: {escape(str(stats['entry_model_dir']))}</div>
          </div>
        </div>
      </div>
    </div>
  </header>
  <nav class="pillnav" id="pillNav"></nav>
  </div>
  <main>
    <div id="attentionStrip"></div>
    <div class="grid">{card_html}</div>
    {''.join(sections)}
  </main>
  <button class="fab" id="topBtn" title="Back to top">↑ Top</button>
  <script>
  // --- Data Mix: merge the five distribution sections (Labels / Symbols /
  // Data Series / Triggers / Sample Sources) into one tabbed section. Runs
  // BEFORE the generic section wrapper below so the merged section gets its
  // own collapse handling and nav pill like any other. ---
  (function() {{
    var names = ['Labels', 'Symbols', 'Data Series', 'Triggers', 'Sample Sources'];
    var found = [];
    document.querySelectorAll('main > section').forEach(function(sec) {{
      var h2 = sec.querySelector('h2');
      if (h2 && names.indexOf(h2.textContent.trim()) !== -1) found.push(sec);
    }});
    if (found.length < 2) return;
    var wrap = document.createElement('section');
    var head = document.createElement('h2');
    head.textContent = 'Data Mix';
    wrap.appendChild(head);
    var chipRow = document.createElement('div');
    chipRow.className = 'tref-chips';
    chipRow.style.padding = '0 0 10px';
    wrap.appendChild(chipRow);
    var panes = [];
    found.forEach(function(sec, i) {{
      var title = sec.querySelector('h2').textContent.trim();
      var pane = document.createElement('div');
      pane.className = 'mix-pane';
      var h2 = sec.querySelector('h2');
      while (h2.nextSibling) pane.appendChild(h2.nextSibling);
      pane.style.display = i === 0 ? '' : 'none';
      panes.push(pane);
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'tref-chip';
      chip.textContent = title;
      chip.dataset.active = i === 0 ? 'true' : 'false';
      chip.addEventListener('click', function() {{
        panes.forEach(function(p, j) {{ p.style.display = p === pane ? '' : 'none'; }});
        chipRow.querySelectorAll('.tref-chip').forEach(function(c) {{ c.dataset.active = c === chip ? 'true' : 'false'; }});
      }});
      chipRow.appendChild(chip);
      wrap.appendChild(pane);
    }});
    found[0].parentNode.insertBefore(wrap, found[0]);
    found.forEach(function(sec) {{ sec.parentNode.removeChild(sec); }});
  }})();

  (function() {{
    var defaultCollapsed = ['Outcome', 'Shadow Template', 'Recent Samples', 'Template Reference', 'BB Envelopes'];

    var sections = Array.prototype.slice.call(document.querySelectorAll('main > section'));
    var nav = document.getElementById('pillNav');

    sections.forEach(function(sec, i) {{
      var h2 = sec.querySelector('h2');
      if (!h2) return;
      var title = h2.textContent.trim();
      var slug = 'sec-' + title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
      if (!sec.id) sec.id = slug;

      // Wrap everything after h2 into a collapsible body.
      var body = document.createElement('div');
      body.className = 'sec-body';
      while (h2.nextSibling) {{ body.appendChild(h2.nextSibling); }}
      sec.appendChild(body);

      var shouldCollapse = defaultCollapsed.some(function(k) {{ return title.indexOf(k) !== -1; }});
      var saved = localStorage.getItem('mldash-collapsed-' + sec.id);
      var collapsed = saved !== null ? saved === '1' : shouldCollapse;
      if (collapsed) sec.classList.add('collapsed');

      h2.addEventListener('click', function() {{
        sec.classList.toggle('collapsed');
        localStorage.setItem('mldash-collapsed-' + sec.id, sec.classList.contains('collapsed') ? '1' : '0');
      }});

      var pill = document.createElement('a');
      pill.href = '#' + sec.id;
      var winrate = sec.getAttribute('data-winrate');
      pill.textContent = title + (winrate !== null ? ' · ' + winrate + '% WR' : '');
      nav.appendChild(pill);
    }});

    // The sticky header can wrap to two rows on narrower windows, so its
    // height is not a constant -- measure it and expose it as --sticky-h so
    // sections' scroll-margin-top always clears the pills instead of a
    // hardcoded 110px that leaves anchored cards hidden behind the nav.
    var stickyEl = document.querySelector('.stickytop');
    function syncStickyH() {{
      if (stickyEl) document.documentElement.style.setProperty('--sticky-h', stickyEl.offsetHeight + 'px');
    }}
    window.addEventListener('resize', syncStickyH);
    syncStickyH();

    // Highlight active nav pill on scroll.
    var pills = Array.prototype.slice.call(nav.querySelectorAll('a'));
    var activePill = null;
    window.addEventListener('scroll', function() {{
      var pos = window.scrollY + (stickyEl ? stickyEl.offsetHeight + 20 : 130);
      var current = sections[0] && sections[0].id;
      // getBoundingClientRect, not offsetTop -- offsetTop is relative to the
      // nearest positioned ancestor (e.g. a section wrapped in a position:relative
      // container), not the document, so it silently gives the wrong scroll
      // threshold for any section that isn't a direct static-positioned child.
      sections.forEach(function(sec) {{
        if (sec.getBoundingClientRect().top + window.scrollY <= pos) current = sec.id;
      }});
      var next = null;
      pills.forEach(function(p) {{
        var isActive = p.getAttribute('href') === '#' + current;
        p.classList.toggle('active', isActive);
        if (isActive) next = p;
      }});
      // nav.pillnav scrolls horizontally (overflow-x: auto) once there are more
      // pills than fit the header width, so the active pill can end up off-screen
      // with no visible cue -- keep it in view as the page section changes.
      // behavior:"instant" (not "smooth"/default): smooth scrollIntoView is a
      // rAF-driven animation that can silently stall and never move the nav's
      // scroll position at all; instant always lands correctly.
      if (next && next !== activePill) {{
        activePill = next;
        next.scrollIntoView({{ block: 'nearest', inline: 'nearest', behavior: 'instant' }});
      }}
    }});

    var topBtn = document.getElementById('topBtn');
    window.addEventListener('scroll', function() {{
      topBtn.style.display = window.scrollY > 400 ? 'block' : 'none';
    }});
    topBtn.style.display = 'none';
    topBtn.addEventListener('click', function() {{ window.scrollTo({{ top: 0, behavior: 'smooth' }}); }});
  }})();

  // --- Header chrome: cross-dashboard links, overflow menu, verification
  // roll-up chip + attention strip, and the phone table stacker. ---
  (function() {{
    function portUrl(port, path) {{
      var host = location.hostname || 'localhost';
      var proto = location.protocol === 'https:' ? 'https:' : 'http:';
      return proto + '//' + host + ':' + port + (path || '/');
    }}
    document.querySelectorAll('a[data-port]').forEach(function(a) {{
      a.href = portUrl(a.dataset.port, a.dataset.path);
    }});

    var overflowBtn = document.getElementById('overflowBtn');
    var overflowPop = document.getElementById('overflowPop');
    if (overflowBtn && overflowPop) {{
      overflowBtn.addEventListener('click', function(event) {{
        event.stopPropagation();
        overflowPop.classList.toggle('open');
      }});
      document.addEventListener('click', function() {{ overflowPop.classList.remove('open'); }});
    }}

    // Verification roll-up: one chip answers "is everything OK?"; failing
    // checks additionally surface in an attention strip above the KPI cards.
    async function refreshHealthChip() {{
      var chip = document.getElementById('healthChip');
      var txt = document.getElementById('healthChipText');
      var strip = document.getElementById('attentionStrip');
      if (!chip || !txt) return;
      try {{
        var response = await fetch('/verification-data', {{ cache: 'no-store' }});
        var data = await response.json();
        var checks = data.checks || [];
        var failing = checks.filter(function(c) {{ return c.verdict === 'fail' || c.verdict === 'error'; }});
        chip.classList.remove('ok', 'warn', 'bad');
        chip.classList.add(failing.length ? 'bad' : (data.passing === data.total ? 'ok' : 'warn'));
        txt.textContent = data.passing + '/' + data.total + ' checks';
        if (strip) {{
          if (failing.length) {{
            strip.innerHTML = '<div class="attention"><strong>Attention</strong> — ' + failing.length +
              ' failing check' + (failing.length === 1 ? '' : 's') + ': ' +
              failing.map(function(c) {{ return '<a href="#verification-section">' + c.label + '</a>'; }}).join(', ') +
              '</div>';
          }} else {{
            strip.innerHTML = '';
          }}
        }}
      }} catch (e) {{ /* chip keeps its last state; the section shows details */ }}
    }}
    refreshHealthChip();
    setInterval(refreshHealthChip, 60000);

    // Phone: stamp each td with its column header so the m-stack CSS can lay
    // rows out as label/value lines. Labels come from the table's own live
    // thead (never nth-child guesses), and the observer restamps after any
    // section's poll rewrites its tbody. Attribute writes don't retrigger a
    // childList observer, so this cannot loop.
    function stampLabels() {{
      document.querySelectorAll('main table').forEach(function(table) {{
        if (table.classList.contains('tref')) return;
        var ths = table.querySelectorAll('thead th');
        if (!ths.length) return;
        var labels = Array.prototype.map.call(ths, function(th) {{ return th.textContent.trim(); }});
        table.querySelectorAll('tbody tr').forEach(function(tr) {{
          Array.prototype.forEach.call(tr.children, function(td, i) {{
            td.setAttribute('data-label', labels[i] || '');
          }});
        }});
        table.classList.add('m-stack');
      }});
    }}
    stampLabels();
    // Exposed so setSectionHTML can restamp synchronously right after a
    // rewrite -- the debounced observer below stays as a backstop for any
    // DOM change that didn't go through the helper.
    window.stampPhoneLabels = stampLabels;
    var stampTimer = null;
    new MutationObserver(function() {{
      clearTimeout(stampTimer);
      stampTimer = setTimeout(stampLabels, 300);
    }}).observe(document.querySelector('main'), {{ childList: true, subtree: true }});
  }})();
  </script>
</body>
</html>"""

@app.get("/stats")
def stats(range_name: str = Query("all", alias="range")) -> Dict[str, Any]:
    return sample_stats(50, range_name)


@app.get("/", response_class=HTMLResponse)
def root_dashboard(range_name: str = Query("all", alias="range")) -> str:
    return dashboard_html(range_name)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(range_name: str = Query("all", alias="range")) -> str:
    return dashboard_html(range_name)


# --- Operations page (engineer view) ---
# The six temalimit auto-tuning evidence panels that used to live on the 8766
# live dashboard (Reassess Activity, Template Coverage/Usage, No-Fill Log,
# ATR Pullback, Sizing Reassess, Entry Gate Reassess) now render here, so the
# live dashboard can stay a plain-English splash page. The data itself is
# still computed by LiveDashboardServer (it owns the TSV parsing), so this
# service just proxies its /api/status same-origin -- no CORS, no duplicated
# parsers, and the ops page degrades to an honest "Offline" chip if 8766 is
# down.
_OPS_PAGE_PATH = Path(__file__).resolve().parent / "ops_dashboard.html"
_LIVE_DASHBOARD_BASE = "http://localhost:8766"


def _proxy_live_dashboard(path_and_query: str) -> Dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(
        f"{_LIVE_DASHBOARD_BASE}{path_and_query}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


@app.get("/ops", response_class=HTMLResponse)
def ops_page() -> str:
    return _OPS_PAGE_PATH.read_text(encoding="utf-8")


@app.get("/ops-data")
def ops_data(range_name: str = Query("all", alias="range")) -> Dict[str, Any]:
    # The ops page ships the same 1D-6D/All/custom range picker as the live
    # dashboard (it moved here from 8766 with that UI intact), but this proxy
    # used to hardcode range=all, so the picker silently did nothing. Pass the
    # page's selection through; 8766's normalize_range() validates it server-side
    # (anything unrecognized falls back to "all"). Evidence tables that aggregate
    # their own TSV logs ignore range either way -- this affects the
    # trade-derived cards (Template Coverage/Usage).
    from urllib.parse import urlencode

    try:
        return _proxy_live_dashboard(f"/api/status?{urlencode({'range': range_name})}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the page as Offline
        raise HTTPException(status_code=502, detail=f"live dashboard (8766) unreachable: {exc}")


@app.get("/api/template-ladder")
def ops_template_ladder(ticker: str = "", template: str = "") -> Dict[str, Any]:
    # Same path as on 8766 so the ops page's relative fetch works unchanged.
    from urllib.parse import urlencode

    query = urlencode({"ticker": ticker, "template": template})
    try:
        return _proxy_live_dashboard(f"/api/template-ladder?{query}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"live dashboard (8766) unreachable: {exc}")


@app.get("/schema")
def schema_summary() -> Dict[str, Any]:
    return {
        "window_shape": [WINDOW_SIZE, len(FEATURE_NAMES)],
        "feature_names": FEATURE_NAMES,
        "classes": CLASSES,
        "min_samples_per_group": MIN_SAMPLES_PER_GROUP,
        "predict_example": {
            "symbol": "NQ",
            "trigger": "vwap",
            "min_confidence": 0.60,
            "window": [[0.0 for _ in FEATURE_NAMES] for _ in range(2)],
            # metadata.bars_period is required — it's how /predict routes to
            # the correct per-(symbol, data_series) model. Without it every
            # request for this symbol collapses into a single "" group.
            "metadata": {"bars_period": "500 Tick"},
        },
    }
