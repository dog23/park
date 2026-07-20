# Architecture

The system is four cooperating layers. The **AI decision layer** is the core; everything else exists to feed it clean data, act on its output safely, and keep it running.

```mermaid
flowchart TB
    subgraph MKT[Live market]
        NT[NinjaTrader 8 platform<br/>real-time bars & ticks]
    end

    subgraph STRAT[2 - Strategy layer C# / NinjaScript]
        TL[temalimit.cs<br/>limit entries, 2-stage exit ladder]
        TT[TrendTcnStrategy.cs<br/>market entries, stop + target]
    end

    subgraph AI[1 - AI decision layer FastAPI + PyTorch]
        ENTRY[Entry model<br/>long / short / no-trade<br/>per instrument+series]
        EXIT[Exit model<br/>hold / exit early]
        TREND[Trend TCN<br/>breakout confidence]
        VERIFY[Verification suites<br/>leakage / drift / integrity]
        TRAIN[Daily retrain<br/>validation + readiness gates]
    end

    subgraph OBS[3 - Observability web dashboards]
        D65[":8765 model health"]
        D66[":8766 live trades & exits"]
        D67[":8767 trend predictions"]
    end

    subgraph OPS[4 - Self-operating automation Windows Task Scheduler]
        WD[Watchdog mesh<br/>restart dead services]
        CB[Circuit breaker<br/>daily-loss cutoff]
        NP[Naked-position guard]
        BK[Off-site DR backups]
        HW[Hardware monitor]
        AC[Auto-commit + auto-push]
    end

    NT --> TL & TT
    TL -->|"POST /predict, /predict-exit"| ENTRY
    TL --> EXIT
    TT -->|"POST /predict"| TREND
    ENTRY -->|"long/short/no-trade"| TL
    EXIT -->|"hold/exit"| TL
    TREND -->|"confidence"| TT
    TL & TT -->|"trade + feature logs (TSV/JSONL)"| TRAIN
    TRAIN --> ENTRY & EXIT & TREND
    VERIFY -.watches.-> TRAIN
    TL & TT -->|"status files"| D66
    ENTRY & VERIFY --> D65
    TREND --> D67
    WD -.restarts.-> ENTRY & TREND & D66 & NT
    CB & NP -.guard.-> TL & TT
    TL & TT --> AC
    ENTRY --> BK
```

## Data flow, end to end

1. **NinjaTrader** streams live bars/ticks to the two C# strategies.
2. A strategy detects a *candidate* setup (a chart-pattern crossover for `temalimit`; a multi-filter breakout for `TrendTcnStrategy`) and `POST`s the feature vector to its model service over localhost HTTP.
3. The **entry model** answers long / short / no-trade — but only if it has cleared its base-rate + directional gate; otherwise the strategy falls back to a plain rule-based signal.
4. If a position opens, `temalimit` polls the **exit model** on open trades to decide hold vs. exit early; `TrendTcnStrategy` re-checks its own **trend TCN** confidence every few bars.
5. Every setup, fill, and exit is written to **trade/feature logs** (TSV/JSONL) on disk.
6. Once a day, the **training pipeline** reads those logs, runs the **verification suites** to reject poisoned data, retrains per-instrument models behind readiness gates, and hot-loads the survivors.
7. **Dashboards** read the same status/log files to show model health, live trades with exit reasons, and trend predictions.
8. The **automation layer** independently watches every process and restarts, guards, backs up, and alerts as needed.

## Why these boundaries

- **Models run out-of-process from the strategy.** NinjaScript is single-threaded on the chart; a slow PyTorch forward pass can't be allowed to stall order handling. HTTP to a local FastAPI service keeps inference off the trading thread, and a model-service outage degrades gracefully to rule-based trading instead of halting.
- **Entry and exit are different models.** "Should I get in?" and "should I stay in?" have different features, different base rates, and different failure costs. Coupling them would let a good entry model paper over a bad exit policy.
- **Per-instrument, per-series models.** The same indicator value carries different information on different contracts and bar types; one global model would average away the edge and leak signal across instruments.
- **Verification is a first-class service, not a script.** In a system that trains on its own trade history, the fastest way to lose money is to train on subtly corrupt data. The checks that catch that run continuously and are surfaced on the model-health dashboard.

## Ports

| Port | Service | Role |
|------|---------|------|
| 8765 | `MLService` | Entry + exit models, model-health dashboard, verification, retraining |
| 8766 | `LiveDashboardServer` | Live trades, exit reasons, auto-applied sizing |
| 8767 | `MLService_Trend` | Trend TCN predictions + its own dashboard |
