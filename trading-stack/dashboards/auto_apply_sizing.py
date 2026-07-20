"""Watcher that reads the same live-trade evidence the dashboard's Limit No-Fill Log and Sizing
Reassess cards show, and -- only when every REASSESS-tier bucket sharing a constant unanimously
agrees on direction -- edits temalimit.cs to match. See ML_SYSTEM_GUIDE.txt for the design
discussion this implements (2026-07-17): unanimous-agreement-per-curve was chosen over
first-bucket-wins or a weighted blend specifically to avoid moving 20+ templates off one
thin-sample bucket.

Both sizing tiers are handled, not just Tier 1: Tier 1 (templates 1-19) edits
InstrumentMultiplier/LadderMultiplier directly. Tier 2 (20-40) edits a *different* constant --
Tier2Target for NQ/RTY/YM (the straight-line endpoint), or EsTier2TicksPerTemplate for ES (the
tick-stepper slope, which is shared between Risk1R and LadderDaily -- see _reconcile_es_tier2).
Both are single-step-invertible from "suggested dollar value at template T" the same way Tier 1
is; there's no case that requires guessing.

Entry gates (2026-07-17): the same unanimity machinery also adjusts the per-tier entry-gate widen
constants (MfiGateWiden*/RsiGateWiden*/StochGateWiden*, evidence: TemaLimit_gateblock_log.tsv) and
expire extras (EntryExpireExtraMinutes*, evidence: TemaLimit_expire_log.tsv's post-cancel touch
watch) -- see check_entry_gate_agreement. These constants are shared across instruments, so
unanimity runs across tickers within each (gate, tier) group rather than across templates.

Run manually to see what it would do without touching anything:
    python auto_apply_sizing.py --dry-run

Run for real:
    python auto_apply_sizing.py --apply

Intended to be scheduled (e.g. every few minutes) once you're comfortable with what it proposes.
Every check and every edit is appended to auto_apply_sizing.log (free-text), and a timestamped
backup of temalimit.cs is written before any change. Applied edits are also appended to
auto_apply_history.json (structured, keyed) -- that's what the dashboard's "Last Applied" columns
on the No-Fill Log / Sizing Reassess / Entry Gate Reassess tables read to show old->new per row. Before writing, the edited curve is fully resimulated
(all 40 templates) and checked for the same invariants established when the formula was built:
zero repeated dollar values, strictly increasing, LadderDaily < Risk1R, and NQ > ES > RTY > YM
ordering against the other three (unedited) instruments' current live values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_dashboard_server import (  # noqa: E402
    EXPIRE_EXTRA_CONSTANTS,
    EXPIRE_MAX_TOTAL_EXTRA,
    GATE_MAX_TOTAL_WIDEN,
    GATE_WIDEN_CONSTANTS,
    NT_DIR,
    build_atr_clamp_stats,
    build_entry_gate_stats,
    build_ladder_trail_diagnosis,
    build_nofill_stats,
    build_slippage_stats,
    build_template_risk_map,
    read_template_reference,
)

TEMALIMIT_CS_PATH = Path(
    os.environ.get("TEMALIMIT_CS_PATH", NT_DIR / "bin" / "Custom" / "Strategies" / "temalimit.cs")
)
AUTO_APPLY_LOG_PATH = Path(__file__).resolve().parent / "auto_apply_sizing.log"
BACKUP_DIR = Path(__file__).resolve().parent / "temalimit_auto_apply_backups"
# Read by temalimit.cs's PrintCompileNotificationIfNeeded() on the next State.DataLoaded after
# recompile, then deleted -- so the Output 2 banner shows what this run changed exactly once,
# instead of either nothing or stale info from a prior run.
COMPILE_NOTIFICATION_PATH = NT_DIR / "temalimit_last_auto_apply.txt"
# Structured, persistent record of every applied change -- unlike auto_apply_sizing.log (free-text,
# append-only) or COMPILE_NOTIFICATION_PATH (one run's summary, deleted after temalimit.cs reads
# it), this is what the dashboard's "Last Applied" columns read to show a value-change column
# directly on the No-Fill Log / Sizing Reassess / Entry Gate Reassess rows. read_auto_apply_history()
# in live_dashboard_server.py is the only reader; keys must match what that side looks up by.
HISTORY_PATH = Path(__file__).resolve().parent / "auto_apply_history.json"
HISTORY_MAX_ENTRIES = 500

# Mirrors temalimit.cs's TieredDollarValue formula constants exactly -- must stay in sync if
# those ever change. Used both to invert suggestions into new constants and to resimulate a
# curve before writing it.
UNIVERSAL_BASE = 3000.0
TIER1_MAX_TEMPLATE = 19
ABSOLUTE_MAX_TEMPLATE = 40
ES_TICK_DOLLARS = 12.5


def _log(message: str) -> None:
    if _capture_buffer is not None:
        _capture_buffer.append(message)
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(AUTO_APPLY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- Repeat-log dampener -------------------------------------------------------------------
# The scheduled task runs every few minutes against evidence that only changes when new trades
# or no-fills arrive, so an unapplied finding (skipped or rejected) used to re-log its identical
# finding + outcome lines on every run -- ~2,000 repeat lines/day drowning the real applies.
# Each finding's lines are captured first, fingerprinted, and emitted only the first time that
# exact block appears each day; repeats are counted and summarized in the end-of-run line.
# Applied findings always emit. State lives in SUPPRESS_STATE_PATH, pruned to today on load.

SUPPRESS_STATE_PATH = Path(__file__).resolve().parent / "auto_apply_log_suppress.json"
_capture_buffer: list[str] | None = None


def _begin_capture() -> None:
    global _capture_buffer
    _capture_buffer = []


def _end_capture() -> list[str]:
    global _capture_buffer
    lines, _capture_buffer = _capture_buffer or [], None
    return lines


def _load_suppress_state() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        state = json.loads(SUPPRESS_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return {fp: day for fp, day in state.items() if day == today}


def _save_suppress_state(state: dict) -> None:
    try:
        SUPPRESS_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _emit_finding(lines: list[str], always_emit: bool, suppress_state: dict) -> bool:
    """Emits the captured finding block unless an identical block was already logged today.
    Returns True if emitted."""
    if not lines:
        return False
    fingerprint = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()
    today = datetime.now().strftime("%Y-%m-%d")
    if not always_emit and suppress_state.get(fingerprint) == today:
        return False
    for line in lines:
        _log(line)
    suppress_state[fingerprint] = today
    return True


def template_multiplier(template_number: int) -> float:
    clamped = max(1, min(ABSOLUTE_MAX_TEMPLATE, template_number))
    return 1.0 + 0.041667 * (clamped - 1)


def round_dollars_to_whole_ticks(value: float, ticker: str) -> float:
    rounded = round(value, 1)
    if ticker == "ES":
        return round(rounded / ES_TICK_DOLLARS) * ES_TICK_DOLLARS
    return rounded


def simulate_curve(ticker: str, tier1_multiplier: float, tier2_param: float) -> list[float]:
    """Reproduces TieredDollarValue for one (ticker, role) across all 40 templates. tier2_param
    is Tier2Target's dollar value for NQ/RTY/YM, or EsTier2TicksPerTemplate for ES."""
    values = []
    for t in range(1, ABSOLUTE_MAX_TEMPLATE + 1):
        if t <= TIER1_MAX_TEMPLATE:
            values.append(round_dollars_to_whole_ticks(UNIVERSAL_BASE * template_multiplier(t) * tier1_multiplier, ticker))
            continue
        if ticker == "ES":
            tier1_end = round_dollars_to_whole_ticks(UNIVERSAL_BASE * template_multiplier(TIER1_MAX_TEMPLATE) * tier1_multiplier, ticker)
            values.append(tier1_end + (t - TIER1_MAX_TEMPLATE) * tier2_param * ES_TICK_DOLLARS)
            continue
        tier1_end_raw = UNIVERSAL_BASE * template_multiplier(TIER1_MAX_TEMPLATE) * tier1_multiplier
        frac = (t - TIER1_MAX_TEMPLATE) / (ABSOLUTE_MAX_TEMPLATE - TIER1_MAX_TEMPLATE)
        values.append(round_dollars_to_whole_ticks(tier1_end_raw + (tier2_param - tier1_end_raw) * frac, ticker))
    return values


def curve_is_safe(values: list[float]) -> bool:
    return len(set(values)) == len(values) and all(values[i] > values[i - 1] for i in range(1, len(values)))


def ordering_is_safe(risk_curves: dict, ladder_curves: dict) -> bool:
    """risk_curves/ladder_curves: ticker -> list of 40 values. Checks NQ>ES>RTY>YM and
    ladder<risk at every template, using whatever mix of edited/current curves is passed in."""
    for t in range(ABSOLUTE_MAX_TEMPLATE):
        if not (risk_curves["NQ"][t] > risk_curves["ES"][t] > risk_curves["RTY"][t] > risk_curves["YM"][t]):
            return False
        if not (ladder_curves["NQ"][t] > ladder_curves["ES"][t] > ladder_curves["RTY"][t] > ladder_curves["YM"][t]):
            return False
        for ticker in ("NQ", "ES", "RTY", "YM"):
            if not (ladder_curves[ticker][t] < risk_curves[ticker][t]):
                return False
    return True


def _current_curve_from_reference(ticker: str, field: str) -> list[float]:
    """field: 'risk1R' or 'ladderDaily'. Ground truth from what temalimit.cs actually generated,
    for the three (unedited) instruments in a cross-instrument ordering check."""
    reference = read_template_reference()
    by_template = {t.get("template"): t for t in reference.get("templates", [])}
    return [(by_template.get(t, {}).get("risk") or {}).get(ticker, {}).get(field, 0.0) for t in range(1, ABSOLUTE_MAX_TEMPLATE + 1)]


def curve_unanimous_direction(buckets_by_template: dict, tier_field: str, direction_field: str) -> str | None:
    """Every bucket at reassess tier for this curve must agree, or this returns None -- including
    when zero buckets qualify. A reassess-tier bucket with no direction (target too close to
    current to bother) doesn't block consensus; only a genuine opposite direction does."""
    directions = set()
    for bucket in buckets_by_template.values():
        if bucket.get(tier_field) == "reassess":
            direction = bucket.get(direction_field)
            if direction:
                directions.add(direction)
    return directions.pop() if len(directions) == 1 else None


ROLE_FIELDS = (
    # role, tier_field, direction_field, suggested_field, risk_map_field, is_ladder
    ("risk1R", "riskReassessTier", "riskDirection", "suggestedRisk1R", "risk1R", False),
    ("ladderDaily", "reassessTier", "ladderDirection", "suggestedLadderDaily", "ladderDaily", True),
)


def check_sizing_agreement() -> list[dict]:
    """One finding per (ticker, role, tier) with unanimous REASSESS-tier agreement. Tier 1 findings
    carry a newMultiplier (InstrumentMultiplier/LadderMultiplier). Tier 2 findings carry either
    newTier2Target (NQ/RTY/YM) or newEsTicksPerTemplate (ES)."""
    # Last-applied cutoffs per (ticker, role, tier): the ladder +/-15% suggestion is multiplicative
    # off the current value, so without resetting its evidence each apply would compound another
    # step off the same historical reversals every run (see _evidence_cutoffs).
    diagnosis = build_ladder_trail_diagnosis(sizing_since=_evidence_cutoffs("sizing", ("ticker", "role", "tier")))
    risk_map = build_template_risk_map()
    findings = []

    for ticker, templates in diagnosis.items():
        tier1 = {k: v for k, v in templates.items() if int(k) <= TIER1_MAX_TEMPLATE}
        tier2 = {k: v for k, v in templates.items() if int(k) > TIER1_MAX_TEMPLATE}

        for role, tier_field, direction_field, suggested_field, risk_field, is_ladder in ROLE_FIELDS:
            # --- Tier 1: solve for a new InstrumentMultiplier/LadderMultiplier ---
            direction = curve_unanimous_direction(tier1, tier_field, direction_field)
            if direction:
                implied = [
                    b[suggested_field] / (UNIVERSAL_BASE * template_multiplier(int(k)))
                    for k, b in tier1.items()
                    if b.get(tier_field) == "reassess" and b.get(direction_field) == direction and b.get(suggested_field)
                ]
                if implied:
                    findings.append({
                        "ticker": ticker, "role": role, "tier": "tier1", "direction": direction,
                        "newMultiplier": round(sum(implied) / len(implied), 6),
                        "supportingTemplates": sorted(int(k) for k, b in tier1.items() if b.get(tier_field) == "reassess" and b.get(direction_field) == direction),
                    })

            # --- Tier 2: solve for a new Tier2Target (NQ/RTY/YM) or ES ticks-per-template ---
            direction2 = curve_unanimous_direction(tier2, tier_field, direction_field)
            if not direction2:
                continue
            tier1_end = (risk_map.get(ticker, {}).get(str(TIER1_MAX_TEMPLATE)) or {}).get(risk_field)
            if not tier1_end:
                continue
            implied_targets, implied_ticks, supporting = [], [], []
            for k, b in tier2.items():
                if b.get(tier_field) != "reassess" or b.get(direction_field) != direction2:
                    continue
                suggested = b.get(suggested_field)
                if not suggested:
                    continue
                t = int(k)
                supporting.append(t)
                if ticker == "ES":
                    steps = t - TIER1_MAX_TEMPLATE
                    implied_ticks.append((suggested - tier1_end) / (steps * ES_TICK_DOLLARS))
                else:
                    frac = (t - TIER1_MAX_TEMPLATE) / (ABSOLUTE_MAX_TEMPLATE - TIER1_MAX_TEMPLATE)
                    implied_targets.append(tier1_end + (suggested - tier1_end) / frac)
            if ticker == "ES" and implied_ticks:
                findings.append({
                    "ticker": ticker, "role": role, "tier": "tier2", "direction": direction2,
                    "newEsTicksPerTemplate": max(1, round(sum(implied_ticks) / len(implied_ticks))),
                    "supportingTemplates": sorted(supporting),
                })
            elif implied_targets:
                findings.append({
                    "ticker": ticker, "role": role, "tier": "tier2", "direction": direction2,
                    "newTier2Target": round(sum(implied_targets) / len(implied_targets), 2),
                    "supportingTemplates": sorted(supporting),
                })
    return findings


def _reconcile_es_tier2(findings: list[dict]) -> list[dict]:
    """EsTier2TicksPerTemplate is one constant shared by Risk1R and LadderDaily -- if both roles
    have an ES Tier 2 finding, they must agree on the same integer or neither gets applied."""
    es_tier2 = [f for f in findings if f["ticker"] == "ES" and f["tier"] == "tier2"]
    if len(es_tier2) <= 1:
        return findings
    values = {f["newEsTicksPerTemplate"] for f in es_tier2}
    if len(values) == 1:
        return findings
    _log(f"ES Tier 2: Risk1R and LadderDaily evidence disagree on ticks/template ({[f['newEsTicksPerTemplate'] for f in es_tier2]}) -- skipping both, shared constant needs one answer.")
    return [f for f in findings if not (f["ticker"] == "ES" and f["tier"] == "tier2")]


def _evidence_cutoffs(kind: str, key_fields: tuple[str, ...]) -> dict:
    """{key -> timestamp of the last applied change of this history kind}, keyed by the given
    fields (tuple key when several, scalar when one). Passing these into the stats builders
    resets each constant's evidence window after each apply: pre-apply rows keep implying the
    already-applied delta, so without the reset every scheduled run re-applies the same step
    until a cap or curve invariant stops it. That's not hypothetical -- it's exactly how YM's
    high-tier pullback ratio walked 0.2 -> 0.0125 in nine steps on 2026-07-17 before the pullback
    cutoff existed, and every delta/drift-style suggestion (gate widen/tighten, expire extras,
    ladder +/-15%, ATR-clamp drift) had the same latent failure mode until they got cutoffs too.
    New-regime evidence only."""
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8")) if HISTORY_PATH.exists() else []
    except (OSError, ValueError):
        history = []
    cutoffs: dict = {}
    for entry in history:  # append-ordered oldest-first, so later entries win
        if entry.get("type") != kind or not entry.get("timestamp"):
            continue
        key = tuple(entry.get(f) for f in key_fields)
        cutoffs[key if len(key) > 1 else key[0]] = entry["timestamp"]
    return cutoffs


def check_pullback_agreement(range_name: str = "all") -> list[dict]:
    """Both tiers (<=17, >=18) for each ticker -- pullback's ratio is flat per tier, not a curve,
    so this doesn't have the Tier 1/2 restriction sizing does."""
    stats = build_nofill_stats(range_name, pullback_since=_evidence_cutoffs("pullback", ("ticker", "tierGroup")))
    reference = read_template_reference()
    base_pullback = {str(t.get("template")): t.get("pullbackTicks") for t in reference.get("templates", [])}

    grouped: dict[tuple[str, str], dict] = {}
    for bucket in stats["byTemplateInstrument"]:
        tier_group = "low" if bucket["template"] <= 17 else "high"
        grouped.setdefault((bucket["ticker"], tier_group), {})[str(bucket["template"])] = bucket

    findings = []
    for (ticker, tier_group), templates in grouped.items():
        direction = curve_unanimous_direction(templates, "reassessTier", "suggestDirection")
        if not direction:
            continue
        implied_ratios, supporting = [], []
        for template_key, bucket in templates.items():
            if bucket.get("reassessTier") != "reassess" or bucket.get("suggestDirection") != direction:
                continue
            suggested_ticks = bucket.get("suggestedPullbackTicks")
            base = base_pullback.get(template_key)
            if not suggested_ticks or not base:
                continue
            implied_ratios.append(suggested_ticks / base)
            supporting.append(int(template_key))
        if not implied_ratios:
            continue
        findings.append({
            "ticker": ticker, "tierGroup": tier_group, "direction": direction,
            "newRatio": round(sum(implied_ratios) / len(implied_ratios), 4),
            "supportingTemplates": sorted(supporting),
        })
    return findings


# Per-run cap on how far one automation pass may move an entry-gate constant. Measured gaps can be
# huge on a quiet day (MFI sitting at 40 vs a LongMax of 8 would suggest +32 in one shot); stepping
# at most this much per run lets the evidence re-accumulate against the new thresholds before the
# next step, same convergence philosophy as the pullback nudges. The hard total ceilings
# (GATE_MAX_TOTAL_WIDEN / EXPIRE_MAX_TOTAL_EXTRA, imported from live_dashboard_server so the stats
# builders can suppress a suggestion once pinned there) keep the automation from ever flattening the
# template ladder -- GetTemplateParams' own clamps (49/51 MFI, 0.49/0.51 Stoch) are the last-resort
# bound, not the target.
GATE_MAX_STEP = {"MFI": 5.0, "RSI": 5.0, "StochRSI": 0.05}


def check_entry_gate_agreement() -> tuple[list[dict], list[dict]]:
    """(gate_findings, expire_findings). The gate-widen/expire-extra constants are shared across all
    four instruments (unlike sizing's per-ticker multipliers), so the unanimity requirement runs
    across TICKERS within each (gate, tierGroup) / (tierGroup) group: every reassess-tier bucket
    must agree on direction, and the applied delta is the mean of the supporting buckets' deltas.

    Evidence window is a 5-day rolling range, NOT "all": the tighten/decrease drifts require a
    bucket with ZERO blocks (or zero touches), and under "all" a single historical block row would
    veto tighten forever -- old evidence has to age out for the drift back toward the designed
    table to ever be reachable. Widen also benefits: it reacts to the current regime instead of
    gaps measured against thresholds that have since been widened.

    On top of the rolling window, last-applied cutoffs (see _evidence_cutoffs): these suggestions
    are DELTAS added to the current constant, so pre-apply rows inside the window would re-apply
    the same step every run -- widen would ratchet to its ceiling in ~3 runs, and the -25% tighten
    "drift" would collapse a widen to zero in under an hour at the task's cadence."""
    stats = build_entry_gate_stats(
        "5d",
        gate_since=_evidence_cutoffs("gate", ("gate", "tierGroup")),
        expire_since=_evidence_cutoffs("expire", ("tierGroup",)),
    )
    gate_findings: list[dict] = []
    expire_findings: list[dict] = []

    gate_groups: dict[tuple[str, str], dict] = {}
    for bucket in stats["gates"]:
        gate_groups.setdefault((bucket["gate"], bucket["tierGroup"]), {})[bucket["ticker"]] = bucket

    for (gate, tier_group), buckets in gate_groups.items():
        direction = curve_unanimous_direction(buckets, "reassessTier", "suggestDirection")
        if not direction:
            continue
        constant = GATE_WIDEN_CONSTANTS.get((gate, tier_group))
        current = stats["constants"].get(constant)
        if constant is None or current is None:
            continue
        deltas = [
            b["suggestedWidenDelta"]
            for b in buckets.values()
            if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction and b.get("suggestedWidenDelta") is not None
        ]
        if not deltas:
            continue
        step_cap = GATE_MAX_STEP[gate]
        delta = max(-step_cap, min(step_cap, sum(deltas) / len(deltas)))
        new_widen = max(0.0, min(GATE_MAX_TOTAL_WIDEN[gate], current + delta))
        gate_findings.append({
            "gate": gate, "tierGroup": tier_group, "direction": direction,
            "constant": constant, "newWiden": round(new_widen, 4),
            "supportingTickers": sorted(t for t, b in buckets.items() if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction),
        })

    expire_groups: dict[str, dict] = {}
    for bucket in stats["expire"]:
        expire_groups.setdefault(bucket["tierGroup"], {})[bucket["ticker"]] = bucket

    for tier_group, buckets in expire_groups.items():
        direction = curve_unanimous_direction(buckets, "reassessTier", "suggestDirection")
        if not direction:
            continue
        constant = EXPIRE_EXTRA_CONSTANTS.get(tier_group)
        current = stats["constants"].get(constant)
        if constant is None or current is None:
            continue
        deltas = [
            b["suggestedExtraDelta"]
            for b in buckets.values()
            if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction and b.get("suggestedExtraDelta") is not None
        ]
        if not deltas:
            continue
        new_extra = int(max(0, min(EXPIRE_MAX_TOTAL_EXTRA, round(current + sum(deltas) / len(deltas)))))
        expire_findings.append({
            "tierGroup": tier_group, "direction": direction,
            "constant": constant, "newExtraMinutes": new_extra,
            "supportingTickers": sorted(t for t, b in buckets.items() if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction),
        })

    return gate_findings, expire_findings


def check_slippage_agreement() -> dict | None:
    """One finding (or None) for the shared SlippageReserveRatio constant. Evidence window is
    "all": stop exits are sparse, both directions are measured (p90-based, no zero-count drift
    that needs evidence to age out), and slippage is a broker/market microstructure property that
    changes slowly. Unanimity across tickers with >= reassess-tier evidence; applied value is the
    mean of supporting suggestions, step-capped to 0.05/run, clamped 0.02-0.25."""
    stats = build_slippage_stats("all")
    current = stats.get("currentRatio")
    if current is None:
        return None
    buckets = {b["ticker"]: b for b in stats["tickers"]}
    direction = curve_unanimous_direction(buckets, "reassessTier", "suggestDirection")
    if not direction:
        return None
    suggestions = [
        b["suggestedRatio"]
        for b in buckets.values()
        if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction and b.get("suggestedRatio") is not None
    ]
    if not suggestions:
        return None
    target = sum(suggestions) / len(suggestions)
    delta = max(-0.05, min(0.05, target - current))
    new_ratio = max(0.02, min(0.25, round(current + delta, 3)))
    return {
        "direction": direction, "constant": "SlippageReserveRatio", "newRatio": new_ratio,
        "supportingTickers": sorted(t for t, b in buckets.items() if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction),
    }


def check_atr_clamp_agreement() -> dict | None:
    """One finding (or None) for the shared AtrClampMin floor. 5-day rolling window, same
    rationale as the entry gates: the increase drift back toward the designed 0.50 requires ZERO
    floor-bound no-fills in range, which old rows would veto forever under "all". Decrease is
    measured (p25 of raw ratios among would-have-filled floor-bound no-fills). The ceiling
    (AtrClampMax) is intentionally NOT automated -- see build_atr_clamp_stats. Step cap 0.10/run,
    clamped 0.20-0.50. Last-applied cutoff (see _evidence_cutoffs) so both the measured decrease
    and the +0.05 increase drift re-earn their sample against the current floor."""
    stats = build_atr_clamp_stats("5d", since=_evidence_cutoffs("atrclamp", ("param",)).get("min"))
    current = stats.get("currentMin")
    if current is None:
        return None
    buckets = {b["ticker"]: b for b in stats["tickers"]}
    direction = curve_unanimous_direction(buckets, "reassessTier", "suggestDirection")
    if not direction:
        return None
    suggestions = [
        b["suggestedMin"]
        for b in buckets.values()
        if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction and b.get("suggestedMin") is not None
    ]
    if not suggestions:
        return None
    target = sum(suggestions) / len(suggestions)
    delta = max(-0.10, min(0.10, target - current))
    new_min = max(0.20, min(0.50, round(current + delta, 2)))
    return {
        "direction": direction, "constant": "AtrClampMin", "newMin": new_min,
        "supportingTickers": sorted(t for t, b in buckets.items() if b.get("reassessTier") == "reassess" and b.get("suggestDirection") == direction),
    }


# Was 2%: too tight in practice -- the July 17 log shows ES's high-tier pullback ratio churning
# 0.1064 <-> 0.1093 (+/-2.7%) for hours as suggestion noise crossed the old dead-band. 5% still
# passes every real change (gate steps, +/-15% ladder nudges are all larger) EXCEPT whole-minute
# expire steps once the extra is large: 1/20 = 5% is not > 5%, so a legitimate 20->21 min step was
# silently dead-banded and re-proposed every run, stalling expire below its 24-min cap. Integer
# constants pass min_abs_step instead (any full-unit change is material by construction).
MATERIAL_CHANGE_THRESHOLD = 0.05  # 5% relative difference


def _materially_different(old_value: float, new_value: float, min_abs_step: float | None = None) -> bool:
    """Guards against a reapply loop: the pullback/multiplier patchers write with limited display
    precision (e.g. 2 decimal places), so a suggestion of 0.1111 gets stored as "0.11" -- the very
    next run then sees old=0.11 vs suggested=0.1111 and "applies" a change again, forever. Found
    2026-07-17 when the scheduled task re-applied the same RTY pullback ratio on its first
    automatic run, 9 minutes after a manual run had already set it. Only apply if the difference
    is bigger than what display rounding alone could produce.

    min_abs_step: for integer-quantized constants (expire whole minutes) the smallest meaningful
    change is one whole unit, so a fixed absolute floor replaces the relative test -- the relative
    5% band wrongly rejects a 20->21 step and would loop-skip it forever otherwise."""
    if min_abs_step is not None:
        return abs(new_value - old_value) >= min_abs_step
    if old_value == 0:
        return new_value != 0
    return abs(new_value - old_value) / abs(old_value) > MATERIAL_CHANGE_THRESHOLD


def _read_cs() -> str:
    return TEMALIMIT_CS_PATH.read_text(encoding="utf-8")


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + os.replace so no reader (the 8766 server's read_gate_widen_constants
    polls temalimit.cs every second; NinjaScript's compiler watches it too) can ever observe a
    half-written file, which a plain write_text's truncate-then-write allows. os.replace on Windows
    can transiently fail with PermissionError if a reader has the target open without share-delete
    (CPython's open() doesn't grant it), so retry briefly, then fall back to the old direct write --
    a rare torn read is self-healing, a silently lost apply under pythonw is not."""
    tmp_path = path.parent / (path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.2)
    _log(f"  WARNING: atomic replace of {path.name} kept failing (reader holding it open?); falling back to direct write.")
    path.write_text(text, encoding="utf-8")
    try:
        tmp_path.unlink()
    except OSError:
        pass


def _write_cs(text: str, original: str) -> None:
    if text.count("{") != original.count("{") or text.count("}") != original.count("}"):
        raise ValueError("Brace count changed by the edit -- refusing to write, this would break compilation.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"temalimit_{stamp}.cs"
    shutil.copy2(TEMALIMIT_CS_PATH, backup_path)
    _atomic_write(TEMALIMIT_CS_PATH, text)
    _log(f"  backup written: {backup_path}")


def _patch_multiplier_function(text: str, function_name: str, ticker: str, new_value: float) -> tuple[str, float]:
    func_match = re.search(rf"private static double {function_name}\(string tickerName\) \{{.*?\n        \}}\n", text, re.DOTALL)
    if not func_match:
        raise ValueError(f"Could not locate {function_name} in temalimit.cs")
    func_text = func_match.group(0)
    case_match = re.search(rf'(case "{ticker}":\s*\n\s*return )([\d.]+)(;)', func_text)
    if not case_match:
        raise ValueError(f'Could not locate case "{ticker}" inside {function_name}')
    old_value = float(case_match.group(2))
    new_func_text = func_text[:case_match.start()] + case_match.group(1) + f"{new_value:.6f}" + case_match.group(3) + func_text[case_match.end():]
    return text[:func_match.start()] + new_func_text + text[func_match.end():], old_value


def _patch_tier2_target(text: str, ticker: str, is_ladder: bool, new_value: float) -> tuple[str, float]:
    func_match = re.search(r"private static double Tier2Target\(string tickerName, bool isLadderDaily\) \{.*?\n        \}\n", text, re.DOTALL)
    if not func_match:
        raise ValueError("Could not locate Tier2Target in temalimit.cs")
    func_text = func_match.group(0)
    case_match = re.search(rf'(case "{ticker}":\s*\n\s*return isLadderDaily \? )([\d.]+)( : )([\d.]+)(;)', func_text)
    if not case_match:
        raise ValueError(f'Could not locate case "{ticker}" inside Tier2Target')
    ladder_str, risk_str = case_match.group(2), case_match.group(4)
    old_value = float(ladder_str if is_ladder else risk_str)
    if is_ladder:
        replacement = f"{case_match.group(1)}{new_value}{case_match.group(3)}{risk_str}{case_match.group(5)}"
    else:
        replacement = f"{case_match.group(1)}{ladder_str}{case_match.group(3)}{new_value}{case_match.group(5)}"
    new_func_text = func_text[:case_match.start()] + replacement + func_text[case_match.end():]
    return text[:func_match.start()] + new_func_text + text[func_match.end():], old_value


def _patch_es_ticks_per_template(text: str, new_ticks: int) -> tuple[str, float]:
    match = re.search(r"(private const double EsTier2TicksPerTemplate = )([\d.]+)(;)", text)
    if not match:
        raise ValueError("Could not locate EsTier2TicksPerTemplate in temalimit.cs")
    old_value = float(match.group(2))
    new_text = text[:match.start()] + match.group(1) + f"{float(new_ticks)}" + match.group(3) + text[match.end():]
    return new_text, old_value


def _patch_pullback_ratio(text: str, ticker: str, tier_group: str, new_value: float) -> tuple[str, float]:
    func_match = re.search(r"private static int PullbackTicksForTicker\(string tickerName, int templateNumber\) \{.*?\n        \}\n", text, re.DOTALL)
    if not func_match:
        raise ValueError("Could not locate PullbackTicksForTicker in temalimit.cs")
    func_text = func_match.group(0)
    if ticker in ("ES", "RTY", "YM"):
        pattern = rf'(tickerName == "{ticker}" \? \(lowTier \? )([\d.]+)( : )([\d.]+)(\))'
    else:
        pattern = r'(: \(lowTier \? )([\d.]+)( : )([\d.]+)(\); // NQ)'
    match = re.search(pattern, func_text)
    if not match:
        raise ValueError(f'Could not locate pullback ratio for "{ticker}" inside PullbackTicksForTicker')
    low_str, high_str = match.group(2), match.group(4)
    old_value = float(low_str if tier_group == "low" else high_str)
    # 4 decimal places, not 2 -- .2f previously caused a real infinite-reapply loop: 0.175 at .2f
    # rounds to "0.17" (binary float representation), so the stored value never actually reaches
    # the target and every subsequent run "applies" the identical no-op change forever. Found
    # 2026-07-17 in production after ~5 scheduled runs kept re-writing YM's ratio.
    if tier_group == "low":
        replacement = f"{match.group(1)}{new_value:.4f}{match.group(3)}{high_str}{match.group(5)}"
    else:
        replacement = f"{match.group(1)}{low_str}{match.group(3)}{new_value:.4f}{match.group(5)}"
    new_func_text = func_text[:match.start()] + replacement + func_text[match.end():]
    return text[:func_match.start()] + new_func_text + text[func_match.end():], old_value


def _patch_gate_constant(text: str, constant: str, new_value) -> tuple[str, float]:
    """Rewrites one of the entry-gate auto-adjust constants (MfiGateWiden*/RsiGateWiden*/
    StochGateWiden*/EntryExpireExtraMinutes*). Doubles always write 4 decimal places -- same lesson
    as _patch_pullback_ratio's .2f infinite-reapply loop."""
    match = re.search(rf"(private const (double|int) {constant} = )(-?[\d.]+)(;)", text)
    if not match:
        raise ValueError(f"Could not locate {constant} in temalimit.cs")
    old_value = float(match.group(3))
    formatted = str(int(round(new_value))) if match.group(2) == "int" else f"{new_value:.4f}"
    new_text = text[:match.start()] + match.group(1) + formatted + match.group(4) + text[match.end():]
    return new_text, old_value


def _current_tier_params(ticker: str, is_ladder: bool) -> tuple[float, float]:
    """(tier1 multiplier, tier2 param) currently live for this (ticker, role), read from the
    template risk map the same way _resimulate_and_check derives its non-overridden halves."""
    risk_map = build_template_risk_map()
    field = "ladderDaily" if is_ladder else "risk1R"
    t1 = (risk_map.get(ticker, {}).get("1") or {}).get(field)
    tier1_mult = t1 / UNIVERSAL_BASE if t1 else 0.0
    if ticker == "ES":
        return tier1_mult, 1.0  # EsTier2TicksPerTemplate; only meaningful for ES
    t40 = (risk_map.get(ticker, {}).get("40") or {}).get(field)
    return tier1_mult, (t40 if t40 else 0.0)


def _curve_violation(ticker: str, is_ladder: bool, tier1_mult_override: float | None, tier2_param_override: float | None) -> str | None:
    """Rebuilds the full 40-template curve for the edited (ticker, role) using current values for
    whichever half wasn't edited, plus the other three instruments' live reference values, and
    checks every invariant established when this formula was built. Returns None if safe, or a
    one-line reason -- no logging, so the tier-2 clamp search below can probe candidates quietly."""
    field = "ladderDaily" if is_ladder else "risk1R"
    current_mult, current_param = _current_tier_params(ticker, is_ladder)
    tier1_mult = tier1_mult_override if tier1_mult_override is not None else current_mult
    tier2_param = tier2_param_override if tier2_param_override is not None else current_param
    edited_curve = simulate_curve(ticker, tier1_mult, tier2_param)

    if not curve_is_safe(edited_curve):
        return f"resimulated {ticker} {field} curve has repeats or isn't strictly increasing."

    risk_curves, ladder_curves = {}, {}
    for tk in ("NQ", "ES", "RTY", "YM"):
        risk_curves[tk] = edited_curve if (tk == ticker and not is_ladder) else _current_curve_from_reference(tk, "risk1R")
        ladder_curves[tk] = edited_curve if (tk == ticker and is_ladder) else _current_curve_from_reference(tk, "ladderDaily")

    if not ordering_is_safe(risk_curves, ladder_curves):
        return f"resimulated {ticker} {field} curve would break NQ>ES>RTY>YM ordering or LadderDaily<Risk1R."

    return None


def _resimulate_and_check(ticker: str, is_ladder: bool, tier1_mult_override: float | None, tier2_param_override: float | None) -> bool:
    violation = _curve_violation(ticker, is_ladder, tier1_mult_override, tier2_param_override)
    if violation:
        _log(f"  REJECTED: {violation}")
        return False
    return True


def _clamp_tier2_target(ticker: str, is_ladder: bool, proposed: float) -> float | None:
    """The evidence's tier-2 target can sit past what the curve invariants allow (e.g. below the
    fixed tier-1 endpoint, or under another instrument's curve). Rather than rejecting 100% of the
    evidence every run forever, binary-search the safe value closest to the proposal between it and
    the current (known-safe) value -- a bounded partial step, same convergence philosophy as
    GATE_MAX_STEP. Returns a safe target rounded to 2 decimals, or None if even the current value's
    neighborhood fails (shouldn't happen -- current is live)."""
    _, current = _current_tier_params(ticker, is_ladder)
    if not current or _curve_violation(ticker, is_ladder, None, current) is not None:
        return None
    bad, good = proposed, current
    for _ in range(60):
        mid = (bad + good) / 2
        if _curve_violation(ticker, is_ladder, None, mid) is None:
            good = mid
        else:
            bad = mid
    # Round toward the safe side (away from the violation boundary) and re-verify.
    for candidate in (round(good, 2), round(good + (0.01 if good > bad else -0.01), 2)):
        if _curve_violation(ticker, is_ladder, None, candidate) is None:
            return candidate
    return None


def _tier1_anchor(ticker: str, is_ladder: bool) -> float:
    """Dollar value of the fixed T19 endpoint the tier-2 segment interpolates away from."""
    tier1_mult, _ = _current_tier_params(ticker, is_ladder)
    return round_dollars_to_whole_ticks(UNIVERSAL_BASE * template_multiplier(TIER1_MAX_TEMPLATE) * tier1_mult, ticker)


def _clamp_es_ticks(is_ladder: bool, proposed: int) -> int | None:
    """Integer analogue of _clamp_tier2_target for EsTier2TicksPerTemplate: walk from the proposal
    toward the current (known-safe) constant one tick at a time and take the first safe value.
    Returns None if none short of the current value passes (a no-op isn't worth proposing)."""
    match = re.search(r"private const double EsTier2TicksPerTemplate = ([\d.]+);", _read_cs())
    if not match:
        return None
    current = int(float(match.group(1)))
    step = 1 if proposed < current else -1
    for candidate in range(proposed + step, current, step):
        if _curve_violation("ES", is_ladder, None, float(candidate)) is None:
            return candidate
    return None


def _try_apply(text: str, description: str, patch_fn, *patch_args, min_step: float | None = None) -> tuple[str, bool, str | None, float | None]:
    """Runs patch_fn(text, *patch_args) -> (new_text, old_value); reverts and logs instead of
    applying if the resulting change is within display-rounding noise (see
    _materially_different). Returns (possibly-updated text, whether it was actually applied,
    a one-line summary of the change for the compile-notification file, and old_value -- both
    None if not applied) -- old_value lets callers build a history entry without re-parsing.

    min_step: pass for integer-quantized constants (expire whole minutes) so the materiality
    check uses an absolute one-unit floor rather than the 5% relative band (which stalls large
    expire values below their cap -- see _materially_different)."""
    new_value = patch_args[-1]
    try:
        new_text, old_value = patch_fn(text, *patch_args)
    except ValueError as error:
        _log(f"  SKIPPED (could not apply safely): {error}")
        return text, False, None, None
    if not _materially_different(old_value, new_value, min_step):
        if float(old_value) == float(new_value):
            _log(f"  SKIPPED (already applied: current value is exactly {old_value})")
        elif min_step is not None:
            _log(f"  SKIPPED (below {min_step:g}-unit step: current={old_value}, suggested={new_value})")
        else:
            _log(f"  SKIPPED (within {MATERIAL_CHANGE_THRESHOLD:.0%} dead-band: current={old_value}, suggested={new_value} -- too small to act on)")
        return text, False, None, None
    summary = f"{description} {old_value} -> {new_value}"
    _log(f"  applied: {summary}")
    return new_text, True, summary, old_value


def _write_compile_notification(summaries: list[str]) -> None:
    lines = [f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} auto_apply_sizing.py changed:"]
    lines += [f"  {s}" for s in summaries]
    try:
        COMPILE_NOTIFICATION_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as error:
        _log(f"  WARNING: could not write compile notification file: {error}")


def _history_entry(kind: str, old_value: float, new_value: float, **keys) -> dict:
    """One record for HISTORY_PATH. `kind` + `keys` together are the lookup key
    live_dashboard_server.py's _history_index() joins dashboard rows against -- see the comment
    on HISTORY_PATH for which fields each dashboard table matches on."""
    entry = {"type": kind, "old": old_value, "new": new_value, "timestamp": datetime.now().isoformat(timespec="seconds")}
    entry.update(keys)
    return entry


def _append_history(entries: list[dict]) -> None:
    """Atomic replace (see _atomic_write) so the 8766 server never reads a torn file. A concurrent
    manual --apply run racing the scheduled task can still lose the other run's entries (classic
    read-modify-write); Task Scheduler's default don't-start-second-instance policy covers the
    scheduled-vs-scheduled case, so that residual window is manual-only and accepted."""
    if not entries:
        return
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8")) if HISTORY_PATH.exists() else []
    except (OSError, ValueError):
        history = []
    history.extend(entries)
    history = history[-HISTORY_MAX_ENTRIES:]
    try:
        _atomic_write(HISTORY_PATH, json.dumps(history, indent=1))
    except OSError as error:
        _log(f"  WARNING: could not write history file: {error}")


def run(apply_changes: bool) -> None:
    _log(f"=== auto_apply_sizing check started (mode={'APPLY' if apply_changes else 'DRY-RUN'}) ===")

    sizing_findings = _reconcile_es_tier2(check_sizing_agreement())
    pullback_findings = check_pullback_agreement()
    gate_findings, expire_findings = check_entry_gate_agreement()
    slippage_finding = check_slippage_agreement()
    atr_clamp_finding = check_atr_clamp_agreement()

    if not sizing_findings and not pullback_findings and not gate_findings and not expire_findings and not slippage_finding and not atr_clamp_finding:
        _log("No curves/tiers have unanimous REASSESS-tier agreement right now. Nothing to do.")
        return

    text = _read_cs()
    original = text
    applied = 0
    applied_summaries: list[str] = []
    applied_history: list[dict] = []
    suppress_state = _load_suppress_state()
    suppressed = 0

    for f in sizing_findings:
        is_ladder = f["role"] == "ladderDaily"
        function_name = "LadderMultiplier" if is_ladder else "InstrumentMultiplier"
        _begin_capture()
        ok = False
        if f["tier"] == "tier1":
            _log(f"SIZING {f['ticker']} {f['role']} (Tier 1): direction={f['direction']} newMultiplier={f['newMultiplier']} "
                 f"(supported by templates {f['supportingTemplates']})")
            if apply_changes and _resimulate_and_check(f["ticker"], is_ladder, f["newMultiplier"], None):
                text, ok, summary, old_value = _try_apply(text, f'{function_name}("{f["ticker"]}")', _patch_multiplier_function, function_name, f["ticker"], f["newMultiplier"])
                applied += int(ok)
                if summary:
                    applied_summaries.append(summary)
                if ok:
                    applied_history.append(_history_entry("sizing", old_value, f["newMultiplier"], ticker=f["ticker"], role=f["role"], tier="tier1"))
        elif f["ticker"] == "ES":
            target_ticks = f["newEsTicksPerTemplate"]
            _log(f"SIZING {f['ticker']} {f['role']} (Tier 2): direction={f['direction']} newEsTicksPerTemplate={target_ticks} "
                 f"(supported by templates {f['supportingTemplates']})")
            if apply_changes:
                violation = _curve_violation(f["ticker"], is_ladder, None, float(target_ticks))
                if violation:
                    clamped_ticks = _clamp_es_ticks(is_ladder, target_ticks)
                    if clamped_ticks is None:
                        _log(f"  REJECTED: {violation}")
                    else:
                        _log(f"  CLAMPED: evidence wants {target_ticks} ticks/template but invariants stop at {clamped_ticks}; applying the largest safe step.")
                        target_ticks = clamped_ticks
                        violation = None
                if violation is None:
                    text, ok, summary, old_value = _try_apply(text, "EsTier2TicksPerTemplate", _patch_es_ticks_per_template, target_ticks)
                    applied += int(ok)
                    if summary:
                        applied_summaries.append(summary)
                    if ok:
                        # EsTier2TicksPerTemplate is one constant shared by Risk1R AND LadderDaily, so
                        # record it under both roles (or only the finding's role's dashboard rows would
                        # show it) and carry a unit -- the value is ticks per template, not dollars like
                        # the columns it renders next to.
                        for role in ("risk1R", "ladderDaily"):
                            applied_history.append(_history_entry("sizing", old_value, target_ticks, ticker=f["ticker"], role=role, tier="tier2", unit="ticks/tmpl"))
        else:
            target = f["newTier2Target"]
            _log(f"SIZING {f['ticker']} {f['role']} (Tier 2): direction={f['direction']} newTier2Target={target} "
                 f"(supported by templates {f['supportingTemplates']})")
            if apply_changes:
                violation = _curve_violation(f["ticker"], is_ladder, None, target)
                if violation:
                    anchor = _tier1_anchor(f["ticker"], is_ladder)
                    clamped = _clamp_tier2_target(f["ticker"], is_ladder, target)
                    if clamped is None:
                        _log(f"  REJECTED: {violation}")
                    else:
                        note = (f" (evidence target {target} sits below the fixed T{TIER1_MAX_TEMPLATE} endpoint {anchor} -- "
                                f"a full move needs tier-1 evidence)") if target < anchor else ""
                        _log(f"  CLAMPED: evidence wants {target} but curve invariants floor it at {clamped}; applying the largest safe step.{note}")
                        target = clamped
                        violation = None
                if violation is None:
                    text, ok, summary, old_value = _try_apply(text, f'Tier2Target("{f["ticker"]}", isLadderDaily={is_ladder})', _patch_tier2_target, f["ticker"], is_ladder, target)
                    applied += int(ok)
                    if summary:
                        applied_summaries.append(summary)
                    if ok:
                        applied_history.append(_history_entry("sizing", old_value, target, ticker=f["ticker"], role=f["role"], tier="tier2"))
        suppressed += int(not _emit_finding(_end_capture(), ok, suppress_state))

    for f in pullback_findings:
        _begin_capture()
        ok = False
        _log(f"PULLBACK {f['ticker']} {f['tierGroup']} tier: direction={f['direction']} newRatio={f['newRatio']} "
             f"(supported by templates {f['supportingTemplates']})")
        if apply_changes:
            text, ok, summary, old_value = _try_apply(text, f'PullbackTicksForTicker("{f["ticker"]}", {f["tierGroup"]} tier)', _patch_pullback_ratio, f["ticker"], f["tierGroup"], f["newRatio"])
            applied += int(ok)
            if summary:
                applied_summaries.append(summary)
            if ok:
                applied_history.append(_history_entry("pullback", old_value, f["newRatio"], ticker=f["ticker"], tierGroup=f["tierGroup"]))
        suppressed += int(not _emit_finding(_end_capture(), ok, suppress_state))

    for f in gate_findings:
        _begin_capture()
        ok = False
        _log(f"ENTRY GATE {f['gate']} {f['tierGroup']}: direction={f['direction']} {f['constant']} -> {f['newWiden']} "
             f"(supported by tickers {f['supportingTickers']})")
        if apply_changes:
            text, ok, summary, old_value = _try_apply(text, f['constant'], _patch_gate_constant, f['constant'], f['newWiden'])
            applied += int(ok)
            if summary:
                applied_summaries.append(summary)
            if ok:
                applied_history.append(_history_entry("gate", old_value, f["newWiden"], gate=f["gate"], tierGroup=f["tierGroup"]))
        suppressed += int(not _emit_finding(_end_capture(), ok, suppress_state))

    for f in expire_findings:
        _begin_capture()
        ok = False
        _log(f"ENTRY EXPIRE {f['tierGroup']}: direction={f['direction']} {f['constant']} -> {f['newExtraMinutes']} "
             f"(supported by tickers {f['supportingTickers']})")
        if apply_changes:
            text, ok, summary, old_value = _try_apply(text, f['constant'], _patch_gate_constant, f['constant'], f['newExtraMinutes'], min_step=1)
            applied += int(ok)
            if summary:
                applied_summaries.append(summary)
            if ok:
                applied_history.append(_history_entry("expire", old_value, f["newExtraMinutes"], tierGroup=f["tierGroup"]))
        suppressed += int(not _emit_finding(_end_capture(), ok, suppress_state))

    if slippage_finding:
        f = slippage_finding
        _begin_capture()
        ok = False
        _log(f"SLIPPAGE RESERVE: direction={f['direction']} SlippageReserveRatio -> {f['newRatio']} "
             f"(supported by tickers {f['supportingTickers']})")
        if apply_changes:
            text, ok, summary, old_value = _try_apply(text, "SlippageReserveRatio", _patch_gate_constant, "SlippageReserveRatio", f["newRatio"])
            applied += int(ok)
            if summary:
                applied_summaries.append(summary)
            if ok:
                applied_history.append(_history_entry("slippage", old_value, f["newRatio"], param="ratio"))
        suppressed += int(not _emit_finding(_end_capture(), ok, suppress_state))

    if atr_clamp_finding:
        f = atr_clamp_finding
        _begin_capture()
        ok = False
        _log(f"ATR CLAMP FLOOR: direction={f['direction']} AtrClampMin -> {f['newMin']} "
             f"(supported by tickers {f['supportingTickers']})")
        if apply_changes:
            text, ok, summary, old_value = _try_apply(text, "AtrClampMin", _patch_gate_constant, "AtrClampMin", f["newMin"])
            applied += int(ok)
            if summary:
                applied_summaries.append(summary)
            if ok:
                applied_history.append(_history_entry("atrclamp", old_value, f["newMin"], param="min"))
        suppressed += int(not _emit_finding(_end_capture(), ok, suppress_state))

    _save_suppress_state(suppress_state)
    repeat_note = f" ({suppressed} repeat finding(s) suppressed -- identical to earlier today)" if suppressed else ""

    if apply_changes and applied > 0:
        _write_cs(text, original)
        _write_compile_notification(applied_summaries)
        _append_history(applied_history)
        _log(f"Wrote temalimit.cs with {applied} change(s){repeat_note}. NinjaScript editor will recompile automatically if it's open.")
    elif apply_changes:
        _log(f"Nothing was actually applied (all findings were skipped/rejected, or none had a safe patch target){repeat_note}.")
    else:
        _log(f"Dry run: {len(sizing_findings)} sizing finding(s), {len(pullback_findings)} pullback finding(s), "
             f"{len(gate_findings)} entry-gate finding(s), {len(expire_findings)} expire finding(s), "
             f"{1 if slippage_finding else 0} slippage finding(s), {1 if atr_clamp_finding else 0} ATR-clamp finding(s){repeat_note} -- rerun with --apply to write them.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Show what would change without writing anything.")
    group.add_argument("--apply", action="store_true", help="Actually edit temalimit.cs.")
    args = parser.parse_args()
    run(apply_changes=args.apply)
