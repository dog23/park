# Park

Public home for my NinjaTrader 8 work: the trading system, plus the plain-English changelog and design diagrams for the trading stack.

## → [AI-driven trading automation](AI%20Deep%20Learning%20Automation/)

A self-operating algorithmic trading system I designed and built solo — **~29,200 lines** across ML services, two live NinjaTrader strategies, dashboards, and the automation that runs the whole thing unattended:

- **The AI** — machine-learning models that decide long / short / no-trade at entry and hold / exit on open positions, trained per-instrument, retrained daily, and continuously verified for data integrity.
- **The strategies** the models drive (`temalimit`, live; `TrendTcnStrategy`).
- **Dashboards** for model health, live trades, and trend predictions.
- **Self-operating automation** — a watchdog mesh, circuit breakers, off-site disaster-recovery backups, and phone alerts, all as scheduled tasks that survive logout and reboot.
- **Auto-tuning** — the "Reassess" automations that adjust the strategy's own sizing/gate/pullback constants from live-trade evidence: **[AUTO-TUNING.md](AI%20Deep%20Learning%20Automation/AUTO-TUNING.md)**.

**Start at → [the overview](AI%20Deep%20Learning%20Automation/README.md).**

| Read this for… | |
|---|---|
| How it's built, ports, data flow | [ARCHITECTURE.md](AI%20Deep%20Learning%20Automation/ARCHITECTURE.md) |
| Installing the servers, strategy, tasks, rclone | [SETUP.md](AI%20Deep%20Learning%20Automation/SETUP.md) |
| What each model sees & how features are computed | [FEATURES.md](AI%20Deep%20Learning%20Automation/FEATURES.md) |
| Checking for data poison, running validation tests, retraining | [MAINTENANCE.md](AI%20Deep%20Learning%20Automation/MAINTENANCE.md) |
| How the Reassess auto-tuning automations work | [AUTO-TUNING.md](AI%20Deep%20Learning%20Automation/AUTO-TUNING.md) |

## Strategies

- **[temalimit](AI%20Deep%20Learning%20Automation/strategies/temalimit.cs)** *(live)* — limit-order strategy on TEMA / Bollinger / VWAP crossovers, driven by the entry and exit models.
- **[TrendTcnStrategy](AI%20Deep%20Learning%20Automation/strategies/TrendTcnStrategy.cs)** — multi-market trend breakouts driven by the trend TCN.

More on each: [strategies/README.md](AI%20Deep%20Learning%20Automation/strategies/README.md).

## Also here

- [CHANGELOG.md](CHANGELOG.md) — plain-English patch notes for `temalimit` and the rest of the stack, written game-patch style (dated patches, ✨ New / 🎨 UI / ⚖️ Balance / 🐛 Fixed / 🔧 Under the hood / 🧭 Known issues sections). Doubles as evidence of shipping cadence.
- [wireframes/](wireframes/) — design diagrams referenced from the changelog.

*Not investment advice; no trading-performance claims are made.*
