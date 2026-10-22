from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from pylablib.devices import Thorlabs


# Conversion factor from mm to device units (validated on your rig)
# 1 / 34304 steps/mm  (MTS-Z8 family encoder resolution)
SCALE_FACTOR = 2.9e-5

# ---------------------------------------------------------------------------
# Stage safety envelope
# ---------------------------------------------------------------------------
# Rig uses Thorlabs MTS50/M-Z8 on X, Y and Z (see photos / labels on the rig).
# Travel is auto-detected from the KDC101 controller when it reports a known
# stage ID; otherwise DEFAULT_TRAVEL_MM is used.
DEFAULT_TRAVEL_MM = 50.0
STAGE_TRAVEL_MM = {
    "MTS25-Z8": 25.0,
    "MTS50-Z8": 50.0,
    "Z806": 6.0,
    "Z812": 12.0,
    "Z825": 25.0,
}

# Keep this far away from the hardware limit switch at the FAR end.
# The home end (0 mm) is the reference position itself, so only a tiny
# readback tolerance is applied there.
SAFETY_MARGIN_MM = 0.5
HOME_TOLERANCE_MM = 0.01

# MTS-Z8 datasheet max velocity is 2.4 mm/s. (The K64F encoder reader is
# also documented to overload above ~3 mm/s, so 2.4 is the binding limit.)
MAX_VELOCITY_MM_S = 2.4

# Homing: always towards the REVERSE limit switch = 0 mm end = the "-" end
# printed on the stage label. On this rig the sample sits on the Z+ side, so
# Z homing (Z-) lifts the probe away from the sample. X/Y also home to "-".
HOME_DIRECTION = "reverse"
# Gentler than the Kinesis default (~2.2 mm/s): less impact at the switch and
# less torque demanded from a stiff axis.
HOME_VELOCITY_MM_S = 0.5
HOME_TIMEOUT_S = 180.0
# After homing at the '+' end the axis sits a full travel away from where the
# operator expects it, so it is driven back to 0 mm. That is an ordinary move,
# not a switch approach, so it does not need the gentle homing velocity.
HOME_RETURN_VELOCITY_MM_S = 2.0
HOME_RETURN_TIMEOUT_S = 180.0
# When homing trips, back away from the end stop by this much so the axis is
# not left pressed against it after a trip.
HOME_RETREAT_MM = 2.0
# --- Stall / overload protection -------------------------------------------
# If the stage stops making progress, stop the motor almost immediately rather
# than let it grind against a mechanical end stop. At the homing velocity the
# stage covers ~3 mm in STALL_TIMEOUT, so this never false-triggers on a
# healthy axis, but a blocked axis is released within ~3 s instead of minutes.
STALL_TIMEOUT_S = 3.0
STALL_GRACE_S = 2.0          # ignore the first moments (motor spin-up)
STALL_TOLERANCE_MM = 0.005   # progress smaller than this counts as "not moving"

# Controller status bits that mean the motor is straining or faulted.
# Any of these stops motion within one poll (~0.2 s).
FAULT_BITS = ("current_limit", "overcurrent", "motion_error",
              "commutation_error", "encoder_fault")

# APT messages the controller can send unprompted. Registering them as
# "background" messages stops pylablib from raising
# "unexpected command received" when one arrives mid-query.
#   0x0080 MGMSG_HW_DISCONNECT
#   0x0081 MGMSG_HW_RESPONSE      - hardware error notification
#   0x0082 MGMSG_HW_RICHRESPONSE  - hardware error with description
HW_ERROR_MSG_IDS = (0x0080, 0x0081, 0x0082)

# Messages a KDC101 can send unprompted. pylablib already registers the motion
# ones (0x0464/0x0466/0x0444) but NOT 0x042A, the status update. If a
# controller has automatic status updates switched on it emits 0x042A
# continuously, every query then reads the wrong reply
# ("unexpected command received: expected 0x0415, got 0x042a") and every
# position/status readout becomes unreliable.
# Registering it makes pylablib skip stray copies while still accepting 0x042A
# as the answer to an actual status request.
ASYNC_MSG_IDS = HW_ERROR_MSG_IDS + (
    0x042A,   # MGMSG_MOT_GET_STATUSUPDATE (unprompted when auto-update is on)
    0x0491,   # status update (other controller families)
    0x0464,   # move completed
    0x0466,   # move stopped
    0x0444,   # homed
)
MSG_STOP_UPDATE_MSGS = 0x0012   # MGMSG_HW_STOP_UPDATEMSGS

# Values used in all existing calibration sequences.
RECOMMENDED_VELOCITY_MM_S = 0.5
RECOMMENDED_DISTANCE_MM = 0.1


class _ChannelDisabled(Exception):
    """The controller switched its output off mid-motion (hardware error)."""


class HomingAborted(RuntimeError):
    """Homing was interrupted by abort() - an operator's emergency stop,
    not a fault of the axis."""


@dataclass
class MoveCheck:
    """Result of a pre-move safety check."""
    ok: bool                    # True -> safe to send the move
    homed: bool                 # False -> position is unknown, cannot verify
    position_mm: Optional[float]
    target_mm: Optional[float]
    travel_mm: float
    reason: str = ""


class StageController:
    """
    Thin wrapper around Thorlabs KinesisMotor with explicit mm->device conversion.

    Notes:
    - This class intentionally keeps the existing behaviour you validated:
      distance_scaled = distance_mm / SCALE_FACTOR and scale=True in pylablib calls.
    - Safety: velocity capped at MAX_VELOCITY_MM_S, and check_move() lets the
      GUI/sequence runner verify a relative move stays inside the travel range
      (only possible once the stage has been homed, because the absolute
      position is unknown before that).
    """

    def __init__(self, serial: str, travel_mm: Optional[float] = None,
                 home_direction: str = HOME_DIRECTION):
        self.serial = str(serial)
        self.stage = None

        # The APT protocol is a strict request/reply stream on one serial port.
        # If two threads interleave their request/reply pairs, each reads the
        # other's answer and the link stays permanently offset by one message.
        # Every controller access must therefore be serialised.
        self._lock = threading.RLock()
        self.stage_name: Optional[str] = None
        self.travel_mm: float = DEFAULT_TRAVEL_MM
        # "reverse" homes to the '-' end (normal). Set "forward" to reference
        # against the '+' end instead - useful to test whether one of the two
        # limit switches is faulty.
        self.home_direction: str = home_direction
        # True once the operator has declared the current position with
        # set_reference_here() instead of homing.
        self._manual_reference = False
        # Which limit switch the current reference came from: "reverse" is the
        # normal one (the switch IS 0 mm). "forward" means the axis was homed
        # against the '+' switch as a fallback, and 0 mm was then derived from
        # the nominal travel - good enough to work with, but not a measured 0.
        self._reference_end = "reverse"

        # --- emergency stop -------------------------------------------------
        # Where this axis was when the operator's current COMMAND began (a
        # jog, a home, a sequence step, a touch pass) - not before every
        # internal move_by(): a touch probe's own abort-retract, say, must not
        # become the point an emergency stop "returns" to. See
        # arm_return_point() and return_to_point().
        self.return_point_mm: Optional[float] = None
        self.return_point_referenced = False
        self._return_hold = False          # a blocking command owns the axis
        self._return_until = 0.0           # a jog: expected to be moving until
        # Set by abort(); every blocking loop in this class checks it, so a
        # thread that was driving the axis stops commanding it instead of
        # carrying on (a homing stall check calling stop() mid-return, etc).
        self._abort = threading.Event()
        # Homing interrupted by abort(): whether the controller may have
        # re-zeroed its counter before it stopped (see _home_once).
        self._home_interrupted = False
        self._home_frame_changed = False

        print(f"[INFO] Initialising stage: {self.serial}")
        try:
            self.stage = Thorlabs.KinesisMotor(self.serial)
        except Exception as e:
            raise RuntimeError(f"Failed to initialise Thorlabs stage ({self.serial}): {e}") from e

        # Unprompted messages must not break normal queries.
        for msg_id in ASYNC_MSG_IDS:
            try:
                self.stage.add_background_comm(msg_id)
            except Exception:
                pass

        # Belt and braces: explicitly tell the controller to stop broadcasting
        # automatic status updates, then clear whatever is already queued.
        self.disable_auto_updates()

        # --- Auto-detect stage model / travel range (best-effort) ---
        if travel_mm is not None:
            self.travel_mm = float(travel_mm)
            print(f"[INFO] Stage {self.serial}: travel set manually to {self.travel_mm:.1f} mm")
        else:
            try:
                name = self.stage.get_stage()
                self.stage_name = str(name).strip().upper() if name else None
            except Exception:
                self.stage_name = None

            if self.stage_name in STAGE_TRAVEL_MM:
                self.travel_mm = STAGE_TRAVEL_MM[self.stage_name]
                print(f"[INFO] Stage {self.serial}: detected {self.stage_name}, travel {self.travel_mm:.1f} mm")
            else:
                print(f"[WARN] Stage {self.serial}: model not reported by controller "
                      f"(got {self.stage_name!r}); assuming MTS50-Z8, travel {self.travel_mm:.1f} mm")

        # --- Force a known, safe homing configuration on the controller ---
        self.configure_homing()
        self.print_switch_config()

        status = self.get_status_list()
        print(f"[INFO] Stage {self.serial}: homed={self.is_homed()}, "
              f"position={self._fmt(self.get_position_mm())} mm, "
              f"enabled={'enabled' in status}, status={status}")
        if "enabled" not in status:
            print(f"[WARN] Stage {self.serial}: controller channel is NOT enabled - "
                  f"motion commands will be accepted but the motor will not turn. "
                  f"Fix: power-cycle this axis' KDC101 controller (off/on), then restart "
                  f"the app and home the axis. This is not a mechanical fault.")

    def configure_homing(self, direction: Optional[str] = None) -> None:
        """
        Write homing parameters to the KDC101 so that Home goes to the chosen
        limit switch at a modest velocity, regardless of what Kinesis or a
        previous user left in the controller.

        direction defaults to this stage's normal end ('-'). Pass "forward" to
        reference against the '+' switch instead - see home(direction=...).
        """
        if self.stage is None:
            return
        direction = direction or self.home_direction
        try:
            params = self.stage.setup_homing(
                home_direction=direction,
                limit_switch=direction,
                velocity=HOME_VELOCITY_MM_S / SCALE_FACTOR,
                scale=True,  # same convention as setup_velocity()
            )
            end = "'-' end, 0 mm" if direction == "reverse" else "'+' end"
            print(f"[INFO] Stage {self.serial}: homing -> {direction} ({end}) "
                  f"@ {HOME_VELOCITY_MM_S} mm/s (controller reports {params})")
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: could not set homing parameters ({e}); "
                  f"controller defaults will be used - verify Home direction manually!")

    def set_home_direction(self, direction: str) -> None:
        """
        Change which limit switch this axis homes against from now on.

        Use when an axis' normal '-' switch is proven dead: setting "forward"
        makes Home and Home All reference against the '+' switch instead, so
        the axis is never again driven into the end stop it cannot detect.
        """
        if direction not in ("reverse", "forward"):
            raise ValueError(f"Unknown homing direction: {direction!r}")
        if direction == self.home_direction:
            return
        self.home_direction = direction
        self.configure_homing(direction)
        print(f"[INFO] Stage {self.serial}: home direction is now '{direction}' "
              f"for every future homing operation.")

    def print_switch_config(self) -> None:
        """
        Print the limit-switch configuration. If a stage's switches are set to
        'ignore', homing can never complete: the stage drives into the
        mechanical end stop instead of stopping at the switch.
        """
        if self.stage is None:
            return
        try:
            lp = self.stage.get_limit_switch_parameters()
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: could not read limit switch config ({e})")
            return

        print(f"[INFO] Stage {self.serial}: limit switches {lp}")
        kinds = {str(getattr(lp, "hw_kind_cw", "")), str(getattr(lp, "hw_kind_ccw", ""))}
        if "ignore" in kinds:
            print(f"[WARN] Stage {self.serial}: a hardware limit switch is set to 'ignore'. "
                  f"Homing cannot detect the end of travel on this axis - fix it in Kinesis "
                  f"(Settings > Limit Switches) or call enable_limit_switches().")

    def enable_limit_switches(self) -> None:
        """
        Set both hardware limit switches to 'make' (switch closes at the limit),
        which is the normal MTS50-Z8 configuration. Use only if the axis is
        reported as 'ignore' above and you know the switches are wired.
        """
        if self.stage is None:
            return
        try:
            lp = self.stage.setup_limit_switch(hw_kind_cw="make", hw_kind_ccw="make")
            print(f"[INFO] Stage {self.serial}: limit switches set to 'make' -> {lp}")
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: could not set limit switches ({e})")

    def get_status_list(self):
        if self.stage is None:
            return []
        try:
            with self._lock:
                return list(self.stage.get_status())
        except Exception:
            return []

    def hw_error_count(self) -> int:
        """Number of unprompted hardware-error messages received so far."""
        total = 0
        for msg_id in HW_ERROR_MSG_IDS:
            try:
                cnt, _ = self.stage.check_background_comm(msg_id)
                total += cnt
            except Exception:
                pass
        return total

    def is_enabled(self) -> bool:
        return "enabled" in self.get_status_list()

    def try_enable(self) -> bool:
        """
        Re-enable the controller channel after a hardware error latched it off.
        Returns True if the channel reports enabled afterwards.
        """
        if self.stage is None:
            return False
        try:
            self.stage._enable_channel(True)
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: enable request failed ({e})")
        time.sleep(0.5)
        self._flush_comm()
        ok = self.is_enabled()
        print(f"[INFO] Stage {self.serial}: re-enable attempt -> enabled={ok}")
        return ok

    def get_faults(self):
        """Return the fault/overload status bits currently reported, if any."""
        return [b for b in self.get_status_list() if b in FAULT_BITS]

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt(v: Optional[float]) -> str:
        return "?" if v is None else f"{v:.3f}"

    def is_homed(self) -> bool:
        if self.stage is None:
            return False
        try:
            with self._lock:
                return bool(self.stage.is_homed())
        except Exception:
            return False

    def is_referenced(self) -> bool:
        """
        True if the absolute position is known - either because the axis was
        homed, or because the operator declared it with set_reference_here().
        """
        return self.is_homed() or self._manual_reference

    def reference_source(self) -> Optional[str]:
        """'homed', 'homed_forward', 'manual', or None if position is unknown."""
        if self.is_homed():
            return "homed" if self._reference_end == "reverse" else "homed_forward"
        return "manual" if self._manual_reference else None

    def set_reference_here(self, position_mm: float = 0.0) -> None:
        """
        Declare the CURRENT position without moving the stage.

        A fallback for an axis that refuses to home: travel-limit checking is
        re-enabled, but it is only as accurate as the value given here.
        """
        if self.stage is None:
            raise RuntimeError("Stage is not initialised.")
        with self._lock:
            self.stage.set_position_reference(position_mm / SCALE_FACTOR, scale=True)
        self._manual_reference = True
        print(f"[INFO] Stage {self.serial}: position reference set by hand to "
              f"{position_mm:.3f} mm (not homed)")

    def clear_manual_reference(self) -> None:
        self._manual_reference = False

    def get_position_mm(self) -> Optional[float]:
        """Current position in mm (same convention as move_by). None if unavailable."""
        if self.stage is None:
            return None
        try:
            with self._lock:
                pos = self.stage.get_position(scale=True)
            return float(pos) * SCALE_FACTOR
        except Exception:
            return None

    def at_hard_limit(self) -> Optional[str]:
        """Return 'forward'/'backward' if a hardware limit switch is active, else None."""
        if self.stage is None:
            return None
        try:
            st = self.stage.get_status()
        except Exception:
            return None
        if "hw_fw_lim" in st:
            return "forward"
        if "hw_bk_lim" in st:
            return "backward"
        return None

    def snapshot(self):
        """
        Read position and status in ONE locked exchange.

        The GUI polls this instead of calling is_homed/get_position/
        at_hard_limit/get_faults separately, cutting the serial traffic per
        stage from four request/reply pairs to two.

        Returns (position_mm, referenced, source, limit, faults) where source is
        'homed', 'manual' or None.
        """
        with self._lock:
            pos = self.get_position_mm()
            status = self.get_status_list()

        limit = "forward" if "hw_fw_lim" in status else ("backward" if "hw_bk_lim" in status else None)
        faults = [b for b in status if b in FAULT_BITS]

        if "homed" in status:
            source = "homed" if self._reference_end == "reverse" else "homed_forward"
        elif self._manual_reference:
            source = "manual"
        else:
            source = None
        return pos, source is not None, source, limit, faults

    def check_move(self, distance_mm: float) -> MoveCheck:
        """
        Verify a relative move stays inside [0, travel - SAFETY_MARGIN].

        If the stage has no position reference the absolute position is
        unknown, so the check cannot pass: ok=False, homed=False. The caller
        decides whether to ask the user for confirmation in that case.
        """
        try:
            distance_mm = float(distance_mm)
        except Exception:
            return MoveCheck(False, self.is_referenced(), None, None, self.travel_mm,
                             "Distance is not numeric.")

        lo = -HOME_TOLERANCE_MM
        hi = self.travel_mm - SAFETY_MARGIN_MM

        if abs(distance_mm) > self.travel_mm:
            return MoveCheck(False, self.is_referenced(), None, None, self.travel_mm,
                             f"|distance| {abs(distance_mm):.3f} mm exceeds stage travel ({self.travel_mm:.1f} mm).")

        referenced = self.is_referenced()
        pos = self.get_position_mm()
        if not referenced or pos is None:
            return MoveCheck(False, False, pos, None, self.travel_mm,
                             "Stage has no position reference: home it, or use 'Set 0' "
                             "at a position you know. Travel limits cannot be verified "
                             "until then.")

        target = pos + distance_mm
        if target < lo or target > hi:
            return MoveCheck(False, True, pos, target, self.travel_mm,
                             f"Target {target:.3f} mm is outside the safe range "
                             f"[0, {hi:.1f}] mm (current {pos:.3f} mm, move {distance_mm:+.3f} mm).")

        return MoveCheck(True, True, pos, target, self.travel_mm, "OK")

    # ------------------------------------------------------------------
    # Motion
    # ------------------------------------------------------------------
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
        if max_velocity > MAX_VELOCITY_MM_S:
            raise ValueError(
                f"max_velocity {max_velocity:.3f} mm/s exceeds stage limit {MAX_VELOCITY_MM_S:.1f} mm/s."
            )

        try:
            min_velocity_scaled = min_velocity / SCALE_FACTOR
            acceleration_scaled = acceleration / SCALE_FACTOR
            max_velocity_scaled = max_velocity / SCALE_FACTOR

            self._retry_comm(
                lambda: self.stage.setup_velocity(
                    min_velocity=min_velocity_scaled,
                    acceleration=acceleration_scaled,
                    max_velocity=max_velocity_scaled,
                    scale=True,  # keep validated behaviour
                ),
                "set velocity",
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

        if self._abort.is_set():
            # Nothing but the emergency stop's own return move (issued after
            # clear_abort()) may move an axis while one is being handled -
            # e.g. a touch probe's abort-retract, or a sequence's next step.
            raise RuntimeError(f"Stage {self.serial}: emergency stop active; "
                               f"move of {distance_mm:+.3f} mm refused.")

        # distance can be negative (direction), so do not block that
        try:
            before = self.get_position_mm()
            distance_scaled = distance_mm / SCALE_FACTOR
            print(f"[INFO] Stage {self.serial} moving by {distance_mm:.4f} mm "
                  f"(from {self._fmt(before)} mm)")
            self._retry_comm(
                lambda: self.stage.move_by(distance_scaled, scale=True),  # keep validated behaviour
                "move",
            )
        except Exception as e:
            raise RuntimeError(f"Failed to move stage {self.serial} by {distance_mm} mm: {e}") from e

    def wait_until_stopped(self, should_continue=None, timeout_s: float = 600.0):
        """
        Wait for the current move to finish, with the same overload protection
        used during homing.

        `should_continue` is an optional callable; when it returns False the
        move is stopped and (True, "aborted") is returned.

        Returns (ok, reason). ok=False means the motor was stopped because of
        a fault or a stall.
        """
        t0 = time.time()
        ref_pos = self.get_position_mm()
        last_move_t = time.time()

        while time.time() - t0 < timeout_s:
            if self._abort.is_set():
                # abort() already stopped the axis; issuing anything more from
                # here could cut off the emergency stop's own return move.
                return False, "aborted by emergency stop"

            if should_continue is not None and not should_continue():
                self.stop()
                return True, "aborted"

            try:
                if not self.stage.is_moving():
                    return True, "done"
            except Exception:
                self._flush_comm()
                time.sleep(0.05)
                continue

            status = self.get_status_list()
            faults = [b for b in status if b in FAULT_BITS]
            if faults:
                self.stop()
                return False, (f"controller reports {faults} at "
                               f"{self._fmt(self.get_position_mm())} mm; motor stopped")

            pos = self.get_position_mm()
            if pos is not None and ref_pos is not None and abs(pos - ref_pos) > STALL_TOLERANCE_MM:
                ref_pos = pos
                last_move_t = time.time()

            if time.time() - t0 > STALL_GRACE_S and time.time() - last_move_t > STALL_TIMEOUT_S:
                self.stop()
                return False, (f"no movement for {STALL_TIMEOUT_S:.0f} s at "
                               f"{self._fmt(pos)} mm (status={status}); motor stopped")

            time.sleep(0.1)

        self.stop()
        return False, f"move timed out after {timeout_s:.0f} s; motor stopped"

    def stop(self) -> None:
        if self.stage is None:
            return
        try:
            with self._lock:
                self.stage.stop()
        except Exception as e:
            print(f"[WARN] Stage {self.serial} stop failed: {e}")

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------
    def arm_return_point(self, hold: bool = False,
                         expected_s: Optional[float] = None) -> None:
        """
        Remember where this axis is as an operator command starts moving it.

        hold=True   a blocking command owns the axis until it calls
                    release_return_point() (home, a sequence step, a touch
                    pass): an emergency stop any time before that returns
                    the axis here.
        hold=False  a fire-and-forget jog: counts as in flight only for
                    expected_s, so an emergency stop long after the jog
                    finished does not "undo" a move nobody is worried about.

        Also records whether the position was referenced, because starting a
        home clears the controller's homed bit - see return_to_point().
        """
        self.return_point_mm = self.get_position_mm()
        self.return_point_referenced = self.is_referenced()
        self._return_hold = bool(hold)
        self._return_until = time.monotonic() + (expected_s or 0.0)
        self._home_interrupted = False
        self._home_frame_changed = False

    def release_return_point(self) -> None:
        """The command finished on its own - nothing left to undo."""
        self.return_point_mm = None
        self._return_hold = False
        self._return_until = 0.0

    def return_point_pending(self) -> bool:
        """True if a command is moving this axis and could be undone."""
        return self.return_point_mm is not None and (
            self._return_hold or time.monotonic() < self._return_until)

    def abort(self) -> None:
        """
        Emergency stop: halt now, and make every blocking loop in this class
        (homing, wait_until_stopped) give up instead of carrying on.

        Abrupt and non-blocking on purpose. The driver's default stop()
        decelerates on a profile AND waits for it, so stopping three axes
        with it halts X, then Y, then Z; this halts all three at once.
        """
        self._abort.set()
        if self.stage is None:
            return
        try:
            with self._lock:
                self.stage.stop(immediate=True, sync=False)
        except TypeError:
            self.stop()
        except Exception as e:
            print(f"[WARN] Stage {self.serial} abort stop failed: {e}")

    def clear_abort(self) -> None:
        self._abort.clear()

    def return_to_point(self, target_mm: float, was_referenced: bool,
                        vel_mm_s: float = RECOMMENDED_VELOCITY_MM_S) -> Tuple[bool, str]:
        """
        Drive back to target_mm (the return point captured when the
        emergency stop was pressed) and, if a home was interrupted on the
        way, put the position reference back.

        Starting a home clears the controller's homed bit, so an interrupted
        home leaves the axis "not referenced" (the '?' next to its position)
        even though nothing about its counter changed - UNLESS homing got as
        far as the limit switch, where the controller re-zeroes the counter.
        _home_once() watches for that. If it happened, the counter no longer
        means what target_mm meant: the axis is left where it stopped, not
        moved, and has to be homed again.

        Returns (ok, reason); never raises - the caller is already handling
        an emergency.
        """
        if self._abort.is_set():
            return False, "aborted by emergency stop"

        if self._home_interrupted and self._home_frame_changed:
            return False, ("homing had reached the limit switch, so the controller may "
                           "have re-zeroed; left where it stopped - home it again")

        self.wait_until_stopped(timeout_s=2.0)
        current = self.get_position_mm()
        if current is None:
            return False, "current position unknown; not moved"

        delta = float(target_mm) - current
        if abs(delta) > self.travel_mm:
            return False, f"return of {delta:+.3f} mm exceeds the travel; not moved"

        if abs(delta) >= 0.001:
            try:
                self.setup_velocity(0, 10, vel_mm_s)
                self.move_by(delta)
            except Exception as e:
                return False, f"return move failed: {e}"
            ok, reason = self.wait_until_stopped(timeout_s=abs(delta) / vel_mm_s + 15.0)
            if not ok:
                return False, reason

        restored = ""
        if self._home_interrupted and was_referenced and not self.is_homed():
            # Same counter, same place: the old reference still holds. Marked
            # as a manual reference, because that is what it now is - not a
            # fresh home.
            self._manual_reference = True
            restored = "; position reference restored"
        self._home_interrupted = False

        pos = self.get_position_mm()
        return True, f"returned to {self._fmt(pos)} mm{restored}"

    def disable_auto_updates(self) -> None:
        """Ask the controller to stop sending unprompted status updates."""
        if self.stage is None:
            return
        try:
            self.stage.send_comm(MSG_STOP_UPDATE_MSGS)
            time.sleep(0.1)
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: could not disable automatic updates ({e})")
        self._flush_comm()

    def _retry_comm(self, fn, what: str, attempts: int = 3):
        """
        Run a controller call, recovering from serial desync.

        A stray unprompted message makes pylablib read the wrong reply; the
        cure is to flush and repeat, not to fail the operation.
        """
        last = None
        for attempt in range(1, attempts + 1):
            try:
                with self._lock:
                    return fn()
            except Exception as e:
                last = e
                if "unexpected command received" not in str(e):
                    raise
                print(f"[WARN] Stage {self.serial}: {what} desynced ({e}); "
                      f"resynchronising and retrying ({attempt}/{attempts})...")
                self.disable_auto_updates()
                time.sleep(0.2)
        raise RuntimeError(f"{what} failed on stage {self.serial} after {attempts} attempts: {last}")

    def _flush_comm(self) -> None:
        """
        Drop any unsolicited messages sitting in the serial buffer.

        The KDC101 spontaneously emits notification messages (e.g. 0x0081).
        If one arrives while pylablib is waiting for a specific reply it
        raises "unexpected command received", which is a communication
        desync, NOT a motion failure. Flushing resynchronises the link.
        """
        if self.stage is None:
            return
        try:
            with self._lock:
                self.stage.flush_comm()
        except Exception:
            pass

    def home(self, timeout_s: float = HOME_TIMEOUT_S,
             direction: Optional[str] = None) -> None:
        """
        Home the stage towards a limit switch. Default: the '-' end.

        direction="forward" references against the '+' switch instead - the
        fallback when the '-' switch is suspected faulty. The controller sets
        the position to 0 wherever it homes, so after a forward home the
        reference is corrected to travel_mm: 0 mm stays at the '-' end and
        every travel-limit check keeps working unchanged. That correction uses
        the NOMINAL travel, so the resulting 0 is a good working reference but
        not a measured one - reference_source() reports it as 'homed_forward'.

        ONE attempt. If the controller trips, the channel is re-enabled and the
        stage is backed off the end stop so it is not left loaded - but homing
        is NOT driven into the stop a second time. Repeating a failed home only
        wears a stiff axis; the operator gets a clear message and the 'Set 0'
        fallback instead.
        """
        if self.stage is None:
            raise RuntimeError("Stage is not initialised.")

        direction = direction or self.home_direction
        if direction not in ("reverse", "forward"):
            raise ValueError(f"Unknown homing direction: {direction!r}")

        # Home is a blocking command: an emergency stop before it finishes
        # brings the axis back here (and restores its reference - starting a
        # home clears the controller's homed bit).
        self.arm_return_point(hold=True)

        if direction != self.home_direction:
            print(f"[INFO] Stage {self.serial}: homing against the '+' switch this time "
                  f"(fallback); the normal '-' direction is restored afterwards.")
            self.configure_homing(direction)

        try:
            self._home_once(timeout_s)
            # The controller has re-zeroed at the switch: from here on the
            # return point is in a frame that no longer exists, so an abort
            # during the rest of this method must not drive back to it.
            self._home_frame_changed = True
            self._manual_reference = False      # a real home supersedes any manual zero
            self._reference_end = direction

            if direction == "forward":
                # The controller now calls the '+' switch 0 mm. Shift the frame
                # back so 0 mm means the '-' end, as everything else assumes.
                with self._lock:
                    self.stage.set_position_reference(
                        self.travel_mm / SCALE_FACTOR, scale=True)
                print(f"[INFO] Stage {self.serial}: homed at the '+' switch; position "
                      f"reference set to {self.travel_mm:.1f} mm so 0 mm remains the "
                      f"'-' end (derived from nominal travel, not measured).")
                # Finish where a normal home would finish, so the operator does
                # not have to drive the axis back by hand every time.
                self._return_to_zero()
            return
        except HomingAborted:
            # Not a fault: the operator's emergency stop. The return point is
            # left armed for it; see return_to_point().
            self._home_interrupted = True
            raise
        except _ChannelDisabled as e:
            print(f"[WARN] Stage {self.serial}: {e}")
            print(f"[INFO] Stage {self.serial}: releasing the axis; homing will NOT be "
                  f"retried automatically.")
            released = False
            if self.try_enable():
                self._retreat_from_end_stop(homed_direction=direction)
                released = True

            tail = ("The axis was backed off the end stop and left there."
                    if released else
                    "The channel would NOT re-enable, so the axis could not be backed "
                    "off - power-cycle this KDC101 before touching it again.")

            switch = "'-'" if direction == "reverse" else "'+'"
            raise RuntimeError(
                f"Stage {self.serial}: {e} {tail}\n\n"
                f"This attempt homed against the {switch} switch. Homing is not retried "
                f"automatically: this is a mechanical or controller fault, and repeating "
                f"it only wears the axis. Grease the lead screw, try the other switch, or "
                f"use 'Set 0' at a position you know to work without homing."
            ) from e
        finally:
            # Finished - successfully or with a fault of its own - so there is
            # nothing for an emergency stop to undo. Only an abort keeps it.
            if not self._abort.is_set():
                self.release_return_point()
            # A fallback home is a one-off: leave the controller configured for
            # the normal '-' end so the next Home behaves as documented.
            if direction != self.home_direction:
                self.configure_homing(self.home_direction)

    def _return_to_zero(self, target_mm: float = 0.0) -> None:
        """
        Drive back to 0 mm after homing at the '+' end.

        A failure here is reported but not raised: the reference is already
        valid and the position is known, so the axis is simply left where it
        stopped rather than the whole homing operation being called a failure.
        """
        pos = self.get_position_mm()
        if pos is None:
            print(f"[WARN] Stage {self.serial}: position unreadable, not returning to "
                  f"{target_mm:.1f} mm.")
            return

        distance = target_mm - pos
        if abs(distance) < STALL_TOLERANCE_MM:
            return

        print(f"[INFO] Stage {self.serial}: returning to {target_mm:.1f} mm "
              f"({distance:+.3f} mm @ {HOME_RETURN_VELOCITY_MM_S} mm/s)...")
        try:
            self.setup_velocity(0, 10, HOME_RETURN_VELOCITY_MM_S)
            self.move_by(distance)
            ok, reason = self.wait_until_stopped(timeout_s=HOME_RETURN_TIMEOUT_S)
            if ok:
                print(f"[INFO] Stage {self.serial}: at "
                      f"{self._fmt(self.get_position_mm())} mm, ready.")
            else:
                print(f"[WARN] Stage {self.serial}: return move stopped early ({reason}). "
                      f"The position reference is still valid.")
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: return to {target_mm:.1f} mm failed ({e}). "
                  f"The position reference is still valid.")

    def _retreat_from_end_stop(self, distance_mm: float = HOME_RETREAT_MM,
                               homed_direction: Optional[str] = None) -> None:
        """
        Move a short way opposite to the direction we were homing in, so the
        axis is not left pressed against the mechanical end stop.
        """
        homed_direction = homed_direction or self.home_direction
        away = distance_mm if homed_direction == "reverse" else -distance_mm
        print(f"[INFO] Stage {self.serial}: backing off {away:+.1f} mm from the end stop "
              f"so it is not left loaded...")
        try:
            self.setup_velocity(0, 10, 0.5)
            self.move_by(away)
            self.wait_until_stopped(timeout_s=30.0)
        except Exception as e:
            print(f"[WARN] Stage {self.serial}: back-off move failed ({e})")

    def _home_once(self, timeout_s: float) -> None:
        print(f"[INFO] Stage {self.serial} moving to home")
        self._flush_comm()
        hw_errors_before = self.hw_error_count()

        if self._abort.is_set():
            raise HomingAborted(f"Homing of stage {self.serial} not started: "
                                f"emergency stop is active.")

        # --- Start homing (async) ---
        started = False
        last_exc = None
        for attempt in (1, 2):
            try:
                self.stage.home(sync=False, force=True)
                started = True
                break
            except Exception as e:
                last_exc = e
                print(f"[WARN] Stage {self.serial}: home command attempt {attempt} failed ({e}); "
                      f"flushing and retrying...")
                self._flush_comm()
                time.sleep(0.3)

        if not started:
            raise RuntimeError(f"Failed to start homing on stage {self.serial}: {last_exc}")

        # --- Poll for completion, watching for progress ---
        t0 = time.time()
        comm_errors = 0
        last_report = 0.0
        ref_pos = self.get_position_mm()
        last_move_t = time.time()

        last_pos = ref_pos
        while time.time() - t0 < timeout_s:
            time.sleep(0.2)
            elapsed = time.time() - t0

            if self._abort.is_set():
                # abort() has already stopped the motor. Raise without any
                # stop()/retreat of our own: the emergency stop owns the axis
                # now and is about to drive it back.
                raise HomingAborted(
                    f"Homing of stage {self.serial} interrupted by emergency stop at "
                    f"{self._fmt(self.get_position_mm())} mm.")

            try:
                if self.stage.is_homed():
                    pos = self.get_position_mm()
                    print(f"[INFO] Stage {self.serial} homed, position={self._fmt(pos)} mm "
                          f"({elapsed:.1f} s)")
                    return
            except Exception as e:
                comm_errors += 1
                if comm_errors in (1, 10, 50):
                    print(f"[WARN] Stage {self.serial}: status read glitch during homing "
                          f"({e}); resynchronising (x{comm_errors})")
                self._flush_comm()
                # Readings around a desync are unreliable: restart the stall window.
                last_move_t = time.time()
                ref_pos = self.get_position_mm()
                continue

            status = self.get_status_list()
            pos = self.get_position_mm()

            # Has the controller (possibly) re-zeroed its counter? It does so
            # at the limit switch. Seeing a limit bit, or the position jumping
            # further in one 0.2 s poll than the stage can travel (2.4 mm/s max),
            # means an interrupted home can no longer trust the old frame -
            # see return_to_point().
            if "hw_bk_lim" in status or "hw_fw_lim" in status:
                self._home_frame_changed = True
            if pos is not None and last_pos is not None and abs(pos - last_pos) > 1.0:
                self._home_frame_changed = True
            if pos is not None:
                last_pos = pos

            # --- 1) Controller latched its output off (hardware error) ---
            if "enabled" not in status:
                self.stop()
                n_new = self.hw_error_count() - hw_errors_before
                raise _ChannelDisabled(
                    f"controller disabled its output {elapsed:.1f} s into homing at "
                    f"{self._fmt(pos)} mm"
                    + (f" after {n_new} hardware-error message(s)" if n_new > 0 else "")
                    + f" (status={status})."
                )

            # --- 2) Controller-reported overload: stop at once ---
            faults = [b for b in status if b in FAULT_BITS]
            if faults:
                self.stop()
                raise RuntimeError(
                    f"Homing aborted on stage {self.serial} after {elapsed:.1f} s: controller "
                    f"reports {faults} at {self._fmt(pos)} mm. Motor stopped immediately "
                    f"(the axis is loaded or blocked)."
                )

            # --- 3) No progress: stop before the motor grinds ---
            if pos is not None and ref_pos is not None and abs(pos - ref_pos) > STALL_TOLERANCE_MM:
                ref_pos = pos
                last_move_t = time.time()

            if elapsed - last_report >= 5.0:
                last_report = elapsed
                print(f"[HOME] {self.serial}: {elapsed:.0f}s pos={self._fmt(pos)} mm status={status}")

            if elapsed > STALL_GRACE_S and time.time() - last_move_t > STALL_TIMEOUT_S:
                self.stop()
                raise RuntimeError(
                    f"Homing stalled on stage {self.serial}: no movement for "
                    f"{STALL_TIMEOUT_S:.0f} s at {self._fmt(pos)} mm (status={status}). "
                    f"Motor stopped. The axis is mechanically blocked or the limit switch "
                    f"is not responding."
                )

        self.stop()
        raise RuntimeError(
            f"Homing timed out on stage {self.serial} after {timeout_s:.0f} s "
            f"(comm glitches: {comm_errors}, last position {self._fmt(ref_pos)} mm, "
            f"status={self.get_status_list()}). Motor stopped."
        )

    def is_moving(self) -> bool:
        if self.stage is None:
            return False
        try:
            with self._lock:
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


# ---------------------------------------------------------------------------
# Simultaneous moves
# ---------------------------------------------------------------------------
def wait_until_all_stopped(controllers, should_continue=None, timeout_s: float = 600.0):
    """
    Wait for several axes moving at the same time, with the same overload
    protection used for a single axis.

    If ANY axis faults or stalls, every axis in the group is stopped - a
    combined move is one motion, so a problem on one axis must not leave the
    others running.

    Returns (ok, reason); ok=False means the group was stopped for safety.
    """
    controllers = list(controllers)
    if not controllers:
        return True, "done"

    def stop_all():
        for c in controllers:
            try:
                c.stop()
            except Exception:
                pass

    t0 = time.time()
    ref_pos = {c: c.get_position_mm() for c in controllers}
    last_move = {c: time.time() for c in controllers}

    def aborting(c) -> bool:
        ev = getattr(c, "_abort", None)
        return isinstance(ev, threading.Event) and ev.is_set()

    while time.time() - t0 < timeout_s:
        if any(aborting(c) for c in controllers):
            # Emergency stop: the axes are already stopped and are about to be
            # driven back; a stop_all() from here could cut that off.
            return True, "aborted by emergency stop"

        if should_continue is not None and not should_continue():
            stop_all()
            return True, "aborted"

        any_moving = False
        for c in controllers:
            try:
                moving = c.is_moving()
            except Exception:
                c._flush_comm()
                any_moving = True
                continue

            if not moving:
                continue
            any_moving = True

            status = c.get_status_list()
            faults = [b for b in status if b in FAULT_BITS]
            if faults:
                stop_all()
                return False, (f"stage {c.serial} reports {faults} at "
                               f"{c._fmt(c.get_position_mm())} mm; all axes stopped")

            pos = c.get_position_mm()
            if pos is not None and ref_pos[c] is not None and abs(pos - ref_pos[c]) > STALL_TOLERANCE_MM:
                ref_pos[c] = pos
                last_move[c] = time.time()

            if (time.time() - t0 > STALL_GRACE_S
                    and time.time() - last_move[c] > STALL_TIMEOUT_S):
                stop_all()
                return False, (f"stage {c.serial} made no progress for "
                               f"{STALL_TIMEOUT_S:.0f} s at {c._fmt(pos)} mm; all axes stopped")

        if not any_moving:
            return True, "done"

        time.sleep(0.1)

    stop_all()
    return False, f"combined move timed out after {timeout_s:.0f} s; all axes stopped"
