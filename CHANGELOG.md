# Changelog

Plain-English changelog for the `temalimit` strategy (and other NT8 trading code). This used to live in the `soy` repo's README; it now lives here so `park` is the single home for changelogs and wireframes/diagrams across all NT8 projects.

See [wireframes/](wireframes/) for the related diagrams (referenced inline below).

---

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

## Template Mode 3 rotation logic

`temalimit` doesn't use one fixed set of rules all day. It has 40 different settings profiles ("templates"), numbered 1 (strictest, fewest trades) to 40 (loosest, most trades), and it rotates through them over time. The picture below shows the rotation logic:

![Template Mode 3 rotation wireframe](wireframes/mode3_wireframe.svg)

In plain terms:
- Every time an order is placed, there's a countdown. If the profile that placed the order has won a trade before, it gets more time to fill (69 minutes); if not, it gets less (about 34 minutes).
- If the order fills in time, the strategy trades it out normally.
- If it doesn't fill in time, the strategy gives up on that profile and moves to the next one in line.
- Once a trade is open, the strategy never switches profiles until that trade is closed.
- After a **win**, it keeps using the same profile. After a **loss**, it steps back one profile (gets a little stricter), unless it was already on the strictest one, in which case it resets to profile 1.

---

### Risk sizing now behaves differently after profile 19, so it can hit realistic per-contract limits (July 17, 2026)

**What it was:** The formula from the entry below used one smooth growth curve for all 40 profiles per contract. That's mathematically clean, but it meant I couldn't independently choose "how much should the riskiest profile (40) risk" without also changing every profile below it in a fixed ratio — and for the S&P 500 (ES) contract specifically, the formula's normal rounding (to the nearest quarter-point, $12.50) started producing two neighboring profiles with the identical dollar amount once the profile numbers got high enough.

**What it is now:** Profiles 1–19 keep using the original smooth formula. Profiles 20–40 switch to a second formula that's allowed to grow at a different rate, so I can set "what does profile 40 risk" independently of "what does profile 1 risk." For the S&P 500 specifically, profiles 20–40 grow by a fixed one-tick ($12.50) step per profile instead of a smooth curve — the only way to guarantee no two profiles ever land on the same dollar number given how coarse that contract's price increments are.

**Profile 1 → Profile 40, in dollars (per-trade risk / daily "keep trying" budget):**

| Contract | Profile 1 | Profile 40 |
| --- | --- | --- |
| Nasdaq (NQ) | $500 / $375 | $1,000 / $800 |
| S&P 500 (ES) | $312.50 / $300 | $812.50 / $787.50 |
| Russell 2000 (RTY) | $200 / $150 | $500 / $375 |
| Dow (YM) | $100 / $75 | $400 / $300 |

Verified by simulating all 40 profiles for all 4 contracts: no two profiles ever risk the identical dollar amount, the amount always increases with the profile number, the daily budget always stays below the per-trade risk, and Nasdaq always risks more than the S&P 500, which always risks more than the Russell, which always risks more than the Dow, at every single profile.

![Two-tier risk sizing wireframe](wireframes/risk_sizing_wireframe.svg)

### Fixed: a file sync accident deleted a chunk of recent work, including an entire AI feature, without anyone noticing at first (July 17, 2026)

**What happened:** While reconciling two versions of the strategy file (one on this computer, one that had been uploaded through GitHub's website), the merge silently kept the older, uploaded version's code in two places instead of properly combining both sides. That wiped out the risk-formula rewrite above, and separately, it deleted the entire "AI can now pick the next settings profile" feature described further down — including the code that logs practice trades for that AI to learn from.

**How it was caught:** While investigating why some AI training data looked corrupted (see next entry), a search for the AI-template-selection code came up empty in the strategy file — even though NinjaTrader was still actively running it and still writing fresh training data. That only made sense if NinjaTrader was running an older, already-compiled version of the strategy that still had the feature, while the source file on disk had lost it. Comparing against an earlier saved version of the file confirmed it: the feature was gone from the file, but recoverable from an older save.

**The fix:** Restored the missing feature from the last version that had it, rebuilt carefully on top of the (also-since-changed) risk formula so nothing else broke, and double-checked nothing besides these two things was actually missing.

### Fixed: one data-quality bug, and hardened against future ones (July 17, 2026)

While reviewing the AI's practice-trade data, one real trade was logged as having moved over 22,000 points against the position — impossible for a contract that only trades in a roughly 700-point range total. The exact cause couldn't be pinned down for certain, but the fix doesn't require knowing the exact cause: the strategy now refuses to record any single-trade price swing bigger than a generous sanity limit, logging a warning instead so a repeat is actually visible rather than silently poisoning the data again.

### The No-Fill Log dashboard card can now suggest widening or narrowing the entry-price cushion, based on real measurements (July 17, 2026)

**What it was:** The dashboard already tracked orders that never filled (because price didn't pull back far enough to reach the order), but had no way to say by how much they missed, or whether the cushion should change.

**What it is now:** The strategy now measures, for every unfilled order, the closest the market actually got to the order's price before giving up. The dashboard uses that to suggest a smaller cushion for profiles/contracts that are consistently missing by a similar amount — and, in the other direction, a slightly bigger cushion for profiles that are filling every single time with room to spare (which might mean a better price was being left on the table). Suggestions only appear once there's enough real evidence (5+ measurements); thinner evidence just gets flagged as "keep watching."

![Pullback suggestion wireframe](wireframes/pullback_feedback_wireframe.svg)
![No-Fill Log bidirectional suggestion wireframe](wireframes/nofill_pullback_column_wireframe.svg)

### New dashboard card: Sizing Reassess (July 17, 2026)

Tracks, per contract and profile, how far real trades moved against the position before turning around (informs whether the per-trade risk amount has room to shrink or needs more room), and how much of a trade's peak profit actually got captured versus given back before it closed (informs the same question for the daily "keep trying" budget). Reversal trades — ones that built real profit and still closed at a loss — are shown as a plain dollar amount ("gave back $860 of $845 peak") rather than a percentage, since percentages built off a small profit peak can swing wildly and be misleading.

### The dashboard can now edit the strategy's own settings automatically — with a lot of guardrails (July 17, 2026)

**What it is:** A new background check, running every 5 minutes, reads the same evidence as the two cards above and — only when every profile that has enough real evidence for a given contract-and-setting agrees on the same direction — edits the strategy file to match. If even one qualifying profile disagrees, nothing happens; that setting just keeps collecting data.

**Guardrails:**
- Requires real, measured evidence (never a guess) for the "narrow the entry cushion" and "reduce per-trade risk" directions. The opposite directions (widen the cushion, raise the daily budget) are clearly labeled as an educated nudge rather than a measurement, since there's no direct way to measure "how much tighter could this safely be."
- Before writing anything, re-simulates the entire 40-profile curve for whatever's being changed and checks the same rules verified above still hold (no two profiles land on the same dollar amount, amounts always increase, Nasdaq > S&P 500 > Russell > Dow, and the daily budget always stays under the per-trade risk). If the change would break any of that, it's rejected and nothing is written.
- Saves a full backup of the strategy file before every single edit.
- Every check — whether anything changed or not — is written to a log file.

**Two real bugs already caught by watching it run for real, not just by reasoning about the design:**
1. One suggestion turned out to be built on a single real measurement, even though the underlying count looked like plenty of evidence — most of the other events it was "backed by" turned out to predate the measurement being logged at all. Fixed by counting only entries that actually have a real measurement.
2. Because the file only stores a couple of decimal places, one suggested value kept getting rounded down on write, which meant every 5-minute check saw a persistent (if tiny) mismatch and "corrected" it again — forever, without the file ever actually changing. Fixed by storing more decimal places.

---

### Risk-per-trade is now calculated by a formula, not a fixed price list (July 16, 2026)

**What it was:** Every combination of futures contract (Nasdaq/NQ, S&P 500/ES, Russell/RTY, Dow/YM) and settings profile (1–40) had its own hand-typed dollar amount in a price list — 160+ numbers to maintain by hand.

**What it is now:** One formula calculates the dollar risk for any contract and any profile automatically. The dollar amount still grows smoothly as the profile number goes up (profile 40 risks more than profile 1), and it's now guaranteed that **no two profiles ever risk the exact same dollar amount** — a problem the old price list occasionally had by accident (some neighboring profiles had accidentally landed on identical numbers).

**Amount risked per trade at Profile 1 (the strictest profile), in dollars:**

| Contract | Amount risked on a losing trade | Daily "keep trying" budget |
| --- | --- | --- |
| Nasdaq (NQ) | $500 | $375 |
| S&P 500 (ES) | $400 | $300 |
| Russell 2000 (RTY) | $200 | $150 |
| Dow (YM) | $100 | $75 |

The "daily keep trying budget" is always smaller than the per-trade risk amount, and both numbers grow together as the profile number goes up. Any contract the strategy doesn't recognize is treated the same as the S&P 500 (ES) row, just like before.

Nothing else about how the strategy enters or exits trades, talks to the AI service, rotates profiles, or logs data was touched — this change only affects how the dollar risk amount is calculated.

![Risk sizing wireframe](wireframes/risk_sizing_wireframe.svg)

### AI can now pick the next settings profile instead of just cycling through them (July 15, 2026)

Added an optional switch (off by default) that lets the strategy ask its AI helper "which profile should I use next?" instead of always going in order. If the AI isn't confident yet (still learning, or hasn't seen enough trades), the strategy ignores it and falls back to the normal rotation — so turning this on is safe even before the AI is fully trained. With the switch off, the strategy behaves exactly as it did before this change.

### Phone alerts when an order is placed or filled (July 15, 2026)

The strategy now sends a notification to my phone the moment an order is placed and again when it fills — but only for my live trading account. Every other account (including the demo account) is ignored, so I don't get spammed by test trades.

### Fine-tuned how far price has to pull back before entering (July 15, 2026)

Adjusted how far the market needs to pull back before the strategy places an entry order. The required pullback distance is now different for each contract and gets bigger on looser (higher-numbered) profiles, so it doesn't chase price as aggressively when the settings are already loose.

### Fixed: orders kept getting cancelled forever near the overnight/day session switch, in "Custom Range" mode (July 15, 2026)

**The problem:** If I had picked a custom list of profiles to use (instead of the default full range) and the market crossed from overnight into regular trading hours (or back), the strategy thought the current profile was "not allowed right now" and cancelled every fresh order the instant it was placed — over and over, forever.

**The fix:** Custom Range mode no longer applies the overnight/day-session restriction that was never meant to apply to it in the first place.

### Fixed: strategy could get permanently stuck on one profile (July 14, 2026)

**The problem:** In "Unused Only" rotation mode, if an order never filled, the timeout logic always tried to jump to "the lowest-numbered profile I haven't traded yet" — but a profile only gets marked as "already traded" once a trade on it actually closes. So a profile that never filled kept picking itself again and again, forever, and the rotation would appear frozen.

**The fix:** When an order times out without filling, the strategy now just moves to the next profile in simple numeric order, the same way the basic rotation mode does. It only tries to prioritize "profiles I haven't used yet" after a trade actually closes.

### Fixed: another stuck-cancelling-orders bug at the overnight/day session switch (July 14, 2026)

**The problem:** When the market crossed from overnight into regular hours (or vice versa) and the current profile fell outside what was allowed in the new session, the strategy cancelled the order — but never updated which profile it thought it was on. So on the very next candle, it tried the same cancelled setup again, got cancelled again, and repeated indefinitely. (I confirmed this by checking the log file: over half of 279 logged "no fill" cancellations happened within the same minute the order was placed.)

**The fix:** When this cancellation happens, the strategy now also resets itself to a profile that's actually allowed in the new session, instead of getting stuck retrying the old one.

Separately, a related setting ("ignore session restrictions entirely" for the fully-random rotation mode) wasn't being respected correctly, and a record-keeping file that was supposed to track "which profiles have I already used today" turned out to have never actually been saved to disk — so the strategy always thought every profile was still unused. Both are now fixed.

### Documented ML template selection (July 15, 2026)

Documented how the AI template-selection switch works in the changelog (see entry above, "AI can now pick the next settings profile...").
