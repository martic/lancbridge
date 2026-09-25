#!/usr/bin/env python3
"""lanc_gpio.py — LANC control for Sony HXR-MC2500 directly from Raspberry Pi
GPIO (no Arduino). Same HTTP API as lancd.py, so Bitfocus Companion wiring is
identical.

Requires pigpiod running. Bookworm: pigpio is not in apt - see SPEC.md install section.

Wiring:  LANC plug tip --[1k]--|<-- GPIO17 (diode cathode toward the plug)
         LANC sleeve -- Pi GND

Run:  sudo python3 lanc_gpio.py --gpio 17 [--listen 127.0.0.1:8787]
"""

import argparse
import json
import threading
import time
def _thread_excepthook(args):
    import traceback
    print("LANC THREAD CRASH:", file=__import__("sys").stderr)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    __import__("sys").stderr.flush()
threading.excepthook = _thread_excepthook
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import pigpio
except ImportError:
    raise SystemExit("pip install pigpio (and run pigpiod)")

BIT_US = 104            # LANC bit time
BIT_LO_LO_US = 60       # a single data-bit low
BIT_LO_HI_US = 300
FRAME_MIN_GAP_US = 5000 # idle between frames ~10ms
ONE_SHOT_FRAMES = 5     # frames a one-shot command is repeated
HOLD_TIMEOUT_FRAMES = 240  # ~5s safety for hold commands

COMMANDS = {
    "stop": (0x00, 0x00),
    "rec": (0x18, 0x33), "poweroff": (0x18, 0x5E), "display": (0x18, 0xB4),
    "aftoggle": (0x28, 0x41),
    "zoomin": (0x28, 0x35), "zoomout": (0x28, 0x37),
    "zoomin_fast": (0x28, 0x39), "zoomout_fast": (0x28, 0x3B),
    "focusnear": (0x28, 0x47), "focusfar": (0x28, 0x45),
    "irisopen": (0x28, 0x55), "irisclose": (0x28, 0x54),
}
ONE_SHOTS = {"rec", "poweroff", "display", "aftoggle"}
HOLDS = {
    ("zoom", "in", "slow"): "zoomin", ("zoom", "out", "slow"): "zoomout",
    ("zoom", "in", "fast"): "zoomin_fast", ("zoom", "out", "fast"): "zoomout_fast",
    ("focus", "near", None): "focusnear", ("focus", "far", None): "focusfar",
    ("iris", "open", None): "irisopen", ("iris", "close", None): "irisclose",
}


class LancGpio:
    def __init__(self, pi: pigpio.pi, gpio: int):
        self.pi = pi
        self.gpio = gpio
        self.cmd = [0x00, 0x00]
        self.cmd_frames_left = 0
        self.last_frame = None
        self.recording = False
        self.connected = False
        self._lock = threading.Lock()
        self._start_event = threading.Event()
        self._last_fall = None
        self._last_frame_tick = None
        self._start_mono = None
        self._period = 19000
        self._wave = None
        self._wave_cmd = None
        pi.set_mode(gpio, pigpio.INPUT)
        pi.set_pull_up_down(gpio, pigpio.PUD_UP)
        pi.callback(gpio, pigpio.EITHER_EDGE, self._edge)
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- start-bit detection ------------------------------------------------
    def _edge(self, gpio, level, tick):
        if level == 0:
            self._last_fall = tick
            return
        # rising edge: measure low pulse width (handles 32-bit tick wrap)
        width = (tick - self._last_fall) & 0xFFFFFFFF if self._last_fall is not None else 0
        # measured on this camera: frames repeat every ~20ms; a data-bit low is
        # ~104us. The frame START is the 1-bit low that follows the long idle
        # (>5ms high). Key on: short low + preceding idle gap.
        if BIT_LO_LO_US <= width <= BIT_LO_HI_US and self._last_fall is not None:
            if self._last_frame_tick is None:
                self._last_frame_tick = self._last_fall
                self._start_mono = time.monotonic()
                self._start_event.set()
            else:
                gap = (self._last_fall - self._last_frame_tick) & 0xFFFFFFFF
                if gap >= FRAME_MIN_GAP_US:
                    if 15000 < gap < 25000:
                        self._period = gap
                    self._last_frame_tick = self._last_fall
                    self._start_mono = time.monotonic()
                    self._start_event.set()

    # ---- waveform for the 2 command bytes -----------------------------------
    def _build_wave(self):
        MASK = 1 << self.gpio
        seq = []
        for byte in self.cmd:
            seq.append((0, MASK, BIT_US))            # start bit: low
            for i in range(8):
                bit = 1 if byte & (1 << i) else 0
                seq.append((MASK if bit else 0,
                            0 if bit else MASK, BIT_US))
            seq.append((0, MASK, BIT_US))            # stop: high (diode blocks)
        self.pi.wave_clear()
        self.pi.wave_add_generic([
            pigpio.pulse(gpio_on, gpio_off, delay) for gpio_on, gpio_off, delay in seq
        ])
        wid = self.pi.wave_create()
        self._wave_cmd = tuple(self.cmd)
        self._wave = wid
        return wid

    # ---- receive one byte (camera driving; we sample mid-bit) ---------------
    def _recv_byte(self):
        # Called positioned at the START of a byte's start bit.
        # LANC byte: 1 start(0) + 8 data LSB-first + 1 stop(1) = 10 bit times.
        b = 0
        time.sleep(BIT_US * 1.5 / 1e6)   # skip start bit, land mid data-bit 0
        for i in range(8):
            if self.pi.read(self.gpio):
                b |= (1 << i)
            time.sleep(BIT_US / 1e6)
        time.sleep(BIT_US * 0.5 / 1e6)   # finish last data bit
        time.sleep(BIT_US * 1.0 / 1e6)   # stop bit + next byte's start bit
        return b

    # ---- frame engine --------------------------------------------------------
    def _loop(self):
        while True:
            if not self._start_event.wait(timeout=2.0):
                self.connected = False
                continue
            self._start_event.clear()
            self.connected = True

            # The frame-start edge was detected by the callback thread; by the
            # time we wake, we are mid-frame. Schedule the send at the NEXT
            # frame start so our 2 bytes replace the remote slot cleanly.
            elapsed = (time.monotonic() - self._start_mono) * 1e6
            # never fire mid-frame: if we woke even slightly late, wait for the
            # next frame start (a 2-byte wave needs to begin at bit 0 exactly)
            k = max(1, int(elapsed // self._period) + (0 if elapsed <= self._period * 0.3 else 1)) if elapsed > 300 else 0
            delay_us = k * self._period - elapsed
            if delay_us > 0:
                time.sleep(delay_us / 1e6)

            with self._lock:
                c0, c1 = self.cmd
                frames_left = self.cmd_frames_left

            wid = self._wave if self._wave_cmd == (c0, c1) else self._build_wave()
            self.pi.set_mode(self.gpio, pigpio.OUTPUT)
            self.pi.wave_send_once(wid)
            # wave duration ~ 20 * 104us = 2.1 ms; camera then drives bytes 2-7
            t_end = time.monotonic() + 0.00208
            while time.monotonic() < t_end and self.pi.wave_tx_busy():
                time.sleep(0.0002)
            self.pi.set_mode(self.gpio, pigpio.INPUT)
            # we are now at bit 20 = camera's byte-2 start bit

            frame = [c0, c1] + [self._recv_byte() for _ in range(6)]
            self.last_frame = frame
            self.recording = (frame[5] & 0xF0) == 0x30

            if frames_left:
                with self._lock:
                    self.cmd_frames_left = frames_left - 1
                    if self.cmd_frames_left == 0:
                        self.cmd = [0x00, 0x00]

    def send_raw(self, c0, c1, frames=8):
        with self._lock:
            self.cmd = [c0 & 0xFF, c1 & 0xFF]
            self.cmd_frames_left = max(1, frames)

    # ---- command dispatch -----------------------------------------------------
    def dispatch(self, action=None, kind=None, direction=None, speed="slow", state="on"):
        if kind:
            key = (kind, direction, speed if kind == "zoom" else None)
            name = HOLDS.get(key)
            if not name:
                return {"error": f"unknown {kind} {direction} {speed}"}
            if state == "off":
                name = "stop"
            self.send(name)
            return {"sent": name, "state": state}
        if action == "stop":
            self.send("stop")
            return {"sent": "stop"}
        if action in COMMANDS:
            self.send(action)
            return {"sent": action}
        return {"error": "no action"}

    def send(self, name):
        with self._lock:
            self.cmd = list(COMMANDS[name])
            self.cmd_frames_left = ONE_SHOT_FRAMES if name in ONE_SHOTS else HOLD_TIMEOUT_FRAMES


def build_server(lanc: LancGpio):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/rec":
                self._json(lanc.dispatch(action="rec"))
            elif u.path == "/zoom":
                self._json(lanc.dispatch(kind="zoom", direction=q.get("dir"),
                                         speed=q.get("speed", "slow"),
                                         state=q.get("state", "on")),
                           200 if q.get("dir") else 400)
            elif u.path == "/focus":
                self._json(lanc.dispatch(kind="focus", direction=q.get("dir"),
                                         state=q.get("state", "on")),
                           200 if q.get("dir") else 400)
            elif u.path == "/iris":
                self._json(lanc.dispatch(kind="iris", direction=q.get("dir"),
                                         state=q.get("state", "on")),
                           200 if q.get("dir") else 400)
            elif u.path == "/stop":
                self._json(lanc.dispatch(action="stop"))
            elif u.path == "/cmd":
                self._json(lanc.dispatch(action=q.get("action")))
            elif u.path == "/raw":
                try:
                    c0 = int(q["c0"], 16); c1 = int(q["c1"], 16)
                    frames = int(q.get("frames", 8))
                except (KeyError, ValueError):
                    self._json({"error": "usage: /raw?c0=28&c1=35&frames=8"}, 400)
                    return
                lanc.send_raw(c0, c1, frames)
                self._json({"sent": f"{c0:02x}{c1:02x}", "frames": frames})
            elif u.path == "/status":
                self._json({"recording": lanc.recording,
                            "connected": lanc.connected,
                            "last_frame": lanc.last_frame})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if self.path == "/rec":
                self._json(lanc.dispatch(action="rec"))
            elif self.path == "/zoom":
                self._json(lanc.dispatch(kind="zoom", direction=data.get("dir"),
                                         speed=data.get("speed", "slow"),
                                         state=data.get("state", "on")))
            elif self.path == "/focus":
                self._json(lanc.dispatch(kind="focus", direction=data.get("dir"),
                                         state=data.get("state", "on")))
            elif self.path == "/iris":
                self._json(lanc.dispatch(kind="iris", direction=data.get("dir"),
                                         state=data.get("state", "on")))
            elif self.path == "/cmd":
                self._json(lanc.dispatch(action=data.get("action")))
            else:
                self._json({"error": "not found"}, 404)

        def log_message(self, fmt=None, *args, **kwargs):
            pass

    return Handler


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpio", type=int, default=17, help="BCM pin number")
    ap.add_argument("--listen", default="127.0.0.1:8787")
    args = ap.parse_args()

    pi = pigpio.pi()
    if not pi.connected:
        raise SystemExit("pigpiod not running: sudo systemctl start pigpiod")

    lanc = LancGpio(pi, args.gpio)
    host, port = args.listen.rsplit(":", 1)
    print(f"lanc_gpio on BCM{args.gpio}, HTTP {args.listen}")
    ThreadingHTTPServer((host, int(port)), build_server(lanc)).serve_forever()
