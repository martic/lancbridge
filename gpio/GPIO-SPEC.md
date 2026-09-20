# LancBridge GPIO — Raspberry Pi → Sony HXR-MC2500 over LANC, end to end

Companion variant. This replaces the Arduino bridge with **direct GPIO on the
Raspberry Pi that already runs Bitfocus Companion**. The original
Arduino version (`firmware/` + `daemon/lancd.py`) is kept untouched as a
fallback — see "Fallback" at the end.

## Why the Pi's audio jack can't do it

LANC is a bidirectional open-collector serial bus clocked by the camera: the
camera pulses a start bit every frame (~20 ms) and the controller must sync to
that before transmitting. The Pi's 3.5mm jack is analog audio *out only* (most
models have no input at all), so the line can never be heard/synced, and analog
audio can't carry the open-collector signaling. It's not a serial port.

## How GPIO replaces the Arduino

The Arduino's only job was bit-banging an open-collector line at 104 µs/bit.
On the Pi:

- **Transmit**: a pre-built **pigpio waveform** (DMA-timed, exact 104 µs bits,
  immune to Linux jitter) is fired when the camera's start bit is detected.
- **Receive**: after the 2 command bytes are sent, the remaining 6 status words
  are sampled mid-bit using pigpio's hardware-timed `gpioDelay`.
- **Start-bit sync**: a falling-edge callback measures the low pulse; a pulse
  of 1200–1500 µs is a frame start (normal data start bits are only ~104 µs).

## Hardware — parts

- Raspberry Pi (any model with GPIO; the one already running Companion)
- 2.5 mm stereo plug (tip = camera power out, ring = LANC signal, sleeve = GND)
- **1N4148 diode** (see below) and a 1 kΩ resistor

## Hardware — wiring

```
LANC plug ring (signal) ──[1kΩ]──►|── GPIO17 (BCM)      diode: anode at GPIO side,
LANC plug sleeve (GND) ───────────────── Pi GND          cathode at LANC side
LANC plug tip ── unused (camera power out; do NOT feed into Pi)
```

The diode makes the Pi behave as true open-collector: GPIO **low** pulls the
LANC line low through diode+resistor; GPIO **high** is blocked by the diode so
the camera's own pull-up raises the line. This means push-pull waveform output
can never fight the camera's line driver. Common ground (sleeve → Pi GND) is
required. Keep the LANC cable away from mains leads; runs of 10 m+ are fine
per the LANC spec.

Pin: BCM17 (physical pin 11) is used by default, changeable with `--gpio N`.

## Protocol recap (identical to the Arduino spec)

- 8-byte frames, 104 µs/bit (9600-baud timing), frame every ~20 ms (PAL)
- Controller transmits words 0–1; camera transmits words 2–7 (status)
- A command takes effect after 3–5 consecutive frames

Command set (byte0, byte1):

| Function       | Bytes   | Hold behaviour            |
|----------------|---------|---------------------------|
| Rec start/stop | `18 33` | one-shot, 5 frames        |
| Zoom tele slow | `28 35` | hold while "on"           |
| Zoom wide slow | `28 37` | hold while "on"           |
| Zoom tele fast | `28 39` | hold                      |
| Zoom wide fast | `28 3B` | hold                      |
| Focus near     | `28 47` | hold                      |
| Focus far      | `28 45` | hold                      |
| AF toggle      | `28 41` | one-shot                  |
| Iris open      | `28 55` | hold                      |
| Iris close     | `28 54` | hold                      |
| Power off      | `18 5E` | one-shot                  |
| Data screen    | `18 B4` | one-shot                  |

## Software — `lanc_gpio.py`

Same HTTP API as the Arduino version's daemon, so the Companion wiring is
identical. Runs directly on the Pi (needs `pigpiod` running):

```
sudo apt install -y pigpiod python3-pigpio        # daemon auto-starts
sudo cp lanc_gpio.py /opt/lancbridge/
```

Endpoints (GET for Companion, POST for anything else):

| Stream Deck button      | Companion HTTP action (GET)                        |
|-------------------------|-----------------------------------------------------|
| REC start/stop          | `http://127.0.0.1:8787/rec`                         |
| Zoom in (hold)          | `.../zoom?dir=in&state=on` — button DOWN            |
| Zoom in (release)       | `.../zoom?dir=in&state=off` — button UP             |
| Zoom out (hold/release) | `.../zoom?dir=out&state=on|off`                     |
| Zoom in fast            | `.../zoom?dir=in&speed=fast&state=on`               |
| Focus near / far        | `.../focus?dir=near|far&state=on`                   |
| Iris open / close       | `.../iris?dir=open|close&state=on`                  |
| Stop zoom/focus/iris    | `.../stop`                                          |
| Power off / display / AF| `.../cmd?action=poweroff|display|aftoggle`          |
| Recording state         | `.../status` → `{"recording":true,...}` for tally   |

Companion setup: HTTP Request module, one action per button; use
"press & hold / release" behaviours for the two-part zoom calls; poll
`/status` into a variable to light the REC button red while recording.

## Frame engine internals

- Worker thread waits on a start-bit event; builds a pigpio wave from the
  current 2 command bytes (start bit + 8 bits LSB-first + stop, each 104 µs),
  sends it (≈2.2 ms), then samples 6 bytes mid-bit for the status words.
- Each full frame is available to the HTTP layer: `last_frame`, and
  `recording` decoded from the camera's VTR status word.
- One-shot commands are sent for 5 frames; hold commands repeat (≈5 s safety
  timeout if a release call is lost).
- If no camera is connected (no start bits for 2 s) the engine marks itself
  offline — `/status` reports `connected:false` instead of erroring.

## Systemd

`lancd-gpio.service` runs it at boot on the Pi. Needs `pigpiod` running
(Debian's package enables it by default; if not:
`sudo systemctl enable --now pigpiod`). Runs as root by default for GPIO
access, or as `pi` if the user is in the `gpio` group.

## Troubleshooting

- **No reaction**: check the 2.5mm plug is in the camera's REMOTE jack and the
  camera has LANC/Remote enabled; check diode orientation; verify `pigpiod` is
  running (`systemctl status pigpiod`).
- **Garbled frames / camera ignores commands**: bit jitter — make sure nothing
  else hammers the Pi CPU; the wave TX is DMA-timed so TX is safe, but if RX
  looks wrong increase `SAMPLE_US` sampling offset in the code.
- **Companion can't reach it**: daemon binds 127.0.0.1 by default; use
  `--listen 0.0.0.0:8787` only if Companion runs on another box.

## Fallback

The original Arduino version is preserved in `firmware/` + `daemon/` with its
own spec (SPEC.md). It remains fully compatible with the same HTTP API — swap
by pointing Companion at the other host/port if you ever go back.
