import socket
import struct
import math
import time
from datetime import datetime
import os
import csv

from PyQt6.QtCore import QThread, pyqtSignal
from lib.TeensySerialv4 import TeensySerial

try:
    import colorama
    colorama.init()
except ImportError:
    pass

IP_ADDRESS = "192.168.5.101"
TCP_PORT = 7851

FID_GET_STATUS = 0
FID_GET_SYSTEM_INFO = 1
FID_GET_FT_SENSOR_DATA = 2
FID_GET_ENCODER_SENSOR_DATA = 3
FID_GET_ALL_SENSOR_DATA = 4
FID_GET_ALL_SENSOR_DATA_CONTINUOUS = 5
FID_START_ACQUISITION_STREAM = 6
FID_STOP_ACQUISITION_STREAM = 7
FID_RESET_ADC = 8
FID_CHECK_ADC = 9
FID_SET_ADC_CONVERSION_MODE = 10
FID_SET_ADC_DATA_RATE = 11

CALIBRATION_MATRIX = [
    [0.00135, -0.04604, 0.02344, -3.22662, -0.00322, 3.17008],
    [-0.00694, 4.16569, 0.02971, -1.87643, 0.02738, -1.79608],
    [3.69182, 0.02713, 3.83425, -0.03329, 3.65801, -0.067],
    [0.15408, 25.37956, 21.34115, -11.71178, -20.50426, -10.51265],
    [-23.9405, 0.07684, 12.2178, 19.58172, 12.22442, -19.60731],
    [0.09832, 17.49271, -0.32435, 15.76607, -0.09154, 15.24495],
]


def print_combined_table(values):
    """
    Optional terminal helper: prints a one-line combined table with ANSI colors.
    values: (fx, fy, fz, tx, ty, tz, px, py, pz, T_x, T_y, T_z)
    """
    RED = "\033[31m"
    GREEN = "\033[32m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    if not hasattr(print_combined_table, "printed_header"):
        print("\n" + BOLD + "==================== COMBINED SENSOR DATA ====================" + RESET)
        print(BOLD + "   fx      fy      fz      tx      ty      tz    px    py    pz   T_x   T_y   T_z" + RESET)
        print("----------------------------------------------------------------")
        print_combined_table.printed_header = True

    line = (
        "\r"
        f"{RED}{values[0]:7.3f} {values[1]:7.3f} {values[2]:7.3f}{RESET} "
        f"{CYAN}{values[3]:7.3f} {values[4]:7.3f} {values[5]:7.3f}{RESET} "
        f"{GREEN}{values[6]:6.2f} {values[7]:6.2f} {values[8]:6.2f}{RESET} "
        f"{BLUE}{values[9]:6.2f} {values[10]:6.2f} {values[11]:6.2f}{RESET}"
        "\033[K"
    )
    print(line, end="", flush=True)


class DataStream(QThread):
    data_received = pyqtSignal(tuple)

    # Latest Teensy snapshot (shared)
    latest_teensy_data = (0.0, 0.0, 0.0)
    latest_teensy_status = 0.0

    def __init__(self, socket_obj, teensy_thread=None):
        super().__init__()
        self.socket = socket_obj
        self.teensy_thread = teensy_thread

        self.stopped = False
        self.logging_flag = False
        self.csv_file = None
        self.csv_writer = None
        self.sequence_name = None

        self.time = 0
        self.start_time = 0
        self.prev_device_time_us = None

        # ATI + encoder storage (6 + 3)
        self.all_data = tuple([0] * 9)
        self.data_offset = tuple([0] * 9)

        # Teensy normalisation parameters
        self.teensy_offset = (0.0, 0.0, 0.0)

    def update_teensy_data(self, data):
        """Accepts list/tuple [Bx,By,Bz] or [Bx,By,Bz,Status], or a CSV-like string for backward compatibility."""
        try:
            if isinstance(data, (list, tuple)):
                if len(data) == 3:
                    bx, by, bz = data
                    status = DataStream.latest_teensy_status
                elif len(data) == 4:
                    bx, by, bz, status = data
                else:
                    return

                DataStream.latest_teensy_data = (float(bx), float(by), float(bz))
                DataStream.latest_teensy_status = float(status)
                return

            parts = [p.strip() for p in str(data).split(",") if p.strip() != ""]
            if len(parts) == 3:
                bx, by, bz = (float(parts[0]), float(parts[1]), float(parts[2]))
                status = DataStream.latest_teensy_status
            elif len(parts) == 4:
                bx, by, bz, status = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            else:
                return

            DataStream.latest_teensy_data = (bx, by, bz)
            DataStream.latest_teensy_status = float(status)

        except Exception as e:
            print(f"[ERROR] Converting Teensy data failed: {e}")
            DataStream.latest_teensy_data = (0.0, 0.0, 0.0)
            DataStream.latest_teensy_status = 0.0

    def set_logging_flag(self, flag: bool) -> None:
        self.logging_flag = flag

    def set_sequence_name(self, sequence_name: str) -> None:
        self.sequence_name = sequence_name

    def set_data_offset(self) -> None:
        # Store offsets for ATI + encoder channels
        self.data_offset = self.all_data

        # Store Teensy offset only if valid data is available
        if DataStream.latest_teensy_data != (0.0, 0.0, 0.0):
            self.teensy_offset = DataStream.latest_teensy_data

    def calibration_transform(self, tuple_values):
        result = [0.0] * 6
        for i in range(6):
            for j in range(6):
                result[i] += CALIBRATION_MATRIX[i][j] * tuple_values[j]
        return tuple(result)

    def normalise_teensy(self, raw_values):
        """
        Linear normalisation from int16 raw domain to [-20, +20].

        Raw is assumed int16: [-32768, +32767]
        - Offset subtraction is done in RAW domain.
        - Then linearly scaled to [-20, +20].
        - Hard-clipped to [-20, +20].
        """
        bx, by, bz = raw_values
        ox, oy, oz = self.teensy_offset

        bx0 = bx - ox
        by0 = by - oy
        bz0 = bz - oz

        FS = 32767.0
        gain = 20.0 / FS

        n_bx = bx0 * gain
        n_by = by0 * gain
        n_bz = bz0 * gain

        n_bx = max(-20.0, min(20.0, n_bx))
        n_by = max(-20.0, min(20.0, n_by))
        n_bz = max(-20.0, min(20.0, n_bz))

        return (n_bx, n_by, n_bz)

    def initialise_csv(self):
        os.makedirs("output", exist_ok=True)
        formatted_time = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())
        self.csv_file = open(f"output/{self.sequence_name}_{formatted_time}.csv", "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)

        self.csv_writer.writerow([
            "real_time", "device_time_us", "t_rel_us", "dt_us", "fs_hz",
            "px", "py", "pz",
            "fx", "fy", "fz",
            "tx", "ty", "tz",
            "raw_bx", "raw_by", "raw_bz", "status_byte",
            "n_bx", "n_by", "n_bz"
        ])

        self.start_time = self.time
        self.prev_device_time_us = self.time

    def close_csv(self):
        try:
            if self.csv_file:
                self.csv_file.close()
        finally:
            self.csv_file = None
            self.csv_writer = None

    def stop(self):
        self.stopped = True

    def run(self):
        all_data_size = struct.calcsize("<ffffffffff")
        csv_initialised = False

        while not self.stopped:
            try:
                data = self.socket.recv(all_data_size)
                if not data:
                    # Peer closed / half-open: exit
                    break

                # Device timestamp (microseconds)
                self.time = struct.unpack("<I", data[:4])[0]

                # Raw sensor payload
                ft_data = struct.unpack("<ffffff", data[4:28])

                # Encoder payload (3 floats)
                enc0, enc1, enc2 = struct.unpack("<fff", data[28:])

                # Encoder order fix (X/Y swap) + sign convention adjustment
                px, py, pz = enc1, enc0, enc2
                py = -py
                encoder_data = (px, py, pz)

                # Apply ATI calibration matrix
                calibrated_ft_data = self.calibration_transform(ft_data)

                # ATI+encoder and apply zero offsets
                self.all_data = calibrated_ft_data + encoder_data
                adjusted_data = tuple(x - offset for x, offset in zip(self.all_data, self.data_offset))

                # Teensy snapshot
                raw_teensy = DataStream.latest_teensy_data
                status = DataStream.latest_teensy_status

                # Normalise Teensy channels to [-20, +20]
                n_bx, n_by, n_bz = self.normalise_teensy(raw_teensy)

                # Emit tuple for GUI:
                # (time_us, fx, fy, fz, tx, ty, tz, px, py, pz, raw_bx, raw_by, raw_bz, n_bx, n_by, n_bz)
                updatVals = (int(self.time),) + adjusted_data + raw_teensy + (n_bx, n_by, n_bz)
                self.data_received.emit(updatVals)

                # CSV logging
                if self.logging_flag:
                    if not csv_initialised:
                        self.initialise_csv()
                        csv_initialised = True

                    time_us, fx, fy, fz, tx, ty, tz, px, py, pz, raw_bx, raw_by, raw_bz, n_bx, n_by, n_bz = updatVals

                    real_time = datetime.now().isoformat(timespec="microseconds")
                    t_rel_us = self.time - self.start_time

                    if self.prev_device_time_us is None:
                        dt_us = 0
                        fs_hz = 0.0
                    else:
                        dt_us = int(self.time - self.prev_device_time_us)
                        fs_hz = (1_000_000.0 / dt_us) if dt_us > 0 else 0.0

                    self.prev_device_time_us = self.time

                    if self.csv_writer is not None:
                        self.csv_writer.writerow([
                            real_time,
                            self.time,
                            t_rel_us,
                            dt_us,
                            fs_hz,
                            px, py, pz,
                            fx, fy, fz,
                            tx, ty, tz,
                            raw_bx, raw_by, raw_bz,
                            status,
                            n_bx, n_by, n_bz,
                        ])

                else:
                    if self.csv_file is not None:
                        self.close_csv()
                        csv_initialised = False

            except socket.timeout:
                # With socket timeout set, just continue; allows stop() to break quickly
                continue
            except Exception as e:
                print(f"[ERROR] Reading data from Ethernet device failed: {e}")
                break

        # Ensure CSV closed on exit
        if self.csv_file is not None:
            self.close_csv()


class TeensyThread(QThread):
    """
    Teensy reader thread.

    Uses TeensySerialv4.connect() as a gate (reset + boot sync + verify).
    If connect fails, the thread exits cleanly and does not update DataStream.
    """

    def __init__(self, data_stream, port="COM5", baudrate=115200, **teensy_kwargs):
        super().__init__()
        self.data_stream = data_stream
        self.running = True

        self.teensy = TeensySerial(
            port=port,
            baudrate=baudrate,
            **teensy_kwargs
        )

    def run(self):
        try:
            self.teensy.connect()

            if self.teensy.ser is None:
                print("[ERROR] Teensy serial is not open; TeensyThread exiting.")
                return

            while self.running:
                data = self.teensy.get_latest(timeout=0.2)
                if data:
                    self.data_stream.update_teensy_data(data)

        except Exception as e:
            print(f"[ERROR] TeensyThread failed: {e}")

        finally:
            try:
                self.teensy.disconnect()
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            self.teensy.disconnect()
        except Exception:
            pass


class K64F:
    """
    Ethernet client wrapper with robust reconnect.

    Key behaviour:
    - Each (re)connect creates a FRESH socket object
    - start_acquisition() attempts STOP->START handshake to clear stale streams
    - reconnect() can be called to recover from a stuck connection between runs
    """

    def __init__(self):
        self.socket = None
        self.connected = False
        self.data_stream = None
        self.teensy_thread = None

        # Connection parameters (tune if needed)
        self.connect_timeout_s = 2.0
        self.reconnect_retries = 6
        self.reconnect_backoff_s = 1.0

        self._make_socket()

    def _make_socket(self) -> None:
        # Always create a fresh socket object
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Timeout prevents connect/recv from blocking forever
        try:
            self.socket.settimeout(self.connect_timeout_s)
        except Exception:
            pass

        # Keepalive best-effort (OS dependent)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

    def _close_socket(self) -> None:
        """
        Robust close:
        - shutdown() best-effort
        - close()
        - ensure flags reset

        With SO_LINGER(1,0), close() will typically send RST and tear down immediately.
        """
        if self.socket is None:
            self.connected = False
            return

        try:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

            try:
                self.socket.close()
            except Exception:
                pass

        finally:
            self.socket = None
            self.connected = False

    def init_ethernet(self):
        # Backwards-compatible initial connect (single attempt)
        try:
            if self.socket is None:
                self._make_socket()
            self.socket.connect((IP_ADDRESS, TCP_PORT))
            self.connected = True
            print("[INFO] Connected to Ethernet device.")
        except Exception as e:
            print(f"[ERROR] Connecting to Ethernet device failed: {e}")
            self.connected = False

    def reconnect(self, retries: int | None = None, backoff_s: float | None = None) -> bool:
        """
        Hard reconnect:
        - close old socket
        - create a new socket
        - connect with retries
        - attempt STOP (idempotent) to clear stale device stream
        """
        retries = self.reconnect_retries if retries is None else int(retries)
        backoff_s = self.reconnect_backoff_s if backoff_s is None else float(backoff_s)

        self._close_socket()
        self._make_socket()

        for attempt in range(1, retries + 1):
            try:
                self.socket.connect((IP_ADDRESS, TCP_PORT))
                self.connected = True
                print(f"[INFO] Ethernet connected (attempt {attempt}/{retries}).")

                # Best-effort: clear stale stream state on device side
                try:
                    self.invoke_fid(FID_STOP_ACQUISITION_STREAM)
                    time.sleep(0.2)
                except Exception:
                    pass

                return True
            except Exception as e:
                self.connected = False
                print(f"[WARN] Ethernet connect failed (attempt {attempt}/{retries}): {e}")
                time.sleep(backoff_s)

        print("[ERROR] Ethernet reconnect failed after retries.")
        return False

    def start_data_stream(self):
        # Stop any existing stream thread first (defensive)
        if self.data_stream is not None:
            try:
                self.data_stream.stop()
            except Exception:
                pass
            self.data_stream = None

        self.data_stream = DataStream(self.socket)
        self.data_stream.start()

    def stop_data_stream(self):
        if self.data_stream:
            try:
                self.data_stream.stop()
            except Exception:
                pass
            self.data_stream = None

        if self.teensy_thread:
            try:
                self.teensy_thread.stop()
            except Exception:
                pass
            self.teensy_thread = None

    def invoke_fid(self, fid):
        if self.socket is None:
            raise RuntimeError("Ethernet socket is not initialised.")
        packet_length = 4
        error = 0
        message_header = struct.pack("<HBB", packet_length, fid, error)
        self.socket.sendall(message_header)

    def start_acquisition(self):
        """
        Robust start:
        - ensure connected (reconnect if needed)
        - STOP then START (idempotent) to clear stale state
        - start DataStream
        """
        if not self.connected:
            ok = self.reconnect()
            if not ok:
                print("[ERROR] Cannot start acquisition: Ethernet not connected.")
                return

        # Clean start handshake
        try:
            self.invoke_fid(FID_STOP_ACQUISITION_STREAM)
            time.sleep(0.2)
        except Exception:
            pass

        try:
            self.invoke_fid(FID_START_ACQUISITION_STREAM)
            print("[INFO] Start acquisition message sent.")
        except Exception as e:
            print(f"[ERROR] Failed to send START acquisition: {e}")
            # One quick recovery attempt
            if self.reconnect(retries=2, backoff_s=1.0):
                try:
                    self.invoke_fid(FID_START_ACQUISITION_STREAM)
                    print("[INFO] Start acquisition message sent (after reconnect).")
                except Exception as e2:
                    print(f"[ERROR] START acquisition failed after reconnect: {e2}")
                    return
            else:
                return

        self.start_data_stream()

    def stop_acquisition(self):
        if not self.connected:
            print("[ERROR] Not connected to the Ethernet device.")
            self.stop_data_stream()
            return

        try:
            self.invoke_fid(FID_STOP_ACQUISITION_STREAM)
            print("[INFO] Stop acquisition message sent.")
        except Exception as e:
            print(f"[WARN] Failed to send STOP acquisition: {e}")

        self.stop_data_stream()

    def enable_logging(self):
        if self.data_stream:
            self.data_stream.set_logging_flag(True)

    def disable_logging(self):
        if self.data_stream:
            self.data_stream.set_logging_flag(False)

    def set_sequence_name(self, sequence_name):
        if self.data_stream:
            self.data_stream.set_sequence_name(sequence_name)

    def set_data_offset(self):
        if self.data_stream:
            self.data_stream.set_data_offset()

    def close(self):
        # Stop threads first
        self.stop_data_stream()
        # Close socket
        self._close_socket()
        print("[INFO] Connection to Ethernet device closed.")
