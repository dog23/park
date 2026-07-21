# Setup & install

The Python services run standalone from this repo. The trading strategy itself requires the NinjaTrader 8 desktop platform. What each part needs:

| Component | Runs from this repo? | Needs |
|-----------|:--:|-------|
| `ml-services/MLService` (:8765) — entry/exit models | Yes | Python 3.13 + `requirements.txt` |
| `ml-services/MLService_Trend` (:8767) — trend TCN | Yes | Python 3.13 + `requirements.txt` |
| `dashboards/` (:8766) — live dashboard | Yes | Python 3.13 (standard-library server) |
| `strategies/*.cs` + `addons/*.cs` — the trading logic | No | NinjaTrader 8 (Windows) |

> A fresh clone will start the services fine, but they have no trained models and no data until a running strategy feeds them trade logs. Standalone, the services run but are empty until the NinjaTrader half is connected.

---

## 1. The Python services (the AI)

### Prerequisites
- **Python 3.13** (the services were built and run on 3.13).
- ~2–3 GB disk for PyTorch and dependencies. CPU-only is fine — inference is CPU-optimized; no GPU required.

### Install
```bash
git clone https://github.com/dog23/park.git
cd "park/AI Deep Learning Automation"

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
```

### Run each service
From its own folder:
```bash
# Entry + exit model service + model-health dashboard  ->  http://localhost:8765
cd ml-services/MLService
python -m uvicorn service:app --host 0.0.0.0 --port 8765

# Trend TCN service + trend dashboard  ->  http://localhost:8767
cd ml-services/MLService_Trend
python -m uvicorn app:app --host 0.0.0.0 --port 8767

# Live trades dashboard  ->  http://localhost:8766
cd dashboards
python live_dashboard_server.py         # honors PORT env var, defaults to 8766
```
Each is independent; run any subset. Open the printed `http://localhost:<port>` in a browser.

### Configuration (paths & env vars)
The services default to a Windows NinjaTrader 8 install path and read the strategy's log/data files from it. Every path is overridable by environment variable — **replace `<user>` and `<ntfy-topic>` below with your own values**:

| Env var | What it points at | Default |
|---------|-------------------|---------|
| `NT_USER_DATA_DIR` | Your NinjaTrader 8 user-data folder (where strategies write logs) | `C:\Users\<user>\Documents\NinjaTrader 8` |
| `PORT` | Dashboard port | `8766` |
| `TEMALIMIT_CS_PATH`, `*_TRADES_PATH`, `*_LOG_PATH`, … | Individual data/log files (all have sane defaults under `NT_USER_DATA_DIR`) | derived |

On non-Windows, set `NT_USER_DATA_DIR` to any working directory — the services create the subfolders they need (`exist_ok=True`) and simply show empty panels until data appears.

Phone alerts (watchdogs, hardware monitor) post to an [ntfy](https://ntfy.sh/) topic — set your own topic ID (replacing `<ntfy-topic>`) before using the alerting scripts.

---

## 2. The strategy (NinjaTrader 8)

The `.cs` files are **NinjaScript** and run **only inside NinjaTrader 8** — they cannot be executed from a clone. To run `temalimit`:

1. Install **[NinjaTrader 8](https://ninjatrader.com/)** (Windows) and connect a data/broker feed.
2. Copy the `.cs` files from [`strategies/`](strategies/) into `Documents\NinjaTrader 8\bin\Custom\Strategies\` (the two strategies and their six companion files), and the [`addons/`](addons/) files into `Documents\NinjaTrader 8\bin\Custom\AddOns\`.
3. Start the Python services above (the strategy calls `http://localhost:8765` / `:8767` for predictions; it degrades to rule-based trading if they're down).
4. In NinjaTrader: **New → NinjaScript Editor → Compile**, then add the strategy to a chart.

**Companion files (included).** The strategies depend on six helper classes, all published here so the project compiles as-is — no other custom code is required (everything else is NinjaTrader's built-in indicators):

| File | Role |
|------|------|
| `ActiveStopVisualStrategyBase.cs` | Base class `temalimit` extends (draws stop lines) |
| `OpenTradeStatusExporter.cs` | Writes the open-trade status files the dashboards/ML read (used by both strategies) |
| `PendingTradeStatusExporter.cs` | Writes pending/limit-order status |
| `PullbackStateExporter.cs` | Writes pullback-evidence state |
| `ManualExitCommand.cs` / `ManualCancelCommand.cs` | Chart buttons for manual exit/cancel |

> These exporters write the TSV/log files the Python services consume — so with the strategy running in NT8 and the services running from this repo, the full AI loop is connected.

---

## 3. Scheduled tasks / watchdogs (optional, Windows)

The `infrastructure/` scripts keep the stack running unattended. They are **not required** to run the strategy or services — they're the "always-on" layer.

> **Before you start:** every script hardcodes this machine's paths as `C:\Users\<user>\Documents\NinjaTrader 8\...`. **You must edit those paths** (and any `<ntfy-topic>`) to match your machine before running anything here. A few window-hiding launchers (e.g. the `*.vbs` wrappers the tasks call) are *not* published — run the `.ps1`/`.py` directly, or point the task at your own launcher.

Each responsibility is a Windows Task Scheduler job. The full list is in [infrastructure/scheduled-tasks/TASKS.md](infrastructure/scheduled-tasks/TASKS.md). To register one, run an elevated (Administrator) command prompt. The pattern (from [`setup_watchdog_task.bat`](infrastructure/scheduled-tasks/setup_watchdog_task.bat)):

```bat
schtasks /create /tn MyServiceWatchdog ^
  /tr "C:\path\to\python.exe C:\path\to\watchdog.py" ^
  /sc minute /mo 1 /ru SYSTEM /rl HIGHEST /f
```

For tasks that must run **whether or not you're logged in** (no stored password), register them **S4U** instead — see [`upgrade_nt8_autocommit_to_s4u.ps1`](infrastructure/scheduled-tasks/upgrade_nt8_autocommit_to_s4u.ps1) for the `Register-ScheduledTask -Principal (New-ScheduledTaskPrincipal -LogonType S4U ...)` pattern. Point each task at the matching script in `infrastructure/watchdogs/`, `infrastructure/backups/`, or `infrastructure/hardware_monitor/`.

## 4. Off-site backups with rclone (optional, Windows)

[`backups/upload_ml_backup_to_mega.ps1`](infrastructure/backups/upload_ml_backup_to_mega.ps1) pushes the daily model-weight backups off-machine to cloud storage, keeping the newest three and alerting your phone if nothing has uploaded in ~26h. It uses [rclone](https://rclone.org/) with a remote the script calls `mega`.

To set it up:

1. **Install rclone** — `winget install Rclone.Rclone` (or download from [rclone.org/downloads](https://rclone.org/downloads/)).
2. **Create the remote** — run `rclone config`, add a **new remote named `mega`**, choose your storage backend (the script name assumes [MEGA](https://rclone.org/mega/), but any rclone backend works if you keep the remote name `mega`), and sign into **your own** cloud account. Credentials are stored in your local `rclone.conf` — **never in this repo**.
3. **Edit the script's variables** to your machine: `$rclone` (path to `rclone.exe`), `$backupRoot` (where local backups live), `$logPath`, and `$ntfyTopic` (your own [ntfy](https://ntfy.sh/) topic, currently `<ntfy-topic>`).
4. **Run it** — `powershell -File upload_ml_backup_to_mega.ps1`. It runs `rclone copy` (incremental) into `mega:NinjaTrader8_Backups/<dated-folder>` and `rclone purge`s folders beyond the newest three. Schedule it daily via §3.

The local snapshot it uploads is produced by [`backups/backup_ml_weights_daily.ps1`](infrastructure/backups/backup_ml_weights_daily.ps1) (model weights, task definitions, code history) — edit its paths the same way.

See [infrastructure/README.md](infrastructure/README.md) for how these pieces fit together.
