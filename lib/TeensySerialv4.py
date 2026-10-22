# lib/TeensySerialv4.py

import sys
import serial
import time
import threading
from queue import Queue, Empty
from typing import Optional, List, Tuple, Dict, Any

TeensyPacket = List[float]  # [Bx, By, Bz] or [Bx, By, Bz, Status]


class TeensySerial:
    """
    Continuous Teensy serial stream reader.

    Supports:
      - 3 columns:  Bx, By, Bz
      - 4 columns:  Bx, By, Bz, Status

    Startup behaviour (v4):
      - Reset Teensy on each connect() (optional)
      - Optional reopen after reset (optional)
      - Wait for a boot sync signal BEFORE flushing buffers:
          * BOOT:READY marker, OR
          * CSV header, OR
          * first valid numeric data line
      - Flush any remaining startup noise
      - Start reader thread
      - Brief stabilization wait
      - Verify stream before acquisition starts
      - Print clear terminal logs
    """

    def __init__(
        self,
        port: str = "COM5",
        baudrate: int = 115200,
        timeout: float = 0.1,
        *,
        # Reset & sync
        reset_on_connect: bool = True,
        reset_method: str = "dtr_rts",          # "dtr_rts" or "dtr_only"
        reset_pulse_s: float = 0.25,
        post_reset_wait_s: float = 1.5,
        reopen_after_reset: bool = True,

        # Boot marker (firmware prints this once on boot)
        boot_marker: str = "BOOT:READY",
        boot_wait_s: float = 8.0,

        # Stabilization & verification
        stabilize_s: float = 2.0,
        verify_window_s: float = 2.0,
        verify_min_packets: int = 5,
        expected_range: Tuple[float, float] = (-32768.0, 32767.0),  # int16 range

        # Logging
        verbose: bool = True,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

        self.reset_on_connect = reset_on_connect
        self.reset_method = reset_method
        self.reset_pulse_s = reset_pulse_s
        self.post_reset_wait_s = post_reset_wait_s
        self.reopen_after_reset = reopen_after_reset

        self.boot_marker = boot_marker
        self.boot_wait_s = boot_wait_s

        self.stabilize_s = stabilize_s
        self.verify_window_s = verify_window_s
        self.verify_min_packets = verify_min_packets
        self.expected_range = expected_range

        self.verbose = verbose

        self.ser: Optional[serial.Serial] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.data_queue: "Queue[TeensyPacket]" = Queue()

    # -----------------------------
    # Public API
    # -----------------------------
    def connect(self) -> None:
        """
        Connect and prepare a clean run:
          1) Open serial
          2) Reset Teensy (optional) and reopen (optional)
          3) Wait for boot sync BEFORE flushing buffers
          4) Flush remaining startup noise
          5) Start read thread
          6) Stabilize briefly
          7) Verify stream and print first samples
        """
        self._stop_event.clear()

        # If reconnecting, clean old thread/port
        if self._thread and self._thread.is_alive():
            self.disconnect()

        try:
            self._log(f"[INFO] Opening serial: {self.port} @ {self.baudrate}")
            self._open_serial()

            if self.reset_on_connect:
                self._log("[INFO] Resetting Teensy (reset_on_connect=True)...")
                self._reset_teensy()

                if self.reopen_after_reset:
                    self._log("[INFO] Reopening serial after reset for stability...")
                    self._safe_reopen()

            # IMPORTANT: wait for sync BEFORE flushing input buffer
            if self.boot_marker:
                self._log(f"[INFO] Waiting for boot sync: '{self.boot_marker}' (timeout={self.boot_wait_s}s)")
                self._wait_for_marker(self.boot_marker, self.boot_wait_s)

            # Now it is safe to flush any remaining junk/header bytes
            self._flush_startup()

            # Start reader thread
            self._thread = threading.Thread(target=self._read_loop, daemon=True)
            self._thread.start()

            # Stabilization wait
            if self.stabilize_s > 0:
                self._log(f"[INFO] Stabilization: waiting {self.stabilize_s:.1f}s...")
                time.sleep(self.stabilize_s)
                self._log("[INFO] Stabilization complete.")

            # Verify stream and print first values
            ok, report = self._verify_stream(
                window_s=self.verify_window_s,
                min_packets=self.verify_min_packets,
                expected_range=self.expected_range,
            )
            self._print_verify_report(ok, report)

            if not ok:
                raise RuntimeError("Teensy stream verification failed; acquisition not started.")

            self._log(f"[INFO] Connected to Teensy on {self.port}")

        except serial.SerialException as e:
            self._log(f"[ERROR] Cannot connect to {self.port}: {e}")
            self.ser = None
        except Exception as e:
            self._log(f"[ERROR] Connect aborted: {e}")
            self.ser = None
            try:
                self.disconnect()
            except Exception:
                pass

    def get_latest(self, timeout: float = 0.1) -> Optional[TeensyPacket]:
        try:
            item = self.data_queue.get(timeout=timeout)
        except Empty:
            return None

        # Drain queue -> keep only the most recent
        while True:
            try:
                item = self.data_queue.get_nowait()
            except Empty:
                break

        return item

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self._log("[INFO] Serial disconnected.")

    # -----------------------------
    # Internal: serial ops
    # -----------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def _open_serial(self) -> None:
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        try:
            self.ser.setDTR(True)
        except Exception:
            pass

    def _safe_reopen(self) -> None:
        if not self.ser:
            return
        try:
            self.ser.close()
        except Exception:
            pass
        time.sleep(0.25)
        self._open_serial()

    def _reset_teensy(self) -> None:
        """Reset Teensy using DTR/RTS toggling (best-effort)."""
        if not self.ser:
            return

        try:
            if self.reset_method == "dtr_only":
                try:
                    self.ser.setDTR(False)
                except Exception:
                    pass
                time.sleep(self.reset_pulse_s)
                try:
                    self.ser.setDTR(True)
                except Exception:
                    pass
            else:
                # dtr_rts (default)
                try:
                    self.ser.setRTS(False)
                except Exception:
                    pass
                try:
                    self.ser.setDTR(False)
                except Exception:
                    pass

                time.sleep(self.reset_pulse_s)

                try:
                    self.ser.setDTR(True)
                except Exception:
                    pass
                try:
                    self.ser.setRTS(True)
                except Exception:
                    pass
        except Exception:
            pass

        time.sleep(self.post_reset_wait_s)

    def _flush_startup(self) -> None:
        """Flush input/output buffers and drain immediate bytes briefly."""
        if not self.ser:
            return

        time.sleep(0.2)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        try:
            self.ser.reset_output_buffer()
        except Exception:
            pass

        t0 = time.time()
        while time.time() - t0 < 0.2:
            try:
                n = getattr(self.ser, "in_waiting", 0)
                if n and n > 0:
                    _ = self.ser.read(n)
                else:
                    break
            except Exception:
                break

    def _wait_for_marker(self, marker: str, timeout_s: float) -> None:
        """
        Wait for a boot synchronization signal.

        Accepts any of the following as "sync acquired":
          1) explicit boot marker (e.g. "BOOT:READY")
          2) CSV header line (e.g. "Raw_Bx,Raw_By,Raw_Bz,Status")
          3) first valid numeric data line (3 or 4 comma-separated numbers)

        This makes startup robust even if the COM port is reopened and the
        marker/header is missed (firmware prints them only once on boot).
        """
        if not self.ser:
            return

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                raw = self.ser.readline()
            except Exception:
                raw = b""

            if not raw:
                time.sleep(0.02)
                continue

            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            # 1) explicit marker
            if marker in line:
                self._log(f"[INFO] Boot sync received (marker): '{line}'")
                return

            # 2) header fallback
            if "Raw_Bx,Raw_By,Raw_Bz" in line:
                self._log(f"[INFO] Boot sync received (header): '{line}'")
                return

            # 3) first valid numeric line fallback
            if self._is_valid_data_line(line):
                self._log(f"[INFO] Boot sync received (first data): '{line}'")
                return

        raise RuntimeError(f"Boot marker not received: '{marker}'")

    def _is_valid_data_line(self, line: str) -> bool:
        """Return True if line looks like 3 or 4 numeric CSV values."""
        parts = [p.strip() for p in line.split(",") if p.strip() != ""]
        if len(parts) not in (3, 4):
            return False
        try:
            _ = [float(p) for p in parts]
            return True
        except ValueError:
            return False

    # -----------------------------
    # Internal: parsing & reading
    # -----------------------------
    def _parse_line(self, line: str) -> Optional[TeensyPacket]:
        if not line or line.startswith("#"):
            return None

        # Ignore CSV header
        if "Raw_Bx" in line or "Raw_By" in line or "Raw_Bz" in line:
            return None

        # Ignore boot/info lines if they slip through
        if "BOOT:READY" in line or "CONFIG" in line:
            return None

        parts = [p.strip() for p in line.split(",") if p.strip() != ""]
        if len(parts) not in (3, 4):
            return None

        try:
            vals = [float(p) for p in parts]
        except ValueError:
            return None

        return vals

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.ser is None:
                    time.sleep(0.05)
                    continue

                raw = self.ser.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="ignore").strip()
                vals = self._parse_line(line)
                if vals is None:
                    continue

                self.data_queue.put(vals)

            except Exception as e:
                self._log(f"[ERROR] Unexpected error in read loop: {e}")
                time.sleep(0.05)
                continue

    # -----------------------------
    # Internal: verification
    # -----------------------------
    def _verify_stream(
        self,
        *,
        window_s: float,
        min_packets: int,
        expected_range: Tuple[float, float],
    ) -> Tuple[bool, Dict[str, Any]]:
        t0 = time.time()
        packets: List[TeensyPacket] = []

        while time.time() - t0 < window_s:
            p = self.get_latest(timeout=0.05)
            if p is not None:
                packets.append(p)

        report: Dict[str, Any] = {
            "window_s": window_s,
            "packets_collected": len(packets),
            "min_packets_required": min_packets,
            "expected_range": expected_range,
            "first_packets": packets[:5],
            "range_violations": 0,
            "notes": [],
        }

        if len(packets) < min_packets:
            report["notes"].append(
                "Not enough packets collected (wrong COM port, stalled stream, or firmware not streaming)."
            )
            return False, report

        lo, hi = expected_range
        violations = 0
        for p in packets:
            xyz = p[:3]
            for v in xyz:
                if v < lo or v > hi:
                    violations += 1

        report["range_violations"] = violations
        if violations > 0:
            report["notes"].append(
                "Some samples exceeded expected numeric range (parse noise/format mismatch/overflow)."
            )
            return False, report

        report["notes"].append("Stream verification passed.")
        return True, report

    def _print_verify_report(self, ok: bool, report: Dict[str, Any]) -> None:
        status = "OK" if ok else "FAIL"
        self._log(f"[INFO] Teensy stream verification: {status}")
        self._log(
            f"[INFO] Verify window: {report['window_s']}s | packets={report['packets_collected']} "
            f"(min={report['min_packets_required']})"
        )
        self._log(
            f"[INFO] Expected range (Bx,By,Bz): {report['expected_range']} | violations={report['range_violations']}"
        )

        first = report.get("first_packets", [])
        if first:
            self._log("[INFO] First received samples:")
            for i, p in enumerate(first, start=1):
                self._log(f"       {i:02d}: {p}")

        for n in report.get("notes", []):
            self._log(f"[INFO] Note: {n}")


# ------------------------------------------------------
# Manual test (runs only when this file is executed directly)
# ------------------------------------------------------
if __name__ == "__main__":
    t = TeensySerial(
        port="COM5",
        baudrate=115200,
        reset_on_connect=True,
        boot_marker="BOOT:READY",
        stabilize_s=2.0,
        verify_window_s=2.0,
        verify_min_packets=5,
        verbose=True,
    )
    t.connect()
    if t.ser is None:
        raise SystemExit(1)

    print("[INFO] Reading... Ctrl+C to stop")

    # In-place RX display settings
    refresh_hz = 10.0  # update the single RX line at ~10 Hz
    refresh_dt = 1.0 / refresh_hz
    last_print = 0.0
    last_vals: Optional[TeensyPacket] = None

    try:
        while True:
            vals = t.get_latest(timeout=0.2)
            if vals is not None:
                last_vals = vals

            now = time.time()
            if now - last_print >= refresh_dt:
                last_print = now

                if last_vals is None:
                    line = "RX: (no data yet)"
                else:
                    line = f"RX: {last_vals}"

                # Print on one line and overwrite previous content
                sys.stdout.write("\r" + line + " " * 20)
                sys.stdout.flush()

    except KeyboardInterrupt:
        # Ensure the cursor moves to a new line before printing final logs
        sys.stdout.write("\n")
        sys.stdout.flush()

    finally:
        t.disconnect()
