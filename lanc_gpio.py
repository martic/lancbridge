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
FRAME_MIN_GAP_US = 9000 # true max fall-to-fall gap across the frame boundary is ~12.6ms (byte7 fall -> next sync); mid-frame falls are ~1.04ms apart
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
        self._lock = threading.RLock()
        self._start_event = threading.Event()
        self._last_fall = None
        self._tick_offset = None
        self._ema_lateness = None
        self._pad_used = 0
        self._lateness_us = 0
        self._d1 = 300            # wave-start latency estimate (us)
        self._pending_rise_tick = None
        self._period_us = 19050
        self._tx_done_mono = None
        self._dbg = None
        self._dbg_edges = []
        self._tx_fifo = None
        self._tx_proc = None
        self._tx_fifo_path = '/tmp/lanc_tx.fifo'
        self._tx_ok = False
        self._tx_writer_started = False
        import queue
        self._txq = queue.Queue()
        self._last_frame_tick = None
        self._wave_id = None
        self._wave_cmd = None
        self.sends = 0
        # dedicated pigpio connection for the callback thread (pigpio's python
        # client is not thread-safe over a shared socket)
        self.pi_tx = pigpio.pi()
        pi.set_mode(gpio, pigpio.INPUT)
        pi.set_pull_up_down(gpio, pigpio.PUD_UP)
        self.pi_tx.set_mode(gpio, pigpio.INPUT)
        self.pi_tx.set_pull_up_down(gpio, pigpio.PUD_UP)
        pi.callback(gpio, pigpio.EITHER_EDGE, self._edge)
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- frame-start detection + transmit ------------------------------------
    def _edge(self, gpio, level, tick):
        if self._dbg is not None:
            self._dbg_edges.append((tick, level))
        if level == 0:
            # FALLING edge: a frame's first bit begins HERE. If the previous
            # frame start was >=5ms ago (inter-frame idle), this is it —
            # fire the command wave right now, at bit 0, from the callback
            # thread (pigpio notification latency ~100us, far better than
            # scheduling from a Python thread).
            if self._last_frame_tick is not None:
                gap = (tick - self._last_frame_tick) & 0xFFFFFFFF
                if gap >= FRAME_MIN_GAP_US:
                    prev_tick = self._last_frame_tick
                    self._last_frame_tick = tick
                    self.connected = True
                    self._on_frame_start(tick, prev_tick)
            else:
                self._last_frame_tick = tick
            self._last_fall = tick
            return
        # rising edge: validate the low pulse was one bit time
        width = (tick - self._last_fall) & 0xFFFFFFFF if self._last_fall is not None else 0
        if BIT_LO_LO_US <= width <= BIT_LO_HI_US:
            self._start_event.set()
        # servo: rendered rise of our transmitted low-run vs expectation
        pend = self._pending_rise_tick
        if pend is not None:
            self._pending_rise_tick = None
            if width > BIT_LO_HI_US and abs(tick - pend) < 6000:
                err = (tick - pend) & 0xFFFFFFFF
                if err > 30000000:
                    err -= 4294967296
                self._d1 = max(0, self._d1 + max(-500, min(500, err)))

    def _ensure_helper(self):
        """Launch the C transmitter (libgpiod, ~20us edge latency). Returns
        True if the helper is alive and the FIFO is open.
        MUST be called only from the TX writer thread (blocks on fifo open)."""
        if self._tx_ok:
            return True
        try:
            import os as _os
            import subprocess as _sp
            binp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lanc_tx')
            if not _os.path.exists(binp):
                print("LANC tx: lanc_tx binary missing — falling back to waves", flush=True)
                return False
            fifo = self._tx_fifo_path
            try:
                _os.mkfifo(fifo)
            except FileExistsError:
                pass
            self._tx_proc = _sp.Popen([binp], stdin=_sp.DEVNULL,
                                      stdout=_sp.DEVNULL)  # stderr -> journal
            self._tx_fifo = open(fifo, 'w', buffering=1)
            time.sleep(0.05)
            if self._tx_proc.poll() is None:
                self._tx_ok = True
                print("LANC tx: using C helper (libgpiod)", flush=True)
                return True
            return False
        except Exception as e:
            print(f"LANC tx helper setup failed: {e}", flush=True)
            self._tx_fifo = None
            return False

    def _tx_writer(self):
        """Dedicated thread: drains the TX queue into the helper FIFO so the
        pigpio callback thread never blocks on I/O."""
        while True:
            c0, c1 = self._txq.get()
            try:
                if not self._tx_ok and not self._ensure_helper():
                    self._txq.task_done()
                    continue
                self._tx_fifo.write(f"{c0:02x} {c1:02x} 1\n")
            except Exception as e:
                print(f"LANC tx fifo write failed: {e}", flush=True)
                self._tx_ok = False
                try:
                    if self._tx_fifo:
                        self._tx_fifo.close()
                except Exception:
                    pass
                self._tx_fifo = None
            finally:
                self._txq.task_done()

    def _start_writer(self):
        if not self._tx_writer_started:
            self._tx_writer_started = True
            threading.Thread(target=self._tx_writer, daemon=True).start()

    def _on_frame_start(self, tick, prev_tick):
        """Runs in the pigpio callback thread, at the frame-start edge.
        We wake ~1ms late (Python callback latency), so we can't hit THIS
        frame's bit 0. Instead we schedule the data for the NEXT frame:
        wave = [HIGH delay][8 data bits][HIGH stop/start][8 data bits][tail],
        where delay lands the first data bit exactly at (next_start + 104us).
        The RX edge callback servos self._d1 (wave-start latency estimate)
        against the rendered rise of our first low-run."""
        if self._tick_offset is None:
            self._tick_offset = time.monotonic() - self.pi_tx.get_current_tick() * 1e-6
        with self._lock:
            frames_left = self.cmd_frames_left
            c0, c1 = self.cmd
        if not frames_left:
            return
        # Helper path — ALWAYS queue; the writer thread does the (blocking)
        # helper setup and FIFO I/O. The callback thread never blocks.
        self._start_writer()
        self._txq.put((c0 & 0xFF, c1 & 0xFF))
        self.sends += 1
        self._tx_done_mono = time.monotonic() + 0.025
        with self._lock:
            self.cmd_frames_left = frames_left - 1
            if self.cmd_frames_left == 0:
                self.cmd = [0x00, 0x00]
        return
        now_rel = (time.monotonic() - self._tick_offset) * 1e6
        T = (tick - prev_tick) & 0xFFFFFFFF if prev_tick is not None else 19050
        if not (15000 < T < 25000):
            T = 19050
        self._period_us = T
        D = T + BIT_US - (now_rel - tick) - self._d1
        if D < 1000:
            return  # too close to the next frame to be useful
        self._build_wave(int(D), c0, c1)
        wid = self._wave_id
        if wid is None:
            return
        self.pi_tx.set_mode(self.gpio, pigpio.OUTPUT)
        self.pi_tx.wave_send_once(wid)
        self.sends += 1
        self._tx_done_mono = time.monotonic() + (D + 2600) / 1e6
        # servo reference: expected rise of our first merged low run.
        # count leading zero DATA bits of byte0 (LSB-first); rise = the end
        # of the camera's sync low (1 bit) + our leading zeros.
        first = c0 & 0xFF
        lead = 0
        for i in range(8):
            if first & (1 << i):
                break
            lead += 1
        if 0 < lead < 8:
            self._pending_rise_tick = (tick + T + (1 + lead) * BIT_US) & 0xFFFFFFFF
        with self._lock:
            self.cmd_frames_left = frames_left - 1
            if self.cmd_frames_left == 0:
                self.cmd = [0x00, 0x00]

    # ---- waveform for the 2 command bytes -----------------------------------
    def _build_wave(self, delay_us=0, c0=None, c1=None):
        # wave = [HIGH delay_us][8 data bits][HIGH 104 (camera stop+start)]
        #        [8 data bits][HIGH tail]. Data lands at the NEXT frame's
        # bit slots 1-8 and 11-18; the delay absorbs our callback latency.
        if c0 is None or c1 is None:
            with self._lock:
                c0, c1 = self.cmd
        MASK = 1 << self.gpio
        c0 &= 0xFF
        c1 &= 0xFF
        order = getattr(self, '_bit_order', 'lsb')
        seq = []
        if delay_us > 0:
            seq.append((MASK, 0, int(delay_us)))  # released/high for the delay
        for byte in (c0, c1):
            for i in range(8):
                bit = 1 if byte & (1 << i) else 0
                if order == 'msb':
                    bit = 1 if byte & (0x80 >> i) else 0
                seq.append((MASK if bit else 0,
                            0 if bit else MASK, BIT_US))
            seq.append((0, MASK, BIT_US))        # stop + next start (camera)
        # final stop bit is included for byte1 too — keep it (camera drives
        # it high; our high is a no-op, but if the last data bit was low we
        # must release before the camera's stop bit)
        self.pi_tx.wave_clear()
        self.pi_tx.wave_add_generic([
            pigpio.pulse(gpio_on, gpio_off, delay) for gpio_on, gpio_off, delay in seq
        ])
        self._wave_id = self.pi_tx.wave_create()
        self._wave_cmd = tuple(self.cmd)

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

            # The TX wave now spans delay+2.08ms (~21ms) and lands its data
            # bits in the NEXT frame's slots. It ends right at the camera's
            # byte-2 start bit of that frame. Wait for the wave to finish
            # BEFORE switching the pin back to INPUT (a mode change cancels
            # an active wave).
            t_done = self._tx_done_mono
            while t_done is not None and time.monotonic() < t_done:
                time.sleep(0.0005)
            self._tx_done_mono = None
            self.pi_tx.set_mode(self.gpio, pigpio.INPUT)
            # we are now at the camera's byte-2 start bit; read bytes 2-7
            frame = None
            try:
                with self._lock:
                    c0, c1 = self.cmd
                frame = [c0, c1] + [self._recv_byte() for _ in range(6)]
                self.last_frame = frame
                self.recording = (frame[5] & 0xF0) == 0x30
            except Exception:
                import traceback; traceback.print_exc()

    def send_raw(self, c0, c1, frames=8):
        with self._lock:
            self.cmd = [c0 & 0xFF, c1 & 0xFF]
            self.cmd_frames_left = max(1, frames)
            self._cmd_total = self.cmd_frames_left
            self._build_wave()

    def debug_run(self, c0, c1, frames=8):
        """Self-measuring send: record edges during the hold, return the
        rendered phase of our merged low-run vs expectation, per frame."""
        lead = 0
        b = c0 & 0xFF
        for i in range(8):
            if b & (1 << i):
                break
            lead += 1
        self._dbg = frames + 3
        self._dbg_edges = []
        self.send_raw(c0, c1, frames)
        time.sleep((frames + 3) * 0.021 + 0.06)
        self._dbg = None
        edges = self._dbg_edges
        self._dbg_edges = []
        out = []
        prev = None
        for idx, (tick, level) in enumerate(edges):
            if prev is not None and level == 0:
                gap = (tick - prev[0]) & 0xFFFFFFFF
                if prev[1] == 1 and gap >= 9000:
                    # sync fall; find the next rising edge
                    rise = None
                    for j in range(idx + 1, len(edges)):
                        if edges[j][1] == 1:
                            rise = edges[j][0]
                            break
                    row = {"sync": tick, "lowrun": None if rise is None else (rise - tick) & 0xFFFFFFFF}
                    if 0 < lead < 8:
                        row["expect"] = (1 + lead) * BIT_US
                        row["err"] = None if rise is None else ((rise - tick) & 0xFFFFFFFF) - (1 + lead) * BIT_US
                    out.append(row)
            prev = (tick, level)
        return {"cmd": f"{b:02x}{c1 & 0xFF:02x}", "lead_zeros": lead,
                "edges_seen": len(edges), "frames": out}

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
            elif u.path == "/forcelow":
                # Electrical reachability test: hold the GPIO hard LOW for
                # N seconds. If the diode path conducts GPIO->line, the
                # camera's transmission must corrupt (its bytes collapse);
                # if the frames stay clean, TX cannot reach the line.
                secs = max(0.5, min(5.0, float(q.get("secs", 2))))
                try:
                    self._json({"held_low_for": secs})
                    self.wfile.flush()
                except Exception:
                    pass
                lanc.pi_tx.set_mode(lanc.gpio, pigpio.OUTPUT)
                lanc.pi_tx.write(lanc.gpio, 0)
                time.sleep(secs)
                lanc.pi_tx.write(lanc.gpio, 1)
                time.sleep(0.05)
                lanc.pi_tx.set_mode(lanc.gpio, pigpio.INPUT)
            elif u.path == "/debug":
                try:
                    c0 = int(q["c0"], 16); c1 = int(q["c1"], 16)
                    frames = max(2, min(32, int(q.get("frames", 8))))
                except (ValueError, KeyError):
                    self._json({"error": "usage: /debug?c0=18&c1=33&frames=8"}, 400)
                else:
                    self._json(lanc.debug_run(c0, c1, frames))
            elif u.path == "/cmd":
                self._json(lanc.dispatch(action=q.get("action")))
            elif u.path == "/raw":
                try:
                    c0 = int(q["c0"], 16); c1 = int(q["c1"], 16)
                    frames = int(q.get("frames", 8))
                except (KeyError, ValueError):
                    self._json({"error": "usage: /raw?c0=28&c1=35&frames=8"}, 400)
                    return
                lanc._bit_order = 'msb' if q.get('order') == 'msb' else 'lsb'
                lanc.send_raw(c0, c1, frames)
                self._json({"sent": f"{c0:02x}{c1:02x}", "frames": frames,
                            "order": lanc._bit_order})
            elif u.path == "/status":
                self._json({"recording": lanc.recording,
                            "connected": lanc.connected,
                            "last_frame": lanc.last_frame,
                            "sends": lanc.sends})
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
