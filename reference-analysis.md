# Reference implementation analysis — fred-dev/arduino_lanC (Martin Koch, tested on Canon XF300)

Source: Lanc_remote2.ino (saved here). Cross-checked against our lanc_gpio.py.

## CONFIRMED SAME AS OURS
- Bit time 104 µs, frame = 8 bytes, inter-frame pause > 5 ms
- Frame start = falling edge AFTER a >5 ms HIGH pause (our detector ✓)
- Camera drives ALL byte start bits (reference waits 104 µs after the fall,
  then drives ONLY the 8 data bits, releases for stop — our data-only wave ✓)
- Data bits LSB-first ("reverse order, right-most bit first") ✓
- Command codes: zoom 0x28 0x00–0x0E (tele, slow→fast), 0x28 0x10–0x1E (wide),
  focus 0x28 0x45/0x47, AF 0x28 0x41, REC 0x18 0x33 ✓ (matches what we sent)
- Command repeated 5 consecutive frames ✓ (we hold frames)
- Open-collector bus, no diode needed (pinout pages confirm: sleeve GND,
  tip power 5-9 V, ring = LANC bus; BS170/MOSFET open-drain is the classic
  interface — our diode+1k arrangement is functionally equivalent)

## DIFFERENCE / SUSPECT
- Reference timing: data bit 0 occupies the SECOND 104 µs slot after the
  falling edge (it waits one full bit after detecting the fall).
- Ours: wave starts at callback time (fall + callback latency ≈ 0.1–0.3 ms).
  If callback+wave-send latency ≠ ~104 µs, every data bit is phase-shifted
  ±1 bit and the camera reads garbage — camera then ignores everything,
  which matches all observations (bits provably on the line, no response).

## NEXT SESSION PLAN
1. Add per-frame latency logging in _on_frame_start: (monotonic - tick/1e6).
2. Passive capture during /raw → measure the exact rendered phase of our
   data bits vs the sync low.
3. Pad the wave with a leading HIGH of (bit - measured_latency) µs so data
   bit 0 lands precisely in the second slot, matching the reference.
4. If phase is proven right and still no response: the MC2500 may gate LANC
   control behind a menu setting — recheck the manual's NETWORK/REMOTE pages.
