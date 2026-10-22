# lib/set_touch_point.py
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Optional, Tuple


@dataclass(frozen=True)
class TouchConfig:
    # Motion
    z_max_mm: float = 2.5

    # The approach is two-stage. Most of the search is empty air and can be
    # crossed at approach_vel_mm_s; the last fraction of a millimetre decides
    # how hard the probe presses whatever it finds, and is taken at
    # fine_vel_mm_s.
    #
    # The changeover is a force, not a position, because the surface's position
    # is the unknown being measured. pre_trigger_n sits below f_touch_n: the
    # first whisper of contact slows the stage, and the touch itself is then
    # latched at the slow speed. A false pre-trigger on noise costs time and
    # nothing else - the approach simply finishes slowly.
    approach_vel_mm_s: float = 0.5      # coarse, across the gap
    fine_vel_mm_s: float = 0.15         # from pre_trigger_n to the latch
    pre_trigger_n: float = 0.005        # ~0.5 gf, a third of f_touch_n

    overtravel_mm: float = 0.2
    retract_mm: float = 0.8
    retract_emergency_mm: float = 1.0

    return_vel_mm_s: float = 0.15       # back TOWARD the surface, to the latched touch
    return_step_mm: float = 0.02
    return_tol_mm: float = 0.01

    # Moves AWAY from the surface (the post-touch retract, the emergency
    # retract). Nothing is measured on the way out, so these need not crawl;
    # the default keeps the old behaviour for any caller that does not ask.
    retract_vel_mm_s: float = 0.15

    # Detection (force in N)
    f_touch_n: float = 0.015          # ~1.5 gf
    confirm_ms: int = 50

    # Safety
    f_abs_limit_n: float = 0.08       # ~8 gf
    dFdt_limit_n_s: float = 1.0       # conservative starter, tune later

    # Baseline + timeouts
    baseline_s: float = 0.6
    probe_timeout_s: float = 10.0
    sample_stale_s: float = 0.5


@dataclass(frozen=True)
class TouchResult:
    ok: bool
    reason: str

    touch_time_us: Optional[int] = None
    touch_pz: Optional[float] = None

    # The last pz seen before the run ended, latched or not - Reached Z_MAX,
    # a hard-limit abort and a timeout all stop with the stage somewhere
    # specific, and a caller chaining a coarse pass into a fine one needs
    # that even when ok=False and touch_pz was never set.
    last_pz: Optional[float] = None

    fz0: Optional[float] = None
    fz_touch: Optional[float] = None

    peak_fz_net: float = 0.0
    peak_dFdt: float = 0.0


Sample = Tuple[int, float, float]  # (time_us, fz, pz)


class TouchProbeRunner:
    """
    Blocking touch procedure runner.

    Requires:
    - stage_z: StageController-like object with setup_velocity(), move_by(), stop(), is_moving()
    - get_latest_sample(): returns (time_us, fz, pz) coming from Display->ATI queue
    """

    def __init__(
        self,
        stage_z,
        get_latest_sample: Callable[[], Optional[Sample]],
        cfg: TouchConfig,
        abort_event: threading.Event,
        status_cb: Optional[Callable[[str], None]] = None,
    ):
        self.stage_z = stage_z
        self.get_latest_sample = get_latest_sample
        self.cfg = cfg
        self.abort_event = abort_event
        self.status_cb = status_cb

        self._fz0 = 0.0
        self._peak_fz_net = 0.0
        self._peak_dFdt = 0.0

    def _status(self, msg: str) -> None:
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass

    def _now(self) -> float:
        return time.monotonic()

    def _get_sample(self) -> Optional[Sample]:
        s = self.get_latest_sample()
        if s is None:
            return None
        t_us, fz, pz = s
        return (int(t_us), float(fz), float(pz))

    def _setup_vel(self, vel_mm_s: float) -> None:
        self.stage_z.setup_velocity(0, 10, float(vel_mm_s))

    def _wait_stage_stop(self, timeout_s: float = 2.0) -> None:
        t0 = self._now()
        while True:
            try:
                moving = bool(self.stage_z.is_moving())
            except Exception:
                moving = False

            if not moving:
                return
            if self._now() - t0 > timeout_s:
                return
            time.sleep(0.02)

    def _move_by_blocking(self, dist_mm: float, vel_mm_s: Optional[float] = None) -> None:
        if vel_mm_s is not None:
            self._setup_vel(vel_mm_s)
        self.stage_z.move_by(dist_mm)
        # If move_by is non-blocking on your driver, this ensures we wait.
        self._wait_stage_stop(timeout_s=10.0)

    def _emergency_stop_and_retract(self) -> None:
        try:
            self.stage_z.stop()
        except Exception:
            pass
        self._wait_stage_stop(timeout_s=1.0)
        try:
            self._move_by_blocking(-abs(self.cfg.retract_emergency_mm), vel_mm_s=self.cfg.retract_vel_mm_s)
        except Exception:
            pass

    def _baseline(self) -> Optional[float]:
        self._status("Baselining...")
        t0 = self._now()
        fz_sum = 0.0
        n = 0

        last_t_us = None
        last_change_wall = self._now()

        while self._now() - t0 < self.cfg.baseline_s:
            if self.abort_event.is_set():
                return None

            s = self._get_sample()
            if s is None:
                time.sleep(0.01)
                continue

            t_us, fz, _pz = s
            if last_t_us is None or t_us != last_t_us:
                last_t_us = t_us
                last_change_wall = self._now()
                fz_sum += fz
                n += 1
            else:
                if self._now() - last_change_wall > self.cfg.sample_stale_s:
                    return None

            time.sleep(0.005)

        if n <= 5:
            return None
        return fz_sum / n

    def _return_to_touch(self, target_pz: float) -> bool:
        self._status("Returning to touch...")
        self._setup_vel(self.cfg.return_vel_mm_s)

        t0 = self._now()
        last_t_us = None
        last_change_wall = self._now()

        while True:
            if self.abort_event.is_set():
                return False
            if self._now() - t0 > self.cfg.probe_timeout_s:
                return False

            s = self._get_sample()
            if s is None:
                time.sleep(0.02)
                continue

            t_us, _fz, pz = s
            if last_t_us is None or t_us != last_t_us:
                last_t_us = t_us
                last_change_wall = self._now()
            else:
                if self._now() - last_change_wall > self.cfg.sample_stale_s:
                    return False

            err = float(target_pz - pz)
            if abs(err) <= self.cfg.return_tol_mm:
                try:
                    self.stage_z.stop()
                except Exception:
                    pass
                self._wait_stage_stop(timeout_s=0.5)
                return True

            step = max(0.0, min(abs(err), self.cfg.return_step_mm))
            dist = step if err > 0 else -step

            try:
                self._move_by_blocking(dist, vel_mm_s=self.cfg.return_vel_mm_s)
            except Exception:
                return False

    def run(self) -> TouchResult:
        # 1) Baseline
        fz0 = self._baseline()
        if fz0 is None:
            return TouchResult(ok=False, reason="No fresh data / baseline failed")

        self._fz0 = float(fz0)

        # 2) Approach
        self._status("Approaching...")
        try:
            self._setup_vel(self.cfg.approach_vel_mm_s)
        except Exception as e:
            return TouchResult(ok=False, reason=f"Stage velocity setup failed: {e}", fz0=self._fz0)

        motion_exc = {"err": None}

        def _approach_motion():
            try:
                # + direction is confirmed correct on your rig
                self.stage_z.move_by(+abs(self.cfg.z_max_mm))
            except Exception as e:
                motion_exc["err"] = e

        motion_thread = threading.Thread(target=_approach_motion, daemon=True)
        motion_thread.start()

        started_moving = False
        t_move_cmd = self._now()

        confirm_needed_s = max(0.0, self.cfg.confirm_ms / 1000.0)
        confirm_acc_s = 0.0

        last_t_us = None
        last_fz_net = None
        last_wall = self._now()
        last_change_wall = self._now()

        touch_time_us = None
        touch_pz = None
        fz_touch = None
        # Updated on every fresh sample, independently of whether a touch
        # ever latches - see TouchResult.last_pz.
        last_pz_seen = None

        # Two-stage approach bookkeeping. pz_start is filled from the first
        # sample, so the remaining travel can be worked out when the stage is
        # stopped and restarted at the fine speed.
        pz_start = None
        slowed = self.cfg.fine_vel_mm_s >= self.cfg.approach_vel_mm_s

        t_start = self._now()

        while True:
            if self.abort_event.is_set():
                self._status("Aborting...")
                self._emergency_stop_and_retract()
                return TouchResult(
                    ok=False,
                    reason="Aborted by user",
                    fz0=self._fz0,
                    peak_fz_net=self._peak_fz_net,
                    last_pz=last_pz_seen,
                    peak_dFdt=self._peak_dFdt,
                )

            if self._now() - t_start > self.cfg.probe_timeout_s:
                self._emergency_stop_and_retract()
                return TouchResult(
                    ok=False,
                    reason="Timeout during approach",
                    fz0=self._fz0,
                    peak_fz_net=self._peak_fz_net,
                    last_pz=last_pz_seen,
                    peak_dFdt=self._peak_dFdt,
                )

            # Motion error?
            if motion_exc["err"] is not None:
                self._emergency_stop_and_retract()
                return TouchResult(
                    ok=False,
                    reason=f"Stage motion failed: {motion_exc['err']}",
                    fz0=self._fz0,
                    peak_fz_net=self._peak_fz_net,
                    last_pz=last_pz_seen,
                    peak_dFdt=self._peak_dFdt,
                )

            # Robust motion state via is_moving() (do NOT use thread aliveness)
            try:
                moving = bool(self.stage_z.is_moving())
            except Exception:
                moving = False

            if not started_moving:
                if moving:
                    started_moving = True
                elif (self._now() - t_move_cmd) > 0.8:
                    self._emergency_stop_and_retract()
                    return TouchResult(
                        ok=False,
                        reason="Stage did not start moving",
                        fz0=self._fz0,
                        peak_fz_net=self._peak_fz_net,
                        last_pz=last_pz_seen,
                        peak_dFdt=self._peak_dFdt,
                    )

            if started_moving and (not moving):
                # Motion ended without touch
                try:
                    self.stage_z.stop()
                except Exception:
                    pass
                self._wait_stage_stop(timeout_s=0.5)
                return TouchResult(
                    ok=False,
                    reason="Reached Z_MAX without touch",
                    fz0=self._fz0,
                    peak_fz_net=self._peak_fz_net,
                    last_pz=last_pz_seen,
                    peak_dFdt=self._peak_dFdt,
                )

            s = self._get_sample()
            if s is None:
                time.sleep(0.01)
                continue

            t_us, fz, pz = s
            last_pz_seen = float(pz)

            # Stale detection via time_us changes
            if last_t_us is None or t_us != last_t_us:
                last_change_wall = self._now()
                dt_wall = self._now() - last_wall
                last_wall = self._now()
            else:
                if self._now() - last_change_wall > self.cfg.sample_stale_s:
                    self._emergency_stop_and_retract()
                    return TouchResult(
                        ok=False,
                        reason="Sensor stream stale",
                        fz0=self._fz0,
                        peak_fz_net=self._peak_fz_net,
                        last_pz=last_pz_seen,
                        peak_dFdt=self._peak_dFdt,
                    )
                time.sleep(0.01)
                continue

            fz_net = float(fz - self._fz0)
            self._peak_fz_net = max(self._peak_fz_net, abs(fz_net))

            # dF/dt using device time_us
            dFdt = 0.0
            if last_t_us is not None and last_fz_net is not None:
                dt_us = int(t_us - last_t_us)
                if dt_us > 0:
                    dt_s = dt_us / 1_000_000.0
                    dFdt = (fz_net - last_fz_net) / dt_s
                    self._peak_dFdt = max(self._peak_dFdt, abs(dFdt))

            last_t_us = t_us
            last_fz_net = fz_net

            if pz_start is None:
                pz_start = float(pz)

            # Stage 1 -> stage 2. Something is there: stop, and cover the rest
            # of the search slowly so the latch happens at the fine speed and
            # the overshoot past it is small.
            if (not slowed) and fz_net >= self.cfg.pre_trigger_n:
                slowed = True
                travelled = abs(float(pz) - pz_start)
                remaining = abs(self.cfg.z_max_mm) - travelled
                self._status(f"Contact at {fz_net*1000:.1f} mN - slowing to "
                             f"{self.cfg.fine_vel_mm_s:g} mm/s for the last "
                             f"{remaining:.2f} mm")
                try:
                    self.stage_z.stop()
                except Exception:
                    pass
                self._wait_stage_stop(timeout_s=1.0)

                if remaining <= 0.01:
                    # Already at the end of the search envelope; let the
                    # "Reached Z_MAX without touch" branch handle it.
                    continue

                try:
                    self._setup_vel(self.cfg.fine_vel_mm_s)
                except Exception as e:
                    self._emergency_stop_and_retract()
                    return TouchResult(
                        ok=False,
                        reason=f"Fine-approach velocity setup failed: {e}",
                        fz0=self._fz0,
                        peak_fz_net=self._peak_fz_net,
                        last_pz=last_pz_seen,
                        peak_dFdt=self._peak_dFdt,
                    )

                def _fine_motion():
                    try:
                        self.stage_z.move_by(+abs(remaining))
                    except Exception as e:
                        motion_exc["err"] = e

                motion_thread = threading.Thread(target=_fine_motion, daemon=True)
                motion_thread.start()

                # The stage is stopped and about to restart, so the
                # "stopped without touching" test has to be re-armed or it
                # fires on this very gap.
                started_moving = False
                t_move_cmd = self._now()
                confirm_acc_s = 0.0
                continue

            # Safety guards
            if abs(fz_net) >= self.cfg.f_abs_limit_n:
                self._status("Hard limit: abs force")
                self._emergency_stop_and_retract()
                return TouchResult(
                    ok=False,
                    reason="Hard limit (abs force)",
                    fz0=self._fz0,
                    peak_fz_net=self._peak_fz_net,
                    last_pz=last_pz_seen,
                    peak_dFdt=self._peak_dFdt,
                )

            # Only once the fine stage is running: decelerating from the
            # coarse speed produces a legitimate transient that is not a crash.
            if slowed and abs(dFdt) >= self.cfg.dFdt_limit_n_s:
                self._status("Hard limit: force slope")
                self._emergency_stop_and_retract()
                return TouchResult(
                    ok=False,
                    reason="Hard limit (force slope)",
                    fz0=self._fz0,
                    peak_fz_net=self._peak_fz_net,
                    last_pz=last_pz_seen,
                    peak_dFdt=self._peak_dFdt,
                )

            # Touch confirmation (net force)
            if fz_net >= self.cfg.f_touch_n:
                confirm_acc_s += max(0.0, dt_wall)
                if confirm_acc_s >= confirm_needed_s:
                    touch_time_us = int(t_us)
                    touch_pz = float(pz)
                    fz_touch = float(fz_net)

                    try:
                        self.stage_z.stop()
                    except Exception:
                        pass
                    self._wait_stage_stop(timeout_s=1.0)
                    break
            else:
                confirm_acc_s = 0.0

            time.sleep(0.005)

        # Best-effort join
        try:
            motion_thread.join(timeout=0.2)
        except Exception:
            pass

        if touch_time_us is None or touch_pz is None:
            self._emergency_stop_and_retract()
            return TouchResult(
                ok=False,
                reason="Touch not latched",
                fz0=self._fz0,
                peak_fz_net=self._peak_fz_net,
                last_pz=last_pz_seen,
                peak_dFdt=self._peak_dFdt,
            )

        # 3) Overtravel (+)
        self._status("Overtravel...")
        try:
            self._move_by_blocking(+abs(self.cfg.overtravel_mm), vel_mm_s=self.cfg.approach_vel_mm_s)
        except Exception as e:
            self._emergency_stop_and_retract()
            return TouchResult(
                ok=False,
                reason=f"Overtravel failed: {e}",
                fz0=self._fz0,
                touch_time_us=touch_time_us,
                touch_pz=touch_pz,
                fz_touch=fz_touch,
                peak_fz_net=self._peak_fz_net,
                last_pz=last_pz_seen,
                peak_dFdt=self._peak_dFdt,
            )

        # 4) Retract (-)
        self._status("Retracting...")
        try:
            self._move_by_blocking(-abs(self.cfg.retract_mm), vel_mm_s=self.cfg.retract_vel_mm_s)
        except Exception as e:
            self._emergency_stop_and_retract()
            return TouchResult(
                ok=False,
                reason=f"Retract failed: {e}",
                fz0=self._fz0,
                touch_time_us=touch_time_us,
                touch_pz=touch_pz,
                fz_touch=fz_touch,
                peak_fz_net=self._peak_fz_net,
                last_pz=last_pz_seen,
                peak_dFdt=self._peak_dFdt,
            )

        # 5) Return to touch (encoder pz target)
        ok_return = self._return_to_touch(target_pz=touch_pz)
        if not ok_return:
            self._emergency_stop_and_retract()
            return TouchResult(
                ok=False,
                reason="Return-to-touch failed",
                fz0=self._fz0,
                touch_time_us=touch_time_us,
                touch_pz=touch_pz,
                fz_touch=fz_touch,
                peak_fz_net=self._peak_fz_net,
                last_pz=last_pz_seen,
                peak_dFdt=self._peak_dFdt,
            )

        self._status("Done")
        return TouchResult(
            ok=True,
            reason="OK",
            fz0=self._fz0,
            touch_time_us=touch_time_us,
            touch_pz=touch_pz,
            fz_touch=fz_touch,
            peak_fz_net=self._peak_fz_net,
            last_pz=last_pz_seen,
            peak_dFdt=self._peak_dFdt,
        )
