from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import torch

from feature_utils import group_key
from ml_model import (
    SHADOW_SAMPLE_WEIGHT,
    FEATURE_NAMES,
    N_FEATURES,
    WINDOW_SIZE,
    MlEngine,
    TemporalCnn,
    classify_entry_model_status,
)

# Matches AbsoluteMaxTemplateNumber in temalimit.cs; class index = template_number - 1.
TEMPLATE_COUNT = 40
TEMPLATE_CLASSES = [str(t) for t in range(1, TEMPLATE_COUNT + 1)]

# Stable column order for both template sample CSVs. Live-only fields stay
# empty on shadow rows; window is a JSON array serialized into the last column.
CSV_COLUMNS = [
    "logged_at",
    "symbol",
    "trigger",
    "setup_timestamp",
    "resolved_timestamp",
    "template_number",
    "selectivity",
    "setup_direction",
    "r_multiple",
    "dollars",
    "shadow",
    "bars_period",
    "entry_price",
    "exit_price",
    "mfe_points",
    "mae_points",
    "bars_held",
    "win",
    "window",
]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _row_is_shadow(row: Dict[str, Any]) -> bool:
    return str(row.get("shadow") or "").strip().lower() in ("true", "1")


class TemplateEngine(MlEngine):
    """Registry of per-(symbol, data_series) template-selection models.

    Reuses MlEngine's checkpoint registry, versioning, and training loop; the
    label space is template numbers 1..TEMPLATE_COUNT instead of entry classes,
    and each training sample is one setup: every template's outcome for the
    same (symbol, bars_period, trigger, setup_direction, setup_timestamp)
    collapses to the row with max r_multiple, whose template is the label.
    No unique setup identifier exists in the strategy, so that composite key is
    the grouping rule.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.shadow_csv = root / "data" / "template_shadow_samples.csv"
        self.live_csv = root / "data" / "template_live_samples.csv"

    # ---------------------------------------------------------------- paths

    def model_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"template_model_{key}.pt"

    def metadata_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"template_model_{key}.json"

    def history_path_for_group(self, key: str) -> Path:
        return self.model_dir / f"template_model_{key}_history.jsonl"

    # ------------------------------------------------------------- registry

    def _new_model(self) -> TemporalCnn:
        return TemporalCnn(n_classes=TEMPLATE_COUNT)

    def known_groups(self) -> List[str]:
        groups = set(self._states.keys())
        for path in self.model_dir.glob("template_model_*.pt"):
            groups.add(path.stem.replace("template_model_", ""))
        for path in self.model_dir.glob("template_model_*.json"):
            stem = path.stem.replace("template_model_", "")
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
                "classes": TEMPLATE_CLASSES,
                "window_size": WINDOW_SIZE,
            },
            self.model_path_for_group(key),
        )

    # ---------------------------------------------------------------- log

    def log_template_sample(self, record: Dict[str, Any]) -> None:
        path = self.shadow_csv if record.get("shadow") else self.live_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(CSV_COLUMNS)
            writer.writerow([_csv_value(record.get(column)) for column in CSV_COLUMNS])

    # ------------------------------------------------------------- loading

    def _iter_template_rows(self):
        for path in (self.shadow_csv, self.live_csv):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        yield row
            except Exception:
                continue

    def _all_setup_groups(self) -> Dict[str, List[Dict[str, Any]]]:
        """Setup-level training samples per model group, chronological by
        setup_timestamp. Weighting mirrors the entry model's live/shadow rule:
        a setup containing at least one live row trains at weight 1.0,
        shadow-only setups at SHADOW_SAMPLE_WEIGHT."""
        setups: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
        for row in self._iter_template_rows():
            try:
                template = int(float(row.get("template_number") or 0))
                r_multiple = float(row.get("r_multiple") or 0.0)
            except (TypeError, ValueError):
                continue
            if not 1 <= template <= TEMPLATE_COUNT:
                continue

            symbol = str(row.get("symbol") or "")
            bars_period = str(row.get("bars_period") or "")
            setup_key = (
                symbol,
                bars_period,
                str(row.get("trigger") or ""),
                str(row.get("setup_direction") or ""),
                str(row.get("setup_timestamp") or ""),
            )
            is_live = not _row_is_shadow(row)
            entry = setups.get(setup_key)
            if entry is None:
                setups[setup_key] = {
                    "group": group_key(symbol, bars_period),
                    "setup_timestamp": setup_key[4],
                    "best_template": template,
                    "best_r": r_multiple,
                    "window": row.get("window") or "[]",
                    "has_live": is_live,
                }
            else:
                entry["has_live"] = entry["has_live"] or is_live
                if r_multiple > entry["best_r"]:
                    entry["best_r"] = r_multiple
                    entry["best_template"] = template
                    entry["window"] = row.get("window") or "[]"

        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for entry in setups.values():
            by_group.setdefault(entry["group"], []).append(entry)
        for entries in by_group.values():
            entries.sort(key=lambda item: item["setup_timestamp"])
        return by_group

    def discover_group_sample_counts(self) -> Dict[str, int]:
        return {key: len(entries) for key, entries in self._all_setup_groups().items()}

    def load_training_samples_for_group(self, key: str) -> Tuple[torch.Tensor, torch.Tensor, List[str], torch.Tensor]:
        xs: List[torch.Tensor] = []
        ys: List[int] = []
        timestamps: List[str] = []
        weights: List[float] = []

        for entry in self._all_setup_groups().get(key, []):
            try:
                window = json.loads(entry["window"]) if entry["window"] else []
            except Exception:
                window = []
            xs.append(self.coerce_window(window))
            ys.append(entry["best_template"] - 1)
            timestamps.append(entry["setup_timestamp"])
            weights.append(1.0 if entry["has_live"] else SHADOW_SAMPLE_WEIGHT)

        if not xs:
            return (
                torch.empty(0, WINDOW_SIZE, N_FEATURES),
                torch.empty(0, dtype=torch.long),
                [],
                torch.empty(0, dtype=torch.float32),
            )

        return torch.stack(xs), torch.tensor(ys, dtype=torch.long), timestamps, torch.tensor(weights, dtype=torch.float32)

    # ------------------------------------------------------------- predict

    def predict(self, symbol: str, bars_period: str, window: List[List[float]], min_confidence: float = 0.0) -> Dict[str, Any]:
        # min_confidence accepted for signature compatibility with MlEngine.predict() but unused --
        # template selection has no "no_trade"-equivalent fallback action; trust is gated entirely by
        # the returned status (good_to_use), not a per-call confidence floor.
        key = group_key(symbol, bars_period)
        state = self._get_or_create_state(key)

        if not state.trained:
            return {
                "template": 0,
                "confidence": 0.0,
                "status": "warming_up",
                "model_ready": False,
                "model_version": state.version,
                "group": key,
                "reason": "warming_up",
            }

        x = self.normalize_window(key, window).unsqueeze(0)
        state.model.eval()
        with torch.no_grad():
            logits = state.model(x)
            probs = torch.softmax(logits, dim=1)[0]

        best_index = int(torch.argmax(probs).item())
        status = classify_entry_model_status(
            state.trained,
            state.metadata.get("val_acc"),
            state.metadata.get("test_acc"),
            state.metadata.get("val_base_rate"),
            state.metadata.get("val_directional"),
        )

        return {
            "template": best_index + 1,
            "confidence": float(probs[best_index].item()),
            "status": status,
            "model_ready": True,
            "model_version": state.version,
            "group": key,
        }


def scan_template_sample_counts(shadow_csv: Path, live_csv: Path) -> Dict[str, int]:
    """Standalone, picklable counterpart to TemplateEngine.discover_group_sample_counts.
    Free of any TemplateEngine instance state so it can run in a subprocess --
    the shadow CSV carries a JSON-serialized feature window per row and has
    grown into tens of MB, so re-parsing it every 10s on an in-process thread
    was a major source of GIL contention against request handlers."""
    setups: Dict[Tuple[str, str, str, str, str], str] = {}
    for path in (shadow_csv, live_csv):
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        template = int(float(row.get("template_number") or 0))
                    except (TypeError, ValueError):
                        continue
                    if not 1 <= template <= TEMPLATE_COUNT:
                        continue
                    symbol = str(row.get("symbol") or "")
                    bars_period = str(row.get("bars_period") or "")
                    setup_key = (
                        symbol,
                        bars_period,
                        str(row.get("trigger") or ""),
                        str(row.get("setup_direction") or ""),
                        str(row.get("setup_timestamp") or ""),
                    )
                    setups[setup_key] = group_key(symbol, bars_period)
        except Exception:
            continue

    counts: Dict[str, int] = {}
    for group in setups.values():
        counts[group] = counts.get(group, 0) + 1
    return counts


def scan_template_sample_counts_by_source(shadow_csv: Path, live_csv: Path) -> Dict[str, Dict[str, int]]:
    """Like scan_template_sample_counts, but splits each group's setups into live
    vs shadow. A setup counts as "live" if it ever produced a real fill (appears
    in the live CSV), otherwise "shadow". total == live + shadow and matches the
    combined scan_template_sample_counts figure (same setup-level dedup)."""
    # setup_key -> (group, has_live)
    setups: Dict[Tuple[str, str, str, str, str], Tuple[str, bool]] = {}
    for path, is_live in ((shadow_csv, False), (live_csv, True)):
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        template = int(float(row.get("template_number") or 0))
                    except (TypeError, ValueError):
                        continue
                    if not 1 <= template <= TEMPLATE_COUNT:
                        continue
                    symbol = str(row.get("symbol") or "")
                    bars_period = str(row.get("bars_period") or "")
                    setup_key = (
                        symbol,
                        bars_period,
                        str(row.get("trigger") or ""),
                        str(row.get("setup_direction") or ""),
                        str(row.get("setup_timestamp") or ""),
                    )
                    prev = setups.get(setup_key)
                    has_live = is_live or (prev[1] if prev else False)
                    setups[setup_key] = (group_key(symbol, bars_period), has_live)
        except Exception:
            continue

    counts: Dict[str, Dict[str, int]] = {}
    for group, has_live in setups.values():
        bucket = counts.setdefault(group, {"total": 0, "live": 0, "shadow": 0})
        bucket["total"] += 1
        bucket["live" if has_live else "shadow"] += 1
    return counts
