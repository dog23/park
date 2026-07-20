from __future__ import annotations

"""Flags any account holding a position with no working protective stop.

Reads NinjaTrader's OWN log (log/log.YYYYMMDD.NNNNN.txt), not the strategy's
*_open_trades.tsv exports. That is deliberate: on 2026-07-20 two ES shorts ran
43 minutes unprotected precisely BECAUSE the strategy instances stopped
servicing that symbol -- their TSVs froze mid-incident and would have reported
nothing wrong. NT writes the log regardless of strategy health, so this check
still fires when an instance dies, orphans a position, or is disabled.

Single-shot: the scheduled task re-invokes it, state lives in JSON between runs.
"""

import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

NT_DIR = Path(r"C:\Users\<user>\Documents\NinjaTrader 8")
LOG_DIR = NT_DIR / "log"
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "naked_position_state.json"
LOG_PATH = SCRIPT_DIR / "naked_position_watchdog.log"
NTFY_TOPIC = "<ntfy-topic>"

# A position is allowed this long with no protective stop before it is an
# incident. Normal entry-fill -> stop-accepted is ~100-300 ms; the gap only
# grows into seconds during a restart or reload, which is exactly the case
# worth catching. 90 s is far outside the healthy band and well inside the
# 43-minute exposure of the incident this was written for.
UNPROTECTED_GRACE_SECONDS = 90

# While a position stays naked, re-alert at most this often.
REALERT_SECONDS = 900

# Staleness check. temalimit rewrites its exports roughly once a second while it
# is executing, so this much silence means the strategy is not running -- even if
# the Control Center still shows it as Enabled. On 2026-07-20 every temalimit
# instance wedged at 04:31:07 and sat there for hours showing "enabled", holding
# six positions nothing was managing, while the position check stayed silent
# because it could not tell "still naked" from "log stopped advancing".
STALENESS_THRESHOLD_MINUTES = 15

# Files proving temalimit is actually executing, vs files proving the PLATFORM is
# alive. Alerting needs both: temalimit quiet AND something else still moving.
# When markets close or the feed drops, everything goes quiet together and that is
# not a wedge -- which is what keeps this silent all weekend with no calendar logic.
TEMALIMIT_GLOB = "TemaLimit_*.tsv"
LIVENESS_GLOBS = ["TrendTcn_*.tsv"]
LIVENESS_DIR_GLOBS = [("trace", "trace.*.txt")]

# Order states after which an order no longer protects anything.
TERMINAL_STATES = {"Filled", "Cancelled", "Rejected"}

# Which order action closes a given position side.
CLOSING_ACTION = {"Long": "Sell", "Short": "Buy to cover"}

# Exit-side stop actions. A working stop with one of these actions but NO position
# behind it is an ORPHAN that opens a naked position if hit (see orphaned_stops()).
EXIT_STOP_ACTIONS = set(CLOSING_ACTION.values())

# How long an exit stop may linger after its position went flat before it is an
# incident. A stop briefly outlives its position during a normal close, but a
# clean stop-fill close pops the stop from tracking (terminal state), so this only
# fires on stops the close did NOT cancel. Kept short -- an orphan can open a
# position -- but above the sub-second close-transition window.
ORPHAN_GRACE_SECONDS = 60

POSITION_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3}).*?"
    r"Instrument='(?P<instrument>[^']*)' Account='(?P<account>[^']*)' "
    r"Average price=(?P<avg>[0-9.]+) Quantity=(?P<qty>\d+) "
    r"Market position=(?P<position>\w+)"
)

ORDER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3}).*?"
    r"Order='(?P<order_id>[^/']*)/(?P<account>[^']*)' Name='(?P<name>[^']*)' "
    r"New state='(?P<state>[^']*)' Instrument='(?P<instrument>[^']*)' "
    r"Action='(?P<action>[^']*)'.*?Type='(?P<type>[^']*)'"
)


def log(message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} - {message}\n")


def send_alert(message: str, title: str) -> None:
    try:
        request = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"},
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


def parse_ts(raw: str) -> datetime.datetime:
    return datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S:%f")


def recent_log_files(days: int = 2) -> list[Path]:
    """Today's log files plus the previous day's, so a position opened before
    midnight is still reconstructable."""
    today = datetime.date.today()
    wanted = {(today - datetime.timedelta(days=d)).strftime("%Y%m%d") for d in range(days)}
    files = [
        p
        for p in LOG_DIR.glob("log.*.txt")
        # log.20260720.00000.txt and log.20260720.00000.en.txt are duplicates;
        # take only the non-".en." one so every line isn't processed twice.
        if ".en." not in p.name and p.name.split(".")[1] in wanted
    ]
    return sorted(files, key=lambda p: p.name)


def scan() -> tuple[dict, datetime.datetime | None]:
    """Replay the logs into current per-(account, instrument) state."""
    positions: dict[tuple[str, str], dict] = {}
    protective: dict[tuple[str, str], dict[str, dict]] = {}
    # When a key last went Flat. Used to age an ORPHANED stop (a working stop
    # left behind after the position closed by some other means, e.g. an
    # EnableAccountClose market order on restart -- which does NOT cancel a
    # manual/OCO-less stop). The Flat line pops protective[key], but any later
    # order-update line for the still-working stop re-adds it, so at end of scan
    # protective[key] is non-empty while positions[key] is gone: that is the orphan.
    flattened_at: dict[tuple[str, str], datetime.datetime] = {}
    last_ts: datetime.datetime | None = None

    for path in recent_log_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for line in text.splitlines():
            match = POSITION_RE.match(line)
            if match:
                key = (match["account"], match["instrument"])
                ts = parse_ts(match["ts"])
                last_ts = ts if last_ts is None or ts > last_ts else last_ts
                position = match["position"]
                if position == "Flat" or match["qty"] == "0":
                    positions.pop(key, None)
                    protective.pop(key, None)
                    flattened_at[key] = ts
                else:
                    existing = positions.get(key)
                    # Keep the time the position was FIRST opened, so an add-on
                    # fill doesn't restart the grace clock.
                    opened = existing["opened"] if existing else ts
                    positions[key] = {
                        "position": position,
                        "qty": int(match["qty"]),
                        "avg": float(match["avg"]),
                        "opened": opened,
                    }
                    flattened_at.pop(key, None)
                continue

            match = ORDER_RE.match(line)
            if match:
                if "Stop" not in match["type"]:
                    continue
                key = (match["account"], match["instrument"])
                ts = parse_ts(match["ts"])
                last_ts = ts if last_ts is None or ts > last_ts else last_ts
                orders = protective.setdefault(key, {})
                if match["state"] in TERMINAL_STATES:
                    orders.pop(match["order_id"], None)
                else:
                    stop_match = re.search(r"Stop price=([0-9.]+)", line)
                    orders[match["order_id"]] = {
                        "action": match["action"],
                        "stop": float(stop_match.group(1)) if stop_match else 0.0,
                        "ts": ts,
                    }

    return {"positions": positions, "protective": protective, "flattened_at": flattened_at}, last_ts


def unprotected(scanned: dict, now: datetime.datetime) -> list[dict]:
    findings = []
    for key, pos in scanned["positions"].items():
        account, instrument = key
        needed = CLOSING_ACTION.get(pos["position"])
        if needed is None:
            continue

        orders = scanned["protective"].get(key, {})
        if any(o["action"] == needed for o in orders.values()):
            continue

        naked_for = (now - pos["opened"]).total_seconds()
        if naked_for < UNPROTECTED_GRACE_SECONDS:
            continue

        findings.append(
            {
                "account": account,
                "instrument": instrument,
                "position": pos["position"],
                "qty": pos["qty"],
                "avg": pos["avg"],
                "opened": pos["opened"].isoformat(),
                "naked_seconds": int(naked_for),
            }
        )
    return findings


def orphaned_stops(scanned: dict, now: datetime.datetime) -> list[dict]:
    """A working EXIT stop with no position behind it.

    The mirror image of unprotected(): there the danger is a position with no
    stop; here it is a stop with no position. A "Buy to cover" stop with no short
    (or a "Sell" stop with no long) is not protective at all -- if price reaches
    it, it OPENS a brand-new naked position in that direction. Seen live on
    2026-07-20: an EnableAccountClose flattened the Simpointandfigure NQ short on
    restart but left a manual (OCO-less) buy-stop working; it trailed down ~1 pt
    above the market for minutes, one uptick away from opening a naked long.

    Only exit-side stops (Sell / Buy to cover) count -- a Buy / Sell short stop is
    an entry order, not an abandoned protective stop.
    """
    findings = []
    for key, orders in scanned["protective"].items():
        if key in scanned["positions"]:
            continue  # a real position owns these stops; unprotected() covers it

        account, instrument = key
        for order_id, o in orders.items():
            if o["action"] not in EXIT_STOP_ACTIONS:
                continue

            # Age from when the position went flat. Absent (position closed before
            # the log window, or never seen) -> treat as old and alert: an exit stop
            # with no position is dangerous, so fail loud rather than stay silent.
            flat_ts = scanned["flattened_at"].get(key)
            orphan_for = (now - flat_ts).total_seconds() if flat_ts else float("inf")
            if orphan_for < ORPHAN_GRACE_SECONDS:
                continue

            findings.append(
                {
                    "account": account,
                    "instrument": instrument,
                    "action": o["action"],
                    "stop": o["stop"],
                    "order_id": order_id,
                    "flat_since": flat_ts.isoformat() if flat_ts else "unknown",
                    "orphan_seconds": None if orphan_for == float("inf") else int(orphan_for),
                }
            )
    return findings


def newest_mtime(paths) -> float | None:
    stamps = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(stamps) if stamps else None


def ninjatrader_running() -> bool:
    try:
        import subprocess

        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq NinjaTrader.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            # No console flash when launched under pythonw by the scheduled task.
            creationflags=0x08000000,
        )
        return "NinjaTrader.exe" in result.stdout
    except Exception:
        # Can't tell -> assume it is running, so a broken probe fails LOUD rather
        # than silently disabling the check.
        return True


def check_staleness(now: datetime.datetime) -> dict | None:
    """temalimit gone quiet while the platform is demonstrably still alive."""
    temalimit = newest_mtime(NT_DIR.glob(TEMALIMIT_GLOB))
    if temalimit is None:
        return None

    now_epoch = now.timestamp()
    temalimit_age = (now_epoch - temalimit) / 60.0
    if temalimit_age < STALENESS_THRESHOLD_MINUTES:
        return None

    if not ninjatrader_running():
        return None  # NT closed: exports stopping is expected, not a wedge.

    liveness_paths = []
    for pattern in LIVENESS_GLOBS:
        liveness_paths.extend(NT_DIR.glob(pattern))
    for subdir, pattern in LIVENESS_DIR_GLOBS:
        liveness_paths.extend((NT_DIR / subdir).glob(pattern))

    liveness = newest_mtime(liveness_paths)
    if liveness is None:
        return None
    liveness_age = (now_epoch - liveness) / 60.0

    # Everything quiet together = closed market or dropped feed, not a wedge.
    if liveness_age >= STALENESS_THRESHOLD_MINUTES:
        return None

    return {
        "temalimit_age_min": int(temalimit_age),
        "liveness_age_min": int(liveness_age),
        "temalimit_last": datetime.datetime.fromtimestamp(temalimit).isoformat(timespec="seconds"),
    }


def main() -> None:
    replay = "--replay" in sys.argv
    scanned, last_ts = scan()

    # In replay mode, judge against the last event in the log rather than the
    # wall clock, so the check can be validated against a historical incident.
    now = last_ts if (replay and last_ts) else datetime.datetime.now()
    findings = unprotected(scanned, now)
    orphans = orphaned_stops(scanned, now)

    # Staleness always judges against the WALL clock -- it compares file mtimes to
    # real time, so the replayed log timestamp would make it meaningless.
    stale = check_staleness(datetime.datetime.now())

    if replay:
        print(f"scanned up to {last_ts}")
        print(f"open positions: {len(scanned['positions'])}")
        for key, pos in sorted(scanned["positions"].items()):
            orders = scanned["protective"].get(key, {})
            print(f"  {key[0]:<20} {key[1]:<12} {pos['position']:<6} "
                  f"qty={pos['qty']} @ {pos['avg']} stops={list(orders.values()) or 'NONE'}")
        print(f"\nUNPROTECTED: {len(findings)}")
        for f in findings:
            print(f"  !! {f['account']} {f['instrument']} {f['position']} {f['qty']} "
                  f"@ {f['avg']} naked {f['naked_seconds']}s (since {f['opened']})")

        print(f"\nORPHANED STOPS: {len(orphans)}")
        for f in orphans:
            age = "unknown age" if f["orphan_seconds"] is None else f"{f['orphan_seconds']}s"
            print(f"  !! {f['account']} {f['instrument']} {f['action']} stop @ {f['stop']} "
                  f"with NO position ({age}, flat since {f['flat_since']}, order {f['order_id'][:8]})")

        print(f"\nSTALE: {'YES' if stale else 'no'}")
        if stale:
            print(f"  !! temalimit last wrote {stale['temalimit_last']} "
                  f"({stale['temalimit_age_min']} min ago) while the platform was "
                  f"active {stale['liveness_age_min']} min ago")
        return

    state = load_state()
    alerted = state.get("alerted", {})
    now_epoch = now.timestamp()
    still_naked = set()

    for finding in findings:
        key = f"{finding['account']}|{finding['instrument']}|{finding['opened']}"
        still_naked.add(key)
        last_alert = alerted.get(key, 0)
        if now_epoch - last_alert < REALERT_SECONDS:
            continue

        minutes = finding["naked_seconds"] // 60
        message = (
            f"{finding['account']} {finding['instrument']} "
            f"{finding['position']} {finding['qty']} @ {finding['avg']} has had NO "
            f"protective stop for {minutes} min. Position is unprotected."
        )
        send_alert(message, "NT8: position with no stop")
        log(f"ALERT {message}")
        alerted[key] = now_epoch

    for orphan in orphans:
        key = f"ORPHAN|{orphan['account']}|{orphan['instrument']}|{orphan['order_id']}"
        still_naked.add(key)
        if now_epoch - alerted.get(key, 0) < REALERT_SECONDS:
            continue

        opens = "long" if orphan["action"] == "Buy to cover" else "short"
        age = "unknown time" if orphan["orphan_seconds"] is None else f"{orphan['orphan_seconds'] // 60} min"
        message = (
            f"{orphan['account']} {orphan['instrument']} has a working {orphan['action']} "
            f"stop @ {orphan['stop']} with NO position behind it ({age}, flat since "
            f"{orphan['flat_since']}). If price reaches it, it OPENS a naked {opens}. "
            f"Cancel order {orphan['order_id'][:8]}."
        )
        send_alert(message, "NT8: orphaned stop (no position)")
        log(f"ALERT {message}")
        alerted[key] = now_epoch

    if stale:
        key = "STALE|temalimit"
        still_naked.add(key)
        if now_epoch - alerted.get(key, 0) >= REALERT_SECONDS:
            message = (
                f"temalimit has not written since {stale['temalimit_last']} "
                f"({stale['temalimit_age_min']} min ago) but NinjaTrader is running and "
                f"was active {stale['liveness_age_min']} min ago. Strategies may show "
                f"Enabled while not executing -- open positions are unmanaged."
            )
            send_alert(message, "NT8: temalimit stopped executing")
            log(f"ALERT {message}")
            alerted[key] = now_epoch

    # Drop incidents that resolved, so a later recurrence alerts immediately.
    for key in list(alerted):
        if key not in still_naked:
            del alerted[key]
            log(f"resolved {key}")

    state["alerted"] = alerted
    save_state(state)


if __name__ == "__main__":
    main()
