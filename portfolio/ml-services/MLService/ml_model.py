from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from feature_utils import group_key, normalize_symbol, data_series_key


WINDOW_SIZE = 50
FEATURE_NAMES = [
    "close_to_vwap",
    "close_to_mid_bb",
    "close_to_upper_bb",
    "close_to_lower_bb",
    "tema_slope",
    "macd_diff",
    "macd_diff_slope",
    "mfi",
    "mfi_slope",
    "rsi",
    "stoch_rsi",
    "stoch_rsi_slope",
    "atr",
    "bb_width",
    "vwap_slope",
    "body_ticks",
    "upper_wick_ticks",
    "lower_wick_ticks",
    "volume_z",
    "prior_bar_return",
    "symbol_hash_1",
    "symbol_hash_2",
    "dollars_per_tick_log",
    "price_scale_log",
    "bars_period_value_log",
    "bars_type_hash_1",
    "bars_type_hash_2",
    "bars_type_category",
]
N_FEATURES = len(FEATURE_NAMES)
CLASSES = ["long", "short", "no_trade"]


# A model must beat the always-majority-class baseline by this margin on the
# validation slice before it may gate live entries. Raw val_acc alone let
# degenerate always-no_trade models go good_to_use whenever the val slice was
# mostly no_trade (ES_1MINUTE July 18: val_acc 1.000 on a 100%-no_trade slice,
# then 12k+ live vetoes).
VAL_BASE_RATE_MARGIN = 0.05

# Minimum long/short rows in the validation slice; below this, directional
# skill is unverifiable no matter what val_acc says.
MIN_VAL_DIRECTIONAL = 10


def classify_entry_model_status(
    model_ready: bool,
    val_acc: Optional[float],
    test_acc: Optional[float],
    val_base_rate: Optional[float] = None,
    val_directional: Optional[int] = None,
) -> str:
    """Single source of truth for entry-model quality gating -- used by both
    the /model-health dashboard badge and predict()'s per-group ML gate, so a
    model is never shown as "good to use" while the strategy silently treats
    it differently (or vice versa). Thresholds mirror the original dashboard
    JS one-for-one (warming_up / do_not_use / overfitting / caution / good_to_use).

    val_base_rate / val_directional come from checkpoint metadata; both are
    None on checkpoints trained before 2026-07-18, which then grade on the
    original val_acc-only rules until their next retrain refreshes metadata."""
    if not model_ready or val_acc is None:
        return "warming_up"
    if val_acc < 0.50:
        return "do_not_use"
    gap = abs(test_acc - val_acc) if test_acc is not None else 0.0
    if gap > 0.10:
        return "overfitting"
    if val_base_rate is not None and val_acc < val_base_rate + VAL_BASE_RATE_MARGIN:
        return "caution"
    if val_directional is not None and val_directional < MIN_VAL_DIRECTIONAL:
        return "caution"
    if val_acc < 0.65:
        return "caution"
    return "good_to_use"

# Minimum labeled samples in a (symbol, data_series) group before it's even
# attempted for training. 20 total across 3 classes was never enough to learn
# anything real; 150 is still modest but at least gives each class a fighting
# chance of a few dozen examples.
MIN_SAMPLES_PER_GROUP = 150

# Shadow samples come from the strategy's paper-traded template sweep, not real
# fills. Their fill simulation is deliberately pessimistic but still can't model
# queue position, so they inform training at reduced weight and can never
# outvote real-fill samples.
SHADOW_SAMPLE_WEIGHT = 0.2


def _is_shadow_sample(metadata: Dict[str, Any]) -> bool:
    return bool(metadata.get("shadow")) or str(metadata.get("source") or "").lower() == "shadow"


def _has_trainable_window(item: Dict[str, Any]) -> bool:
    """Rows without a feature window are gate-veto telemetry (temalimit's
    LogLiveNoTrade never attaches one), not labeled observations: their
    'no_trade' label is the model's own output fed back, with no outcome
    attached. Found 2026-07-17: 124,846 of 135,038 rows (92.5%) were these,
    training as all-zero windows and inflating val_acc to ~1.0. They must
    never count as training data -- see ML_SYSTEM_GUIDE.txt changelog."""
    window = item.get("window")
    return isinstance(window, list) and len(window) > 0


# Thresholds for "is there enough data to trust an ablation of
# SHADOW_SAMPLE_WEIGHT." Live floor is set well above MIN_SAMPLES_PER_GROUP
# since the no-shadow baseline arm needs its own healthy 70/15/15 split, not
# just the bare minimum to train at all. Shadow needs its own volume plus
# long/short representation for the weight to actually matter in the result.
READY_MIN_LIVE = 200
READY_MIN_SHADOW = 200
READY_MIN_DIRECTIONAL_SHADOW = 30


def assess_group_readiness(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Breaks a group's sample rows down by source (live vs. shadow) and
    checks against READY_* thresholds. Shared by the dashboard (live view
    across all groups) and tools/ablate_shadow_weight.py (a single group, on
    demand) so both agree on what "ready" means."""
    live = [r for r in rows if not _is_shadow_sample(r.get("metadata") or {})]
    shadow = [r for r in rows if _is_shadow_sample(r.get("metadata") or {})]
    directional_shadow = sum(1 for r in shadow if str(r.get("label", "")).lower() in ("long", "short"))

    live_ready = len(live) >= READY_MIN_LIVE
    shadow_ready = len(shadow) >= READY_MIN_SHADOW and directional_shadow >= READY_MIN_DIRECTIONAL_SHADOW

    return {
        "live_count": len(live),
        "live_ready": live_ready,
        "shadow_count": len(shadow),
        "shadow_directional": directional_shadow,
        "shadow_ready": shadow_ready,
        "ready": live_ready and shadow_ready,
    }


def scan_entry_sample_counts(samples_path: Path) -> Dict[str, int]:
    """Standalone, picklable counterpart to MlEngine.discover_group_sample_counts.
    Kept free of any MlEngine instance state (loaded torch models, etc.) so it
    can run in a subprocess -- this file is 150MB+ and growing, and re-parsing
    it every 10s on an in-process thread was starving the GIL that request
    handlers (e.g. /log-exit-sample, /log-template-sample) need."""
    counts: Dict[str, int] = {}
    if not samples_path.exists():
        return counts
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        label = str(item.get("label", "")).lower()
        if label not in CLASSES:
            continue
        if not _has_trainable_window(item):
            continue
        metadata = item.get("metadata") or {}
        bars_period = metadata.get("bars_period", "")
        key = group_key(item.get("symbol", ""), bars_period)
        counts[key] = counts.get(key, 0) + 1
    return counts


def scan_entry_sample_counts_by_source(samples_path: Path) -> Dict[str, Dict[str, int]]:
    """Like scan_entry_sample_counts, but splits each group's eligible rows into
    live vs shadow. Per group returns {"total", "live", "shadow"} where
    total == live + shadow (backfill rows fold into "live", so the total still
    matches scan_entry_sample_counts' combined figure)."""
    counts: Dict[str, Dict[str, int]] = {}
    if not samples_path.exists():
        return counts
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        label = str(item.get("label", "")).lower()
        if label not in CLASSES:
            continue
        if not _has_trainable_window(item):
            continue
        metadata = item.get("metadata") or {}
        key = group_key(item.get("symbol", ""), metadata.get("bars_period", ""))
        is_shadow = bool(metadata.get("shadow")) or str(metadata.get("source") or "").lower() == "shadow"
        bucket = counts.setdefault(key, {"total": 0, "live": 0, "shadow": 0})
        bucket["total"] += 1
        bucket["shadow" if is_shadow else "live"] += 1
    return counts


def scan_entry_ablation_readiness(samples_path: Path) -> Dict[str, Dict[str, Any]]:
    """Standalone, picklable counterpart to MlEngine.all_group_ablation_readiness.
    Only keeps label+metadata per row (never the large "window" array) so the
    result stays cheap to ship back across the subprocess boundary."""
    rows_by_group: Dict[str, List[Dict[str, Any]]] = {}
    if not samples_path.exists():
        return {}
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        label = str(item.get("label", "")).lower()
        if label not in CLASSES:
            continue
        if not _has_trainable_window(item):
            continue
        metadata = item.get("metadata") or {}
        key = group_key(item.get("symbol", ""), metadata.get("bars_period", ""))
        rows_by_group.setdefault(key, []).append({"label": item.get("label"), "metadata": metadata})
    return {key: assess_group_readiness(rows) for key, rows in rows_by_group.items()}


class TemporalCnn(nn.Module):
    def __init__(self, n_features: int = N_FEATURES, n_classes: int = len(CLASSES)) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(n_features, 48, kernel_size=5, padding=2),
            nn.BatchNorm1d(48),
            nn.ReLU(),
            nn.Dropout(0.10),
            nn.Conv1d(48, 64, kernel_size=5, padding=2),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.Dropout(0.10),
            nn.Conv1d(64, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Linear(64, 48),
            nn.ReLU(),
            nn.Dropout(0.10),
            nn.Linear(48, n_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # API shape is [batch, window, features]; Conv1d expects [batch, features, window].
        x = x.transpose(1, 2)
        return self.head(self.net(x))


@dataclass
class ModelState:
    model: TemporalCnn
    mean: torch.Tensor
    std: torch.Tensor
    version: int
    trained: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class MlEngine:
    """Registry of per-(symbol, data_series) entry models.

    Each group gets its own TemporalCnn, its own checkpoint file, and its own
    held-out validation split. Groups are lazily created on first use so we
    don't need to know the full symbol/series universe up front.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.model_dir = root.parent
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.samples_path = root / "data" / "training_samples.jsonl"
        self.triggers_path = root / "data" / "triggers.jsonl"
        self.root.joinpath("data").mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, ModelState] = {}

    # ---------------------------------------------------------------- paths

    def model_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"TemaLimit_bb_vwap_tcnn_{key}.pt"

    def metadata_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"TemaLimit_bb_vwap_tcnn_{key}.json"

    def history_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"TemaLimit_bb_vwap_tcnn_{key}_history.jsonl"

    # ------------------------------------------------------------- registry

    def _new_model(self) -> TemporalCnn:
        # Factory hook so subclasses (TemplateEngine) can swap the output layer
        # without duplicating the checkpoint load/registry logic below.
        return TemporalCnn()

    def _get_or_create_state(self, key: str) -> ModelState:
        if key in self._states:
            return self._states[key]

        model = self._new_model()
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
                model = self._new_model()
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
        """All groups we have either a checkpoint, metadata file, or in-memory state for."""
        groups = set(self._states.keys())
        for path in self.model_dir.glob("TemaLimit_bb_vwap_tcnn_*.pt"):
            stem = path.stem.replace("TemaLimit_bb_vwap_tcnn_", "")
            groups.add(stem)
        for path in self.model_dir.glob("TemaLimit_bb_vwap_tcnn_*.json"):
            stem = path.stem.replace("TemaLimit_bb_vwap_tcnn_", "")
            if not stem.endswith("_history"):
                groups.add(stem)
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

        status = classify_entry_model_status(
            state.trained,
            state.metadata.get("val_acc"),
            state.metadata.get("test_acc"),
            state.metadata.get("val_base_rate"),
            state.metadata.get("val_directional"),
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

    def log_trigger(self, record: Dict[str, Any]) -> None:
        self.append_jsonl(self.triggers_path, record)

    def log_training_sample(self, record: Dict[str, Any]) -> None:
        self.append_jsonl(self.samples_path, record)

    # ------------------------------------------------------------- loading

    def _iter_training_rows(self):
        # Only consumed by training/eligibility paths (load_training_samples_for_group,
        # discover_group_sample_counts, all_group_ablation_readiness), so the
        # windowless-veto filter lives here once instead of in each caller.
        if not self.samples_path.exists():
            return
        for line in self.samples_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not _has_trainable_window(item):
                continue
            yield item

    def discover_group_sample_counts(self) -> Dict[str, int]:
        """Counts labeled rows per (symbol, data_series) group, for reporting
        and for deciding which groups are eligible to retrain."""
        counts: Dict[str, int] = {}
        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            metadata = item.get("metadata") or {}
            bars_period = metadata.get("bars_period", "")
            key = group_key(item.get("symbol", ""), bars_period)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def load_training_samples_for_group(self, key: str) -> Tuple[torch.Tensor, torch.Tensor, List[str], torch.Tensor]:
        """Returns (X, y, timestamps, weights) for a single group, in the order
        they appear in the jsonl file (i.e. chronological, since it's
        append-only). Shadow samples carry SHADOW_SAMPLE_WEIGHT, real ones 1.0.

        Exact (window, label) duplicates are collapsed to their first
        occurrence. Multiple shadow templates trading the same bar log the same
        window+label repeatedly (verification dup_scan found ~50% duplicate
        rows in busy NQ groups, with copies straddling the train/holdout
        boundary and inflating val/test accuracy); the repeats carry no new
        information about the market, only about template-band overlap. Same
        window with a DIFFERENT label is kept -- that's genuine outcome
        disagreement between templates, not oversampling.
        """
        xs: List[torch.Tensor] = []
        ys: List[int] = []
        timestamps: List[str] = []
        weights: List[float] = []
        seen: set = set()

        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            metadata = item.get("metadata") or {}
            bars_period = metadata.get("bars_period", "")
            row_key = group_key(item.get("symbol", ""), bars_period)
            if row_key != key:
                continue
            window = item.get("window", [])
            try:
                canon = json.dumps([[round(float(v), 6) for v in bar] for bar in window])
            except Exception:
                canon = json.dumps(str(window))
            dedupe_key = (hashlib.md5(canon.encode()).hexdigest(), label)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            xs.append(self.coerce_window(window))
            ys.append(CLASSES.index(label))
            timestamps.append(str(item.get("timestamp") or item.get("logged_at") or ""))
            weights.append(SHADOW_SAMPLE_WEIGHT if _is_shadow_sample(metadata) else 1.0)

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
        epochs: int = 8,
        batch_size: int = 64,
        learning_rate: float = 1e-3,
        permute_labels: bool = False,
        publish: bool = True,
        from_scratch: bool = False,
        randomize_features: bool = False,
        shuffle_split: bool = False,
    ) -> Dict[str, Any]:
        # The extra flags are the verification suite's dry-run hooks (see
        # verification.py), mirroring MLService_Trend's trend_model.py. Defaults
        # leave the live retrain path untouched. With publish=False the run
        # trains a DETACHED model with local norm stats and never writes to the
        # live ModelState or disk -- critical here because the honest path
        # trains state.model in place.
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
            # Break the window->label tie while preserving class distribution
            # (permutation/leakage test).
            y = y[torch.randperm(n)]

        if randomize_features:
            # Null-feature baseline: pure-noise windows, true labels.
            x_raw = torch.randn_like(x_raw)

        if shuffle_split:
            # Random split instead of chronological (walk-forward-gap check).
            # Fixed seed so reruns are comparable.
            perm = torch.randperm(n, generator=torch.Generator().manual_seed(0))
            x_raw, y, w = x_raw[perm], y[perm], w[perm]

        # Time-ordered split (NOT a random shuffle) — samples are appended to
        # the jsonl chronologically, so a slice split keeps validation/test
        # genuinely out-of-sample instead of leaking future bars into training.
        train_end = max(1, int(n * 0.70))
        val_end = max(train_end + 1, int(n * 0.90))
        val_end = min(val_end, n)
        if train_end >= n:
            train_end = max(1, n - 2)
            val_end = max(train_end + 1, n - 1)

        x_train, y_train, w_train = x_raw[:train_end], y[:train_end], w[:train_end]
        x_val, y_val = x_raw[train_end:val_end], y[train_end:val_end]
        x_test, y_test = x_raw[val_end:], y[val_end:]

        state = self._get_or_create_state(key)
        mean = x_train.reshape(-1, N_FEATURES).mean(dim=0)
        std = x_train.reshape(-1, N_FEATURES).std(dim=0).clamp_min(1e-6)

        def norm(x: torch.Tensor) -> torch.Tensor:
            return (x - mean.view(1, 1, -1)) / std.view(1, 1, -1)

        if publish:
            # Live path, unchanged behavior: train state.model in place and
            # publish the new norm stats on the shared state.
            state.mean = mean
            state.std = std
            model = state.model
        else:
            # Verification dry run: detached model, state untouched.
            model = TemporalCnn()
            if not from_scratch:
                model.load_state_dict(state.model.state_dict())
        model.train()
        loader = DataLoader(TensorDataset(norm(x_train), y_train, w_train), batch_size=max(1, min(batch_size, len(y_train))), shuffle=True)
        optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
        criterion = nn.CrossEntropyLoss()
        criterion_per_sample = nn.CrossEntropyLoss(reduction="none")

        best_state = None
        best_val_loss = float("inf")
        patience = 5
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
                val_acc = float((val_logits.argmax(1) == y_val).float().mean().item())
                val_loss_final = float(criterion(val_logits, y_val).item())
            else:
                val_acc = None
                val_loss_final = None

            if len(y_test):
                test_logits = model(norm(x_test))
                test_acc = float((test_logits.argmax(1) == y_test).float().mean().item())
            else:
                test_acc = None

        # Majority-class base rate per split -- what a do-nothing model would
        # score. The verification suite compares dry-run val_acc against this.
        def _base_rate(labels: torch.Tensor) -> Optional[float]:
            if not len(labels):
                return None
            counts = torch.bincount(labels, minlength=len(CLASSES)).float()
            return float((counts.max() / counts.sum()).item())

        # Long/short rows per holdout slice -- classify_entry_model_status
        # refuses good_to_use when the val slice has too few to verify
        # directional skill (a 100%-no_trade slice grades any always-no_trade
        # model at val_acc 1.0).
        no_trade_index = CLASSES.index("no_trade")

        def _directional(labels: torch.Tensor) -> Optional[int]:
            if not len(labels):
                return None
            return int((labels != no_trade_index).sum().item())

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
            "val_loss": val_loss_final,
            "test_acc": test_acc,
            "val_base_rate": _base_rate(y_val),
            "test_base_rate": _base_rate(y_test),
            "val_directional": _directional(y_val),
            "test_directional": _directional(y_test),
            "published": publish,
        }

        # Verification dry run: metrics only, never bump/save the live state.
        if not publish:
            result["model_version"] = state.version
            return result

        state.version += 1
        state.trained = True
        self.save(key)
        result["model_version"] = state.version
        result["model_path"] = str(self.model_path_for_group(key))

        state.metadata = result
        self.metadata_path_for_group(key).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

        # Append to rolling history so callers can look at a trend instead of
        # trusting a single noisy snapshot.
        self.append_jsonl(self.history_path_for_group(key), {
            "ts": timestamp,
            "val_acc": val_acc,
            "test_acc": test_acc,
            "samples": int(n),
        })

        return result

    def retrain(
        self,
        symbol: Optional[str] = None,
        bars_period: Optional[str] = None,
        epochs: int = 8,
        batch_size: int = 64,
        learning_rate: float = 1e-3,
    ) -> Dict[str, Any]:
        """Retrain one specific group if symbol+bars_period given, otherwise
        retrain every eligible group (>= MIN_SAMPLES_PER_GROUP labeled rows)."""
        if symbol and bars_period is not None:
            key = group_key(symbol, bars_period)
            return self.retrain_group(key, epochs=epochs, batch_size=batch_size, learning_rate=learning_rate)

        counts = self.discover_group_sample_counts()
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

    def group_health(
        self,
        key: str,
        sample_counts: Optional[Dict[str, int]] = None,
        source_counts: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> Dict[str, Any]:
        state = self._get_or_create_state(key)
        counts = sample_counts if sample_counts is not None else self.discover_group_sample_counts()
        samples = counts.get(key, 0)
        src = (source_counts or {}).get(key) or {}
        live_samples = src.get("live", 0)
        shadow_samples = src.get("shadow", 0)
        symbol_part, _, series_part = key.partition("_")
        val_acc = state.metadata.get("val_acc")
        test_acc = state.metadata.get("test_acc")
        val_base_rate = state.metadata.get("val_base_rate")
        val_directional = state.metadata.get("val_directional")

        # Data-quality tripwires (added 2026-07-17 after the empty-window veto
        # flood pushed several groups to a fictitious val_acc of 1.0). The
        # first flags only; the base-rate and directional-count conditions
        # ALSO gate via classify_entry_model_status (added 2026-07-18) -- the
        # warnings here explain a resulting "caution" badge.
        warnings: List[str] = []
        if val_acc is not None and val_acc >= 0.98:
            warnings.append(
                f"val_acc {val_acc:.3f} >= 0.98 -- on market data this is a degenerate-data/"
                "leakage tripwire; verify the training rows before trusting this model"
            )
        if val_acc is not None and val_base_rate is not None and val_acc < val_base_rate + VAL_BASE_RATE_MARGIN:
            warnings.append(
                f"val_acc {val_acc:.3f} does not beat the always-majority baseline "
                f"{val_base_rate:.3f} by {VAL_BASE_RATE_MARGIN:.2f} -- no edge, not gating live entries"
            )
        if val_directional is not None and val_directional < MIN_VAL_DIRECTIONAL:
            warnings.append(
                f"only {val_directional} directional (long/short) rows in the validation slice "
                f"(< {MIN_VAL_DIRECTIONAL}) -- directional skill unverifiable, not gating live entries"
            )
        if state.trained and samples < MIN_SAMPLES_PER_GROUP:
            warnings.append(
                f"model is trained but only {samples} eligible samples remain "
                f"(< {MIN_SAMPLES_PER_GROUP}) -- checkpoint predates a data purge and is stale"
            )

        return {
            "warnings": warnings,
            "group": key,
            "symbol": symbol_part,
            "data_series_key": series_part,
            "samples": samples,
            "live_samples": live_samples,
            "shadow_samples": shadow_samples,
            "warmup_remaining": max(0, MIN_SAMPLES_PER_GROUP - samples),
            "model_ready": state.trained,
            "model_version": state.version,
            "last_trained": state.metadata.get("last_trained"),
            "val_acc": val_acc,
            "test_acc": test_acc,
            "train_samples": state.metadata.get("train_samples"),
            "val_samples": state.metadata.get("val_samples"),
            "test_samples": state.metadata.get("test_samples"),
            "val_base_rate": val_base_rate,
            "val_directional": val_directional,
            "status": classify_entry_model_status(state.trained, val_acc, test_acc, val_base_rate, val_directional),
        }

    def all_group_health(self) -> Dict[str, Dict[str, Any]]:
        counts = self.discover_group_sample_counts()
        keys = sorted(set(self.known_groups()) | set(counts.keys()))
        return {key: self.group_health(key, counts) for key in keys}

    def all_group_ablation_readiness(self) -> Dict[str, Dict[str, Any]]:
        """Per-group progress toward READY_* thresholds for a meaningful
        SHADOW_SAMPLE_WEIGHT ablation -- powers the dashboard's readiness card."""
        rows_by_group: Dict[str, List[Dict[str, Any]]] = {}
        for item in self._iter_training_rows():
            label = str(item.get("label", "")).lower()
            if label not in CLASSES:
                continue
            metadata = item.get("metadata") or {}
            key = group_key(item.get("symbol", ""), metadata.get("bars_period", ""))
            rows_by_group.setdefault(key, []).append(item)

        keys = sorted(set(self.known_groups()) | set(rows_by_group.keys()))
        return {key: assess_group_readiness(rows_by_group.get(key, [])) for key in keys}
