#include <Wire.h>

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) {}

  Serial.println("MLX90393 address ACK test...");
  Wire.begin();   // Teensy 4.1: SDA=18, SCL=19
}

void loop() {
  for (uint8_t addr = 0x0C; addr <= 0x0F; addr++) {
    Wire.beginTransmission(addr);
    uint8_t err = Wire.endTransmission();

    Serial.print("Addr 0x");
    if (addr < 16) Serial.print("0");
    Serial.print(addr, HEX);
    Serial.print(" -> err = ");
    Serial.println(err);
  }

  Serial.println("----");
  delay(1000);
}
