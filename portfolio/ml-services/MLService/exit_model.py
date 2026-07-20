from __future__ import annotations

import csv
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import torch
from sklearn.metrics import roc_auc_score
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence, pad_sequence
from torch.utils.data import DataLoader, Dataset

from feature_utils import dollars_per_tick_feature, exit_group_key, normalize_symbol, symbol_hash_feature


DEVICE = torch.device("cpu")
N_SEQ_FEATURES = 9
N_CONTEXT_FEATURES = 8
EXIT_MODEL_WARMUP_MIN = 500
# Per-example sequences are capped to the most recent bars of the trade. Without
# this cap, _build_examples stores a full prefix copy of the trade's history for
# every bar (O(N^2) floats per trade) — a 3LineBreak trade held for tens of
# thousands of bars made the ES_3LINEBREAK retrain attempt a 311 GB allocation
# (2026-07-19), thrashing the pagefile and stalling every request on the service
# for minutes at a time. The model reads the last transformer step, so exit
# decisions only ever depended on the trailing window anyway.
EXIT_SEQ_MAX_BARS = 128

# Retrains run in ProcessPoolExecutor children (max_workers=2). Torch otherwise
# grabs every logical core per process, so two concurrent retrains oversubscribe
# the box and starve the live /predict-exit path on the service itself. Cap each
# worker so training stays background work rather than crowding out inference.
EXIT_TRAIN_TORCH_THREADS = 4

# Only the rows nearest the exit become training examples. Hold rows are logged
# at up to 5 Hz (MlExitSampleMinInterval = 200 ms in temalimit.cs) for as long as
# a position is open, so one trade emitted 23,168 near-identical rows -- median
# trade is 29. Capping example CREATION per trade cuts ~96.5% of rows (2369:1 ->
# 56:1) and turns a 134-minute retrain into minutes.
#
# Justified by COST, not accuracy: a controlled test (17 features, logistic
# regression, 12-40 seeds) showed imbalance ratio does NOT drive learnability --
# sweeping 17:1 to 2369:1 with positives held fixed moved AUC not at all
# (0.963-0.981 everywhere). What governs it is the absolute positive count.
#
# Matches EXIT_SEQ_MAX_BARS so the kept span equals what one example can see.
EXIT_TRAIN_MAX_ROWS_PER_TRADE = 128


class TradeExitModel(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=N_SEQ_FEATURES,
            hidden_size=48,
            num_layers=2,
            dropout=0.3,
            batch_first=True,
        )
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=48,
            nhead=4,
            dim_feedforward=96,
            dropout=0.1,
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=1)
        self.context_fc = nn.Sequential(nn.Linear(N_CONTEXT_FEATURES, 16), nn.ReLU())
        self.head = nn.Sequential(
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(32, 1),
        )
        self.register_buffer("seq_mean", torch.zeros(N_SEQ_FEATURES, dtype=torch.float32))
        self.register_buffer("seq_std", torch.ones(N_SEQ_FEATURES, dtype=torch.float32))
        self.register_buffer("context_mean", torch.zeros(N_CONTEXT_FEATURES, dtype=torch.float32))
        self.register_buffer("context_std", torch.ones(N_CONTEXT_FEATURES, dtype=torch.float32))

    def forward(self, sequences: torch.Tensor, lengths: torch.Tensor, context: torch.Tensor) -> torch.Tensor:
        lengths = lengths.to(dtype=torch.long, device="cpu").clamp_min(1)
        sequences = (sequences - self.seq_mean.view(1, 1, -1)) / self.seq_std.clamp_min(1e-6).view(1, 1, -1)
        context = (context - self.context_mean.view(1, -1)) / self.context_std.clamp_min(1e-6).view(1, -1)

        packed = pack_padded_sequence(sequences, lengths, batch_first=True, enforce_sorted=False)
        packed_out, _ = self.lstm(packed)
        lstm_out, _ = pad_packed_sequence(packed_out, batch_first=True)

        max_len = lstm_out.size(1)
        positions = torch.arange(max_len, device=lstm_out.device).unsqueeze(0)
        padding_mask = positions >= lengths.to(lstm_out.device).unsqueeze(1)
        transformed = self.transformer(lstm_out, src_key_padding_mask=padding_mask)

        batch_index = torch.arange(transformed.size(0), device=transformed.device)
        last_index = (lengths.to(transformed.device) - 1).clamp_min(0)
        last_step = transformed[batch_index, last_index, :]
        context_features = self.context_fc(context)
        logits = self.head(torch.cat([last_step, context_features], dim=1))
        return logits.squeeze(1)


class ExitSequenceDataset(Dataset):
    def __init__(
        self,
        examples: Sequence[Tuple[torch.Tensor, torch.Tensor, float, float]],
        augment: bool = False,
    ) -> None:
        self.examples = list(examples)
        self.augment = augment

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        sequence, context, label, weight = self.examples[index]
        if self.augment:
            sequence = sequence + torch.randn_like(sequence) * 0.01
        return (
            sequence,
            context,
            torch.tensor(float(label), dtype=torch.float32),
            torch.tensor(float(weight), dtype=torch.float32),
        )


def _collate_exit_batch(items: Sequence[Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]]):
    sequences = [item[0] for item in items]
    lengths = torch.tensor([max(1, seq.size(0)) for seq in sequences], dtype=torch.long)
    return (
        pad_sequence(sequences, batch_first=True),
        lengths,
        torch.stack([item[1] for item in items]),
        torch.stack([item[2] for item in items]),
        torch.stack([item[3] for item in items]),
    )


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _parse_date(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

def _data_series_type_feature(value: Any) -> float:
    text = str(value or "").strip().lower()
    if text == "tick":
        return -0.75
    if text == "minute":
        return -0.25
    if text == "second":
        return -0.50
    if text == "range":
        return 0.25
    if text == "day":
        return 0.50
    if text == "volume":
        return 0.75
    return 0.0


def _data_series_value_feature(value: Any) -> float:
    raw = max(1.0, _parse_float(value, 1.0))
    return max(0.0, min(1.0, math.log10(raw) / 4.0))


def _context_from_row(row: Dict[str, str], symbol: str) -> List[float]:
    bars_held = _parse_float(row.get("bars_held"), 0.0)
    unrealized_r = _parse_float(row.get("unrealized_r"), 0.0)
    direction_text = str(row.get("direction") or "").strip().lower()
    direction = 1.0 if direction_text == "long" else -1.0 if direction_text == "short" else 0.0
    bar_duration = max(1.0, _parse_float(row.get("bar_duration_sec"), 60.0))
    avg_bar_speed = max(0.0, min(3.0, 60.0 / bar_duration))
    return [
        max(0.0, min(5.0, bars_held / 50.0)),
        max(-5.0, min(5.0, unrealized_r)),
        direction,
        avg_bar_speed,
        symbol_hash_feature(symbol),
        dollars_per_tick_feature(symbol),
        _data_series_type_feature(row.get("data_series_type")),
        _data_series_value_feature(row.get("data_series_value")),
    ]


def _load_rows(tsv_path: Path, group: str) -> List[Dict[str, str]]:
    if not tsv_path.exists():
        return []
    rows: List[Dict[str, str]] = []
    with tsv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            row_group = exit_group_key(
                row.get("symbol", ""),
                row.get("data_series_type", ""),
                row.get("data_series_value", ""),
            )
            if row_group == group:
                rows.append(row)
    return rows


def _build_examples(rows: Sequence[Dict[str, str]], symbol: str):
    by_trade: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        trade_id = str(row.get("trade_id") or "").strip()
        if not trade_id:
            trade_id = f"{symbol}_unknown"
        by_trade.setdefault(trade_id, []).append(row)

    max_date = max((_parse_date(str(row.get("sample_date") or row.get("timestamp") or "")) for row in rows), default=datetime.now(timezone.utc))
    examples_by_trade: Dict[str, List[Tuple[torch.Tensor, torch.Tensor, float, float]]] = {}
    label_counts = {"hold": 0, "exit": 0}

    for trade_id, trade_rows in by_trade.items():
        trade_rows.sort(key=lambda item: str(item.get("timestamp") or ""))
        feature_history: List[List[float]] = []
        examples: List[Tuple[torch.Tensor, torch.Tensor, float, float]] = []
        # Every row still feeds feature_history so sequence context stays exact;
        # only example creation is capped. Exit rows (label 0) are never dropped --
        # they are the scarce class and the whole point of the model.
        keep_from = max(0, len(trade_rows) - EXIT_TRAIN_MAX_ROWS_PER_TRADE)
        for idx, row in enumerate(trade_rows):
            features = [_parse_float(row.get(f"f{i}"), 0.0) for i in range(N_SEQ_FEATURES)]
            feature_history.append(features)
            label = 1.0 if _parse_int(row.get("label"), 1) == 1 else 0.0
            if idx < keep_from and label >= 0.5:
                continue
            if label >= 0.5:
                label_counts["hold"] += 1
            else:
                label_counts["exit"] += 1
            sample_date = _parse_date(str(row.get("sample_date") or row.get("timestamp") or ""))
            recency_days = (sample_date - max_date).days
            recency_weight = math.exp(recency_days / 30.0)
            seq = torch.tensor(feature_history[-EXIT_SEQ_MAX_BARS:], dtype=torch.float32)
            ctx = torch.tensor(_context_from_row(row, symbol), dtype=torch.float32)
            examples.append((seq, ctx, label, recency_weight))
        if examples:
            examples_by_trade[trade_id] = examples

    return examples_by_trade, label_counts


def _split_trade_ids(trade_ids: List[str]) -> Tuple[List[str], List[str], List[str]]:
    shuffled = list(trade_ids)
    random.Random(1337).shuffle(shuffled)
    n = len(shuffled)
    if n <= 2:
        return shuffled, shuffled, shuffled
    train_end = max(1, int(n * 0.70))
    val_end = max(train_end + 1, int(n * 0.90))
    train_ids = shuffled[:train_end]
    val_ids = shuffled[train_end:val_end] or shuffled[:1]
    test_ids = shuffled[val_end:] or val_ids
    return train_ids, val_ids, test_ids


def _flatten_examples(examples_by_trade: Dict[str, List[Tuple[torch.Tensor, torch.Tensor, float, float]]], ids: Sequence[str]):
    rows: List[Tuple[torch.Tensor, torch.Tensor, float, float]] = []
    for trade_id in ids:
        rows.extend(examples_by_trade.get(trade_id, []))
    return rows


def _set_normalization(model: TradeExitModel, examples: Sequence[Tuple[torch.Tensor, torch.Tensor, float, float]]) -> None:
    if not examples:
        return
    # Accumulate per-feature sums instead of torch.cat over every example's
    # sequence rows — the concatenated tensor is what blew up to a single
    # hundreds-of-GB allocation on the big 3LINEBREAK groups.
    seq_sum = torch.zeros(N_SEQ_FEATURES, dtype=torch.float64)
    seq_sumsq = torch.zeros(N_SEQ_FEATURES, dtype=torch.float64)
    seq_count = 0
    for item in examples:
        rows = item[0].to(dtype=torch.float64)
        seq_sum += rows.sum(dim=0)
        seq_sumsq += (rows * rows).sum(dim=0)
        seq_count += rows.size(0)
    seq_mean = seq_sum / max(1, seq_count)
    seq_var = (seq_sumsq / max(1, seq_count) - seq_mean * seq_mean).clamp_min(0.0)
    denom = max(1, seq_count - 1)
    seq_std = (seq_var * seq_count / denom).sqrt()
    ctx_rows = torch.stack([item[1] for item in examples])
    model.seq_mean.copy_(seq_mean.to(dtype=torch.float32))
    model.seq_std.copy_(seq_std.to(dtype=torch.float32).clamp_min(1e-6))
    model.context_mean.copy_(ctx_rows.mean(dim=0))
    model.context_std.copy_(ctx_rows.std(dim=0).clamp_min(1e-6))


def _auc_or_half(labels: Sequence[float], scores: Sequence[float]) -> float:
    unique = set(float(label) for label in labels)
    if len(unique) < 2:
        return 0.5
    return float(roc_auc_score(labels, scores))


def _evaluate(model: TradeExitModel, examples: Sequence[Tuple[torch.Tensor, torch.Tensor, float, float]], batch_size: int = 64):
    if not examples:
        return 0.0, 0.5
    model.eval()
    loader = DataLoader(ExitSequenceDataset(examples), batch_size=max(1, batch_size), shuffle=False, collate_fn=_collate_exit_batch)
    criterion = nn.BCEWithLogitsLoss(reduction="none")
    losses: List[float] = []
    labels: List[float] = []
    scores: List[float] = []
    with torch.no_grad():
        for sequences, lengths, context, y, weights in loader:
            logits = model(sequences.to(DEVICE), lengths.to(DEVICE), context.to(DEVICE))
            loss = criterion(logits, y.to(DEVICE))
            losses.append(float((loss * weights.to(DEVICE)).mean().item()))
            labels.extend([float(v) for v in y.tolist()])
            scores.extend([float(v) for v in torch.sigmoid(logits).cpu().tolist()])
    return float(sum(losses) / max(1, len(losses))), _auc_or_half(labels, scores)


def train_exit_model(group: str, data_path: str) -> Dict[str, Any]:
    torch.set_num_threads(EXIT_TRAIN_TORCH_THREADS)
    root = Path(data_path)
    tsv_path = root / f"exit_samples_{group}.tsv"
    model_path = root / f"exit_model_{group}.pt"
    metadata_path = root / f"exit_model_{group}.json"

    # group is "{SYMBOL}_{SERIESKEY}" (e.g. "NQ_500TICK") — the actual ticker
    # is needed on its own for the symbol_hash/dollars_per_tick context features.
    symbol = group.split("_", 1)[0] if "_" in group else group

    try:
        rows = _load_rows(tsv_path, group)
        if len(rows) < EXIT_MODEL_WARMUP_MIN:
            return {"error": "insufficient_samples", "count": len(rows), "group": group}

        examples_by_trade, label_counts = _build_examples(rows, symbol)
        if label_counts["hold"] <= 0 or label_counts["exit"] <= 0:
            return {"error": "need_both_labels", "group": group, "label_counts": label_counts}

        trade_ids = list(examples_by_trade.keys())
        completed_trades = len(trade_ids)
        weeks_represented = len({
            _parse_date(str(row.get("sample_date") or row.get("timestamp") or "")).isocalendar()[:2]
            for row in rows
        })

        train_ids, val_ids, test_ids = _split_trade_ids(trade_ids)
        train_examples = _flatten_examples(examples_by_trade, train_ids)
        val_examples = _flatten_examples(examples_by_trade, val_ids)
        test_examples = _flatten_examples(examples_by_trade, test_ids)
        if not train_examples:
            return {"error": "insufficient_samples", "count": len(rows), "group": group}

        pos_count = sum(1 for item in train_examples if item[2] >= 0.5)
        neg_count = sum(1 for item in train_examples if item[2] < 0.5)
        if pos_count <= 0 or neg_count <= 0:
            return {"error": "need_both_labels", "group": group, "label_counts": label_counts}

        model = TradeExitModel().to(DEVICE)
        _set_normalization(model, train_examples)
        loader = DataLoader(
            ExitSequenceDataset(train_examples, augment=True),
            batch_size=64,
            shuffle=True,
            collate_fn=_collate_exit_batch,
        )
        optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
        pos_weight = torch.tensor([neg_count / max(1, pos_count)], dtype=torch.float32, device=DEVICE)
        criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight, reduction="none")

        best_state = None
        best_val_loss = float("inf")
        patience = 10
        stale_epochs = 0
        for _epoch in range(30):
            model.train()
            for sequences, lengths, context, y, weights in loader:
                optimizer.zero_grad()
                logits = model(sequences.to(DEVICE), lengths.to(DEVICE), context.to(DEVICE))
                loss_values = criterion(logits, y.to(DEVICE))
                loss = (loss_values * weights.to(DEVICE)).mean()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                optimizer.step()

            val_loss, _ = _evaluate(model, val_examples)
            if val_loss < best_val_loss - 1e-6:
                best_val_loss = val_loss
                best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
                stale_epochs = 0
            else:
                stale_epochs += 1
                if stale_epochs >= patience:
                    break

        if best_state is not None:
            model.load_state_dict(best_state)
        train_loss, train_auc = _evaluate(model, train_examples)
        val_loss, val_auc = _evaluate(model, val_examples)
        test_loss, test_auc = _evaluate(model, test_examples)

        torch.save(model.state_dict(), model_path)
        timestamp = datetime.now(timezone.utc).isoformat()
        metadata = {
            "status": "ok",
            "group": group,
            "symbol": symbol,
            "last_trained": timestamp,
            "val_auc": float(val_auc),
            "test_auc": float(test_auc),
            "train_auc": float(train_auc),
            "samples": int(len(rows)),
            "completed_trades": int(completed_trades),
            "weeks_represented": int(weeks_represented),
            "label_counts": label_counts,
            "last_retrain_trigger": int(len(rows)),
            "train_loss": float(train_loss),
            "val_loss": float(val_loss),
            "test_loss": float(test_loss),
            "model_path": str(model_path),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

        history_path = root / f"exit_model_{group}_history.jsonl"
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": timestamp,
                "val_auc": float(val_auc),
                "test_auc": float(test_auc),
                "completed_trades": int(completed_trades),
                "weeks_represented": int(weeks_represented),
            }) + "\n")

        return metadata
    except Exception as exc:
        return {"error": "training_failed", "detail": str(exc), "group": group}
