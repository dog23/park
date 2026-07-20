# 3 · Dashboards & observability

How you watch an autonomous trading system without babysitting it. Three separate web dashboards, each with a single job — deliberately not merged, so a bug in one view can't take down the others.

| Port | Dashboard | Shows |
|------|-----------|-------|
| 8765 | **Model health** | Verification-suite results, per-model readiness, ablation runs, entry-gate reassessment |
| 8766 | **Live trades** | Open positions, completed trades, exit reasons, and auto-applied position sizing |
| 8767 | **Trend predictions** | The trend TCN's live confidence and signals |

Included here:

- **[`live_dashboard_server.py`](live_dashboard_server.py)** *(~3,900 lines)* — the port-8766 live dashboard. A dependency-light Python HTTP server (standard library only) that reads the strategies' status/log files and renders live trades, exit-reason breakdowns, and completed-trade charts. Notable engineering: it **auto-discovers new strategies** and auto-hides ones idle for 24h; it renders trade charts by pulling bar data **off the UI thread** with error-cache TTLs so a slow fetch can't freeze the page; and it survives locked/half-written TSV files with retry-plus-cached-fallback instead of dropping the whole status payload.
- **[`auto_apply_sizing.py`](auto_apply_sizing.py)** *(~1,000 lines)* — the automation behind the sizing card: it reads accumulated trade evidence and **auto-applies position-sizing adjustments** within invariant floors, with honest "nothing changed and here's why" messaging and a once-per-day log dampener so it doesn't spam.

## Design choices

- **Read-only over shared files.** Dashboards never call into the trading process; they read the same status/log files the strategies write. A dashboard crash can't touch live trading.
- **Self-restarting.** Each dashboard exposes a `/restart` route and is backed by its own watchdog task (see [../infrastructure/](../infrastructure/)), so a hung view recovers on its own.
- **No build step, no framework.** Vanilla JS/HTML/CSS/SVG served from Python — one less toolchain to break at 3 a.m.
