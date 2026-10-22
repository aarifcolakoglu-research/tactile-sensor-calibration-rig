# Firmware

Teensy sketches used with the calibration rig. Build with the Arduino IDE and
Teensyduino (USB type **Serial**); no extra libraries are needed beyond `Wire`.

| Sketch | Purpose |
|---|---|
| `single_sensor_calibration/` | Firmware used for every sensor calibration: one MLX90393 at address `0x0C` on `Wire` (SDA 18, SCL 19) |
| `tools/i2c_scanner/` | Lists every device that answers on `Wire` |
| `tools/mlx90393_address_check/` | Checks which of the MLX90393 addresses `0x0C`–`0x0F` acknowledge |

## Serial format

115200 baud:

```
BOOT:READY
Raw_Bx,Raw_By,Raw_Bz,Status
-314,-1778,7061,2
```

`lib/TeensySerialv4.py` waits for `BOOT:READY` (or the header line) and then reads
3 or 4 comma-separated values per line. Values are signed 16-bit integers.
X and Y are swapped and inverted in the firmware so that the rig's X stage moves `Raw_Bx`
and its Y stage moves `Raw_By`; the calibration uses `Raw_Bz`.

The sensor registers (`0x00 = 0x005C`, `0x02 = 0x02B4`) are the same as in the helmet
12-sensor firmware
([sensor-tracking-studio](https://github.com/aarifcolakoglu-research/sensor-tracking-studio)),
so calibration and measurement share one raw scale.
