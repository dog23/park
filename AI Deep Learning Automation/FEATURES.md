# Features — what the models see, and how each is computed

The models don't see raw prices. Each candidate setup is turned into a short list of numbers (the "feature vector") the strategy computes in NinjaScript and sends to the model service. Two normalization ideas run through all of it:

- **Prices are measured in *ticks* (entry) or *ATRs* (trend), not dollars** — so the same feature means the same thing whether it's a $6,000 NQ or a $70 CL bar.
- A few **identity/scale features** (symbol hash, dollars-per-tick, price scale, bar-type) are appended so a *single* model can generalize across instruments and chart types instead of needing one per contract.

---

## Entry model features (28 per bar) — `temalimit`

Computed in `temalimit.cs`. Most are divided by the instrument's tick size.

| # | Feature | What it measures |
|---|---------|------------------|
| 1 | (close − VWAP) / tick | Distance of price from the volume-weighted average price |
| 2–4 | (close − Bollinger mid / upper / lower) / tick | Where price sits relative to each Bollinger band |
| 5 | (TEMA − prev TEMA) / tick | TEMA slope (trend direction/strength) |
| 6 | MACD / tick | MACD value |
| 7 | (MACD − prev MACD) / tick | MACD momentum (rising/falling) |
| 8–9 | MFI, and its change | Money Flow Index level and momentum |
| 10 | RSI | Relative Strength Index (defaults to 50 if unavailable) |
| 11–12 | Stochastic, and its change | Stochastic oscillator level and momentum |
| 13 | ATR / tick | Volatility (average true range) |
| 14 | (Bollinger upper − lower) / tick | Band width — volatility expansion/contraction |
| 15 | (VWAP − prev VWAP) / tick | VWAP slope |
| 16 | \|close − open\| / tick | Candle body size |
| 17 | (high − max(open,close)) / tick | Upper wick length |
| 18 | (min(open,close) − low) / tick | Lower wick length |
| 19 | volume / 10,000 | Bar volume, normalized |
| 20 | (close − prior close) / tick | One-bar price change (return) |
| 21–28 | 2× symbol hash, dollars-per-tick, price scale (log), bar-period value, 2× bar-type hash, bar-type category | Identity/scale features so one model serves many instruments & chart types |

## Exit model features — `temalimit`

The exit model reads a **sequence** (the trade's recent bars, up to 128) plus a fixed **context** vector.

**Sequence — 9 numbers per bar:**

| # | Feature | What it measures |
|---|---------|------------------|
| 1 | unrealized R | Current open profit/loss in units of the trade's risk |
| 2 | bars held / 50 | How long the trade has been open |
| 3 | ATR / price | Relative volatility right now |
| 4 | (price − session VWAP) / ATR | Distance from session VWAP, in volatility units |
| 5 | Bollinger position | Where price sits within the bands |
| 6 | TEMA slope | Local trend direction |
| 7 | (reserved) | Placeholder (0.0) |
| 8–9 | sin / cos of time-of-day | Time of day, encoded cyclically so 23:59 is next to 00:00 |

**Context — 8 numbers (fixed for the trade):** bars held (clamped), unrealized R (clamped), direction (long/short), average bar speed, symbol hash, dollars-per-tick, data-series type, data-series value.

## Trend model features (10 per bar) — `TrendTcnStrategy`

Computed in `TrendTcnStrategy.cs`, normalized by ATR. Fed as a rolling window into the TCN.

| # | Feature | What it measures |
|---|---------|------------------|
| 1 | (close − Donchian high) / ATR | Breakout above the recent high channel |
| 2 | (close − Donchian low) / ATR | Distance from the low channel |
| 3 | SuperTrend direction | +1 up-trend / −1 down-trend |
| 4 | (close − SuperTrend) / ATR | Distance from the SuperTrend line |
| 5 | LinReg slope / ATR | Linear-regression slope (trend steepness) |
| 6 | ADX | Trend strength |
| 7 | Choppiness Index | Trending vs. ranging |
| 8 | relative volume | This bar's volume vs. its moving average |
| 9 | order-flow delta / avg volume | Net buying vs. selling pressure (ask − bid volume) |
| 10 | ATR / price | Relative volatility |

---

*Feature definitions live in the strategy `.cs` files (they build and send the vectors); the Python side (`feature_utils.py`, `trend_utils.py`) handles validation and the symbol/series grouping. If features are added or reordered, the model retrains against the new schema and the [feature-schema check](MAINTENANCE.md) guards against mixing old and new layouts.*
