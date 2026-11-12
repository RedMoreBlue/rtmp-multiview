# RTMP Multiview (developed with AI)

> This project was written with an AI coding assistant (Claude) and tested by a human on
> real playout workflows. Review it like any other code before relying on it.

Pulls up to 4 RTMP (or HLS/SRT) feeds, builds a labelled 2×2 grid at 1920×1080 and shows it
**live in any web browser**: a TV or monitor in fullscreen, a laptop, or a phone, on the LAN or
over the internet with a password. Optionally it also sends the grid to the network as **NDI**
or **OMT**, or shows it in a local window. Each tile shows:

- the channel title (editable live in the web UI)
- a tally: **LIVE** (green) / **NO SIGNAL** (red) / **CONNECTING** (amber) / **STANDBY**
  (blue, see power saving below) / **NOT SET**
- incoming codec, resolution and nominal fps, plus the frame rate *actually* being received
- peak meters for embedded audio channels 1–2 (or **NO AUDIO** if the stream has no audio track)
- when a feed is down: the reason (refused, not found, stream ended, stalled …), how long it
  has been down, and that it keeps retrying

A clock sits in the middle so a frozen picture is obvious. You can put a line of text under it
(e.g. `TALLINN LOCAL TIME`)

An empty channel is retried every 10 s (`reconnect_delay`) and keeps showing NO SIGNAL while
retrying, so it lights up on its own as soon as someone starts streaming to it. A feed that was
live and drops is retried after 2 s (`reconnect_delay_after_drop`). Every input runs in its own
ffmpeg process and thread, and each output has its own thread too. A bad feed or a hung
receiver can never freeze the other tiles or the output.

### Power saving (default on)

The usual case is that the channels have their stream keys configured but nobody is
streaming. The program handles that in three steps, and every channel always **lights up by
itself** when a stream starts. Nothing ever needs to be woken by hand.

| situation | what happens | CPU (measured, NDI on) |
|-----------|--------------|------------------------|
| no feed live | grid sent at `idle_fps` (10), empty channels checked every 10 s | ~12% of one core |
| no stream on a channel for 15 min (`standby_after`) | channel shows **STANDBY**, checked once a minute (`standby_retry`), lights up within about a minute of a stream starting | |
| every channel in standby or without URL | grid sent at `standby_fps` (5) | ~5–7% of one core |
| any feed live | full 50 fps | see below |

A "check" is one short ffmpeg connection attempt to the stream URL, which costs practically
nothing. With `"output": {"enabled": false}` and no window, it composes only for the web
preview and uses about 2%. Set `standby_after` to `0` to never go to standby.

## Web multiviewer

Open `http://<host>:8080/` and log in. The page is just the live multiview, scaled to the
window. Press **F** or the fullscreen button (or double-click) and the screen becomes a
multiviewer. A small toolbar in the corner fades out after 3 seconds and comes back when you
move the mouse or tap:

- the dot shows the connection (red banner + automatic reconnect if it drops)
- **Auto / HD / SD / Mobile** picks the stream quality (Auto chooses by screen size)
- fullscreen
- **⚙ settings**: channel titles, stream URLs (keys hidden until you press *Show*), the text
  under the clock, sign out. Only a tile whose URL changed reconnects.

**On a phone in portrait** you get the picture on top and a readable list under it: tally,
name, codec/resolution/fps and audio bars per channel. Turn the phone sideways for the full
picture. On iPhone use *Share → Add to Home Screen* for a fullscreen app-like view (iOS
doesn't allow the fullscreen button for web pages). The page keeps the screen awake while it's
open.

The picture is sent as an MJPEG stream (a series of JPEGs). It works in every browser, phones
included, with well under a second of delay. An encoder only runs while somebody watches that
quality, and it is shared by all viewers of that quality.

| quality | picture | measured bandwidth (1 live feed) | server CPU while watched |
|---------|---------|----------------------------------|--------------------------|
| HD | 1920×1080 @ 25 fps | ~15 Mbit/s | ~0.5 core |
| SD | 1280×720 @ 15 fps | ~5 Mbit/s | ~0.15 core |
| Mobile | 960×540 @ 10 fps | ~1–2 Mbit/s | ~0.06 core |

Use HD on the LAN and Mobile over mobile data. The profiles can be changed in
`web.stream_profiles` (`[width, height, fps, jpeg quality 2..31]`). With nobody watching and
NDI/OMT off, the server only composes 2 frames a second.

## Security and internet access

The web UI is designed to be safe to open to the internet **behind HTTPS**:

- **Login page with sessions.** The browser gets a random session cookie (HttpOnly,
  SameSite=Strict, Secure on HTTPS). You stay logged in for `session_hours` (default 7 days,
  extended on use). *Sign out* ends the session immediately, also for an open live stream.
- **Two roles (optional).** With only `password` set, everyone who logs in can watch and
  change settings. Also set `admin_password`, and plain logins can only watch. Opening the
  settings then asks for the admin password, which stays unlocked for `admin_minutes`
  (default 30). Give techs the viewing password and keep the admin one for whoever manages the
  stream keys.
- **Stream keys stay private.** Viewers never receive the URLs; only the settings panel (admin)
  does. Error messages on the picture and in the logs show only the server part of a URL
  (`rtmp://server/…`).
- **Brute-force protection.** After 5 wrong passwords from one address the login locks for
  30 s, doubling up to 15 min.
- **Hashed passwords.** `python3 multiview.py --hash-password` (or
  `docker run --rm -it ghcr.io/redmoreblue/rtmp-multiview --hash-password`) prints a hash to
  put in `web.password` / `MV_PASSWORD` instead of the plain text.
- Strict security headers (Content-Security-Policy, no framing, no referrer), CSRF protection
  on all changes, request size and time limits, and a limit on simultaneous live streams
  (`max_viewers`, default 20).

**HTTPS is required for internet access**, because the password would otherwise travel in
plain text. Either use the Docker setup with Caddy (automatic Let's Encrypt certificate, see
below), put it behind your own reverse proxy (nginx/Traefik), or set `web.tls_cert` /
`web.tls_key` to serve HTTPS directly. With a reverse proxy, list its address in
`web.trusted_proxies` (or `MV_TRUSTED_PROXIES`) so login lockouts see the real client
address, and turn off response buffering for `/stream.mjpg` (nginx: `proxy_buffering off;`,
which the app also requests with `X-Accel-Buffering: no`).

Without any password the UI is open to anyone who can reach the port. That's only acceptable
on a trusted LAN, and the log warns about it. The Docker image refuses to start without one.

| setting | env override | default |
|---------|--------------|---------|
| `web.password` | `MV_PASSWORD` | empty = no login |
| `web.admin_password` | `MV_ADMIN_PASSWORD` | empty = same as password |
| `web.session_hours` | | 168 |
| `web.admin_minutes` | | 30 |
| `web.trusted_proxies` | `MV_TRUSTED_PROXIES` (comma separated) | none |
| `web.tls_cert`, `web.tls_key` | | empty (plain HTTP) |
| `web.max_viewers` | | 20 |

## Docker

The image (`ghcr.io/redmoreblue/rtmp-multiview`) contains Python, ffmpeg and the app, runs as an
unprivileged user and keeps `config.json` in the `/config` volume. NDI/OMT aren't included in
the image; it's meant for the web multiviewer.

```bash
git clone https://github.com/RedMoreBlue/rtmp-multiview.git && cd rtmp-multiview
cp .env.example .env            # set MV_PASSWORD (and MV_ADMIN_PASSWORD, MV_DOMAIN)
mkdir -p config && sudo chown 10001:10001 config
docker compose up -d                       # LAN only: http://<host>:8080
docker compose --profile https up -d       # + Caddy: https://MV_DOMAIN with a real certificate
```

For the https profile, `MV_DOMAIN` must be a DNS name that points at the host, with ports 80
and 443 reachable from the internet (Let's Encrypt checks them). Set `MV_HTTP_BIND=127.0.0.1`
in `.env` so plain-HTTP port 8080 is only reachable through Caddy. Then open the channels in
the web UI (⚙). The config is saved to `./config/config.json`.

Without compose:

```bash
docker run -d --name multiview --restart unless-stopped -p 8080:8080 \
  -e MV_PASSWORD='your passphrase' -v "$PWD/config:/config" ghcr.io/redmoreblue/rtmp-multiview
```

Every push builds the image on GitHub and runs `ci/smoke-test.sh` against it before
publishing. The test sends a real RTMP stream through the container and checks the login,
live status, MJPEG stream and the security checks. Release tags publish `:latest` and the
version tag; `main` publishes `:main`.

## System requirements

Measured on an Intel i7-14700HX (2023) with 6 Mbit/s 1080p50 H.264 + AAC feeds:

| part | CPU at 50 fps |
|------|---------------|
| each live 1080p50 feed (decode + scale + audio meter) | ~0.4–0.6 core |
| compositor + NDI encode of the 1080p50 output | ~0.6 core |
| **4 live feeds + NDI out** | **~3.2 cores** |
| idle / standby (nothing live) | ~0.12 / ~0.06 core |

Older CPUs need roughly 1.5–2× that per feed (estimate). The usual case is one or two live
feeds at a time and long idle periods, so the box mostly sits near zero. Size it for the
busiest day you expect.

| deployment | minimum (1–2 live feeds) | recommended (4 live feeds at once) |
|------------|--------------------------|-----------------------------------|
| **Windows PC, 24/7, window + NDI/OMT** | 4-core CPU from ~2016 (Intel 6th gen i5 / Ryzen 1000), 8 GB RAM, gigabit Ethernet, Windows 10/11 | 4-core/8-thread from ~2018+ (Intel 8th gen i5 / Ryzen 3000), 8 GB RAM |
| **Headless Linux (Proxmox LXC/VM)** | 2 vCPU, 512 MB RAM, 2 GB disk | 4 vCPU, 1 GB RAM (+3 GB disk temporarily if you build OMT) |
| **Linux desktop, fullscreen on a TV** | 2-core/4-thread with Intel GPU decoding (`"hwaccel": "vaapi"`), 4 GB RAM | 4 cores, or `"fps": 25` on a dual-core |

Notes:
- All of them need **wired gigabit Ethernet**. A 1080p50 NDI/OMT output is about
  130–170 Mbit/s.
- Docker: see [Docker](#docker). Same CPU figures; budget 1 GB RAM.
- Web viewers add a little CPU on the server (see [Web multiviewer](#web-multiviewer)).
- Hardware decoding (`"hwaccel"`) mostly helps weak CPUs. On a modern CPU the scaling step
  costs as much as the decoding.

### Old Mac mini (2014) as the TV box

A 2014 Mac mini (dual-core Haswell i5/i7, Intel HD 5000/Iris 5100) running Debian with a
light desktop (Xfce) works as a "plug into the TV and forget it" box. These sizing numbers are
estimates; I haven't measured on that hardware.

1. Install Debian 12/13 with Xfce, plug in Ethernet (the Wi-Fi needs non-free Broadcom
   drivers, and you want cable anyway), and enable auto-login for the user.
2. `./install.sh --autostart --no-service` (add `--omt` or `--ndi` if it should also send the
   multiview to the network).
   It boots straight into the fullscreen multiview. The config is
   `~/.config/rtmp-multiview/config.json`.
3. In that config set `"hwaccel": "vaapi"` so the Intel GPU decodes the H.264, and
   `"output": {"enabled": false}` if nothing else needs the NDI/OMT feed (saves ~0.6 core).
4. Expected: idle and 1–2 live feeds at 50 fps are fine. For 4 live feeds at once on the
   1.4 GHz base model, set `"fps": 25`.

The window keeps the screensaver and screen blanking off while it's open (SDL default). If
the TV still goes black, turn off display power management in the Xfce settings.

## 1. Create the LXC (Proxmox)

- Debian 12 or 13 template, unprivileged is fine
- **Network: bridged** (e.g. `vmbr0`, same LAN/VLAN as the studio). NDI/OMT discovery uses
  mDNS and won't cross NAT or VLANs.
- 2–4 vCPU and 1 GB RAM (see System requirements above)

## 2. Install

Copy `multiview.py`, `install.sh`, `rtmp-multiview.service` and `config.example.json` into the
container (e.g. `scp` to `/root/rtmp-multiview/`), then:

```bash
cd /root/rtmp-multiview
chmod +x install.sh
./install.sh            # web multiviewer (no network output)
# optional network output, then set "output": {"enabled": true, ...}:
./install.sh --omt      # OMT (builds libomt from source, a few minutes)
./install.sh --ndi      # NDI (downloads the NDI 6 runtime), and set "type": "ndi"
```

Open `http://<container-ip>:8080/`, open ⚙ settings, enter the titles and URLs, press
**Save & apply**.
The config lives in `/etc/rtmp-multiview/config.json` with mode 600, because it holds the
stream keys. Running the installer again updates the program and keeps your config.

- Logs: `journalctl -u rtmp-multiview -f`
- Restart: `systemctl restart rtmp-multiview` (only needed after editing the config file by hand)

Set a password before anyone else can reach the page: `"web": {"password": "…"}` in the
config (see [Security and internet access](#security-and-internet-access)).

## 3. Stream URLs

Use the RTMP URL. It has lower latency (about 1–2 s) than HLS (about 10 s or more):

```
rtmp://<videolevels-server>/<app>/<streamkey>
```

The stream key works as a playback URL because Videolevels uses the same app/stream name for
publishing and playing. Treat these URLs like passwords: whoever has one can also push into
that channel. HLS (`https://…/playlist.m3u8`), SRT (`srt://host:port?streamid=…`), RTSP and
UDP URLs work too.

### Input buffer (`input_buffer_ms`, default 1000)

Each feed has a jitter buffer. Incoming frames are held for this long and then played out at
the source's own frame rate. RTMP over the internet often arrives in bursts, and without a
buffer the tile would stutter. With it, motion is smooth. The price is that the multiview
runs this much behind the live feed. The audio meters are delayed by the same amount, so they
stay in sync with the picture. For RTMP inputs the same value is also sent to the server as
the client buffer length.

- `0` shows each frame the moment it arrives (lowest latency, may stutter)
- raise it (e.g. `2000`) on a bad link where the web UI shows "underruns"
- per input: add `"buffer_ms": 500` to that input's entry in `inputs`

The web UI shows each live feed's buffer fill (e.g. `buffer 1.0/1.0s`) and any underruns.
Memory: 1 s of buffer is about 45 MB per live 1080p50 feed.

## 4. Ways to run it

| where | how | what you get |
|-------|-----|--------------|
| Docker (company server) | `docker compose up -d` | web multiviewer (+ HTTPS with the Caddy profile) |
| Headless Linux (LXC, server) | `./install.sh` (systemd service; add `--omt`/`--ndi` for network output) | web multiviewer (+ NDI/OMT if enabled) |
| Linux with a desktop | `./install.sh --desktop` → "RTMP Multiview" in the app menu, or `rtmp-multiview-gui` | fullscreen window + web UI |
| Windows | double-click `rtmp-multiview.exe` | window + web UI (+ NDI/OMT if enabled) |

The window uses **ffplay** (part of ffmpeg). Keys: `f` or a double-click toggles fullscreen,
`q`/`Esc` quits, and closing the window stops the program. Command-line flags override the
config:

```bash
rtmp-multiview [config.json] --display        # force a window
rtmp-multiview [config.json] --fullscreen     # fullscreen window
rtmp-multiview [config.json] --headless       # never a window
rtmp-multiview [config.json] --no-output      # no NDI/OMT, just window / web UI
```

`display.enabled` is `"auto"` by default: a window on Windows and on Linux desktops, none
under systemd or without a desktop. Use `true`/`false` to force it.

On a Linux desktop the config is `~/.config/rtmp-multiview/config.json` (created on first
start). If the background service also runs on that machine, it already has port 8080, so the
desktop app runs without its own web page. Use `./install.sh --desktop --no-service` if that
machine should only run the window.

## 5. Network output (`output` in config.json)

Off by default. Set `"enabled": true` to send the multiview as NDI or OMT as well. With it
off, nothing is sent and the program only composes what the web viewers and the window need.

`output.type` (when enabled):

| type         | what it does |
|--------------|--------------|
| `omt`        | **default type.** OMT source named `output.name`, sent from inside the program via `libomt` (needs `install.sh --omt`, or `libomt.dll` on Windows) |
| `ndi`        | NDI source, sent from inside the program (needs `install.sh --ndi`, or the NDI Runtime/Tools on Windows) |
| `ndi-gst`    | old method: pipe into `gst-launch-1.0 … ndisink` (gst-plugins-rs, not installed by default) |
| `omt-ffmpeg` | pipe into an OMT-patched ffmpeg (`-f libomt`, path in `output.omt_ffmpeg`) |
| `custom`     | pipe raw UYVY frames into any command in `output.command` |
| `null`       | no network output, web preview only (handy for testing) |

The web UI status line shows whether the output is running and how many receivers are
connected.

### OMT

OMT (Open Media Transport) is open source (MIT) and vMix receives it natively. No prebuilt
Linux library exists, so `./install.sh --omt` builds `libvmx` + `libomt` from
github.com/openmediatransport with .NET 8 (installed to `/opt/dotnet`). On x86 this needs a
CPU with AVX2. Then set `"type": "omt"` and restart. `omt_quality` can be `default`, `low`,
`medium` or `high`.

## 6. Watching it

- **Mac:** NDI Video Monitor (free, part of NDI Tools for macOS)
- **vMix / OBS / TriCaster:** add it as an NDI (or OMT) input
- **Raspberry Pi:** an NDI player (e.g. Dicaffeine). A Pi 4 struggles to decode full NDI
  1080p50. If it stutters, set `"fps": 25` in the config, which halves the load on the viewer.
- Bandwidth: 1080p50 NDI uses roughly 130–170 Mbit/s, which is fine on gigabit.
- If the source doesn't show up, check that `avahi-daemon` is running in the container and
  that the viewer is on the same subnet/VLAN. On networks where mDNS is blocked, use an NDI
  Discovery Server.

- **Alternative:** the TV machine can run the multiview itself in fullscreen (section 4), with
  no NDI/OMT receiver needed.

## 7. Windows version

`rtmp-multiview.exe` is the same program as a single Windows file (build it with
`build_windows.ps1`, which needs Python on PATH). When you start it, it opens the multiview in a
window. The console window next to it shows the log, and closing either one stops the program.

1. Put `rtmp-multiview.exe` in a folder with `ffmpeg.exe` and `ffplay.exe` (e.g. from the
   gyan.dev "essentials" build), or have both on PATH.
2. For the default OMT output, put `libomt.dll` + `libvmx.dll` (from the
   openmediatransport/libomtnet releases) next to the exe. For NDI instead, install **NDI
   Tools** or the **NDI 6 Runtime** and set `"type": "ndi"`; the exe finds the runtime through
   `NDI_RUNTIME_DIR_V6`. Without either, the window and web UI still work, and the web UI shows
   the output as not running.
3. Double-click the exe. On first start it creates `config.json` next to itself and serves
   the web UI on `http://localhost:8080/`. Allow it through the Windows firewall when asked,
   so other machines can open the web page (and see the NDI/OMT source if you enable it).
4. Closing the console window stops it.

## Tuning

- `stall_timeout` (s): no frames for this long → NO SIGNAL + reconnect
- `connect_timeout` (s): give up a connection attempt after this long
- `reconnect_delay` (s, default 10): pause between retries of an empty channel
- `reconnect_delay_after_drop` (s, default 2): first retry after a live feed drops
- `idle_fps` (default 10): frame rate while nothing is live; `0` = always full rate
- `standby_after` (s, default 900 = 15 min), `standby_retry` (s, default 60), `standby_fps`
  (default 5): power saving, see above
- `hwaccel`: `""` (software), `"auto"`, `"vaapi"` (Intel GPU on Linux), `"d3d11va"` (Windows)
- `decoder_threads` (default 2): threads per input. `0` = ffmpeg's default of one per CPU
  core, which measured 3–4× more CPU for the same result.
- `input_args`: extra ffmpeg input args for every feed, e.g. `["-rw_timeout", "10000000"]`
- per-input `"args"` work the same for a single feed
- `clock`: `false` hides the clock; `clock_caption`: text under it (also editable in the web UI)
- `audio_meters`: `false` disables audio metering (saves a little CPU)
