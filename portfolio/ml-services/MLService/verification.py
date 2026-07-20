"""Verification suite for the TemaLimit entry models -- integrity/leakage
checks that run alongside the shadow-weight ablation study.

Port of MLService_Trend/verification.py adapted to this service's data layout
(bars_period lives in metadata, shadow samples instead of near-miss buckets)
plus one temalimit-specific check: the SymbolContext parity checker. Same
architecture: each check is a spec in CHECKS with a readiness probe and a run
function; heavy checks execute in a threadpool as background jobs; every
finished run appends one line to data/verification_results.jsonl.
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

from ml_model import CLASSES, FEATURE_NAMES, MIN_SAMPLES_PER_GROUP, N_FEATURES
from feature_utils import group_key

PERMUTATION_TOLERANCE = 0.05
# Matches the live retrain budget (epochs=8) -- groups here carry thousands of
# rows, so the full 30-epoch trend budget would make heavy checks needlessly slow.
PERMUTATION_EPOCHS = 8

DRIFT_MIN_SAMPLES = 40
DRIFT_WARN_DELTA = 0.15
DRIFT_FAIL_DELTA = 0.30
# A fail needs a new-window (newest 25%) of at least this many rows -- a
# 30pt label-share swing measured on a dozen rows is noise, not a labeling
# break (July 19: RTY_1MINUTE "failed" on 13 shorts out of 17 recent rows).
# Undersized new windows cap at warn.
DRIFT_MIN_NEW_WINDOW = 40

SPLIT_GAP_WARN = 0.10
SPLIT_GAP_FAIL = 0.20

DUP_FAIL_HOLDOUT_PCT = 0.01

PSI_WARN = 0.25
PSI_FAIL = 0.50
PSI_BUCKETS = 10
# Features that TRACK price level / volatility regime by design. On any
# trending market their decile distributions shift wholesale (PSI 2-8 against
# a 0.50 fail bar tuned for stationary features), which is organic drift, not
# contamination -- confirmed July 18-19: 12 of 15 failing groups failed on
# price_scale_log alone while every poison check (permutation, dup_scan,
# cross_symbol, null_feature) passed. These features get their own
# "level drift" lane capped at warn; fail is reserved for the stationary
# features, where a big PSI genuinely means something broke.
PSI_LEVEL_FEATURES = {"price_scale_log", "atr"}

SEED_VARIANCE_SEEDS = 3
SEED_VARIANCE_WARN_STDEV = 0.08

# temalimit.cs SymbolContext static parity checker (found+fixed the
# LastShadowSession save gap). Exit 0 = every field mirrored, 1 = a gap.
PARITY_TOOL = Path(__file__).resolve().parent.parent / "bin" / "Custom" / "Strategies" / "tools" / "check_context_parity.py"

# Exit-sample label integrity. Every trade that has CLOSED must contribute
# exactly one label-0 (exit) row; all-hold trades teach "positions never exit".
# The dead Flat-check silently produced 613 such trades between July 1-17 before
# it was fixed 2026-07-18 -- three weeks of exit data with the single most
# informative event missing, undetected because nothing checked for it. Those
# rows are now in archive/exit_samples_pre_20260718 (see
# tools/quarantine_exitless_trades.py); this check exists so a regression is
# caught in hours, not weeks.
EXIT_TSV_DIR = Path(__file__).resolve().parent
# A trade whose newest sample is within this many minutes of the group's newest
# sample overall is treated as possibly still open, so it is not required to
# have an exit row yet.
EXIT_OPEN_GRACE_MIN = 30.0
EXIT_LABEL_COL = 18  # 0-indexed; header column 19 is "label"


def results_path(root: Path) -> Path:
    return root / "data" / "verification_results.jsonl"


# --------------------------------------------------------------- job state
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


def _set(name: str, **kw: Any) -> None:
    with _lock:
        cur = _jobs.get(name) or {}
        cur.update(kw)
        _jobs[name] = cur


def all_job_states() -> Dict[str, Dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _jobs.items()}


# --------------------------------------------------------------- row access
def _row_group(item: Dict[str, Any]) -> str:
    return group_key(item.get("symbol", ""), (item.get("metadata") or {}).get("bars_period", ""))


def _labels_by_group(engine: Any) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for item in engine._iter_training_rows():
        label = str(item.get("label", "")).lower()
        if label not in ("long", "short", "no_trade"):
            continue
        out.setdefault(_row_group(item), []).append(label)
    return out


def _rows_by_group(engine: Any) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for item in engine._iter_training_rows():
        out.setdefault(_row_group(item), []).append(item)
    return out


def _overall(verdicts: List[str]) -> str:
    if any(v == "fail" for v in verdicts):
        return "fail"
    if any(v == "warn" for v in verdicts):
        return "warn"
    if any(v == "pass" for v in verdicts):
        return "pass"
    return "skip"


def _window_hash(window: Any) -> str:
    try:
        canon = json.dumps([[round(float(v), 6) for v in bar] for bar in window])
    except Exception:
        canon = json.dumps(str(window))
    return hashlib.md5(canon.encode()).hexdigest()


def _dedupe_rows_by_window_label(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse exact (window, label) duplicates to their first occurrence --
    the same view load_training_samples_for_group hands the trainer (July 18
    dedupe). Shadow-template rotation logs one row per template for the same bar
    (identical window + identical label), so the raw stream over-counts those
    setups by 2-40x; deduping here stops that fan-out from being read as drift."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        k = (_window_hash(r.get("window", [])), str(r.get("label", "")).lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# --------------------------------------------------------------- checks
def permutation_ready(engine: Any) -> Dict[str, Any]:
    by_group = _labels_by_group(engine)
    ready_groups = [k for k, labels in by_group.items() if len(labels) >= MIN_SAMPLES_PER_GROUP]
    return {
        "ready": len(ready_groups) > 0,
        "ready_groups": sorted(ready_groups),
        "detail": f"{len(ready_groups)} group(s) with >= {MIN_SAMPLES_PER_GROUP} samples",
    }


def _retrain_variant_check(engine: Any, progress: Callable[[float, str], None],
                           tolerance: float, **retrain_kw: Any) -> Dict[str, Any]:
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


def run_permutation(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    return _retrain_variant_check(engine, progress, PERMUTATION_TOLERANCE, permute_labels=True)


def run_null_feature(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    return _retrain_variant_check(engine, progress, PERMUTATION_TOLERANCE, randomize_features=True)


def run_split_gap(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
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


def _drift_labels_by_group(engine: Any) -> Dict[str, List[str]]:
    """Class labels per group on the loader's deduped view (see
    _dedupe_rows_by_window_label). Runs and readiness both use this so the check
    counts what the trainer counts, not the shadow-inflated raw stream."""
    classes = ("long", "short", "no_trade")
    out: Dict[str, List[str]] = {}
    for key, rows in _rows_by_group(engine).items():
        labeled = [r for r in rows if str(r.get("label", "")).lower() in classes]
        deduped = _dedupe_rows_by_window_label(labeled)
        out[key] = [str(r.get("label", "")).lower() for r in deduped]
    return out


def label_drift_ready(engine: Any) -> Dict[str, Any]:
    by_group = _drift_labels_by_group(engine)
    ready = [k for k, labels in by_group.items() if len(labels) >= DRIFT_MIN_SAMPLES]
    return {"ready": len(ready) > 0, "ready_groups": sorted(ready),
            "detail": f"{len(ready)} group(s) with >= {DRIFT_MIN_SAMPLES} samples"}


def run_label_drift(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    # Deduped view: raw counts double-count shadow-template rotation (one row per
    # template for the same bar), which inflated the newest-window share and
    # false-FAILed rotation-heavy groups like ES_1MINUTE. dup_scan already judges
    # on this same deduped view.
    raw_counts = {k: len(v) for k, v in _labels_by_group(engine).items()}
    by_group = _drift_labels_by_group(engine)
    per_group: List[Dict[str, Any]] = []
    classes = ("long", "short", "no_trade")

    for key, labels in sorted(by_group.items()):
        raw_n = raw_counts.get(key, len(labels))
        if len(labels) < DRIFT_MIN_SAMPLES:
            per_group.append({"group": key, "verdict": "skip",
                              "samples": len(labels), "raw_samples": raw_n})
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
        rec = {"group": key, "verdict": verdict, "samples": len(labels),
               "raw_samples": raw_n, "new_window": len(new),
               "max_delta": round(worst, 4), "deltas": deltas}
        if small_new_window and worst >= DRIFT_FAIL_DELTA:
            rec["note"] = (f"delta over fail threshold but new window has only {len(new)} rows "
                           f"(< {DRIFT_MIN_NEW_WINDOW}) -- capped at warn")
        per_group.append(rec)

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def run_dup_scan(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Duplicate scan on the SAME view of the data the training loader sees:
    load_training_samples_for_group collapses exact (window, label) duplicates
    to their first occurrence (July 18 fix), so this check simulates that
    dedupe first, then looks for surviving cross-boundary duplicates -- which
    after dedupe can only be the same window carrying conflicting labels.
    Raw duplicate counts are still reported for visibility."""
    rows_by_group = _rows_by_group(engine)
    per_group: List[Dict[str, Any]] = []

    for key, rows in sorted(rows_by_group.items()):
        if len(rows) < DRIFT_MIN_SAMPLES:
            per_group.append({"group": key, "verdict": "skip", "samples": len(rows)})
            continue
        raw_hashes = [_window_hash(r.get("window", [])) for r in rows]
        raw_dupes = len(raw_hashes) - len(set(raw_hashes))

        # Simulate the loader's (window, label) dedupe.
        seen = set()
        surviving: List[str] = []
        for r, h in zip(rows, raw_hashes):
            k = (h, str(r.get("label", "")).lower())
            if k in seen:
                continue
            seen.add(k)
            surviving.append(h)

        train_end = max(1, int(len(surviving) * 0.70))
        train_set = set(surviving[:train_end])
        holdout = surviving[train_end:]
        cross = sum(1 for h in holdout if h in train_set)
        cross_pct = cross / max(1, len(holdout))
        verdict = ("fail" if cross_pct >= DUP_FAIL_HOLDOUT_PCT and cross > 1
                   else ("warn" if cross > 0 else "pass"))
        per_group.append({"group": key, "verdict": verdict, "samples": len(rows),
                          "post_dedupe_samples": len(surviving),
                          "raw_duplicates": raw_dupes, "cross_boundary": cross,
                          "cross_pct": round(cross_pct, 4)})

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def empty_window_ready(engine: Any) -> Dict[str, Any]:
    n = sum(len(v) for v in _labels_by_group(engine).values())
    return {"ready": n > 0, "detail": f"{n} samples on disk"}


def run_empty_window(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
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
    rows_by_group = _rows_by_group(engine)
    per_group: List[Dict[str, Any]] = []

    for key, rows in sorted(rows_by_group.items()):
        if len(rows) < DRIFT_MIN_SAMPLES:
            per_group.append({"group": key, "verdict": "skip", "samples": len(rows)})
            continue
        split = max(1, int(len(rows) * 0.75))
        worst_feature, worst_psi = None, 0.0          # stationary features -> can fail
        level_feature, level_psi = None, 0.0          # level-tracking features -> warn lane
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
        # Stationary features drive the verdict; level-tracking features can
        # only ever raise it to warn ("level drift"), never fail.
        verdict = ("fail" if worst_psi >= PSI_FAIL
                   else ("warn" if worst_psi >= PSI_WARN else "pass"))
        if verdict == "pass" and level_psi >= PSI_FAIL:
            verdict = "warn"
        rec = {"group": key, "verdict": verdict, "samples": len(rows),
               "worst_feature": worst_feature, "worst_psi": round(worst_psi, 4),
               "level_feature": level_feature, "level_psi": round(level_psi, 4)}
        if level_psi >= PSI_FAIL:
            rec["note"] = (f"level drift: {level_feature} PSI {level_psi:.2f} "
                           "(tracks price/volatility by design; capped at warn)")
        per_group.append(rec)

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def determinism_ready(engine: Any) -> Dict[str, Any]:
    health = engine.all_group_health() if hasattr(engine, "all_group_health") else {}
    trained = [k for k, g in health.items() if g.get("model_ready")]
    return {"ready": len(trained) > 0, "ready_groups": sorted(trained),
            "detail": f"{len(trained)} trained model(s)"}


def run_determinism(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    from ml_model import MlEngine
    root = getattr(engine, "root", None) or Path(engine.samples_path).resolve().parent.parent

    trained = determinism_ready(engine)["ready_groups"]
    rows_by_group = _rows_by_group(engine)
    eng_a, eng_b = MlEngine(root), MlEngine(root)
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


CROSS_SYMBOL_TOOL = Path(__file__).resolve().parent / "tools" / "check_cross_symbol_leakage.py"


def cross_symbol_ready(engine: Any) -> Dict[str, Any]:
    ok = CROSS_SYMBOL_TOOL.exists()
    return {"ready": ok, "detail": "tripwire script" if ok else "checker script not found"}


def run_cross_symbol(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Cross-instrument bleed tripwire (tools/check_cross_symbol_leakage.py):
    duplicate windows under multiple symbols, window fingerprint vs row symbol
    (the July 15/16 class -- 390 rows purged July 18 evening), unflagged shadow
    rows, exit-TSV symbol mismatches. Exit 0 clean / 1 findings."""
    proc = subprocess.run([sys.executable, str(CROSS_SYMBOL_TOOL)],
                          capture_output=True, text=True, timeout=300)
    progress(1.0, "done")
    verdict = "pass" if proc.returncode == 0 else "fail"
    try:
        detail = json.loads(proc.stdout or "{}")
        groups = [{"group": f.get("check"), "verdict": "fail", "detail": str(f.get("detail"))[:300]}
                  for f in detail.get("findings") or []] or [{"group": "all", "verdict": verdict,
                                                             "rows_scanned": detail.get("rows_scanned")}]
    except Exception:
        groups = [{"group": "all", "verdict": verdict,
                   "output": (proc.stdout or proc.stderr or "")[-1000:]}]
    return {"verdict": verdict, "groups": groups}


def base_rate_gate_ready(engine: Any) -> Dict[str, Any]:
    trained = [k for k, g in engine.all_group_health().items() if g.get("model_ready")]
    return {"ready": bool(trained),
            "detail": f"{len(trained)} trained model(s)" if trained else "no trained models yet"}


def run_base_rate_gate(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Regression tripwire for the 2026-07-18 base-rate quality gate: no group
    may hold good_to_use (live entry-veto power) unless its val_acc beats the
    always-majority baseline by VAL_BASE_RATE_MARGIN with at least
    MIN_VAL_DIRECTIONAL long/short rows in the val slice. Catches both a gate
    code regression and a checkpoint gating on legacy metadata (no base rate /
    directional count persisted -- the ES_1MINUTE July 18 failure mode)."""
    from ml_model import MIN_VAL_DIRECTIONAL, VAL_BASE_RATE_MARGIN

    per_group: List[Dict[str, Any]] = []
    for key, g in sorted(engine.all_group_health().items()):
        if not g.get("model_ready"):
            continue
        status = g.get("status")
        val = g.get("val_acc")
        base = g.get("val_base_rate")
        directional = g.get("val_directional")
        legacy = base is None or directional is None
        edge = None if (val is None or base is None) else round(val - base, 4)

        if status == "good_to_use":
            if legacy:
                verdict, note = "fail", "gating on legacy metadata (no val_base_rate/val_directional) -- retrain to refresh"
            elif val < base + VAL_BASE_RATE_MARGIN:
                verdict, note = "fail", f"gating without beating baseline (+{VAL_BASE_RATE_MARGIN}) -- gate regression"
            elif directional < MIN_VAL_DIRECTIONAL:
                verdict, note = "fail", f"gating with only {directional} directional val rows (< {MIN_VAL_DIRECTIONAL}) -- gate regression"
            else:
                verdict, note = "pass", "genuine edge over baseline"
        else:
            verdict, note = "pass", "not gating (status honest)" if not legacy else "not gating; legacy metadata refreshes on next retrain"

        per_group.append({"group": key, "verdict": verdict, "status": status,
                          "val_acc": val, "val_base_rate": base, "edge": edge,
                          "val_directional": directional, "detail": note})

    progress(1.0, "done")
    if not per_group:
        return {"verdict": "skip", "groups": [{"group": "all", "verdict": "skip", "detail": "no trained models"}]}
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


def context_parity_ready(engine: Any) -> Dict[str, Any]:
    ok = PARITY_TOOL.exists()
    return {"ready": ok, "detail": str(PARITY_TOOL) if ok else "checker script not found"}


def run_context_parity(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Static SymbolContext field-mirroring check for temalimit.cs (the tool
    that caught the LastShadowSession save gap). Exit 0 = parity, 1 = a gap."""
    proc = subprocess.run([sys.executable, str(PARITY_TOOL)],
                          capture_output=True, text=True, timeout=120)
    progress(1.0, "done")
    verdict = "pass" if proc.returncode == 0 else "fail"
    return {"verdict": verdict,
            "groups": [{"group": "temalimit.cs", "verdict": verdict,
                        "output": (proc.stdout or proc.stderr or "")[-2000:]}]}


def exit_label_integrity_ready(engine: Any) -> Dict[str, Any]:
    files = list(EXIT_TSV_DIR.glob("exit_samples_*.tsv"))
    return {"ready": bool(files), "detail": f"{len(files)} exit-sample TSVs on disk"}


def _parse_ts(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except Exception:
        return None


def run_exit_label_integrity(engine: Any, progress: Callable[[float, str], None]) -> Dict[str, Any]:
    """Every CLOSED trade must contribute an exit (label-0) row.

    Regression tripwire for the dead Flat-check (fixed 2026-07-18) that produced
    613 all-hold trades over three weeks. Trades still running are excluded via
    a recency grace window, so an open position is never reported as a fault.
    """
    files = sorted(EXIT_TSV_DIR.glob("exit_samples_*.tsv"))
    per_group: List[Dict[str, Any]] = []

    for idx, path in enumerate(files):
        group = path.stem.replace("exit_samples_", "")
        progress(idx / max(1, len(files)), group)

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except PermissionError:
            per_group.append({"group": group, "verdict": "skip", "reason": "tsv locked"})
            continue
        if len(lines) < 2:
            continue

        has_exit: Dict[str, bool] = {}
        last_ts: Dict[str, Optional[datetime]] = {}
        rows_by_trade: Dict[str, int] = {}
        newest: Optional[datetime] = None

        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) <= EXIT_LABEL_COL:
                continue
            tid = parts[0]
            rows_by_trade[tid] = rows_by_trade.get(tid, 0) + 1
            if parts[EXIT_LABEL_COL] == "0":
                has_exit[tid] = True
            has_exit.setdefault(tid, False)
            ts = _parse_ts(parts[1])
            if ts is not None:
                if last_ts.get(tid) is None or ts > last_ts[tid]:
                    last_ts[tid] = ts
                if newest is None or ts > newest:
                    newest = ts

        closed_missing: List[Dict[str, Any]] = []
        open_skipped = 0
        for tid, ok in has_exit.items():
            if ok:
                continue
            ts = last_ts.get(tid)
            if newest is not None and ts is not None:
                age_min = (newest - ts).total_seconds() / 60.0
                if age_min <= EXIT_OPEN_GRACE_MIN:
                    open_skipped += 1          # plausibly still running
                    continue
            closed_missing.append({"trade_id": tid, "rows": rows_by_trade.get(tid, 0)})

        closed_missing.sort(key=lambda d: d["rows"], reverse=True)
        verdict = "fail" if closed_missing else "pass"
        entry: Dict[str, Any] = {
            "group": group, "verdict": verdict,
            "trades": len(has_exit),
            "trades_with_exit": sum(1 for v in has_exit.values() if v),
            "closed_missing_exit": len(closed_missing),
            "open_excluded": open_skipped,
            "rows_at_risk": sum(d["rows"] for d in closed_missing),
        }
        if closed_missing:
            entry["worst"] = closed_missing[:5]
        per_group.append(entry)

    progress(1.0, "done")
    return {"verdict": _overall([g["verdict"] for g in per_group]), "groups": per_group}


CHECKS: Dict[str, Dict[str, Any]] = {
    "permutation": {
        "label": "Permutation (shuffled labels)", "guards": "leakage", "heavy": True,
        "ready_fn": permutation_ready, "run_fn": run_permutation,
        "tip": ("Retrains each group on randomly shuffled labels (detached dry run). "
                "Validation accuracy must collapse to the majority-class base rate; "
                f"staying more than {int(PERMUTATION_TOLERANCE * 100)} points above it "
                "means the pipeline is leaking."),
    },
    "null_feature": {
        "label": "Null-feature baseline", "guards": "leakage", "heavy": True,
        "ready_fn": permutation_ready, "run_fn": run_null_feature,
        "tip": ("Retrains each group with pure-noise windows and true labels. Accuracy "
                "above the base rate is signal arriving from outside the features -- "
                "an eval or pipeline bug."),
    },
    "split_gap": {
        "label": "Walk-forward vs random split", "guards": "temporal", "heavy": True,
        "ready_fn": permutation_ready, "run_fn": run_split_gap,
        "tip": ("Chronological vs random split, both from scratch. Random beating "
                f"walk-forward by {int(SPLIT_GAP_WARN * 100)}+ pts (warn) / "
                f"{int(SPLIT_GAP_FAIL * 100)}+ (fail) = temporal leakage across the boundary."),
    },
    "label_drift": {
        "label": "Label distribution drift", "guards": "poison", "heavy": False,
        "ready_fn": label_drift_ready, "run_fn": run_label_drift,
        "tip": ("Newest 25% vs older 75% label share per group. Sudden proportion "
                f"shifts flag a labeling break. Warn {int(DRIFT_WARN_DELTA * 100)}pt, "
                f"fail {int(DRIFT_FAIL_DELTA * 100)}pt; a fail additionally needs "
                f"{DRIFT_MIN_NEW_WINDOW}+ rows in the new window (else capped at warn). "
                "A same-direction shift across many groups at once is usually market "
                "regime, not a break."),
    },
    "dup_scan": {
        "label": "Train/val duplicate scan", "guards": "poison", "heavy": False,
        "ready_fn": label_drift_ready, "run_fn": run_dup_scan,
        "tip": ("Hashes every window; duplicates straddling the 70% train boundary "
                "let the model memorize holdout answers."),
    },
    "empty_window": {
        "label": "Empty-window audit", "guards": "poison", "heavy": False,
        "ready_fn": empty_window_ready, "run_fn": run_empty_window,
        "tip": ("Regression tripwire for the July 17 empty-window purge: any empty, "
                "malformed, or all-zero window in the training data is an instant fail."),
    },
    "feature_psi": {
        "label": "Feature drift (PSI)", "guards": "drift", "heavy": False,
        "ready_fn": label_drift_ready, "run_fn": run_feature_psi,
        "tip": ("Population stability index per feature, newest 25% vs older 75%. "
                f"Warn at {PSI_WARN}, fail at {PSI_FAIL} -- but only STATIONARY "
                "features can fail; price_scale_log/atr track price level and "
                "volatility by design, so their drift reports as a 'level drift' "
                "warn instead of a fail."),
    },
    "determinism": {
        "label": "Determinism (restart parity)", "guards": "logic", "heavy": False,
        "ready_fn": determinism_ready, "run_fn": run_determinism,
        "tip": ("Two fresh engines loaded from disk predict the same window. Any "
                "probability difference = hidden state or load-order dependence."),
    },
    "cross_symbol": {
        "label": "Cross-instrument bleed", "guards": "poison", "heavy": False,
        "ready_fn": cross_symbol_ready, "run_fn": run_cross_symbol,
        "tip": ("Tripwire for shadow trades logged under the wrong instrument "
                "(390 rows purged July 18): duplicate windows across symbols, "
                "window symbol_hash fingerprint vs the row's symbol field, "
                "unflagged shadow rows, exit-TSV mismatches."),
    },
    "base_rate_gate": {
        "label": "Base-rate gate", "guards": "logic", "heavy": False,
        "ready_fn": base_rate_gate_ready, "run_fn": run_base_rate_gate,
        "tip": ("No model may gate live entries (good_to_use) unless val_acc beats the "
                "always-majority baseline by 5pts with >= 10 directional val rows. "
                "Regression tripwire for the July 18 degenerate-model fix "
                "(ES_1MINUTE vetoed 12k+ entries at val_acc 1.0 on a 100%-no_trade slice)."),
    },
    "context_parity": {
        "label": "Context parity (temalimit.cs)", "guards": "logic", "heavy": False,
        "ready_fn": context_parity_ready, "run_fn": run_context_parity,
        "tip": ("Static SymbolContext field-mirroring check on temalimit.cs "
                "(caught the LastShadowSession save gap). Every field must round-trip "
                "through LoadContext AND SaveContext."),
    },
    "exit_label_integrity": {
        "label": "Exit-label integrity", "guards": "poison", "heavy": False,
        "ready_fn": exit_label_integrity_ready, "run_fn": run_exit_label_integrity,
        "tip": ("Every CLOSED trade must contribute one exit (label-0) row. Regression "
                "tripwire for the dead Flat-check fixed July 18, which silently produced "
                "613 all-hold trades over three weeks -- data that teaches 'positions "
                "never exit'. Those rows are quarantined in "
                "archive/exit_samples_pre_20260718. Trades whose newest sample is within "
                f"{int(EXIT_OPEN_GRACE_MIN)} min of the group's newest sample are excluded "
                "as still-running, so an open position never reports as a fault."),
    },
    "seed_variance": {
        "label": "Seed variance", "guards": "noise", "heavy": True,
        "ready_fn": permutation_ready, "run_fn": run_seed_variance,
        "tip": (f"From-scratch retrains across {SEED_VARIANCE_SEEDS} seeds. Wide val-acc "
                "spread means single-run ablation deltas are seed noise. Advisory only."),
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
    spec = CHECKS.get(name)
    if not spec:
        raise ValueError(f"unknown check '{name}'")

    started = time.time()
    _set(name, state="running", progress=0.0, message="starting", started_at=_now(), finished_at=None)

    def progress(frac: float, msg: str = "") -> None:
        _set(name, state="running", progress=round(float(frac), 3), message=msg)

    try:
        result = spec["run_fn"](engine, progress)
    except Exception as exc:  # noqa: BLE001 -- a failed check must record, not crash
        record = {"ts": _now(), "check": name, "verdict": "error",
                  "error": str(exc), "duration_s": round(time.time() - started, 1)}
        _append(root, record)
        _set(name, state="error", message=str(exc), finished_at=_now())
        return record

    record = {"ts": _now(), "check": name, "verdict": result["verdict"],
              "groups": result.get("groups"), "duration_s": round(time.time() - started, 1)}
    _append(root, record)
    _set(name, state="idle", progress=1.0, message=result["verdict"], finished_at=_now())
    return record


def last_results(root: Path) -> Dict[str, Dict[str, Any]]:
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
