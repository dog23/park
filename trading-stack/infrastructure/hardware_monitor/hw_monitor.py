#!/usr/bin/env python3
# =============================================================================
#  hw_monitor.py  --  Set-and-forget Windows hardware monitor for a
#                     trading / automation laptop, with hosted ntfy.sh alerts.
#
#  PURPOSE
#  -------
#  Decide whether this machine's REAL workload (NinjaTrader strategies,
#  Python servers, Python automation, long-running background processes)
#  can move from 128 GB RAM down to 64 GB RAM.
#
#  It watches RAM / commit / CPU / GPU, distinguishes brief spikes from
#  sustained pressure, and pushes a *small* number of meaningful alerts to
#  your phone via the hosted ntfy.sh service. No spam, no config files,
#  no .env, no template to fill in -- every default is hardcoded below.
#
#  DEPENDENCIES
#  ------------
#      pip install psutil
#  GPU stats use nvidia-smi (already on the machine). pynvml is used if it
#  happens to be installed, but is NOT required.
#
#  RUN
#  ---
#      python  hw_monitor.py         (console, prints a status line each poll)
#      pythonw hw_monitor.py         (silent, for Task Scheduler / background)
#
#  See README.md in this folder for subscribing on your phone and reading
#  the results.
# =============================================================================

import csv
import ctypes
import ctypes.wintypes as wt
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    sys.stderr.write(
        "\n[hw_monitor] psutil is required.  Install it with:\n"
        "    pip install psutil\n\n"
    )
    sys.exit(1)


# =============================================================================
#  >>>>>>>>>>>>>>>>>>>>>>  NTFY TOPIC IS DEFINED HERE  <<<<<<<<<<<<<<<<<<<<<<<<<
# -----------------------------------------------------------------------------
#  This is the ONE thing you must know. Subscribe to this exact topic in the
#  ntfy phone app (Add subscription -> paste the topic string). Anyone who
#  knows this string can read your alerts, so it is deliberately long and
#  random. Change it here if you ever want a fresh channel.
# =============================================================================
NTFY_TOPIC = "<ntfy-topic>"
NTFY_SERVER = "https://ntfy.sh"          # hosted ntfy -- no self-hosting
# =============================================================================


# -----------------------------------------------------------------------------
#  Tunables -- all hardcoded, no config file. Edit here if you really want to.
# -----------------------------------------------------------------------------
POLL_SECONDS = 15                        # how often we sample the machine

GB = 1024 ** 3                           # 1 GB == 1 GiB (matches Task Manager)

# --- RAM decision thresholds (this is the whole point: 64 GB vs 128 GB) ------
RAM_BORDERLINE_GB = 50.0                 # >= this used  -> at least "borderline"
RAM_CRITICAL_GB   = 60.0                 # >  this used  -> "critical" (keep 128)
COMMIT_BORDERLINE_PCT = 80.0             # commit charge vs commit limit
COMMIT_CRITICAL_PCT   = 90.0
PAGEFILE_PRESSURE_PCT = 88.0             # commit% above which we call it "paging pressure"

# --- How long pressure must last before it counts ("sustained", not a spike) -
RAM_ESCALATE_SAMPLES = 12                # ~3 min of continuous worse-or-equal state
RAM_RECOVER_SAMPLES  = 40                # ~10 min back in safe range before "recovered"

CPU_THRESHOLD_PCT   = 90.0
CPU_SUSTAIN_SAMPLES = 20                 # ~5 min continuously above threshold
CPU_REALERT_SECONDS = 30 * 60            # don't re-nag about CPU more than every 30 min

GPU_UTIL_THRESHOLD_PCT = 90.0
GPU_VRAM_THRESHOLD_PCT = 90.0
GPU_SUSTAIN_SAMPLES = 20                 # ~5 min continuously above threshold
GPU_REALERT_SECONDS = 30 * 60

# --- Optional daily heartbeat (kept extremely low-noise: at most once/24h) ----
HEARTBEAT_ENABLED = True
HEARTBEAT_HOUR    = 9                    # local hour (0-23) to send the daily summary

# --- Global debounce backstop so nothing can ever machine-gun notifications ---
MIN_SECONDS_BETWEEN_NOTIFICATIONS = 20

# --- Where logs go (project folder, created automatically, no config) ---------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_JSONL  = os.path.join(SCRIPT_DIR, "hw_monitor_log.jsonl")
LOG_CSV    = os.path.join(SCRIPT_DIR, "hw_monitor_log.csv")

HOSTNAME = socket.gethostname()

# Severity -> recommendation string (exactly as requested)
RECOMMENDATION = {
    "safe":     "64GB likely safe",
    "borderline": "64GB borderline",
    "critical": "Keep 128GB",
}
LEVEL = {"safe": 0, "borderline": 1, "critical": 2}
LEVEL_NAME = {0: "safe", 1: "borderline", 2: "critical"}


# -----------------------------------------------------------------------------
#  Windows commit / pagefile info via GlobalMemoryStatusEx (no extra deps).
#  psutil does not expose the commit limit cleanly on Windows, so we read it
#  directly. Falls back to psutil.swap_memory() if the call ever fails.
# -----------------------------------------------------------------------------
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def read_commit():
    """Return (commit_used_gb, commit_limit_gb, commit_pct) or None on failure.

    'Commit' == ullTotalPageFile (the system commit limit = RAM + pagefile).
    commit_used = total - avail. This is the number that tells you whether
    dropping physical RAM would start forcing the pagefile to work hard.
    """
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)) == 0:
            raise OSError("GlobalMemoryStatusEx failed")
        limit = stat.ullTotalPageFile
        avail = stat.ullAvailPageFile
        used = limit - avail
        pct = (used / limit * 100.0) if limit else 0.0
        return (used / GB, limit / GB, pct)
    except Exception:
        try:
            sw = psutil.swap_memory()
            vm = psutil.virtual_memory()
            used = vm.used + sw.used
            limit = vm.total + sw.total
            pct = (used / limit * 100.0) if limit else 0.0
            return (used / GB, limit / GB, pct)
        except Exception:
            return None


# -----------------------------------------------------------------------------
#  GPU telemetry. Prefer pynvml if importable; otherwise shell out to
#  nvidia-smi. Either way, gracefully returns None when no NVIDIA GPU / driver.
# -----------------------------------------------------------------------------
_NVML = None
try:
    import pynvml as _pynvml
    _pynvml.nvmlInit()
    _NVML = _pynvml
except Exception:
    _NVML = None


def _gpu_via_nvml():
    h = _NVML.nvmlDeviceGetHandleByIndex(0)
    util = _NVML.nvmlDeviceGetUtilizationRates(h).gpu
    mem = _NVML.nvmlDeviceGetMemoryInfo(h)
    vram_used_gb = mem.used / GB
    vram_total_gb = mem.total / GB
    vram_pct = (mem.used / mem.total * 100.0) if mem.total else 0.0
    top_proc = None
    try:
        procs = []
        for fn in (_NVML.nvmlDeviceGetComputeRunningProcesses,
                   _NVML.nvmlDeviceGetGraphicsRunningProcesses):
            try:
                procs.extend(fn(h))
            except Exception:
                pass
        best = None
        for p in procs:
            mem_used = getattr(p, "usedGpuMemory", 0) or 0
            if best is None or mem_used > best[1]:
                try:
                    name = psutil.Process(p.pid).name()
                except Exception:
                    name = str(p.pid)
                best = (name, mem_used)
        if best:
            top_proc = (best[0], best[1] / GB)
    except Exception:
        pass
    return dict(util_pct=float(util), vram_used_gb=vram_used_gb,
                vram_total_gb=vram_total_gb, vram_pct=vram_pct, top_proc=top_proc)


def _gpu_via_smi():
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if out.returncode != 0 or not out.stdout.strip():
        return None
    util_s, used_s, total_s = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
    util = float(util_s)
    vram_used_gb = float(used_s) / 1024.0        # nvidia-smi reports MiB
    vram_total_gb = float(total_s) / 1024.0
    vram_pct = (vram_used_gb / vram_total_gb * 100.0) if vram_total_gb else 0.0

    top_proc = None
    try:
        pout = subprocess.run(
            ["nvidia-smi",
             "--query-compute-apps=used_memory,process_name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        best = None
        for line in pout.stdout.strip().splitlines():
            if not line.strip():
                continue
            mem_s, name = [x.strip() for x in line.split(",", 1)]
            try:
                mem_mb = float(mem_s)
            except ValueError:
                continue
            if best is None or mem_mb > best[1]:
                best = (name, mem_mb)
        if best:
            top_proc = (best[0], best[1] / 1024.0)
    except Exception:
        pass
    return dict(util_pct=util, vram_used_gb=vram_used_gb,
                vram_total_gb=vram_total_gb, vram_pct=vram_pct, top_proc=top_proc)


def read_gpu():
    """Return a gpu dict or None if no NVIDIA GPU / telemetry is available."""
    try:
        if _NVML is not None:
            return _gpu_via_nvml()
    except Exception:
        pass
    try:
        return _gpu_via_smi()
    except Exception:
        return None


# -----------------------------------------------------------------------------
#  Top processes. Each tuple is (name, pid, value). Used both for the ntfy
#  alert text (n=3, kept short) and for process-level log capture (n=5) so
#  a captured episode names its actual culprit instead of just its shape.
# -----------------------------------------------------------------------------
def top_memory_processes(n=5):
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss
            procs.append((p.info["name"] or "?", p.info["pid"], rss / GB))
        except Exception:
            continue
    procs.sort(key=lambda x: x[2], reverse=True)
    return procs[:n]


# These are not real workload -- they represent idle/kernel time, not consumption.
_CPU_PROC_IGNORE = {"System Idle Process", "Idle"}


def top_cpu_processes(n=5):
    # Prime cpu_percent (first call always returns 0.0), wait briefly, read again.
    procs = list(psutil.process_iter(["name", "pid"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.4)
    ncpu = psutil.cpu_count() or 1
    results = []
    for p in procs:
        try:
            name = p.info["name"] or "?"
            pid = p.info["pid"]
            if pid == 0 or name in _CPU_PROC_IGNORE:
                continue                         # skip the idle "process"
            pct = p.cpu_percent(None) / ncpu     # normalize to whole-machine %
            results.append((name, pid, pct))
        except Exception:
            continue
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:n]


def fmt_proc_list(procs, unit):
    if not procs:
        return "n/a"
    return ", ".join(f"{name} (pid {pid}) {val:.1f}{unit}" for name, pid, val in procs)


def _procs_to_json(procs, value_key):
    return [{"name": name, "pid": pid, value_key: round(val, 3)} for name, pid, val in procs]


# -----------------------------------------------------------------------------
#  ntfy.sh sender -- robust: never raises, never crashes the monitor.
# -----------------------------------------------------------------------------
def send_ntfy(title, message, priority="default", tags=None):
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": priority,          # min/low/default/high/urgent
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    data = message.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        # Network / ntfy outage: log and carry on. Never let this kill the loop.
        _log_line({"ts": _now_iso(), "event": "ntfy_error", "detail": str(e)})
        return False
    except Exception as e:
        _log_line({"ts": _now_iso(), "event": "ntfy_error", "detail": repr(e)})
        return False


# -----------------------------------------------------------------------------
#  Logging (JSONL always; CSV mirror of the numeric fields). No config needed.
# -----------------------------------------------------------------------------
_CSV_FIELDS = ["ts", "ram_used_gb", "ram_pct", "ram_avail_gb", "commit_pct",
               "cpu_pct", "gpu_pct", "vram_pct", "ram_state", "event",
               "top_ram_proc", "top_ram_proc_gb", "top_cpu_proc", "top_cpu_proc_pct"]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _log_line(obj):
    try:
        with open(LOG_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def _log_csv(sample, state, event=""):
    try:
        new = not os.path.exists(LOG_CSV)
        top_ram = sample.get("top_ram_procs") or []
        top_cpu = sample.get("top_cpu_procs") or []
        with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({
                "ts": sample["ts"],
                "ram_used_gb": round(sample["ram_used_gb"], 2),
                "ram_pct": round(sample["ram_pct"], 1),
                "ram_avail_gb": round(sample["ram_avail_gb"], 2),
                "commit_pct": round(sample["commit_pct"], 1) if sample["commit_pct"] is not None else "",
                "cpu_pct": round(sample["cpu_pct"], 1),
                "gpu_pct": round(sample["gpu_pct"], 1) if sample["gpu_pct"] is not None else "",
                "vram_pct": round(sample["vram_pct"], 1) if sample["vram_pct"] is not None else "",
                "ram_state": state,
                "event": event,
                "top_ram_proc": top_ram[0]["name"] if top_ram else "",
                "top_ram_proc_gb": top_ram[0]["rss_gb"] if top_ram else "",
                "top_cpu_proc": top_cpu[0]["name"] if top_cpu else "",
                "top_cpu_proc_pct": top_cpu[0]["cpu_pct"] if top_cpu else "",
            })
    except Exception:
        pass


# -----------------------------------------------------------------------------
#  Classification + sampling
# -----------------------------------------------------------------------------
def classify_ram(used_gb, commit_pct):
    """Instantaneous RAM severity for a single sample."""
    if used_gb > RAM_CRITICAL_GB or (commit_pct is not None and commit_pct > COMMIT_CRITICAL_PCT):
        return "critical"
    if used_gb >= RAM_BORDERLINE_GB or (commit_pct is not None and commit_pct > COMMIT_BORDERLINE_PCT):
        return "borderline"
    return "safe"


def paging_status(commit_pct):
    if commit_pct is None:
        return "unknown"
    if commit_pct >= PAGEFILE_PRESSURE_PCT:
        return f"HIGH ({commit_pct:.0f}% commit)"
    if commit_pct >= COMMIT_BORDERLINE_PCT:
        return f"elevated ({commit_pct:.0f}% commit)"
    return f"low ({commit_pct:.0f}% commit)"


def sample_machine(cpu_pct):
    vm = psutil.virtual_memory()
    commit = read_commit()                 # (used_gb, limit_gb, pct) or None
    commit_pct = commit[2] if commit else None
    gpu = read_gpu()
    s = {
        "ts": _now_iso(),
        "ram_used_gb": vm.used / GB,
        "ram_pct": vm.percent,
        "ram_avail_gb": vm.available / GB,
        "ram_total_gb": vm.total / GB,
        "commit_used_gb": commit[0] if commit else None,
        "commit_limit_gb": commit[1] if commit else None,
        "commit_pct": commit_pct,
        "cpu_pct": cpu_pct,
        "gpu_pct": gpu["util_pct"] if gpu else None,
        "vram_pct": gpu["vram_pct"] if gpu else None,
        "vram_used_gb": gpu["vram_used_gb"] if gpu else None,
        "vram_total_gb": gpu["vram_total_gb"] if gpu else None,
        "gpu_top_proc": gpu["top_proc"] if gpu else None,
    }
    s["ram_state"] = classify_ram(s["ram_used_gb"], commit_pct)
    return s


# -----------------------------------------------------------------------------
#  Notification bodies
# -----------------------------------------------------------------------------
def build_ram_body(sample, severity):
    lines = [
        f"Host: {HOSTNAME}",
        f"Severity: {severity.upper()}",
        f"RAM: {sample['ram_used_gb']:.1f} GB used "
        f"({sample['ram_pct']:.0f}%), {sample['ram_avail_gb']:.1f} GB free "
        f"of {sample['ram_total_gb']:.0f} GB",
        f"CPU: {sample['cpu_pct']:.0f}%",
    ]
    if sample["gpu_pct"] is not None:
        lines.append(
            f"GPU: {sample['gpu_pct']:.0f}% util, "
            f"VRAM {sample['vram_used_gb']:.1f}/{sample['vram_total_gb']:.1f} GB "
            f"({sample['vram_pct']:.0f}%)")
    lines.append(f"Paging: {paging_status(sample['commit_pct'])}")
    lines.append("Top RAM: " + fmt_proc_list(top_memory_processes(3), " GB"))
    lines.append("Top CPU: " + fmt_proc_list(top_cpu_processes(3), "%"))
    lines.append(f">> Recommendation: {RECOMMENDATION[severity]}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
#  Main loop with state machine
# -----------------------------------------------------------------------------
def main():
    # Prime CPU measurement so the first real reading is meaningful.
    psutil.cpu_percent(None)
    time.sleep(1.0)

    # --- Startup test notification (requirement: exactly one at launch) -------
    boot = sample_machine(psutil.cpu_percent(None))
    gpu_line = "GPU telemetry: available" if boot["gpu_pct"] is not None \
        else "GPU telemetry: NOT available (no NVIDIA driver/nvidia-smi)"
    startup_msg = (
        f"Host: {HOSTNAME}\n"
        f"hw_monitor started OK.\n"
        f"RAM now: {boot['ram_used_gb']:.1f} GB used ({boot['ram_pct']:.0f}%), "
        f"{boot['ram_avail_gb']:.1f} GB free of {boot['ram_total_gb']:.0f} GB\n"
        f"Commit/paging: {paging_status(boot['commit_pct'])}\n"
        f"CPU: {boot['cpu_pct']:.0f}%\n"
        f"{gpu_line}\n"
        f"Watching for the 64GB-vs-128GB decision. You'll only hear from me "
        f"on sustained changes."
    )
    send_ntfy("hw_monitor online", startup_msg, priority="low", tags=["computer", "white_check_mark"])
    _log_line({"ts": _now_iso(), "event": "startup", "sample": boot})
    _log_csv(boot, boot["ram_state"], "startup")
    print(f"[hw_monitor] started on {HOSTNAME}. Topic: {NTFY_TOPIC}")

    # --- State ----------------------------------------------------------------
    ram_states = deque(maxlen=max(RAM_ESCALATE_SAMPLES, RAM_RECOVER_SAMPLES))
    confirmed_ram = "safe"                 # last state we actually notified about
    peak_ram = "safe"                      # worst confirmed state since last recovery

    cpu_hist = deque(maxlen=CPU_SUSTAIN_SAMPLES)
    gpu_hist = deque(maxlen=GPU_SUSTAIN_SAMPLES)
    last_cpu_alert = 0.0
    last_gpu_alert = 0.0

    last_notify_ts = 0.0
    last_heartbeat_day = None

    def notify(title, body, priority, tags):
        nonlocal last_notify_ts
        # Global debounce backstop -- prevents any accidental burst.
        if time.time() - last_notify_ts < MIN_SECONDS_BETWEEN_NOTIFICATIONS:
            time.sleep(MIN_SECONDS_BETWEEN_NOTIFICATIONS)
        ok = send_ntfy(title, body, priority=priority, tags=tags)
        last_notify_ts = time.time()
        return ok

    while True:
        loop_start = time.time()
        try:
            cpu_pct = psutil.cpu_percent(None)      # avg since last call (~POLL_SECONDS)
            s = sample_machine(cpu_pct)
            inst_state = s["ram_state"]
            ram_states.append(LEVEL[inst_state])
            cpu_hist.append(cpu_pct)
            gpu_hist.append(s["gpu_pct"] if s["gpu_pct"] is not None else 0.0)

            event = ""

            # ---------------- RAM state machine (sustained only) --------------
            # ESCALATION: the floor of the last N samples is worse than confirmed.
            if len(ram_states) >= RAM_ESCALATE_SAMPLES:
                window = list(ram_states)[-RAM_ESCALATE_SAMPLES:]
                sustained_level = min(window)      # level held throughout the window
                if sustained_level > LEVEL[confirmed_ram]:
                    new_state = LEVEL_NAME[sustained_level]
                    body = build_ram_body(s, new_state)
                    pr = "urgent" if new_state == "critical" else "high"
                    tag = "rotating_light" if new_state == "critical" else "warning"
                    notify(f"RAM {new_state.upper()} - {RECOMMENDATION[new_state]}",
                           body, pr, [tag, "floppy_disk"])
                    event = f"escalate:{confirmed_ram}->{new_state}"
                    confirmed_ram = new_state
                    peak_ram = LEVEL_NAME[max(LEVEL[peak_ram], sustained_level)]

            # RECOVERY: sustained back in "safe" range for the longer cooldown.
            if confirmed_ram != "safe" and len(ram_states) >= RAM_RECOVER_SAMPLES:
                window = list(ram_states)[-RAM_RECOVER_SAMPLES:]
                if max(window) == LEVEL["safe"]:
                    body = build_ram_body(s, "safe")
                    body = (f"Recovered from {peak_ram.upper()}.\n\n" + body)
                    notify(f"RAM recovered - {RECOMMENDATION['safe']}",
                           body, "default", ["white_check_mark", "floppy_disk"])
                    event = f"recover:{confirmed_ram}->safe"
                    confirmed_ram = "safe"
                    peak_ram = "safe"

            # ---------------- Sustained CPU alert -----------------------------
            if len(cpu_hist) >= CPU_SUSTAIN_SAMPLES and all(c > CPU_THRESHOLD_PCT for c in cpu_hist):
                if time.time() - last_cpu_alert >= CPU_REALERT_SECONDS:
                    body = (f"Host: {HOSTNAME}\n"
                            f"Severity: CPU SUSTAINED\n"
                            f"CPU held >{CPU_THRESHOLD_PCT:.0f}% for "
                            f"~{CPU_SUSTAIN_SAMPLES * POLL_SECONDS // 60} min "
                            f"(now {cpu_pct:.0f}%).\n"
                            f"RAM: {s['ram_used_gb']:.1f} GB ({s['ram_pct']:.0f}%)\n"
                            f"Top CPU: " + fmt_proc_list(top_cpu_processes(3), "%"))
                    notify("CPU sustained high", body, "high", ["fire"])
                    last_cpu_alert = time.time()
                    event = (event + "|" if event else "") + "cpu_sustained"

            # ---------------- Sustained GPU / VRAM alert ----------------------
            if s["gpu_pct"] is not None and len(gpu_hist) >= GPU_SUSTAIN_SAMPLES:
                util_sustained = all(g > GPU_UTIL_THRESHOLD_PCT for g in gpu_hist)
                vram_high = s["vram_pct"] is not None and s["vram_pct"] > GPU_VRAM_THRESHOLD_PCT
                if (util_sustained or vram_high) and time.time() - last_gpu_alert >= GPU_REALERT_SECONDS:
                    reason = []
                    if util_sustained:
                        reason.append(f"util >{GPU_UTIL_THRESHOLD_PCT:.0f}% sustained")
                    if vram_high:
                        reason.append(f"VRAM {s['vram_pct']:.0f}%")
                    top = s["gpu_top_proc"]
                    top_s = f"{top[0]} {top[1]:.1f} GB" if top else "n/a"
                    body = (f"Host: {HOSTNAME}\n"
                            f"Severity: GPU SUSTAINED\n"
                            f"Reason: {', '.join(reason)}\n"
                            f"GPU: {s['gpu_pct']:.0f}% util, "
                            f"VRAM {s['vram_used_gb']:.1f}/{s['vram_total_gb']:.1f} GB "
                            f"({s['vram_pct']:.0f}%)\n"
                            f"Top GPU proc: {top_s}")
                    notify("GPU sustained high", body, "high", ["fire", "video_game"])
                    last_gpu_alert = time.time()
                    event = (event + "|" if event else "") + "gpu_sustained"

            # ---------------- Optional daily heartbeat (<=1/day) --------------
            if HEARTBEAT_ENABLED:
                now = datetime.now()
                if now.hour == HEARTBEAT_HOUR and last_heartbeat_day != now.date():
                    body = build_ram_body(s, inst_state)
                    body = ("Daily heartbeat -- monitor alive.\n\n" + body)
                    notify("hw_monitor daily heartbeat", body, "min", ["bar_chart"])
                    last_heartbeat_day = now.date()
                    event = (event + "|" if event else "") + "heartbeat"

            # ---------------- Process-level capture ----------------------------
            # Only while there's pressure worth explaining (borderline/critical,
            # or any state-change event) -- keeps the common "safe" case cheap
            # and avoids psutil.process_iter() + the 0.4s CPU-sample sleep on
            # every single 15s poll.
            if inst_state != "safe" or event:
                s["top_ram_procs"] = _procs_to_json(top_memory_processes(5), "rss_gb")
                s["top_cpu_procs"] = _procs_to_json(top_cpu_processes(5), "cpu_pct")

            # ---------------- Log every sample --------------------------------
            _log_line({**s, "confirmed_ram": confirmed_ram, "event": event})
            _log_csv(s, inst_state, event)

            # Console line (invisible under pythonw; handy under python).
            gline = (f" GPU {s['gpu_pct']:.0f}% VRAM {s['vram_pct']:.0f}%"
                     if s["gpu_pct"] is not None else " GPU n/a")
            print(f"{s['ts']}  RAM {s['ram_used_gb']:5.1f}GB "
                  f"({s['ram_pct']:4.0f}%) commit {('%.0f%%' % s['commit_pct']) if s['commit_pct'] is not None else 'n/a':>4}  "
                  f"CPU {cpu_pct:4.0f}%{gline}  [{inst_state}]"
                  + (f"  <{event}>" if event else ""),
                  flush=True)

        except Exception as e:
            # Never crash the 24/7 loop. Log and keep going.
            _log_line({"ts": _now_iso(), "event": "loop_error", "detail": repr(e)})

        # Sleep the remainder of the poll interval (accounts for work time).
        elapsed = time.time() - loop_start
        time.sleep(max(1.0, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[hw_monitor] stopped.")
