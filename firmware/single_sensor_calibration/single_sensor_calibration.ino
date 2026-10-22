/* ===========================================================================
   Single-sensor calibration firmware (Teensy)

   Reads one MLX90393 Hall-effect sensor (address 0x0C on Wire: SDA 18, SCL 19)
   and streams its raw field over USB serial to ATI_App, which pairs each
   force/torque record of the calibration rig with the latest sample.

   Sensor : register 0x00 = 0x005C (GAIN_SEL 5), register 0x02 = 0x02B4;
            identical to the helmet 12-sensor firmware
   Axes   : X and Y are swapped and inverted so that the rig's X stage moves
            Raw_Bx and its Y stage moves Raw_By; Z is the sensor's own axis
   Serial : 115200 baud. "BOOT:READY" (or "CONFIG ERROR"), then the header
                Raw_Bx,Raw_By,Raw_Bz,Status
            followed by lines such as  -314,-1778,7061,2
   Timing : a new measurement roughly every 100 ms; the latest value is
            printed on every pass of the main loop

   Used to record every sensor calibration in the study.
   =========================================================================== */
#include <Arduino.h>
#include <Wire.h>

/* ---------------------------------------------------------------------------
   MLX90393 I2C ADDRESS
   Default: 1:0x0C
   Alternatives: 2:0x0D, 3:0x0E, 4:0x0F
--------------------------------------------------------------------------- */
const uint8_t SENSOR_ADDR = 0x0C;

/* ---------------------------------------------------------------------------
   MLX90393 COMMANDS
--------------------------------------------------------------------------- */
const uint8_t CMD_WRITE_REG   = 0x60; // Write configuration register
const uint8_t CMD_MEASURE_ZYX = 0x3E; // Start single measurement on Z,Y,X
const uint8_t CMD_READ_MEAS   = 0x4E; // Read measurement data

/* ---------------------------------------------------------------------------
   TIMING & RETRIES
--------------------------------------------------------------------------- */
const unsigned long I2C_READ_TIMEOUT_MS  = 80;   // Max wait for I2C bytes
const unsigned long MEASUREMENT_DELAY_MS = 50;   // Measurement interval (ms)
const int MAX_WRITE_RETRIES              = 3;    // Retry count if NACK received

/* ---------------------------------------------------------------------------
   GLOBAL VARIABLES
--------------------------------------------------------------------------- */
volatile int16_t Raw_Bx = 0;   // Latest X-axis (after mapping)
volatile int16_t Raw_By = 0;   // Latest Y-axis (after mapping)
volatile int16_t Raw_Bz = 0;   // Latest Z-axis (after mapping)
volatile uint8_t LastStatus = 0;

/* ---------------------------------------------------------------------------
   Safe I2C write with retries
--------------------------------------------------------------------------- */
bool safeWrite(uint8_t addr, const uint8_t* buf, size_t len, bool noStop = false) {
  for (int attempt = 0; attempt < MAX_WRITE_RETRIES; ++attempt) {
    Wire.beginTransmission(addr);
    for (size_t i = 0; i < len; ++i) Wire.write(buf[i]);
    uint8_t result = noStop ? Wire.endTransmission(false) : Wire.endTransmission();
    if (result == 0) return true;
    delay(10);
  }
  return false;
}

/* ---------------------------------------------------------------------------
   Read bytes from sensor with timeout
--------------------------------------------------------------------------- */
size_t requestRead(uint8_t addr, size_t expected, uint8_t* dest, unsigned long timeout_ms) {
  size_t count = 0;
  Wire.requestFrom((int)addr, (int)expected);
  unsigned long start = millis();
  while (count < expected && (millis() - start) < timeout_ms) {
    if (Wire.available()) dest[count++] = Wire.read();
  }
  return count;
}

/* ---------------------------------------------------------------------------
   Configure MLX90393 registers
--------------------------------------------------------------------------- */
bool configureSensor() {
  uint8_t cfg1[] = { CMD_WRITE_REG, 0x00, 0x5C, 0x00 }; // Gain, resolution, filter
  if (!safeWrite(SENSOR_ADDR, cfg1, sizeof(cfg1))) return false;

  uint8_t status;
  if (requestRead(SENSOR_ADDR, 1, &status, I2C_READ_TIMEOUT_MS) != 1) return false;

  uint8_t cfg2[] = { CMD_WRITE_REG, 0x02, 0xB4, 0x08 }; // Temp compensation, trigger
  if (!safeWrite(SENSOR_ADDR, cfg2, sizeof(cfg2))) return false;

  if (requestRead(SENSOR_ADDR, 1, &status, I2C_READ_TIMEOUT_MS) != 1) return false;

  return true;
}

/* ---------------------------------------------------------------------------
   Read raw X, Y, Z magnetic values + status
   X<->Y swap in software, to match the calibration rig:
     - X stage motion affects Raw_By
     - Y stage motion affects Raw_Bx
   Z stays unchanged (already correct).
--------------------------------------------------------------------------- */
bool readXYZ(int16_t &Bx, int16_t &By, int16_t &Bz, uint8_t &status) {
  uint8_t startCmd = CMD_MEASURE_ZYX;
  if (!safeWrite(SENSOR_ADDR, &startCmd, 1)) return false;

  delay(MEASUREMENT_DELAY_MS);

  uint8_t readCmd = CMD_READ_MEAS;
  if (!safeWrite(SENSOR_ADDR, &readCmd, 1, true)) return false;

  uint8_t buf[7] = {0}; // Status + 3x 16-bit axis
  if (requestRead(SENSOR_ADDR, 7, buf, I2C_READ_TIMEOUT_MS) != 7) return false;

  status = buf[0];

  // Axes as returned by the sensor
  int16_t sx = (int16_t)((buf[1] << 8) | buf[2]);
  int16_t sy = (int16_t)((buf[3] << 8) | buf[4]);
  int16_t sz = (int16_t)((buf[5] << 8) | buf[6]);

  // Mapping fix:
  // Keep Z as-is, swap X/Y to avoid confusion downstream.
  Bx = -sy;   // swapped
  By = -sx;   // swapped
  Bz =  sz;   // unchanged

  return true;
}

/* ---------------------------------------------------------------------------
   Setup
--------------------------------------------------------------------------- */
void setup() {
  Wire.begin();
  Wire.setClock(100000);  // Standard 100kHz

  Serial.begin(115200);
  delay(200);

  bool ok = configureSensor();
  if (!ok) {
    Serial.println("CONFIG ERROR");
  } else {
    // Boot marker for Python-side synchronization
    Serial.println("BOOT:READY");
  }

  // Print CSV header for Python parsing
  Serial.println("Raw_Bx,Raw_By,Raw_Bz,Status");
}

/* ---------------------------------------------------------------------------
   Acquisition loop
--------------------------------------------------------------------------- */
void acquisitionLoop() {
  static unsigned long lastTime = 0;
  unsigned long now = millis();

  if (now - lastTime >= MEASUREMENT_DELAY_MS) {
    lastTime = now;

    int16_t x, y, z;
    uint8_t st = 0;
    if (readXYZ(x, y, z, st)) {
      Raw_Bx = x;
      Raw_By = y;
      Raw_Bz = z;
      LastStatus = st;
    }
  }
}

/* ---------------------------------------------------------------------------
   Main loop
--------------------------------------------------------------------------- */
void loop() {
  acquisitionLoop();

  // Print latest mapped raw values + status in CSV format
  Serial.print(Raw_Bx); Serial.print(',');
  Serial.print(Raw_By); Serial.print(',');
  Serial.print(Raw_Bz); Serial.print(',');
  Serial.println(LastStatus);

  delay(MEASUREMENT_DELAY_MS);
}


/*
========================================================================
DATA FORMAT GUARANTEE (DO NOT CHANGE WITHOUT PYTHON UPDATE)

- Bx, By, Bz values are SIGNED (two’s complement)
- Effective numeric range (int16_t):
      min = -32768
      max = +32767

- Values are zero-centred around 0
- Negative values indicate opposite field direction
- Positive values indicate field direction

IMPORTANT:
- Python side (TeensySerialv4 + K64F) assumes SIGNED values.
- Treating these values as UNSIGNED will cause wrap-around
  (e.g. -1000 -> 64536) and break:
    * normalisation
    * plotting
    * calibration

Output format over Serial:
  Bx,By,Bz,Status
Example:
  -314,-1778,7061,2

Boot marker:
  BOOT:READY
========================================================================
*/
