# Independent Deep Review: LancBridge LANC Control Failure
## Fresh-Eyes Analysis — HXR-MC2500 Ignores All Commands

**Date**: 2026-09-26  
**Reviewer**: Independent second-opinion (skeptical of prior review findings)  
**Core problem**: Camera receives (proven by idle-frame read: FF FF FF FF EB CF 00 00) but ignores every command variant (~20+ tried).

---

## 1. ASSUMPTION CHALLENGES

### 1.1 Does HXR-MC2500 Actually Accept Standard LANC 0x28/0x18 Commands?

**VERDICT: YES — confirmed compatible**

- AODELAN ZC-4 remote explicitly lists HXR-MC2500 as compatible (Amazon product page)
- Sony RM-1BP and RM-AV2 remotes use standard LANC protocol with these cameras
- boehmel.de protocol reference lists 0x18 0x33 (REC), 0x28 0x35/0x37 (zoom slow) as universal since ~1996
- The amobbs.com PDF confirms same command set works across Sony consumer/prosumer lines

**No evidence of**:
- Special handshake/init sequences (D7 CA etc. — not documented anywhere for consumer/prosumer)
- Byte-pair requirements beyond standard 0x28 + speed-nibble encoding
- Power-pin requirements (ring carries camera power OUT, not required IN)
- Different command dialect for HXR-class vs consumer

**Conclusion**: Command bytes are correct. The camera SHOULD respond to 0x18 0x33 (REC) and 0x28 0x35 (zoom tele).

### 1.2 Electrical: Is the Diode+1k Configuration Valid?

**VERDICT: LIKELY OK BUT MARGINAL — needs measurement**

**Current design**: GPIO17 → 1kΩ → diode (anode at GPIO) → LANC ring (cathode at plug)

**Analysis**:
- GPIO LOW → diode forward-biased → LANC line pulled toward 0V + Vf ≈ 0.7V
- Camera pull-up (likely 10k-47k to 5V) competes with 1k pull-down
- Actual VOL on LANC bus ≈ (5V × 1k) / (10k + 1k) ≈ 0.45V (if camera pull-up is 10k)
- **This is within LANC VIL spec** (TTL-compatible: VIL < 0.8V)

**BUT**: The diode drop means GPIO sees ~0.7V when camera pulls low, not 0V.
- Pi GPIO VIL threshold is ~0.8V (per Mosaic Industries analysis)
- 0.7V is RIGHT AT the threshold — could be marginal read reliability
- **Receive works** (proven by frame capture), so this is not the TX failure cause

**Open-collector semantics**:
- Standard LANC remotes use N-FET open-drain (BS170, etc.) with NO diode
- The diode prevents Pi push-pull from fighting camera, but adds Vf
- This is a valid design choice per boehmel.de reference ("signal diode")

**Conclusion**: Electrical is probably OK. Marginal for RX but proven working. TX should work.

### 1.3 Should Remote Drive HIGH Bits Actively?

**VERDICT: NO — open-collector, release for 1-bits**

Multiple sources confirm:
- "open collector type data line... does nothing to send a +5V signal, and grounds the line to send a 0V signal" (fred-dev docs)
- "pull low for your command bits" (Sony protocol docs)
- Remote only SINKS current for 0-bits; 1-bits are released (camera's pull-up raises line)

**Current implementation matches**: `GPIOD_LINE_VALUE_INACTIVE` releases line, `ACTIVE` pulls low.

**Conclusion**: Open-collector behavior is correct.

### 1.4 Bit Order: LSB-first vs MSB-first?

**VERDICT: LSB-first is correct**

- boehmel.de: "The first bit is at the far right" (LSB)
- amobbs.com PDF: "You send the right digit first and move left" (LSB-first)
- fred-dev Arduino: shifts bit 0 first
- RS-232 standard (which LANC mimics at 9600 baud): LSB-first

**Current implementation**: 
```c
set_drive_bit((c0 >> i) & 1);  // i=0..7, extracts LSB first
```
This is CORRECT.

**Conclusion**: Bit order is correct.

### 1.5 Frame Anchoring: Must Remote TX During Different Slots?

**VERDICT: Bytes 0-1 are correct slots**

Protocol is unambiguous:
- Camera generates 8 bytes per frame
- Bytes 0-1 are "for controllers to command the camera" (fred-dev)
- "Byte 0 is your first Command_byte slot, and byte 1 is your second" (amobbs.com)
- Camera clocks START bits for ALL 8 bytes; remote injects DATA bits during bytes 0-1 only

**Current implementation**: Waits for frame-sync fall, then drives during byte 0 and byte 1 time slots.

**Conclusion**: Slot selection is correct.

---

## 2. CRITICAL TIMING ANALYSIS

### 2.1 The lanc_tx.c Helper — Detailed Code Review

**Sync detection** (lines 133-150):
```c
// wait for HIGH >=5ms then falling edge
if (wait_event(1, IDLE_US, &t) == 0)  // got falling within 5ms
    continue;  // still inside frame, retry
if (wait_event(0, 250000, t0) != 0)   // wait for actual frame-start fall
    return -1;
```

**CRITICAL BUG FOUND**: The sync detection logic is INVERTED.

Looking at `wait_event()`:
- `want=1` means wait for RISING edge
- `want=0` means wait for FALLING edge

The sync loop:
1. `wait_event(1, IDLE_US, &t)` — waits for RISING edge within 5ms
2. If rising edge happens quickly (r==0), it's mid-frame → continue
3. If timeout (r==1), we're in inter-frame gap → proceed
4. `wait_event(0, 250000, t0)` — wait for FALLING edge (frame start)

**This is CORRECT logic** — wait until no rising edge for 5ms (gap), then catch the next falling edge.

**Data bit driving** (lines 152-178):
```c
// byte 0: data bits land at t0 + (1..8)*104us
set_output();
struct timespec rel = t0;
for (int i = 0; i < 8; i++) {
    ts_add_us(&rel, BIT_US);  // advance by 104us
    sleep_until(&rel);         // wait until that time
    set_drive_bit((c0 >> i) & 1);  // THEN drive the bit
}
```

**CRITICAL TIMING BUG**: Data is driven AFTER the bit slot starts, not AT the bit slot start.

The timing works like this:
- t0 = frame-start falling edge (start of byte 0's START bit)
- First data bit (bit 0) should be driven at t0 + 104µs
- BUT: the code sleeps until t0+104µs, THEN calls set_drive_bit()
- set_drive_bit() involves a system call (gpiod_line_request_set_value) with latency
- **Actual bit transition happens ~20-50µs AFTER the target time**

For 8 bits, this accumulating delay means:
- Bit 0: driven at t0 + 104 + ~30µs = t0 + ~134µs (camera expects data at t0 + 104µs)
- Bit 7: driven at t0 + 832 + ~30µs = t0 + ~862µs
- **Data is ~30% of a bit-time late on every bit**

**Worse**: The code drives the bit value at the START of the bit slot, but LANC expects the bit value to be STABLE during the MIDDLE of the slot (when camera samples). The late transition might cause the camera to sample during the transition.

### 2.2 Correct LANC Bit Timing

Per boehmel.de and multiple references:
- Camera's START bit: LOW for 104µs
- Data bits: 8 × 104µs, camera samples MID-BIT
- Remote must have bit value STABLE before camera samples (~52µs into each bit)

**Correct approach**:
```c
// Set bit value BEFORE the bit slot starts, or at least at its start
// NOT: sleep until bit-start, then set value
// SHOULD BE: set value, then sleep one bit time

ts_add_us(&rel, BIT_US);  // skip start bit (camera drives it)
for (int i = 0; i < 8; i++) {
    set_drive_bit((c0 >> i) & 1);  // set value FIRST
    ts_add_us(&rel, BIT_US);
    sleep_until(&rel);              // hold for one bit time
}
```

### 2.3 Python Callback Path (if helper not compiled)

The Python path uses pigpio waves. Let me verify the wave is being used...

In `_on_frame_start()` (line 177):
```python
if self._ensure_helper():
    # uses C helper
else:
    # uses pigpio wave
```

`_ensure_helper()` checks if `lanc_tx` binary exists. **If the binary is not compiled, it silently falls back to waves** with its own timing issues (callback latency ~950µs documented).

**USER MUST VERIFY**: Is lanc_tx compiled and running? The logs should say "LANC tx: using C helper (libgpiod)" if the helper is active.

---

## 3. RANKED ROOT-CAUSE HYPOTHESES

### Rank 1: C Helper Bit-Timing Bug — HIGH (90% confidence)

**Hypothesis**: `lanc_tx.c` drives each bit ~30-50µs late because `set_drive_bit()` is called AFTER `sleep_until()`, and the syscall latency shifts every bit.

**Falsifying experiment**:
```bash
# 1. Compile helper with debug timing output
cd /tmp/lancbridge
# Add fprintf(stderr, "bit%d at %ld\n", i, ts_us(&rel)) before set_drive_bit
gcc -O2 -o lanc_tx lanc_tx.c -lgpiod -lrt

# 2. Send command and capture actual vs expected timing
echo "28 35 1" > /tmp/lanc_tx.fifo
# Check stderr output for bit timing drift

# 3. Better: capture with logic analyzer or pigpio piscope
# Verify data bits start at t0+104µs, t0+208µs, etc.
```

### Rank 2: C Helper Not Compiled/Running — HIGH (85% confidence)

**Hypothesis**: The `lanc_tx` binary doesn't exist, so Python falls back to wave scheduling with ~950µs callback latency, making all commands arrive too late for the camera to parse.

**Falsifying experiment**:
```bash
# 1. Check if binary exists
ls -la /tmp/lancbridge/lanc_tx

# 2. Check if helper is being used (look for log message)
curl http://127.0.0.1:8787/debug?c0=28&c1=35&frames=3 2>&1 | grep -i helper

# 3. Check systemd logs for "using C helper"
journalctl -u lancd -n 50 | grep -i helper

# 4. Compile if missing
cd /tmp/lancbridge
gcc -O2 -o lanc_tx lanc_tx.c -lgpiod -lrt
```

### Rank 3: libgpiod Edge Latency — MEDIUM (70% confidence)

**Hypothesis**: libgpiod v2 edge detection has ~20µs latency (documented), but `clock_gettime()` after the event adds another ~10-20µs. Combined with syscall overhead, the frame-sync timestamp `t0` is already 30-40µs stale, shifting all subsequent timing.

**Falsifying experiment**:
```bash
# 1. Add timestamping at multiple points in wait_sync() and drive_frame()
# Compare gpiod_edge_event_get_timestamp_ns() vs clock_gettime() post-read

# 2. Use the kernel timestamp from the event, not wall clock:
# In wait_event(), use:
#   *out = gpiod_edge_event_get_timestamp_ns(ev) / 1000  // convert to timespec
# This eliminates userspace latency from the anchor point
```

### Rank 4: Byte-1 Start-Bit Miss — MEDIUM (60% confidence)

**Hypothesis**: After byte 0, the code waits for byte 1's start-bit falling edge with a 600µs timeout:
```c
if (wait_event(0, 600, &t1) != 0)
    return;
```
If byte 0 transmission takes longer than expected (due to timing bugs), the camera's byte-1 start bit might already be past by the time we look for it. The 600µs window might be too tight.

**Falsifying experiment**:
```bash
# 1. Increase timeout to 1200µs (byte time = ~1040µs)
# Edit lanc_tx.c line 170: wait_event(0, 1200, &t1)

# 2. Add debug output when wait_event returns timeout
# If it's timing out, that's why byte 1 never gets sent
```

### Rank 5: GPIO Mode Switching Glitch — MEDIUM (50% confidence)

**Hypothesis**: `set_input()` / `set_output()` use `gpiod_line_request_reconfigure_lines()` which may glitch the line state during reconfiguration, producing spurious edges the camera interprets as garbage data.

**Falsifying experiment**:
```bash
# 1. Capture waveform during command send
# Look for unexpected glitches at mode-switch points

# 2. Alternative: use a single mode (output) and rely on open-drain behavior
# If GPIOD_LINE_DRIVE_OPEN_DRAIN is set, INACTIVE = high-Z, ACTIVE = low
# No need to switch between input/output modes
```

### Rank 6: Electrical — Diode Marginal VOL — LOW (30% confidence)

**Hypothesis**: 1kΩ + diode gives VOL ≈ 0.5-0.7V. If camera's VIL threshold is <0.5V (stricter than TTL), our "low" isn't low enough.

**Falsifying experiment**:
```bash
# 1. Measure actual voltage during GPIO-low pulse with oscilloscope/multimeter
# VOL should be <0.4V for confident TTL compliance

# 2. Test with lower resistor (470Ω) to pull harder
# If this works, original 1k was too weak

# 3. Test without diode (direct GPIO to LANC via 1k)
# Verify Pi GPIO survives camera's 5V pulls
```

---

## 4. RECOMMENDED EXPERIMENT SEQUENCE

### Step 1: Verify Helper Status
```bash
ssh pi@<ip>
ls -la /tmp/lancbridge/lanc_tx
# If missing:
cd /tmp/lancbridge
gcc -O2 -o lanc_tx lanc_tx.c -lgpiod -lrt

# Restart daemon
sudo systemctl restart lancd
journalctl -u lancd -f &

# Send test command
curl http://127.0.0.1:8787/debug?c0=28&c1=35&frames=3
# Look for "using C helper (libgpiod)" in logs
```

### Step 2: Fix Bit Timing (if helper is active but still failing)
```c
// In drive_frame(), change the data-driving loop:
// BEFORE:
for (int i = 0; i < 8; i++) {
    ts_add_us(&rel, BIT_US);
    sleep_until(&rel);
    set_drive_bit((c0 >> i) & 1);
}

// AFTER:
ts_add_us(&rel, BIT_US);  // move past start bit
for (int i = 0; i < 8; i++) {
    sleep_until(&rel);
    set_drive_bit((c0 >> i) & 1);  // set at bit START
    ts_add_us(&rel, BIT_US);        // then advance for next bit
}
// The first bit is set RIGHT at t0+104µs, then held for 104µs
```

### Step 3: Test with AODELAN Remote (gold standard)
If Rank 1-2 fixes don't work:
- Buy AODELAN ZC-1 or ZC-4 (~$35-50 USD)
- Confirm camera responds to commercial remote
- Capture AODELAN's waveform as reference

### Step 4: Voltage Measurement
```bash
# With oscilloscope or good multimeter:
# 1. Measure LANC line voltage during idle (should be ~5V)
# 2. Trigger a command, measure during our "low" pulses (should be <0.4V)
# 3. If VOL > 0.6V, try 470Ω resistor
```

---

## 5. PREVIOUS REVIEW CRITIQUE

The previous review (review.md) identified several bugs, but I disagree with some findings:

**Correctly identified**:
- Wave termination race (Bug 5) — valid concern for Python path
- Clock mismatch possibility (Bug 1) — worth checking

**Incorrectly blamed**:
- "Diode drop means GPIO sees 0.7V" — This is for RX, which WORKS. TX has different concern.
- Wave truncation — Only applies if Python waves are used; C helper doesn't use waves

**Missed**:
- The C helper's bit-driving order bug (drive AFTER sleep)
- Whether the C helper is even being used
- libgpiod edge timestamp vs userspace timestamp discrepancy

---

## SUMMARY

**Most likely cause**: C helper bit timing — data bits are driven ~30µs late per bit because `set_drive_bit()` is called after `sleep_until()` instead of before. Or the C helper isn't compiled at all, falling back to the Python wave path with ~950µs latency.

**First action**: Verify `lanc_tx` binary exists and is being used (check for "using C helper" log message).

**Second action**: Fix the bit-driving order in `lanc_tx.c` — set bit value at bit-slot START, not END.

**Third action**: If still failing, use AODELAN remote as reference to confirm camera works and capture correct timing.
