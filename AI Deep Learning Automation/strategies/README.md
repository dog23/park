# 2 · The strategies the AI drives

Two C# / NinjaScript strategies. They handle everything the model doesn't: detecting candidate setups, calling the model service, and turning a prediction into a risk-managed order with real exits, sizing, and safety rails. The AI decides *whether* and *which way*; the strategy decides *how much*, *at what price*, and *when to bail*.

## `temalimit.cs` — the live one *(~8,400 lines)*

The only strategy trading real money. A limit-order strategy on chart-pattern crossovers (TEMA / Bollinger Bands / VWAP) with momentum filters and a two-stage exit ladder.

Key features:

- **Dual-model integration.** Calls the entry model for long/short/no-trade, and separately polls the exit model on open positions — each over localhost HTTP, each degrading to rule-based behavior if the service is unavailable.
- **Template/profile rotation.** 40 configuration "templates" and 5 rotation modes (including P&L-ranked losers-first / winners-first rotation), letting the strategy vary its own parameters and learn which profiles work per instrument+series.
- **Two-stage exit ladder** with an ATR-bound trailing stop and an 8-bar no-fill fallback to a looser entry band.
- **Real risk controls:** an account-wide **max-day-margin cap** across all tickers, and stop-legality handling (buy stops to the ask, sell stops to the bid) to avoid rejected orders.
- **Self-logging for training.** Every setup, fill, and exit — plus every *shadow* setup tested across all templates — is written to disk to become tomorrow's training data, with pessimistic fill assumptions on the shadow trades.
- **Crash-recovery reconciliation.** On restart it reconciles against externally-closed positions so the trade journal stays honest.

## `TrendTcnStrategy.cs` — multi-market trend *(~1,800 lines)*

Multi-market trend breakouts (oil, FX, index futures, gold) driven by the trend TCN. Market entries with a single stop and target, and continuous self-doubt: it re-checks the model's confidence every few bars and exits early if conviction fades. Currently in a learning phase — instrumented and logging, running alongside `temalimit`.

## Engineering notes

- **Written to be operated unattended** — extensive structured logging, defensive parsing of model responses (hand-rolled tolerant JSON reads), and graceful degradation everywhere a network call could fail.
- **Auto-committed every few minutes** to a local Git repo for rollback history (see [../infrastructure/](../infrastructure/)) — edits are versioned even mid-session.
- NinjaScript **auto-compiles** on save, so a single stray character anywhere under the strategy folder silently keeps the *old* code trading — a constraint that shaped how carefully these are edited.

