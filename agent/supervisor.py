"""Keeps the PC Remote agent running.

Starts agent.py and restarts it whenever it exits (crash, error), with a short
back-off. Only one supervisor runs at a time (named mutex), so the scheduled
tasks can launch this freely: a second copy just exits.

Launched by start-agent.vbs (hidden) from the "PCRemote Agent" task at logon
and the "PCRemote Watchdog" task every 5 minutes.
"""

import ctypes
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "startup.log"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
ERROR_ALREADY_EXISTS = 183


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%m/%d/%Y %I:%M:%S %p')} supervisor: {msg}\n")


def notify(title: str, message: str) -> None:
    """Push to the phone through ntfy (same topic the agent uses). Best effort."""
    try:
        afk = json.loads((BASE / "config.json").read_text(encoding="utf-8")).get("afk", {})
        topic = afk.get("ntfy_topic", "")
        if not topic:
            return
        req = urllib.request.Request(
            f"{afk.get('ntfy_url', 'https://ntfy.sh')}/{topic}", data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "warning", "Priority": "high"},
        )
        try:
            import certifi
            import ssl
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            ctx = None
        urllib.request.urlopen(req, timeout=10, context=ctx).read()
    except Exception as exc:  # noqa: BLE001
        log(f"notify failed: {exc}")


def main() -> None:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\PCRemoteSupervisor")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return  # another supervisor is already running
    log("started")
    delay = 3
    while True:
        started = time.time()
        with open(LOG, "a", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [str(PYTHONW), str(BASE / "agent.py")], cwd=str(BASE),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            code = proc.wait()
        ran = time.time() - started
        log(f"agent exited with {code} after {int(ran)} s; restarting in {delay} s")
        notify("PC Remote agent restarted",
               f"The agent on the PC stopped (exit code {code}) after running {int(ran)} s. "
               f"Restarting it in {delay} s. Details in startup.log.")
        time.sleep(delay)
        # Back off if it keeps dying immediately; reset once it ran a while.
        delay = 3 if ran > 120 else min(delay * 2, 60)


if __name__ == "__main__":
    main()
