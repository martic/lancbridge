# LANC/Control-L Protocol Research

Deep-dive research from primary and authoritative sources. Last updated: 2026-09-26.

## Primary Sources Referenced

1. **boehmel.de/lanc.htm** - Manfred Boehmel's comprehensive LANC documentation (most authoritative community reference, updated 2020-11-08)
2. **fred-dev/arduino_lanC** - GitHub repository with archived protocol documentation including Sony service manual excerpts
3. **Martin Koch (controlyourcamera.blogspot.com)** - Arduino LANC implementation reference (2011)
4. **David Meed (dmeed@nbnet.nb.ca)** - Sony LANC Control-L Protocol Summary (1994), cites official Sony manual
5. **pinoutguide.com** - Connector pinouts
6. **Sony Service Manual "Protocol of Control L/LF" (P/N 9-972-453-11 / 86C0943-1)** - Official Sony documentation (referenced but rare)

---

## 1. Bit Timing and Start Bit Ownership

### Timing Specifications (All Sources Agree)

| Parameter | Value | Source |
|-----------|-------|--------|
| Bit duration | **104 µs** | boehmel.de, Martin Koch, David Meed |
| Baud rate | **9600 baud** (standard RS232 timing) | All sources |
| Inter-byte gap | **1200-1400 µs** between start bits | boehmel.de |
| Frame period | **20 ms** (PAL/625) or **16.6 ms** (NTSC/525) | boehmel.de, Sony spec |
| Inter-frame gap | **>5 ms** of high (idle) before new frame | Martin Koch, all implementations |

### Start Bit Ownership: CAMERA DRIVES ALL START BITS

**Critical finding**: The camera (master) generates **all 8 start bits** in every frame. The remote/controller never sends start bits.

> "The master (camcorder or still video camera) generates the telegram frame, creating 8 startbits, followed each by 8 bits (1 byte) and a (long) stopbit."
> — boehmel.de

> "Start bit is sent from the VTR. First 2 words (0 and 1) are commands from the computer to the VTR, the last 6 are from the VTR to the computer"
> — David Meed, 1994, citing Sony Control-L protocol documentation

> "The VCR will send the start bit for the first 4 words, but will leave the line at +5V (all zeros) for the data."
> — Curt Welch (curt@oasys.dt.navy.mil), rec.video 1991

### Byte Slot Allocation

| Byte | Who sends START bit | Who fills DATA bits | Purpose |
|------|---------------------|---------------------|---------|
| 0 | Camera | **Remote** (controller) | Sub-command / device code |
| 1 | Camera | **Remote** (controller) | Command code |
| 2 | Camera | Camera (or tuner) | Tuner control / camera ID |
| 3 | Camera | Camera (or tuner) | Tuner control / camera ID |
| 4 | Camera | Camera | Status code |
| 5 | Camera | Camera | Status / guide code |
| 6 | Camera | Camera | Counter / time code |
| 7 | Camera | Camera | Counter / time code |

**The remote's job**: Wait for the camera's start bit on byte 0, then **overlay** data bits 0-7. Repeat for byte 1. The camera sends `0x00 0x00` in slots 0-1 by default (keeping line high), and the remote pulls low for its command bits.

### Transmission Procedure (Remote Side)

1. Wait for inter-frame gap (>5ms of high/idle)
2. Detect falling edge of start bit for byte 0
3. Wait 104 µs (skip start bit)
4. Transmit 8 data bits, LSB first, 104 µs each
5. Release line (go high-Z) before stop bit
6. Wait for start bit of byte 1
7. Repeat transmission for byte 1 data
8. Release line for remaining 6 bytes (let camera transmit)

---

## 2. Do ANY Sony Cameras Require Remote to Drive Start Bits?

**Answer: NO** — No documented evidence exists of any Sony camera requiring the remote to drive start bits.

All sources consistently state:
- Camera is **always the master**
- Camera provides timing/clock via start bits
- Remote **overlays** data during bytes 0-1 slots
- Some Panasonic cameras use 10ms intervals instead of 16.6/20ms, but still camera-driven

> "LANC, that's a blast from the past. My recollection is that coincident with every frame of video, the camera sends a LANC frame on the bus, with certain bytes (such as timecode) filled in. Other bytes in the frame, the remote is supposed to 'fill in' with its commands during the camera's transmission."
> — EEVblog forum, experienced developer

**Note**: Some documentation mentions that remotes can theoretically act as masters (generate frames), but no consumer Sony cameras expect this. The documented master/slave relationship is:
- **Slave**: VTR, Camera
- **Commander**: Computer, Editor, Remote

---

## 3. Command Byte Tables

### Byte 0: Sub-Command (Device Code)

| Binary | Hex | Description |
|--------|-----|-------------|
| `0001 1000` | **0x18** | Normal command to VTR or video camera |
| `0010 1000` | **0x28** | Special command to video camera (zoom, focus, iris) |
| `0011 1000` | **0x38** | Special command to VTR |
| `0001 1110` | **0x1E** | Normal command to still video camera |
| `1101 1000` | **0xD8** | Command to SD-card recording cameras (newer) |

### Byte 1: Zoom Commands (Sub-command 0x28)

**Variable Speed Zoom (Tele/In direction)**:

| Hex | Speed | Description |
|-----|-------|-------------|
| **0x00** | Tele 1 | Slowest speed |
| **0x02** | Tele 2 | Faster than 00 |
| **0x04** | Tele 3 | Faster than 02 |
| **0x06** | Tele 4 | Faster than 04 |
| **0x08** | Tele 5 | Faster than 06 |
| **0x0A** | Tele 6 | Faster than 08 |
| **0x0C** | Tele 7 | Faster than 0A |
| **0x0E** | Tele 8 | **Fastest speed** |

**Variable Speed Zoom (Wide/Out direction)**:

| Hex | Speed | Description |
|-----|-------|-------------|
| **0x10** | Wide 1 | Slowest speed |
| **0x12** | Wide 2 | Faster than 10 |
| **0x14** | Wide 3 | Faster than 12 |
| **0x16** | Wide 4 | Faster than 14 |
| **0x18** | Wide 5 | Faster than 16 |
| **0x1A** | Wide 6 | Faster than 18 |
| **0x1C** | Wide 7 | Faster than 1A |
| **0x1E** | Wide 8 | **Fastest speed** |

**Optical-Only Zoom (avoids digital zoom, some cameras)**:

| Hex | Description |
|-----|-------------|
| **0x30** - **0x3E** | Tele speeds (even values), optical only |

**Universal Zoom Commands (all cameras since ~1996)**:

| Hex | Description |
|-----|-------------|
| **0x35** | Zoom Tele slow |
| **0x37** | Zoom Wide slow |
| **0x39** | Zoom Tele fast |
| **0x3B** | Zoom Wide fast |

### Byte 1: Record Commands (Sub-command 0x18)

| Hex | Description | Notes |
|-----|-------------|-------|
| **0x33** | **Start/Stop** (toggle) | Most universal REC command |
| **0x3A** | Record | Direct record (some devices) |
| **0x3C** | Record-pause | Some devices |

**DV-specific** (Sub-command 0x28):

| Hex | Description |
|-----|-------------|
| **0x27** | Rec start (DV, some cameras) |
| **0x29** | Rec stop (DV, some cameras) |

### Complete Command Reference Table (0x18 sub-command)

| Hex | Action |
|-----|--------|
| 0x30 | Stop |
| 0x32 | Pause |
| 0x33 | **Start/Stop** (REC toggle) |
| 0x34 | Play |
| 0x36 | Rewind |
| 0x38 | Fast Forward |
| 0x3A | Record |
| 0x5E | Power Off |
| 0x5C | Power On (some models) |
| 0x2A | Power/Viewfinder Off (older models) |

### Command Repetition Requirement

> "A command is valid after 3...4 telegrams."
> — boehmel.de

> "LANC commands must be repeated 4 times in order to be accepted by the camera."
> — Martin Koch

**Most implementations use 4-5 consecutive frames** of the same command. This is not a minimum — it's a reliability requirement for the camera to acknowledge the command.

---

## 4. Initialization / Handshake Requirements

### Power-On from Off State

> "Connect LANC Signal to GND for more than 140ms to power on (or on Mini-DIN Pin 3 to GND)."
> — boehmel.de

**Power-on procedure**:
1. Pull LANC data line LOW (to GND) for **>140 ms**
2. Camera wakes up and begins sending frames
3. Wait for camera to stabilize (~5 seconds recommended)
4. Begin sending commands

> "I found that giving the LanC line a low (1) or 0 vdc command from 500mS starts the camera's data Frame, this data Frame only lasts for a few seconds then shuts down again."
> — DIY implementer (amobbs.com document)

### No Formal Handshake

**There is NO handshake protocol**. The remote simply:
1. Monitors for valid frames (8 bytes, correct timing)
2. Injects commands into bytes 0-1 slots
3. Repeats for 4+ frames

The camera does not explicitly acknowledge commands. You verify success by:
- Reading status bytes (4-7) for mode changes
- Observing physical camera response

### Idle Behavior

When no command is being sent, the remote should:
- Keep LANC line in **high-impedance** (tri-state)
- Or actively drive high (but high-Z is preferred to avoid conflicts)
- Camera sees `0x00 0x00` in bytes 0-1 (no command)

**There is no documented "idle for N frames before commands accepted" requirement**, but best practice is to wait a few frames after power-on before sending commands.

---

## 5. Electrical Specifications

### Voltage Levels

| Parameter | Value | Source |
|-----------|-------|--------|
| Line idle (HIGH) | **+5V to +8V** | Varies by camera model and battery |
| Line active (LOW) | **0V** (ground) | All sources |
| Logic threshold | ~1.3V - 2.5V (TTL compatible) | Implied by implementations |
| Power output (tip/pin 1) | **5.9V to 9V DC, 100mA max** | Sony spec, David Meed |

> "Voltage depends on model and battery."
> — boehmel.de

> "+5...8 volt, depending on model and battery used"
> — pinoutguide.com

### Pull-Up Resistor

The camera has an **internal pull-up resistor** that holds the line high:

> "The LANC bus is open collector so it is normally pulled high to about 5v"
> — David Meed

| Implementation | Typical Value |
|----------------|---------------|
| Camera internal pull-up | **4.7kΩ** (VTR) or **10kΩ** (camera) |
| External pull-up (if needed) | **10kΩ** to line voltage |

### Open-Collector Operation

> "The serial line is an open-collector type data line. This means that the VCR normally holds the line at +5V with a pull-up resistor. An external unit that wants to send data uses an open-collector type TTL gate that does nothing to send a +5V signal, and grounds the line to send a 0V signal."
> — Curt Welch

**Remote TX requirements**:
- Must be able to **sink current to ground** (pull line low)
- Must present **high impedance** when not transmitting (don't fight pull-up)
- Open-collector/open-drain output, or tri-state with active-low drive

### Is a Diode in Series Acceptable?

**CRITICAL FOR YOUR ISSUE**:

If a series diode drops ~0.6-0.7V, your LOW level becomes **0.6-0.7V instead of 0V**.

Most LANC inputs have **TTL-compatible thresholds**:
- LOW: < 0.8V (some sources say <1.3V)
- HIGH: > 2.0V

**A 0.7V "low" is marginal and may not be recognized as LOW by all cameras.**

Working DIY implementations typically use:
1. **NPN transistor** (2N2222, BC547, etc.) with collector to LANC, emitter to GND
   - Achieves true 0V when ON
2. **Direct GPIO** (3.3V or 5V MCU) driving through resistor
   - Works if line voltage is 5V and GPIO can sink enough current
3. **Zener diode protection** on input only (5.1V), not in series with output

> "The 5.1V Zener diode protects the Arduino because the LANC supply/signaling voltage can go to up to +8V as per spec"
> — Novgorod/LANC-USB-GUI

**If your circuit has a diode in the TX path causing 0.7V low, this is a likely cause of commands being ignored.**

### Typical Working Circuit (Arduino/MCU)

From multiple sources (Martin Koch, L-Rosen, Novgorod):

```
LANC pin ----+---- 10kΩ ----+---- MCU Input Pin (RX)
             |              |
             +--[5.1V Zener]---GND  (protection)
             |
             +---- 1kΩ ---- Collector of NPN transistor
                                    |
                            Emitter to GND
                            Base via 1kΩ to MCU Output Pin (TX)
```

Or simpler (5V systems where LANC ≤ 5V):

```
LANC pin ---- 1kΩ ---- MCU Pin (bidirectional, INPUT when idle, OUTPUT LOW to transmit)
```

---

## 6. Known Reasons Commands Get Ignored

### Voltage/Electrical Issues

1. **LOW voltage not low enough** — Series diode, weak transistor drive, or GPIO not reaching 0V
2. **Rise/fall time too slow** — RC time constant too high
3. **Line contention** — Remote not going high-Z, fighting camera's transmission
4. **Missing pull-up** — If camera expects external pull-up and none provided

### Timing Issues

5. **Start bit not detected** — Remote transmitting before/after camera's start bit
6. **Bit timing drift** — 104µs timing not accurate enough
7. **Frame sync lost** — Not waiting for >5ms gap before byte 0

### Protocol Issues

8. **Insufficient repetition** — Command sent <4 frames
9. **Wrong byte order** — LSB must be sent first
10. **Wrong sub-command** — Using 0x18 for zoom (needs 0x28) or vice versa
11. **Invalid command** — Camera doesn't implement that specific command code

### Camera State Issues

12. **Wrong mode** — Camera in VTR/playback mode, command only works in camera mode
13. **Camera not ready** — Still powering up, servo not engaged
14. **LANC disabled** — Some cameras have a menu setting to enable/disable LANC
15. **Wrong connector** — Some Sony cameras with multi-pin connectors require pin 7 connected to GND through 100kΩ to enable LANC mode

> "Sony uses a LANC select signal. You MUST connect pins 7 and 8 together using a 100k ohm resistor if you are connecting a LANC controller to the Sony 10-pin A/V connector."
> — studio1productions.com

### HXR-MC2500 Specific

The Sony HXR-MC2500 is a **modern AVCHD camcorder** (not tape-based). Potential issues:

16. **New command set** — May require sub-command **0xD8** instead of 0x18/0x28
17. **SD-card mode commands** — See boehmel.de table for 0xD8 commands:
    - `0xD8 0x00` = start/stop
    - `0xD8 0x0C` = photo capture

18. **Tip power** — Does your 2.5mm plug have the tip connected? Some implementations need to sense or use the +5V on the tip.

---

## 7. Diagnostic Recommendations

Based on this research, for your Raspberry Pi implementation:

### Verify Electrical

1. **Measure LOW voltage** when Pi transmits — must be <0.8V, ideally <0.3V
2. **Check if series diode exists** and if it's been bridged
3. **Measure line idle voltage** — should be ~5V from camera
4. **Verify transistor saturation** — Vce(sat) should be <0.2V

### Verify Timing

5. Use oscilloscope or logic analyzer to confirm:
   - 104µs bit period
   - Commands appear immediately after camera's start bit
   - Commands present in byte 0 and byte 1 slots (not later)

### Try Alternative Commands

6. Test with 0x18 0x30 (STOP) — simpler than REC toggle
7. Test with 0x28 0x00 (Tele slowest) or 0x28 0x10 (Wide slowest)
8. Try 0xD8 0x00 (SD-card start/stop) if standard commands fail

### Verify Camera Behavior

9. Confirm camera's LANC is enabled in menus
10. Try commands in both CAMERA and VTR modes
11. Check if camera acknowledges via status bytes (byte 4 changes)

---

## References

- http://www.boehmel.de/lanc.htm — Primary protocol reference
- https://github.com/fred-dev/arduino_lanC/blob/master/LANC%20Documenation/sony_lanc.html — Archived documentation
- https://controlyourcamera.blogspot.com/2011/02/arduino-controlled-video-recording-over.html — Martin Koch Arduino implementation
- http://pdf.textfiles.com/manuals/STARINMANUALS/Sony%20Video/Manuals/-%20LANC%20Protocol.htm — Boehmel mirror
- https://pinoutguide.com/DigitalCameras/sony_lanc_pinout.shtml — Connector pinouts
- https://github.com/Novgorod/LANC-USB-GUI — Modern implementation with circuit
- https://projecthub.arduino.cc/L-Rosen/serial-to-lanc-control-l — L-Rosen Arduino interface
- https://www.studio1productions.com/sony-pinout/ — Sony multi-pin connector info
- Sony Service Manual "Protocol of Control L/LF" (P/N 9-972-453-11) — Official (rare)
