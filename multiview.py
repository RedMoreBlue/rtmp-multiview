#!/usr/bin/env python3
"""
RTMP Multiview
--------------
Pulls up to 4 RTMP/HLS/SRT feeds with ffmpeg, composites a labelled 2x2 grid
(default 1920x1080 @ 50 fps) and sends it to the network as NDI or OMT.

Each tile shows the channel name, a LIVE / NO SIGNAL / CONNECTING / NOT SET
tally, the incoming resolution, codec and the frame rate actually being
received, audio peak meters for embedded channels 1-2 and, when a feed is down,
the reason. A clock in the middle makes a frozen output obvious.

A small web page (default port 8080) shows a live preview and lets you change
the channel names / URLs without restarting anything.

Usage:  multiview.py [/path/to/config.json]
"""
import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("multiview")

IS_WIN = os.name == "nt"
# folder of the .exe when frozen with PyInstaller, else of this script
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
DEFAULT_CONFIG_PATH = (os.path.join(APP_DIR, "config.json") if IS_WIN
                       else "/etc/rtmp-multiview/config.json")
FONT_FALLBACKS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]

DEFAULT_CONFIG = {
    "output": {
        # ndi        - in-process NDI sender (libndi.so.6, install.sh --ndi)
        # omt        - in-process OMT sender (libomt.so, install.sh --omt)
        # ndi-gst    - gst-launch + ndisink from gst-plugins-rs
        # omt-ffmpeg - OMT-patched ffmpeg with -f libomt
        # custom     - pipe raw UYVY into output.command
        # null       - no network output (web preview only)
        "enabled": False,              # true = send the multiview to the network as NDI/OMT
        "type": "omt",
        "name": "RTMP Multiview",      # NDI / OMT source name
        "width": 1920,
        "height": 1080,
        "fps": 50,
        "ndi_lib": "",                 # path to libndi.so.6 (empty = search)
        "ndi_groups": "",              # NDI groups, empty = public
        "omt_lib": "",                 # path to libomt.so (empty = search)
        "omt_quality": "default",      # default | low | medium | high
        "omt_ffmpeg": "/opt/ffmpeg-omt/bin/ffmpeg",
        "command": None,               # for type=custom: list, placeholders {width} {height} {fps} {name}
    },
    # Local window showing the multiview (uses ffplay). "auto" = on for Windows and for Linux
    # desktops (DISPLAY/WAYLAND_DISPLAY set), off when headless / under systemd.
    "display": {"enabled": "auto", "fullscreen": False, "width": 1280, "height": 720,
                "exit_on_close": True},
    "web": {
        "bind": "0.0.0.0", "port": 8080,
        # Login. Plain text or a hash from `multiview.py --hash-password`. Env MV_PASSWORD /
        # MV_ADMIN_PASSWORD override these. Empty password = no login (only for a trusted LAN).
        "password": "",
        "admin_password": "",          # optional: needed to open the settings (else same as password)
        "session_hours": 168,          # stay logged in this long (sliding)
        "admin_minutes": 30,           # settings access after entering the admin password
        "trusted_proxies": [],         # reverse proxy IPs/hostnames whose X-Forwarded-* to trust
        "tls_cert": "", "tls_key": "", # serve HTTPS directly (else use a reverse proxy)
        "max_viewers": 20,             # simultaneous live streams
        # live stream profiles: [width, height, fps, jpeg quality 2 (best) .. 31]
        "stream_profiles": {"hd": [1920, 1080, 25, 5], "sd": [1280, 720, 15, 6],
                            "mobile": [960, 540, 10, 7]},
    },
    "ffmpeg": "ffmpeg",
    "ffplay": "ffplay",
    "input_args": [],                  # extra ffmpeg args before -i for every input (e.g. hwaccel)
    "connect_timeout": 15.0,           # seconds to wait for the first frame
    "stall_timeout": 4.0,              # no frames for this long -> NO SIGNAL + reconnect
    "reconnect_delay": 10.0,           # retry interval for a channel with no stream
    "reconnect_delay_after_drop": 2.0, # first retry after a LIVE feed drops (quick recovery)
    "idle_fps": 10,                    # frame rate while no feed is live (saves CPU/bandwidth); 0 = full rate
    # Power saving: a channel with no stream for standby_after seconds goes to STANDBY and is
    # then only checked every standby_retry seconds. It still wakes up by itself as soon as a
    # stream is pushed. When every channel is in standby (or has no URL) the output runs at
    # standby_fps.
    "standby_after": 900,              # 15 min; 0 = never go to standby
    "standby_retry": 60,               # seconds between checks in standby (min 5)
    "standby_fps": 5,
    "hwaccel": "",                     # "" = software decode; "auto", "vaapi" (Intel iGPU on Linux), "d3d11va" ...
    "decoder_threads": 2,              # threads per input (0 = ffmpeg default: one per core, much more CPU)
    "clock": True,
    "clock_caption": "",               # text under the clock, e.g. "TALLINN LOCAL TIME"
    "audio_meters": True,
    # Jitter buffer per input: frames are held this long and played out at the source's own
    # frame rate, so bursty RTMP delivery looks smooth. Adds this much delay. 0 = show frames
    # as soon as they arrive (lowest latency). Per input: "buffer_ms" in that input's entry.
    "input_buffer_ms": 1000,
    "font": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "inputs": [
        {"name": "Channel 1", "url": ""},
        {"name": "Channel 2", "url": ""},
        {"name": "Channel 3", "url": ""},
        {"name": "Channel 4", "url": ""},
    ],
}

ALLOWED_SCHEMES = ("rtmp://", "rtmps://", "rtmpt://", "rtmpe://", "srt://", "http://",
                   "https://", "rtsp://", "rtsps://", "udp://", "rtp://", "tcp://")

# ----------------------------------------------------------------------------
# Colour helpers (BT.709 limited range, packed UYVY 4:2:2)
# ----------------------------------------------------------------------------

def rgb_to_uyvy(rgb: np.ndarray) -> np.ndarray:
    """(h, w, 3) uint8 RGB, w even -> (h, w*2) uint8 UYVY."""
    f = rgb.astype(np.float32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    cb = (b - y) / 1.8556
    cr = (r - y) / 1.5748
    Y = 16 + y * (219 / 255)
    U = 128 + cb * (224 / 255)
    V = 128 + cr * (224 / 255)
    U = (U[:, 0::2] + U[:, 1::2]) / 2
    V = (V[:, 0::2] + V[:, 1::2]) / 2
    h, w = y.shape
    out = np.empty((h, w // 2, 4), np.float32)
    out[..., 0] = U
    out[..., 1] = Y[:, 0::2]
    out[..., 2] = V
    out[..., 3] = Y[:, 1::2]
    return np.clip(out + 0.5, 0, 255).astype(np.uint8).reshape(h, w * 2)


def uyvy_to_rgb_half(frame: np.ndarray) -> np.ndarray:
    """(h, w*2) UYVY -> (h/2, w/2, 3) RGB preview (one pixel per UYVY pair)."""
    p = frame[0::2].reshape(frame.shape[0] // 2, -1, 4).astype(np.float32)
    U, Y0, V, Y1 = p[..., 0], p[..., 1], p[..., 2], p[..., 3]
    y = ((Y0 + Y1) / 2 - 16) * (255 / 219)
    cb = (U - 128) * (255 / 224)
    cr = (V - 128) * (255 / 224)
    r = y + 1.5748 * cr
    g = y - 0.1873 * cb - 0.4681 * cr
    b = y + 1.8556 * cb
    return np.clip(np.stack([r, g, b], -1) + 0.5, 0, 255).astype(np.uint8)


def even(n):
    return int(n) // 2 * 2


def fmt_dur(s):
    s = int(s)
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h {m:02d}m" if h else (f"{m}m {x:02d}s" if m else f"{x}s")


# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------

class Layout:
    def __init__(self, width, height):
        self.W, self.H = even(width), even(height)
        self.tile_w, self.tile_h = even(self.W // 2), even(self.H // 2)
        self.label_h = max(24, even(self.tile_h * 0.075))
        self.gap = 4
        self.vid_h = even(self.tile_h - self.label_h - self.gap)
        self.vid_w = min(even(self.vid_h * 16 / 9), self.tile_w - 2 * self.gap)
        self.vid_w = even(self.vid_w)
        # audio meter strip, overlaid on the right edge of each video area
        self.meter_w = max(32, even(self.vid_h * 0.1))
        self.meter_h = even(self.vid_h - 2 * even(self.vid_h * 0.03))
        # (x, y) of each tile's video area, label bar and meter.
        # Top row: label under the video. Bottom row: label above the video. All four label
        # bars meet in a band across the middle, where the clock box sits.
        self.video_pos, self.label_pos, self.meter_pos = [], [], []
        for i in range(4):
            tx, ty = (i % 2) * self.tile_w, (i // 2) * self.tile_h
            vx = even(tx + (self.tile_w - self.vid_w) // 2)
            if i < 2:
                vy = ty + self.gap // 2
                ly = vy + self.vid_h + self.gap // 2
            else:
                ly = ty
                vy = ty + self.label_h + self.gap // 2
            self.video_pos.append((vx, vy))
            self.label_pos.append((tx, ly))
        self.band_y = self.label_pos[0][1]             # centre band: 2 label rows high
        self.band_h = self.label_pos[2][1] + self.label_h - self.band_y
        for i in range(4):
            vx, vy = self.video_pos[i]
            m = even(self.vid_h * 0.03)
            self.meter_pos.append((even(vx + self.vid_w - self.meter_w - m), vy + m))

    def frame_bytes(self):
        return self.W * self.H * 2

    def video_bytes(self):
        return self.vid_w * self.vid_h * 2


def blit(frame, tile, x, y):
    """Copy a UYVY tile (h, w*2) into frame at pixel x (even), y."""
    h, w2 = tile.shape
    frame[y:y + h, 2 * x:2 * x + w2] = tile


# ----------------------------------------------------------------------------
# Input puller
# ----------------------------------------------------------------------------

STATE_UNSET, STATE_CONNECTING, STATE_LIVE, STATE_OFFLINE = "unset", "connecting", "live", "offline"
STATE_STANDBY = "standby"

VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})")
AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+).*?, (\d+) Hz, ([^,]+)")
FPS_RE = re.compile(r"([\d.]+) fps")
LEVEL_RE = re.compile(r"lavfi\.astats\.([12])\.Peak_level=(\S+)")

# (substring in ffmpeg stderr, lower case) -> human readable reason. First match wins.
REASONS = [
    ("connection refused", "Connection refused (server down or wrong port)"),
    ("error number -138", "Connection refused (server down or wrong port)"),   # Windows builds
    ("name or service not known", "Host not found (DNS)"),
    ("temporary failure in name resolution", "Host not found (DNS)"),
    ("failed to resolve", "Host not found (DNS)"),
    ("no route to host", "No route to host"),
    ("network is unreachable", "Network unreachable"),
    ("streamnotfound", "Stream not found on server"),
    ("stream not found", "Stream not found on server"),
    ("404", "Not found (404)"),
    ("401", "Access denied (401)"),
    ("403", "Access denied (403)"),
    ("permission denied", "Access denied"),
    ("unauthorized", "Access denied"),
    ("server error", None),                 # use the server's own text
    ("connection reset", "Connection reset by server"),
    ("connection timed out", "Connection timed out"),
    ("input/output error", "Server closed the connection (not live?)"),
    ("end of file", "Stream ended"),
    ("invalid data found", "Invalid stream data"),
    ("protocol not found", "Unsupported URL type"),
    ("no such file", "Bad URL"),
    ("does not contain any stream", "No video in stream"),
    ("could not find codec parameters", "Could not detect video format"),
]
ERROR_HINTS = ("error", "failed", "refused", "not found", "timed out", "invalid", "denied",
               "no such", "unauthorized", "unreachable", "reset", "end of file", "40")


URL_PATH_RE = re.compile(r"((?:rtmps?|rtmpt|rtmpe|srt|https?|rtsps?|udp|rtp|tcp)://[^/\s?]+)[^\s'\"]*", re.I)


def redact(text, url=""):
    """Hide stream keys: drop everything after the host part of any URL in the text."""
    if not text:
        return text
    if url:
        text = text.replace(url, URL_PATH_RE.sub(r"\1/…", url))
    return URL_PATH_RE.sub(r"\1/…", text)


def classify(lines):
    """Pick the most meaningful reason from recent ffmpeg stderr lines."""
    for needle, text in REASONS:
        for line in reversed(lines):
            if needle in line.lower():
                if text is None:
                    m = re.search(r"server error:?\s*(.*)", line, re.I)
                    return ("Server error: " + (m.group(1) if m else line))[:120]
                return text
    return None


class Input:
    def __init__(self, idx, cfg, app):
        self.idx = idx
        self.app = app
        self.name = cfg.get("name") or f"Channel {idx + 1}"
        self.url = (cfg.get("url") or "").strip()
        self.args = list(cfg.get("args") or [])
        self.state = STATE_UNSET if not self.url else STATE_CONNECTING
        self.cfg_in = cfg
        self.frame = None            # UYVY tile currently shown (np array)
        self.q = deque()             # jitter buffer of received tiles
        self.src_fps = 0.0           # nominal source frame rate from the stream info
        self._prebuffering = True
        self._acc = 0.5
        self._depth = 0.0            # smoothed buffer depth (frames)
        self._arr = deque(maxlen=400) # arrival times, for the real frame rate
        self._pb_t = None            # arrival time of the first frame while (re)buffering
        self.delay_s = 0.0           # current buffer depth in seconds
        self.underruns = 0
        self._lv = deque(maxlen=600) # (time, channel, dBFS) for delayed audio meters
        self.last_frame_t = 0.0
        self.started_t = 0.0
        self.live_since = 0.0
        self.down_since = 0.0
        self.failed = False          # had a failure since last LIVE / URL change -> stay NO SIGNAL
        self.standby = False         # no stream for standby_after seconds -> slow checks
        self.info = ""               # e.g. "h264 1920x1080 50fps"
        self.audio_info = ""         # e.g. "aac 48k stereo"
        self.has_audio = None        # None = unknown yet
        self.peak = [-math.inf, -math.inf]
        self.audio_t = 0.0
        self.last_error = ""
        self.measured_fps = 0.0
        self.attempts = 0
        self._count = 0
        self._count_t = time.monotonic()
        self._got_frame = False
        self._kill_reason = None
        self._lines = deque(maxlen=30)
        self.proc = None
        self.wake = threading.Event()
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name=f"input{idx + 1}", daemon=True)
        self.watch = threading.Thread(target=self._watchdog, name=f"watch{idx + 1}", daemon=True)

    # -- public ------------------------------------------------------------
    def start(self):
        self.thread.start()
        self.watch.start()

    def update(self, name, url):
        url = (url or "").strip()
        self.name = name or f"Channel {self.idx + 1}"
        if url != self.url:
            log.info("[%d] URL changed", self.idx + 1)
            self.url = url
            self._reset_buffer()
            self.failed = False
            self.standby = False
            self.down_since = 0.0
            self.last_error = ""
            self.state = STATE_UNSET if not url else STATE_CONNECTING
            self._kill()
            self.wake.set()

    def stop(self):
        self.stopping = True
        self._kill()
        self.wake.set()

    def buffer_s(self):
        v = self.cfg_in.get("buffer_ms", self.app.cfg.get("input_buffer_ms", 1000))
        try:
            return max(0.0, min(10.0, float(v or 0) / 1000.0))
        except (TypeError, ValueError):
            return 1.0

    def _reset_buffer(self):
        self.q.clear()
        self.frame = None
        self._prebuffering = True
        self._acc = 0.5
        self._depth = 0.0
        self._arr.clear()
        self._pb_t = None
        self.delay_s = 0.0
        self._lv.clear()

    def _push(self, tile, t):
        """A decoded tile arrived from ffmpeg at time t."""
        self._arr.append(t)
        if self._prebuffering and self._pb_t is None:
            self._pb_t = t
        if self.buffer_s() > 0:
            self.q.append(tile)
            if len(self.q) > 600:               # safety cap (~12 s at 50 fps)
                self.q.popleft()
        else:
            self.frame = tile

    def play_fps(self):
        """Rate to play the buffer out at: the stream's nominal fps, unless the frames really
        arrive at a clearly different rate (measured over the last ~5 s)."""
        nominal = self.src_fps
        a = self._arr
        est = 0.0
        if len(a) >= 10:
            i = 0
            while a[-1] - a[i] > 5.0 and i < len(a) - 10:
                i += 1
            span = a[-1] - a[i]
            if span >= 1.0:
                est = (len(a) - 1 - i) / span
        if nominal and (not est or abs(est - nominal) / nominal < 0.1):
            return nominal
        return est or nominal or 25.0

    def playout(self, dt):
        """Called by the compositor every output frame: returns the tile to show now.

        Frames leave the buffer at the source frame rate, nudged by up to +-5% to keep the
        buffer near its target depth (absorbs clock drift without visible jumps). If the
        buffer runs dry the last picture is held until it has refilled."""
        b = self.buffer_s()
        if b <= 0:
            self.delay_s = 0.0
            return self.frame
        fps = self.play_fps()
        target = max(1, int(round(b * fps)))
        q = self.q
        if self._prebuffering:
            # Start only after the buffer time has really passed since the first frame came
            # in: right after connecting ffmpeg delivers a backlog burst, which fills the
            # queue instantly but gives no real cushion. Then drop that backlog so the delay
            # is exactly the configured one.
            need = target
            waited = (self._arr[-1] - self._pb_t) if (self._arr and self._pb_t is not None) else 0.0
            if len(q) < need or waited < need / fps:
                return self.frame
            while len(q) > need:
                q.popleft()
            self._prebuffering, self._acc, self._depth = False, 0.5, float(len(q))
        if len(q) > 2 * target + 2:                 # far behind (e.g. a big burst): catch up
            while len(q) > target:
                q.popleft()
            self._depth = float(len(q))
        # Speed correction from the depth averaged over ~3 s, so the sawtooth of bursty
        # delivery doesn't wobble the rate (which would show as freeze+skip pairs).
        self._depth += (len(q) - self._depth) * min(1.0, dt / 3.0)
        adj = max(-0.05, min(0.05, 0.05 * (self._depth - target) / target))
        self._acc += dt * fps * (1.0 + adj)
        n = int(self._acc)
        self._acc -= n
        for _ in range(n):
            if not q:
                self._prebuffering = True
                self._pb_t = None
                self.underruns += 1
                break
            self.frame = q.popleft()
        self.delay_s = len(q) / fps
        return self.frame

    def levels(self):
        """Peak dBFS per channel matching the picture currently shown; -inf when silent/stale."""
        t = time.monotonic() - self.delay_s
        found = [None, None]                    # (timestamp, dB) per channel
        for ts, ch, v in reversed(self._lv):
            if ts <= t and found[ch] is None:
                found[ch] = (ts, v)
                if found[0] and found[1]:
                    break
        return [f[1] if f and t - f[0] < 0.6 else -math.inf for f in found]

    def status(self):
        now = time.monotonic()
        lv = self.levels() if self.state == STATE_LIVE else [-math.inf, -math.inf]
        return {
            "index": self.idx + 1,
            "name": self.name,
            "has_url": bool(self.url),
            "state": self.state,
            "info": self.info,
            "audio": self.audio_info if self.has_audio else ("no audio" if self.has_audio is False else ""),
            "has_audio": self.has_audio,
            "levels": [round(x, 1) if x > -100 else None for x in lv],
            "fps": round(self.measured_fps, 1),
            "buffer_ms": int(self.buffer_s() * 1000),
            "buffered_ms": int(self.delay_s * 1000),
            "underruns": self.underruns,
            "live_for": int(now - self.live_since) if self.state == STATE_LIVE else 0,
            "down_for": int(now - self.down_since)
            if self.state in (STATE_OFFLINE, STATE_STANDBY) and self.down_since else 0,
            "error": self.last_error,
            "attempts": self.attempts,
        }

    # -- internals ---------------------------------------------------------
    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def _cmd(self):
        L, cfg = self.app.layout, self.app.cfg
        vf = (f"scale={L.vid_w}:{L.vid_h}:force_original_aspect_ratio=decrease:flags=bilinear,"
              f"format=yuv420p,pad={L.vid_w}:{L.vid_h}:(ow-iw)/2:(oh-ih)/2,format=uyvy422")
        hw = str(cfg.get("hwaccel") or "").strip()
        # Few threads per input: ffmpeg's default (one per CPU core, for decoder AND filters)
        # costs 3-4x more CPU in thread overhead than it saves for a 1080p50 tile.
        th = int(cfg.get("decoder_threads") or 0)
        cmd = ([cfg["ffmpeg"], "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
                "-fflags", "+discardcorrupt"]
               + (["-filter_threads", "1", "-threads", str(th)] if th > 0 else [])
               + (["-hwaccel", hw] if hw else [])
               + (["-rtmp_buffer", str(int(self.buffer_s() * 1000))]
                  if self.url.lower().startswith("rtmp") and self.buffer_s() > 0 else [])
               + list(cfg.get("input_args") or []) + self.args
               + ["-i", self.url,
                  # output 1: scaled video as raw UYVY on stdout
                  "-map", "0:v:0", "-an", "-sn", "-dn",
                  "-vf", vf, "-f", "rawvideo", "-pix_fmt", "uyvy422", "pipe:1"])
        if cfg.get("audio_meters", True):
            # output 2: peak levels of embedded channels 1-2, printed to stderr.
            # The video is stream-copied into the null muxer as well, so this output always
            # has a stream even when the source has no audio (ffmpeg would abort otherwise).
            af = ("aformat=channel_layouts=stereo,asetnsamples=n=2048:p=0,"
                  "astats=metadata=1:reset=1,"
                  "ametadata=mode=print:key=lavfi.astats.1.Peak_level,"
                  "ametadata=mode=print:key=lavfi.astats.2.Peak_level")
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-af", af, "-f", "null", "-"]
        return cmd

    def _stderr_reader(self, proc):
        in_output = False
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            m = LEVEL_RE.search(line)
            if m:
                try:
                    v = float(m.group(2))
                except ValueError:
                    v = -math.inf
                ch = int(m.group(1)) - 1
                self.peak[ch] = v
                self.audio_t = time.monotonic()
                self._lv.append((self.audio_t, ch, v))
                continue
            if "Parsed_ametadata" in line:      # "frame:N pts:..." headers
                continue
            if line.startswith("Output #") or line.startswith("Stream mapping"):
                in_output = True          # ignore our own output streams
            elif line.startswith("Input #"):
                in_output = False
            if not in_output:
                m = VIDEO_RE.search(line)
                if m:
                    fps = FPS_RE.search(line)
                    if fps and 1.0 <= float(fps.group(1)) <= 120.0:
                        self.src_fps = float(fps.group(1))
                    self.info = f"{m.group(1)} {m.group(2)}x{m.group(3)}" + (
                        f"@{float(fps.group(1)):g}" if fps else "")
                    if self.has_audio is None:
                        self.has_audio = False   # until an audio stream line shows up
                m = AUDIO_RE.search(line)
                if m:
                    hz = int(m.group(2))
                    self.has_audio = True
                    self.audio_info = f"{m.group(1)} {hz / 1000:g}k {m.group(3).strip()}"
            low = line.lower()
            if any(k in low for k in ERROR_HINTS):
                self._lines.append(line[-200:])

    def _watchdog(self):
        while not self.stopping:
            time.sleep(0.5)
            now = time.monotonic()
            dt = now - self._count_t
            if dt >= 1.0:
                self.measured_fps = self._count / dt
                self._count, self._count_t = 0, now
            p = self.proc
            if not p or p.poll() is not None:
                continue
            ct, st = self.app.cfg["connect_timeout"], self.app.cfg["stall_timeout"]
            if self._got_frame and now - self.last_frame_t > st:
                log.warning("[%d] stalled, reconnecting", self.idx + 1)
                self._kill_reason = "stall"
                self._kill()
            elif not self._got_frame and now - self.started_t > ct:
                self._kill_reason = "timeout"
                self._kill()

    def _reason(self, was_live, rc):
        cfg = self.app.cfg
        if self._kill_reason == "stall":
            return f"Stream stalled (no video for {cfg['stall_timeout']:g}s)"
        found = classify(list(self._lines))
        if self._kill_reason == "timeout":
            return found or f"No stream (nothing received in {cfg['connect_timeout']:g}s)"
        if found:
            return found
        if was_live:
            return "Stream ended"
        if self._lines:
            return self._lines[-1][-120:]
        return f"ffmpeg exited (code {rc})"

    def _run(self):
        size = self.app.layout.video_bytes()
        shape = (self.app.layout.vid_h, self.app.layout.vid_w * 2)
        while not self.stopping:
            if not self.url:
                self.state = STATE_UNSET
                self.wake.wait()
                self.wake.clear()
                continue
            url = self.url
            # A feed that already failed keeps showing NO SIGNAL (or STANDBY) while we retry;
            # CONNECTING is only shown for the first attempt after start / URL change.
            self.state = (STATE_STANDBY if self.standby else
                          STATE_OFFLINE if self.failed else STATE_CONNECTING)
            self.info, self.audio_info, self.has_audio = "", "", None
            self.src_fps = 0.0
            self._reset_buffer()
            self.peak = [-math.inf, -math.inf]
            self._lines.clear()
            self._kill_reason = None
            self._got_frame = False
            self.started_t = time.monotonic()
            self.attempts += 1
            log.debug("[%d] connecting (attempt %d)", self.idx + 1, self.attempts)
            try:
                self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             bufsize=size * 2)
            except Exception as e:
                self.last_error = f"Cannot start ffmpeg: {e}"
                log.error("[%d] %s", self.idx + 1, self.last_error)
                self._fail(False)
                continue
            proc = self.proc
            reader = threading.Thread(target=self._stderr_reader, args=(proc,), daemon=True)
            reader.start()
            was_live = False
            while not self.stopping and url == self.url:
                buf = proc.stdout.read(size)
                if not buf or len(buf) < size:
                    break
                self.last_frame_t = time.monotonic()
                self._push(np.frombuffer(buf, np.uint8).reshape(shape), self.last_frame_t)
                self._got_frame = True
                self._count += 1
                if self.state != STATE_LIVE:
                    self.state = STATE_LIVE
                    self.live_since = self.last_frame_t
                    self.failed = False
                    self.standby = False
                    self.down_since = 0.0
                    self.last_error = ""
                    was_live = True
                    log.info("[%d] LIVE %s %s", self.idx + 1, self.info, self.audio_info)
            self._kill()
            try:
                rc = proc.wait(timeout=5)
            except Exception:
                rc = None
            reader.join(timeout=1)       # make sure all stderr lines are in before classifying
            if self.stopping:
                break
            if url != self.url:          # changed by the web UI; reconnect at once
                continue
            self.last_error = redact(self._reason(was_live, rc), url)
            if was_live or not self.failed:
                log.info("[%d] %s: %s", self.idx + 1, "signal lost" if was_live else "no signal",
                         self.last_error)
            self._fail(was_live)

    def _fail(self, was_live):
        now = time.monotonic()
        if was_live or not self.failed or not self.down_since:
            self.down_since = now
        self.failed = True
        self._reset_buffer()
        cfg = self.app.cfg
        after = float(cfg.get("standby_after") or 0)
        if not self.standby and after > 0 and now - self.down_since >= after:
            self.standby = True
            log.info("[%d] no stream for %s - standby", self.idx + 1, fmt_dur(now - self.down_since))
        self.state = STATE_STANDBY if self.standby else STATE_OFFLINE
        if self.standby:
            # keep checking on our own (never stop), so a channel wakes up by itself when
            # someone starts streaming to it
            delay = max(5.0, float(cfg.get("standby_retry") or 60))
        elif was_live:
            delay = cfg.get("reconnect_delay_after_drop", 2.0)
        else:
            delay = cfg["reconnect_delay"]
        self.wake.wait(delay)
        self.wake.clear()


# ----------------------------------------------------------------------------
# Output sinks
# ----------------------------------------------------------------------------

def _load_lib(explicit, env_dir, names, what):
    cands = []
    if explicit:
        cands.append(explicit)
    dirs = []
    if env_dir and os.environ.get(env_dir):
        dirs.append(os.environ[env_dir])
    dirs.append(APP_DIR)
    if not IS_WIN:
        dirs += ["/usr/local/lib", "/usr/lib"]
    cands += [os.path.join(d, n) for d in dirs for n in names]
    cands += names
    for c in cands:
        if os.path.isabs(c) and not os.path.exists(c):
            continue
        try:
            if IS_WIN and os.path.isabs(c):
                os.add_dll_directory(os.path.dirname(c))   # so e.g. libvmx.dll next to libomt.dll loads
            return ctypes.CDLL(c)
        except OSError:
            pass
    raise RuntimeError(f"cannot load the {what} library ({', '.join(names)}); "
                       f"set output.{what.lower()}_lib or run install.sh --{what.lower()}")


FOURCC_UYVY = 0x59565955   # 'U','Y','V','Y'


class NDISink:
    """In-process NDI sender via the NDI 6 runtime (libndi.so.6)."""

    class SendCreate(ctypes.Structure):
        _fields_ = [("p_ndi_name", ctypes.c_char_p), ("p_groups", ctypes.c_char_p),
                    ("clock_video", ctypes.c_bool), ("clock_audio", ctypes.c_bool)]

    class VideoFrame(ctypes.Structure):
        _fields_ = [("xres", ctypes.c_int), ("yres", ctypes.c_int), ("FourCC", ctypes.c_int),
                    ("frame_rate_N", ctypes.c_int), ("frame_rate_D", ctypes.c_int),
                    ("picture_aspect_ratio", ctypes.c_float), ("frame_format_type", ctypes.c_int),
                    ("timecode", ctypes.c_int64), ("p_data", ctypes.c_void_p),
                    ("line_stride_in_bytes", ctypes.c_int), ("p_metadata", ctypes.c_char_p),
                    ("timestamp", ctypes.c_int64)]

    def __init__(self, ocfg):
        self.ocfg = ocfg
        self.lib = None
        self.inst = None

    def open(self):
        if self.lib is None:
            lib = _load_lib(self.ocfg.get("ndi_lib"), "NDI_RUNTIME_DIR_V6",
                            ["Processing.NDI.Lib.x64.dll"] if IS_WIN else ["libndi.so.6", "libndi.so"],
                            "NDI")
            lib.NDIlib_initialize.restype = ctypes.c_bool
            lib.NDIlib_send_create.restype = ctypes.c_void_p
            lib.NDIlib_send_create.argtypes = [ctypes.POINTER(self.SendCreate)]
            lib.NDIlib_send_send_video_async_v2.restype = None
            lib.NDIlib_send_send_video_async_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.VideoFrame)]
            lib.NDIlib_send_get_no_connections.restype = ctypes.c_int
            lib.NDIlib_send_get_no_connections.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            lib.NDIlib_send_destroy.argtypes = [ctypes.c_void_p]
            if not lib.NDIlib_initialize():
                raise RuntimeError("NDIlib_initialize failed (CPU not supported?)")
            self.lib = lib
        groups = self.ocfg.get("ndi_groups") or None
        self._create = self.SendCreate(self.ocfg["name"].encode(),
                                       groups.encode() if groups else None, False, False)
        self.inst = self.lib.NDIlib_send_create(ctypes.byref(self._create))
        if not self.inst:
            raise RuntimeError("NDIlib_send_create failed")
        w, h, fps = self.ocfg["width"], self.ocfg["height"], self.ocfg["fps"]
        n, d = (int(round(fps * 1000)), 1000) if fps != int(fps) else (int(fps), 1)
        self.vf = self.VideoFrame(w, h, FOURCC_UYVY, n, d, w / h, 1,   # 1 = progressive
                                  0x7fffffffffffffff, None, w * 2, None, 0)
        log.info("NDI source %r created", self.ocfg["name"])

    def send(self, frame):
        # Async send: NDI compresses this frame in the background while we prepare the next.
        # The buffer must stay valid until the next call, so keep a reference to it.
        self.vf.p_data = frame.ctypes.data
        self.lib.NDIlib_send_send_video_async_v2(self.inst, ctypes.byref(self.vf))
        self._inflight = frame

    def connections(self):
        if self.inst:
            return self.lib.NDIlib_send_get_no_connections(self.inst, 0)
        return None

    def close(self):
        if self.inst:
            self.lib.NDIlib_send_send_video_async_v2(self.inst, None)   # flush, release buffer
            self.lib.NDIlib_send_destroy(self.inst)
            self.inst = None
            self._inflight = None

    def kill(self):
        pass


class OMTSink:
    """In-process OMT sender via libomt.so (Open Media Transport, MIT licence)."""

    class MediaFrame(ctypes.Structure):
        _fields_ = [("Type", ctypes.c_int), ("Timestamp", ctypes.c_int64), ("Codec", ctypes.c_int),
                    ("Width", ctypes.c_int), ("Height", ctypes.c_int), ("Stride", ctypes.c_int),
                    ("Flags", ctypes.c_int), ("FrameRateN", ctypes.c_int),
                    ("FrameRateD", ctypes.c_int), ("AspectRatio", ctypes.c_float),
                    ("ColorSpace", ctypes.c_int), ("SampleRate", ctypes.c_int),
                    ("Channels", ctypes.c_int), ("SamplesPerChannel", ctypes.c_int),
                    ("Data", ctypes.c_void_p), ("DataLength", ctypes.c_int),
                    ("CompressedData", ctypes.c_void_p), ("CompressedLength", ctypes.c_int),
                    ("FrameMetadata", ctypes.c_void_p), ("FrameMetadataLength", ctypes.c_int)]

    QUALITY = {"default": 0, "low": 1, "medium": 50, "high": 100}

    def __init__(self, ocfg):
        self.ocfg = ocfg
        self.lib = None
        self.inst = None
        self.n = 0

    def open(self):
        if self.lib is None:
            lib = _load_lib(self.ocfg.get("omt_lib"), None,
                            ["libomt.dll"] if IS_WIN else ["libomt.so"], "OMT")
            lib.omt_send_create.restype = ctypes.c_void_p
            lib.omt_send_create.argtypes = [ctypes.c_char_p, ctypes.c_int]
            lib.omt_send.restype = ctypes.c_int
            lib.omt_send.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.MediaFrame)]
            lib.omt_send_connections.restype = ctypes.c_int
            lib.omt_send_connections.argtypes = [ctypes.c_void_p]
            lib.omt_send_destroy.argtypes = [ctypes.c_void_p]
            self.lib = lib
        q = self.QUALITY.get(str(self.ocfg.get("omt_quality", "default")).lower(), 0)
        self.inst = self.lib.omt_send_create(self.ocfg["name"].encode(), q)
        if not self.inst:
            raise RuntimeError("omt_send_create failed")
        w, h, fps = self.ocfg["width"], self.ocfg["height"], self.ocfg["fps"]
        n, d = (int(round(fps * 1000)), 1000) if fps != int(fps) else (int(fps), 1)
        self.fr = self.MediaFrame()
        self.fr.Type, self.fr.Codec = 2, FOURCC_UYVY            # Video, UYVY
        self.fr.Width, self.fr.Height, self.fr.Stride = w, h, w * 2
        self.fr.FrameRateN, self.fr.FrameRateD = n, d
        self.fr.AspectRatio, self.fr.ColorSpace = w / h, 709
        self.fr.DataLength = w * h * 2
        self.tick = 10_000_000 * d / n                         # OMT timestamps: 100 ns units
        log.info("OMT source %r created", self.ocfg["name"])

    def send(self, frame):
        self.fr.Data = frame.ctypes.data
        self.fr.Timestamp = int(self.n * self.tick)
        self.n += 1
        self.lib.omt_send(self.inst, ctypes.byref(self.fr))

    def connections(self):
        return self.lib.omt_send_connections(self.inst) if self.inst else None

    def close(self):
        if self.inst:
            self.lib.omt_send_destroy(self.inst)
            self.inst = None

    def kill(self):
        pass


class PipeSink:
    """Raw UYVY frames into a child process's stdin."""

    def __init__(self, cmd):
        self.cmd = cmd
        self.proc = None

    def open(self):
        log.info("starting output: %s", " ".join(self.cmd))
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE)

    def send(self, frame):
        if self.proc.poll() is not None:
            raise RuntimeError(f"output process exited (code {self.proc.returncode})")
        self.proc.stdin.write(frame.data)

    def connections(self):
        return None

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            p.kill()

    def close(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.stdin.close()
            except Exception:
                pass
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except Exception:
                    p.kill()


def make_sink(ocfg):
    if not ocfg.get("enabled", True):
        return None
    t = (ocfg.get("type") or "ndi").lower()
    w, h, fps, name = ocfg["width"], ocfg["height"], ocfg["fps"], ocfg["name"]
    if t == "ndi":
        return NDISink(ocfg)
    if t == "omt":
        return OMTSink(ocfg)
    if t == "ndi-gst":
        return PipeSink(["gst-launch-1.0", "-q",
                         "fdsrc", "fd=0", f"blocksize={w * h * 2}", "!",
                         "rawvideoparse", f"width={w}", f"height={h}", "format=uyvy",
                         f"framerate={fps}/1", "!",
                         "queue", "max-size-buffers=3", "leaky=downstream", "!",
                         "ndisink", f"ndi-name={name}", "sync=false"])
    if t == "omt-ffmpeg":
        return PipeSink([ocfg.get("omt_ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "warning",
                         "-f", "rawvideo", "-pix_fmt", "uyvy422", "-s", f"{w}x{h}", "-r", str(fps),
                         "-i", "pipe:0", "-f", "libomt", name])
    if t == "custom":
        cmd = ocfg.get("command")
        if not cmd:
            raise SystemExit("output.type=custom needs output.command")
        return PipeSink([str(c).format(width=w, height=h, fps=fps, name=name) for c in cmd])
    if t == "null":
        return None
    raise SystemExit(f"Unknown output.type {t!r}")


def display_command(cfg, W, H):
    """ffplay reading raw UYVY from stdin. Timestamps come from the arrival time, so it simply
    shows every frame as soon as it arrives, whatever rate we send at (50 fps live, idle_fps idle)."""
    d = cfg["display"]
    cmd = [cfg["ffplay"], "-hide_banner", "-loglevel", "error", "-nostats",
           "-window_title", cfg["output"].get("name") or "RTMP Multiview",
           "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
           "-use_wallclock_as_timestamps", "1",
           "-f", "rawvideo", "-pixel_format", "uyvy422", "-video_size", f"{W}x{H}",
           "-framerate", str(cfg["output"]["fps"]), "-an", "-sn"]
    if d.get("fullscreen"):
        cmd += ["-fs"]
    else:
        cmd += ["-x", str(int(d.get("width") or 1280)), "-y", str(int(d.get("height") or 720))]
    return cmd + ["-i", "pipe:0"]


class Output:
    """Sends frames from its own thread so a slow or hung sink can never stall the
    compositor: if the sink is busy the newest frame replaces the waiting one.

    on_close: called (and the output stops) when the sink's process exits by itself,
    used for the display window so closing it ends the program."""

    def __init__(self, sink, kind, on_close=None):
        self.type = kind
        self.sink = sink
        self.on_close = on_close
        self.ok = self.sink is None
        self.error = ""
        self.sent = 0
        self.dropped = 0
        self.conns = None
        self._pending = None
        self._cv = threading.Condition()
        self._busy_since = 0.0
        self._killed = False
        self._stop = False
        if self.sink is not None:
            threading.Thread(target=self._run, name=f"output-{kind}", daemon=True).start()

    @property
    def active(self):
        return self.sink is not None and not self._stop

    def write(self, f):
        """f must be a frame the caller won't modify any more (it is sent asynchronously)."""
        if not self.active:
            return
        with self._cv:
            if self._pending is not None:
                self.dropped += 1
            self._pending = f
            self._cv.notify()
        b = self._busy_since
        if b and time.monotonic() - b > 3.0:          # sink hung for 3 s: kill it
            log.error("%s stuck, restarting it", self.type)
            self._busy_since = 0.0
            self._killed = True              # our kill, not the user closing the window
            try:
                self.sink.kill()
            except Exception:
                pass

    def _run(self):
        opened = False
        next_try = 0.0
        backoff = 5.0
        n_conn_t = 0.0
        while not self._stop:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait(0.5)
                f, self._pending = self._pending, None
            if f is None:
                continue
            now = time.monotonic()
            if not opened:
                if now < next_try:
                    continue
                try:
                    self.sink.open()
                    opened, self.ok, self.error = True, True, ""
                    backoff = 5.0
                except FileNotFoundError as e:
                    self.ok, self.error = False, f"program not found: {e.filename}"
                    log.error("%s: %s - disabled", self.type, self.error)
                    self._stop = True
                    return
                except Exception as e:
                    self.ok, self.error = False, str(e)
                    log.error("cannot start %s output: %s (retry in %ds)", self.type, e, backoff)
                    next_try = now + backoff
                    backoff = min(60.0, backoff * 2)
                    continue
            try:
                self._busy_since = time.monotonic()
                self.sink.send(f)
                self._busy_since = 0.0
                self.sent += 1
            except Exception as e:
                self._busy_since = 0.0
                self.ok, self.error = False, str(e) or e.__class__.__name__
                killed, self._killed = self._killed, False
                if self.on_close and not self._stop and not killed:
                    log.info("%s closed", self.type)
                    self._stop = True
                    try:
                        self.sink.close()
                    except Exception:
                        pass
                    self.on_close()
                    return
                log.error("%s output failed: %s - restarting in 3 s", self.type, self.error)
                try:
                    self.sink.close()
                except Exception:
                    pass
                opened, next_try = False, time.monotonic() + 3
                continue
            if now - n_conn_t > 1.0:
                n_conn_t = now
                try:
                    self.conns = self.sink.connections()
                except Exception:
                    self.conns = None

    def stop(self):
        self._stop = True
        with self._cv:
            self._cv.notify()
        if self.sink is not None:
            try:
                self.sink.close()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Compositor
# ----------------------------------------------------------------------------

STATE_STYLE = {
    STATE_LIVE: ("LIVE", (31, 157, 85)),
    STATE_OFFLINE: ("NO SIGNAL", (192, 41, 43)),
    STATE_CONNECTING: ("CONNECTING", (214, 137, 16)),
    STATE_UNSET: ("NOT SET", (90, 90, 90)),
    STATE_STANDBY: ("STANDBY", (52, 86, 128)),
}

METER_FLOOR = -60.0
METER_ZONES = [(-18.0, (40, 200, 90), (18, 60, 30)),     # up to -18 dBFS: green
               (-6.0, (240, 190, 40), (70, 58, 16)),     # -18 .. -6: amber
               (0.0, (235, 50, 45), (72, 22, 20))]       # -6 .. 0: red
METER_TICKS = (0, -6, -12, -18, -24, -36, -48)


class Meter:
    """Two-channel vertical peak meter in UYVY, rendered from two precomputed images."""

    def __init__(self, layout, font):
        self.w, self.h = layout.meter_w, layout.meter_h
        w, h = self.w, self.h
        self.label_h = max(12, even(w * 0.4))
        self.top = even(h * 0.02) + 2
        self.bar_h = h - self.label_h - self.top
        bw = even(w * 0.3)
        gap = even((w - 2 * bw) / 3)
        self.bars = [(gap, bw), (w - gap - bw, bw)]
        self.unlit = rgb_to_uyvy(self._draw(font, lit=False))
        self.lit = rgb_to_uyvy(self._draw(font, lit=True))
        self.absent = rgb_to_uyvy(self._draw_absent(font))
        self.white = rgb_to_uyvy(np.full((2, w, 3), 235, np.uint8))

    def _y(self, db):
        """dB -> row (inside full meter image) of the top of a bar at that level."""
        frac = (min(0.0, max(METER_FLOOR, db)) - METER_FLOOR) / -METER_FLOOR
        return self.top + self.bar_h - int(round(frac * self.bar_h))

    def _draw(self, font, lit):
        im = Image.new("RGB", (self.w, self.h), (10, 10, 12))
        d = ImageDraw.Draw(im)
        lo = METER_FLOOR
        for hi, on, off in METER_ZONES:
            for x, bw in self.bars:
                d.rectangle([x, self._y(hi), x + bw - 1, self._y(lo) - 1], fill=on if lit else off)
            lo = hi
        b0, b1 = self.bars[0][0] + self.bars[0][1], self.bars[1][0]
        for t in METER_TICKS:
            y = self._y(t)
            d.line([b0 + 1, y, b1 - 2, y], fill=(150, 150, 150))
        f = font(self.label_h - 2)
        for (x, bw), txt in zip(self.bars, ("1", "2")):
            d.text((x + bw // 2, self.h - self.label_h // 2), txt, font=f, fill=(200, 200, 200), anchor="mm")
        return np.asarray(im)

    def _draw_absent(self, font):
        im = Image.new("RGB", (self.w, self.h), (10, 10, 12))
        d = ImageDraw.Draw(im)
        f = font(max(10, int(self.w * 0.28)))
        cx = self.w // 2
        for k, ch in enumerate("NO AUDIO"):
            d.text((cx, self.h // 2 + int((k - 3.5) * getattr(f, "size", 10) * 1.15)), ch, font=f,
                   fill=(220, 70, 60), anchor="mm")
        return np.asarray(im)

    def render(self, levels, holds):
        out = self.unlit.copy()
        for (x, bw), db, hold in zip(self.bars, levels, holds):
            c0, c1 = 2 * x, 2 * (x + bw)
            y = self._y(db)
            bottom = self.top + self.bar_h
            if y < bottom:
                out[y:bottom, c0:c1] = self.lit[y:bottom, c0:c1]
            if hold > METER_FLOOR:
                yh = max(self.top, self._y(hold) - 1)
                out[yh:yh + 2, c0:c1] = self.white[:, c0:c1]
        return out


class Renderer:
    def __init__(self, layout, font_path):
        self.L = layout
        self.font_path = font_path
        self.cache = {}
        self.meter = Meter(layout, self.font)

    def font(self, size):
        key = ("font", size)
        if key not in self.cache:
            for path in [self.font_path] + FONT_FALLBACKS:
                try:
                    self.cache[key] = ImageFont.truetype(path, size)
                    break
                except Exception:
                    continue
            else:
                try:
                    self.cache[key] = ImageFont.load_default(size)
                except TypeError:                 # Pillow < 10.1
                    self.cache[key] = ImageFont.load_default()
        return self.cache[key]

    def _cached(self, key, fn):
        img = self.cache.get(key)
        if img is None:
            if len(self.cache) > 400:
                self.cache = {k: v for k, v in self.cache.items() if k[0] == "font"}
            img = fn()
            self.cache[key] = img
        return img

    def label(self, name, state, right, reserve_left=0, reserve_right=0, video_above=True):
        def make():
            L = self.L
            w, h = L.tile_w - reserve_left - reserve_right, L.label_h
            im = Image.new("RGB", (w, h), (18, 18, 20))
            d = ImageDraw.Draw(im)
            text, color = STATE_STYLE[state]
            # tally-coloured stripe on the edge that faces this label's own picture
            sy = 0 if video_above else h - 3
            d.rectangle([0, sy, w, sy + 2], fill=color)
            fs = int(h * 0.5)
            f = self.font(fs)
            pad = int(h * 0.25)
            tw = d.textlength(text, font=f)
            d.rectangle([pad, pad // 2, pad + tw + 2 * pad, h - pad // 2], fill=color)
            d.text((2 * pad, h // 2), text, font=f, fill=(255, 255, 255), anchor="lm")
            x = int(pad + tw + 3 * pad)
            rf = self.font(int(h * 0.4))
            # info text may use at most ~70% of the bar; the name gets the rest.
            # Shorten from the middle so the received fps (last part) always stays visible.
            head, sep, tail = right.rpartition("  ·  ")
            rt = right
            while head and d.textlength(rt, font=rf) > (w - x) * 0.7:
                head = head[:-2]
                rt = head + "…" + sep + tail
            rw = d.textlength(rt, font=rf) if rt else 0
            d.text((w - pad - rw, h // 2), rt, font=rf, fill=(170, 170, 170), anchor="lm")
            nm = name
            maxw = w - x - rw - 3 * pad
            while nm and d.textlength(nm, font=f) > maxw:
                nm = nm[:-2] + "…" if len(nm) > 2 else ""
            d.text((x, h // 2), nm, font=f, fill=(255, 255, 255), anchor="lm")
            return rgb_to_uyvy(np.asarray(im))
        img = self._cached(("label", name, state, right, reserve_left, reserve_right, video_above), make)
        if reserve_left or reserve_right:
            full = np.empty((self.L.label_h, self.L.tile_w * 2), np.uint8)
            full[:, 0::2], full[:, 1::2] = 128, 16
            full[:, 2 * reserve_left:2 * reserve_left + img.shape[1]] = img
            return full
        return img

    def slate(self, state, detail):
        def make():
            L = self.L
            im = Image.new("RGB", (L.vid_w, L.vid_h), (28, 28, 32))
            d = ImageDraw.Draw(im)
            text, color = STATE_STYLE[state]
            if state == STATE_UNSET:
                text = "NO URL SET"
            elif state == STATE_STANDBY:
                color = (110, 150, 200)          # brighter than the tally so it reads on the slate
            f = self.font(int(L.vid_h * 0.11))
            d.text((L.vid_w // 2, L.vid_h // 2 - int(L.vid_h * 0.07)), text, font=f,
                   fill=color, anchor="mm")
            if detail:
                sf = self.font(max(12, int(L.vid_h * 0.04)))
                det = detail
                while det and d.textlength(det, font=sf) > L.vid_w * 0.86:
                    det = det[:-2] + "…"
                d.text((L.vid_w // 2, L.vid_h // 2 + int(L.vid_h * 0.06)), det, font=sf,
                       fill=(200, 200, 200), anchor="mm")
            return rgb_to_uyvy(np.asarray(im))
        return self._cached(("slate", state, detail), make)

    def slate_line(self, text):
        """Small strip under the slate text (changes every second, so kept tiny)."""
        def make():
            L = self.L
            h = even(L.vid_h * 0.07)
            w = even(L.vid_w * 0.7)
            im = Image.new("RGB", (w, h), (28, 28, 32))
            ImageDraw.Draw(im).text((w // 2, h // 2), text, font=self.font(max(11, int(h * 0.5))),
                                    fill=(130, 130, 135), anchor="mm")
            return rgb_to_uyvy(np.asarray(im))
        return self._cached(("sline", text), make)

    def _clock_fonts(self, caption):
        h = self.L.band_h
        if caption:
            return self.font(int(h * 0.5)), self.font(int(h * 0.24))
        return self.font(int(h * 0.7)), None

    def center_width(self, caption):
        """Width of the centre box; depends only on the caption so it doesn't jitter."""
        def make():
            cf, capf = self._clock_fonts(caption)
            d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
            w = d.textlength("88:88:88", font=cf)
            if caption:
                w = max(w, d.textlength(caption, font=capf))
            w += self.L.band_h * 0.5
            step = 32
            return min(even(self.L.W * 0.44), int(math.ceil(w / step) * step))
        return self._cached(("cwidth", caption), make)

    def center(self, clock_text, caption):
        """Clock (and optional caption line) box for the centre band."""
        def make():
            L = self.L
            w, h = self.center_width(caption), L.band_h
            cf, capf = self._clock_fonts(caption)
            im = Image.new("RGB", (w, h), (0, 0, 0))
            d = ImageDraw.Draw(im)
            if caption:
                d.text((w // 2, int(h * 0.36)), clock_text, font=cf, fill=(245, 245, 245), anchor="mm")
                cap = caption
                while cap and d.textlength(cap, font=capf) > w - 8:
                    cap = cap[:-2] + "…"
                d.text((w // 2, int(h * 0.8)), cap, font=capf, fill=(255, 196, 70), anchor="mm")
            else:
                d.text((w // 2, h // 2), clock_text, font=cf, fill=(245, 245, 245), anchor="mm")
            return rgb_to_uyvy(np.asarray(im))
        return self._cached(("center", clock_text, caption), make)


class App:
    def __init__(self, config_path, overrides=None):
        self.config_path = config_path
        self.cfg = self._load()
        for (section, key), v in (overrides or {}).items():     # command line wins over the file
            self.cfg[section][key] = v
        o = self.cfg["output"]
        self.layout = Layout(o["width"], o["height"])
        self.fps = float(o["fps"])
        self.renderer = Renderer(self.layout, self.cfg["font"])
        self.inputs = [Input(i, c, self) for i, c in enumerate((self.cfg["inputs"] + [{}] * 4)[:4])]
        self.stopping = threading.Event()
        kind = (o.get("type") or "ndi").lower() if o.get("enabled", True) else "off"
        self.output = Output(make_sink(o), kind)
        self.display = None
        if self._display_wanted():
            d = self.cfg["display"]
            cmd = display_command(self.cfg, self.layout.W, self.layout.H)
            self.display = Output(PipeSink(cmd), "display",
                                  on_close=self._display_closed if d.get("exit_on_close", True) else None)
        self.mode = ""
        self.boot = secrets.token_hex(4)          # lets browsers notice a restart
        self.previews = {}
        for name, v in (self.cfg["web"].get("stream_profiles") or {}).items():
            try:
                w, h, fps, q = v
                self.previews[name] = PreviewStream(self, name, int(w), int(h), float(fps), int(q))
            except (TypeError, ValueError):
                log.error("bad web.stream_profiles entry %r: %r", name, v)
        self.frame = np.zeros((self.layout.H, self.layout.W * 2), np.uint8)
        self.frame[:, 0::2] = 128   # U/V neutral
        self.frame[:, 1::2] = 16    # Y black
        self.stats = {"render_ms": 0.0, "late_frames": 0, "frames": 0}
        self.cfg_lock = threading.Lock()
        self._snap = (0.0, b"")
        self._snap_lock = threading.Lock()
        # meter ballistics per input: displayed level, peak hold value, hold time
        self._disp = [[METER_FLOOR, METER_FLOOR] for _ in range(4)]
        self._hold = [[METER_FLOOR, METER_FLOOR, 0.0, 0.0] for _ in range(4)]
        self._last_t = time.monotonic()

    def _display_wanted(self):
        e = self.cfg["display"].get("enabled", "auto")
        if isinstance(e, str) and e.lower() == "auto":
            if IS_WIN:
                return True
            has_gui = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            under_systemd = bool(os.environ.get("INVOCATION_ID"))
            return has_gui and not under_systemd
        return bool(e) and str(e).lower() not in ("0", "false", "no", "off")

    def _display_closed(self):
        log.info("display window closed - exiting")
        self.stopping.set()

    # -- config ------------------------------------------------------------
    def _load(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8-sig") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        else:
            log.warning("config %s not found, creating it with defaults", self.config_path)
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
                fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({k: v for k, v in cfg.items() if k != "font"}, f, indent=2)
            except OSError as e:
                log.warning("could not write %s: %s", self.config_path, e)
        # an ffmpeg.exe next to the program wins over one on PATH (Windows build)
        if cfg.get("ffmpeg") == "ffmpeg":
            local = os.path.join(APP_DIR, "ffmpeg.exe" if IS_WIN else "ffmpeg")
            if os.path.isfile(local):
                cfg["ffmpeg"] = local
        if cfg.get("ffplay") == "ffplay":       # next to the program, else next to ffmpeg, else PATH
            exe = "ffplay.exe" if IS_WIN else "ffplay"
            for d in (APP_DIR, os.path.dirname(cfg["ffmpeg"])):
                if d and os.path.isfile(os.path.join(d, exe)):
                    cfg["ffplay"] = os.path.join(d, exe)
                    break
        while len(cfg["inputs"]) < 4:
            cfg["inputs"].append({"name": f"Channel {len(cfg['inputs']) + 1}", "url": ""})
        return cfg

    def save_inputs(self, items, caption=None):
        clean = []
        for i, item in enumerate(items[:4]):
            name = str(item.get("name", "")).strip()[:60]
            url = str(item.get("url", "")).strip()[:1000]
            changed = i >= len(self.inputs) or url != self.inputs[i].url
            if changed and url and not url.lower().startswith(ALLOWED_SCHEMES):
                raise ValueError(f"Input {i + 1}: URL must start with rtmp://, srt://, https:// …")
            if changed and any(c in url for c in "\r\n\t "):
                raise ValueError(f"Input {i + 1}: URL contains spaces")
            clean.append((name, url))
        with self.cfg_lock:
            for i, (name, url) in enumerate(clean):
                self.inputs[i].update(name, url)
                self.cfg["inputs"][i] = {**self.cfg["inputs"][i], "name": name, "url": url}
            # write back only what was in the file plus the inputs, keep the file mode 600
            on_disk = {}
            if os.path.exists(self.config_path):
                with open(self.config_path, encoding="utf-8-sig") as f:
                    on_disk = json.load(f)
            on_disk["inputs"] = self.cfg["inputs"]
            if caption is not None:
                caption = " ".join(str(caption).split())[:80]
                self.cfg["clock_caption"] = caption
                on_disk["clock_caption"] = caption
            tmp = self.config_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(on_disk, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.config_path)

    # -- main loop ---------------------------------------------------------
    def _meter_levels(self, i, inp, now, dt):
        lv = inp.levels()
        disp, hold = self._disp[i], self._hold[i]
        for ch in range(2):
            v = max(METER_FLOOR, lv[ch])
            disp[ch] = v if v >= disp[ch] else max(v, disp[ch] - 24.0 * dt)   # 24 dB/s fall
            if v >= hold[ch] or now - hold[2 + ch] > 1.5:
                hold[ch], hold[2 + ch] = v, now
        return disp, hold[:2]

    def compose(self):
        L, R, fr = self.layout, self.renderer, self.frame
        now = time.monotonic()
        dt, self._last_t = now - self._last_t, now
        meters = self.cfg.get("audio_meters", True)
        show_clock = self.cfg.get("clock", True)
        caption = str(self.cfg.get("clock_caption") or "").strip()
        box_w = R.center_width(caption) if (show_clock or caption) else 0
        res = box_w // 2
        for i, inp in enumerate(self.inputs):
            vx, vy = L.video_pos[i]
            state = inp.state
            tile = inp.playout(dt) if state == STATE_LIVE else None
            if state == STATE_LIVE and tile is None:
                state = STATE_CONNECTING            # stream is in, buffer still filling
            if tile is not None:
                blit(fr, tile, vx, vy)
                if meters:
                    mx, my = L.meter_pos[i]
                    if inp.has_audio is False:
                        blit(fr, R.meter.absent, mx, my)
                    else:
                        disp, hold = self._meter_levels(i, inp, now, dt)
                        blit(fr, R.meter.render(disp, hold), mx, my)
            else:
                if state == STATE_OFFLINE:
                    detail = inp.last_error or "No signal"
                    sub = (f"down {fmt_dur(now - inp.down_since)}  ·  retrying every "
                           f"{self.cfg['reconnect_delay']:g}s") if inp.down_since else "retrying…"
                elif state == STATE_STANDBY:
                    detail = "No stream - lights up automatically when one starts"
                    sub = (f"idle {fmt_dur(now - inp.down_since)}  ·  checking every "
                           f"{max(5.0, float(self.cfg.get('standby_retry') or 60)):g}s")
                elif state == STATE_CONNECTING:
                    detail = "Buffering…" if inp.state == STATE_LIVE else "Waiting for stream…"
                    sub = ""
                else:
                    detail, sub = "Enter a stream URL in the web UI", ""
                blit(fr, R.slate(state, detail), vx, vy)
                if sub:
                    s = R.slate_line(sub)
                    blit(fr, s, even(vx + (L.vid_w - s.shape[1] // 2) // 2),
                         vy + L.vid_h // 2 + even(L.vid_h * 0.13))
            right = ""
            if state == STATE_LIVE:
                parts = [p for p in (inp.info, inp.audio_info if inp.has_audio else
                                     ("no audio" if inp.has_audio is False else "")) if p]
                parts.append(f"{inp.measured_fps:.1f} fps")
                right = "  ·  ".join(parts)
            lx, ly = L.label_pos[i]
            # keep room on the inner side of every label bar for the centre clock box
            blit(fr, R.label(inp.name, state, right,
                             reserve_left=res if i % 2 == 1 else 0,
                             reserve_right=res if i % 2 == 0 else 0, video_above=i < 2), lx, ly)
        if box_w:
            c = R.center(time.strftime("%H:%M:%S") if show_clock else "", caption)
            blit(fr, c, even((L.W - box_w) // 2), L.band_y)

    def _rate(self, sinks, previews=()):
        """Frame rate for this frame: full while anything is live, idle_fps when every feed is
        empty, standby_fps when all are in standby. With nobody watching or receiving it only
        composes a few frames a second for snapshots. Web viewers alone need at most the
        fastest stream profile's rate."""
        if not sinks and not previews:
            return min(self.fps, 2.0), "nobody watching"
        cap = self.fps if sinks else min(self.fps, max(p.fps for p in previews))
        idle = float(self.cfg.get("idle_fps") or 0)
        if idle <= 0 or any(i.state == STATE_LIVE for i in self.inputs):
            return cap, "live"
        sb = float(self.cfg.get("standby_fps") or 0)
        if sb > 0 and all(i.state in (STATE_STANDBY, STATE_UNSET) for i in self.inputs):
            return min(cap, idle, sb), "standby (power saving)"
        return min(cap, idle), "idle (no live feeds)"

    def run(self):
        for inp in self.inputs:
            inp.start()
        t_next = time.monotonic()
        log.info("running %dx%d @ %g fps (idle %s fps), output=%s, display=%s",
                 self.layout.W, self.layout.H, self.fps, self.cfg.get("idle_fps"),
                 self.output.type, "on" if self.display else "off")
        while not self.stopping.is_set():
            t0 = time.monotonic()
            sinks = [s for s in (self.output, self.display) if s and s.active]
            previews = [p for p in self.previews.values() if p.active]
            rate, self.mode = self._rate(sinks, previews)
            period = 1.0 / rate
            try:
                self.compose()
            except Exception:
                log.exception("compose failed")
            if sinks or previews:
                f = self.frame.copy()            # one copy, shared read-only by all outputs
                for s in sinks:
                    s.write(f)
                for p in previews:
                    p.submit(f, t0)
            for p in self.previews.values():
                p.maintain(t0)
            dt = (time.monotonic() - t0) * 1000
            self.stats["render_ms"] = self.stats["render_ms"] * 0.98 + dt * 0.02
            self.stats["frames"] += 1
            t_next += period
            delay = t_next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                if delay < -period:
                    self.stats["late_frames"] += 1
                if delay < -0.25:            # fell far behind: resync
                    t_next = time.monotonic()
        for inp in self.inputs:
            inp.stop()
        self.output.stop()
        if self.display:
            self.display.stop()
        for p in self.previews.values():
            p.stop()

    def snapshot_jpeg(self, quality=75, max_age=0.2):
        """Preview JPEG, shared between all viewers and regenerated at most 5x per second."""
        with self._snap_lock:
            t, data = self._snap
            now = time.monotonic()
            if now - t < max_age and data:
                return data
            rgb = uyvy_to_rgb_half(self.frame)
            buf = io.BytesIO()
            Image.fromarray(rgb).save(buf, "JPEG", quality=quality)
            self._snap = (now, buf.getvalue())
            return self._snap[1]

    def config_view(self, auth):
        """Settings for the admin panel - the only place stream URLs (keys) are sent out."""
        o = self.cfg["output"]
        return {
            "inputs": [{"index": i.idx + 1, "name": i.name, "url": i.url, "state": i.state}
                       for i in self.inputs],
            "caption": self.cfg.get("clock_caption") or "",
            "open": auth.open, "separate_admin": bool(auth.admin_pw),
            "output": {"enabled": bool(o.get("enabled")), "type": o.get("type"), "name": o.get("name")},
        }

    def status(self):
        o, d = self.output, self.display
        rate, _ = self._rate([s for s in (o, d) if s and s.active],
                             [p for p in self.previews.values() if p.active])
        return {
            "mode": self.mode, "rate": rate,
            "viewers": sum(p.viewers for p in self.previews.values()),
            "boot": self.boot,
            "display": {"enabled": d is not None, "running": bool(d and d.active and d.ok),
                        "error": d.error if d else ""},
            "output": {"type": o.type, "name": self.cfg["output"]["name"],
                       "size": f"{self.layout.W}x{self.layout.H}", "fps": self.fps,
                       "enabled": o.sink is not None,
                       "running": o.ok, "error": o.error, "connections": o.conns,
                       "sent": o.sent, "dropped": o.dropped,
                       "render_ms": round(self.stats["render_ms"], 2),
                       "late_frames": self.stats["late_frames"]},
            "inputs": [i.status() for i in self.inputs],
            "caption": self.cfg.get("clock_caption") or "",
            "standby_retry": max(5.0, float(self.cfg.get("standby_retry") or 60)),
            "time": time.strftime("%H:%M:%S"),
        }


# ----------------------------------------------------------------------------
# Live stream for the web UI (MJPEG, encoded only while somebody watches)
# ----------------------------------------------------------------------------

class PreviewStream:
    """Turns the composited frames into an MJPEG stream for browsers.

    One ffmpeg process per quality profile. It starts when the first viewer connects and
    stops a few seconds after the last one leaves, so it costs nothing while nobody watches.
    Every viewer always gets the newest JPEG; a slow connection simply skips frames."""

    IDLE_STOP_S = 10.0

    def __init__(self, app, name, w, h, fps, q):
        self.app, self.name = app, name
        self.w, self.h = even(w), even(h)
        self.fps = max(1.0, min(float(fps), 60.0))
        self.q = max(2, min(int(q), 31))
        self.viewers = 0
        self.jpeg, self.seq = None, 0
        self.cv = threading.Condition()       # a new JPEG is available
        self.wcv = threading.Condition()      # a new raw frame for the encoder
        self._pending = None
        self.proc = None
        self._last_submit = 0.0
        self._idle_since = None

    @property
    def active(self):
        return self.viewers > 0 and self.proc is not None

    def acquire(self):
        with self.cv:
            self.viewers += 1
            self._idle_since = None
            if self.proc is None or self.proc.poll() is not None:
                self._start()

    def release(self):
        with self.cv:
            self.viewers = max(0, self.viewers - 1)
            if self.viewers == 0:
                self._idle_since = time.monotonic()

    def _start(self):
        L = self.app.layout
        vf = ("format=yuvj420p" if (self.w, self.h) == (L.W, L.H)
              else f"scale={self.w}:{self.h}:flags=area,format=yuvj420p")
        cmd = [self.app.cfg["ffmpeg"], "-hide_banner", "-loglevel", "error",
               "-filter_threads", "1",
               "-f", "rawvideo", "-pix_fmt", "uyvy422", "-s", f"{L.W}x{L.H}",
               "-framerate", f"{self.fps:g}", "-i", "pipe:0",
               "-vf", vf, "-c:v", "mjpeg", "-q:v", str(self.q), "-threads", "2",
               "-f", "mjpeg", "pipe:1"]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            log.error("web stream '%s': cannot start ffmpeg: %s", self.name, e)
            self.proc = None
            return
        log.info("web stream '%s' started (%dx%d @ %g fps)", self.name, self.w, self.h, self.fps)
        self._last_submit = 0.0
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True,
                         name=f"mjpeg-out-{self.name}").start()
        threading.Thread(target=self._writer, args=(self.proc,), daemon=True,
                         name=f"mjpeg-in-{self.name}").start()

    def submit(self, frame, now):
        """Called by the compositor with each new frame; keeps only what the profile's fps needs
        (on average exactly fps, also when the compositor runs at a rate that isn't a multiple)."""
        period = 1.0 / self.fps
        if now + 0.002 < self._last_submit:
            return
        # next due time; resync if we fell more than a frame behind
        self._last_submit = max(self._last_submit + period, now - period + 0.002)
        with self.wcv:
            self._pending = frame
            self.wcv.notify()

    def _writer(self, proc):
        while proc.poll() is None:
            with self.wcv:
                if self._pending is None:
                    self.wcv.wait(0.5)
                f, self._pending = self._pending, None
            if f is None:
                continue
            try:
                proc.stdin.write(f.data)
            except (BrokenPipeError, OSError, ValueError):
                break

    def _reader(self, proc):
        # ffmpeg's mjpeg output is a plain concatenation of JPEG files: cut at SOI/EOI markers
        # (0xFFD9 cannot occur inside the entropy-coded data, it is byte-stuffed there)
        buf = bytearray()
        while True:
            try:
                chunk = proc.stdout.read(262144)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                if s < 0:
                    del buf[:-1]
                    break
                e = buf.find(b"\xff\xd9", s + 2)
                if e < 0:
                    if s:
                        del buf[:s]
                    break
                jpg = bytes(buf[s:e + 2])
                del buf[:e + 2]
                with self.cv:
                    self.jpeg = jpg
                    self.seq += 1
                    self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def wait_next(self, last_seq, timeout=5.0):
        with self.cv:
            if self.seq == last_seq:
                self.cv.wait(timeout)
            return self.seq, self.jpeg

    def maintain(self, now):
        p = self.proc
        if p is None:
            return
        if p.poll() is not None:                 # encoder died: restart if still watched
            self.proc = None
            if self.viewers:
                log.warning("web stream '%s' encoder exited, restarting", self.name)
                with self.cv:
                    self._start()
            return
        if self.viewers == 0 and self._idle_since and now - self._idle_since > self.IDLE_STOP_S:
            self.stop()

    def stop(self):
        p, self.proc = self.proc, None
        if p:
            for f in (p.stdin.close, p.kill):
                try:
                    f()
                except Exception:
                    pass
            log.info("web stream '%s' stopped (no viewers)", self.name)


# ----------------------------------------------------------------------------
# Web authentication
# ----------------------------------------------------------------------------

PBKDF2_ITERATIONS = 300_000


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(PBKDF2_ITERATIONS, base64.b64encode(salt).decode(),
                                           base64.b64encode(dk).decode())


def verify_password(password, stored):
    if not stored or not isinstance(password, str):
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, it, salt, dk = stored.split("$")
            calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
            return hmac.compare_digest(calc, base64.b64decode(dk))
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(password.encode(), stored.encode())


class Auth:
    """Password login with server-side sessions (cookie holds a random token only).

    Roles: with only `password` set, everyone who logs in may also change settings. With
    `admin_password` set, the settings need that password, and admin rights expire after
    admin_minutes of not using them. No password at all = open access (trusted LAN only)."""

    COOKIE = "mv_session"

    def __init__(self, wcfg):
        self.view_pw = os.environ.get("MV_PASSWORD") or str(wcfg.get("password") or "")
        self.admin_pw = os.environ.get("MV_ADMIN_PASSWORD") or str(wcfg.get("admin_password") or "")
        self.open = not self.view_pw and not self.admin_pw
        self.session_s = max(0.1, float(wcfg.get("session_hours") or 168)) * 3600
        self.admin_s = max(1.0, float(wcfg.get("admin_minutes") or 30)) * 60
        self.sessions = {}            # sha256(token) -> {"exp": t, "admin_until": t}
        self.failures = {}            # ip -> (count, locked_until, first_failure)
        self.lock = threading.Lock()

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def locked_for(self, ip):
        with self.lock:
            f = self.failures.get(ip)
            return max(0.0, f[1] - time.monotonic()) if f else 0.0

    def login(self, password, ip, old_token=None):
        """-> (token, None) on success, (None, message) on failure."""
        wait = self.locked_for(ip)
        if wait > 0:
            return None, f"Too many failed attempts. Try again in {int(wait) + 1} s."
        is_admin = bool(self.admin_pw) and verify_password(password, self.admin_pw)
        ok = is_admin or (bool(self.view_pw) and verify_password(password, self.view_pw))
        now = time.monotonic()
        if not ok:
            with self.lock:
                cnt, until, first = self.failures.get(ip, (0, 0.0, now))
                if now - first > 900:
                    cnt, first = 0, now
                cnt += 1
                if cnt >= 5:                       # 30 s, 60 s, 120 s ... up to 15 min
                    until = now + min(900.0, 30.0 * 2 ** (cnt - 5))
                if len(self.failures) > 10000:
                    self.failures.clear()
                self.failures[ip] = (cnt, until, first)
            log.warning("web: failed login from %s", ip)
            time.sleep(0.4)
            return None, "Wrong password."
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.failures.pop(ip, None)
            if old_token:
                self.sessions.pop(self._key(old_token), None)
            if len(self.sessions) > 2000:
                self.sessions = {k: v for k, v in self.sessions.items() if v["exp"] > now}
            self.sessions[self._key(token)] = {"exp": now + self.session_s,
                                               "admin_until": now + self.admin_s if is_admin else 0.0}
        log.info("web: login from %s (%s)", ip, "admin" if is_admin or not self.admin_pw else "viewer")
        return token, None

    def session(self, token, touch=True):
        if not token:
            return None
        now = time.monotonic()
        with self.lock:
            s = self.sessions.get(self._key(token))
            if not s:
                return None
            if s["exp"] < now:
                self.sessions.pop(self._key(token), None)
                return None
            if touch:
                s["exp"] = now + self.session_s
            return s

    def is_admin(self, s, touch=False):
        if self.open or not self.admin_pw:
            return True
        now = time.monotonic()
        if s and s["admin_until"] > now:
            if touch:
                s["admin_until"] = now + self.admin_s
            return True
        return False

    def logout(self, token):
        if token:
            with self.lock:
                self.sessions.pop(self._key(token), None)


class TrustedProxies:
    """IPs / networks / hostnames of reverse proxies whose X-Forwarded-* headers we believe."""

    def __init__(self, entries):
        self.entries = [str(e).strip() for e in entries if str(e).strip()]
        self.nets, self.t = [], 0.0

    def trusted(self, ip):
        if not self.entries:
            return False
        if time.monotonic() - self.t > 60:          # re-resolve hostnames once a minute
            nets = []
            for e in self.entries:
                try:
                    nets.append(ipaddress.ip_network(e, strict=False))
                except ValueError:
                    try:
                        for info in socket.getaddrinfo(e, None):
                            nets.append(ipaddress.ip_network(info[4][0]))
                    except OSError:
                        pass
            self.nets, self.t = nets, time.monotonic()
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in self.nets)


# ----------------------------------------------------------------------------
# Web pages
# ----------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000"><meta name="robots" content="noindex">
<title>RTMP Multiview - sign in</title>
<style nonce="{{N}}">
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0c0e;color:#e8e8ea;font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
form{width:min(340px,90vw);background:#16171b;border:1px solid #2a2c32;border-radius:12px;padding:24px}
h1{font-size:18px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #33363d;background:#0e0f12;color:inherit;font-size:16px}
button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:8px;background:#3b6ef5;color:#fff;font-weight:600;font-size:16px;cursor:pointer}
#e{color:#ff6b6b;min-height:20px;margin-top:10px;font-size:14px}
</style></head><body>
<form id="f"><h1>RTMP Multiview</h1>
<input id="p" type="password" autocomplete="current-password" placeholder="Password" autofocus required>
<button>Sign in</button><div id="e"></div></form>
<script nonce="{{N}}">
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const er=document.getElementById('e');er.textContent='';
 try{const r=await fetch('api/login',{method:'POST',headers:{'Content-Type':'application/json','X-MV':'1'},
   body:JSON.stringify({password:document.getElementById('p').value})});
  if(r.ok){location.replace('./');return}
  const j=await r.json().catch(()=>({}));er.textContent=j.error||('Error '+r.status)}
 catch(x){er.textContent='Cannot reach the server'}};
</script></body></html>"""

VIEWER_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000"><meta name="robots" content="noindex">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Multiview">
<link rel="manifest" href="manifest.webmanifest">
<title>RTMP Multiview</title>
<style nonce="{{N}}">
:root{color-scheme:dark;--fg:#e8e8ea;--mut:#9a9ca3;--card:#16171b;--line:#2a2c32}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#000;color:var(--fg);font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
body{overflow:hidden}
#stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:#000}
#mv{max-width:100%;max-height:100%;display:block;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}
#list{display:none}
#hud{position:fixed;right:max(12px,env(safe-area-inset-right));bottom:max(12px,env(safe-area-inset-bottom));display:flex;gap:4px;align-items:center;
 padding:5px;border-radius:12px;background:rgba(20,21,25,.8);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);transition:opacity .4s;z-index:5}
body.idle #hud{opacity:0;pointer-events:none}
body.idle{cursor:none}
.btn{height:38px;min-width:38px;padding:0 10px;border:0;border-radius:8px;background:transparent;color:var(--fg);font:inherit;font-size:13px;cursor:pointer;
 display:inline-flex;align-items:center;justify-content:center;gap:6px}
.btn:hover{background:rgba(255,255,255,.1)}
.btn svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
select.btn{background:rgba(255,255,255,.07);appearance:none;-webkit-appearance:none;padding:0 12px}
#dot{width:9px;height:9px;border-radius:50%;background:#28c85a;margin:0 8px 0 6px;flex:none}
#dot.bad{background:#eb322d}
#banner{position:fixed;top:max(12px,env(safe-area-inset-top));left:50%;transform:translateX(-50%);background:#c0292b;color:#fff;padding:8px 14px;
 border-radius:8px;font-weight:600;display:none;z-index:6;white-space:nowrap}
#banner.show{display:block}
.pill{font-size:11px;font-weight:700;padding:3px 7px;border-radius:5px;color:#fff;white-space:nowrap}
.live{background:#1f9d55}.offline{background:#c0292b}.connecting{background:#d68910}.unset{background:#5a5a5a}.standby{background:#345680}
@media (orientation:portrait) and (max-width:900px){
 body{overflow:auto}
 #stage{position:relative;inset:auto;width:100%;aspect-ratio:16/9;padding-top:env(safe-area-inset-top)}
 #mv{width:100%;max-height:none}
 #list{display:block;padding:10px 12px calc(84px + env(safe-area-inset-bottom))}
 #hud{left:50%;right:auto;transform:translateX(-50%)}
}
.row{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:center;padding:12px;margin-bottom:8px;background:var(--card);border:1px solid var(--line);border-radius:10px}
.nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{grid-column:1/3;color:var(--mut);font-size:12.5px;word-break:break-word}
.mt{grid-column:1/3;display:flex;gap:8px;align-items:center;font-size:11px;color:var(--mut)}
.bar{flex:1;height:6px;position:relative;border-radius:3px;overflow:hidden;background:linear-gradient(90deg,#28c85a 0 70%,#f0be28 70% 90%,#eb322d 90%)}
.bar i{position:absolute;top:0;right:0;bottom:0;width:100%;background:#26282d;transition:width .3s}
.db{width:58px;text-align:right;font-variant-numeric:tabular-nums}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:9}
body.open #scrim{display:block}
#drawer{position:fixed;top:0;right:0;bottom:0;width:min(470px,100vw);background:#101115;border-left:1px solid var(--line);transform:translateX(102%);
 transition:transform .25s ease;z-index:10;overflow-y:auto;padding:max(16px,env(safe-area-inset-top)) 16px calc(24px + env(safe-area-inset-bottom))}
body.open #drawer{transform:none}
.dh{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
h2{font-size:17px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px}
.ch{display:flex;align-items:center;gap:8px;font-weight:600}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
.in{width:100%;padding:10px;border-radius:7px;border:1px solid #33363d;background:#0b0c0f;color:var(--fg);font:16px ui-monospace,Menlo,Consolas,monospace}
.urlrow{display:flex;gap:6px}
.urlrow .in{flex:1;min-width:0}
.primary{background:#3b6ef5;color:#fff;font-weight:600;height:44px;padding:0 20px}
.primary:hover{background:#4d7bf6}
.note{color:var(--mut);font-size:12.5px}
.err{color:#ff6b6b}
.actions{display:flex;align-items:center;gap:12px;margin:4px 0 14px}
[hidden]{display:none!important}
</style></head><body>
<div id="stage"><img id="mv" alt="Multiview"></div>
<div id="list"></div>
<div id="banner">Connection lost - reconnecting...</div>
<div id="hud">
 <span id="dot" title="Connection"></span>
 <select id="q" class="btn" aria-label="Quality"><option value="auto">Auto</option><option value="hd">HD</option><option value="sd">SD</option><option value="mobile">Mobile</option></select>
 <button class="btn" id="fs" aria-label="Fullscreen" title="Fullscreen (F)"><svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>
 <button class="btn" id="gear" aria-label="Settings" title="Settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button>
</div>
<div id="scrim"></div>
<aside id="drawer" aria-label="Settings">
 <div class="dh"><h2>Settings</h2><button class="btn" id="close" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
 <div id="adminbox" class="card" hidden>
  <div>Enter the admin password to change channels.</div>
  <label for="apw">Admin password</label><input id="apw" class="in" type="password" autocomplete="current-password">
  <div class="actions"><button class="btn primary" id="alogin" type="button">Unlock</button><span id="aerr" class="note err"></span></div>
 </div>
 <form id="cfg" hidden autocomplete="off">
  <div class="card"><label for="caption">Text under the clock</label><input id="caption" class="in" maxlength="80" placeholder="TALLINN LOCAL TIME"></div>
  <div id="chans"></div>
  <div class="actions"><button class="btn primary" type="submit">Save &amp; apply</button><span id="msg" class="note"></span></div>
 </form>
 <div class="card note" id="sysinfo"></div>
 <div id="openwarn" class="card note err" hidden>No password is set: anyone who can reach this page can watch and change the stream keys. Set web.password (or MV_PASSWORD) before exposing it.</div>
 <button class="btn" id="logout" type="button">Sign out</button>
</aside>
<script nonce="{{N}}">
(()=>{
const $=id=>document.getElementById(id), body=document.body;
const LABEL={live:'LIVE',offline:'NO SIGNAL',connecting:'CONNECTING',unset:'NOT SET',standby:'STANDBY'};
const store={get(k,d){try{return localStorage.getItem(k)||d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const portrait=()=>matchMedia('(orientation:portrait) and (max-width:900px)').matches;
let q=store.get('mv.q','auto'); $('q').value=q;
function autoProfile(){
 const coarse=matchMedia('(pointer:coarse)').matches, dpr=window.devicePixelRatio||1;
 if(coarse&&Math.min(screen.width,screen.height)<700) return 'mobile';
 const w=Math.max(innerWidth,innerHeight)*dpr; return w>=1700?'hd':w>=1000?'sd':'mobile';
}
const profile=()=>q==='auto'?autoProfile():q;
let src='';
function startStream(force){
 if(document.hidden) return;
 const s='stream.mjpg?p='+profile();
 if(!force&&s===src) return;
 src=s; $('mv').src=s+'&t='+Date.now();
}
function stopStream(){src='';$('mv').src='data:,';}
$('q').onchange=()=>{q=$('q').value;store.set('mv.q',q);startStream(true)};
addEventListener('resize',()=>{if(q==='auto')startStream(false)});

// toolbar auto-hide (not on phones in portrait, not while settings are open)
let idleT;
function wake(){body.classList.remove('idle');clearTimeout(idleT);
 idleT=setTimeout(()=>{if(!body.classList.contains('open')&&!portrait())body.classList.add('idle')},3000)}
['mousemove','mousedown','touchstart','keydown'].forEach(e=>addEventListener(e,wake,{passive:true})); wake();

// fullscreen
const de=document.documentElement, fsReq=de.requestFullscreen||de.webkitRequestFullscreen;
const fsEl=()=>document.fullscreenElement||document.webkitFullscreenElement;
function toggleFs(){ if(fsEl()){(document.exitFullscreen||document.webkitExitFullscreen).call(document)} else if(fsReq){fsReq.call(de).catch(()=>{})} }
if(!fsReq) $('fs').hidden=true;
$('fs').onclick=toggleFs; $('stage').ondblclick=toggleFs;
addEventListener('keydown',e=>{ if(e.target.tagName==='INPUT')return;
 if(e.key==='f'||e.key==='F')toggleFs(); if(e.key==='Escape')closeDrawer(); });

// keep the screen on while watching
let lock=null;
async function wakeLock(){try{if('wakeLock' in navigator&&!document.hidden)lock=await navigator.wakeLock.request('screen')}catch(e){}}
document.addEventListener('visibilitychange',()=>{ if(document.hidden){stopStream()} else {startStream(true);wakeLock()} });

// status list (phone portrait) + connection watchdog
const rows={};
function row(i){
 let r=rows[i.index]; if(r) return r;
 const el=document.createElement('div'); el.className='row';
 el.innerHTML='<span class="pill"></span><span class="nm"></span><div class="meta"></div>'+
  '<div class="mt">1<div class="bar"><i></i></div><span class="db"></span></div><div class="mt">2<div class="bar"><i></i></div><span class="db"></span></div>';
 $('list').appendChild(el);
 r=rows[i.index]={pill:el.querySelector('.pill'),nm:el.querySelector('.nm'),meta:el.querySelector('.meta'),
  bars:el.querySelectorAll('.bar i'),dbs:el.querySelectorAll('.db')};
 return r;
}
const dur=s=>{const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+'h ':'')+(h||m?m+'m ':'')+x+'s'};
function render(s){
 s.inputs.forEach(i=>{
  const r=row(i); r.pill.className='pill '+i.state; r.pill.textContent=LABEL[i.state]||i.state; r.nm.textContent=i.name;
  let m='';
  if(i.state==='live') m=[i.info,i.audio,i.fps+' fps','live '+dur(i.live_for)].filter(Boolean).join(' · ');
  else if(i.state==='offline') m=(i.error||'No signal')+(i.down_for?' · down '+dur(i.down_for):'')+' · retrying';
  else if(i.state==='standby') m='No stream for '+dur(i.down_for)+' · lights up by itself';
  else if(i.state==='connecting') m='Connecting...';
  else m='No URL set';
  r.meta.textContent=m;
  [0,1].forEach(c=>{const v=i.state==='live'&&i.has_audio?i.levels[c]:null;
   r.bars[c].style.width=(100-(v===null?0:Math.max(0,Math.min(100,(v+60)/60*100))))+'%';
   r.dbs[c].textContent=i.state!=='live'?'':(i.has_audio===false?'no audio':(v===null?'silent':v.toFixed(1)+' dB'))});
 });
 const o=s.output;
 $('sysinfo').textContent='Mode: '+s.mode+' · '+s.rate+' fps · viewers: '+s.viewers+
  ' · network output: '+(o.enabled?(o.type.toUpperCase()+' '+(o.running?'running':'NOT running')+(o.connections!=null?' ('+o.connections+' receivers)':'')):'off');
}
let failed=false, boot=null;
async function poll(){
 try{
  const r=await fetch('api/status',{cache:'no-store'});
  if(r.status===401){location.replace('login');return}
  const s=await r.json();
  if((boot&&s.boot!==boot)||failed) startStream(true);
  boot=s.boot; failed=false; $('banner').classList.remove('show'); $('dot').classList.remove('bad');
  render(s);
 }catch(e){ failed=true; $('banner').classList.add('show'); $('dot').classList.add('bad'); }
}
setInterval(poll,1000); poll();

// settings drawer
function openDrawer(){body.classList.add('open');loadConfig()}
function closeDrawer(){body.classList.remove('open');wake()}
$('gear').onclick=openDrawer; $('close').onclick=closeDrawer; $('scrim').onclick=closeDrawer;
const H={'Content-Type':'application/json','X-MV':'1'};
async function loadConfig(){
 $('msg').textContent='';
 const r=await fetch('api/config',{cache:'no-store'});
 if(r.status===401){location.replace('login');return}
 if(r.status===403){$('adminbox').hidden=false;$('cfg').hidden=true;$('apw').focus();return}
 const c=await r.json(); $('adminbox').hidden=true; $('cfg').hidden=false; $('openwarn').hidden=!c.open;
 $('caption').value=c.caption||'';
 const box=$('chans'); box.textContent='';
 c.inputs.forEach((i,k)=>{
  const d=document.createElement('div'); d.className='card';
  d.innerHTML='<div class="ch"><span></span><span class="pill"></span></div>'+
   '<label>Title shown on the multiview</label><input class="in nm" maxlength="60">'+
   '<label>Stream URL (rtmp://, srt://, https://...m3u8)</label>'+
   '<div class="urlrow"><input class="in url" type="password" spellcheck="false" autocapitalize="off" placeholder="rtmp://server/app/streamkey">'+
   '<button class="btn show" type="button">Show</button></div>';
  d.querySelector('.ch span').textContent='Channel '+(k+1);
  const p=d.querySelector('.pill'); p.className='pill '+i.state; p.textContent=LABEL[i.state]||i.state;
  d.querySelector('.nm').value=i.name; d.querySelector('.url').value=i.url;
  d.querySelector('.show').onclick=e=>{const u=d.querySelector('.url');u.type=u.type==='password'?'text':'password';e.target.textContent=u.type==='password'?'Show':'Hide'};
  box.appendChild(d);
 });
}
$('alogin').onclick=async()=>{
 $('aerr').textContent='';
 const r=await fetch('api/login',{method:'POST',headers:H,body:JSON.stringify({password:$('apw').value})});
 $('apw').value='';
 if(r.ok){loadConfig()} else {const j=await r.json().catch(()=>({}));$('aerr').textContent=j.error||'Error'}
};
$('apw').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('alogin').click()}};
$('cfg').onsubmit=async e=>{
 e.preventDefault();
 const inputs=[...$('chans').children].map(d=>({name:d.querySelector('.nm').value,url:d.querySelector('.url').value}));
 const r=await fetch('api/config',{method:'POST',headers:H,body:JSON.stringify({inputs,caption:$('caption').value})});
 const j=await r.json().catch(()=>({}));
 if(r.status===403){loadConfig();return}
 $('msg').textContent=r.ok?'Saved':('Error: '+(j.error||r.status));
 setTimeout(()=>{$('msg').textContent=''},4000);
};
$('logout').onclick=async()=>{await fetch('api/logout',{method:'POST',headers:H}).catch(()=>{});location.replace('login')};

startStream(true); wakeLock();
})();
</script></body></html>"""

MANIFEST = json.dumps({"name": "RTMP Multiview", "short_name": "Multiview", "start_url": "./",
                       "display": "fullscreen", "background_color": "#000000",
                       "theme_color": "#000000"}).encode()


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------

def make_handler(app, auth, proxies, tls):
    wcfg = app.cfg["web"]
    max_viewers = int(wcfg.get("max_viewers") or 20)

    class H(BaseHTTPRequestHandler):
        server_version = "multiview"
        sys_version = ""
        timeout = 30                      # socket timeout while reading requests (slowloris)

        def log_message(self, *a):
            pass

        # -- helpers --------------------------------------------------------
        def client_ip(self):
            ip = self.client_address[0]
            if proxies.trusted(ip):
                xff = self.headers.get("X-Forwarded-For", "")
                if xff:
                    return xff.split(",")[-1].strip() or ip
            return ip

        def https(self):
            return tls or (proxies.trusted(self.client_address[0])
                           and self.headers.get("X-Forwarded-Proto", "").lower() == "https")

        def _common_headers(self, nonce=None):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            n = nonce or "none"
            self.send_header("Content-Security-Policy",
                             f"default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; "
                             "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
                             "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            if self.https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")

        def _send(self, code, body, ctype, extra=None, nonce=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self._common_headers(nonce)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj).encode(), "application/json", extra)

        def _page(self, html):
            nonce = secrets.token_urlsafe(16)
            self._send(200, html.replace("{{N}}", nonce).encode(), "text/html; charset=utf-8",
                       nonce=nonce)

        def _redirect(self, where):
            self._send(303, b"", "text/plain", {"Location": where})

        def _token(self):
            from http.cookies import SimpleCookie
            try:
                c = SimpleCookie(self.headers.get("Cookie", ""))
                m = c.get(Auth.COOKIE)
                return m.value if m else None
            except Exception:
                return None

        def _cookie(self, token, max_age):
            attrs = f"{Auth.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            if self.https():
                attrs += "; Secure"
            return attrs

        def _session(self):
            """-> session dict, or None. Open mode: a pseudo session."""
            if auth.open:
                return {"admin_until": float("inf")}
            return auth.session(self._token())

        def _same_origin(self):
            # custom header (forces a CORS preflight that we never answer) + Origin check
            if self.headers.get("X-MV") != "1":
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = self.headers.get("X-Forwarded-Host") if proxies.trusted(self.client_address[0]) else None
                host = host or self.headers.get("Host", "")
                if origin.split("://", 1)[-1].rstrip("/") != host:
                    return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("request too large")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, (dict, list)):
                raise ValueError("bad request")
            return data

        # -- routes ---------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if path == "/manifest.webmanifest":
                return self._send(200, MANIFEST, "application/manifest+json")
            if path in ("/favicon.ico", "/robots.txt"):
                return self._send(200 if path == "/robots.txt" else 204,
                                  b"User-agent: *\nDisallow: /\n" if path == "/robots.txt" else b"",
                                  "text/plain")
            s = self._session()
            if path == "/login":
                return self._redirect("./") if s else self._page(LOGIN_PAGE)
            if path in ("/", "/index.html"):
                return self._page(VIEWER_PAGE) if s else self._redirect("login")
            if not s:
                return self._json(401, {"error": "login required"})
            if path == "/stream.mjpg":
                return self._stream()
            if path == "/snapshot.jpg":
                return self._send(200, app.snapshot_jpeg(), "image/jpeg")
            if path == "/api/status":
                return self._json(200, app.status())
            if path == "/api/config":
                if not auth.is_admin(s, touch=True):
                    return self._json(403, {"error": "admin password required"})
                return self._json(200, app.config_view(auth))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._same_origin():
                return self._json(403, {"error": "cross-site request refused"})
            try:
                if path == "/api/login":
                    body = self._body()
                    pw = body.get("password") if isinstance(body, dict) else None
                    if auth.open:
                        return self._json(200, {"ok": True})
                    token, err = auth.login(pw if isinstance(pw, str) else "", self.client_ip(),
                                            old_token=self._token())
                    if not token:
                        return self._json(429 if "Too many" in err else 401, {"error": err})
                    return self._json(200, {"ok": True},
                                      {"Set-Cookie": self._cookie(token, int(auth.session_s))})
                if path == "/api/logout":
                    auth.logout(self._token())
                    return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
                s = self._session()
                if not s:
                    return self._json(401, {"error": "login required"})
                if path in ("/api/config", "/api/inputs"):
                    if not auth.is_admin(s, touch=True):
                        return self._json(403, {"error": "admin password required"})
                    body = self._body()
                    caption = None
                    if isinstance(body, dict):
                        items, caption = body.get("inputs", []), body.get("caption")
                    else:
                        items = body
                    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                        raise ValueError("expected a list of {name, url}")
                    app.save_inputs(items, caption)
                    log.info("web: settings changed from %s", self.client_ip())
                    return self._json(200, {"ok": True})
                self._send(404, b"not found", "text/plain")
            except (ValueError, json.JSONDecodeError) as e:
                self._json(400, {"error": str(e)})

        def _stream(self):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            want = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("p", "")
            ps = app.previews.get(want) or app.previews.get("sd") or next(iter(app.previews.values()), None)
            if ps is None:
                return self._send(404, b"no stream profiles configured", "text/plain")
            if sum(p.viewers for p in app.previews.values()) >= max_viewers:
                return self._send(503, b"too many viewers", "text/plain")
            token = self._token()
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=mvframe")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Accel-Buffering", "no")       # nginx: don't buffer
            self.send_header("Connection", "close")
            self._common_headers()
            self.end_headers()
            ps.acquire()
            try:
                self.connection.settimeout(20)
                last, checked = -1, time.monotonic()
                while not app.stopping.is_set():
                    seq, jpg = ps.wait_next(last, 5.0)
                    now = time.monotonic()
                    if now - checked > 10:                  # stop when logged out / expired
                        checked = now
                        if not auth.open and not auth.session(token, touch=False):
                            break
                    if jpg is None or seq == last:
                        continue
                    last = seq
                    self.wfile.write(b"--mvframe\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                ps.release()

    return H


def main():
    import argparse
    ap = argparse.ArgumentParser(description="RTMP Multiview: 4 stream pulls -> 2x2 grid -> NDI/OMT")
    default_cfg = os.environ.get("MV_CONFIG") or DEFAULT_CONFIG_PATH
    ap.add_argument("config", nargs="?", default=default_cfg,
                    help=f"config file (default {default_cfg}, created if missing)")
    ap.add_argument("--hash-password", action="store_true",
                    help="ask for a password, print its hash for web.password / MV_PASSWORD, and exit")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--display", dest="display", action="store_true", default=None,
                   help="show the multiview in a window (needs a desktop)")
    g.add_argument("--no-display", "--headless", dest="display", action="store_false",
                   help="no window, run in the background / on a server")
    ap.add_argument("--fullscreen", action="store_true", help="window in fullscreen (implies --display)")
    ap.add_argument("--no-output", action="store_true", help="don't send NDI/OMT, ignore output.enabled")
    args = ap.parse_args()
    if args.hash_password:
        import getpass
        pw = getpass.getpass("New web password: ")
        if len(pw) < 8:
            sys.exit("Use at least 8 characters (better: a long passphrase).")
        if getpass.getpass("Repeat: ") != pw:
            sys.exit("The passwords don't match.")
        print(hash_password(pw))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    overrides = {}
    if args.fullscreen:
        overrides[("display", "fullscreen")] = True
        overrides[("display", "enabled")] = True
    if args.display is not None:
        overrides[("display", "enabled")] = args.display
    if args.no_output:
        overrides[("output", "enabled")] = False
    path = args.config
    log.info("config: %s", os.path.abspath(path))
    app = App(path, overrides)

    def stop(*_):
        app.stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    w = app.cfg["web"]
    auth = Auth(w)
    if auth.open:
        if os.environ.get("MV_REQUIRE_PASSWORD") == "1":
            log.error("No web password set. Set MV_PASSWORD (plain or a hash from --hash-password) "
                      "or web.password in the config, or MV_REQUIRE_PASSWORD=0 for an open LAN-only setup.")
            sys.exit(2)
        if w.get("bind", "0.0.0.0") not in ("127.0.0.1", "::1", "localhost"):
            log.warning("web UI has NO PASSWORD - anyone who can reach port %s can watch and change "
                        "stream keys. Set web.password before exposing it.", w.get("port"))
    for name, val in (("password", auth.view_pw), ("admin_password", auth.admin_pw)):
        if val and not val.startswith("pbkdf2_sha256$"):
            log.warning("web %s is stored in plain text; consider a hash from --hash-password", name)
    tp = os.environ.get("MV_TRUSTED_PROXIES")
    proxies = TrustedProxies(tp.split(",") if tp else (w.get("trusted_proxies") or []))
    if w.get("port"):
        try:
            srv = ThreadingHTTPServer((w.get("bind", "0.0.0.0"), int(w["port"])),
                                      make_handler(app, auth, proxies, bool(w.get("tls_cert"))))
            srv.daemon_threads = True
            scheme = "http"
            if w.get("tls_cert") and w.get("tls_key"):
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(w["tls_cert"], w["tls_key"])
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
                scheme = "https"
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            log.info("web UI on %s://%s:%s/ (%s)", scheme, w.get("bind", "0.0.0.0"), w["port"],
                     "no login" if auth.open else "login required")
        except (OSError, ssl.SSLError) as e:
            log.error("web UI disabled - cannot listen on port %s: %s", w["port"], e)
    app.run()


if __name__ == "__main__":
    main()
