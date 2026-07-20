# AI-Driven Trading Automation

A production-grade, **self-operating algorithmic trading stack** I designed and built end-to-end, solo. Two live [NinjaTrader 8](https://ninjatrader.com/) strategies make their **entry and exit decisions with machine-learning models that I train, serve, and continuously verify myself** — wrapped in an automation layer that retrains the models, monitors every moving part, backs itself up off-site, and self-heals with no human in the loop.

This is a curated, PII-redacted snapshot of that system. It is real code that runs real automation on live markets — not a demo.

> **Running it?** See **[SETUP.md](SETUP.md)** — the Python services (the AI) install and run from this repo; the strategy itself needs the NinjaTrader 8 platform.

> **Scope of this snapshot:** 38 source files, **~29,900 lines** — ~17.4K Python (ML services, dashboards, automation), ~10.9K C# (the two trading strategies + their compile dependencies), ~1.5K PowerShell/batch (the self-operating automation). All personal identifiers, hosts, credentials, and account numbers have been redacted; see [Redaction & privacy](#redaction--privacy).

---

## What this demonstrates

- **Applied ML in a live, adversarial, latency-sensitive setting** — models that decide long / short / no-trade at entry and hold / exit on open positions, trained per-instrument, gated so they only act when they've earned the right to.
- **The unglamorous ML engineering that makes that safe** — automated data-integrity verification, leakage tripwires, validation splits, readiness gates, and reproducible daily retraining.
- **End-to-end systems ownership** — from the C# strategy on the chart, through FastAPI model-serving microservices, to the Windows automation that keeps it all alive unattended.
- **Reliability engineering for something that loses money when it breaks** — a mesh of watchdogs, circuit breakers, off-site disaster recovery, and phone alerting, all running as scheduled tasks that survive logout, reboot, and power loss.

---

## Read it in this order

| # | Section | What's inside |
|---|---------|---------------|
| **1** | **[The AI](ml-services/) — models that decide every trade** | The entry/exit models, per-instrument training pipeline, daily auto-retraining, verification suites, and the FastAPI services that serve predictions to the live strategies. **Start here.** |
| **2** | **[The strategies](strategies/) the AI drives** | `temalimit` (live) and `TrendTcnStrategy` — the C# NinjaTrader logic that turns model predictions into risk-managed orders. |
| **3** | **[Dashboards & observability](dashboards/)** | Live web dashboards for model health, open trades, and trend predictions — how I watch an autonomous system without babysitting it. |
| **4** | **[Self-operating automation](infrastructure/)** | The part that runs itself: 11 scheduled automations, a watchdog mesh, circuit breakers, off-site backups, hardware monitoring, and phone alerts. **Read this last** — it's what turns the above into a system that operates unattended. |

Architecture overview and data flow: **[ARCHITECTURE.md](ARCHITECTURE.md)**. Diagrams: **[diagrams/](diagrams/)**.

---

## 1. The AI, in one screen

Two independently-trained model families back the strategies. They are deliberately **conservative** — the default answer is "don't trade."

**Entry model** — given a candidate setup, predicts **long / short / no-trade**. Trained *per instrument and per data series* (a model for ES 1-min is not the same model as NQ Renko), because a signal that means one thing on one contract means nothing on another. It only gets a vote once it clears a **base-rate + directional gate** — it must beat the naïve "always guess the majority class" baseline *and* show real directional edge, or it's ignored and the strategy falls back to plain rule-based signals.

**Exit model** — a *separate* model that watches already-open positions and predicts **hold vs. exit early**. It only loads if it passes **readiness gates** (minimum AUC, minimum minority-class examples) computed from held-out data — a lucky score off three examples can't put it in charge of live money.

**Trend model** — a temporal convolutional network (TCN) for multi-market trend breakouts (oil, FX, index futures, gold), re-evaluating its own confidence every few bars and bailing when it starts to doubt itself.

**What keeps the AI honest** (see [ml-services/verification.py](ml-services/MLService/verification.py)):
- Automated **data-integrity verification suites** (9–10 checks per service) run continuously — catching cross-instrument leakage, duplicate training windows, feature-schema drift, label drift, and "exitless" trades that would quietly teach the model that positions never close.
- **Reproducible daily retraining** at a fixed time, with validation splits, sequence-length caps to bound memory, and per-group cadence so a 70K-sample group doesn't retrain on every new tick.
- Every gate that blocks a trade **logs why**, so the system is auditable after the fact.

---

## 4. …and it runs itself

Everything above is orchestrated by an automation layer designed to operate with the laptop closed and nobody watching (full detail in **[infrastructure/](infrastructure/)**):

- **11 scheduled automations** (Windows Task Scheduler, S4U so they run whether or not anyone is logged in, and battery-aware so they survive on a laptop).
- A **watchdog mesh** that restarts any dead model service, dashboard, or the trading platform itself — plus a **daily-loss circuit breaker** and a **naked-position guard** that catches an unhedged live position and shouts about it.
- **Off-site disaster recovery**: daily model-weight backups, a full off-machine push to cloud storage with a staleness alarm, and a bare-metal recovery guide that ships inside every backup.
- **Phone alerts** for anything that needs a human — via a push service, no app to build.
- **Auto-commit** of every strategy edit every few minutes for local rollback history, and an auto-push pipeline (this repo) guarded by a **local PII pre-push hook**.

---

## Tech stack

| Layer | Tools |
|-------|-------|
| Strategy logic | C# / NinjaScript (NinjaTrader 8) |
| ML training & serving | Python, PyTorch, scikit-learn, FastAPI, uvicorn |
| Dashboards | Python (stdlib HTTP), vanilla JS/HTML/CSS, SVG |
| Automation & ops | PowerShell, Windows Task Scheduler (S4U), rclone, ntfy push, Git |

---

## Redaction & privacy

This is a public snapshot, so every personal identifier was mechanically removed before publishing:

- Usernames and `C:\Users\…` home paths → `<user>`
- Push-notification topic IDs → `<ntfy-topic>`
- Broker account and order IDs → `<account>` / `<order-id>`
- Loopback addresses normalized to `localhost`
- API keys were **already** kept out of source (referenced only through environment variables) — nothing to redact

A local `git` pre-push hook independently scans every outgoing change for emails, phone numbers, SSN-shaped strings, IP addresses, home paths, and account-number-shaped digit runs, and blocks the push if it finds any. No credentials, keys, or personal data appear anywhere in this snapshot.

*Built and maintained solo. Shared here as a redacted snapshot; not investment advice, and no trading-performance claims are made or implied — the point is the engineering.*
