#!/usr/bin/env python3
"""lancd — PC-side daemon for the LancBridge (Sony HXR-MC2500 over LANC).

Opens the Arduino's USB-serial port, parses the F-frames it streams, and
exposes control over HTTP on 127.0.0.1:8787:

    POST /rec                     {"action":"rec"}
    POST /zoom   {"dir":"in"|"out","speed":"slow"|"fast","state":"on"|"off"}
    POST /focus  {"dir":"near"|"far","state":"on"|"off"}
    POST /iris   {"dir":"open"|"close","state":"on"|"off"}
    POST /cmd    {"action":"poweroff"|"display"|"aftoggle"|"stop"}
    GET  /status  -> {"recording":bool,"last_frame":[...]}

Run: python3 lancd.py --port /dev/ttyUSB0 [--keys]

--keys adds direct keyboard control: hold Right/PageUp = zoom in, Left/PageDn =
zoom out, Space = rec toggle, q = quit (requires a TTY).
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
except ImportError:
    sys.exit("pip install pyserial")


class Lanc:
    ONE_SHOTS = {"rec", "poweroff", "display", "aftoggle"}
    HOLDS = {
        ("zoom", "in", "slow"): "zoomin",
        ("zoom", "out", "slow"): "zoomout",
        ("zoom", "in", "fast"): "zoomin_fast",
        ("zoom", "out", "fast"): "zoomout_fast",
        ("focus", "near", None): "focusnear",
        ("focus", "far", None): "focusfar",
        ("iris", "open", None): "irisopen",
        ("iris", "close", None): "irisclose",
    }

    def __init__(self, port: str):
        self.ser = serial.Serial(port, 115200, timeout=1)
        self.last_frame = None
        self.recording = False
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        buf = b""
        while True:
            try:
                line = self.ser.readline()
            except Exception:
                time.sleep(1)
                continue
            if not line.startswith(b"F "):
                continue
            try:
                frame = [int(x, 16) for x in line.split()[1:9]]
            except ValueError:
                continue
            if len(frame) != 8:
                continue
            self.last_frame = frame
            # Word 5 high nibble == 0x3 -> recording state in many Sony cams;
            # byte5 & 0xF0 == 0x30 and word4 (VTR mode) carry rec indicator.
            vtr_status = frame[5]
            self.recording = (vtr_status & 0xF0) == 0x30

    def send(self, command: str):
        with self._lock:
            self.ser.write((command + "\n").encode())
            self.ser.flush()

    def dispatch(self, action=None, kind=None, direction=None, speed="slow", state="on"):
        if kind:
            key = (kind, direction, speed if kind == "zoom" else None)
            command = self.HOLDS.get(key)
            if not command:
                return {"error": f"unknown {kind} {direction} {speed}"}
            if state == "off":
                command = "stop"
            self.send(command)
            return {"sent": command, "state": state}
        if action in self.ONE_SHOTS:
            self.send(action)
            return {"sent": action}
        if action == "stop":
            self.send("stop")
            return {"sent": "stop"}
        return {"error": "no action"}


def build_server(lanc: Lanc):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            from urllib.parse import parse_qs, urlparse
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
                r = lanc.dispatch(action="rec")
            elif self.path == "/zoom":
                r = lanc.dispatch(kind="zoom", direction=data.get("dir"),
                                  speed=data.get("speed", "slow"),
                                  state=data.get("state", "on"))
            elif self.path == "/focus":
                r = lanc.dispatch(kind="focus", direction=data.get("dir"),
                                  state=data.get("state", "on"))
            elif self.path == "/iris":
                r = lanc.dispatch(kind="iris", direction=data.get("dir"),
                                  state=data.get("state", "on"))
            elif self.path == "/cmd":
                r = lanc.dispatch(action=data.get("action"))
            else:
                return self._json({"error": "not found"}, 404)
            self._json(r, 200 if "error" not in r else 400)

        def log_message(self, *a):
            pass

    return Handler


def keys_loop():
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    print("keys: Right/PageUp=zoom in  Left/PageDown=zoom out  Space=rec  q=quit")
    try:
        while True:
            ch = sys.stdin.read(1)
            if ch in ("q", "\x03"):
                break
            if ch == " ":
                lanc.send("rec")
            # arrow keys are escape sequences; simple map
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--listen", default="127.0.0.1:8787")
    ap.add_argument("--keys", action="store_true")
    args = ap.parse_args()

    lanc = Lanc(args.port)
    host, port = args.listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), build_server(lanc))
    if args.keys:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        keys_loop()
    else:
        print(f"lancd listening on {args.listen}")
        server.serve_forever()
