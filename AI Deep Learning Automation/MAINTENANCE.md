# Maintaining the models

How the ML side is kept healthy: detecting poisoned training data, running validation tests, and retraining. Everything here is served by `MLService` (:8765) and `MLService_Trend` (:8767) and surfaced on their model-health dashboards — no separate tooling to install.

There are two loops:

- **Automated** — models retrain daily (14:00 entry/exit, 14:05 trend) on the strategies' own trade logs, behind readiness gates, and the light integrity checks run continuously.
- **Manual** — you can run any check, the validation (ablation) tests, or a retrain on demand from the dashboard or the API.

---

## Where to look

Open the **model-health dashboard** at **http://localhost:8765/** (pages: `/`, `/dashboard`, `/ops`) — and **http://localhost:8767/** for the trend model. Each check shows a **green / amber / red** verdict with a **Run** button, per model group (per instrument + data series). `/health`, `/stats`, and `/schema` return the same data as JSON.

---

## 1. Data-integrity checks (poison detection)

These catch training data that would quietly teach the model something false. They're light and safe to run anytime.

| Check (`name`) | Catches |
|----------------|---------|
| `cross_symbol` | A trade on one instrument leaking into another's training set |
| `dup_scan` | The same setup counted many times (e.g. shadow-tested across templates) |
| `feature_psi` | Feature distributions drifting from what the model trained on (PSI) |
| `label_drift` | A sudden shift in the long/short/no-trade mix (a labeling bug) |
| `empty_window` | Empty-window rows that poison the veto signal |
| `determinism` | Non-deterministic inference (same input → different output) |

**Run one:**
```bash
curl -X POST http://localhost:8765/run-check -H "Content-Type: application/json" -d '{"name":"dup_scan"}'
# then read the result:
curl "http://localhost:8765/verification-output?check=dup_scan"
```
Or click the check's **Run** button on the dashboard.

## 2. Validation tests (ablation)

These confirm the model is learning real signal, not noise. They're **heavy** (they retrain variants), so they're blocked during the **13:50–14:10** auto-retrain window.

| Test (`name`) | Validates |
|---------------|-----------|
| `permutation` | With labels shuffled, the model should **not** be able to learn — if it still "predicts" well, there's leakage |
| `null_feature` | An injected random feature should **not** gain importance |
| `split_gap` | Performance holds across a proper train/test time split (no lookahead) |
| `seed_variance` | Results are stable across random seeds (not a lucky init) |

Run them the same way (`/run-check` with the name above), or use **`POST /run-ablation`** and read **`GET /ablation-output`** for the combined ablation report. On the dashboard these are the **Run** buttons in the ablation section.

## 3. Retraining

- **Automatic:** daily at **14:00** (entry/exit) and **14:05** (trend), triggered by [`infrastructure/scheduled-tasks/ml_daily.ps1`](infrastructure/scheduled-tasks/ml_daily.ps1). Retrain frequency also scales with group size so large groups don't retrain on every new tick.
- **Manual:** open **http://localhost:8765/retrain** (an HTML trigger page) or `POST /retrain`; trend is `POST /retrain-trend`.
- **Readiness gates** decide whether a freshly-trained model is actually used:
  - *Entry model* must beat a **base-rate + directional** baseline, or it's ignored and the strategy falls back to rule-based signals.
  - *Exit model* must clear a minimum **AUC** and a minimum count of **minority-class examples**, both on held-out data.
  - Every gate that blocks a model logs why.

---

## Reading verdicts & remediating

- **Green** — healthy, no action.
- **Amber** — a heads-up on thin evidence (e.g. a check on too few recent rows). Watch it; don't act yet.
- **Red** — a real problem. Open the check's detail (`/verification-output?check=…`) to see which model group and rows are implicated, then remediate the data — typically **archive the offending rows** (quarantine, never hard-delete) and retrain. The checks are designed to go back to green once the bad data is out.

Real examples this suite has caught (see the [changelog](https://github.com/dog23/park/blob/main/CHANGELOG.md)): a duplicate-window + feature-schema-mix poisoning event, 13 "exitless" trades contributing ~22,651 misleading rows, and a cross-instrument leakage event that required purging 30 rows.

---

## Quick API reference

| Endpoint | Purpose |
|----------|---------|
| `POST /run-check {"name": …}` | Run one integrity/validation check |
| `GET /verification-output?check=…` | Full per-group result of the last run |
| `POST /run-ablation` · `GET /ablation-output` | Run / read the ablation report |
| `GET /retrain` (page) · `POST /retrain` · `POST /retrain-trend` | Retrain |
| `GET /health` · `GET /stats` · `GET /schema` | Health, counts, feature schema |
