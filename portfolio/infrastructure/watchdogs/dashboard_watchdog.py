from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8766
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "watchdog.log"
ALERT_MARKER = BASE_DIR / "watchdog_alert.marker"
DISK_ALERT_MARKER = BASE_DIR / "watchdog_disk_alert.marker"
DISK_FREE_GB_THRESHOLD = 10
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
NTFY_TOPIC = "<ntfy-topic>"


def is_listening(port: int) -> bool:
    # A raw TCP connect only proves the socket is bound and accepting -- it stays
    # "true" even if the process crashes on every request after accepting (as
    # happened when this server ran under pythonw.exe with sys.stderr == None: the
    # port looked healthy but every single request got an empty reply, and this
    # check's old connect-only version never noticed, so no alert ever fired).
    # Require an actual HTTP response instead.
    try:
        request = urllib.request.Request(f"http://localhost:{port}/dashboard.html", method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


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


def check_disk_space() -> None:
    free_gb = shutil.disk_usage(BASE_DIR.anchor).free / (1024 ** 3)
    if free_gb >= DISK_FREE_GB_THRESHOLD:
        if DISK_ALERT_MARKER.exists():
            DISK_ALERT_MARKER.unlink()
            send_alert(
                f"Free space on {BASE_DIR.anchor} is back up to {free_gb:.1f} GB.",
                "NT8 Disk Space: recovered",
            )
        return

    log(f"low disk space on {BASE_DIR.anchor}: {free_gb:.1f} GB free")
    if not DISK_ALERT_MARKER.exists():
        DISK_ALERT_MARKER.write_text(datetime.datetime.now().isoformat(), encoding="utf-8")
        send_alert(
            f"Only {free_gb:.1f} GB free on {BASE_DIR.anchor}. Logging, training data, and "
            "backups can start failing silently below this. Free up space soon.",
            "NT8 Disk Space: LOW",
        )


def main() -> None:
    check_disk_space()

    if is_listening(PORT):
        if ALERT_MARKER.exists():
            ALERT_MARKER.unlink()
            send_alert("Live dashboard (port 8766) is back up.", "NT8 Dashboard: recovered")
        return

    log(f"port {PORT} not listening, restarting live_dashboard_server.py")
    subprocess.Popen(
        [str(PYTHONW), str(BASE_DIR / "live_dashboard_server.py")],
        cwd=str(BASE_DIR),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )

    time.sleep(5)
    if is_listening(PORT):
        log("restart confirmed OK")
        return

    log("restart attempted but port still not listening after 5 seconds")
    if not ALERT_MARKER.exists():
        ALERT_MARKER.write_text(datetime.datetime.now().isoformat(), encoding="utf-8")
        send_alert(
            "Live dashboard (port 8766) is down and the watchdog's restart attempt failed. Manual attention needed.",
            "NT8 Dashboard: DOWN",
        )


if __name__ == "__main__":
    main()
