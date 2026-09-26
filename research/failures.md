# DIY LANC Transmitter Failures and Fixes - Research Summary

**Date:** September 2026  
**Context:** Investigating why Sony HXR-MC2500 ignores LANC commands with correct timing/bytes

---

## 1. Documented Cases: Correct Timing But Camera Ignores Commands

### 1.1 Voltage Level Problems

#### Case: Sony VX2100 / HVR-Z5U - 3V LANC Signal, Not 5V
**Source:** [Arduino Forum - Does clock rate change with power supply voltage](https://forum.arduino.cc/t/does-clock-rate-change-with-power-supply-voltage/57394)

> "It turns out that despite EVERYTHING I have read about the LANC protocol, my particular Sony cameras (the old VX2100 and a newer HVR-Z5U) produce a LANC signal that swings between +3v and 0v, not between +5v and 0v."

**Fix:** Use a transistor level converter to amplify 3V LANC signal to 5V for Arduino digital pin detection. This also inverts the signal (bonus: removes need for software inversion).

**Key Insight:** Many Sony cameras output **3V-3.3V** on LANC, not the "5V" stated in protocol docs. A 3.3V GPIO trying to read this on a 5V input threshold may see intermittent failures.

---

#### Case: Raspberry Pi Pico (3.3V) LANC Controller
**Source:** [EEVblog Forum - Raspberry Pi Pico LANC controller - Weird transistor issue](https://www.eevblog.com/forum/beginners/raspberry-pi-pico-lanc-controller-weird-transistor-issue/)

**Problem:** Standard Arduino LANC circuits designed for 5V don't work with 3.3V Pico.

**Quote from search result:** "The schematic is for a 5V development board... The Raspberry pico is a 3.3V based development board..."

**Fix:** Level shifter circuit required. For open-collector TX, the GPIO must still pull to true 0V (ground) when driving low - **camera samples the absolute voltage, not relative.**

---

#### Case: Panasonic HC-X20 Uses 3.3V Instead of 5V
**Source:** [DVinfo.net - Panasonic HC-X20 LANC Control](https://dvinfo.net/forum/panasonic-hc-series-camcorders/539722-panasonic-hc-x20-lanc-control-my-findings-video-post1971948.html)

> "It seems that the Sony standard uses 5V default voltage level, but Panasonic uses 3.3V. This may be the case to stay compatible with older remotes – but I don't know exactly."

**Note:** Confirms voltage variation between manufacturers/models.

---

### 1.2 Low Voltage on LOW State (Not Reaching True 0V)

#### Case: Open-Collector Not Pulling Low Enough
**Source:** [boehmel.de/lanc.htm - The SONY LANC Protocol](https://www.boehmel.de/lanc.htm) (authoritative reference)

**Protocol Specification:**
> "The LANC bus is open collector so it is normally pulled high to about 5v and is **pulled low to send commands**... An external unit that wants to send data uses an open-collector type TTL gate that **does nothing to send a +5V signal, and grounds the line to send a 0V signal.**"

**Problem with Series Diode/Resistor:** If using a series diode (1N4148 or similar) for protection, the voltage drop (~0.6-0.7V) means your "LOW" is 0.6-0.7V, not 0V. Some cameras have tight thresholds and may not recognize this as LOW.

**Fix Options:**
1. Remove series diode if possible (accept risk to GPIO)
2. Use logic-level N-channel MOSFET as open-drain driver (source to GND, drain to LANC)
3. Ensure direct connection to ground through transistor (collector/drain to LANC line, emitter/source to GND)

---

### 1.3 Timing Issues

#### Case: ATtiny84/85 delayMicroseconds() Inaccuracy
**Source:** [Arduino Forum - LANC remote control using ATTiny84a help request](https://forum.arduino.cc/t/lanc-remote-control-using-attiny84a-help-request/1372560)

**Problem:** Internal oscillator not calibrated, causing bit timing drift.

**Solution Found:**
> "It turned out that pulseIn() function was working fine. I still suspect that delayMicroseconds() was the culprit. To troubleshoot this sketch first make sure your internal oscillator is calibrated. Then tweak the TOP value argument for the function delayTimer() in sendBytes() and sendLanc(). For me 101 was enough to obtain good results."

**Key Fix:** Calibrate internal oscillator AND tune bit duration to ~101-104µs based on actual measurement.

---

#### Case: ESP32 pulseIn() on Wrong Pin
**Source:** [Arduino Forum - Help with pulseIn alternative for ESP32](https://forum.arduino.cc/t/help-with-pulsein-alternative-for-esp32/1267826)

> "I think I have solved the problem I originally posed. The issue was not with PulseIn which seems to be working fine now that I have switched to another pin on the ESP32!"

**Fix:** Try different GPIO pins - some ESP32 pins have different input characteristics.

---

#### Case: Panasonic Needs Interrupt-Based Timing
**Source:** [DVinfo.net - Panasonic HC-X20 LANC Control](https://dvinfo.net/forum/archive/index.php/t-539722.html)

> "The timing of signal transmission seems critical to Panasonic. I used fixed timings first, but it seems it's needed to use 'interrupt logic' to react exactly when camera signals that commands can be sent."
> "My electronics circuit seemed to cause 'imprecise' timings when transferring control codes. So I used an Oscilloscope with 'protocol decoder' to tweak & validate Arduino transmission timings until everything could be 'read' via Oscilloscope."

**Fix:** Use oscilloscope to verify timing; consider interrupt-driven bit sync.

---

### 1.4 Inverted Logic Errors

#### Case: AI-Generated Circuit Had Inverted TX Logic
**Source:** [DVinfo.net - Panasonic HC-X20 LANC Control](https://dvinfo.net/forum/panasonic-hc-series-camcorders/539722-panasonic-hc-x20-lanc-control-my-findings-video-post1971948.html)

> "As AI helped me with the electronics circuit, I understood far late that the Arduino transmission logic was inverted. So if the camera doesn't react, check if you run into the same problem with your circuit … and invert the codes per software."

**Fix:** Verify whether your circuit inverts the signal. LANC is **inverted logic**: 
- Logic 1 (data bit = 1) → **0V on line**
- Logic 0 (data bit = 0) → **5V on line** (let pull-up take it high)

If your transistor already inverts, you may be double-inverting.

---

### 1.5 Command Must Be Repeated 4-5 Times

#### Case: Single Command Ignored
**Source:** [boehmel.de/lanc.htm](https://www.boehmel.de/lanc.htm)

> "Oh yeah - and in at least 5 consecutive frames to make sure the VCR hears and understands your command."

**Source:** [fred-dev arduino_lanC documentation](https://github.com/fred-dev/arduino_lanC/blob/master/LANC%20Documenation/sony_lanc.html)

> "The manual also says that you must send every command multiple times to make sure the VCR responds to it. It says: 'In order that the VTR makes the command effective, it is necessary to transmit the same code continuously over 4 fields. Therefore, it is required for the peripheral side to transmit over 5 fields of the same code.'"

**Fix:** Send command for 5 consecutive frames minimum (100ms for PAL, 83ms for NTSC).

---

### 1.6 Power-On Sequence Required

#### Case: Camera Off Won't Respond to LANC Commands
**Source:** [Arduino Forum - Looking for LANC power OFF and power ON commands](https://forum.arduino.cc/t/looking-for-lanc-power-off-and-power-on-comands-arduino-project/73068)

**Problem:** User could power-on camera by holding LANC to ground for 140ms, but camera would turn off again after 2 seconds.

**Workaround Found:**
> "What I am doing is turning the physical on/off switch to the 'on' position. Then I use the power off code to turn the camera off. The physical switch is still in the 'on' position but the camera is off. So when I connect the LANC to ground, the camera starts up again and stays on."

**To Power On:** Connect LANC signal to GND for >140ms.

---

### 1.7 Sony-Specific "Remote Control" Menu Setting

#### Case: LANC/Remote Must Be Enabled in Camera Menu
**Source:** [Facebook discussion on Canon XF605](https://www.facebook.com/groups/canonxf605/posts/1884183228748765/)

> "Check remote control protocol, ensure it's set to standard, and verify the controller is in Sony/Canon mode"

**Source:** [Sony FX6 Manual](https://www.sony.com/electronics/support/res/manuals/5024/c3bfbc891ee0f149e46d142754fd6aa7/50244581M.pdf)

> "Camera Remote Control: Enable / Disable - Sets whether to enable remote control..."

**Fix:** Check camera menu for:
- "Remote Control" → Enable
- "LANC" → Enable  
- "Remote Terminal" → Standard (not proprietary)

**HXR-MC2500 Specific:** Check menu for A/V Remote or LANC settings.

---

### 1.8 Zoom Command Encoding Variations

#### Case: Variable-Speed Zoom Nibble Encoding
**Source:** [AlexNe/arduino_lanc_sample](https://github.com/AlexNe/arduino_lanc_sample)

Different zoom command byte 1 values for different speeds:
- `0x00-0x0E`: Variable speed zoom Tele (0x00=slowest, 0x0E=fastest)
- `0x10-0x1E`: Variable speed zoom Wide
- **Legacy commands that work on older cameras:**
  - `0x35`: Zoom Tele slow (working all cameras since ~1996)
  - `0x37`: Zoom Wide slow
  - `0x39`: Zoom Tele fast
  - `0x3B`: Zoom Wide fast

**Note:** Your `0x28 0x02` zoom command - `0x02` is "variable speed zoom Tele: faster than 00". Try:
- Legacy `0x28 0x35` (slow tele) or `0x28 0x39` (fast tele)
- These may have better compatibility.

---

### 1.9 Blackmagic Camera Specific Issues

#### Case: BMCC/BMPCC Need Different Codes
**Source:** [Blackmagic Forum - LANC remote control commands for BMCC](https://forum.blackmagicdesign.com/viewtopic.php?f=2&t=16676)

User reports:
> "Unfortunately when connecting to BMPC using the codes found in this forum it doesn't do anything!"

Also noted:
> "Ive found that on the Blackmagic Camera it seems that when the chord is plugged in it does not send out 5v. When using the Arduino not plugged into USB its only sending out 0.81-0.82 v of power."

**Fix for BMCC:** Blackmagic cameras may have different LANC voltage and codes than Sony.

---

## 2. Oscilloscope-Verified Real Sony Remote Signals

### 2.1 Voltage Levels
**Source:** [boehmel.de/lanc.htm](https://www.boehmel.de/lanc.htm)

Protocol spec states:
- **HIGH (idle/stop bit/0-bit):** ~5V (pulled up by camera's internal resistor)
- **LOW (start bit/1-bit):** 0V (actively pulled to ground)
- **Voltage depends on model and battery** - can be 3V-8V on some devices

**From Arduino forum case study:**
- Some Sony cameras output **3V**, not 5V
- Threshold for detection is model-specific

### 2.2 Timing (Verified)
- **Bit duration:** 104µs (9600 baud)
- **Inter-byte gap:** Variable, ~1200-1400µs between start bits
- **Frame gap:** 5ms+ (>5000µs) identifies start of new 8-byte frame
- **Frame period:** 20ms (PAL) / 16.6ms (NTSC)

### 2.3 Real Remote Behavior
**Source:** [fred-dev arduino_lanC sony_lanc.html](https://github.com/fred-dev/arduino_lanC/blob/master/LANC%20Documenation/sony_lanc.html)

Sony RM-95 wired remote captured codes:
```
Button              Bytes 0+1
Zoom tele           28 35
Zoom wide           28 37
focus toggle        28 41
focus far           28 45
focus near          28 47
start/stop rec      18 33
edit search -       18 65
edit search +       18 67
rec review          18 69
power               18 5E
```

**Note:** Byte 1 values `0x35`/`0x37` for zoom (not `0x02`) - these are the legacy slow zoom codes.

---

## 3. Pi-Specific LANC Projects That Work With Sony

### 3.1 Raspberry Pi Forum Discussion
**Source:** [Raspberry Pi Forums - LANC Communication](https://forums.raspberrypi.com/viewtopic.php?t=85985)

Only 2 posts, no working solution shared. Mentions:
- Protocol: 9600 baud single-wire serial
- First 4 bytes from controller, last 4 from camera
- Timing critical

**No Pi-specific working circuits found** in the forums searched.

### 3.2 Arduino-Based Circuits (Adaptable to Pi)
**Source:** [Control Your Camera - Arduino LANC](http://controlyourcamera.blogspot.com/2011/02/arduino-controlled-video-recording-over.html)

**Martin Koch Circuit (widely used):**
```
Components:
- 1× NPN transistor (2N2222 or similar)
- 1× 4.7kΩ resistor (R1 - base resistor)
- 1× 10kΩ resistor (R2 - pull-up)
- 1× 5.1V Zener diode (D1 - protection)

Connections:
- Arduino Pin 7 → R1 (4.7kΩ) → Transistor Base
- Transistor Emitter → GND
- Transistor Collector → R2 (10kΩ) → +5V
- Transistor Collector → LANC Ring
- Arduino Pin 11 → LANC Ring (read)
- D1 (Zener) → Collector to GND (cathode to collector)
- LANC Sleeve → GND
```

**Note:** This circuit was designed for 5V Arduino. For 3.3V Pi:
- Add level shifter for GPIO protection
- Or use MOSFET-based open-drain driver
- Verify GPIO input threshold can detect 3V LANC signal

### 3.3 Single-Pin LANC Interface
**Source:** [Arduino Forum - LANC remote control using ATTiny84a](https://forum.arduino.cc/t/lanc-remote-control-using-attiny84a-help-request/1372560)

User DC42's suggestion:
> "All that is needed is one I/O pin with the 4K7 pullup to +5V and a 100 ohm series resistor to Lanc signal. The open-collector output can be emulated by switching the pin mode (or data direction register) to Output to send a Low, and Input to send a High."

**Single-Pin Circuit:**
```
GPIO ──┬── 100Ω ──── LANC Signal (Ring)
       │
      4.7kΩ
       │
      +5V
```

**Driving LOW:** Set pin as OUTPUT LOW (drives through 100Ω to ~0V)  
**Driving HIGH:** Set pin as INPUT (lets pull-up take line high)

---

## 4. Summary of Common Fixes to Try

| Problem | Symptom | Fix |
|---------|---------|-----|
| LOW voltage not low enough | Camera ignores commands | Remove series diode; use direct transistor to GND |
| 3.3V GPIO, 5V LANC | Intermittent detection | Level shifter or transistor buffer |
| Camera LANC only 3V | GPIO doesn't detect highs | Lower threshold detection; different GPIO pin |
| Single command sent | Command ignored | Send 5+ consecutive frames |
| Wrong timing | Commands corrupted | Calibrate oscillator; verify with scope |
| Inverted logic | Commands wrong | Check if circuit inverts; adjust software |
| Wrong zoom encoding | Zoom doesn't work | Try legacy codes 0x35/0x37/0x39/0x3B |
| Remote disabled in menu | All commands ignored | Enable LANC/Remote in camera menu |
| Camera fully off | No response | Power-on: hold LANC to GND for >140ms |

---

## 5. Recommended Next Steps for HXR-MC2500

1. **Verify Camera Menu:** Check for LANC/Remote Control/A/V Remote enable setting
2. **Measure Actual Voltages:** Oscilloscope capture of camera's LANC output voltage (may be 3V not 5V)
3. **Check LOW Voltage:** Measure your TX LOW level - must be <0.3V for reliable detection
4. **Try Legacy Zoom Codes:** Use `0x28 0x35` instead of `0x28 0x02`
5. **Verify 5-Frame Repeat:** Ensure command is sent for minimum 5 consecutive frames
6. **Test with Commercial Remote:** If available, verify camera responds to any LANC commands
7. **Consider Pull-up Value:** Camera's internal pull-up may be weak - try adding external 4.7kΩ to 5V

---

## Sources Referenced

1. https://www.boehmel.de/lanc.htm - Comprehensive LANC protocol documentation
2. https://forum.arduino.cc/t/does-clock-rate-change-with-power-supply-voltage/57394
3. https://forum.arduino.cc/t/lanc-remote-control-using-attiny84a-help-request/1372560
4. https://forum.arduino.cc/t/looking-for-lanc-power-off-and-power-on-comands-arduino-project/73068
5. https://forum.arduino.cc/t/help-with-pulsein-alternative-for-esp32/1267826
6. https://dvinfo.net/forum/panasonic-hc-series-camcorders/539722-panasonic-hc-x20-lanc-control-my-findings-video-post1971948.html
7. https://github.com/fred-dev/arduino_lanC
8. https://github.com/AlexNe/arduino_lanc_sample
9. https://forum.blackmagicdesign.com/viewtopic.php?f=2&t=16676
10. http://controlyourcamera.blogspot.com/2011/02/arduino-controlled-video-recording-over.html
11. https://forums.raspberrypi.com/viewtopic.php?t=85985
12. https://www.eevblog.com/forum/beginners/raspberry-pi-pico-lanc-controller-weird-transistor-issue/
13. https://www.eevblog.com/forum/beginners/very-simple-lanc-controller/
14. https://www.sony.com/electronics/support/articles/00027561
15. https://projects.esac.org.uk/SonyLancControl.asp
16. https://d1.amobbs.com/bbs_upload782111/files_44/ourdev_667831AK3GIT.pdf - Sony Lan-C & Control-L detailed doc
