# Auto-tuning — the "Reassess" automations

Separate from the ML models: [`dashboards/auto_apply_sizing.py`](dashboards/auto_apply_sizing.py) is a background script that watches `temalimit`'s own live-trade evidence and, when the evidence is strong and unanimous enough, **edits `temalimit.cs`'s constants itself** — no ML involved, just statistics over recent fills/misses. This is what the dashboard's **Reassess Activity**, **Sizing Reassess**, and **Entry Gate Reassess** cards show.

## What it tunes

| Constant | Evidence it watches | What "wrong" looks like |
|----------|---------------------|--------------------------|
| Position sizing (`InstrumentMultiplier` / `LadderMultiplier`, Tier 1; `Tier2Target` / `EsTier2TicksPerTemplate`, Tier 2) | Trade slippage and risk-sizing logs | Realized risk consistently drifting from the sizing curve's target |
| Entry-gate widen (`MfiGateWiden*` / `RsiGateWiden*` / `StochGateWiden*`) | `TemaLimit_gateblock_log.tsv` | Setups repeatedly blocked at the gate that would have gone on to fill |
| Expire extras (`EntryExpireExtraMinutes*`) | `TemaLimit_expire_log.tsv` (post-cancel touch watch) | Orders expiring just before price would have touched them |
| Pullback ratio (per tier group) | `TemaLimit_nofill_log.tsv` / pullback state | Orders consistently missing fills by a measurable, correctable margin |
| `AtrClampMin` | 5-day rolling ATR-clamp stats | The volatility floor sitting away from where fills actually cluster |

## The safety model

This is the part worth reading closely, because it's what keeps a script that edits live trading code from doing something reckless:

1. **Every constant is split into buckets** (per ticker × template-tier, or per gate × tier for the shared entry-gate constants). Each bucket needs **at least 5 real samples** before it's even eligible to suggest a change — below that it's `too_few` or `monitor`, not `reassess`.
2. **Unanimous agreement required.** A constant only moves if **every bucket that shares it** is at `reassess` tier *and* agrees on direction. One noisy, thin-sample bucket can't drag 20+ templates in a direction the rest disagree with — see the design note in the module docstring: this was chosen over "first bucket wins" or a weighted blend specifically to avoid that failure mode.
3. **Hard ceilings and per-run step caps.** Entry-gate widens are capped in total (`GATE_MAX_TOTAL_WIDEN`: MFI/RSI 15, StochRSI 0.15) and per run (`GATE_MAX_STEP`); expire extras cap at 24 minutes total; the pullback ratio and ATR-clamp floor step by a small fixed amount per run (0.05 and 0.10 respectively) and are clamped to a valid range — no single run can swing a constant far.
4. **Full resimulation before writing.** For sizing changes, all 40 templates are resimulated with the proposed constant and checked against the same invariants the sizing formula was built with (strictly increasing, no repeated dollar values, `LadderDaily < Risk1R`, and the NQ > ES > RTY > YM ordering) — a change that would break the curve is never written.
5. **Backup before every edit.** A timestamped copy of `temalimit.cs` is written to `temalimit_auto_apply_backups/` before any change lands.
6. **Full audit trail.** Every check (whether or not it changed anything) is appended to `auto_apply_sizing.log`; every applied edit is appended, structured, to `auto_apply_history.json` — that's what powers the "old → new" column on the dashboard's Reassess cards, and what `temalimit.cs` reads once (then deletes) to print a one-time compile notification of what just changed.

## Running it

```bash
cd dashboards
python auto_apply_sizing.py --dry-run   # shows what it would change, touches nothing
python auto_apply_sizing.py --apply     # applies unanimous, gate-passing changes
```
It's meant to be scheduled (e.g. every few minutes) once you trust what it proposes — see [infrastructure/scheduled-tasks/TASKS.md](infrastructure/scheduled-tasks/TASKS.md) for how the rest of the automation is registered.

## Where to watch it

The **live dashboard** (:8766) — the **Sizing Reassess**, **Entry Gate Reassess**, and **No-Fill Log** cards show each bucket's current tier, the supporting evidence, and (once something is applied) the old → new value. **Reassess Activity** is the combined feed of recent findings and outcomes across all of the above.
