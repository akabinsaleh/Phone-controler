"""PC Remote agent.

Small HTTP server that lets the PC Remote iPhone app (and the web page it
serves at "/") see this PC's status, put it to sleep, restart it, launch and
close apps. Every API call needs the shared token from config.json.

Reachable three ways:
  * home Wi-Fi:  http://<this PC's LAN IP>:8765
  * anywhere:    https://<tailscale name>.ts.net  (Tailscale Funnel on this PC
                 forwards to localhost:8765; the phone needs no VPN)
  * wake-up:     the agent keeps a UDP port forwarded on the router (UPnP) so a
                 Wake-on-LAN packet sent to the home public IP reaches this PC
                 while it sleeps.

Starts at logon via PCRemote.vbs in the Startup folder.
"""

import ctypes
import ctypes.wintypes as wt
import hmac
import ipaddress
import json
import logging
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

import upnp

# Python's default trust store on this PC rejects Let's Encrypt chains
# ("certificate has expired") although Windows itself accepts them, so HTTPS
# calls (ntfy, ipify) use the certifi root bundle instead.
try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL = ssl.create_default_context()


def https_open(req, timeout: float = 10):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL)

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
TOKEN = CONFIG["token"]
PORT = int(CONFIG.get("port", 8765))
WOL_PORT = int(CONFIG.get("wol_port", 40009))
APPS = CONFIG["apps"]
WEB_INDEX = BASE / "web" / "index.html"

logging.basicConfig(
    filename=BASE / "agent.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Processes whose windows are desktop plumbing, not apps the user opened.
HIDDEN_WINDOW_PROCESSES = {
    "explorer.exe", "textinputhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "systemsettings.exe",
    "lockapp.exe", "nvidia overlay.exe", "medal.exe",
}

# Never killed by "Close all": the desktop itself, this agent, and the
# Claude app (so a remote tap can't cut off a session that's editing the PC).
PROTECTED_PROCESSES = {"explorer.exe", "pythonw.exe", "python.exe", "claude.exe"}

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def is_lan(ip: str) -> bool:
    """Home network, this PC (which is also what Funnel traffic looks like), or Tailscale."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr in TAILSCALE_NET


# --- Power ------------------------------------------------------------------

def go_to_sleep() -> None:
    # SetSuspendState(Hibernate=False, ForceCritical=True, DisableWakeEvent=False)
    # Hibernate=False means suspend to RAM (sleep), so Wake-on-LAN can wake it.
    ctypes.windll.powrprof.SetSuspendState(0, 1, 0)


def restart_pc() -> None:
    subprocess.Popen(
        ["shutdown", "/r", "/t", "3", "/c", "Restart requested from PC Remote"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


# --- Internet reachability (public IP + router port for Wake-on-LAN) ---------

_net_state = {
    "public_ip": "",
    "public_ip_source": "",
    "public_ip_at": 0.0,
    "gateway": None,
    "gateway_name": "",
    "wol_mapping": "not tried",
}


def _gateway():
    gw = _net_state["gateway"]
    if gw is None:
        try:
            gw = upnp.find_gateway(gateway_ip=_default_gateway_ip())
        except Exception:  # noqa: BLE001
            gw = None
        _net_state["gateway"] = gw
        _net_state["gateway_name"] = f"{gw.manufacturer} {gw.model}".strip() if gw else ""
    return gw


def _default_gateway_ip() -> str | None:
    try:
        for nic, addrs in psutil.net_if_addrs().items():
            pass
        out = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True, text=True,
                             timeout=5, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                return parts[2]
    except Exception:  # noqa: BLE001
        pass
    return None


def refresh_public_ip() -> None:
    ip, source = "", ""
    gw = _gateway()
    if gw is not None:
        try:
            ip, source = gw.external_ip(), "router"
        except Exception:  # noqa: BLE001
            _net_state["gateway"] = None
    if not ip:
        try:
            ip = https_open("https://api.ipify.org", timeout=5).read().decode().strip()
            source = "ipify"
        except Exception:  # noqa: BLE001
            pass
    if ip:
        _net_state.update(public_ip=ip, public_ip_source=source, public_ip_at=time.time())


def ensure_wol_mapping() -> None:
    """Keep UDP WOL_PORT forwarded from the internet to this PC."""
    gw = _gateway()
    if gw is None:
        _net_state["wol_mapping"] = "router has no UPnP"
        return
    try:
        lan = upnp.lan_ip()
        existing = gw.get_port_mapping(WOL_PORT, "UDP")
        if existing and existing.get("NewInternalClient") == lan:
            _net_state["wol_mapping"] = f"ok -> {lan}:{WOL_PORT}"
            return
        last_error = ""
        for lease in (0, 604800):
            try:
                gw.add_port_mapping(WOL_PORT, "UDP", lan, WOL_PORT, "PC Remote wake", lease)
                _net_state["wol_mapping"] = f"ok -> {lan}:{WOL_PORT} (lease {lease or 'permanent'})"
                logging.info("UPnP: mapped UDP %d -> %s:%d", WOL_PORT, lan, WOL_PORT)
                return
            except upnp.UPnPError as exc:
                last_error = str(exc)
        _net_state["wol_mapping"] = f"failed: {last_error}"
        logging.warning("UPnP mapping failed: %s", last_error)
    except Exception as exc:  # noqa: BLE001
        _net_state["wol_mapping"] = f"failed: {exc}"
        _net_state["gateway"] = None


TAILSCALE_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
_funnel = {"state": "unknown", "url": "", "enable_link": "", "proc": None}


def _run(cmd: list[str], timeout: int = 20) -> str:
    # stdin must be closed: the Tailscale CLI otherwise waits for a key press
    # when it wants to show its "Funnel is not enabled" message.
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    return (out.stdout or "") + (out.stderr or "")


def _funnel_is_on() -> bool:
    state = _run([str(TAILSCALE_EXE), "funnel", "status"])
    if f":{PORT}" in state and "https://" in state:
        for line in state.splitlines():
            if line.strip().startswith("https://"):
                _funnel["url"] = line.strip().split()[0].rstrip("/")
                break
        _funnel["state"] = "on"
        return True
    return False


def ensure_funnel() -> None:
    """Keep Tailscale Funnel publishing this agent at https://<pc>.<tailnet>.ts.net.

    Funnel must be switched on once in the Tailscale admin console. Until then
    `tailscale funnel` prints an enable link and then waits (for as long as it
    takes) for the switch to be flipped, so it is run in the background and the
    link is surfaced in /status as funnel_enable_link.
    """
    if not TAILSCALE_EXE.exists():
        _funnel["state"] = "tailscale not installed"
        return
    try:
        if _funnel_is_on():
            _funnel["proc"] = None
            return
        proc = _funnel["proc"]
        if proc is not None and proc.poll() is None:
            return  # still waiting for the enable click
        proc = subprocess.Popen(
            [str(TAILSCALE_EXE), "funnel", "--bg", "--yes", str(PORT)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _funnel.update(proc=proc, state="starting")

        def pump():
            for raw in proc.stdout:
                line = raw.strip()
                if line.startswith("https://login.tailscale.com/f/funnel"):
                    _funnel.update(enable_link=line, state="needs enable")
                    logging.warning("Funnel not enabled on the tailnet; enable at %s", line)
                elif line:
                    logging.info("funnel cli: %s", line)
            rc = proc.wait()
            logging.info("funnel cli exited with %s", rc)
            if _funnel_is_on():
                logging.info("Funnel on: %s", _funnel["url"])

        threading.Thread(target=pump, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        _funnel["state"] = f"failed: {exc}"


def _maintenance_loop() -> None:
    tick = 0
    last = time.time()
    # Fresh boot (agent started within 3 minutes of Windows starting) -> tell the phone.
    if time.time() - psutil.boot_time() < 180:
        notify("PC is on", f"{os.environ.get('COMPUTERNAME', 'PC')} just booted.", tags="green_circle")
    while True:
        now = time.time()
        if now - last > 90:  # the loop sleeps 10 s; a gap this big means the PC was asleep
            notify("PC is on", f"{os.environ.get('COMPUTERNAME', 'PC')} woke up from sleep.", tags="green_circle")
        last = now
        try:
            if tick % 60 == 0:          # every 10 minutes
                refresh_public_ip()
            # Every hour, or every 10 minutes while the router rule isn't in place
            # (e.g. the network wasn't up yet when Windows logged in).
            if tick % 360 == 0 or (tick % 60 == 0 and not _net_state["wol_mapping"].startswith("ok")):
                ensure_wol_mapping()
            if tick % 30 == 0 and _funnel["state"] != "on":   # every 5 minutes until Funnel is up
                ensure_funnel()
        except Exception:  # noqa: BLE001
            logging.exception("maintenance failed")
        tick += 1
        time.sleep(10)


# --- Windows / processes ----------------------------------------------------

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
DWMWA_CLOAKED = 14
WM_CLOSE = 0x0010
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def open_windows(process_names: dict[int, str]) -> list[dict]:
    """Visible, non-cloaked top-level windows with a title."""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        cloaked = wt.DWORD(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = process_names.get(pid.value, "")
        if not exe or exe.lower() in HIDDEN_WINDOW_PROCESSES:
            return True
        app = exe[:-4] if exe.lower().endswith(".exe") else exe
        key = (app, buf.value)
        if key not in seen:
            seen.add(key)
            found.append({"app": app, "title": buf.value, "hwnd": int(hwnd), "pid": pid.value})
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found


def process_names() -> dict[int, str]:
    names: dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        names[proc.info["pid"]] = proc.info.get("name") or ""
    return names


def close_window(hwnd: int) -> bool:
    """Ask one window to close (same as clicking its X)."""
    if not user32.IsWindow(hwnd):
        return False
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return True


def _terminate_later(pids: list[int], delay: float = 3.0) -> None:
    def finish():
        time.sleep(delay)
        for pid in pids:
            if pid == os.getpid():
                continue
            try:
                proc = psutil.Process(pid)
                if proc.is_running():
                    proc.terminate()
            except psutil.Error:
                pass
    threading.Thread(target=finish, daemon=True).start()


def end_window_task(hwnd: int) -> bool:
    """Close a window; if it is still there after 3 s, end its process
    (same as Task Manager's "End task")."""
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not close_window(hwnd):
        return False

    def finish():
        time.sleep(3)
        if user32.IsWindow(hwnd) and pid.value and pid.value != os.getpid():
            try:
                psutil.Process(pid.value).terminate()
            except psutil.Error:
                pass

    threading.Thread(target=finish, daemon=True).start()
    return True


def close_app(names_wanted: list[str]) -> int:
    """Close every window of the given processes, then end any still alive
    after a few seconds (apps that only minimize to the tray)."""
    wanted = {n.lower() for n in names_wanted}
    names = process_names()
    pids = [pid for pid, name in names.items() if name.lower() in wanted and pid != os.getpid()]
    if not pids:
        return 0
    for win in open_windows(names):
        if win["pid"] in pids:
            close_window(win["hwnd"])
    _terminate_later(pids)
    return len(pids)


def close_all() -> int:
    """Close every open window (like Task Manager "End task" on each app)."""
    names = process_names()
    targets: dict[int, list[int]] = {}
    for win in open_windows(names):
        exe = names.get(win["pid"], "").lower()
        if win["pid"] == os.getpid() or exe in PROTECTED_PROCESSES:
            continue
        targets.setdefault(win["pid"], []).append(win["hwnd"])
    for hwnds in targets.values():
        for hwnd in hwnds:
            close_window(hwnd)
    _terminate_later(list(targets))
    return len(targets)


# --- Microphone -------------------------------------------------------------

# Microphone control runs in a separate process (mic_helper.py). The Windows
# COM objects behind it crashed the agent twice (access violations during
# garbage collection on the wrong thread); in their own process a crash only
# costs a helper restart. The helper mutes/unmutes EVERY active recording
# device: apps like Discord pick a specific mic (the QuadCast) while the
# Windows default is a virtual one (Voicemod).
_mic = {"proc": None, "lock": threading.Lock(), "state": None, "t": 0.0}


def _mic_helper_start():
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "mic_helper.py")], cwd=str(BASE),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _mic["proc"] = proc
    return proc


def _mic_command(cmd: str) -> bool | None:
    """Send one command to the helper (restarting it if it died); returns muted state."""
    with _mic["lock"]:
        for attempt in range(2):
            proc = _mic["proc"]
            try:
                if proc is None or proc.poll() is not None:
                    proc = _mic_helper_start()
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("helper closed its output")
                answer = json.loads(line)
                if not answer.get("ok"):
                    raise RuntimeError(answer.get("error", "helper error"))
                _mic.update(state=answer.get("muted"), t=time.time())
                return _mic["state"]
            except Exception as exc:  # noqa: BLE001
                logging.warning("mic helper failed (%s), attempt %d", exc, attempt + 1)
                try:
                    if proc is not None:
                        proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                _mic["proc"] = None
        return None


def mic_muted() -> bool | None:
    # Status is polled every 3 s; ask the helper at most every 5 s.
    if time.time() - _mic["t"] < 5:
        return _mic["state"]
    return _mic_command("get")


def set_mic_muted(muted: bool) -> bool | None:
    return _mic_command("mute" if muted else "unmute")


# --- Camera (Windows privacy switch: Settings > Privacy > Camera, "Let apps access your camera")

import winreg

_CAM_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam",
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam\NonPackaged",
)


def _privacy_camera_enabled() -> bool | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CAM_KEYS[0]) as k:
            value, _ = winreg.QueryValueEx(k, "Value")
        return value != "Deny"
    except OSError:
        return None


def _set_privacy_camera(enabled: bool) -> None:
    try:
        for path in _CAM_KEYS:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
                winreg.SetValueEx(k, "Value", 0, winreg.REG_SZ, "Allow" if enabled else "Deny")
    except OSError:
        logging.exception("camera privacy setting failed")


# Device-level switch: the camera hardware is disabled/enabled in Device Manager
# (cuts even a running Discord stream). pnputil needs admin, so it runs through
# two scheduled tasks created once with admin rights:
#   "PCRemote Camera Off" -> pnputil /disable-device /class Camera
#   "PCRemote Camera On"  -> pnputil /enable-device  /class Camera
_cam_cache = {"t": 0.0, "devices": []}


def camera_devices() -> list[dict]:
    """[{id, name, started}] for every device in the Camera class (cached 5 s)."""
    if time.time() - _cam_cache["t"] < 5:
        return _cam_cache["devices"]
    devices, cur = [], {}
    try:
        out = _run(["pnputil", "/enum-devices", "/class", "Camera"], timeout=15)
        for line in out.splitlines():
            if line.startswith("Instance ID:"):
                if cur:
                    devices.append(cur)
                cur = {"id": line.split(":", 1)[1].strip(), "name": "", "started": False}
            elif line.startswith("Device Description:") and cur:
                cur["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Status:") and cur:
                cur["started"] = "Started" in line
        if cur:
            devices.append(cur)
    except Exception:  # noqa: BLE001
        logging.exception("camera enumeration failed")
    _cam_cache.update(t=time.time(), devices=devices)
    return devices


_task_cache: dict[str, tuple[float, bool]] = {}


def _task_exists(name: str) -> bool:
    """schtasks lookup, cached 10 minutes (it is slow and rarely changes)."""
    t, exists = _task_cache.get(name, (0.0, False))
    if time.time() - t > 600:
        exists = "TaskName" in _run(["schtasks", "/query", "/tn", name], timeout=15)
        _task_cache[name] = (time.time(), exists)
    return exists


def camera_enabled() -> bool | None:
    devs = camera_devices()
    if devs:
        return any(d["started"] for d in devs)
    return _privacy_camera_enabled()


def set_camera_enabled(enabled: bool) -> bool | None:
    _set_privacy_camera(enabled)
    task = "PCRemote Camera On" if enabled else "PCRemote Camera Off"
    if _task_exists(task):
        _run(["schtasks", "/run", "/tn", task], timeout=15)
        for _ in range(10):          # wait up to ~5 s for Device Manager to apply it
            time.sleep(0.5)
            _cam_cache["t"] = 0.0
            if camera_enabled() == enabled:
                break
    else:
        logging.warning("scheduled task %r missing; only the privacy switch was changed", task)
    _cam_cache["t"] = 0.0
    return camera_enabled()


# --- AFK watch: notify the phone (ntfy) when the PC is on but nobody is using it

AFK = CONFIG.get("afk", {})
AFK_MINUTES = int(AFK.get("minutes", 30))
AFK_REPEAT_MINUTES = int(AFK.get("repeat_minutes", 120))
NTFY_TOPIC = AFK.get("ntfy_topic", "")
NTFY_URL = AFK.get("ntfy_url", "https://ntfy.sh")


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> int:
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return int((ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000)


def notify(title: str, message: str, tags: str = "desktop_computer") -> bool:
    if not NTFY_TOPIC:
        return False
    try:
        req = urllib.request.Request(
            f"{NTFY_URL}/{NTFY_TOPIC}", data=message.encode("utf-8"),
            headers={"Title": title, "Tags": tags, "Priority": "default"},
        )
        https_open(req, timeout=10).read()
        return True
    except Exception:  # noqa: BLE001
        logging.exception("ntfy notification failed")
        return False


_afk = {"notified_at": 0.0, "last_idle": 0}


def _afk_loop() -> None:
    while True:
        try:
            idle = idle_seconds()
            _afk["last_idle"] = idle
            now = time.time()
            if idle >= AFK_MINUTES * 60:
                due = now - _afk["notified_at"] >= AFK_REPEAT_MINUTES * 60
                if due:
                    minutes = idle // 60
                    if notify("PC is on and idle",
                              f"{os.environ.get('COMPUTERNAME', 'PC')} has had no keyboard/mouse for {minutes} min. "
                              f"Open PC Remote to put it to sleep.", tags="zzz"):
                        _afk["notified_at"] = now
                        logging.info("AFK notification sent (idle %d min)", minutes)
            else:
                _afk["notified_at"] = 0.0  # back at the PC: next long idle notifies again
        except Exception:  # noqa: BLE001
            logging.exception("afk watch failed")
        time.sleep(60)


# --- Status & stats ---------------------------------------------------------

# CPU load sampled continuously in the background, so every request sees a
# fresh 1-second average instead of a point sample.
_cpu = {"total": 0.0, "per_core": []}


def _cpu_sampler() -> None:
    while True:
        per_core = psutil.cpu_percent(interval=1, percpu=True)
        _cpu["per_core"] = per_core
        _cpu["total"] = round(sum(per_core) / max(len(per_core), 1), 1)


def status() -> dict:
    names = process_names()
    running_exes = {n.lower() for n in names.values()}
    running_apps = [
        name for name, app in APPS.items()
        if any(p.lower() in running_exes for p in app.get("process", []))
    ]
    mem = psutil.virtual_memory()
    return {
        "ok": True,
        "name": os.environ.get("COMPUTERNAME", ""),
        "cpu": _cpu["total"],
        "ram_used_gb": round((mem.total - mem.available) / 2**30, 1),
        "ram_total_gb": round(mem.total / 2**30, 1),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "running": running_apps,
        "windows": open_windows(names),
        "public_ip": _net_state["public_ip"],
        "wol_port": WOL_PORT,
        "wol_mapping": _net_state["wol_mapping"],
        "lan_ip": upnp.lan_ip(),
        "funnel": _funnel["state"],
        "funnel_url": _funnel["url"],
        "funnel_enable_link": _funnel["enable_link"],
        "mic_muted": mic_muted(),
        "camera_enabled": camera_enabled(),
        "camera_hard_switch": _task_exists("PCRemote Camera Off"),
        "idle_s": _afk["last_idle"],
        "afk_minutes": AFK_MINUTES,
        "ntfy_topic": NTFY_TOPIC,
    }


_net_last = {"t": time.time(), "sent": 0, "recv": 0}


def gpu_stats() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        line = out.stdout.strip().splitlines()[0]
        name, util, used, total, temp = [p.strip() for p in line.split(",")]
        return {
            "name": name,
            "util": float(util),
            "mem_used_gb": round(float(used) / 1024, 1),
            "mem_total_gb": round(float(total) / 1024, 1),
            "temp_c": float(temp),
        }
    except Exception:  # noqa: BLE001
        return None


def detailed_stats() -> dict:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    freq = psutil.cpu_freq()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({
            "name": part.mountpoint.rstrip("\\"),
            "used_gb": round(usage.used / 2**30, 1),
            "total_gb": round(usage.total / 2**30, 1),
        })
    net = psutil.net_io_counters()
    now = time.time()
    dt = max(now - _net_last["t"], 0.001)
    up_rate = (net.bytes_sent - _net_last["sent"]) / dt if _net_last["sent"] else 0.0
    down_rate = (net.bytes_recv - _net_last["recv"]) / dt if _net_last["recv"] else 0.0
    _net_last.update(t=now, sent=net.bytes_sent, recv=net.bytes_recv)

    threads = psutil.cpu_count(logical=True) or 1
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        name = (p.info.get("name") or "").removesuffix(".exe")
        if name in ("System Idle Process", "System", ""):
            continue
        try:
            mem_info = p.info.get("memory_info")
            procs.append({
                "name": name,
                # psutil reports per-core percent; normalize to 0-100 of the whole CPU.
                "cpu": round(p.cpu_percent(interval=None) / threads, 1),
                "mem_mb": round((mem_info.rss if mem_info else 0) / 2**20),
            })
        except psutil.Error:
            continue
    top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:5]
    top_mem = sorted(procs, key=lambda x: x["mem_mb"], reverse=True)[:5]

    return {
        "ok": True,
        "name": os.environ.get("COMPUTERNAME", ""),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "cpu": _cpu["total"],
        "cpu_per_core": _cpu["per_core"],
        "cpu_cores": psutil.cpu_count(logical=False) or 0,
        "cpu_threads": psutil.cpu_count(logical=True) or 0,
        "cpu_mhz": round(freq.current) if freq else 0,
        "ram_used_gb": round((mem.total - mem.available) / 2**30, 1),
        "ram_total_gb": round(mem.total / 2**30, 1),
        "swap_used_gb": round(swap.used / 2**30, 1),
        "swap_total_gb": round(swap.total / 2**30, 1),
        "disks": disks,
        "gpu": gpu_stats(),
        "net_up_kbps": round(up_rate / 1024, 1),
        "net_down_kbps": round(down_rate / 1024, 1),
        "net_sent_gb": round(net.bytes_sent / 2**30, 2),
        "net_recv_gb": round(net.bytes_recv / 2**30, 2),
        "process_count": len(procs),
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "public_ip": _net_state["public_ip"],
        "public_ip_source": _net_state["public_ip_source"],
        "router": _net_state["gateway_name"],
        "wol_port": WOL_PORT,
        "wol_mapping": _net_state["wol_mapping"],
        "funnel": _funnel["state"],
        "funnel_url": _funnel["url"],
        "funnel_enable_link": _funnel["enable_link"],
    }


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PCRemote/3"

    def log_message(self, fmt, *args):
        # Behind Tailscale Funnel the peer is localhost; the real client is in X-Forwarded-For.
        who = self.headers.get("X-Forwarded-For") or self.client_address[0]
        logging.info("%s " + fmt, who, *args)

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not is_lan(self.client_address[0]):
            self._send(403, {"error": "forbidden"})
            return False
        token = self.headers.get("X-Token", "")
        if not hmac.compare_digest(token, TOKEN):
            time.sleep(0.5)  # slow down guessing
            self._send(401, {"error": "bad token"})
            return False
        return True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send_file(WEB_INDEX, "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self._authorized():
            return
        if path == "/status":
            try:
                self._send(200, status())
            except Exception as exc:  # noqa: BLE001
                logging.exception("status failed")
                self._send(500, {"error": str(exc)})
        elif path == "/stats":
            try:
                self._send(200, detailed_stats())
            except Exception as exc:  # noqa: BLE001
                logging.exception("stats failed")
                self._send(500, {"error": str(exc)})
        elif path == "/apps":
            self._send(200, {"apps": list(APPS.keys())})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/sleep":
            self._send(200, {"ok": True})
            # Let the response go out before the PC suspends.
            threading.Timer(1.0, go_to_sleep).start()
        elif path == "/restart":
            self._send(200, {"ok": True})
            restart_pc()
        elif path == "/close-all":
            self._send(200, {"ok": True, "closed": close_all()})
        elif path in ("/mic/on", "/mic/off", "/mic/toggle"):
            current = mic_muted()
            if current is None:
                self._send(500, {"error": "no microphone"})
                return
            want_muted = {"/mic/on": False, "/mic/off": True, "/mic/toggle": not current}[path]
            result = set_mic_muted(want_muted)
            self._send(200, {"ok": True, "mic_muted": result})
        elif path in ("/camera/on", "/camera/off", "/camera/toggle"):
            current = camera_enabled()
            if current is None:
                self._send(500, {"error": "camera setting not available"})
                return
            want = {"/camera/on": True, "/camera/off": False, "/camera/toggle": not current}[path]
            self._send(200, {"ok": True, "camera_enabled": set_camera_enabled(want)})
        elif path == "/notify-test":
            ok = notify("PC Remote", "Notifications work. You'll get one when the PC sits idle.", tags="white_check_mark")
            self._send(200 if ok else 500, {"ok": ok})
        elif path.startswith("/launch/"):
            name = urllib.parse.unquote(path[len("/launch/"):])
            app = APPS.get(name)
            if app is None:
                self._send(404, {"error": "unknown app"})
                return
            # An app entry has either "cmd" (one command) or "cmds" (several,
            # launched in order, e.g. a "Study Mode" that opens three programs).
            commands = app.get("cmds") or [app["cmd"]]
            try:
                for cmd in commands:
                    subprocess.Popen(
                        cmd,
                        cwd=app.get("cwd") or None,
                        creationflags=subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP,
                        close_fds=True,
                    )
                self._send(200, {"ok": True, "launched": name})
            except Exception as exc:  # noqa: BLE001
                logging.exception("launch %s failed", name)
                self._send(500, {"error": str(exc)})
        elif path.startswith("/close/"):
            name = urllib.parse.unquote(path[len("/close/"):])
            app = APPS.get(name)
            if app is None:
                self._send(404, {"error": "unknown app"})
                return
            count = close_app(app.get("process", []))
            self._send(200, {"ok": True, "closed": name, "processes": count})
        elif path.startswith("/close-window/"):
            try:
                hwnd = int(path[len("/close-window/"):])
            except ValueError:
                self._send(400, {"error": "bad window id"})
                return
            if end_window_task(hwnd):
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "window not found"})
        else:
            self._send(404, {"error": "not found"})


def main() -> None:
    threading.Thread(target=_cpu_sampler, daemon=True).start()
    threading.Thread(target=_maintenance_loop, daemon=True).start()
    threading.Thread(target=_afk_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("listening on port %d with %d apps", PORT, len(APPS))
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        logging.exception("agent crashed")
        raise
