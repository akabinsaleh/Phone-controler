# Phone Controller (PC Remote)

Control a Windows PC from an iPhone, at home or from anywhere, with no VPN on the phone.

- **iOS app** (`ios/`): SwiftUI, built and installed with [xtool](https://github.com/xtool-org/xtool) from Linux/WSL, no Mac needed.
- **PC agent** (`agent/`): a small Python HTTP server on the PC. Also serves a web version of the app at `/` for any phone browser.

## Features

| | |
|---|---|
| Status | online/offline, CPU, RAM, uptime, idle time |
| Stats page | per-core CPU, GPU (NVIDIA), disks, network rates, top processes |
| Power | Wake-on-LAN (Turn On), Sleep, Restart, Close all apps |
| Apps | launch configured apps or "modes" (several apps at once), Running badge, End task |
| Windows | list of open windows, End task per window |
| Devices | microphone mute (all recording devices), camera on/off (device-level, via admin tasks) |
| Notifications | via [ntfy](https://ntfy.sh): PC idle, PC booted/woke, agent restarted |
| Remote access | Tailscale **Funnel** on the PC publishes the agent over HTTPS; the phone needs nothing |
| Wake from the internet | the agent keeps a UDP port forwarded on the router (UPnP) and reports the public IP; the app sends the magic packet there |

Every API call needs a shared token (`X-Token` header).

## PC setup (Windows)

1. Install Python 3.12+ and `pip install psutil pycaw comtypes certifi` (into the main site-packages, not `--user`, so scheduled tasks see them).
2. Copy `agent/` to e.g. `C:\PCRemote\`, copy `config.example.json` → `config.json`, set a long random `token`, your apps, and an ntfy topic.
3. Allow inbound TCP 8765 in Windows Firewall (home subnet + Tailscale range `100.64.0.0/10`).
4. Run `pythonw supervisor.py` (restarts the agent on crash). For auto-start create a scheduled task at logon that runs `start-agent.vbs`, and optionally a 5-minute watchdog task that runs it again (a second supervisor exits by itself).
5. Optional, remote access: install Tailscale on the PC and enable Funnel for the tailnet; the agent runs `tailscale funnel 8765` itself.
6. Optional, camera button: create two admin tasks `PCRemote Camera Off` / `PCRemote Camera On` running `pnputil /disable-device /class Camera` and `/enable-device /class Camera`.

## iOS app

1. Install xtool (Linux or WSL), run `xtool setup` with your Apple ID.
2. `cd ios`, copy `Sources/PCRemote/Secrets.example.swift` → `Secrets.swift` with your token.
3. Edit the defaults in `Sources/PCRemote/Settings.swift` (PC LAN IP, MAC, broadcast, Funnel URL, wake port) or change them later in the app's Settings screen.
4. Phone plugged in and unlocked: `xtool dev`. With a free Apple ID the app must be reinstalled every 7 days.

## Agent API (all need `X-Token`)

```
GET  /status            GET  /stats            GET  /apps
POST /sleep             POST /restart          POST /close-all
POST /launch/<app>      POST /close/<app>      POST /close-window/<hwnd>
POST /mic/on|off|toggle POST /camera/on|off|toggle
POST /notify-test
GET  /                  web UI (no token for the HTML; the page asks for it)
```

## Notes

- Use Sleep, not Shut down, if you want wake-over-internet: from sleep the NIC keeps answering the router (ARP offload); from S5 it doesn't.
- The agent only launches apps listed in `config.json`; `Close all` never closes the desktop, the agent, or processes in `PROTECTED_PROCESSES`.
