# AI-Driven NinjaTrader 8 Trading Automation

This is an automated trading strategy for **NinjaTrader 8**, together with the machine-learning services and automation that run it. Two strategies (`temalimit`, `TrendTcnStrategy`) decide when to enter and exit trades using machine-learning models served from local Python services. Web dashboards show what the system is doing, and a set of Windows scheduled tasks keeps everything running unattended.

Redacted snapshot: 39 source files, ~30,000 lines. See [Redaction](#redaction).

---

## What it's built on

| Layer | Technology |
|-------|-----------|
| Trading strategies | C# / NinjaScript (NinjaTrader 8) |
| ML training & serving | Python, PyTorch, scikit-learn, FastAPI, uvicorn |
| Dashboards | Python (standard-library HTTP server), vanilla JS / HTML / CSS, SVG |
| Automation & ops | PowerShell, Windows Task Scheduler, rclone, ntfy (push notifications), Git |

---

## What it does

### The AI — models that decide each trade *([ml-services/](ml-services/))*
- **Entry model** — predicts **long / short / no-trade** for a candidate setup. Trained separately per instrument and per data series, and only used when it passes a base-rate + directional check; otherwise the strategy falls back to rule-based signals.
- **Exit model** — a separate model that predicts **hold / exit early** on open positions. Only loaded when it passes minimum-AUC and minimum-example checks.
- **Trend model** — a temporal convolutional network (TCN) for multi-market trend breakouts.
- **Training** — models retrain daily on the strategies' own trade logs, with validation splits and automated data-integrity checks (leakage, duplicate windows, label drift, etc.).

### The strategies — turn predictions into orders *([strategies/](strategies/), [addons/](addons/))*
- **`temalimit`** *(live)* — limit-order strategy on TEMA / Bollinger / VWAP crossovers with momentum filters, template rotation, and a two-stage exit ladder. Calls the entry and exit models.
- **`TrendTcnStrategy`** — multi-market trend breakouts (oil, FX, index futures, gold) driven by the trend TCN.
- **AddOns** — `ChartDataExporter` and `DashboardTradeLogger` bridge the strategies to the Python side.

### Dashboards — see what it's doing *([dashboards/](dashboards/))*
- `:8765` model health · `:8766` live trades & exit reasons · `:8767` trend predictions.

### Self-operating automation — keeps it running *([infrastructure/](infrastructure/))*
- 11 Windows scheduled tasks: watchdogs that restart dead services or the trading platform, a daily-loss circuit breaker, a naked-position guard, off-site backups, a hardware monitor, and auto-commit — with phone alerts via [ntfy](https://ntfy.sh/) when something needs attention. See [infrastructure/TASKS.md](infrastructure/scheduled-tasks/TASKS.md).

Architecture and data flow: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## How to set it up

Full instructions: **[SETUP.md](SETUP.md)**. In short:

- **The Python services** install and run from this repo — Python 3.13, `pip install -r requirements.txt`, then launch each service (`ml-services/MLService` on :8765, `ml-services/MLService_Trend` on :8767, `dashboards/` on :8766).
- **The strategy** runs only inside **NinjaTrader 8**: copy the `.cs` files from [`strategies/`](strategies/) into `bin\Custom\Strategies\`, the [`addons/`](addons/) files into `bin\Custom\AddOns\`, and compile. All companion files needed to compile are included.
- **Maintaining the models** — running data-poison checks, validation (ablation) tests, and retraining: see **[MAINTENANCE.md](MAINTENANCE.md)**.

---

## Contents

| Folder | Contents |
|--------|----------|
| [`strategies/`](strategies/) | The two strategies + 6 companion files they need to compile |
| [`addons/`](addons/) | The two NinjaTrader AddOns (strategy ↔ dashboard/ML bridge) |
| [`ml-services/`](ml-services/) | The entry/exit and trend model services (Python) |
| [`dashboards/`](dashboards/) | The live dashboard server |
| [`infrastructure/`](infrastructure/) | Watchdogs, backups, scheduled tasks, hardware monitor |
| [`diagrams/`](diagrams/) | Wireframes / design diagrams |

---

## Redaction

This is a public snapshot; personal identifiers were removed before publishing:

- Usernames and `C:\Users\…` home paths → `<user>`
- Push-notification topic IDs → `<ntfy-topic>`
- Broker account and order IDs → `<account>` / `<order-id>`
- Loopback addresses → `localhost`
- API keys are referenced only through environment variables — none appear in source.

A local `git` pre-push hook scans every change for emails, phone numbers, SSN-shaped strings, IP addresses, home paths, and account-number-shaped digit runs, and blocks the push if it finds any.

*Built and maintained solo. Not investment advice; no trading-performance claims are made.*
