"""Coverage for lib/stageController.py's emergency-stop path.

StageController.__init__ opens a real Thorlabs serial connection, so these
tests build an instance with object.__new__() and stub only the methods that
touch hardware. Everything under test - arm/release/pending, abort, the
return and reference restore, the abort checks in move_by(),
wait_until_stopped() and homing - is the real class's own code.

    python -m unittest tests.test_stage_controller
"""
import threading
import types
import unittest

from lib.stageController import HomingAborted, StageController


def _bare(pos=10.0, homed=False, **attrs):
    """A StageController with __init__ (and its hardware) skipped."""
    ctrl = object.__new__(StageController)
    ctrl._lock = threading.RLock()
    ctrl.serial = "TEST"
    ctrl.travel_mm = 50.0
    ctrl._manual_reference = False
    ctrl._reference_end = "reverse"
    ctrl.return_point_mm = None
    ctrl.return_point_referenced = False
    ctrl._return_hold = False
    ctrl._return_until = 0.0
    ctrl._abort = threading.Event()
    ctrl._home_interrupted = False
    ctrl._home_frame_changed = False

    state = {"pos": pos, "homed": homed, "moves": [], "stops": []}

    def _move_by(scaled, scale=True):
        from lib.stageController import SCALE_FACTOR
        state["moves"].append(scaled * SCALE_FACTOR)
        state["pos"] += scaled * SCALE_FACTOR

    ctrl.stage = types.SimpleNamespace(
        move_by=_move_by,
        is_moving=lambda: False,
        is_homed=lambda: state["homed"],
        get_position=lambda scale=True: state["pos"] / __import__(
            "lib.stageController", fromlist=["SCALE_FACTOR"]).SCALE_FACTOR,
        stop=lambda immediate=False, sync=True: state["stops"].append(immediate),
        setup_velocity=lambda **k: None,
    )
    ctrl._retry_comm = lambda fn, what: fn()
    ctrl.get_status_list = lambda: []
    for k, v in attrs.items():
        setattr(ctrl, k, v)
    return ctrl, state


class ReturnPointTest(unittest.TestCase):
    def test_hold_is_pending_until_released(self):
        ctrl, _ = _bare()
        ctrl.arm_return_point(hold=True)
        self.assertTrue(ctrl.return_point_pending())
        ctrl.release_return_point()
        self.assertFalse(ctrl.return_point_pending())

    def test_a_jog_is_pending_only_inside_its_window(self):
        ctrl, _ = _bare()
        ctrl.arm_return_point(hold=False, expected_s=60.0)
        self.assertTrue(ctrl.return_point_pending())
        ctrl.arm_return_point(hold=False, expected_s=0.0)
        self.assertFalse(ctrl.return_point_pending())   # jog long finished

    def test_records_position_and_reference(self):
        ctrl, _ = _bare(pos=12.256, homed=True)
        ctrl.arm_return_point(hold=True)
        self.assertAlmostEqual(ctrl.return_point_mm, 12.256)
        self.assertTrue(ctrl.return_point_referenced)

    def test_move_by_does_not_touch_the_return_point(self):
        # A touch probe's own abort-retract must not become the point an
        # emergency stop returns to.
        ctrl, _ = _bare(pos=10.0)
        ctrl.arm_return_point(hold=True)
        ctrl.move_by(1.0)
        self.assertAlmostEqual(ctrl.return_point_mm, 10.0)


class AbortTest(unittest.TestCase):
    def test_abort_stops_abruptly_without_waiting(self):
        ctrl, state = _bare()
        ctrl.abort()
        self.assertTrue(ctrl._abort.is_set())
        self.assertEqual(state["stops"], [True])   # immediate=True

    def test_move_by_is_refused_while_aborting(self):
        ctrl, state = _bare()
        ctrl.abort()
        with self.assertRaises(RuntimeError):
            ctrl.move_by(1.0)
        self.assertEqual(state["moves"], [])

    def test_wait_until_stopped_gives_up_without_stopping_again(self):
        ctrl, state = _bare()
        ctrl.stage.is_moving = lambda: True
        ctrl.abort()
        state["stops"].clear()
        ok, reason = ctrl.wait_until_stopped(timeout_s=2.0)
        self.assertFalse(ok)
        self.assertIn("emergency stop", reason)
        self.assertEqual(state["stops"], [])

    def test_homing_is_not_started_while_aborting(self):
        ctrl, _ = _bare()
        ctrl.hw_error_count = lambda: 0
        ctrl._flush_comm = lambda: None
        started = []
        ctrl.stage.home = lambda sync=False, force=True: started.append(True)
        ctrl.abort()
        with self.assertRaises(HomingAborted):
            ctrl._home_once(timeout_s=5.0)
        self.assertEqual(started, [])

    def test_homing_in_progress_raises_homing_aborted(self):
        # Pressed after homing started: the poll loop must give up itself,
        # rather than wait out a stall timeout and call stop() over the
        # emergency stop's return move - which is what left Z short.
        ctrl, state = _bare()
        ctrl.hw_error_count = lambda: 0
        ctrl._flush_comm = lambda: None
        ctrl.stage.home = lambda sync=False, force=True: ctrl._abort.set()
        state["stops"].clear()
        with self.assertRaises(HomingAborted):
            ctrl._home_once(timeout_s=5.0)
        self.assertEqual(state["stops"], [])


class ReturnToPointTest(unittest.TestCase):
    def test_drives_back_to_the_return_point(self):
        ctrl, state = _bare(pos=11.786)
        ok, reason = ctrl.return_to_point(12.256, was_referenced=True)
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(state["pos"], 12.256, places=6)

    def test_refuses_while_still_aborting(self):
        ctrl, state = _bare(pos=11.0)
        ctrl.abort()
        ok, _ = ctrl.return_to_point(12.0, was_referenced=True)
        self.assertFalse(ok)
        self.assertEqual(state["moves"], [])

    def test_interrupted_home_gets_its_reference_back(self):
        # The case that was reported: Home All, emergency stop - the axis
        # must come back AND not be left showing '?'.
        ctrl, state = _bare(pos=11.786, homed=False)
        ctrl._home_interrupted = True
        ok, reason = ctrl.return_to_point(12.256, was_referenced=True)
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(state["pos"], 12.256, places=6)
        self.assertTrue(ctrl.is_referenced())
        self.assertIn("reference restored", reason)

    def test_no_reference_invented_if_there_was_none(self):
        ctrl, _ = _bare(pos=11.0, homed=False)
        ctrl._home_interrupted = True
        ctrl.return_to_point(12.0, was_referenced=False)
        self.assertFalse(ctrl.is_referenced())

    def test_left_alone_if_homing_reached_the_switch(self):
        # The controller may have re-zeroed: the return point means nothing
        # in the new frame, so do not move.
        ctrl, state = _bare(pos=0.3)
        ctrl._home_interrupted = True
        ctrl._home_frame_changed = True
        ok, reason = ctrl.return_to_point(12.256, was_referenced=True)
        self.assertFalse(ok)
        self.assertIn("home it again", reason)
        self.assertEqual(state["moves"], [])

    def test_refuses_an_implausible_return(self):
        ctrl, state = _bare(pos=10.0)
        ok, _ = ctrl.return_to_point(90.0, was_referenced=True)
        self.assertFalse(ok)
        self.assertEqual(state["moves"], [])

    def test_unknown_position_is_not_moved(self):
        ctrl, state = _bare()
        ctrl.get_position_mm = lambda: None
        ok, _ = ctrl.return_to_point(12.0, was_referenced=True)
        self.assertFalse(ok)
        self.assertEqual(state["moves"], [])


if __name__ == "__main__":
    unittest.main()
