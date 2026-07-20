# Park

Public home for my NinjaTrader 8 work: a redacted snapshot of the trading system, plus the plain-English changelog and design diagrams for the trading stack.

## → [AI-driven trading automation](portfolio/)

A curated, PII-redacted snapshot of a self-operating algorithmic trading system I designed and built solo — **~29,200 lines** across ML services, two live NinjaTrader strategies, dashboards, and the automation that runs the whole thing unattended:

- **The AI** — machine-learning models that decide long / short / no-trade at entry and hold / exit on open positions, trained per-instrument, retrained daily, and continuously verified for data integrity.
- **The strategies** the models drive (`temalimit`, live; `TrendTcnStrategy`).
- **Dashboards** for model health, live trades, and trend predictions.
- **Self-operating automation** — a watchdog mesh, circuit breakers, off-site disaster-recovery backups, and phone alerts, all as scheduled tasks that survive logout and reboot.

**Start at → [portfolio/README.md](portfolio/README.md).**

## Also here

- [CHANGELOG.md](CHANGELOG.md) — plain-English patch notes for `temalimit` and the rest of the stack, written game-patch style (dated patches, ✨ New / 🎨 UI / ⚖️ Balance / 🐛 Fixed / 🔧 Under the hood / 🧭 Known issues sections). Doubles as evidence of shipping cadence.
- [wireframes/](wireframes/) — design diagrams referenced from the changelog.

*Everything here is redacted for public sharing; a local `git` pre-push hook scans every change for personal data before it leaves the machine. Not investment advice; no trading-performance claims are made.*
