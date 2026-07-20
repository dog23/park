from __future__ import annotations

import datetime
import json
import urllib.request
from pathlib import Path

NT_DIR = Path(r"C:\Users\<user>\Documents\NinjaTrader 8")
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "circuit_breaker_state.json"
LOG_PATH = SCRIPT_DIR / "circuit_breaker_watchdog.log"
NTFY_TOPIC = "<ntfy-topic>"
MARKER = "DailyMaxLossExit"

EXTRA_FILES = [
    NT_DIR / "fulltwenties" / "trade_log.csv",
    NT_DIR / "TwentyFourSevenBot" / "trade_log.csv",
]


def log(message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} - {message}\n")


def send_alert(message: str, title: str) -> None:
    try:
        request = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "warning"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5)
    except Exception:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def target_files() -> list[Path]:
    files = list(NT_DIR.glob("*_completed_trades.tsv"))
    for extra in EXTRA_FILES:
        if extra.is_file():
            files.append(extra)
    return files


def main() -> None:
    state = load_state()
    changed = False

    for path in target_files():
        key = str(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue

        last_offset = state.get(key, 0)
        if last_offset > size:
            last_offset = 0  # file was truncated/rotated

        if key not in state:
            # First time seeing this file -- record current size, don't
            # alert on pre-existing historical rows.
            state[key] = size
            changed = True
            continue

        if size == last_offset:
            continue

        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            f.seek(last_offset)
            new_text = f.read()

        state[key] = size
        changed = True

        for line in new_text.splitlines():
            if MARKER in line:
                parts = line.split("\t")
                time_field = parts[0] if len(parts) > 0 else "?"
                ticker_field = parts[1] if len(parts) > 1 else "?"
                strategy = path.stem.replace("_completed_trades", "")
                message = f"{strategy} / {ticker_field} hit its daily max loss circuit breaker at {time_field}."
                log(f"ALERT: {message} (file={key})")
                send_alert(message, "NT8 Circuit Breaker: daily max loss hit")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
