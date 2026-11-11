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
import hmac
import io
import json
import logging
import math
import os
import re
import signal
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
        "enabled": True,               # false = don't send anything to the network
        "type": "ndi",
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
    "web": {"bind": "0.0.0.0", "port": 8080, "password": ""},
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
        self.frame = None            # latest UYVY tile (np array)
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
            self.frame = None
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

    def levels(self):
        """Current peak dBFS per channel; -inf when silent/stale."""
        if time.monotonic() - self.audio_t > 0.6:
            return [-math.inf, -math.inf]
        return list(self.peak)

    def status(self):
        now = time.monotonic()
        lv = self.levels() if self.state == STATE_LIVE else [-math.inf, -math.inf]
        return {
            "index": self.idx + 1,
            "name": self.name,
            "url": self.url,
            "state": self.state,
            "info": self.info,
            "audio": self.audio_info if self.has_audio else ("no audio" if self.has_audio is False else ""),
            "has_audio": self.has_audio,
            "levels": [round(x, 1) if x > -100 else None for x in lv],
            "fps": round(self.measured_fps, 1),
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
                self.frame = np.frombuffer(buf, np.uint8).reshape(shape)
                self.last_frame_t = time.monotonic()
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
            self.last_error = self._reason(was_live, rc)
            if was_live or not self.failed:
                log.info("[%d] %s: %s", self.idx + 1, "signal lost" if was_live else "no signal",
                         self.last_error)
            self._fail(was_live)

    def _fail(self, was_live):
        now = time.monotonic()
        if was_live or not self.failed or not self.down_since:
            self.down_since = now
        self.failed = True
        self.frame = None
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
            tile = inp.frame if state == STATE_LIVE else None
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
                    detail, sub = "Waiting for stream…", ""
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

    def _rate(self, sinks):
        """Frame rate for this frame: full while anything is live, idle_fps when every feed is
        empty, and just enough for the web preview when nothing is being sent anywhere."""
        if not sinks:
            return min(self.fps, 5.0), "preview only (no output)"
        idle = float(self.cfg.get("idle_fps") or 0)
        if idle <= 0 or any(i.state == STATE_LIVE for i in self.inputs):
            return self.fps, "live"
        sb = float(self.cfg.get("standby_fps") or 0)
        if sb > 0 and all(i.state in (STATE_STANDBY, STATE_UNSET) for i in self.inputs):
            return min(self.fps, idle, sb), "standby (power saving)"
        return min(self.fps, idle), "idle (no live feeds)"

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
            rate, self.mode = self._rate(sinks)
            period = 1.0 / rate
            try:
                self.compose()
            except Exception:
                log.exception("compose failed")
            if sinks:
                f = self.frame.copy()            # one copy, shared read-only by all outputs
                for s in sinks:
                    s.write(f)
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

    def status(self):
        o, d = self.output, self.display
        rate, _ = self._rate([s for s in (o, d) if s and s.active])
        return {
            "mode": self.mode, "rate": rate,
            "display": {"enabled": d is not None, "running": bool(d and d.active and d.ok),
                        "error": d.error if d else ""},
            "output": {"type": o.type, "name": self.cfg["output"]["name"],
                       "size": f"{self.layout.W}x{self.layout.H}", "fps": self.fps,
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
# Web UI
# ----------------------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RTMP Multiview</title><style>
:root{--bg:#0f1012;--card:#1a1b1f;--line:#2b2d33;--fg:#e8e8ea;--mut:#9a9ca3;
--live:#1f9d55;--off:#c0292b;--con:#d68910;--unset:#5a5a5a}
*{box-sizing:border-box}body{margin:0 auto;background:var(--bg);color:var(--fg);
font:14px/1.4 system-ui,sans-serif;padding:16px;max-width:1100px}
h1{font-size:18px;margin:0 0 4px}#out{color:var(--mut);margin-bottom:12px;font-size:13px}
#out b.bad{color:#ff6b6b}
img{width:100%;border-radius:6px;background:#000;display:block;aspect-ratio:16/9}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.hd{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.pill{font-size:11px;font-weight:700;padding:2px 7px;border-radius:4px;color:#fff}
.live{background:var(--live)}.offline{background:var(--off)}.connecting{background:var(--con)}.unset{background:var(--unset)}.standby{background:#345680}
.meta{color:var(--mut);font-size:12px;min-height:17px;word-break:break-word}
.mt{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:11px;color:var(--mut)}
.bar{flex:1;height:8px;background:#26282d;border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#28c85a 0 70%,#f0be28 70% 90%,#eb322d 90%)}
.db{width:56px;text-align:right;font-variant-numeric:tabular-nums}
label{display:block;font-size:12px;color:var(--mut);margin-top:8px}
input{width:100%;padding:8px;border-radius:5px;border:1px solid var(--line);
background:#111215;color:var(--fg);font:13px ui-monospace,monospace}
button{margin-top:14px;padding:10px 18px;border:0;border-radius:6px;background:#3b6ef5;
color:#fff;font-weight:600;cursor:pointer}#msg{margin-left:10px;color:var(--mut)}
</style></head><body>
<h1>RTMP Multiview</h1><div id="out">…</div>
<img id="snap" src="snapshot.jpg" alt="preview">
<form id="f"><div class="card" style="margin-top:14px"><label style="margin-top:0">Text under the clock (e.g. TALLINN LOCAL TIME, STILL FRAME TALLINN PLAYOUT; empty = clock only)</label>
<input name="caption" id="caption" maxlength="80" placeholder="TALLINN LOCAL TIME"></div>
<div class="grid" id="cards"></div>
<button type="submit">Save &amp; apply</button><span id="msg"></span></form>
<script>
const cards=document.getElementById('cards');let built=false;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function dur(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+'h ':'')+(h||m?m+'m ':'')+x+'s'}
const pct=db=>db===null?0:Math.max(0,Math.min(100,(db+60)/60*100));
async function poll(){
 try{const r=await fetch('api/status');const s=await r.json();const o=s.output;
 const run=o.running?'running':`<b class="bad">NOT RUNNING${o.error?': '+esc(o.error):''}</b>`;
 const con=o.connections===null||o.connections===undefined?'':` · ${o.connections} receiver${o.connections==1?'':'s'}`;
 const net=(o.type=='off'||o.type=='null')?'Network output OFF':`${esc(o.type.toUpperCase())} "${esc(o.name)}" · ${run}${con}`;
 const disp=s.display.enabled?` · window ${s.display.running?'open':'<b class="bad">not running</b>'}`:'';
 document.getElementById('out').innerHTML=`${net}${disp} · ${o.size} · ${esc(s.mode)} → ${s.rate} fps · render ${o.render_ms} ms · dropped ${o.dropped}`;
 if(!built)document.getElementById('caption').value=s.caption||'';
 s.inputs.forEach((i,k)=>{
  if(!built){cards.insertAdjacentHTML('beforeend',`<div class="card"><div class="hd"><b>${i.index}</b>
   <span class="pill" id="p${k}"></span></div><div class="meta" id="m${k}"></div>
   <div class="mt">1<div class="bar"><i id="a${k}0"></i></div><span class="db" id="d${k}0"></span></div>
   <div class="mt">2<div class="bar"><i id="a${k}1"></i></div><span class="db" id="d${k}1"></span></div>
   <label>Title shown on the multiview</label><input name="name${k}" value="${esc(i.name)}" maxlength="60">
   <label>Stream URL (rtmp://, https://…m3u8, srt://)</label><input name="url${k}" value="${esc(i.url)}" placeholder="rtmp://server/app/streamkey" autocomplete="off" spellcheck="false"></div>`)}
  const p=document.getElementById('p'+k);p.className='pill '+i.state;
  p.textContent={live:'LIVE',offline:'NO SIGNAL',connecting:'CONNECTING',unset:'NOT SET',standby:'STANDBY'}[i.state];
  let m='';
  if(i.state=='live')m=`${i.info}${i.audio?' · '+i.audio:''} · ${i.fps} fps · live ${dur(i.live_for)}`;
  else if(i.state=='offline')m=`${i.error||'No signal'}${i.down_for?' · down '+dur(i.down_for):''} · retrying`;
  else if(i.state=='standby')m=`No stream for ${dur(i.down_for)} · checking every ${s.standby_retry}s, lights up by itself`;
  else if(i.state=='connecting')m='Connecting…';
  document.getElementById('m'+k).textContent=m;
  [0,1].forEach(c=>{const v=i.state=='live'&&i.has_audio?i.levels[c]:null;
   document.getElementById(`a${k}${c}`).style.width=pct(v)+'%';
   document.getElementById(`d${k}${c}`).textContent=i.state!='live'?'':(i.has_audio===false?'no audio':(v===null?'silent':v.toFixed(1)+' dB'));});
 });built=true;}catch(e){document.getElementById('out').textContent='Lost connection to the multiview service…'}
}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);
 const items=[0,1,2,3].map(k=>({name:fd.get('name'+k),url:fd.get('url'+k)}));
 const msg=document.getElementById('msg');
 const body={inputs:items,caption:fd.get('caption')||''};
 try{const r=await fetch('api/inputs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json();msg.textContent=r.ok?'Saved':('Error: '+(j.error||r.status));}catch(err){msg.textContent='Error: '+err}
 setTimeout(()=>msg.textContent='',4000)};
const snap=document.getElementById('snap');let loading=false;
snap.onload=snap.onerror=()=>{loading=false};
setInterval(()=>{if(!loading&&!document.hidden){loading=true;snap.src='snapshot.jpg?'+Date.now()}},250);
setInterval(poll,500);poll();
</script></body></html>"""


def make_handler(app):
    password = str(app.cfg["web"].get("password") or "")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not password:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode("utf-8", "replace").partition(":")
                    if hmac.compare_digest(pw.encode(), password.encode()):
                        return True
                except Exception:
                    pass
            self._send(401, b"login required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="RTMP Multiview"'})
            return False

        def do_GET(self):
            if not self._authorized():
                return
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif p == "/snapshot.jpg":
                self._send(200, app.snapshot_jpeg(), "image/jpeg")
            elif p == "/api/status":
                self._send(200, json.dumps(app.status()).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._authorized():
                return
            if self.path.split("?")[0] != "/api/inputs":
                return self._send(404, b"not found", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 100000)))
                caption = None
                if isinstance(body, dict):          # {"inputs": [...], "caption": "..."}
                    items, caption = body.get("inputs", []), body.get("caption")
                else:                               # plain list (older UI)
                    items = body
                if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                    raise ValueError("expected a list of {name, url}")
                app.save_inputs(items, caption)
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
    return H


def main():
    import argparse
    ap = argparse.ArgumentParser(description="RTMP Multiview: 4 stream pulls -> 2x2 grid -> NDI/OMT")
    ap.add_argument("config", nargs="?", default=DEFAULT_CONFIG_PATH,
                    help=f"config file (default {DEFAULT_CONFIG_PATH}, created if missing)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--display", dest="display", action="store_true", default=None,
                   help="show the multiview in a window (needs a desktop)")
    g.add_argument("--no-display", "--headless", dest="display", action="store_false",
                   help="no window, run in the background / on a server")
    ap.add_argument("--fullscreen", action="store_true", help="window in fullscreen (implies --display)")
    ap.add_argument("--no-output", action="store_true", help="don't send NDI/OMT, ignore output.enabled")
    args = ap.parse_args()

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
    if w.get("port"):
        try:
            srv = ThreadingHTTPServer((w.get("bind", "0.0.0.0"), int(w["port"])), make_handler(app))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            log.info("web UI on http://%s:%s/", w.get("bind", "0.0.0.0"), w["port"])
        except OSError as e:
            log.error("web UI disabled - cannot listen on port %s: %s", w["port"], e)
    app.run()


if __name__ == "__main__":
    main()
