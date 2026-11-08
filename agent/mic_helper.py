"""Microphone helper process for the PC Remote agent.

All Windows COM / pycaw work lives here, in its own process, so a COM crash
can never take the agent down. Protocol: one command per line on stdin
("get", "mute", "unmute"), one JSON line per answer on stdout:
    {"ok": true, "muted": true|false, "count": <mics>}
"muted" is true only when every active recording device is muted.
"""

import gc
import json
import sys
import time
from ctypes import POINTER, cast

from comtypes import CLSCTX_ALL, CoInitialize
from pycaw.constants import DEVICE_STATE, EDataFlow
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


def all_mics():
    vols = []
    for dev in AudioUtilities.GetAllDevices(data_flow=EDataFlow.eCapture.value,
                                            device_state=DEVICE_STATE.ACTIVE.value):
        try:
            vols.append(cast(dev._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                             POINTER(IAudioEndpointVolume)))
        except Exception:  # noqa: BLE001
            continue
    return vols


def main() -> None:
    CoInitialize()
    cached = {"t": 0.0, "vols": []}
    for raw in sys.stdin:
        cmd = raw.strip().lower()
        if not cmd:
            continue
        try:
            if time.time() - cached["t"] > 60 or not cached["vols"]:
                cached["vols"] = []
                gc.collect()
                cached["vols"] = all_mics()
                cached["t"] = time.time()
                gc.collect()
            vols = cached["vols"]
            if cmd in ("mute", "unmute"):
                for v in vols:
                    v.SetMute(1 if cmd == "mute" else 0, None)
            muted = all(bool(v.GetMute()) for v in vols) if vols else None
            out = {"ok": True, "muted": muted, "count": len(vols)}
        except Exception as exc:  # noqa: BLE001
            cached["vols"] = []
            out = {"ok": False, "error": str(exc)}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
