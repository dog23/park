# Scheduled-task inventory

Every long-running responsibility in the system is a **Windows Task Scheduler** job — registered **S4U** (runs whether or not anyone is logged in, no stored password) and battery-aware. This is what lets the whole stack operate unattended. Below is the live inventory (task names shown; some are gated to only run when relevant).

| Task | Runs | Purpose |
|------|------|---------|
| **NT8MLServiceWatchdog** | continuous | Restart the entry/exit model service (:8765) if it dies; reap orphaned training workers |
| **TrendMLServiceWatchdog** | continuous | Restart the trend TCN service (:8767) if it dies |
| **NT8DashboardWatchdog** | every ~2 min | Restart the live trades dashboard (:8766) if it stops responding |
| **NT8NinjaTraderProcessWatchdog** | continuous | Detect the trading platform crashing *or freezing* and relaunch it |
| **NT8CircuitBreakerWatchdog** | continuous | Daily-loss circuit breaker — halt and alert past a loss threshold |
| **NT8NakedPositionWatchdog** | continuous | Detect an unhedged live position (missing protective stop) and alert urgently |
| **NT8HardwareMonitor** | continuous | CPU/GPU/thermal/disk monitoring with phone alerts |
| **NT8TraceLogCleanup** | scheduled | Prune platform trace logs so disk doesn't fill |
| **NT8AutoCommit** | every few min | Auto-commit the working tree for local rollback history |
| **StrategyAutoCommit** | every 5 min | Auto-commit strategy edits (local rollback; never pushed) |
| **ParkAutopushWatchdog** | on change | Debounced auto-commit + push of the docs repo |

Plus a daily model-retrain trigger and a daily off-site backup/upload, invoked from the ML service scripts (`ml_daily.ps1`, `backup_ml_weights_daily.ps1`, `upload_ml_backup_to_mega.ps1`).

## Registration scripts included here

- **[`setup_watchdog_task.bat`](setup_watchdog_task.bat)** — registers a watchdog task from the command line.
- **[`upgrade_nt8_autocommit_to_s4u.ps1`](upgrade_nt8_autocommit_to_s4u.ps1)** — migrates a task to S4U logon (run-whether-or-not-logged-on, no stored password).
- **[`ml_daily.ps1`](ml_daily.ps1)** — daily retrain trigger + service (re)launch, hidden-window.
- **[`trace_log_cleanup.ps1`](trace_log_cleanup.ps1)** — scheduled log housekeeping with a phone alert on failure.
- **[`maintain_ninjatrader_strategies.ps1`](maintain_ninjatrader_strategies.ps1)** — strategy-folder maintenance.
- **[`autopush.ps1`](autopush.ps1)** / **[`watch_and_autopush.ps1`](watch_and_autopush.ps1)** — the debounced file-watch → commit → push pipeline for this repo.

*Why S4U everywhere: a trading system can't depend on someone staying logged in. S4U tasks keep running across logout and reboot without a password sitting in a config file.*
