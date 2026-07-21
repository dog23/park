# 1 · The AI — models that decide every trade

Two FastAPI microservices serve machine-learning predictions to the strategies, retrain daily, and verify the data they train on.

- **`MLService/`** (port 8765) — entry model, exit model, verification suite, training pipeline, and the model-health dashboard. Serves `temalimit`.
- **`MLService_Trend/`** (port 8767) — the trend TCN and its own verification + dashboard. Serves `TrendTcnStrategy`.

## The models

### Entry model — *should I take this trade, and which way?*
Given a candidate setup's feature vector, predicts **long / short / no-trade**.

- **Trained per instrument *and* per data series.** ES on a 1-minute chart and NQ on a Renko chart get separate models — the same feature means different things on different contracts and bar types, and one global model both averages away the edge and leaks signal across instruments.
- **Gated behind a base-rate + directional test.** Before the model is allowed to influence a trade it must (a) beat the naïve "always predict the majority class" baseline and (b) demonstrate genuine directional edge. If it can't, its vote is discarded and the strategy trades on plain rules. Conservatism is the default.
- Files: [`ml_model.py`](MLService/ml_model.py), [`feature_utils.py`](MLService/feature_utils.py).

### Exit model — *should I stay in this trade?*
A **separate** model that watches open positions and predicts **hold vs. exit early**.

- Kept independent from the entry model on purpose: entering and holding have different features, base rates, and costs of being wrong.
- **Readiness-gated:** it only loads if it clears a minimum AUC *and* a minimum count of minority-class examples, both measured on held-out data — so a lucky score computed off a handful of examples can never take control of live exits. Both reasons are logged when it's rejected.
- File: [`exit_model.py`](MLService/exit_model.py).

### Trend model — *is this breakout real?*
A temporal convolutional network (TCN) for multi-market trend breakouts across oil, FX, index futures, and gold. It re-scores its own confidence every few bars and exits early when conviction drops.

- Files: [`trend_model.py`](MLService_Trend/trend_model.py), [`trend_utils.py`](MLService_Trend/trend_utils.py).

## Architecture & how the models learn

All three are small **PyTorch** networks, trained **per instrument + data series** on the strategies' own logged trades — each setup's features are the input, its realized outcome is the label. Training runs daily on CPU, with a validation split and best-validation-loss checkpointing.

| Model | Network type | Shape |
|-------|-------------|-------|
| Entry (`TemporalCnn`) | 1-D temporal **CNN** | 3× `Conv1d` (48→64→64, kernels 5/5/3) with BatchNorm / ReLU / Dropout 0.1 → linear head → 3 classes (long / short / no-trade) |
| Exit (`TradeExitModel`) | **LSTM + Transformer** sequence model | 2-layer LSTM (hidden 48) + a 1-layer Transformer encoder over the trade's last ≤128 bars, plus a static-context branch → a single hold/exit logit |
| Trend (`TrendTcn`) | **TCN** — dilated causal CNN | residual temporal blocks, dilations 1/2/4/8, kernel 3, 32 channels → linear head → 3 classes |

**How training works:**
- **Loss** — cross-entropy for the 3-class entry and trend models (trend adds class weights to handle label imbalance); binary cross-entropy (`BCEWithLogitsLoss`) for the exit model.
- **Optimizer** — AdamW, weight decay `1e-4`; ~8 epochs (entry) / 30 (trend), keeping the best-validation checkpoint so an overfit late epoch is discarded.
- **Sample weighting** — shadow-traded samples train at weight **0.2**, so they inform the model without outvoting real live trades.
- **Sequences (exit)** — variable-length trades are packed/padded, inputs normalized by stored mean/std, and a little noise is added to sequences during training for robustness.
- Features are computed strategy-side and validated in [`feature_utils.py`](MLService/feature_utils.py) / [`trend_utils.py`](MLService_Trend/trend_utils.py). **The full feature list — what each one is and how it's computed — is in [../FEATURES.md](../FEATURES.md).**

## Data-integrity verification suites

Because the models train on the system's own trade history, corrupt training data is a real risk. Data integrity is handled by a continuously-running set of checks — 9–10 per model service, surfaced on the health dashboard with one-click ablation runs. For how to run these, read the verdicts, and retrain, see **[../MAINTENANCE.md](../MAINTENANCE.md)**.

Representative checks (see [`MLService/verification.py`](MLService/verification.py) and [`MLService_Trend/verification.py`](MLService_Trend/verification.py)):

| Check | Catches |
|-------|---------|
| Cross-instrument leakage | A trade on one symbol contaminating another symbol's training set |
| Duplicate-window scan | The same setup counted many times because it was shadow-tested across templates |
| Feature-schema mix | Rows written under two different feature layouts poisoning one model |
| Label drift | A sudden shift in the long/short/no-trade mix that signals a labeling bug |
| Exit-label integrity | "Exitless" trades whose close was never recorded — which would teach the model that positions never close |
| AUC / minority-count floors | A model being trusted off too few examples |

Real incidents these caught (from the project changelog): a duplicate-window + feature-schema-mix poisoning event; 13 exitless trades contributing **22,651** misleading training rows; and a leakage event that required purging 30 cross-instrument rows.

## The training pipeline — reproducible and bounded

- **Daily retrain at a fixed time** (14:00 for entry/exit, 14:05 for trend — staggered so the two services don't contend for CPU), with manual trigger available.
- **Validation splits** on every retrain; models are compared against their gates before they're allowed to serve.
- **Per-group cadence** — retrain frequency scales with group size (`max(200, total // 20)`), so a 70K-sample group stops retraining on every 200 new ticks.
- **Bounded resources** — sequence-length caps in both training and inference prevent an O(N²) prefix blow-up that once OOM'd the box; a thread cap keeps a retrain from starving the live service.
- Files: [`train_template.py`](MLService/train_template.py), [`service.py`](MLService/service.py), [`app.py`](MLService_Trend/app.py).

## Serving

- **FastAPI + uvicorn**, one service per model family, reached over localhost by the C# strategies.
- **CPU-optimized batch-1 inference.** The hot path is single-prediction latency, not throughput; the CPU path measured *faster* than a GPU for batch-1, so the models are served on CPU with a bounded thread pool.
- **Fails safe.** If a service is down or slow, the strategy degrades to rule-based trading rather than blocking — a model outage must never freeze order handling.
