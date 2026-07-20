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
The services default to a Windows NinjaTrader 8 install path and read the strategy's log/data files from it. Every path is overridable by environment variable — **redacted values (`<user>`, `<ntfy-topic>`) must be replaced with your own**:

| Env var | What it points at | Default |
|---------|-------------------|---------|
| `NT_USER_DATA_DIR` | Your NinjaTrader 8 user-data folder (where strategies write logs) | `C:\Users\<user>\Documents\NinjaTrader 8` |
| `PORT` | Dashboard port | `8766` |
| `TEMALIMIT_CS_PATH`, `*_TRADES_PATH`, `*_LOG_PATH`, … | Individual data/log files (all have sane defaults under `NT_USER_DATA_DIR`) | derived |

On non-Windows, set `NT_USER_DATA_DIR` to any working directory — the services create the subfolders they need (`exist_ok=True`) and simply show empty panels until data appears.

Phone alerts (watchdogs, hardware monitor) post to an [ntfy](https://ntfy.sh/) topic; the topic ID is redacted to `<ntfy-topic>` throughout — set your own before using the alerting scripts.

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

> These exporters write the TSV/log files the Python services consume — so with the strategy running in NT8 and the services running from this repo, the full AI loop is connected. Redacted the same way as everything else (`<user>`, `<account>`, etc.).

---

## 3. Automation (optional, Windows)

The `infrastructure/` scripts (watchdogs, backups, scheduled tasks) are **Windows-specific** and reference this machine's paths. They're included as a reference for how the stack keeps itself running unattended — not required to run the services, and they'd need their paths and `<ntfy-topic>` adjusted for another machine. See [infrastructure/README.md](infrastructure/README.md).
