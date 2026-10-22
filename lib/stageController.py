from __future__ import annotations

from pylablib.devices import Thorlabs


# Conversion factor from mm to device units (validated on your rig)
SCALE_FACTOR = 2.9e-5


class StageController:
    """
    Thin wrapper around Thorlabs KinesisMotor with explicit mm->device conversion.

    Notes:
    - This class intentionally keeps the existing behaviour you validated:
      distance_scaled = distance_mm / SCALE_FACTOR and scale=True in pylablib calls.
    - Safety improvements: input validation, clearer errors, and optional close().
    """

    def __init__(self, serial: str):
        self.serial = str(serial)
        self.stage = None

        print(f"[INFO] Initialising stage: {self.serial}")
        try:
            self.stage = Thorlabs.KinesisMotor(self.serial)
        except Exception as e:
            raise RuntimeError(f"Failed to initialise Thorlabs stage ({self.serial}): {e}") from e

    def setup_velocity(self, min_velocity: float, acceleration: float, max_velocity: float) -> None:
        if self.stage is None:
            raise RuntimeError("Stage is not initialised.")

        try:
            min_velocity = float(min_velocity)
            acceleration = float(acceleration)
            max_velocity = float(max_velocity)
        except Exception as e:
            raise ValueError(f"Velocity parameters must be numeric: {e}") from e

        if min_velocity < 0 or acceleration < 0 or max_velocity < 0:
            raise ValueError("Velocity/acceleration cannot be negative.")
        if max_velocity < min_velocity:
            raise ValueError("max_velocity must be >= min_velocity.")

        try:
            min_velocity_scaled = min_velocity / SCALE_FACTOR
            acceleration_scaled = acceleration / SCALE_FACTOR
            max_velocity_scaled = max_velocity / SCALE_FACTOR

            self.stage.setup_velocity(
                min_velocity=min_velocity_scaled,
                acceleration=acceleration_scaled,
                max_velocity=max_velocity_scaled,
                scale=True,  # keep validated behaviour
            )
        except Exception as e:
            raise RuntimeError(f"Failed to set velocity on stage {self.serial}: {e}") from e

    def move_by(self, distance_mm: float) -> None:
        if self.stage is None:
            raise RuntimeError("Stage is not initialised.")

        try:
            distance_mm = float(distance_mm)
        except Exception as e:
            raise ValueError(f"Distance must be numeric: {e}") from e

        # distance can be negative (direction), so do not block that
        try:
            distance_scaled = distance_mm / SCALE_FACTOR
            print(f"[INFO] Stage {self.serial} moving by {distance_mm:.4f} mm")
            self.stage.move_by(distance_scaled, scale=True)  # keep validated behaviour
        except Exception as e:
            raise RuntimeError(f"Failed to move stage {self.serial} by {distance_mm} mm: {e}") from e

    def stop(self) -> None:
        if self.stage is None:
            return
        try:
            self.stage.stop()
        except Exception as e:
            print(f"[WARN] Stage {self.serial} stop failed: {e}")

    def home(self) -> None:
        if self.stage is None:
            raise RuntimeError("Stage is not initialised.")
        try:
            print(f"[INFO] Stage {self.serial} moving to home")
            self.stage.home()
        except Exception as e:
            raise RuntimeError(f"Failed to home stage {self.serial}: {e}") from e

    def is_moving(self) -> bool:
        if self.stage is None:
            return False
        try:
            return bool(self.stage.is_moving())
        except Exception:
            # If driver call fails, treat as not moving to avoid hard-crash in GUI polling loops
            return False

    def close(self) -> None:
        """
        Optional cleanup. Safe to call multiple times.
        """
        if self.stage is None:
            return
        try:
            # pylablib devices typically expose close()
            self.stage.close()
        except Exception:
            pass
        finally:
            self.stage = None
