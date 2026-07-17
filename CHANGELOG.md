# Changelog

Plain-English changelog for the `temalimit` strategy (and other NT8 trading code). This used to live in the `soy` repo's README; it now lives here so `park` is the single home for changelogs and wireframes/diagrams across all NT8 projects.

See [wireframes/](wireframes/) for the related diagrams (referenced inline below).

---

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

See [wireframes/risk_sizing_wireframe.svg](wireframes/risk_sizing_wireframe.svg) for the sizing diagram.

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
