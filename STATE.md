# LancBridge — state of play 2026-09-26 EOD

## Where we are
MC2500 still ignores zoom/REC, but everything else is now PROVEN:
- C helper (lanc_tx.c, libgpiod v2) is LIVE and transmits: journal shows
  "using C helper", process persists, kernel edge timestamps, bit
  transitions scheduled 57us early vs ~60us ioctl lateness -> on-slot.
- **Helper bit mapping was INVERTED — fixed 14:46.** Wire (via /debug
  'bits'/'runs', ignore the inverted 'wire' numbers) now decodes EXACTLY
  0x28 0x02 on 8 consecutive frames.
- GPIO reaches the line: /forcelow corrupts the camera's TX visibly.
- Camera reacts to bus activity (status bytes changed when we TX).

## Tomorrow's plan (in order)
1. **Diode check**: is the diode still in the TX line (not bridged)?
   If present, lows sit at ~0.7V (diode Vf) and the camera's VIL may
   need <0.4V (its own lows are 0V). Bridge/short the diode, rerun zoom.
   If already bridged, lows are ~0V and this step is done.
2. **Dialect sweep** (only if wire is perfect AND lows are 0V and still
   nothing): try 0x28 0x0A / 0x28 0x35 (faster zoom speeds), REC forms
   0x28 0x33, and watch lens/screen each time.
3. Wire-verification commands: restart lancd, then
   curl -s 'http://127.0.0.1:8787/raw?c0=28&c1=02&frames=64'
   curl -s http://127.0.0.1:8787/debug?c0=28\&c1=02\&frames=8

## Gotchas
- /debug 'wire' values print INVERTED (decoder bug); trust bits/runs.
- runs labels fixed 14:5x (H=high). Payload runs gate: first 3 payload frames.
- gcc must be rerun after every lanc_tx.c pull; check mtime.
- gpt-6-sol/claude opinions were ONE-OFF delegation reviews, not defaults.
