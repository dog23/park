# Auto-tuning — the "Reassess" automations, in detail

Separate from the ML models: [`dashboards/auto_apply_sizing.py`](dashboards/auto_apply_sizing.py) is a background script that watches `temalimit`'s own live-trade evidence and, when the evidence is strong and unanimous enough, **edits `temalimit.cs`'s constants itself** — no ML involved, just statistics over recent fills/misses. This is what the dashboard's **Reassess Activity**, **Sizing Reassess**, and **Entry Gate Reassess** cards show.

This page goes constant-by-constant: what's measured, the exact formula that turns a measurement into a suggestion, and how five separate suggestions get reduced to one safe edit.

---

## The pipeline (same shape for all five constants)

```
1. MEASURE   — read the evidence log/TSV for one bucket (ticker × template-tier, or gate × tier)
2. TIER      — too_few → monitor → reassess, based on sample count (almost always ≥5, see below)
3. SUGGEST   — a direction (e.g. widen/tighten, increase/decrease) + a proposed new value, per bucket
4. UNANIMITY — collect the direction from every "reassess"-tier bucket sharing the constant;
               if they don't all agree, or none qualify, nothing happens
5. REDUCE    — average the supporting buckets' suggested deltas into one number
6. STEP-CAP  — clamp that averaged delta to a fixed per-run maximum
7. CEILING   — clamp the resulting new value to a fixed valid range
8. (sizing only) RESIMULATE — rebuild all 40 templates' dollar values and check curve invariants
9. WRITE     — patch temalimit.cs, back it up first, log the change (free-text + structured)
```

Steps 2–3 live in [`live_dashboard_server.py`](live_dashboard_server.py) (the same functions that render the dashboard cards); steps 4–9 live in [`auto_apply_sizing.py`](auto_apply_sizing.py). Because the *measurement and display* code is identical to the *auto-apply* code, what you see on the dashboard is exactly what the automation is deciding from — there's no separate, hidden calculation.

---

## 1. Position sizing — `InstrumentMultiplier` / `LadderMultiplier` (Tier 1), `Tier2Target` / `EsTier2TicksPerTemplate` (Tier 2)

**The curve being tuned.** `temalimit.cs`'s `TieredDollarValue` formula gives each of the 40 templates a dollar risk value:

```
template_multiplier(t) = 1 + 0.041667 × (t − 1)          # linear ramp, t = 1..40 (clamped)
Tier 1 (t ≤ 19):  value = round_to_ticks(UNIVERSAL_BASE × template_multiplier(t) × tier1_multiplier)
Tier 2 (t > 19):
  ES:            value = tier1_end + (t − 19) × EsTier2TicksPerTemplate × $12.50/tick
  NQ/RTY/YM:     value = tier1_end + (Tier2Target − tier1_end) × (t − 19) / (40 − 19)     # linear interpolation
```
`UNIVERSAL_BASE = $3000`. ES steps by a fixed **tick count per template** (a slope); the other three instruments interpolate straight-line to a **dollar target** at template 40.

**What's measured.** `build_ladder_trail_diagnosis()` compares each bucket's *realized* slippage/risk against what the curve predicted, per (ticker, role ∈ {risk1R, ladderDaily}, template).

**Suggestion → constant, by tier:**
- **Tier 1** — for each of the ≤19 templates in `reassess` tier, invert the formula: `implied_multiplier = suggested_dollar_value / (UNIVERSAL_BASE × template_multiplier(t))`. The new `InstrumentMultiplier`/`LadderMultiplier` is the **mean of the implied multipliers** across every agreeing template.
- **Tier 2, ES** — invert the slope: `implied_ticks_per_template = (suggested − tier1_end) / ((t − 19) × $12.50)`, averaged across agreeing templates → new `EsTier2TicksPerTemplate` (rounded to a whole tick, minimum 1).
- **Tier 2, NQ/RTY/YM** — invert the interpolation for the new endpoint: `implied_target = tier1_end + (suggested − tier1_end) / ((t − 19) / 21)`, averaged → new `Tier2Target`.

**Unanimity** runs **per ticker, separately for Tier 1 and Tier 2** — every reassess-tier template within that tier must agree on direction (over/under-sized), or nothing is proposed for that tier.

**Before writing:** the proposed multiplier/target is resimulated across **all 40 templates** and checked against the same invariants the curve was designed with — strictly increasing values, no two templates landing on the same dollar figure, `LadderDaily < Risk1R` at every template, and `NQ > ES > RTY > YM` at every template (checked against the other three, currently-unedited instruments' live values). A change that would violate any of these is discarded, not written.

---

## 2. Entry-gate widen — `MfiGateWiden*` / `RsiGateWiden*` / `StochGateWiden*`

**What's measured** (`gate_reassess()`, per ticker × tier-group × gate, 5-day rolling window): for every setup the gate *blocked*, the **gap** — the smallest amount the gate's threshold would have needed to move for that bar to have passed.

**Widen** (data-derived): once a bucket has **≥5 measured gaps** and their average is positive, the suggested delta *is* that average gap — `suggestedWidenDelta = avg_gap`. No heuristic; the evidence directly states how far to move.

**Tighten** (heuristic drift back toward the designed table): only proposed when the gate produced **zero blocks** across ≥5 fills *and* the widen is currently > 0 — `delta = −0.25 × current_widen`. This is a cautious drift, not a measurement — there's no way to measure "how much could we have tightened," so it nudges back 25% at a time and lets new evidence confirm or reverse it.

**Reduction across instruments:** the widen/tighten constants are **shared across all four tickers** (unlike sizing, which is per-ticker). So unanimity runs across tickers within each (gate, tier-group): every reassess-tier ticker must agree on direction, and the applied delta is the **mean of the supporting tickers' deltas**, capped at a per-run step (`GATE_MAX_STEP`: MFI/RSI 5.0 points, StochRSI 0.05), then clamped to `[0, GATE_MAX_TOTAL_WIDEN]` (MFI/RSI 15.0, StochRSI 0.15).

**Why a 5-day rolling window, not "all":** a tighten needs a bucket with *zero* blocks — under an all-time window, one historical block would veto tightening forever. A **last-applied cutoff** also excludes evidence rows from before the previous apply, so the same measured gap can't get re-applied every run (which would ratchet a widen to its ceiling in a handful of runs).

---

## 3. Expire extras — `EntryExpireExtraMinutes*`

**What's measured** (`expire_reassess()`, per ticker × tier-group): for every order that was cancelled at expiry but whose price would still have touched it if the watch window had run longer, `neededExtra` — how many minutes past expiry the touch actually came.

**Increase** (data-derived): with **≥5 measured touches**, the suggested delta is the **75th percentile** of `neededExtra` (rounded up, capped to **1–10 minutes per step**) — biased toward covering most of the late touches rather than just the average one.

**Decrease** (heuristic): only when a **full sample (≥5) of complete watches** produced **zero** touches and the current extra is > 0 — `delta = −1` minute. Only *full* watches count (one truncated early by the next cancel never had the chance to prove a late touch wouldn't come), so a partial-watch streak can't drive a decrease.

**Reduction:** same shared-across-tickers unanimity and mean-of-deltas as the gate widen, clamped to `[0, EXPIRE_MAX_TOTAL_EXTRA = 24]` minutes (the strategy separately hard-clamps effective expiry to 30 minutes regardless).

---

## 4. Pullback ratio (ATR-bound pullback ticks, per tier group ≤17 / ≥18)

**What's measured**: for each no-fill event, `missedByTicks` — how many ticks short the pullback fell of what would have filled.

**Decrease** (data-derived): with **≥5 real measured misses** (not just no-fill *events* — older rows predate the `missedByTicks` field, so a bucket with 19 no-fills but only 1 measured miss does **not** qualify), `suggested = max(1, current_ticks − ceil(avg_missed))`.

**Increase** (heuristic): only when there's **at most 1** no-fill in the window and **≥5** fills — `suggested = current_ticks + max(1, round(current_ticks × 0.15))`, i.e. widen by 15%, minimum 1 tick.

Unlike sizing/gates, the pullback ratio is **flat per tier group**, not a 40-point curve — so there's no resimulation step here, just the tier's single value.

---

## 5. `AtrClampMin` (the volatility floor)

**What's measured** (5-day rolling): among no-fill events where the pullback was floor-bound (clamped at `AtrClampMin` rather than the raw ATR-derived value), the **raw ratio** that would have actually filled.

**Decrease** (data-derived): **p25** (25th percentile) of those raw ratios, averaged across agreeing tickers.

**Increase** (heuristic drift back toward the designed 0.50): only when there are **zero** floor-bound no-fills in the window for that ticker.

**Reduction:** per-run step cap **0.10**, final value clamped to **[0.20, 0.50]**. `AtrClampMax` (the ceiling) is intentionally **not** automated.

---

## Sample-size gate, formalized

Every bucket needs a minimum number of *real, measured* data points before it can reach `reassess` tier — below that it's `too_few` (nothing yet) or `monitor` (some signal, not enough to trust):

| Domain | Constant | Threshold |
|---|---|---|
| Sizing | `MIN_SAMPLES_FOR_REASSESS` | 5 |
| Pullback ratio | `MIN_SAMPLES_FOR_PULLBACK_REASSESS` | 5 |
| Entry gate / expire / ATR clamp | `MIN_SAMPLES_FOR_GATE_REASSESS` | 5 |

## Unanimity, formalized

`curve_unanimous_direction()` collects the `direction` field from every bucket at `reassess` tier for a given group. If that set has **exactly one member**, that's the consensus direction; if it's empty (nothing qualifies) or has **two or more distinct directions**, the function returns `None` and **nothing is proposed for that constant this run.** A `reassess`-tier bucket with no direction (its target is too close to current to bother suggesting anything) doesn't block consensus — only a genuine *opposing* direction does.

## Evidence windows & re-application

Two mechanisms stop the automation from compounding the same evidence run after run:
- **A 5-day rolling window** (not all-time) for gates/expire/ATR-clamp, so old evidence ages out and a decrease/tighten drift is actually reachable.
- **Last-applied cutoffs** (`_evidence_cutoffs`) — evidence rows timestamped before the most recent apply for that exact bucket are excluded, so the same measured gap or slippage doesn't get re-applied every run. Without this, a widen could ratchet to its ceiling in ~3 runs off one persistent (already-addressed) gap.

## Safety net around all five

- **Backup before every edit** — a timestamped copy of `temalimit.cs` to `temalimit_auto_apply_backups/`.
- **Full audit trail** — every check (whether or not it changed anything) appended to `auto_apply_sizing.log`; every applied edit appended, structured, to `auto_apply_history.json` (what feeds the dashboard's "old → new" columns and the one-time compile notification `temalimit.cs` prints after recompiling).
- **Hard ceilings everywhere** — every constant above has a fixed valid range it can never leave, regardless of what the evidence suggests.

---

## Running it

```bash
cd dashboards
python auto_apply_sizing.py --dry-run   # shows what it would change, touches nothing
python auto_apply_sizing.py --apply     # applies unanimous, gate-passing changes
```
Meant to be scheduled (e.g. every few minutes) once you trust what it proposes — see [infrastructure/scheduled-tasks/TASKS.md](infrastructure/scheduled-tasks/TASKS.md) for how the rest of the automation is registered.

## Where to watch it

The **live dashboard** (:8766) — the **Sizing Reassess**, **Entry Gate Reassess**, and **No-Fill Log** cards show each bucket's current tier, the supporting evidence, and (once something is applied) the old → new value. **Reassess Activity** is the combined feed of recent findings and outcomes across all five constants.
