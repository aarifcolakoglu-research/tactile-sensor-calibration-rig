#include <Wire.h>

void setup() {
  Serial.begin(115200);
  // Short wait for Teensy's USB serial interface to initialize
  while (!Serial && millis() < 2000) {}

  Serial.println("I2C Scanner started...");
  Wire.begin();   // SDA = 18, SCL = 19
}

void loop() {
  byte error, address;
  int nDevices = 0;

  Serial.println("Scanning for I2C devices...");

  for (address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    error = Wire.endTransmission();

    if (error == 0) {
      Serial.print("I2C device found at address: 0x");
      if (address < 16) Serial.print("0");
      Serial.println(address, HEX);
      nDevices++;
    } 
    else if (error == 4) {
      Serial.print("Unknown error at address: 0x");
      if (address < 16) Serial.print("0");
      Serial.println(address, HEX);
    }
  }

  if (nDevices == 0) {
    Serial.println("No I2C devices found.");
  }

  Serial.println("Scan completed.\n");
  delay(2000);  // Repeat scan every 2 seconds
}
