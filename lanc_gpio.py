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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import pigpio
except ImportError:
    raise SystemExit("pip install pigpio (and run pigpiod)")

BIT_US = 104            # LANC bit time
START_LO_US = 1200      # valid frame start-bit low pulse
START_HI_US = 1500
ONE_SHOT_FRAMES = 5     # frames a one-shot command is repeated
HOLD_TIMEOUT_FRAMES = 240  # ~5s safety for hold commands

COMMANDS = {
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
        if START_LO_US <= width <= START_HI_US:
            self._start_event.set()

    # ---- waveform for the 2 command bytes -----------------------------------
    def _build_wave(self):
        seq = []
        for byte in self.cmd:
            seq.append((0, self.gpio, BIT_US))            # start bit: low
            for i in range(8):
                bit = 1 if byte & (1 << i) else 0
                seq.append((self.gpio if bit else 0,
                            self.gpio if not bit else 0, BIT_US))
            seq.append((0, self.gpio, BIT_US))            # stop: high (diode blocks)
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
        b = 0
        for i in range(8):
            time.sleep(BIT_US / 2 / 1e6)
            if self.pi.read(self.gpio):
                b |= (1 << i)
            time.sleep(BIT_US / 2 / 1e6)
        time.sleep(BIT_US / 1e6)  # stop bit
        return b

    # ---- frame engine --------------------------------------------------------
    def _loop(self):
        while True:
            if not self._start_event.wait(timeout=2.0):
                self.connected = False
                continue
            self._start_event.clear()
            self.connected = True

            with self._lock:
                c0, c1 = self.cmd
                frames_left = self.cmd_frames_left

            wid = self._wave if self._wave_cmd == (c0, c1) else self._build_wave()
            self.pi.wave_send_once(wid)
            # wave duration ~ 20 * 104us = 2.1 ms
            t_end = time.monotonic() + 0.0024
            while time.monotonic() < t_end and self.pi.wave_tx_busy():
                time.sleep(0.0002)
            self.pi.set_mode(self.gpio, pigpio.INPUT)

            frame = [c0, c1] + [self._recv_byte() for _ in range(6)]
            self.last_frame = frame
            self.recording = (frame[5] & 0xF0) == 0x30

            if frames_left:
                with self._lock:
                    self.cmd_frames_left = frames_left - 1
                    if self.cmd_frames_left == 0:
                        self.cmd = [0x00, 0x00]

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
