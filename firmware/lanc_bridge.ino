// LancBridge — PC -> LANC bridge for Sony HXR-MC2500 (2.5mm REMOTE jack)
//
// Wiring: LANC ring --[1k]-- D2 ; LANC sleeve -- GND. Line is open-collector:
// we READ with pin as INPUT, transmit a 0-bit by driving OUTPUT LOW.
// NEVER drive the pin HIGH.
//
// Serial 115200. Commands (newline-terminated):
//   rec zoomin zoomout zoomin_fast zoomout_fast focusnear focusfar
//   aftoggle irisopen irisclose poweroff display stop
// Each frame is echoed as: "F <8 hex bytes space-separated>"

const uint8_t LANC_PIN = 2;
const unsigned int BIT_US = 104;          // LANC bit time (9600 baud timing)
const unsigned int START_MIN_US = 1200;   // start-bit pulse (1200-1400 us)
const unsigned int START_MAX_US = 1500;

uint8_t cmd[2] = {0x00, 0x00};            // current command (what we send)
uint8_t cmdHold = 0;                      // frames remaining for one-shot cmds
uint8_t frameIn[8];                       // last full frame from camera

// bit0 of cmdByte0 is a 1 for command classes we use (0x18/0x28 have LSB 0/0)
// LANC transmits LSB-first.

// Release the line (Hi-Z) so the camera pulls it high.
static inline void lineRelease() { pinMode(LANC_PIN, INPUT); }
// Pull the line low (open-collector "0").
static inline void lineLow() {
  digitalWrite(LANC_PIN, LOW);
  pinMode(LANC_PIN, OUTPUT);
}

// Wait for the camera's start bit (line goes low), with timeout.
static bool waitForStartBit() {
  lineRelease();
  uint32_t t0 = micros();
  while (digitalRead(LANC_PIN) == HIGH) {
    if (micros() - t0 > 25000UL) return false;  // no frame this cycle
  }
  return true;
}

// Send one byte LSB-first, replacing the camera's byte slot.
// Caller must have already consumed the start bit.
static void sendByte(uint8_t b) {
  lineLow();                    // our start bit (we echo a start bit)
  delayMicroseconds(BIT_US);
  for (uint8_t i = 0; i < 8; i++) {
    if (b & (1 << i)) lineRelease(); else lineLow();
    delayMicroseconds(BIT_US);
  }
  lineRelease();                // stop bit
  delayMicroseconds(BIT_US);
}

// Receive one byte (8 bits after its start bit) while camera drives the line.
// We sample near the middle of each bit. Start bit already consumed.
static uint8_t recvByte() {
  uint8_t b = 0;
  for (uint8_t i = 0; i < 8; i++) {
    delayMicroseconds(BIT_US / 2);
    if (digitalRead(LANC_PIN) == HIGH) b |= (1 << i);
    delayMicroseconds(BIT_US / 2);
  }
  delayMicroseconds(BIT_US);    // skip stop bit
  return b;
}

void setup() {
  Serial.begin(115200);
  lineRelease();
}

void handleSerial() {
  static char buf[24];
  static uint8_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      buf[n] = 0;
      if (n) applyCommand(buf);
      n = 0;
    } else if (n < sizeof(buf) - 1) {
      buf[n++] = c;
    }
  }
}

void applyCommand(const char *s) {
  bool hold = false, oneshot = false;
  if      (!strcmp(s, "rec"))          { cmd[0]=0x18; cmd[1]=0x33; oneshot=true; }
  else if (!strcmp(s, "poweroff"))     { cmd[0]=0x18; cmd[1]=0x5E; oneshot=true; }
  else if (!strcmp(s, "display"))      { cmd[0]=0x18; cmd[1]=0xB4; oneshot=true; }
  else if (!strcmp(s, "zoomin"))       { cmd[0]=0x28; cmd[1]=0x35; hold=true; }
  else if (!strcmp(s, "zoomout"))      { cmd[0]=0x28; cmd[1]=0x37; hold=true; }
  else if (!strcmp(s, "zoomin_fast"))  { cmd[0]=0x28; cmd[1]=0x39; hold=true; }
  else if (!strcmp(s, "zoomout_fast")) { cmd[0]=0x28; cmd[1]=0x3B; hold=true; }
  else if (!strcmp(s, "focusnear"))    { cmd[0]=0x28; cmd[1]=0x47; hold=true; }
  else if (!strcmp(s, "focusfar"))     { cmd[0]=0x28; cmd[1]=0x45; hold=true; }
  else if (!strcmp(s, "aftoggle"))     { cmd[0]=0x28; cmd[1]=0x41; oneshot=true; }
  else if (!strcmp(s, "irisopen"))     { cmd[0]=0x28; cmd[1]=0x55; hold=true; }
  else if (!strcmp(s, "irisclose"))    { cmd[0]=0x28; cmd[1]=0x54; hold=true; }
  else if (!strcmp(s, "stop"))         { cmd[0]=0x00; cmd[1]=0x00; hold=false; cmdHold=0; Serial.println("OK stop"); return; }
  else { Serial.print("ERR unknown: "); Serial.println(s); return; }

  cmdHold = oneshot ? 5 : 255;   // 5 frames for one-shots; 255 (~5s) for holds
  Serial.print("OK "); Serial.println(s);
}

void loop() {
  if (!waitForStartBit()) { handleSerial(); return; }   // idle / no camera
  handleSerial();

  // Words 0 and 1: WE drive these bytes.
  uint8_t c0 = cmd[0], c1 = cmd[1];
  sendByte(c0);
  sendByte(c1);

  // Words 2..7: camera drives; we read.
  for (uint8_t w = 2; w < 8; w++) frameIn[w] = recvByte();
  frameIn[0] = c0;
  frameIn[1] = c1;

  // Echo frame to PC
  Serial.print('F');
  for (uint8_t w = 0; w < 8; w++) {
    Serial.print(' ');
    if (frameIn[w] < 0x10) Serial.print('0');
    Serial.print(frameIn[w], HEX);
  }
  Serial.println();

  if (cmdHold != 255 && cmdHold > 0) {
    if (--cmdHold == 0) { cmd[0] = 0x00; cmd[1] = 0x00; }
  } else if (cmdHold == 255) {
    static uint8_t holdFrames = 0;
    if (++holdFrames >= 240) { cmd[0] = 0x00; cmd[1] = 0x00; holdFrames = 0; }  // ~5s safety
  }
}
