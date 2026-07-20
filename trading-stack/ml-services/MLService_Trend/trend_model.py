from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from trend_utils import group_key, normalize_symbol

# Per-bar feature order matches the locked-in entry gate:
#   Donchian breakout position, SuperTrend, LinReg slope, ADX, Choppiness,
#   Relative Volume, Order Flow Delta, ATR context. Same feature set and
# thresholds for every symbol (NQ/YM/ES/RTY) -- only the per-symbol weights
# differ, so entries stay comparable across symbols while training data never
# crosses symbol boundaries.
FEATURE_NAMES = [
    "close_to_donchian_high",
    "close_to_donchian_low",
    "supertrend_direction",
    "supertrend_distance",
    "linreg_slope_norm",
    "adx",
    "choppiness_index",
    "relative_volume",
    "order_flow_delta",
    "atr_norm",
]
N_FEATURES = len(FEATURE_NAMES)
CLASSES = ["long", "short", "no_trade"]

# Rolling window of bars fed into the TCN per prediction. 30 bars gives the
# dilated stack (dilations 1/2/4/8 below) a receptive field comfortably past
# the whole window, so it isn't the bottleneck.
WINDOW_SIZE = 30

# Modest floor -- trend setups fire less often than mean-reversion triggers,
# so we don't want to demand as many samples before a first model exists.
MIN_SAMPLES_PER_GROUP = 100

# Near-miss samples (gate conditions 3-5 of 6 matched, forward-labeled the
# same way as real candidates) supplement the sparse real-trigger stream.
# Weighted well below 1.0 so they can never outvote real signal; missing
# entries (match_count outside 3-5, or a malformed record) get weight 0.
NEAR_MISS_WEIGHTS = {5: 0.4, 4: 0.25, 3: 0.2}

# Thresholds for "is there enough data to trust an ablation of NEAR_MISS_WEIGHTS."
# READY_MIN_LIVE: the no-near-miss baseline needs its own healthy 70/15/15
#   split -- MIN_SAMPLES_PER_GROUP is the bare minimum to train at all, not
#   enough for a stable val/test read, hence the margin above it.
# READY_MIN_NEAR_MISS_PER_BUCKET: each of the 3/4/5-match buckets needs its
#   own volume, since they're weighted (and therefore evaluated) separately.
# READY_MIN_DIRECTIONAL_PER_BUCKET: long+short count within a bucket -- below
#   this, long/short recall for that bucket comes back undefined (None),
#   which is exactly the metric an ablation exists to compare.
READY_MIN_LIVE = 150
READY_MIN_NEAR_MISS_PER_BUCKET = 50
READY_MIN_DIRECTIONAL_PER_BUCKET = 10


def assess_group_readiness(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Breaks a group's sample rows down by source (live vs. near-miss
    bucket) and checks against READY_* thresholds. Shared by the dashboard
    (live view across all groups) and tools/ablate_near_miss_weights.py (a
    single group, on demand) so both agree on what "ready" means."""
    live = [r for r in rows if not (r.get("metadata") or {}).get("near_miss")]
    buckets: Dict[int, List[Dict[str, Any]]] = {3: [], 4: [], 5: []}
    other_near_miss = 0
    for r in rows:
        meta = r.get("metadata") or {}
        if not meta.get("near_miss"):
            continue
        mc = meta.get("match_count")
        if mc in buckets:
            buckets[mc].append(r)
        else:
            other_near_miss += 1

    bucket_reports: Dict[int, Dict[str, Any]] = {}
    all_buckets_ready = True
    for mc, bucket_rows in buckets.items():
        directional = sum(1 for r in bucket_rows if str(r.get("label", "")).lower() in ("long", "short"))
        ready = len(bucket_rows) >= READY_MIN_NEAR_MISS_PER_BUCKET and directional >= READY_MIN_DIRECTIONAL_PER_BUCKET
        all_buckets_ready = all_buckets_ready and ready
        bucket_reports[mc] = {"count": len(bucket_rows), "directional": directional, "ready": ready}

    live_ready = len(live) >= READY_MIN_LIVE

    return {
        "live_count": len(live),
        "live_ready": live_ready,
        "buckets": bucket_reports,
        "other_near_miss_ignored": other_near_miss,
        "ready": live_ready and all_buckets_ready,
    }


def _near_miss_weight(metadata: Dict[str, Any]) -> float:
    if not metadata or not metadata.get("near_miss"):
        return 1.0
    try:
        match_count = int(metadata.get("match_count", 0))
    except (TypeError, ValueError):
        return 0.0
    return NEAR_MISS_WEIGHTS.get(match_count, 0.0)


def _per_class_metrics(y_true: torch.Tensor, y_pred: torch.Tensor) -> Dict[str, Dict[str, Optional[float]]]:
    """Precision/recall/support per class. None (not 0) when undefined --
    e.g. recall is undefined with zero true examples of that class, which is
    a different situation from the model catching 0 of many."""
    metrics: Dict[str, Dict[str, Optional[float]]] = {}
    for idx, cls in enumerate(CLASSES):
        true_pos = int(((y_pred == idx) & (y_true == idx)).sum().item())
        pred_pos = int((y_pred == idx).sum().item())
        actual_pos = int((y_true == idx).sum().item())
        metrics[cls] = {
            "precision": (true_pos / pred_pos) if pred_pos > 0 else None,
            "recall": (true_pos / actual_pos) if actual_pos > 0 else None,
            "support": actual_pos,
        }
    return metrics


class Chomp1d(nn.Module):
    """Trims the extra right-side padding a causal conv leaves behind, so
    layer output length matches input length without looking into the future."""

    def __init__(self, chomp_size: int) -> None:
        super().__init__()
        self.chomp_size = chomp_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.chomp_size == 0:
            return x
        return x[:, :, :-self.chomp_size].contiguous()


class TemporalBlock(nn.Module):
    """One dilated-causal-conv residual block: two conv layers at the same
    dilation, each followed by chomp/ReLU/dropout, plus a residual connection
    (1x1 conv if channel counts differ)."""

    def __init__(self, n_inputs: int, n_outputs: int, kernel_size: int, dilation: int, dropout: float = 0.15) -> None:
        super().__init__()
        padding = (kernel_size - 1) * dilation

        self.conv1 = nn.Conv1d(n_inputs, n_outputs, kernel_size, padding=padding, dilation=dilation)
        self.chomp1 = Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)

        self.conv2 = nn.Conv1d(n_outputs, n_outputs, kernel_size, padding=padding, dilation=dilation)
        self.chomp2 = Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)

        self.downsample = nn.Conv1d(n_inputs, n_outputs, 1) if n_inputs != n_outputs else None
        self.relu_out = nn.ReLU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.dropout1(self.relu1(self.chomp1(self.conv1(x))))
        out = self.dropout2(self.relu2(self.chomp2(self.conv2(out))))
        residual = x if self.downsample is None else self.downsample(x)
        return self.relu_out(out + residual)


class TrendTcn(nn.Module):
    """Temporal Convolutional Network: stacked dilated causal conv blocks
    (dilations 1, 2, 4, 8) give a receptive field well past WINDOW_SIZE bars
    without recurrence. Untried elsewhere in this project -- the entry model
    uses a plain (non-dilated) CNN, the exit model uses LSTM+Transformer."""

    def __init__(self, n_features: int = N_FEATURES, n_classes: int = len(CLASSES), channels: int = 32) -> None:
        super().__init__()
        dilations = [1, 2, 4, 8]
        layers = []
        in_ch = n_features
        for dilation in dilations:
            layers.append(TemporalBlock(in_ch, channels, kernel_size=3, dilation=dilation))
            in_ch = channels
        self.tcn = nn.Sequential(*layers)
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(channels, 32),
            nn.ReLU(),
            nn.Dropout(0.15),
            nn.Linear(32, n_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # API shape is [batch, window, features]; Conv1d expects [batch, features, window].
        x = x.transpose(1, 2)
        return self.head(self.tcn(x))


@dataclass
class ModelState:
    model: TrendTcn
    mean: torch.Tensor
    std: torch.Tensor
    version: int
    trained: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class TrendMlEngine:
    """Registry of per-(symbol, data_series) trend models. Each group gets its
    own TCN, its own checkpoint file, and its own held-out validation split.
    Groups are lazily created on first use."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.model_dir = root / "weights"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.samples_path = root / "data" / "trend_samples.jsonl"
        self.root.joinpath("data").mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, ModelState] = {}

    # ---------------------------------------------------------------- paths

    def model_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"{key}_trend_weights.pt"

    def metadata_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"{key}_trend_weights.json"

    def history_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"{key}_trend_history.jsonl"

    # ------------------------------------------------------------- registry

    def _get_or_create_state(self, key: str) -> ModelState:
        if key in self._states:
            return self._states[key]

        model = TrendTcn()
        mean = torch.zeros(N_FEATURES, dtype=torch.float32)
        std = torch.ones(N_FEATURES, dtype=torch.float32)
        version = 0
        trained = False
        metadata: Dict[str, Any] = {}

        model_path = self.model_path_for_group(key)
        if model_path.exists():
            try:
                bundle = torch.load(model_path, map_location="cpu")
                saved_features = bundle.get("feature_names", [])
                if saved_features and len(saved_features) != N_FEATURES:
                    raise ValueError(
                        f"checkpoint has {len(saved_features)} features, service expects {N_FEATURES}"
                    )
                model.load_state_dict(bundle["model"])
                mean = bundle.get("mean", mean).float()
                std = bundle.get("std", std).float().clamp_min(1e-6)
                version = int(bundle.get("version", 0))
                trained = version > 0
            except Exception:
                model = TrendTcn()
                mean = torch.zeros(N_FEATURES, dtype=torch.float32)
                std = torch.ones(N_FEATURES, dtype=torch.float32)
                version = 0
                trained = False

        metadata_path = self.metadata_path_for_group(key)
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}

        model.eval()
        state = ModelState(model=model, mean=mean, std=std, version=version, trained=trained, metadata=metadata)
        self._states[key] = state
        return state

    def known_groups(self) -> List[str]:
        groups = set(self._states.keys())
        for path in self.model_dir.glob("*_trend_weights.pt"):
            groups.add(path.stem.replace("_trend_weights", ""))
        for path in self.model_dir.glob("*_trend_weights.json"):
            groups.add(path.stem.replace("_trend_weights", ""))
        return sorted(groups)

    def save(self, key: str) -> None:
        state = self._get_or_create_state(key)
        torch.save(
            {
                "model": state.model.state_dict(),
                "mean": state.mean,
                "std": state.std,
                "version": state.version,
                "feature_names": FEATURE_NAMES,
                "classes": CLASSES,
                "window_size": WINDOW_SIZE,
            },
            self.model_path_for_group(key),
        )

    # -------------------------------------------------------------- window

    def coerce_window(self, window: List[List[float]]) -> torch.Tensor:
        rows = [[float(v) for v in row[:N_FEATURES]] for row in window]
        if not rows:
            rows = [[0.0] * N_FEATURES]

        rows = [row + [0.0] * (N_FEATURES - len(row)) for row in rows]
        if len(rows) < WINDOW_SIZE:
            pad = [rows[0]] * (WINDOW_SIZE - len(rows))
            rows = pad + rows
        elif len(rows) > WINDOW_SIZE:
            rows = rows[-WINDOW_SIZE:]

        return torch.tensor(rows, dtype=torch.float32)

    def normalize_window(self, key: str, window: List[List[float]]) -> torch.Tensor:
        state = self._get_or_create_state(key)
        x = self.coerce_window(window)
        return (x - state.mean.view(1, -1)) / state.std.view(1, -1)

    # ------------------------------------------------------------- predict

    def predict(self, symbol: str, bars_period: str, window: List[List[float]], min_confidence: float) -> Dict[str, Any]:
        key = group_key(symbol, bars_period)
        state = self._get_or_create_state(key)

        if not state.trained:
            return {
                "action": "no_trade",
                "raw_action": "no_trade",
                "confidence": 0.0,
                "probabilities": {name: 0.0 for name in CLASSES},
                "model_version": state.version,
                "model_ready": False,
                "group": key,
                "reason": "warming_up",
                "status": "warming_up",
                "feature_names": FEATURE_NAMES,
            }

        x = self.normalize_window(key, window).unsqueeze(0)
        state.model.eval()
        with torch.no_grad():
            logits = state.model(x)
            probs = torch.softmax(logits, dim=1)[0]

        best_index = int(torch.argmax(probs).item())
        confidence = float(probs[best_index].item())
        action = CLASSES[best_index]
        if confidence < min_confidence:
            action = "no_trade"

        status = self.classify_direction_status(
            state.trained,
            state.metadata.get("val_acc"),
            state.metadata.get("test_acc"),
            state.metadata.get("holdout_per_class"),
            action,
        )

        return {
            "action": action,
            "raw_action": CLASSES[best_index],
            "confidence": confidence,
            "probabilities": {name: float(probs[i].item()) for i, name in enumerate(CLASSES)},
            "model_version": state.version,
            "model_ready": True,
            "group": key,
            "status": status,
            "feature_names": FEATURE_NAMES,
        }

    # --------------------------------------------------------------- log

    def append_jsonl(self, path: Path, record: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def log_training_sample(self, record: Dict[str, Any]) -> None:
        self.append_jsonl(self.samples_path, record)

    # ------------------------------------------------------------- loading

    def _iter_training_rows(self):
        if not self.samples_path.exists():
            return
        for line in self.samples_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue

    def discover_group_sample_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            key = group_key(item.get("symbol", ""), item.get("bars_period", ""))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def discover_group_sample_breakdown(self) -> Dict[str, Dict[str, int]]:
        """Counts per group by source, for dashboard visibility -- mirrors the
        shadow/live split MLService already exposes via /stats."""
        breakdown: Dict[str, Dict[str, int]] = {}
        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            key = group_key(item.get("symbol", ""), item.get("bars_period", ""))
            entry = breakdown.setdefault(key, {"live": 0, "near_miss": 0})
            metadata = item.get("metadata") or {}
            if metadata.get("near_miss"):
                entry["near_miss"] += 1
            else:
                entry["live"] += 1
        return breakdown

    def load_training_samples_for_group(self, key: str) -> Tuple[torch.Tensor, torch.Tensor, List[str], torch.Tensor]:
        xs: List[torch.Tensor] = []
        ys: List[int] = []
        timestamps: List[str] = []
        weights: List[float] = []

        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            row_key = group_key(item.get("symbol", ""), item.get("bars_period", ""))
            if row_key != key:
                continue
            xs.append(self.coerce_window(item.get("window", [])))
            ys.append(CLASSES.index(label))
            timestamps.append(str(item.get("timestamp") or item.get("logged_at") or ""))
            weights.append(_near_miss_weight(item.get("metadata") or {}))

        if not xs:
            return (
                torch.empty(0, WINDOW_SIZE, N_FEATURES),
                torch.empty(0, dtype=torch.long),
                [],
                torch.empty(0, dtype=torch.float32),
            )

        return torch.stack(xs), torch.tensor(ys, dtype=torch.long), timestamps, torch.tensor(weights, dtype=torch.float32)

    # ------------------------------------------------------------- retrain

    def retrain_group(
        self,
        key: str,
        epochs: int = 30,
        batch_size: int = 32,
        learning_rate: float = 1e-3,
        permute_labels: bool = False,
        publish: bool = True,
        from_scratch: bool = False,
        randomize_features: bool = False,
        shuffle_split: bool = False,
    ) -> Dict[str, Any]:
        # permute_labels/publish/from_scratch are the verification suite's hooks
        # (see verification.py). Defaults leave the real retrain path untouched:
        #   permute_labels -- shuffle labels before the split so any real
        #     window->label association is destroyed (permutation/leakage test).
        #   from_scratch   -- start from a fresh TrendTcn instead of warm-starting
        #     the live model, so a permuted-label fit can't inherit real signal.
        #   publish=False  -- train + evaluate but never touch the live
        #     ModelState or disk; just return the metrics.
        x_raw, y, _timestamps, w = self.load_training_samples_for_group(key)
        n = len(y)
        if n < MIN_SAMPLES_PER_GROUP:
            return {
                "trained": False,
                "reason": f"Need at least {MIN_SAMPLES_PER_GROUP} labeled samples for this group.",
                "group": key,
                "samples": int(n),
            }

        if permute_labels:
            # Shuffle the whole label vector before the time-ordered split. Class
            # distribution is preserved (same labels, reordered); only the tie to
            # each window is broken -- which is exactly what a permutation test
            # needs. Per-sample near-miss weights (w) stay with their rows since
            # they don't depend on the label.
            y = y[torch.randperm(n)]

        if randomize_features:
            # Null-feature baseline: windows replaced with pure noise while
            # labels keep their true order. Any above-base-rate accuracy now has
            # to come from somewhere other than the features -- i.e. an eval bug.
            x_raw = torch.randn_like(x_raw)

        if shuffle_split:
            # Random split instead of time-ordered (rows shuffled together, so
            # window/label/weight stay glued). Used by the walk-forward-gap
            # check: if a random split scores far above the honest chronological
            # split, temporally-adjacent near-duplicate windows are leaking
            # across the split boundary. Fixed seed so reruns are comparable.
            perm = torch.randperm(n, generator=torch.Generator().manual_seed(0))
            x_raw, y, w = x_raw[perm], y[perm], w[perm]

        # Time-ordered split -- samples are appended chronologically, so a
        # slice split keeps validation/test genuinely out-of-sample instead of
        # leaking future bars into training.
        train_end = max(1, int(n * 0.70))
        val_end = max(train_end + 1, int(n * 0.90))
        val_end = min(val_end, n)
        if train_end >= n:
            train_end = max(1, n - 2)
            val_end = max(train_end + 1, n - 1)

        x_train, y_train, w_train = x_raw[:train_end], y[:train_end], w[:train_end]
        x_val, y_val = x_raw[train_end:val_end], y[train_end:val_end]
        x_test, y_test = x_raw[val_end:], y[val_end:]

        # Train a detached copy with locally held stats, and only publish the
        # finished ModelState at the very end (a single dict assignment, so any
        # in-flight predict() keeps a consistent old model+mean+std). Training
        # state.model in place raced live predictions -- auto-retrain fires at
        # 14:05, mid-session, and predict() could see half-updated weights.
        state = self._get_or_create_state(key)
        mean = x_train.reshape(-1, N_FEATURES).mean(dim=0)
        std = x_train.reshape(-1, N_FEATURES).std(dim=0).clamp_min(1e-6)

        def norm(x: torch.Tensor) -> torch.Tensor:
            return (x - mean.view(1, 1, -1)) / std.view(1, 1, -1)

        model = TrendTcn()
        if not from_scratch:
            model.load_state_dict(state.model.state_dict())
        model.train()
        loader = DataLoader(TensorDataset(norm(x_train), y_train, w_train), batch_size=max(1, min(batch_size, len(y_train))), shuffle=True)
        optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)

        # Trend setups are rare by construction (no_trade dominates every
        # group's label distribution), so unweighted cross-entropy lets the
        # model minimize loss by just always predicting no_trade. Inverse-
        # frequency weighting keeps long/short gradient signal from being
        # drowned out. Classes absent from this particular train split get
        # weight 0 (nothing to learn from), not inf.
        class_counts = torch.bincount(y_train, minlength=len(CLASSES)).float()
        class_weights = torch.where(class_counts > 0, 1.0 / class_counts, torch.zeros_like(class_counts))
        if class_weights.sum() > 0:
            class_weights = class_weights * (len(CLASSES) / class_weights.sum())
        criterion = nn.CrossEntropyLoss(weight=class_weights)
        # Per-sample near-miss weighting stacks multiplicatively on top of the
        # per-class weighting above: reduction="none" keeps the class-weight
        # scaling per example, then each example is further scaled by its
        # near-miss weight (1.0 for real samples) before averaging.
        criterion_per_sample = nn.CrossEntropyLoss(weight=class_weights, reduction="none")

        best_state = None
        best_val_loss = float("inf")
        patience = 6
        stale = 0

        for _epoch in range(max(1, epochs)):
            model.train()
            for xb, yb, wb in loader:
                optimizer.zero_grad()
                loss = (criterion_per_sample(model(xb), yb) * wb).sum() / wb.sum().clamp_min(1e-6)
                loss.backward()
                optimizer.step()

            model.eval()
            with torch.no_grad():
                if len(y_val):
                    val_logits = model(norm(x_val))
                    val_loss = float(criterion(val_logits, y_val).item())
                else:
                    val_loss = float("inf")

            if val_loss < best_val_loss - 1e-6:
                best_val_loss = val_loss
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                stale = 0
            else:
                stale += 1
                if stale >= patience:
                    break

        if best_state is not None:
            model.load_state_dict(best_state)
        model.eval()

        with torch.no_grad():
            train_logits = model(norm(x_train))
            train_acc = float((train_logits.argmax(1) == y_train).float().mean().item())

            if len(y_val):
                val_logits = model(norm(x_val))
                val_pred = val_logits.argmax(1)
                val_acc = float((val_pred == y_val).float().mean().item())
            else:
                val_pred = torch.empty(0, dtype=torch.long)
                val_acc = None

            if len(y_test):
                test_logits = model(norm(x_test))
                test_pred = test_logits.argmax(1)
                test_acc = float((test_pred == y_test).float().mean().item())
            else:
                test_pred = torch.empty(0, dtype=torch.long)
                test_acc = None

            # Overall accuracy is misleading here: no_trade dominates every
            # group's label distribution (breakout+trend+ADX+chop+relvol all
            # aligning is rare by construction), so a model that always
            # predicts no_trade scores ~90% without having learned anything.
            # Per-class recall/precision on the combined held-out set is what
            # actually shows whether long/short are being identified at all.
            holdout_true = torch.cat([y_val, y_test])
            holdout_pred = torch.cat([val_pred, test_pred])
            holdout_per_class = _per_class_metrics(holdout_true, holdout_pred)

        # Majority-class base rate per split -- the accuracy a do-nothing model
        # (always predict the most common label) would score. The permutation
        # test compares val_acc against this: a shuffled-label fit should not
        # beat it. Harmless, useful context for the honest path too.
        def _base_rate(labels: torch.Tensor) -> Optional[float]:
            if not len(labels):
                return None
            counts = torch.bincount(labels, minlength=len(CLASSES)).float()
            return float((counts.max() / counts.sum()).item())

        val_base_rate = _base_rate(y_val)
        test_base_rate = _base_rate(y_test)

        timestamp = datetime.now(timezone.utc).isoformat()
        symbol_part, _, series_part = key.partition("_")
        result = {
            "trained": True,
            "group": key,
            "symbol": symbol_part,
            "data_series_key": series_part,
            "last_trained": timestamp,
            "samples": int(n),
            "train_samples": int(train_end),
            "val_samples": int(val_end - train_end),
            "test_samples": int(n - val_end),
            "train_acc": train_acc,
            "val_acc": val_acc,
            "test_acc": test_acc,
            "val_base_rate": val_base_rate,
            "test_base_rate": test_base_rate,
            "holdout_per_class": holdout_per_class,
            "published": publish,
        }

        # A dry run (verification) trains and evaluates but must not disturb the
        # live model or its saved artifacts -- return metrics only.
        if not publish:
            result["model_version"] = state.version
            return result

        new_state = ModelState(model=model, mean=mean, std=std, version=state.version + 1, trained=True)
        self._states[key] = new_state
        self.save(key)
        result["model_version"] = new_state.version
        result["model_path"] = str(self.model_path_for_group(key))

        new_state.metadata = result
        self.metadata_path_for_group(key).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        self.append_jsonl(self.history_path_for_group(key), {
            "ts": timestamp, "val_acc": val_acc, "test_acc": test_acc, "samples": int(n),
        })

        return result

    def retrain(
        self,
        symbol: Optional[str] = None,
        bars_period: Optional[str] = None,
        epochs: int = 30,
        batch_size: int = 32,
        learning_rate: float = 1e-3,
    ) -> Dict[str, Any]:
        if symbol and bars_period is not None:
            key = group_key(symbol, bars_period)
            return self.retrain_group(key, epochs=epochs, batch_size=batch_size, learning_rate=learning_rate)

        counts = self.discover_group_sample_counts()
        if symbol:
            # A symbol without a bars_period scopes the retrain to that symbol's
            # groups -- previously this fell through and retrained every group.
            prefix = normalize_symbol(symbol) + "_"
            counts = {k: c for k, c in counts.items() if k.startswith(prefix)}
        eligible = sorted(k for k, c in counts.items() if c >= MIN_SAMPLES_PER_GROUP)
        results: Dict[str, Any] = {}
        for key in eligible:
            results[key] = self.retrain_group(key, epochs=epochs, batch_size=batch_size, learning_rate=learning_rate)

        return {
            "trained_groups": len(eligible),
            "total_groups_seen": len(counts),
            "eligible_groups": eligible,
            "results": results,
        }

    # -------------------------------------------------------------- health

    @staticmethod
    def classify_direction_status(
        model_ready: bool,
        val_acc: Optional[float],
        test_acc: Optional[float],
        per_class: Optional[Dict[str, Any]],
        direction: str,
    ) -> str:
        """Single source of truth for per-direction quality gating -- used by both
        the /model-health dashboard status pill and predict()'s live ML gate, so a
        direction is never shown blocked on the dashboard while the strategy trades
        it anyway. Mirrors the dashboard's original thresholds one-for-one
        (warming_up / no_direction_tests / low_direction_recall / do_not_use /
        overfitting / caution / good_to_use)."""
        if not model_ready or val_acc is None:
            return "warming_up"

        if direction in ("long", "short"):
            entry = (per_class or {}).get(direction) or {}
            support = entry.get("support") or 0
            recall = entry.get("recall")
            if support == 0:
                return "no_direction_tests"
            if recall is None or float(recall) < 0.50:
                return "low_direction_recall"

        if val_acc < 0.50:
            return "do_not_use"
        gap = abs(test_acc - val_acc) if test_acc is not None else 0.0
        if gap > 0.10:
            return "overfitting"
        if val_acc < 0.65:
            return "caution"
        return "good_to_use"

    @staticmethod
    def classify_group_status(model_ready: bool, val_acc: Optional[float], test_acc: Optional[float]) -> str:
        if not model_ready or val_acc is None:
            return "warming_up"
        if val_acc < 0.50:
            return "do_not_use"
        gap = abs(test_acc - val_acc) if test_acc is not None else 0.0
        if gap > 0.10:
            return "overfitting"
        if val_acc < 0.65:
            return "caution"
        return "good_to_use"

    def group_health(
        self,
        key: str,
        sample_counts: Optional[Dict[str, int]] = None,
        sample_breakdown: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> Dict[str, Any]:
        state = self._get_or_create_state(key)
        counts = sample_counts if sample_counts is not None else self.discover_group_sample_counts()
        samples = counts.get(key, 0)
        breakdown = sample_breakdown if sample_breakdown is not None else self.discover_group_sample_breakdown()
        symbol_part, _, series_part = key.partition("_")
        val_acc = state.metadata.get("val_acc")
        test_acc = state.metadata.get("test_acc")
        return {
            "group": key,
            "symbol": symbol_part,
            "data_series_key": series_part,
            "samples": samples,
            "live_samples": breakdown.get(key, {}).get("live", 0),
            "near_miss_samples": breakdown.get(key, {}).get("near_miss", 0),
            "warmup_remaining": max(0, MIN_SAMPLES_PER_GROUP - samples),
            "model_ready": state.trained,
            "model_version": state.version,
            "last_trained": state.metadata.get("last_trained"),
            "val_acc": val_acc,
            "test_acc": test_acc,
            "holdout_per_class": state.metadata.get("holdout_per_class"),
            "status": self.classify_group_status(state.trained, val_acc, test_acc),
        }

    def all_group_health(self) -> Dict[str, Dict[str, Any]]:
        counts = self.discover_group_sample_counts()
        breakdown = self.discover_group_sample_breakdown()
        keys = sorted(set(self.known_groups()) | set(counts.keys()))
        return {key: self.group_health(key, counts, breakdown) for key in keys}

    def all_group_ablation_readiness(self) -> Dict[str, Dict[str, Any]]:
        """Per-group progress toward READY_* thresholds for a meaningful
        NEAR_MISS_WEIGHTS ablation -- powers the dashboard's readiness card."""
        rows_by_group: Dict[str, List[Dict[str, Any]]] = {}
        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            key = group_key(item.get("symbol", ""), item.get("bars_period", ""))
            rows_by_group.setdefault(key, []).append(item)

        keys = sorted(set(self.known_groups()) | set(rows_by_group.keys()))
        return {key: assess_group_readiness(rows_by_group.get(key, [])) for key in keys}
