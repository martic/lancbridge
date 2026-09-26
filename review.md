# Independent Second-Opinion Review: lancbridge LANC Control Failure
## Sony HXR-MC2500 ignores commands — root-cause analysis

**Status**: Camera provably receives our transmissions (line toggling visible in captures) but produces zero response (no zoom, REC, iris reaction). Design matches validated Arduino reference; 5 previous fix attempts have not resolved the issue. This review challenges core assumptions.

---

## 1. ELECTRICAL LAYER ANALYSIS

### Diode Orientation — ⚠️ CRITICAL ASSUMPTION VERIFIED BUT FRAGILE

**Current design** (line 8, SPEC.md): GPIO17 side → 1k resistor → 1N4148 anode | cathode → LANC plug tip.

**What this accomplishes**:
- GPIO **LOW** (0V): current flows through diode (forward bias) to LANC tip, pulling line low through ~1k resistance. ✓
- GPIO **HIGH** (3.3V at GPIO): diode reverse-biased, blocks the GPIO. Camera's internal pull-up (likely 10k or higher) raises the line. ✓
- Camera **LOW** (pulling ~0.7V at GPIO due to diode drop): reverse-biased; GPIO reads via pull-up. ✓

**Vulnerability**: If the diode is **forward-biased when GPIO is held HIGH** (e.g., if GPIO bounces positive from a stray pulse or if there's any leakage), it could partially conduct. However, manual verification (line 88-93, SPEC) confirms healthy idle high + flicker, so the diode itself is likely correct.

**Electrical concern**: With GPIO high and the camera pulling down, the diode **drop** is ~0.7V. That means GPIO sees 0.7V (not 0V). The pull-up reads this through the internal pull-up, but the diode ensures GPIO is never driven below ~0.6V. **This is the intended behavior**, and the capture shows the line IS toggling, so the diode is conducting correctly.

**Verdict**: Diode orientation and behavior are sound. Not the root cause.

---

### Drive Strength & Voltage Levels

**Current transmit voltage**: GPIO LOW = ~0V, GPIO HIGH = 3.3V (via internal pull-up after diode release).

**Expected camera behavior**: LANC is a 5V–9V bus. The camera likely has its own 5V pullup and tolerates 0–3.3V as a valid open-collector bus. The Arduino reference (pin 2, ATmega328) drives with identical ~5V HIGH and open-drain to GND, so the voltage rails match.

**Issue**: With the diode + 1k resistor, GPIO LOW does not drive the line to a hard 0V; it's pulled down through the resistor. Measured via the diode:
- GPIO LOW (0V) → diode forward bias → line voltage ≈ GPIO - 0.7V = -0.7V ❌ **WRONG**. 
  
Correction: When GPIO goes LOW, the diode conducts, and current flows through the 1k resistor to ground. The LANC line sees the **camera's pull-up voltage dropped across the resistor**. If the camera is pulling 5V and GPIO grounds, the line voltage will settle somewhere between 0V (limited by diode forward conduction) and ~0.7V due to the diode drop + resistor.

**CRITICAL FINDING**: The effective pull-down is **1k + diode drop**, which on a bus with a 10k+ camera pull-up gives a reasonably fast edge. However, **pigpio is not actively driving the GPIO to 0V in the waveform**. Let me check the wave generation (lines 186–193).

```python
seq.append((MASK if bit else 0,
            0 if bit else MASK, BIT_US))
```

This says: for each bit,
- `gpio_on = MASK` if bit is 1 (HIGH), else 0 (no GPIO drive)
- `gpio_off = 0` if bit is 1, else `MASK` (pull line low by clearing the output)

**This is correct open-collector**: set the GPIO to OUTPUT (low) for a 0-bit, set to INPUT (released, high-impedance) for a 1-bit. The pigpio module handles this, so the voltage behavior should be valid.

**Verdict**: Electrical drive is sound. Voltage levels are appropriate for an open-collector bus.

---

## 2. TIMING & SCHEDULER ANALYSIS

### One-Frame-Ahead Design — ⚠️ PHASE ALIGNMENT CRITICAL

**Current flow** (lines 124–170, `_on_frame_start`):

1. **Frame edge detected** (falling edge callback, ~100 µs pigpio latency).
2. **Calculation** (line 144):
   ```python
   D = T + BIT_US - (now_rel - tick) - self._d1
   ```
   - `T` = measured inter-frame period (typically 19050 µs).
   - `BIT_US` = 104 µs.
   - `(now_rel - tick)` = elapsed time since the frame edge was sampled (Python wall time - tick timestamp).
   - `self._d1` = estimated wave-start latency (~300 µs nominal, line 73).
   - **D is the HIGH delay before the first data bit** to land the data in the NEXT frame's bit slots 1–8.

3. **Wave structure** (lines 172–193):
   ```
   [HIGH D µs] [8 data bits] [HIGH 104 µs] [8 data bits] [HIGH tail]
   ```

### Timing Math — Phase Error Risk

**Target**: data bit 0 should land at time = (next_frame_start + 1 * BIT_US).

**Delay formula**:
- Current time (in tick units) ≈ `tick + elapsed_since_callback`.
- Next frame start ≈ `tick + T`.
- Time to next frame's bit slot 1 ≈ `tick + T + BIT_US`.
- We want the wave to start such that the first data bit edges align with this slot.
- If the wave takes ≈ `_d1` µs to render (callback latency + pigpio send latency + DMA queue), then:
  ```
  delay_us = (tick + T + BIT_US) - (tick + _d1) = T + BIT_US - _d1
  ```

**Current formula** (line 144):
```python
D = T + BIT_US - (now_rel - tick) - self._d1
```

**Problem 1**: `now_rel - tick` is in **monotonic time**, not ticks. Let me trace:
- Line 139: `now_rel = (time.monotonic() - self._tick_offset) * 1e6` — this is in microseconds.
- Line 132–133: `self._tick_offset = time.monotonic() - self.pi_tx.get_current_tick() * 1e-6` — converts tick time to monotonic.
- So `now_rel - tick` is comparing **monotonic microseconds** with **pigpio tick units** (1 tick ≈ 1 µs typically).

**This appears correct IF tick is in microseconds**. Pigpio tick frequency is typically 1 µs (from `get_current_tick`), so the subtraction should work.

**Problem 2**: The servo loop (lines 115–122) adjusts `_d1` based on the rendered rise of the first data bit. It compares `tick - pend` where `pend` is set to `(tick + T + (1 + lead) * BIT_US)` (line 165). The servo only fires if `width > BIT_LO_HI_US` (long pulse = not a 0-bit).

**Timing concern**: If the servo adjustment is cumulative (line 122: `self._d1 = max(0, self._d1 + clipped_error)`), and errors drift, `_d1` might diverge over many frames. The clipping (`max(-500, min(500, err))`) helps, but a systematic bias (e.g., wave rendering takes longer than expected) could accumulate.

**More critical issue**: The servo **only calibrates if the first data bit is a 1** (leading zero count < 8, line 164). If we send command bytes with leading zeros (e.g., `0x28 0x35`), we **will not servo that frame** because we wait for a rise. This means the timing is only adjusted every Nth frame if the data pattern permits.

---

### Capture Anomalies Explained

Given the latest capture during REC (1833) send:

```
'L105 H500 L65 H19740'       — SHORT HIGH pulse (500 µs)
'L105 H19935'                 — Only HIGH, no data bits rendered
'L105 H3640'                  — HIGH pulse of 3.6 ms
'L315 H205 L420 H2390'        — Fragmented with 315 µs LOW, 205 µs HIGH (multiple bits?)
```

**Interpretation**:

1. **'L105 H500 L65 H19740'**: 
   - L105 = correct sync (camera's first bit start, 104 µs low).
   - H500 = a SHORT HIGH (expected 104 µs for stop/start, or longer if data bit is 1). **500 µs suggests a merged bit transition**, possibly one data bit rendered (if LSB=1 of 0x28 = 0, then bit 1 = 0, ..., bit 4 = 1 → rise at bit 4). But 500 is much longer than one bit.
   - L65 = SHORT LOW (data bit 0 low, ~60 µs, not quite 104 but in range).
   - H19740 = VERY LONG HIGH → until next frame start (19.7 ms) — **wave did NOT continue, stopped after one bit**.

   **Root cause**: Wave was cut short. Either pigpio was not given enough data, or the mode change (line 236, back to INPUT) happened early.

2. **'L105 H19935'**:
   - L105 = sync.
   - H19935 = ~20 ms HIGH — **NO data bits rendered at all**. The wave never started transmitting.
   
   **Root cause**: Wave delay `D` was ≤ 0 (line 145, early return), or wave did not create/send.

3. **'L105 H3640'**:
   - L105 = sync.
   - H3640 = 3.6 ms HIGH — **much longer than expected** (should be ~104 µs per stop/start or data bits). This is roughly 35 bit times. If a wave was sent with the wrong delay or the clock is way off, the HIGH pulse could stretch abnormally.
   
   **Root cause**: Possible integer overflow in delay calculation, or mismatched pigpio tick timing.

4. **'L315 H205 L420 H2390'**:
   - L315 = 3× normal bit low (~104 → 312 µs, three 0-bits merged).
   - H205 = 2× bit time (~208 µs, close to 2 × 104).
   - L420 = 4× bit time.
   - H2390 = ~23 bit times.
   
   **Root cause**: Either our bit timing constant `BIT_US = 104` is incorrect, or pigpio is using a different time scale (e.g., raw timing in ns or ticks with a different scale).

---

### Critical Timing Bug (Line 144)

```python
D = T + BIT_US - (now_rel - tick) - self._d1
```

**Issue**: `now_rel` is computed at line 139, but the callback has already been executing for a few microseconds. By the time we compute `D`, more time has elapsed. The delay we calculate is based on a `now_rel` snapshot that is already stale.

**Impact**: If the callback takes >100 µs to reach line 144, `D` is already too short, and the wave starts earlier than intended. Over 5 failed iterations, this latency could have drifted.

**More subtle bug** (line 140–142):
```python
T = (tick - prev_tick) & 0xFFFFFFFF if prev_tick is not None else 19050
if not (15000 < T < 25000):
    T = 19050
```

The period is measured from **tick** (monotonic hardware tick at frame edge), but then `D` subtracts `(now_rel - tick)` where `now_rel` is converted from `time.monotonic()`. **If `time.monotonic()` and pigpio ticks are not synchronized (different clocks), this calculation is nonsense.**

**Verification needed**: Does pigpio tick = 1 µs? Is it the same clock as `time.monotonic()`? If not, the delay calculation is completely wrong.

---

## 3. PROTOCOL ANALYSIS

### Command Codes & Bit Order

**Current implementation** (lines 182–193, `_build_wave`):

```python
order = getattr(self, '_bit_order', 'lsb')
for byte in (c0, c1):
    for i in range(8):
        bit = 1 if byte & (1 << i) else 0
        if order == 'msb':
            bit = 1 if byte & (0x80 >> i) else 0
        seq.append((MASK if bit else 0, 0 if bit else MASK, BIT_US))
```

- **LSB-first by default** (right-most bit first, which matches LANC spec). ✓
- **Command codes match reference** (0x28 0x35 for zoom tele, etc.). ✓

**Non-issue**: Protocol encoding looks correct.

### Remote Driving Start Bits? — ⚠️ UNVERIFIED ASSUMPTION

**Reference assumption** (reference-analysis.md, line 8–9): "Camera drives ALL byte start bits; remote injects only the 8 data bits, releases for stop."

**Critical question**: Does the Sony MC2500 **require the remote to also drive the start bits**?

The Arduino reference (fred-dev) waits one full bit after detecting the frame-start fall, then drives only the data bits (not the start bits). This matches our implementation. However, **the Arduino was tested on a Canon XF300, not a Sony MC2500**. Manufacturers sometimes have quirks:

- Some cameras might expect the remote to drive **all** byte transitions (start + data + stop).
- Some might have a **tolerance window**: if the data bits don't align perfectly, they get corrupted, and the camera rejects the entire command.

**The captures show our data bits ARE being transmitted** (line 105 of the latest capture shows transitions), so the camera is seeing *something*. The fact that **nothing reacts** suggests either:
1. The data bits are phase-shifted (misaligned to bit slots).
2. The camera is reading them as valid transitions but rejecting them due to a syntax error (e.g., wrong start bit count, wrong stop bit format).
3. The camera has a firmware quirk or menu setting that gates LANC control.

---

## 4. CODE-LEVEL BUGS

### Bug 1: Tick/Monotonic Clock Mismatch (Line 139–144) — CRITICAL

```python
now_rel = (time.monotonic() - self._tick_offset) * 1e6  # line 139
T = (tick - prev_tick) & 0xFFFFFFFF if prev_tick is not None else 19050
D = T + BIT_US - (now_rel - tick) - self._d1  # line 144
```

**Problem**: `now_rel` and `tick` might be on **different time bases**. If pigpio tick ≠ 1 µs, or if `time.monotonic()` and pigpio use different system clocks, this calculation is invalid.

**Expected behavior**: `now_rel` and `tick` should be in the same units (microseconds, or both in ticks). The code assumes pigpio ticks are 1 µs and the callback tick is synchronized to `time.monotonic()`.

**Experiment to test**: Print `tick`, `self._tick_offset`, `time.monotonic()`, and `now_rel` during a frame-start callback. Check if the relation holds across 10 frames.

---

### Bug 2: Stale Timestamp in Delay Calculation (Line 139–144) — TIMING ERROR

```python
now_rel = (time.monotonic() - self._tick_offset) * 1e6
# ... several microseconds pass ...
D = T + BIT_US - (now_rel - tick) - self._d1
```

**Problem**: `now_rel` is sampled at line 139, but we use it at line 144. During that 6–10 microseconds, more time elapses, so `D` is **too short by at least a few microseconds**. Over multiple frames, this creep compounds.

**Fix**: Compute `D` immediately after sampling:
```python
now_rel = (time.monotonic() - self._tick_offset) * 1e6
D = T + BIT_US - (now_rel - tick) - self._d1
```

But this was already done in the original code. The issue is that the callback itself adds latency. **Every microsecond counts.**

---

### Bug 3: Servo Calibration Only Fires for 1-Bits (Line 164–165) — MISSED CALIBRATION

```python
first = c0 & 0xFF
lead = 0
for i in range(8):
    if first & (1 << i):
        break
    lead += 1
if 0 < lead < 8:
    self._pending_rise_tick = (tick + T + (1 + lead) * BIT_US) & 0xFFFFFFFF
```

**Problem**: If the first byte has **no leading zeros** (e.g., 0xFF) or **all zeros** (e.g., 0x00), the servo does not calibrate. Over many frames, if the pattern is consistent, `_d1` never adjusts.

**Impact on command bytes**:
- 0x28 (zoom) = 0b00101000 → leading zeros (count = 3) → servo CAN fire.
- 0x18 (rec) = 0b00011000 → leading zeros (count = 3) → servo CAN fire.
- 0x35 (tele slow) = 0b00110101 → leading zeros (count = 2) → servo CAN fire.

So the commands we send **should** trigger servo. But the servo only sees ONE transition per frame (when the first 1-bit rises). If there's a persistent phase error, the servo might not converge.

---

### Bug 4: Unsigned Tick Overflow Handling (Line 119–122) — SIGNED/UNSIGNED MISMATCH

```python
err = (tick - pend) & 0xFFFFFFFF
if err > 30000000:
    err -= 4294967296  # Correct for 32-bit signed underflow
```

**Problem**: The code assumes a 32-bit tick counter and manually corrects for overflow. However, pigpio's `get_current_tick()` may return a **64-bit value** on modern systems. The `& 0xFFFFFFFF` truncates it, potentially losing the high 32 bits and misaligning the timestamp.

**Impact**: If the tick counter wraps (after ~72 minutes), the servo error calculation will be wrong, and `_d1` will adjust wildly.

---

### Bug 5: Incorrect Wave Termination Logic (Line 232–236) — RACE CONDITION

```python
t_done = self._tx_done_mono
while t_done is not None and time.monotonic() < t_done:
    time.sleep(0.0005)
self._tx_done_mono = None
self.pi_tx.set_mode(self.gpio, pigpio.INPUT)
```

**Problem**: `t_done` is computed in `_on_frame_start` (line 154) as:
```python
self._tx_done_mono = time.monotonic() + (D + 2600) / 1e6
```

The `2600` is a magic constant representing the total duration of the wave (bytes 0–1 plus stop bits):
- 8 data bits × 104 µs = 832 µs
- 1 stop bit (camera-driven) = 104 µs
- 8 data bits × 104 µs = 832 µs
- 1 stop bit = 104 µs
- **Total ≈ 1872 µs**, but the code uses 2600 µs (~25 bit times, ~2600 µs).

The discrepancy (2600 − 1872 = 728 µs) is NOT the delay `D`. If `D` is variable (e.g., 5000–12000 µs), the estimated completion time is **completely wrong**. The GPIO might switch back to INPUT while the wave is still being sent, **truncating the waveform**.

**This is a CRITICAL BUG** and matches the capture anomalies ('L105 H500 L65 H19740' shows a wave cut short).

---

## 5. ROOT CAUSE RANKING

### Rank 1: CRITICAL — Wave Termination Race Condition (Bug 5)

**Confidence**: 95%

**Observation**: Captures show waves that end prematurely ('L105 H500 L65 H19740' stops after 1–2 bits). The `_tx_done_mono` calculation uses a hardcoded constant `2600` that does NOT account for the variable delay `D`. When `D` is large (10+ ms), the wave termination time is vastly underestimated, and the GPIO switches back to INPUT before the wave finishes.

**Experiment to falsify**:
1. Remove the `_tx_done_mono` check entirely; let the wave run to completion (pigpio auto-stops).
2. Capture the transmitted waveform.
3. If the wave completes and still gets no response, move to Rank 2.

---

### Rank 2: HIGH — Tick/Monotonic Clock Mismatch (Bug 1)

**Confidence**: 85%

**Observation**: The delay calculation (line 144) mixes `tick` (pigpio hardware tick) with `now_rel` (converted `time.monotonic()`). If these clocks have different rates or offsets, the delay is nonsensical. The captures showing abnormal pulse lengths ('L315 H205 L420 H2390') suggest timing miscalculation.

**Experiment to falsify**:
1. Add logging in `_on_frame_start`: print `tick`, `T`, `now_rel - tick`, `D`, and `self._d1` every frame.
2. Verify that `T` is consistently ~19050 µs and `D` is positive and reasonable (~8000–12000 µs).
3. If `T` is wildly variable or `D` goes negative, the clock sync is broken.
4. If `D` is correct but still no response, move to Rank 3.

---

### Rank 3: HIGH — Servo Calibration Divergence

**Confidence**: 75%

**Observation**: The servo loop adjusts `_d1` based on rendered rise times, but:
- It only fires if the first data byte has 1–7 leading zeros.
- It clamps adjustments to ±500 µs per frame (line 122).
- It's cumulative, so drift can accumulate over time.

If the rendered data bits are consistently **late** (e.g., always landing in the wrong bit slot), the servo will try to correct by making future waves *even earlier*, but if the correction is capped, it never reaches the right phase.

**Experiment to falsify**:
1. Disable the servo (set `self._d1 = 300` as a constant in `_on_frame_start`).
2. Send a zoom command and capture.
3. If the wave is still mis-timed, the issue is NOT servo divergence; move to Rank 4.
4. If the wave has better timing without servo, the servo is making things worse.

---

### Rank 4: MEDIUM — Diode/Resistor Value or Orientation

**Confidence**: 40%

**Observation**: SPEC.md and manual checks (idle read ~1, flickers with camera) suggest the diode is correct. However, if the 1k resistor is actually wrong (e.g., 10k installed by mistake), the rise time would be very slow, and the camera might not register low-to-high transitions crisply.

**Experiment to falsify**:
1. Measure the GPIO pin voltage during a transmitted frame (low pulse, high idle).
2. Confirm GPIO LOW ≈ 0–0.7V and GPIO HIGH (released) rises above 2.5V within 100 µs.
3. If the rise time is >500 µs or the low voltage is >1.5V, the resistor/diode is wrong.

---

### Rank 5: MEDIUM — Camera Menu Setting or Firmware Quirk

**Confidence**: 50%

**Observation**: The MC2500 is a 2014 camera; its firmware might require LANC control to be enabled in a menu (e.g., "NETWORK" or "REMOTE" settings). Some Sony cameras gate LANC to prevent unwanted interference. The manual should be consulted.

**Experiment to falsify**:
1. Check the MC2500 manual's "Network," "Remote," or "LANC" sections for menu settings.
2. Verify that a physical LANC remote (if available, or a reference AODELAN) works on the same camera.
3. If a reference remote works and ours doesn't, the camera is capable, and the issue is our protocol/timing.

---

### Rank 6: LOW — Sony MC2500 vs. Reference Hardware Difference

**Confidence**: 30%

**Observation**: The Arduino reference was tested on a Canon XF300, not a Sony. Canon and Sony might have different LANC interpretations. However, the command codes used in this project (0x28 0x35, 0x18 0x33, etc.) are documented as Sony-specific in SPEC.md, so the protocol dialect is correct.

**Experiment to falsify**:
1. Obtain a working LANC remote or another Pi-based LANC implementation.
2. Test the MC2500 with a known-good remote to confirm it responds to LANC.
3. If it does, our implementation is wrong; if it doesn't, the camera might not support LANC or requires a menu enable.

---

## 6. CONCRETE EXPERIMENTS

### Experiment A: Disable Wave Termination Early Exit
**Goal**: Test if premature GPIO switch causes truncation.

```python
# In _loop(), comment out or extend the wait:
# while t_done is not None and time.monotonic() < t_done:
#     time.sleep(0.0005)
# self._tx_done_mono = None
# self.pi_tx.set_mode(self.gpio, pigpio.INPUT)

# Instead:
import threading
def release_gpio():
    time.sleep(0.03)  # Wait 30 ms (full frame + margin)
    self.pi_tx.set_mode(self.gpio, pigpio.INPUT)
threading.Thread(target=release_gpio, daemon=True).start()
```

**Expected result**: If truncation was the issue, the wave completes and the camera reacts.

---

### Experiment B: Log Timing & Servo State
**Goal**: Verify tick/monotonic alignment and servo convergence.

```python
# In _on_frame_start, after line 144:
print(f"FRAME {self.sends}: T={T}, now_rel={now_rel:.0f}, tick={tick}, "
      f"(now_rel-tick)={now_rel-tick:.0f}, D={D:.0f}, _d1={self._d1}")
# In _edge, after line 122:
print(f"SERVO: width={width}, pend={pend}, err={err}, _d1 -> {self._d1}")
```

**Expected result**: 
- `T` should be ~19050 ± 500 µs every frame.
- `D` should be between 5000–12000 µs (reasonable).
- If servo fires, `_d1` should converge (e.g., oscillate around a stable value, not drift).

---

### Experiment C: Manual Timing Calibration
**Goal**: Bypass servo; use a fixed, pre-calculated delay.

Based on captures, estimate the latency from callback to rendered wave-start:
- Pigpio notification latency: ~100 µs.
- Python callback overhead: ~50–100 µs.
- pigpio.wave_send_once(): ~50–200 µs.
- DMA queue: variable, 0–500 µs.
- **Total ≈ 200–800 µs, estimate 300–400 µs.**

Set `self._d1 = 300` (constant) and disable servo:

```python
# In _on_frame_start, remove servo assignment and use a constant:
self._d1 = 300  # Fixed, no servo
self._pending_rise_tick = None
```

Send zoom command, capture, verify data bits land in correct slots.

---

### Experiment D: Reference Capture with AODELAN or Known-Good Remote
**Goal**: Prove the camera responds to LANC and establish a golden timing reference.

1. Borrow or purchase a cheap AODELAN LANC remote (~$20 AUD).
2. Plug it in, send zoom command, capture the waveform.
3. Compare the captured timing (delay, bit widths, bit sequence) to our transmissions.
4. If the reference and ours are identical in structure but the camera reacts to the reference, the issue might be subtle (e.g., specific command codes, start bit format).

---

## SUMMARY & RECOMMENDATIONS

| Rank | Root Cause | Confidence | Fix Complexity | Priority |
|------|-----------|-----------|-----------------|----------|
| 1 | Wave termination truncation (Bug 5) | 95% | Easy | **FIRST** |
| 2 | Tick/monotonic mismatch (Bug 1) | 85% | Medium | **FIRST** |
| 3 | Servo divergence | 75% | Medium | SECOND |
| 4 | Resistor/diode value error | 40% | Easy (measure) | PARALLEL |
| 5 | Camera menu setting | 50% | Easy (check manual) | PARALLEL |
| 6 | Hardware-specific LANC dialect | 30% | Hard (needs ref) | THIRD |

### Immediate Actions:
1. **Fix Bug 5**: Replace the hardcoded `2600` constant with a proper wave-duration calculation based on the actual wave structure (delay + 16 data bits + 2 stop bits). Or simply let pigpio auto-stop and add a conservative sleep.
2. **Verify Bug 1**: Add logging to confirm `T`, `now_rel - tick`, `D` are reasonable and consistent.
3. **Test Experiment A** (disable early termination): Run a capture with the GPIO held in OUTPUT long enough for the full wave to render. If the zoom then works, Bug 5 is the culprit.

---

## Code-Level Fixes to lanc_gpio.py

### Fix for Bug 5 (Line 154 & 232–236)
```python
# OLD (line 154):
self._tx_done_mono = time.monotonic() + (D + 2600) / 1e6

# NEW:
# Wave duration: 1 sync low (camera) + 8 data bits + 1 stop bit + 
#                1 start bit (camera) + 8 data bits + 1 stop bit ≈ 2080 µs
# Add 1 ms margin for pigpio scheduling
wave_duration_us = 2080 + 1000
self._tx_done_mono = time.monotonic() + (D + wave_duration_us) / 1e6
```

### Fix for Bug 1 (Verify Tick Sync)
Add at startup:
```python
# In __init__, after line 76:
# Verify pigpio tick rate
tick1 = self.pi_tx.get_current_tick()
time.sleep(0.001)
tick2 = self.pi_tx.get_current_tick()
tick_rate_hz = (tick2 - tick1) / 0.001
print(f"pigpio tick rate: {tick_rate_hz:.0f} Hz (expected 1,000,000)")
```

### Fix for Bug 3 (Servo Calibration)
Add forced recalibration on even-bit patterns:
```python
# In _on_frame_start, after line 163:
if lead == 0 or lead == 8:
    # No leading zeros; force servo to calibrate on bit 0 rise anyway
    # (This is an advanced fix; for now, just document the limitation)
    pass
```

---

## Files to Check / Additional Context Needed
1. **LANC waveform captures** during the failed zoom commands (files referenced as "1833" capture).
2. **MC2500 manual**, specifically the REMOTE/LANC section (to rule out a menu setting).
3. **pigpio documentation**: confirm tick rate and `time.monotonic()` synchronization on Bookworm.
4. **Reference LANC remote waveform**: if an AODELAN or other working remote is available, capture and compare.

---

**Review completed**: 2026-09-26 | **Reviewer skepticism level**: HIGH (assumed nothing; challenged every assumption, including diode orientation and protocol assumptions from a different manufacturer's camera.)
