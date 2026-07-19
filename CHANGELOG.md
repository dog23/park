# Patch Notes

Plain-English patch notes for the `temalimit` strategy and the rest of the NT8 trading stack, written the way a game studio ships them: one patch per day, newest on top, with **New / UI / Balance / Fixed / Under the hood / Known issues** sections and *dev notes* where the reasoning matters. This used to live in the `soy` repo's README; `park` is now the single home for changelogs and wireframes/diagrams.

See [wireframes/](wireframes/) for diagrams (referenced inline below). Static reference material lives at the [bottom of this file](#reference-template-mode-3-rotation-logic) so the newest patch always stays on top.

---

## Patch 2026-07-19 — "Regime Change"

The market's July 16–17 selloff set off two data alarms. This patch teaches the alarms the difference between "the market changed" and "the data broke," fixes a stat freeze in background tabs, and adds a hardware monitor for the machine itself.

### ✨ New
- **Laptop hardware monitor.** A standalone background tool watches the machine's memory, pagefile pressure, CPU, and GPU every 15 seconds to answer one question: can this workload live on 64GB of RAM instead of 128GB? It only pings the phone on *lasting* changes — never brief spikes — and every alert ends with a plain verdict: "64GB likely safe," "64GB borderline," or "keep 128GB." Runs on its own notification channel, starts at login, restarts itself on failure. Completely independent of the trading system.
- **The data-integrity checks now also run themselves once a day**, right after the 14:00 model retrain — previously they only ran when someone clicked a button, so a real problem could go unnoticed indefinitely between manual checks. This is the one moment each day the training data actually changes, so it's the only moment a fresh check can find something new.
  *Dev note: prompted by a fair question — "if the Run button is always clickable, how would I know when to actually click it?" The clickable state was never meant to mean "due for a run"; now there's an actual daily run to rely on, and the panel's description spells out the difference.*
- **The "Data Mix" section now shows a live-vs-shadow split under every row** (Labels, Symbols, Data Series, Triggers), matching the breakdown already shown per-market on the Model Health tables. All four sections' splits check out exactly against the total sample count (394 live / 9,375 shadow / 9,769 total).
  *Dev note: user noticed this split existed on the per-market tables but not here and asked for parity. One caveat carried over from the existing per-market tables: clicking the "Count" column header to sort no longer sorts cleanly by the big number when a row has this extra live/shadow line underneath it — a pre-existing quirk this reuses rather than introduces, flagged for a future fix.*

### ⚖️ Balance (data alarms)
- The **label-drift alarm** now needs 40+ recent samples before it may go red; thinner evidence shows an amber note instead.
  *Dev note: two markets were "failing" on 13–17 recent rows. A 30-point swing on a dozen samples is noise, not a labeling break.*
- The **input-drift alarm** now files gauges that measure price level and volatility *by design* under an amber "regime drift" note; red is reserved for inputs that should stay stable in any market.
  *Dev note: once those gauges stopped hogging the report, it turned out the trend also shifted several genuinely stable inputs past the red line — so the alarm stays honestly red for now. Every check that would indicate actual data corruption passes.*
- Both alarm changes mirrored to the trend robot's dashboard (its oil-market label warning dropped from red to amber on the spot).
- The engineers' Ops page now polls the trade server every 5 seconds instead of 2 — the evidence tables only change on the minutes scale.

### 🐛 Fixed
- Animated counters (the big profit number, stat cards) froze at stale values while a dashboard tab sat in the background. They now update instantly whenever the tab isn't visible and only animate while you're actually watching.
- **The live dashboard's "Data Series" breakdown could show 0 trades no matter what time range you picked** — including "All time" — for an actively-traded strategy that simply hadn't traded in the last 24 hours (like over a weekend). Every other breakdown tab (Instrument, Direction, Session, Exit Signal) kept showing that same strategy's trades just fine, so only this one tab looked broken.
  *Dev note: an old "hide deprecated strategies" filter was being applied to Data Series before the time-range filter ever ran, so no range selection could undo it. Removed that filter from this one spot — it now follows the same rules as its sibling tabs. Confirmed fixed across All/5D/3D/2D/1D, matching the Instrument tab's counts exactly at every range.*

### 🧭 Known issues
- Two drift alarms remain honestly red (NQ 60-Range labels; several trend-geometry inputs): the market genuinely looks different after July 16–17. Expected to clear as new data accumulates — still red in a week means it's a real signal worth acting on.

---

## Patch 2026-07-18 — "Three Dashboards, One Product"

The big one: a full dashboard redesign around a single rule — **the live page is for anyone; the other pages are for engineers** — plus a data-honesty audit that caught two serious problems, and the discovery that the exit-AI had never learned anything, ever.

### ✨ New
- **Ops page** (port 8765/ops): all six auto-tuning evidence panels that used to crowd the live page (Reassess Activity, Template Coverage & Usage, No-Fill Log, ATR Pullback, Sizing Reassess, Entry Gate Reassess) now live in one engineers' room, fed live from the same data.
- **Verification Suite panel** on both AI servers: a row of on-demand integrity checks ("does the AI collapse to random guessing when we scramble the answers on purpose?", "are practice trades leaking into the exam pile?"), each with Pass/Warn/Fail and saved results. This suite is what caught both data-honesty bugs below.
- **Reassess Activity card**: day-by-day view of exactly what the automatic settings-editor found and decided, with All / Applied / Rejected / Skipped / Dry-run filters.
- **Active-profile table**: a live dashboard list of every running strategy copy (account, contract, chart type) and which settings profile it's on right now — no more opening individual chart logs.
- **Two rotation modes**: *Losers First* trades only currently-losing profiles starting with the biggest loser (a losing profile deserves another look before a winner is touched); *Winners First* is the mirror image. Both re-rank continuously off real profit/loss.

### 🎨 UI
- **Live page (8766) is now the splash page**: one big profit/loss number with the equity chart and time-range buttons under it, the trade lists, and a tabbed "Breakdown" card (market / direction / session / exit reason). A "Models" header chip answers "are the robots healthy?" with a green dot.
- **Models page (8765) leads with the verdict**: a checks-passing chip plus a red "Attention" bar that appears only when something is actually failing. Five distribution tables merged into one tabbed "Data Mix" card.
- **Trend page (8767) went card-first**: each market gets a status card with progress toward its 100-sample training gate; the giant 14-column table is one click away. Four "Recent…" tables merged into one tabbed "Activity" feed.
- **One visual language everywhere**: flat dark panels, no neon or glow, red/green reserved strictly for loss/win and fail/pass, no emoji. Every page links to the other three; restart/retrain moved into a "…" menu so they can't be fat-fingered.
- **Phones work now**: tables fold into label/value rows on small screens (Models and Trend previously had no phone layout at all), and the live page gets a thumb-reach bottom tab bar. Card dragging hides behind an explicit "Edit layout" button.
  *Dev note: nothing about trading logic, data collection, or the AI changed in the redesign — purely presentation.*

### ⚖️ Balance
- **Entry-price cushion restored** on the loosest dozen profiles per contract. A chain of small automatic adjustments had walked it down to essentially "fill immediately, no cushion" — those trades filled far more often and lost money on 3 of 4 contracts, while wider-cushion trades made money on all 4. Restored to intended sizes, with a safeguard so stale evidence can't walk it back down.

### 🐛 Fixed
- **~420 practice trades were filed under the wrong market.** The strategy juggles four markets in one program, and a bookkeeping gap let a trade opened while watching one market get recorded under another. All wrong-market records were purged (backups kept), affected models retrained, and two tripwires added — each practice trade now refuses to be recorded under any market but its own, and a "Cross-instrument bleed" check scans the whole pile on demand.
- **The AI's report card was too easy to ace.** A model that lazily always says "don't trade" scored near-perfect on quiet stretches — one such model had earned veto power and silently blocked 12,000+ potential S&P entries in three days. Grading now requires beating the "always say don't-trade" score by a real margin on a test with enough genuine buy/sell moments. Today zero models hold veto power; every market trades on plain technical signals until a model honestly earns it.
- **The exit-AI had never learned anything — ever.** It logged 185,000+ "still holding" examples and exactly zero "exit" examples, because the code meant to fire when a trade closes checked a flag that could never come out true. Fixed; exit examples accumulate from the next closed trade onward.
- **The exit-AI's readiness check graded a nonexistent placeholder** (its daily "has it earned more control?" query was missing the chart-series field), so the answer was always "not ready." Fixed.
- **An AI-server outage no longer halts trading.** Previously, if the strategy couldn't reach its AI helper it gave up on the entry entirely; now an outage falls back to plain technical signals — the same safe fallback used when the AI is online but unconfident.
- **Two more "propose the same change and reject it forever" bugs** in the automatic settings-editor: a mathematically-unreachable risk suggestion now moves partway as far as it safely can, and "already at the ceiling" is now a calm state instead of a stuck suggestion. Its skip log (bloated to ~2,190 lines of repeats) now logs each distinct finding once per day.
- **Practice-trade bookkeeping cleanup**: pretend trades now use the exact same entry-distance math as real trades (they'd drifted onto an easier-to-fill formula), plus four smaller measurement fixes (overwritten snapshots, inconsistent timestamps, undercounted wait times, near-misses measured against pre-order prices).
  *Dev note: none dramatic alone, but all five fed the automatic settings-editor — cleaning them up makes its suggestions trustworthy.*
- **Session-boundary snap**: crossing the overnight/day boundary now immediately switches to an allowed profile instead of trading a disallowed one for up to a full no-fill window (10–23 minutes).

---

## Patch 2026-07-17 — "The Great Bug Hunt"

A full sweep of the trend robot, the practice-trade pipeline, and the machine's safety nets — plus the settings-editor going autonomous (with guardrails) and a two-tier risk formula.

### ✨ New
- **The dashboard now edits the strategy's own settings automatically** — every 5 minutes, and only when every profile with enough real evidence for a given contract-and-setting agrees on the same direction. Guardrails: measured evidence required for tightening directions, the full 40-profile curve is re-simulated before any write (rejected if any invariant would break), a full file backup before every edit, and every decision logged.
  *Dev note: watching it run live caught two real bugs its design review missed — a suggestion built on a single real measurement, and a rounding loop that "corrected" the same value every 5 minutes forever.*
- **No-Fill Log suggestions**: every unfilled order now records how close price actually came; the dashboard suggests a smaller cushion where orders consistently miss by similar amounts, or a bigger one where every order fills with room to spare. Suggestions need 5+ real measurements.
- **Sizing Reassess card**: per contract and profile, how far real trades moved against the position before turning, and how much peak profit was captured vs. given back. Reversal trades show plain dollars ("gave back $860 of $845 peak") because percentages off small peaks mislead.
- **Trend dashboard health banner**: flags a market that goes quiet while its neighbors stay live (the closest it can get to noticing a switched-off strategy), and actively watches for the exact data mix-up in the crude-oil bug below — two markets showing identical buying-pressure numbers, or one frozen.

### ⚖️ Balance
- **Two-tier risk sizing.** Profiles 1–19 keep the smooth growth curve; profiles 20–40 grow on their own curve so profile 40's risk is settable independently. The S&P specifically steps a fixed $12.50 per profile above 19 — the only way its coarse tick size never lands two profiles on the same dollar.

  | Contract | Profile 1 | Profile 40 |
  | --- | --- | --- |
  | Nasdaq (NQ) | $500 / $375 | $1,000 / $800 |
  | S&P 500 (ES) | $312.50 / $300 | $812.50 / $787.50 |
  | Russell 2000 (RTY) | $200 / $150 | $500 / $375 |
  | Dow (YM) | $100 / $75 | $400 / $300 |

  *(per-trade risk / daily "keep trying" budget — verified across all 160 combinations: always increasing, never duplicated, NQ > ES > RTY > YM at every profile.)*

  ![Two-tier risk sizing wireframe](wireframes/risk_sizing_wireframe.svg)

### 🐛 Fixed
- **Five real bugs in the trend robot**, headlined by: *all nine markets were feeding the AI crude oil's order-flow data* (a hardcoded market #0), so the gold, Nasdaq, and bitcoin models were learning from crude's numbers while believing them their own. 713 of 779 contaminated practice trades purged (crude's own 66 kept, backups first) and two already-trained markets reset to "still learning." Also fixed: the every-5-bars "do you still like this trade?" exit never ran (math overflow made "5 bars yet?" permanently false), four end-of-day close-out settings did nothing, one-market retrains secretly retrained everything, and the daily 2:05 PM self-retrain could serve half-updated answers mid-rebuild.
- **A file-sync accident had silently deleted recent work** — including the entire "AI picks the next profile" feature — while NinjaTrader kept running the older compiled copy. Caught because the code was missing from disk yet still writing fresh training data. Restored and rebuilt on top of the new risk formula.
- **A 22,000-point "price swing" in the practice data** (impossible for a ~700-point-range contract) led to a sanity clamp: no single-trade excursion beyond a generous limit gets recorded; a warning is logged instead so a repeat is visible rather than silently poisoning the data.
- **Same-day dashboard glitch**: a text-quoting style broke two model-health sections after a restart. Fixed, and dashboard changes now get their browser-side code actually executed in a real check before shipping.

### 🔧 Under the hood
- **Per-market memory checker**: a standing tool now verifies all ~150 pieces of per-market state survive the swap when one strategy instance juggles multiple markets — it immediately found (and fixed) two unsaved rotation fields.
- **Practice and real trades now share one filter core.** The three indicator pass/fail rules existed as hand-duplicated copies — risky, since practice trades teach the AI. Consolidated to shared cores with thin wrappers, verified identical across 200,000 randomized cases.
- **Safety nets hardened**: the daily Mega backup now includes the strategies' full version-control history (not just file snapshots); a new phone alert fires below 10GB free disk; and a local-only background task snapshots the two live strategy files every 5 minutes for instant rollback (never uploaded anywhere — strategy files don't push to GitHub).

---

## Patch 2026-07-16 — "The Formula"

### ⚖️ Balance
- **Risk-per-trade is now a formula, not a 160-number hand-typed price list.** Dollar risk still grows smoothly with profile number, and no two profiles can ever risk the identical amount (the old list had accidental duplicates). Unknown contracts fall back to the ES row, as before.

  | Contract | Per-trade risk (Profile 1) | Daily budget |
  | --- | --- | --- |
  | Nasdaq (NQ) | $500 | $375 |
  | S&P 500 (ES) | $400 | $300 |
  | Russell 2000 (RTY) | $200 | $150 |
  | Dow (YM) | $100 | $75 |

  ![Risk sizing wireframe](wireframes/risk_sizing_wireframe.svg)

---

## Patch 2026-07-15 — "Quality of Life"

### ✨ New
- **AI profile picker** (off by default): the strategy can ask its AI "which profile next?" instead of cycling in order. If the AI isn't confident, normal rotation continues — safe to enable even mid-training. Switch off = identical behavior to before.
- **Phone alerts on order placed and filled** — live account only; demo and test accounts stay silent.

### ⚖️ Balance
- **Entry pullback distances fine-tuned** per contract, growing on looser profiles so the strategy doesn't chase price when settings are already loose.

### 🐛 Fixed
- **Custom Range mode cancelled every fresh order forever** near the overnight/day session switch, because a session restriction that was never meant to apply to it did. It no longer does.

---

## Patch 2026-07-14 — "Unstuck"

### 🐛 Fixed
- **"Unused Only" rotation could freeze on one profile forever**: an unfilled order's timeout kept re-picking "the lowest profile I haven't traded" — but profiles only count as traded once a trade *closes*, so it picked itself endlessly. Timeouts now simply advance to the next profile; "unused first" logic waits for an actual closed trade.
- **Another cancel-forever loop at the session switch**: cancelling a now-disallowed profile's order never updated which profile the strategy thought it was on, so it retried the same doomed setup every candle (over half of 279 logged no-fill cancels landed within a minute of placement). Cancelling now also resets to an allowed profile. Bonus finds: the "ignore session restrictions" toggle wasn't respected in random mode, and the "profiles already used today" file had never actually been saved to disk.

---

## Reference: Template Mode 3 rotation logic

*(Static reference material, not a patch — kept at the bottom so the newest patch stays on top.)*

                ┌──────────────────────────────┐
                │      ACTIVE TEMPLATE: T       │
                │          T = 1...40           │
                └──────────────┬───────────────┘
                               │
                               ▼
            Is this template marked as a winner?
                    │                      │
                  YES                      NO
                    │                      │
                    ▼                      ▼
           69-minute fill window    34m30s fill window
                    │                      │
                    └──────────┬───────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
      Entry fills          No fill by            Position is
      before deadline      deadline              still open
          │                    │                    │
          │                    │                    └─ Keep template;
          │                    │                       never rotate during
          │                    │                       an open position
          │                    ▼
          │             Advance one:
          │             T1 → T2 → ... → T40 → T1
          │             Arm normal 34m30s window
          │
          ▼
    Trade eventually closes
          │
    ┌─────┴──────┐
    │            │
    ▼            ▼
  WIN          LOSS
    │            │
    ▼            ▼
Stay on T     T2–T39 → T-1
Arm 69m       T1 → T1
window        T40 → T1
              Arm normal 34m30s window

`temalimit` doesn't use one fixed set of rules all day. It has 40 different settings profiles ("templates"), numbered 1 (strictest, fewest trades) to 40 (loosest, most trades), and it rotates through them over time. The picture below shows the rotation logic:

![Template Mode 3 rotation wireframe](wireframes/mode3_wireframe.svg)

In plain terms:
- Every time an order is placed, there's a countdown. If the profile that placed the order has won a trade before, it gets more time to fill (69 minutes); if not, it gets less (about 34 minutes).
- If the order fills in time, the strategy trades it out normally.
- If it doesn't fill in time, the strategy gives up on that profile and moves to the next one in line.
- Once a trade is open, the strategy never switches profiles until that trade is closed.
- After a **win**, it keeps using the same profile. After a **loss**, it steps back one profile (gets a little stricter), unless it was already on the strictest one, in which case it resets to profile 1.
