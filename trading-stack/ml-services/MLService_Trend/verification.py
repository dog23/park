"""Verification suite -- integrity/leakage checks that run alongside the
ablation study.

This is logic only. Presentation (the dashboard card, buttons, pills) lives in
app.py, mirroring how trend_model.py holds the training logic while app.py
renders it. Each check is a small spec in CHECKS with a readiness probe and a
run function. Heavy checks (permutation, etc.) execute in a threadpool as a
background job; job state is tracked here in a thread-safe dict, and every
finished run appends one line to data/verification_results.jsonl so the last
verdict survives a service restart.

First check implemented: permutation (shuffled-label) test. It retrains a
group's model on randomly shuffled labels; if validation accuracy stays
meaningfully above the majority-class base rate, the pipeline is finding signal
that cannot exist once labels are decoupled from windows -- i.e. leakage or an
eval bug. See run_permutation for the exact verdict rule.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from trend_model import MIN_SAMPLES_PER_GROUP, N_FEATURES, WINDOW_SIZE
from trend_utils import group_key

# A permuted-label model should score no better than always guessing the
# majority class. More than this many points above the val base rate means the
# pipeline is still finding signal that shuffling should have destroyed.
PERMUTATION_TOLERANCE = 0.05

# Label-distribution drift: compare each class's share of the newest 25% of a
# group's samples against its share of the older 75%. A labeling-pipeline break
# (e.g. the losing-ML-trades-labeled-no_trade incident) shows up as a sudden
# proportion shift long before it shows up in model metrics.
DRIFT_MIN_SAMPLES = 40
DRIFT_WARN_DELTA = 0.15
DRIFT_FAIL_DELTA = 0.30
# Mirrored from MLService/verification.py (July 19): a fail needs a new-window
# (newest 25%) of at least this many rows -- a 30pt+ label-share swing measured
# on a dozen rows is noise, not a labeling break. Undersized windows cap at warn.
DRIFT_MIN_NEW_WINDOW = 40

# Walk-forward gap: how many points a random split may beat the honest
# chronological split by before it signals temporal leakage across the boundary.
SPLIT_GAP_WARN = 0.10
SPLIT_GAP_FAIL = 0.20

# Duplicate scan: identical windows straddling the 70% train boundary inflate
# holdout scores. Any at all is worth a warning; percentage of holdout drives fail.
DUP_FAIL_HOLDOUT_PCT = 0.01

# Feature PSI (population stability index), newest 25% vs older 75%, per feature.
# Convention: <0.1 stable, 0.1-0.25 minor, >0.25 major shift.
PSI_WARN = 0.25
PSI_FAIL = 0.50
# Mirrored from MLService/verification.py (July 19): features that measure
# market REGIME by design -- volatility (atr_norm) and trend strength /
# choppiness (adx, choppiness_index) -- shift wholesale whenever the market
# moves between chop and trend, which is organic drift, not a broken input
# (CL failed on adx PSI 3.5 during the July 16-17 regime break). They report
# in a separate "regime drift" lane capped at warn; fail stays reserved for
# the stationary inputs (order_flow_delta, relative_volume, donchian/
# supertrend positions), where a big PSI genuinely means a feed broke.
PSI_LEVEL_FEATURES = {"atr_norm", "adx", "choppiness_index"}
PSI_BUCKETS = 10

# Seed variance: honest from-scratch retrains across seeds. High spread means
# single-run ablation numbers are noise. Advisory only -- never a FAIL.
SEED_VARIANCE_SEEDS = 3
SEED_VARIANCE_WARN_STDEV = 0.08

# Epochs for the throwaway permuted-label fit. Matches the real training budget
# so the test reflects the real pipeline's actual capacity to (over)fit -- a
# shorter budget could hide a leak that only shows up with full training.
PERMUTATION_EPOCHS = 30


def results_path(root: Path) -> Path:
    return root / "data" / "verification_results.jsonl"


# --------------------------------------------------------------- job state
# Guarded by _lock. One entry per check name: {state, progress, message,
# started_at, finished_at}. state is one of idle | running | error.
_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_state(name: str) -> Dict[str, Any]:
    with _lock:
        return dict(_jobs.get(name) or {"state": "idle"})


def is_busy(name: str) -> bool:
    with _lock:
        return (_jobs.get(name) or {}).get("state") == "running"


def any_busy() -> bool:
    with _lock:
        return any((j or {}).get("state") == "running" for j in _jobs.values())


def _set(name: str, **kw: Any) -> None:
    with _lock:
        cur = _jobs.get(name) or {}
        cur.update(kw)
        _jobs[name] = cur


# --------------------------------------------------------------- checks
def permutation_ready(engine: Any) -> Dict[str, Any]:
    """A group can take a permutation test as soon as it has enough samples to
    train at all (same bar as retrain_group's MIN_SAMPLES_PER_GROUP)."""
    health = engine.all_group_health()
    ready_groups = [
        key for key, g in health.items()
        if int(g.get("samples", 0)) >= MIN_SAMPLES_PER_GROUP
    ]
    return {
        "ready": len(ready_groups) > 0,
        "ready_groups": ready_groups,
        "detail": f"{len(ready_groups)} group(s) with >= {MIN_SAMPLES_PER_GROUP} samples",
    }


def run_permutation(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Retrain each ready group from scratch on shuffled labels and compare
    validation accuracy to that split's majority-class base rate.

    Trains from scratch (not warm-started from the live model) on purpose: a
    warm start would inherit real predictive structure and inflate accuracy
    even with shuffled labels, producing a false failure. Publishes nothing --
    the throwaway model never touches the live ModelState or disk.
    """
    info = permutation_ready(engine)
    groups: List[str] = info["ready_groups"]
    per_group: List[Dict[str, Any]] = []

    for i, key in enumerate(groups):
        progress(i / max(1, len(groups)), f"training {key}")
        res = engine.retrain_group(
            key,
            epochs=PERMUTATION_EPOCHS,
            permute_labels=True,
            publish=False,
            from_scratch=True,
        )
        val_acc = res.get("val_acc")
        base = res.get("val_base_rate")
        if val_acc is None or base is None:
            verdict, delta = "skip", None
        else:
            delta = float(val_acc) - float(base)
            verdict = "fail" if delta > PERMUTATION_TOLERANCE else "pass"
        per_group.append({
            "group": key,
            "val_acc": val_acc,
            "base_rate": base,
            "delta": delta,
            "verdict": verdict,
            "trained": bool(res.get("trained")),
            "reason": res.get("reason"),
        })

    progress(1.0, "done")
    verdicts = [g["verdict"] for g in per_group]
    if any(v == "fail" for v in verdicts):
        overall = "fail"
    elif any(v == "pass" for v in verdicts):
        overall = "pass"  # remaining groups were skipped (splits too small), none failed
    else:
        overall = "skip"
    return {"verdict": overall, "groups": per_group}


def _labels_by_group(engine: Any) -> Dict[str, List[str]]:
    """Chronological label sequence per group (rows are appended in time order,
    same assumption retrain_group's slice split already relies on)."""
    out: Dict[str, List[str]] = {}
    for item in engine._iter_training_rows():
        label = str(item.get("label", "")).lower()
        if label not in ("long", "short", "no_trade"):
            continue
        key = group_key(item.get("symbol", ""), item.get("bars_period", ""))
        out.setdefault(key, []).append(label)
    return out


def label_drift_ready(engine: Any) -> Dict[str, Any]:
    by_group = _labels_by_group(engine)
    ready_groups = [k for k, labels in by_group.items() if len(labels) >= DRIFT_MIN_SAMPLES]
    return {
        "ready": len(ready_groups) > 0,
        "ready_groups": ready_groups,
        "detail": f"{len(ready_groups)} group(s) with >= {DRIFT_MIN_SAMPLES} samples",
    }


def run_label_drift(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Newest-25% vs older-75% label share per group. Max per-class proportion
    delta drives the verdict: >= DRIFT_FAIL_DELTA fail, >= DRIFT_WARN_DELTA warn."""
    by_group = _labels_by_group(engine)
    per_group: List[Dict[str, Any]] = []
    classes = ("long", "short", "no_trade")

    for key, labels in sorted(by_group.items()):
        if len(labels) < DRIFT_MIN_SAMPLES:
            per_group.append({"group": key, "verdict": "skip", "samples": len(labels)})
            continue
        split = max(1, int(len(labels) * 0.75))
        old, new = labels[:split], labels[split:]
        deltas = {}
        for cls in classes:
            old_p = sum(1 for l in old if l == cls) / len(old)
            new_p = sum(1 for l in new if l == cls) / len(new)
            deltas[cls] = round(new_p - old_p, 4)
        worst = max(abs(d) for d in deltas.values())
        verdict = "fail" if worst >= DRIFT_FAIL_DELTA else ("warn" if worst >= DRIFT_WARN_DELTA else "pass")
        small_new_window = len(new) < DRIFT_MIN_NEW_WINDOW
        if verdict == "fail" and small_new_window:
            verdict = "warn"
        rec = {
            "group": key, "verdict": verdict, "samples": len(labels),
            "new_window": len(new), "max_delta": round(worst, 4), "deltas": deltas,
        }
        if small_new_window and worst >= DRIFT_FAIL_DELTA:
            rec["note"] = (f"delta over fail threshold but new window has only {len(new)} rows "
                           f"(< {DRIFT_MIN_NEW_WINDOW}) -- capped at warn")
        per_group.append(rec)

    progress(1.0, "done")
    verdicts = [g["verdict"] for g in per_group]
    if any(v == "fail" for v in verdicts):
        overall = "fail"
    elif any(v == "warn" for v in verdicts):
        overall = "warn"
    elif any(v == "pass" for v in verdicts):
        overall = "pass"
    else:
        overall = "skip"
    return {"verdict": overall, "groups": per_group}


def _overall(verdicts: List[str]) -> str:
    """fail > warn > pass > skip, same aggregation for every multi-group check."""
    if any(v == "fail" for v in verdicts):
        return "fail"
    if any(v == "warn" for v in verdicts):
        return "warn"
    if any(v == "pass" for v in verdicts):
        return "pass"
    return "skip"


def _rows_by_group(engine: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Full sample rows per group, in chronological (append) order."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for item in engine._iter_training_rows():
        key = group_key(item.get("symbol", ""), item.get("bars_period", ""))
        out.setdefault(key, []).append(item)
    return out


def _window_hash(window: Any) -> str:
    """Stable hash of a window's values (rounded so float noise from re-serialization
    doesn't hide true duplicates)."""
    try:
        canon = json.dumps([[round(float(v), 6) for v in bar] for bar in window])
    except Exception:
        canon = json.dumps(str(window))
    return hashlib.md5(canon.encode()).hexdigest()


# ------------------------------------------------------------ heavy retrains
def _retrain_variant_check(engine: Any, progress: Callable[[float, str], None],
                           tolerance: float, **retrain_kw: Any) -> Dict[str, Any]:
    """Shared body for permutation/null-feature: from-scratch dry-run retrain
    per ready group, val_acc compared against the split's base rate."""
    groups: List[str] = permutation_ready(engine)["ready_groups"]
    per_group: List[Dict[str, Any]] = []

    for i, key in enumerate(groups):
        progress(i / max(1, len(groups)), f"training {key}")
        res = engine.retrain_group(key, epochs=PERMUTATION_EPOCHS,
                                   publish=False, from_scratch=True, **retrain_kw)
        val_acc, base = res.get("val_acc"), res.get("val_base_rate")
        if val_acc is None or base is None:
            verdict, delta = "skip", None
        else:
            delta = float(val_acc) - float(base)
            verdict = "fail" if delta > tolerance else "pass"
        per_group.append({"group": key, "val_acc": val_acc, "base_rate": base,
                          "delta": delta, "verdict": verdict, "reason": res.get("reason")})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def run_null_feature(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    return _retrain_variant_check(engine, progress, PERMUTATION_TOLERANCE, randomize_features=True)


def run_split_gap(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Honest chronological split vs random split, both from scratch. A random
    split scoring far above walk-forward = temporal leakage across the boundary."""
    groups: List[str] = permutation_ready(engine)["ready_groups"]
    per_group: List[Dict[str, Any]] = []

    for i, key in enumerate(groups):
        progress(i / max(1, len(groups)), f"training {key} (chronological)")
        honest = engine.retrain_group(key, epochs=PERMUTATION_EPOCHS, publish=False, from_scratch=True)
        progress((i + 0.5) / max(1, len(groups)), f"training {key} (random split)")
        shuffled = engine.retrain_group(key, epochs=PERMUTATION_EPOCHS, publish=False,
                                        from_scratch=True, shuffle_split=True)
        h_acc, s_acc = honest.get("val_acc"), shuffled.get("val_acc")
        if h_acc is None or s_acc is None:
            verdict, gap = "skip", None
        else:
            gap = float(s_acc) - float(h_acc)
            verdict = "fail" if gap >= SPLIT_GAP_FAIL else ("warn" if gap >= SPLIT_GAP_WARN else "pass")
        per_group.append({"group": key, "walkforward_val_acc": h_acc,
                          "random_split_val_acc": s_acc, "gap": gap, "verdict": verdict})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def run_seed_variance(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Honest from-scratch retrains across seeds; the spread says how much of
    any single ablation delta is just seed noise. Advisory: warn/pass only."""
    import torch
    groups: List[str] = permutation_ready(engine)["ready_groups"]
    per_group: List[Dict[str, Any]] = []

    for i, key in enumerate(groups):
        accs: List[float] = []
        for s in range(SEED_VARIANCE_SEEDS):
            progress((i + s / SEED_VARIANCE_SEEDS) / max(1, len(groups)), f"{key} seed {s}")
            torch.manual_seed(s)
            res = engine.retrain_group(key, epochs=PERMUTATION_EPOCHS, publish=False, from_scratch=True)
            if res.get("val_acc") is not None:
                accs.append(float(res["val_acc"]))
        if len(accs) < 2:
            per_group.append({"group": key, "verdict": "skip", "val_accs": accs})
            continue
        mean = sum(accs) / len(accs)
        stdev = (sum((a - mean) ** 2 for a in accs) / (len(accs) - 1)) ** 0.5
        verdict = "warn" if stdev > SEED_VARIANCE_WARN_STDEV else "pass"
        per_group.append({"group": key, "verdict": verdict, "val_acc_mean": round(mean, 4),
                          "val_acc_stdev": round(stdev, 4), "val_accs": [round(a, 4) for a in accs]})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


# ------------------------------------------------------------ light scans
def dup_scan_ready(engine: Any) -> Dict[str, Any]:
    by_group = _labels_by_group(engine)
    ready = [k for k, labels in by_group.items() if len(labels) >= DRIFT_MIN_SAMPLES]
    return {"ready": len(ready) > 0, "ready_groups": ready,
            "detail": f"{len(ready)} group(s) with >= {DRIFT_MIN_SAMPLES} samples"}


def run_dup_scan(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Exact-duplicate windows, per group: total dupes, and dupes straddling the
    70% train boundary (those directly inflate holdout accuracy)."""
    rows_by_group = _rows_by_group(engine)
    per_group: List[Dict[str, Any]] = []

    for key, rows in sorted(rows_by_group.items()):
        if len(rows) < DRIFT_MIN_SAMPLES:
            per_group.append({"group": key, "verdict": "skip", "samples": len(rows)})
            continue
        hashes = [_window_hash(r.get("window", [])) for r in rows]
        train_end = max(1, int(len(rows) * 0.70))
        train_set = set(hashes[:train_end])
        holdout = hashes[train_end:]
        cross = sum(1 for h in holdout if h in train_set)
        total_dupes = len(hashes) - len(set(hashes))
        cross_pct = cross / max(1, len(holdout))
        verdict = ("fail" if cross_pct >= DUP_FAIL_HOLDOUT_PCT and cross > 1
                   else ("warn" if cross > 0 else "pass"))
        per_group.append({"group": key, "verdict": verdict, "samples": len(rows),
                          "total_duplicates": total_dupes, "cross_boundary": cross,
                          "cross_pct": round(cross_pct, 4)})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def empty_window_ready(engine: Any) -> Dict[str, Any]:
    n = sum(len(v) for v in _labels_by_group(engine).values())
    return {"ready": n > 0, "detail": f"{n} samples on disk"}


def run_empty_window(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Regression tripwire for the July 17 empty-window purge: any empty,
    malformed, or all-zero window still reaching the training loader is a fail."""
    rows_by_group = _rows_by_group(engine)
    per_group: List[Dict[str, Any]] = []

    for key, rows in sorted(rows_by_group.items()):
        empty = malformed = all_zero = 0
        for r in rows:
            w = r.get("window")
            if not w:
                empty += 1
                continue
            try:
                if any(len(bar) != N_FEATURES for bar in w):
                    malformed += 1
                    continue
                if all(all(float(v) == 0.0 for v in bar) for bar in w):
                    all_zero += 1
            except Exception:
                malformed += 1
        bad = empty + malformed + all_zero
        per_group.append({"group": key, "verdict": "fail" if bad else "pass",
                          "samples": len(rows), "empty": empty,
                          "malformed": malformed, "all_zero": all_zero})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def run_feature_psi(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Per-feature PSI, newest 25% of windows vs older 75%. Catches an input
    source silently breaking (feature stuck at 0, scale change) before the next
    14:05 retrain bakes it into the model."""
    from trend_model import FEATURE_NAMES
    rows_by_group = _rows_by_group(engine)
    per_group: List[Dict[str, Any]] = []

    for key, rows in sorted(rows_by_group.items()):
        if len(rows) < DRIFT_MIN_SAMPLES:
            per_group.append({"group": key, "verdict": "skip", "samples": len(rows)})
            continue
        split = max(1, int(len(rows) * 0.75))
        worst_feature, worst_psi = None, 0.0          # stationary features -> can fail
        level_feature, level_psi = None, 0.0          # regime features -> warn lane
        for f in range(N_FEATURES):
            old_vals: List[float] = []
            new_vals: List[float] = []
            for i, r in enumerate(rows):
                target = old_vals if i < split else new_vals
                try:
                    for bar in r.get("window") or []:
                        target.append(float(bar[f]))
                except Exception:
                    continue
            if len(old_vals) < PSI_BUCKETS * 2 or not new_vals:
                continue
            old_sorted = sorted(old_vals)
            edges = [old_sorted[int(len(old_sorted) * q / PSI_BUCKETS)] for q in range(1, PSI_BUCKETS)]

            def share(vals: List[float]) -> List[float]:
                counts = [0] * PSI_BUCKETS
                for v in vals:
                    b = 0
                    while b < len(edges) and v > edges[b]:
                        b += 1
                    counts[b] += 1
                return [max(c / len(vals), 1e-4) for c in counts]

            po, pn = share(old_vals), share(new_vals)
            psi = sum((n_ - o_) * math.log(n_ / o_) for o_, n_ in zip(po, pn))
            if FEATURE_NAMES[f] in PSI_LEVEL_FEATURES:
                if psi > level_psi:
                    level_psi, level_feature = psi, FEATURE_NAMES[f]
            elif psi > worst_psi:
                worst_psi, worst_feature = psi, FEATURE_NAMES[f]
        # Stationary features drive the verdict; regime gauges can only ever
        # raise it to warn ("regime drift"), never fail.
        verdict = ("fail" if worst_psi >= PSI_FAIL
                   else ("warn" if worst_psi >= PSI_WARN else "pass"))
        if verdict == "pass" and level_psi >= PSI_FAIL:
            verdict = "warn"
        rec = {"group": key, "verdict": verdict, "samples": len(rows),
               "worst_feature": worst_feature, "worst_psi": round(worst_psi, 4),
               "level_feature": level_feature, "level_psi": round(level_psi, 4)}
        if level_psi >= PSI_FAIL:
            rec["note"] = (f"regime drift: {level_feature} PSI {level_psi:.2f} "
                           "(tracks volatility/trend regime by design; capped at warn)")
        per_group.append(rec)

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def determinism_ready(engine: Any) -> Dict[str, Any]:
    health = engine.all_group_health()
    trained = [k for k, g in health.items() if g.get("model_ready")]
    return {"ready": len(trained) > 0, "ready_groups": trained,
            "detail": f"{len(trained)} trained model(s)"}


def run_determinism(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Restart parity: two fresh engines loaded from disk must produce identical
    probabilities for the same window. A mismatch means hidden state or
    load-order dependence -- predictions would differ across service restarts."""
    from trend_model import TrendMlEngine
    root = Path(engine.root) if hasattr(engine, "root") else None
    if root is None:
        # Engine keeps its root implicitly via paths; recover from samples_path.
        root = Path(engine.samples_path).resolve().parent.parent

    trained = determinism_ready(engine)["ready_groups"]
    rows_by_group = _rows_by_group(engine)
    eng_a, eng_b = TrendMlEngine(root), TrendMlEngine(root)
    per_group: List[Dict[str, Any]] = []

    for key in trained:
        rows = rows_by_group.get(key) or []
        if not rows:
            per_group.append({"group": key, "verdict": "skip", "reason": "no sample window"})
            continue
        window = rows[-1].get("window") or []
        symbol_part, _, series_part = key.partition("_")
        pa = eng_a.predict(symbol_part, series_part, window, 0.0)
        pb = eng_b.predict(symbol_part, series_part, window, 0.0)
        probs_a = pa.get("probabilities") or {}
        probs_b = pb.get("probabilities") or {}
        max_diff = max((abs(float(probs_a.get(c, 0)) - float(probs_b.get(c, 0)))
                        for c in set(probs_a) | set(probs_b)), default=0.0)
        verdict = "pass" if max_diff < 1e-6 and pa.get("action") == pb.get("action") else "fail"
        per_group.append({"group": key, "verdict": verdict, "max_prob_diff": max_diff,
                          "action_a": pa.get("action"), "action_b": pb.get("action")})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


CHECKS: Dict[str, Dict[str, Any]] = {
    "permutation": {
        "label": "Permutation (shuffled labels)",
        "guards": "leakage",
        "heavy": True,
        "ready_fn": permutation_ready,
        "run_fn": run_permutation,
        "tip": (
            "Retrains each group on randomly shuffled labels. Validation "
            "accuracy must collapse to the majority-class base rate; staying "
            f"more than {int(PERMUTATION_TOLERANCE * 100)} points above it "
            "means the pipeline is leaking future information into training."
        ),
    },
    "null_feature": {
        "label": "Null-feature baseline",
        "guards": "leakage",
        "heavy": True,
        "ready_fn": permutation_ready,
        "run_fn": run_null_feature,
        "tip": (
            "Retrains each group with windows replaced by pure noise (labels "
            "kept). Accuracy must collapse to the base rate -- anything above "
            "it is signal arriving through something other than the features, "
            "i.e. an eval or pipeline bug."
        ),
    },
    "split_gap": {
        "label": "Walk-forward vs random split",
        "guards": "temporal",
        "heavy": True,
        "ready_fn": permutation_ready,
        "run_fn": run_split_gap,
        "tip": (
            "Trains each group twice from scratch: honest chronological split "
            "vs random split. A random split beating walk-forward by "
            f"{int(SPLIT_GAP_WARN * 100)}+ points (warn) or "
            f"{int(SPLIT_GAP_FAIL * 100)}+ (fail) means temporally adjacent "
            "near-duplicate windows leak across the split boundary."
        ),
    },
    "label_drift": {
        "label": "Label distribution drift",
        "guards": "poison",
        "heavy": False,
        "ready_fn": label_drift_ready,
        "run_fn": run_label_drift,
        "tip": (
            "Compares each class's share of the newest 25% of samples against "
            "the older 75%, per group. A sudden shift in label proportions "
            "usually means a labeling-pipeline break, not a market change. "
            f"Warn at {int(DRIFT_WARN_DELTA * 100)}pt shift, fail at "
            f"{int(DRIFT_FAIL_DELTA * 100)}pt; a fail additionally needs "
            f"{DRIFT_MIN_NEW_WINDOW}+ rows in the new window (else capped at "
            "warn). A one-sided trending day shifts labels legitimately."
        ),
    },
    "dup_scan": {
        "label": "Train/val duplicate scan",
        "guards": "poison",
        "heavy": False,
        "ready_fn": dup_scan_ready,
        "run_fn": run_dup_scan,
        "tip": (
            "Hashes every window and counts exact duplicates, especially ones "
            "straddling the 70% train boundary -- those let the model 'memorize' "
            "holdout answers and inflate val/test accuracy."
        ),
    },
    "empty_window": {
        "label": "Empty-window audit",
        "guards": "poison",
        "heavy": False,
        "ready_fn": empty_window_ready,
        "run_fn": run_empty_window,
        "tip": (
            "Regression tripwire for the July 17 empty-window purge: any "
            "empty, malformed, or all-zero window still reaching the training "
            "data is an instant fail."
        ),
    },
    "feature_psi": {
        "label": "Feature drift (PSI)",
        "guards": "drift",
        "heavy": False,
        "ready_fn": dup_scan_ready,
        "run_fn": run_feature_psi,
        "tip": (
            "Population stability index per feature, newest 25% of windows vs "
            "older 75%. Catches an input source silently breaking before the "
            f"next retrain bakes it in. Warn at {PSI_WARN}, fail at {PSI_FAIL} "
            "-- but only STATIONARY inputs can fail; atr_norm/adx/choppiness "
            "measure volatility/trend regime by design, so their drift reports "
            "as a 'regime drift' warn instead of a fail."
        ),
    },
    "determinism": {
        "label": "Determinism (restart parity)",
        "guards": "logic",
        "heavy": False,
        "ready_fn": determinism_ready,
        "run_fn": run_determinism,
        "tip": (
            "Loads each trained model into two fresh engines (as two restarts "
            "would) and predicts the same window with both. Any probability "
            "difference means hidden state or load-order dependence."
        ),
    },
    "seed_variance": {
        "label": "Seed variance",
        "guards": "noise",
        "heavy": True,
        "ready_fn": permutation_ready,
        "run_fn": run_seed_variance,
        "tip": (
            f"Retrains each group from scratch across {SEED_VARIANCE_SEEDS} "
            "seeds. If val accuracy spread is wide, single-run ablation deltas "
            "are seed noise, not signal. Advisory -- warns, never fails."
        ),
    },
}


# --------------------------------------------------------------- run + persist
def can_start(name: str, engine: Any) -> Tuple[bool, str]:
    spec = CHECKS.get(name)
    if not spec:
        return False, f"unknown check '{name}'"
    if is_busy(name):
        return False, "already running"
    ready = spec["ready_fn"](engine)
    if not ready.get("ready"):
        return False, "not ready: " + str(ready.get("detail") or "")
    return True, "ok"


def _append(root: Path, record: Dict[str, Any]) -> None:
    path = results_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def run_check(name: str, engine: Any, root: Path) -> Dict[str, Any]:
    """Runs a check to completion (call inside a threadpool). Updates job state
    as it goes and appends the final verdict to the results log."""
    spec = CHECKS.get(name)
    if not spec:
        raise ValueError(f"unknown check '{name}'")

    started = time.time()
    _set(name, state="running", progress=0.0, message="starting", started_at=_now(), finished_at=None)

    def progress(frac: float, msg: str = "") -> None:
        _set(name, state="running", progress=round(float(frac), 3), message=msg)

    try:
        result = spec["run_fn"](engine, progress)
    except Exception as exc:  # noqa: BLE001 -- a failed check must record, not crash the loop
        record = {
            "ts": _now(), "check": name, "verdict": "error",
            "error": str(exc), "duration_s": round(time.time() - started, 1),
        }
        _append(root, record)
        _set(name, state="error", message=str(exc), finished_at=_now())
        return record

    record = {
        "ts": _now(), "check": name, "verdict": result["verdict"],
        "groups": result.get("groups"), "duration_s": round(time.time() - started, 1),
    }
    _append(root, record)
    _set(name, state="idle", progress=1.0, message=result["verdict"], finished_at=_now())
    return record


def last_results(root: Path) -> Dict[str, Dict[str, Any]]:
    """Most recent record per check name from the results log (last line wins)."""
    path = results_path(root)
    out: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            key = rec.get("check")
            if key:
                out[key] = rec
    except Exception:
        return out
    return out


def last_sweep_time(root: Path) -> Optional[str]:
    recs = last_results(root)
    times = [r.get("ts") for r in recs.values() if r.get("ts")]
    return max(times) if times else None


# --------------------------------------------------------------- ablation runs
# The ablation itself stays a CLI tool (tools/ablate_near_miss_weights.py); the
# dashboard's Run button just launches it as a subprocess and captures stdout.
# Job state reuses the same _jobs dict under an "ablation:<group>" name.

ABLATION_TIMEOUT_S = 3600  # 5 scales x 5 seeds x retrain -- minutes, not hours


def ablation_job_name(group: str) -> str:
    return f"ablation:{group}"


def ablation_runs_path(root: Path) -> Path:
    return root / "data" / "ablation_runs.jsonl"


def run_ablation(group: str, root: Path, tool_relpath: str = "tools/ablate_near_miss_weights.py") -> Dict[str, Any]:
    """Runs the ablation tool for one group to completion (call in a
    threadpool). Same job-state/JSONL pattern as run_check."""
    name = ablation_job_name(group)
    started = time.time()
    _set(name, state="running", progress=0.0, message="running ablation tool", started_at=_now(), finished_at=None)

    try:
        proc = subprocess.run(
            [sys.executable, str(root / tool_relpath), "--group", group],
            capture_output=True, text=True, timeout=ABLATION_TIMEOUT_S, cwd=str(root),
        )
        ok = proc.returncode == 0
        record = {
            "ts": _now(), "group": group, "ok": ok,
            "returncode": proc.returncode,
            "output": (proc.stdout or "")[-8000:],
            "stderr": (proc.stderr or "")[-2000:] if not ok else "",
            "duration_s": round(time.time() - started, 1),
        }
    except subprocess.TimeoutExpired:
        record = {
            "ts": _now(), "group": group, "ok": False,
            "output": "", "stderr": f"timed out after {ABLATION_TIMEOUT_S}s",
            "duration_s": round(time.time() - started, 1),
        }
    except Exception as exc:  # noqa: BLE001
        record = {
            "ts": _now(), "group": group, "ok": False,
            "output": "", "stderr": str(exc),
            "duration_s": round(time.time() - started, 1),
        }

    path = ablation_runs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    _set(name, state="idle" if record["ok"] else "error",
         progress=1.0, message="ok" if record["ok"] else (record.get("stderr") or "failed")[:200],
         finished_at=_now())
    return record


def last_ablation_runs(root: Path) -> Dict[str, Dict[str, Any]]:
    """Most recent ablation record per group (last line wins)."""
    path = ablation_runs_path(root)
    out: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            grp = rec.get("group")
            if grp:
                out[grp] = rec
    except Exception:
        return out
    return out
