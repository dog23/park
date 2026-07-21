# 4 · Self-operating automation

The automation that keeps the AI and the strategies running unattended — through logout, reboot, and power loss — with a phone alert only when a human is needed.

Everything here runs as **Windows Task Scheduler** jobs, registered **S4U** (run whether or not the user is logged on, no stored password) and **battery-aware** so they keep going on a laptop. Full inventory: **[scheduled-tasks/TASKS.md](scheduled-tasks/TASKS.md)**.

## The watchdog mesh — `watchdogs/`

Every critical process has something watching it, and the watchdogs are themselves scheduled tasks so nothing depends on a person keeping a terminal open.

- **[`dashboard_watchdog.py`](watchdogs/dashboard_watchdog.py)**, **[`trend_watchdog.ps1`](watchdogs/trend_watchdog.ps1)**, and the ML-service watchdog — restart any dead model service or dashboard, and reap orphaned training workers so a restart doesn't leave zombie processes hogging cores.
- **[`ninjatrader_process_watchdog.ps1`](watchdogs/ninjatrader_process_watchdog.ps1)** / **[`watchdog_ninjatrader_watcher.ps1`](watchdogs/watchdog_ninjatrader_watcher.ps1)** — detect the trading platform crashing *or freezing* and bring it back.
- **[`circuit_breaker_watchdog.py`](watchdogs/circuit_breaker_watchdog.py)** — a **daily-loss circuit breaker**: past a threshold, it stops the bleeding and alerts.
- **[`naked_position_watchdog.py`](watchdogs/naked_position_watchdog.py)** — catches an **unhedged live position** (an entry filled but its protective stop missing) and raises an urgent alert.

## Off-site disaster recovery — `backups/`

Designed around one question: *if this laptop died right now, could I rebuild everything?*

- **[`backup_ml_weights_daily.ps1`](backups/backup_ml_weights_daily.ps1)** — daily local snapshot of model weights, scheduled-task definitions, and config, plus a bare-metal **recovery guide** bundled *inside* every backup.
- **[`upload_ml_backup_to_mega.ps1`](backups/upload_ml_backup_to_mega.ps1)** — pushes backups off-machine to cloud storage via `rclone`, with dedup, "keep newest three" pruning, and a **staleness alarm** that pings my phone if nothing has left the machine recently (this exact alarm caught 288 silent skips once).
- **[`auto_commit_nt8_repo.ps1`](backups/auto_commit_nt8_repo.ps1)** — commits the working tree on a schedule for local rollback history (version control, distinct from off-site backup).

## Hardware & housekeeping — `hardware_monitor/`, `scheduled-tasks/`

- **[`hardware_monitor/hw_monitor.py`](hardware_monitor/hw_monitor.py)** — watches CPU/GPU/thermals/disk on the trading box and pushes phone alerts on trouble; zero-config, never crashes the thing it's monitoring.
- **[`scheduled-tasks/`](scheduled-tasks/)** — the task-registration scripts (`setup_watchdog_task.bat`, the S4U upgrade helper, `ml_daily.ps1`), log housekeeping (`trace_log_cleanup.ps1`), and this repo's own **auto-push** pipeline (`autopush.ps1` + `watch_and_autopush.ps1`).

## Alerting

All of it reports to a single phone via **[ntfy](https://ntfy.sh/)** push — no mobile app to build, no inbox to watch. Routine success is silent; only things that need a human make noise.

## Principles

- **Nothing critical depends on a logged-in session or an open terminal.** If it matters, it's a scheduled task.
- **Fail loud, fail safe.** Guards stop trading and alert; they never try to be clever with live money.
- **Backups are only real if you've thought about the restore.** The recovery guide ships inside the backup, and the off-site path is monitored for staleness — because a backup that silently stopped is worse than none.
