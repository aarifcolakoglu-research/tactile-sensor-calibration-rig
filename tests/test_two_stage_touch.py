"""
The two-stage touch approach, driven against a simulated stage and specimen.

There is no hardware in CI and the probe is the one piece of this application
that can destroy a specimen, so the state machine is exercised here instead:
does it actually change speed, does it latch at the slow speed, and how far
past the surface does the tip end up?

The specimen is modelled as a linear spring that only pushes once the tip is past
its surface, which is enough to reproduce what the approach sees.

    python -m pytest tests/test_two_stage_touch.py -v
    python tests/test_two_stage_touch.py            # same checks, no pytest
"""

import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.set_touch_point as stp
from lib.set_touch_point import TouchConfig, TouchProbeRunner


class FakeStage:
    """A Z stage that moves at a commanded velocity in its own thread."""

    def __init__(self, clock, pos_mm=0.0):
        self.clock = clock
        self.pos = pos_mm
        self.vel = 0.0
        self.target = None
        self._moving = False
        self.velocity_log = []          # every speed the runner asked for
        self.lock = threading.Lock()

    def setup_velocity(self, _min, _acc, vel_mm_s):
        self.velocity_log.append(round(float(vel_mm_s), 4))
        self.vel = float(vel_mm_s)

    def move_by(self, distance_mm):
        with self.lock:
            self.target = self.pos + distance_mm
            self._moving = True

    def stop(self):
        with self.lock:
            self.target = None
            self._moving = False

    def is_moving(self):
        return self._moving

    def get_position_mm(self):
        return self.pos

    def tick(self, dt):
        """Advance the stage by one time slice."""
        with self.lock:
            if not self._moving or self.target is None:
                return
            step = self.vel * dt
            if abs(self.target - self.pos) <= step:
                self.pos = self.target
                self._moving = False
                self.target = None
            else:
                self.pos += step if self.target > self.pos else -step


class Rig:
    """Clock, stage and specimen, sampled the way the F/T stream is sampled."""

    SAMPLE_DT = 0.01          # 100 Hz, as the real stream runs

    def __init__(self, surface_mm, stiffness_n_per_mm=5.0, noise_n=0.0):
        self.t = 0.0
        self.stage = FakeStage(self)
        self.surface = surface_mm
        self.k = stiffness_n_per_mm
        self.noise = noise_n
        self._n = 0
        self._last_sample_t = -1e9
        self._last = None

    # Simulated seconds per clock read. The runner sleeps 5 ms of REAL time per
    # poll, so the simulation has to advance faster than real time or a 6 mm
    # search at 0.15 mm/s would take 40 s of wall clock to model.
    CLOCK_STEP = 0.005

    def now(self):
        """Every call advances time, so the runner's polling drives the sim."""
        self.t += self.CLOCK_STEP
        self.stage.tick(self.CLOCK_STEP)
        return self.t

    def force(self):
        penetration = max(0.0, self.stage.pos - self.surface)
        # a deterministic ripple, so a "noisy" case is still reproducible
        self._n += 1
        ripple = self.noise * ((self._n % 7) - 3) / 3.0
        return self.k * penetration + ripple

    def sample(self):
        """(time_us, fz, pz), stamped from the SAME clock that moves the stage.

        A new sample only appears every SAMPLE_DT of simulated time; in between
        the previous one is handed back unchanged, which is what the real
        stream does and what the runner's staleness check expects. Stamping the
        sample from its own counter instead would decouple device time from
        motion and make the apparent approach speed - and so dF/dt - wrong.
        """
        if self.t - self._last_sample_t >= self.SAMPLE_DT or self._last is None:
            self._last_sample_t = self.t
            self._last = (int(self.t * 1e6), self.force(), self.stage.pos)
        return self._last


def run_probe(surface_mm, cfg, stiffness=5.0, noise_n=0.0):
    """One probe against a simulated specimen, as fast as the CPU will model it.

    The runner sleeps between polls so it does not busy-wait on real hardware.
    Here those sleeps are pure wall-clock waste - the simulation's own clock
    advances on every read - so they are removed for the duration.
    """
    rig = Rig(surface_mm, stiffness, noise_n)

    class NoAbort:
        @staticmethod
        def is_set():
            return False

    runner = TouchProbeRunner(
        stage_z=rig.stage,
        get_latest_sample=rig.sample,
        cfg=cfg,
        abort_event=NoAbort(),
        status_cb=lambda m: None,
    )
    runner._now = rig.now          # the sim's clock drives the sim's motion

    real_sleep = stp.time.sleep
    stp.time.sleep = lambda _s: None
    try:
        return runner.run(), rig
    finally:
        stp.time.sleep = real_sleep


def config(**over):
    base = dict(
        z_max_mm=6.0,
        approach_vel_mm_s=0.15,
        fine_vel_mm_s=0.15,
        pre_trigger_n=0.005,
        overtravel_mm=0.2,
        retract_mm=0.8,
        return_vel_mm_s=0.15,
        f_touch_n=0.015,
        confirm_ms=50,
        f_abs_limit_n=0.08,
        dFdt_limit_n_s=1.0,
        baseline_s=0.6,          # _baseline() rejects n <= 5 samples
        probe_timeout_s=600.0,
        sample_stale_s=5.0,
    )
    base.update(over)
    return TouchConfig(**base)


# --- the checks ---------------------------------------------------------------

def test_a_fast_coarse_approach_trips_the_slope_guard():
    """Why the approach is single-speed: 0.5 mm/s cannot be slowed in time.

    Against a ~5 N/mm specimen at 100 Hz the force rises ~25 mN between samples,
    so the 5 mN pre-trigger and the 15 mN latch fall inside one sample and
    dF/dt reaches 2.5 N/s against a 1.0 N/s guard.
    """
    result, _ = run_probe(3.0, config(approach_vel_mm_s=0.5, fine_vel_mm_s=0.15))
    assert not result.ok, "the coarse stage was expected to trip a guard"
    assert "limit" in result.reason.lower(), result.reason


def test_the_single_speed_approach_succeeds():
    """What is actually shipped: 0.15 mm/s throughout."""
    result, rig = run_probe(surface_mm=3.0, cfg=config())
    assert result.ok, result.reason
    assert 0.15 in rig.stage.velocity_log, rig.stage.velocity_log


def test_the_latch_is_close_to_the_real_surface():
    """touch_pz is what the whole measurement is; 0.1 mm is the budget."""
    for surface in (1.0, 2.5, 4.0):
        result, _ = run_probe(surface_mm=surface, cfg=config())
        assert result.ok, f"{surface} mm: {result.reason}"
        error = result.touch_pz - surface
        assert 0 <= error < 0.10, f"surface {surface}: latched {error*1000:.1f} um past it"


def test_the_confirm_window_sets_the_speed_limit():
    """The rule the approach speed comes from.

    The stage keeps moving for the whole confirm window after the threshold is
    crossed, so the force at the latch is about

        f_touch_n + confirm_ms * approach_vel * stiffness

    and that has to stay under f_abs_limit_n or the probe aborts instead of
    latching. For this specimen (~5 N/mm), 50 ms confirm and an 80 mN abort:

        v < (0.08 - 0.015) / (0.05 * 5) = 0.26 mm/s

    0.15 mm/s sits at about half of it. 0.3 mm/s does not latch at all - and
    the failure is a breach of the force limit, not a missed surface.
    """
    slow, _ = run_probe(3.0, config())
    assert slow.ok, slow.reason
    assert slow.fz_touch < 0.08, f"latched at {slow.fz_touch*1000:.0f} mN"

    fast, _ = run_probe(3.0, config(approach_vel_mm_s=0.3, fine_vel_mm_s=0.3,
                                    dFdt_limit_n_s=99.0))
    assert not fast.ok, "0.3 mm/s was expected to breach the force limit"
    assert "abs force" in fast.reason.lower(), fast.reason


def test_it_still_finds_a_surface_at_the_far_end_of_the_search():
    """The descending leg parks the tip ~4.4 mm out; 6 mm must still reach."""
    result, _ = run_probe(surface_mm=4.4, cfg=config())
    assert result.ok, result.reason
    assert abs(result.touch_pz - 4.4) < 0.10


def test_nothing_there_is_reported_not_crashed():
    """An empty search has to come back as a reason, not an exception."""
    result, _ = run_probe(surface_mm=99.0, cfg=config())
    assert not result.ok
    assert "Z_MAX" in result.reason, result.reason


def test_a_stiff_collision_still_trips_the_hard_limit():
    """A rigid obstacle must abort, not be latched as a touch."""
    result, _ = run_probe(surface_mm=2.0, cfg=config(), stiffness=5000.0)
    assert not result.ok, "a 5000 N/mm obstacle was accepted as a touch"
    assert "limit" in result.reason.lower(), result.reason


def test_noise_at_the_pre_trigger_does_not_break_it():
    """A false pre-trigger costs time, never correctness."""
    result, rig = run_probe(surface_mm=3.0, cfg=config(), noise_n=0.004)
    assert result.ok, result.reason
    assert 0.15 in rig.stage.velocity_log
    assert abs(result.touch_pz - 3.0) < 0.15


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    width = max(len(c.__name__) for c in checks)
    failed = 0
    for check in checks:
        try:
            check()
            summary = (check.__doc__ or "").strip().splitlines()
            print(f"  ok    {check.__name__:<{width}}  {summary[0] if summary else ''}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {check.__name__:<{width}}  {e}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    sys.exit(1 if failed else 0)
