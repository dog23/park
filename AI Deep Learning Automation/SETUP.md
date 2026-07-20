# Setup & install

Honest summary first: **the Python services (the AI) run standalone from this repo; the trading strategy itself does not — it requires the NinjaTrader 8 desktop platform.** Here's what each part needs.

| Component | Runs from this repo? | Needs |
|-----------|:--:|-------|
| `ml-services/MLService` (:8765) — entry/exit models | ✅ yes | Python 3.13 + `requirements.txt` |
| `ml-services/MLService_Trend` (:8767) — trend TCN | ✅ yes | Python 3.13 + `requirements.txt` |
| `dashboards/` (:8766) — live dashboard | ✅ yes | Python 3.13 (standard-library server) |
| `strategies/*.cs` — the actual trading logic | ❌ no | **NinjaTrader 8** (proprietary, Windows) + companion files — see [The strategy](#the-strategy-ninjatrader-8) |

> A fresh clone will **start** the services fine, but they have **no trained models and no data** until a running strategy feeds them trade logs. Standalone, you get live, healthy, *empty* services — useful to inspect the API, dashboards, and training/verification code, not to reproduce live predictions without the NinjaTrader half.

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
2. Copy the strategy `.cs` files into `Documents\NinjaTrader 8\bin\Custom\Strategies\`.
3. Start the Python services above (the strategy calls `http://localhost:8765` / `:8767` for predictions; it degrades to rule-based trading if they're down).
4. In NinjaTrader: **New → NinjaScript Editor → Compile**, then add the strategy to a chart.

> ⚠️ **Companion files not included.** `temalimit.cs` depends on several helper classes that are **not published in this snapshot**, so it will **not compile as-is**: `OpenTradeStatusExporter`, `PendingTradeStatusExporter`, `PullbackStateExporter`, `ActiveStopVisualStrategyBase`, `ManualExitCommand`, `ManualCancelCommand`. These write the status/log files the dashboards and ML services read. Without them the project is a **reference/reading** sample, not a compile-and-run one. (If you want the strategy to actually compile, those files would need to be added too — a decision about publishing more of the source.)

---

## 3. Automation (optional, Windows)

The `infrastructure/` scripts (watchdogs, backups, scheduled tasks) are **Windows-specific** and reference this machine's paths. They're included as a reference for how the stack keeps itself running unattended — not required to run the services, and they'd need their paths and `<ntfy-topic>` adjusted for another machine. See [infrastructure/README.md](infrastructure/README.md).
