# LancBridge — PC → Sony HXR-MC2500 control over LANC (pbcc project)

The MC2500's USB port is host-only, so PC control goes through the 2.5mm REMOTE
> GPIO variant for a Raspberry Pi (no Arduino): see gpio/GPIO-SPEC.md.
(LANC) jack, bridged by a microcontroller that presents USB-serial to the PC.

## The LANC (Control-L) protocol

- Open-collector serial bus. The **camera is master**: it starts every frame by
  pulling the line low (start bit), then clocks 8 bytes. A bit is **104 µs**
  (9600-baud timing); start-bit spacing 1200–1400 µs; frame repeat every
  ~20 ms (PAL) / 16.6 ms (NTSC).
- The controller injects its 2 command bytes during words 0–1 of each frame by
  pulling the line low for 0-bits (never driving high).
- A command takes effect after **3–5 consecutive frames**. Keep repeating it
  while the action should continue (e.g. hold zoom), stop to end.
- Verified MC2500-relevant commands (byte0, byte1):

| Function        | Bytes     | Notes                          |
|-----------------|-----------|--------------------------------|
| Rec start/stop  | `18 33`   | one-shot, send 5 frames        |
| Zoom tele slow  | `28 35`   | hold = continuous zoom         |
| Zoom wide slow  | `28 37`   | hold = continuous zoom         |
| Zoom tele fast  | `28 39`   |                                |
| Zoom wide fast  | `28 3B`   |                                |
| Focus near      | `28 47`   | manual focus mode              |
| Focus far       | `28 45`   |                                |
| AF on/off       | `28 41`   | toggle                         |
| Iris open       | `28 55`   |                                |
| Iris close      | `28 54`   |                                |
| Power off       | `18 5E`   |                                |
| Data screen     | `18 B4`   | toggle on LCD                  |

The camera also streams status back in words 4–7 (mode, counter, tape/rec
state) — the firmware forwards each full frame to USB so the daemon can decode
recording state.

## Hardware

Parts:
- Arduino Nano / Uno (5V AVR — simplest; the Pico 3.3V variant needs the
  divider below on the sense side only, transmit is identical open-drain)
- 2.5 mm stereo plug (sleeve = GND, tip = +5V-ish supply from camera, ring = LANC signal)
- 1 kΩ resistor (series, protect the MCU pin), optional 10 kΩ pull-down none needed
- Optional: 100 µF cap across camera tip/sleeve if you want to power the Nano
  from the camera's LANC supply (tip can be 5–9 V unregulated — use a
  5 V regulator / Nano's VIN, not raw 5V pin)

Wiring (self-powered from PC USB — recommended, skip the regulator):

```
LANC plug ring (signal) ──[1kΩ]──┬── D2 (Nano)     ← read & open-drain transmit
                                 │
                              (MCU side)
LANC plug sleeve (GND) ──────────┴── GND (Nano)   ← common ground REQUIRED
LANC plug tip ── not connected (unless powering the Nano via regulator)
```

The MCU pin is switched between INPUT (line released — camera pulls it high)
and OUTPUT-LOW (drive line low = send a 0 bit). Never drive HIGH. That emulates
open-collector exactly and needs no transistor.

Pinout reference (Sony 2.5mm LANC): tip = power out (up to 100 mA), ring = LANC
signal, sleeve = ground.

## Firmware

`firmware/lanc_bridge.ino`:
- Syncs to the camera's start bit, transmits the current 2 command bytes in
  words 0–1, then reads the remaining 6 words and prints the full 8-byte frame
  to USB serial at 115200 as `F 18 FF ... 8C 00 1F\n` per frame.
- Serial commands from the PC (one per line): `rec`, `zoomin`, `zoomout`,
  `zoomin_fast`, `zoomout_fast`, `focusnear`, `focusfar`, `aftoggle`,
  `irisopen`, `irisclose`, `poweroff`, `display`, `stop` (clear command).
- `rec`-style commands auto-send for 5 frames; `zoom*`/`focus*`/`iris*` hold
  until `stop` arrives (or 10 s safety timeout).

## Integration: Bitfocus Companion (Raspberry Pi) → Stream Deck

The Raspberry Pi already running Bitfocus Companion gets the Arduino plugged
into it, with `daemon/lancd.py` running there (systemd unit). Companion
triggers the camera using its built-in **HTTP Request / Generic HTTP** module —
every endpoint also answers plain **GET**, which that module can send directly:

| Stream Deck button        | Companion HTTP action (GET)                          |
|---------------------------|------------------------------------------------------|
| REC start/stop            | `GET http://127.0.0.1:8787/rec`                      |
| Zoom in (hold)            | `GET .../zoom?dir=in&state=on`  — button DOWN        |
| Zoom in (release)         | `GET .../zoom?dir=in&state=off` — button UP          |
| Zoom out (hold/release)   | `GET .../zoom?dir=out&state=on|off`                  |
| Zoom in fast              | `GET .../zoom?dir=in&speed=fast&state=on`            |
| Focus near / far          | `GET .../focus?dir=near|far&state=on`                |
| Iris open / close         | `GET .../iris?dir=open|close&state=on`               |
| Stop zoom/focus/iris      | `GET .../stop`                                       |
| Power off / display / AF  | `GET .../cmd?action=poweroff|display|aftoggle`       |

Companion "Press and hold / release" button behaviour maps the two GET calls
for continuous zoom. Button feedback (e.g. red tally while recording) comes
from the Companion **Variable** poll: define a variable that GETs
`http://127.0.0.1:8787/status` (returns `{"recording":true,...}`) and style the
REC button on that variable.

The daemon binds 127.0.0.1 by default; set `--listen 0.0.0.0:8787` only if
Companion runs on a different box.

## PC daemon

`daemon/lancd.py` runs on the Raspberry Pi next to Bitfocus Companion (Arduino
plugged into the Pi): opens /dev/ttyUSB0, exposes
`POST /zoom {dir,in|out, speed slow|fast, state on|off}`, `POST /rec`,
`GET /status` (last frame, rec-state decoded from word 5 status nibble), on
port 8787. Also a `--keys` mode mapping arrow keys/PageUp-Down to zoom/rec for
direct control.

## Sources
- boehmel.de/lanc — the canonical reverse-engineered LANC spec
- github.com/AlexNe/arduino_lanc_sample — reference firmware
- pdf.textfiles.com LANC/Control-L protocol dump
