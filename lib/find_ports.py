import serial.tools.list_ports

TEENSY_VID = 0x16C0  # Teensy vendor ID


def list_serial_devices(verbose: bool = True):
    ports = serial.tools.list_ports.comports()
    devices = []

    if verbose:
        print("\n--- Connected Serial Devices ---\n")

    for p in ports:
        device_info = {
            "port": p.device,
            "desc": p.description,
            "manufacturer": p.manufacturer,
            "vid": p.vid,
            "pid": p.pid,
            "serial": p.serial_number
        }

        devices.append(device_info)

        if verbose:
            print(f"Port        : {p.device}")
            print(f"Description : {p.description}")
            print(f"Manufacturer: {p.manufacturer}")
            print(f"VID:PID     : {p.vid}:{p.pid}")
            print(f"Serial No   : {p.serial_number}")
            print("-" * 50)

    return devices


def find_teensy(devices):
    teensy_ports = []

    for d in devices:
        if d["vid"] == TEENSY_VID:
            teensy_ports.append(d)

    return teensy_ports


def find_teensy_port(verbose: bool = False):
    """
    Quiet helper for the app: return the COM port of the first Teensy found
    (VID 0x16C0), or None if no Teensy is connected.

    Never raises - a failure to enumerate ports is treated as "not found".
    """
    try:
        devices = list_serial_devices(verbose=verbose)
        teensy_devices = find_teensy(devices)
    except Exception as e:
        if verbose:
            print(f"[WARN] Serial port scan failed: {e}")
        return None

    if not teensy_devices:
        return None

    if len(teensy_devices) > 1 and verbose:
        ports = ", ".join(t["port"] for t in teensy_devices)
        print(f"[WARN] Multiple Teensy devices found ({ports}); using {teensy_devices[0]['port']}")

    return teensy_devices[0]["port"]


if __name__ == "__main__":
    devices = list_serial_devices()
    teensy_devices = find_teensy(devices)

    print("\n=== Teensy Scan Result ===\n")

    if teensy_devices:
        for t in teensy_devices:
            print(f"✔ Teensy FOUND on {t['port']}")
            print(f"  Description : {t['desc']}")
            print(f"  Serial No   : {t['serial']}")
            print("-" * 40)
    else:
        print("✖ No Teensy device found (VID 0x16C0 not detected)")
