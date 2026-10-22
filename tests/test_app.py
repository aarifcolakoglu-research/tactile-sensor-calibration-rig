"""Headless coverage for app/ATI.py, app/Display.py and app/theme.py.

The rig is stubbed out (fake StageController / K64F / Teensy /
TouchProbeRunner), so this needs PyQt6 but no hardware. It is skipped where
PyQt6 is missing. Run with:

    python -m unittest tests.test_app
"""
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    raise unittest.SkipTest("PyQt6 not available")

import lib.stageController as sc_mod


# --------------------------------------------------------------------------- #
# Hardware stubs
# --------------------------------------------------------------------------- #
class FakeStage:
    def __init__(self, *a, **k):
        self.travel_mm = 50.0
        self.serial = "FAKE"
        self.home_direction = "reverse"
        self._pos = 10.0
        self.moves = []
        self.marks = []
        self.stop_calls = 0
        self.abort_calls = 0
        self.return_calls = 0
        # Emergency-stop state, same names as StageController so the GUI
        # code under test reads it the same way.
        import threading
        self._abort = threading.Event()
        self.return_point_mm = None
        self.return_point_referenced = False
        self._return_hold = False

    # geometry / state
    def is_referenced(self):
        return True

    def is_homed(self):
        return True

    def get_position_mm(self):
        return self._pos

    def is_busy(self):
        return False

    def is_moving(self):
        return False

    def at_hard_limit(self):
        return False

    def set_home_direction(self, d):
        self.home_direction = d

    def check_move(self, distance_mm):
        target = self._pos + distance_mm
        ok = -0.5 <= target <= self.travel_mm - 0.5
        return sc_mod.MoveCheck(ok, True, self._pos, target, self.travel_mm,
                                "OK" if ok else "outside travel")

    # motion (record, don't move)
    def setup_velocity(self, *a, **k):
        pass

    def move_by(self, d):
        self.moves.append(d)
        self._pos += d

    def home(self, direction=None):
        self._pos = 0.0

    def stop(self):
        self.stop_calls += 1

    # Emergency stop: the same contract as StageController, minus hardware.
    # The real return/reference logic is covered in test_stage_controller.py.
    def arm_return_point(self, hold=False, expected_s=None):
        self.return_point_mm = self._pos
        self.return_point_referenced = True
        self._return_hold = hold or bool(expected_s)

    def release_return_point(self):
        self.return_point_mm = None
        self._return_hold = False

    def return_point_pending(self):
        return self.return_point_mm is not None and self._return_hold

    def abort(self):
        self.abort_calls += 1
        self._abort.set()

    def clear_abort(self):
        self._abort.clear()

    def return_to_point(self, target_mm, was_referenced):
        self.return_calls += 1
        if self._abort.is_set():
            return False, "aborted by emergency stop"
        self._pos = target_mm
        return True, f"returned to {self._pos:.3f} mm"

    def close(self):
        pass

    def snapshot(self):
        return types.SimpleNamespace(position_mm=self._pos, status=[])

    def __getattr__(self, name):
        return lambda *a, **k: None


class FakeK64F:
    connected = True
    data_stream = None

    def init_ethernet(self):
        self.connected = True

    def __getattr__(self, name):
        return lambda *a, **k: None


_STUBBED = False


def _install_stubs():
    global _STUBBED
    if _STUBBED:
        return
    sc_mod.StageController = FakeStage
    import app.Display as disp
    disp.K64F = FakeK64F
    disp.TeensyThread = lambda *a, **k: types.SimpleNamespace(
        start=lambda: None, stop=lambda: None)
    disp.find_teensy_port = lambda *a, **k: None

    # No one clicks OK in a headless test: a real QMessageBox.exec() would hang
    # forever. Make the modal helpers non-blocking with sensible answers.
    from PyQt6.QtWidgets import QMessageBox
    import app.ATI as m
    SB = QMessageBox.StandardButton
    m.QMessageBox.critical = staticmethod(lambda *a, **k: SB.Ok)
    m.QMessageBox.warning = staticmethod(lambda *a, **k: SB.Ok)
    m.QMessageBox.information = staticmethod(lambda *a, **k: SB.Ok)
    m.QMessageBox.question = staticmethod(lambda *a, **k: SB.Yes)

    # Display.show_ethernet_help builds a QMessageBox instance directly and
    # calls .exec() on it rather than going through the static helpers above,
    # so it needs its own patch or a headless run of anything that reaches it
    # (a failed Acquire, a failed Reconnect) hangs forever on a modal nobody
    # can dismiss.
    QMessageBox.exec = lambda self: SB.Ok
    _STUBBED = True


_APP = None
_WIN = None


def _mainwindow(tmp_cfg=None):
    """A constructed MainWindow with stubbed hardware."""
    global _APP
    _install_stubs()
    import app.theme as theme
    if tmp_cfg is not None:
        theme.SETTINGS_FILE = str(tmp_cfg)
    theme.load_settings()
    from app.ATI import MainWindow
    import queue
    _APP = _APP or QApplication.instance() or QApplication([])
    return MainWindow(queue.Queue(), queue.Queue())


def _shared_window():
    """One MainWindow reused across tests that only read from it or reset the
    handful of fields they touch — keeps the suite from building ~15 windows."""
    global _WIN
    if _WIN is None:
        _WIN = _mainwindow()
    return _WIN


def tearDownModule():
    # The panels run listener QThreads / QTimers; without stopping them the
    # interpreter can hang on exit.
    global _WIN
    if _WIN is not None:
        for panel in (_WIN.stage_panel, _WIN.sensor_panel):
            try:
                panel.shutdown()
            except Exception:
                pass
        _WIN = None


# --------------------------------------------------------------------------- #
class StepGrammarTest(unittest.TestCase):
    def setUp(self):
        from app.ATI import StagePanel
        self.SP = StagePanel

    def _roundtrip(self, step):
        line = self.SP._step_to_line(step)
        self.assertIsNotNone(line, step)
        back = self.SP._line_to_step(line)
        self.assertEqual(back, step, f"{line!r}")

    def test_every_step_type_round_trips(self):
        for step in [
            ("touch", {}),
            ("touch", {"retract_mm": 2.5}),
            ("record", {"label": "force"}),
            ("pause", 10.0),
            ("move", [("X_Stage", 0.5, 0.1)]),
            ("move", [("X_Stage", 0.5, 0.1), ("Y_Stage", 0.5, 0.2)]),
        ]:
            self._roundtrip(step)

    def test_describe_row_html_and_estimate_never_crash(self):
        for step in [
            ("touch", {"retract_mm": 2.0}), ("record", {"label": "x"}),
            ("pause", 3.0), ("move", [("Z_Stage", 0.5, 1.0)]),
        ]:
            title, detail = self.SP._describe(step)
            self.assertTrue(title and isinstance(title, str))
            html = self.SP._row_html(step)
            self.assertIn("<b>", html)
            est = self.SP._estimate_step_seconds(step)
            self.assertIsInstance(est, (int, float))
            self.assertGreaterEqual(est, 0)

    def test_loose_spellings_and_comments(self):
        self.assertIsNone(self.SP._line_to_step("# a comment"))
        self.assertIsNone(self.SP._line_to_step(""))

    def test_all_repo_sequence_files_parse(self):
        for d in ("sequences", "tests"):
            for f in (ROOT / d).glob("*.txt"):
                for i, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    s = raw.strip()
                    if not s or s.startswith("#"):
                        continue
                    self.assertIsNotNone(self.SP._line_to_step(raw),
                                         f"{f.name}:{i}  {raw!r}")


class TouchDataListenerTest(unittest.TestCase):
    def test_ft_window_and_latest(self):
        _APP2 = QApplication.instance() or QApplication([])
        from app.ATI import TouchDataListener
        import time
        L = TouchDataListener(None)
        now = time.monotonic()
        for i in range(30):
            L._buf.append((now - 0.5 + i * 0.02, 1000 + i,
                           0.0, 0.0, 1.0 + 0.01 * (i % 2), 0.0, 0.0, 0.0, 5.0, 3.0, -1.0))
        w = L.ft_window(1.0)
        self.assertGreater(w["n_samples"], 10)
        self.assertAlmostEqual(w["fz_N"], 1.005, places=2)
        self.assertGreater(w["fz_sd_N"], 0)
        self.assertEqual(w["pz_mm"], -1.0)
        self.assertIsNone(L.latest_sample())
        L._buf.clear()
        self.assertIsNone(L.ft_window(1.0))     # empty buffer -> None


class CoarseFindConfigTest(unittest.TestCase):
    """_coarse_find_config() sizes the search to what is actually left in
    front of the stage, unlike _touch_config()'s fixed envelope - and its
    timeout to that search,
    not the flat default that under-covered the fine pass (probe_timeout_s
    note in _touch_config)."""

    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.stage_panel
        # stageZ_controller is the one FakeStage shared across the whole
        # module (_shared_window()); every test here mutates _pos/travel_mm,
        # so restore both or a later test (PreflightTest in particular,
        # which assumes "each Z card starts at pos 10") sees the wrong stage.
        ctrl = self.sp.stageZ_controller
        orig_pos, orig_travel = ctrl._pos, ctrl.travel_mm
        self.addCleanup(lambda: (setattr(ctrl, "_pos", orig_pos),
                                 setattr(ctrl, "travel_mm", orig_travel)))

    def test_z_max_is_remaining_travel_from_current_position(self):
        # FakeStage: travel_mm=50.0, position=10.0, SAFETY_MARGIN_MM=0.5
        self.sp.stageZ_controller._pos = 10.0
        self.sp.stageZ_controller.travel_mm = 50.0
        cfg = self.sp._coarse_find_config()
        self.assertAlmostEqual(cfg.z_max_mm, 50.0 - 0.5 - 10.0)

    def test_z_max_never_goes_negative_near_the_limit(self):
        self.sp.stageZ_controller._pos = 49.9
        self.sp.stageZ_controller.travel_mm = 50.0
        cfg = self.sp._coarse_find_config()
        self.assertGreaterEqual(cfg.z_max_mm, 0.0)

    def test_timeout_covers_the_search_at_the_coarse_speed(self):
        self.sp.stageZ_controller._pos = 10.0
        self.sp.stageZ_controller.travel_mm = 50.0
        cfg = self.sp._coarse_find_config()
        needed = cfg.z_max_mm / cfg.approach_vel_mm_s
        self.assertGreater(cfg.probe_timeout_s, needed)

    def test_safety_guards_are_unchanged_from_the_fine_config(self):
        fine = self.sp._touch_config()
        coarse = self.sp._coarse_find_config()
        for field in ("f_touch_n", "f_abs_limit_n", "dFdt_limit_n_s",
                     "confirm_ms", "sample_stale_s", "overtravel_mm",
                     "retract_mm", "retract_emergency_mm"):
            self.assertEqual(getattr(coarse, field), getattr(fine, field), field)
        # Only the search itself differs from the fine pass.
        self.assertNotEqual(coarse.z_max_mm, fine.z_max_mm)
        self.assertGreater(coarse.approach_vel_mm_s, fine.approach_vel_mm_s)

    def test_falls_back_to_fine_envelope_when_position_is_unknown(self):
        # stageZ_controller is the one shared FakeStage instance for the whole
        # module (_shared_window()); restore it so later tests still see a
        # normal get_position_mm.
        original = self.sp.stageZ_controller.get_position_mm
        self.addCleanup(setattr, self.sp.stageZ_controller,
                        "get_position_mm", original)
        self.sp.stageZ_controller.get_position_mm = lambda: None
        cfg = self.sp._coarse_find_config()
        self.assertEqual(cfg.z_max_mm, self.sp._touch_config().z_max_mm)


class EmergencyStopTest(unittest.TestCase):
    """on_emergency_stop() wiring: every axis aborted at once, only the axes
    the interrupted command was moving are driven back, Home All does not
    carry on to the next axis, and a second press cancels the return. The
    return/reference logic itself is covered in tests/test_stage_controller.py
    against the real StageController."""

    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.stage_panel
        self.axes = (self.sp.stageX_controller, self.sp.stageY_controller,
                     self.sp.stageZ_controller)
        for ctrl in self.axes:
            ctrl.abort_calls = ctrl.return_calls = 0
            ctrl.release_return_point()
            ctrl.clear_abort()
            orig = ctrl._pos
            self.addCleanup(setattr, ctrl, "_pos", orig)
            self.addCleanup(ctrl.release_return_point)
            self.addCleanup(ctrl.clear_abort)
        self.sp._touch_abort.clear()
        self.addCleanup(self.sp._touch_abort.clear)
        self.addCleanup(setattr, self.sp, "sequence_active", False)
        # Drain every test's background return before the next test - in this
        # class or any other sharing _shared_window() - can see its state.
        self.addCleanup(self._drain)

    def _pump_until(self, predicate, timeout_s=3.0):
        import time
        end = time.perf_counter() + timeout_s
        while time.perf_counter() < end:
            QApplication.processEvents()
            if predicate():
                return True
        return False

    def _drain(self):
        self._pump_until(lambda: not self.sp._estop_running)
        t = self.sp._estop_thread
        if t is not None:
            t.join(2)
        QApplication.processEvents()

    def test_every_axis_is_aborted_at_once(self):
        self.sp.on_emergency_stop()
        for ctrl in self.axes:
            self.assertEqual(ctrl.abort_calls, 1)

    def test_only_the_axis_that_was_moving_is_returned(self):
        z = self.sp.stageZ_controller
        z.arm_return_point(hold=True)       # e.g. Z mid-home
        start = z._pos
        z._pos = start - 0.47               # it got this far
        self.sp.on_emergency_stop()
        self.assertTrue(self._pump_until(lambda: not self.sp._estop_running))
        self.assertAlmostEqual(z._pos, start)
        self.assertEqual(z.return_calls, 1)
        self.assertEqual(self.sp.stageX_controller.return_calls, 0)
        self.assertEqual(self.sp.stageY_controller.return_calls, 0)

    def test_idle_press_moves_nothing(self):
        self.sp.on_emergency_stop()
        self._pump_until(lambda: not self.sp._estop_running)
        self.assertEqual(sum(c.return_calls for c in self.axes), 0)

    def test_aborts_are_cleared_and_return_points_released_afterwards(self):
        self.sp.stageZ_controller.arm_return_point(hold=True)
        self.sp.on_emergency_stop()
        self._pump_until(lambda: not self.sp._estop_running)
        for ctrl in self.axes:
            self.assertFalse(ctrl._abort.is_set())
            self.assertIsNone(ctrl.return_point_mm)

    def test_stops_an_active_sequence_and_the_touch_probe(self):
        self.sp.sequence_active = True
        self.sp.on_emergency_stop()
        self.assertFalse(self.sp.sequence_active)
        self.assertTrue(self.sp._touch_abort.is_set())

    def test_controls_disabled_until_the_return_is_done(self):
        self.sp.on_emergency_stop()
        self._pump_until(lambda: not self.sp._estop_running)
        QApplication.processEvents()
        self.assertTrue(self.sp.emergency_stop_button.isEnabled())
        self.assertTrue(self.sp.home_all_button.isEnabled())

    def test_second_press_during_the_return_cancels_it(self):
        import threading
        z = self.sp.stageZ_controller
        z.arm_return_point(hold=True)
        target = z._pos
        z._pos = target - 1.0

        # Hold the worker in its join phase so the second press lands during
        # the return, deterministically.
        gate = threading.Event()
        orig_join = self.sp._join_motion_workers
        self.sp._join_motion_workers = lambda *a, **k: gate.wait(2)
        self.addCleanup(setattr, self.sp, "_join_motion_workers", orig_join)

        self.sp.on_emergency_stop()
        self.sp.on_emergency_stop()          # second press
        gate.set()
        self.assertTrue(self._pump_until(lambda: not self.sp._estop_running))
        self.assertAlmostEqual(z._pos, target - 1.0)   # left where it stopped
        self.assertEqual(z.return_calls, 0)
        self.assertFalse(z._abort.is_set())            # and not left locked out

    def test_home_all_does_not_carry_on_to_the_next_axis(self):
        import app.ATI as m
        homed = []
        z, x, y = (self.sp.stageZ_controller, self.sp.stageX_controller,
                   self.sp.stageY_controller)

        def z_home(direction=None):
            homed.append("Z")
            self.sp._estop_generation += 1   # an emergency stop, from the
            raise m.HomingAborted("interrupted")  # worker's point of view

        z.home = z_home
        x.home = lambda direction=None: homed.append("X")
        y.home = lambda direction=None: homed.append("Y")
        for c in (z, x, y):
            self.addCleanup(lambda c=c: c.__dict__.pop("home", None))

        # home_all() prints the axis's switch as a Unicode minus. In the app,
        # stdout is lib.applog's UTF-8 _Tee; under a bare test runner on a
        # cp1252 console that print raises, and the worker reads it as a
        # homing failure - so capture output the way the app would.
        import io
        import sys
        orig_stdout = sys.stdout
        sys.stdout = io.StringIO()
        self.addCleanup(setattr, sys, "stdout", orig_stdout)

        self.sp.home_all()
        self.sp._home_all_thread.join(3)
        QApplication.processEvents()
        self.assertEqual(homed, ["Z"])


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.stage_panel
        self.sp.latest_time_us = 1

    def test_clean_move_sequence_passes(self):
        self.assertTrue(self.sp._preflight_sequence([("move", [("Z_Stage", 0.5, 1.0)])]))

    def test_travel_limit_blocks(self):
        # each Z card starts at pos 10; +45 would exceed 49.5
        self.assertFalse(self.sp._preflight_sequence([("move", [("Z_Stage", 0.5, 45.0)])]))

    def test_touch_or_record_without_sensor_data_blocks(self):
        self.sp.latest_time_us = None
        self.assertFalse(self.sp._preflight_sequence([("touch", {})]))
        self.assertFalse(self.sp._preflight_sequence([("record", {"label": "x"})]))

    def test_pause_touch_record_are_skipped_in_travel_check(self):
        self.sp.latest_time_us = 1        # sensor data present
        steps = [("pause", 1.0),
                 ("touch", {}), ("record", {"label": "x"})]
        self.assertTrue(self.sp._preflight_sequence(steps))


class RunStepTest(unittest.TestCase):
    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.stage_panel
        self.sp.latest_time_us = 1
        self.sp._cal_log = None

    def _cont(self):
        return True

    def test_record_step_writes_a_cal_row(self):
        import glob, os
        self.sp._open_cal_log("unit")
        path = self.sp._cal_log_path
        self.sp.touch_listener.ft_window = lambda w: {
            "n_samples": 12, "window_s": w, "fx_N": 0.0, "fy_N": 0.0, "fz_N": 2.5,
            "tx_Nmm": 0.0, "ty_Nmm": 0.0, "tz_Nmm": 0.0, "fz_sd_N": 0.004,
            "px_mm": 1.0, "py_mm": 2.0, "pz_mm": -3.0}
        ok, msg = self.sp._run_record_step({"label": "force", "window_s": 0.02},
                                           self._cont, step_index=4)
        self.sp._close_cal_log()
        self.assertTrue(ok, msg)
        text = Path(path).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#"))
        self.assertIn("iso_time,seq_step,label", text)
        self.assertIn(",force,", text)
        self.assertNotIn("kpa", text.splitlines()[1])   # stage-only columns
        os.remove(path)

    def test_touch_step_success(self):
        import app.ATI as m

        class Runner:
            def __init__(self, **k):
                pass

            def run(self):
                return types.SimpleNamespace(ok=True, touch_pz=-1.234,
                                             reason="OK", fz0=0.0, fz_touch=0.02)

        old = m.TouchProbeRunner
        m.TouchProbeRunner = Runner
        try:
            ok, msg = self.sp._run_touch_step({}, self._cont, step_index=1)
        finally:
            m.TouchProbeRunner = old
        self.assertTrue(ok, msg)
        self.assertAlmostEqual(self.sp.touch_pz, -1.234)

    def test_touch_step_fail_then_operator_skip(self):
        import app.ATI as m

        class Runner:
            def __init__(self, **k):
                pass

            def run(self):
                return types.SimpleNamespace(ok=False, touch_pz=None,
                                             reason="Reached Z_MAX without touch",
                                             fz0=0.0, fz_touch=None)

        old_runner = m.TouchProbeRunner
        old_retries = m.TOUCH_STEP_RETRIES
        m.TouchProbeRunner = Runner
        m.TOUCH_STEP_RETRIES = 0            # straight to the operator prompt
        # answer the prompt with "skip" without a real dialog
        self.sp._ask_touch_operator = lambda reason, cont: "skip"
        try:
            ok, msg = self.sp._run_touch_step({}, self._cont, step_index=2)
        finally:
            m.TouchProbeRunner = old_runner
            m.TOUCH_STEP_RETRIES = old_retries
        self.assertTrue(ok)               # skip -> sequence continues
        self.assertIn("skipped", msg)


class CalLogTest(unittest.TestCase):
    def test_legend_header_and_pandas_readable(self):
        w = _shared_window()
        sp = w.stage_panel
        sp._open_cal_log("callog")
        sp._write_cal_row("touch", 3, ft={"n_samples": 10, "window_s": 1.0,
                                          "fx_N": 0.1, "fy_N": 0.2, "fz_N": 1.5,
                                          "tx_Nmm": 0.0, "ty_Nmm": 0.0, "tz_Nmm": 0.0,
                                          "fz_sd_N": 0.003, "px_mm": 1.0, "py_mm": 2.0,
                                          "pz_mm": -1.0},
                          touch_pz=-1.0, touch_ok=True, touch_note="attempt 1")
        path = sp._cal_log_path
        sp._close_cal_log()
        try:
            import pandas as pd
            df = pd.read_csv(path, comment="#")
            self.assertEqual(list(df.columns)[:4],
                             ["iso_time", "seq_step", "label", "n_samples"])
            self.assertFalse([c for c in df.columns if "kpa" in c])
            self.assertEqual(df.iloc[0]["label"], "touch")
            self.assertAlmostEqual(float(df.iloc[0]["fz_N"]), 1.5)
        finally:
            import os
            os.remove(path)


class ThemeAndLayoutTest(unittest.TestCase):
    def test_settings_round_trip(self):
        import tempfile, os
        import app.theme as theme
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            theme.SETTINGS_FILE = p
            theme.set_mode("dark")
            theme.set_scale(0.85)
            theme.SHOW_READOUT = False
            theme.CHART_CHANNELS = ["fx", "fz", "pz"]
            theme.WINDOW_STATE = "abc123"
            theme.save_settings()
            # reset then reload
            theme.MODE, theme.SCALE = "light", 1.0
            theme.CHART_CHANNELS = []
            theme.load_settings()
            self.assertEqual(theme.MODE, "dark")
            self.assertEqual(theme.SCALE, 0.85)
            self.assertFalse(theme.SHOW_READOUT)
            self.assertEqual(theme.CHART_CHANNELS, ["fx", "fz", "pz"])
            self.assertEqual(theme.WINDOW_STATE, "abc123")
        finally:
            os.remove(p)

    def test_fresh_defaults(self):
        import app.theme as theme
        self.assertEqual(theme.DEFAULT_MODE, "dark")
        self.assertEqual(theme.DEFAULT_SCALE, 0.85)

    def test_three_docks_and_state_round_trip(self):
        w = _shared_window()
        self.assertEqual([d.objectName() for d in w._docks],
                         ["dock_stages", "dock_sequence", "dock_sensor"])
        w._save_window_geometry()
        import app.theme as theme
        self.assertTrue(theme.WINDOW_STATE)
        self.assertTrue(w._restore_dock_state())
        w.reset_window_layout()   # must not raise

    def test_dialog_parent_is_the_window(self):
        w = _shared_window()
        self.assertIs(w.stage_panel._dialog_parent(), w)

    def test_x_axis_homes_forward_by_default(self):
        w = _shared_window()
        dirs = {c.stage_name: c.stage_controller.home_direction
                for c in w.stage_panel.stage_controls}
        self.assertEqual(dirs["X_Stage"], "forward")
        self.assertEqual(dirs["Y_Stage"], "reverse")
        self.assertEqual(dirs["Z_Stage"], "reverse")


def _long_sequence_file(n=683):
    """A sequence of n stage steps (moves, pauses, touches, records) - the
    size of the longest calibration file used on this rig."""
    import tempfile, os
    lines = ["# synthetic long sequence"]
    kinds = ["Z_Stage Velocity: 0.1, Distance: 0.1", "Pause Time: 1",
             "Touch Point retract 3.5", "Record Point p_{i}",
             "Combined X_Stage:0.5,0.1 Y_Stage:0.5,0.2"]
    for i in range(n):
        lines.append(kinds[i % len(kinds)].format(i=i))
    d = tempfile.mkdtemp()
    path = os.path.join(d, "long.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return Path(path)


class SequenceLoadAndLockTest(unittest.TestCase):
    """Loading must not freeze the window, and a running sequence's list
    must not be edited under it (29 Sep 2026)."""

    RUN3 = _long_sequence_file()

    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.stage_panel
        self.addCleanup(self.sp.motion_list.clear)
        self.addCleanup(setattr, self.sp, "sequence_active", False)
        self.addCleanup(setattr, self.sp, "_running_step", -1)

    def test_run04_loads_in_well_under_a_second_per_hundred_steps(self):
        # It took ~42 s - the whole window frozen, EMERGENCY STOP included.
        import time
        t0 = time.perf_counter()
        self.sp.load_sequence_file(str(self.RUN3))
        took = time.perf_counter() - t0
        self.assertEqual(self.sp.motion_list.count(), 683)
        self.assertLess(took, 3.0)

    def test_loaded_steps_are_the_files_steps(self):
        from app.ATI import StagePanel as SP
        self.sp.load_sequence_file(str(self.RUN3))
        lines = self.RUN3.read_text(encoding="utf-8").splitlines()
        want = [s for s in (SP._line_to_step(l) for l in lines) if s is not None]
        self.assertEqual(self.sp.steps(), want)

    def test_a_file_saved_in_the_windows_default_encoding_still_loads(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        p = os.path.join(d, "legacy.txt")
        with open(p, "w", encoding="cp1252") as fh:
            fh.write("Record Point café µm\nPause Time: 2\n")
        self.sp.load_sequence_file(p)
        self.assertEqual(self.sp.motion_list.count(), 2)
        self.assertIn("café µm", str(self.sp.steps()[0]))

    def test_the_list_cannot_be_edited_while_a_sequence_runs(self):
        self.sp.load_sequence_file(str(ROOT / "sequences" / "C_Z1p8_H10_C1_R20.txt"))
        before = self.sp.steps()
        self.sp.sequence_active = True
        self.sp.add_step(("pause", 1.0))
        self.sp._rebuild_rows(before[:3])
        self.sp.clear_sequence()
        self.sp.load_sequence()            # refused before any file dialog opens
        self.assertEqual(self.sp.steps(), before)

    def test_edit_buttons_are_greyed_during_a_run_and_back_after(self):
        self.sp._set_sequence_editable(False)
        self.assertFalse(self.sp.load_sequence_button.isEnabled())
        self.assertTrue(self.sp.save_sequence_button.isEnabled())
        self.sp._set_sequence_editable(True)
        self.assertTrue(self.sp.load_sequence_button.isEnabled())

    def test_progress_past_the_end_of_the_list_does_not_raise(self):
        # An exception in a PyQt6 slot aborts the whole application.
        self.sp.motion_list.clear()
        self.sp._on_sequence_progress(40)

    def test_the_pulse_only_restyles_rows_that_changed(self):
        self.sp.load_sequence_file(str(self.RUN3))
        self.sp._running_step = 10
        self.sp._pulse_on = True
        self.sp._restyle_seq_rows()
        calls = []
        for i in range(self.sp.motion_list.count()):
            w = self.sp.motion_list.itemWidget(self.sp.motion_list.item(i))
            if w is None:          # rows out of view have no widget yet
                continue
            orig = w.setStyleSheet
            w.setStyleSheet = (lambda s, o=orig: (calls.append(1), o(s)))
        self.sp._pulse_tick()
        self.assertEqual(len(calls), 1)    # just the running row's blink


class SequenceListTest(unittest.TestCase):
    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.stage_panel
        self.sp.motion_list.clear()
        for i in range(4):
            self.sp.add_step(("pause", float(i)))

    def _secs(self):
        return [s[1] for s in self.sp.steps()]

    def _row_css(self, i):
        return self.sp.motion_list.itemWidget(self.sp.motion_list.item(i)).styleSheet()

    def test_rows_actually_paint_their_stylesheet(self):
        # regression: a plain QWidget subclass drops stylesheet background and
        # border unless WA_StyledBackground is set, which made every highlight
        # _restyle_seq_rows() computes a silent no-op
        from PyQt6.QtCore import Qt
        for i in range(self.sp.motion_list.count()):
            w = self.sp.motion_list.itemWidget(self.sp.motion_list.item(i))
            self.assertTrue(
                w.testAttribute(Qt.WidgetAttribute.WA_StyledBackground),
                f"row {i} would not paint its highlight")

    def test_running_step_is_highlighted(self):
        import app.theme as theme
        self.sp.motion_list.clearSelection()
        self.sp._running_step = 2
        self.sp._pulse_on = True
        self.sp._restyle_seq_rows()
        self.assertIn(theme.ACCENT, self._row_css(2))          # accent bar
        self.assertIn(theme.ACCENT_SOFT, self._row_css(2))     # lit tint
        for other in (0, 1, 3):
            self.assertIn("transparent", self._row_css(other))
        # the tint blinks, the bar stays on
        self.sp._pulse_on = False
        self.sp._restyle_seq_rows()
        self.assertIn(theme.ACCENT, self._row_css(2))
        self.assertNotIn(theme.ACCENT_SOFT, self._row_css(2))
        self.sp._running_step = -1

    def test_selected_rows_are_tinted(self):
        import app.theme as theme
        self.sp._running_step = -1
        for r in (1, 3):
            self.sp.motion_list.item(r).setSelected(True)
        self.sp._restyle_seq_rows()
        for r in (1, 3):
            self.assertIn(theme.ACCENT_DARK, self._row_css(r))
        self.assertIn("transparent", self._row_css(0))

    def test_insert_after_selected_row(self):
        self.sp.motion_list.setCurrentRow(1)
        self.sp._insert_step(("pause", 99.0))
        self.assertEqual(self._secs(), [0.0, 1.0, 99.0, 2.0, 3.0])
        self.assertEqual(self.sp.motion_list.currentRow(), 2)

    def test_insert_appends_when_nothing_selected(self):
        self.sp.motion_list.clearSelection()
        self.sp.motion_list.setCurrentRow(-1)
        self.sp._insert_step(("pause", 99.0))
        self.assertEqual(self._secs()[-1], 99.0)

    def test_remove_selected_block(self):
        for r in (0, 2):
            self.sp.motion_list.item(r).setSelected(True)
        self.sp._remove_selected()
        self.assertEqual(self._secs(), [1.0, 3.0])

    def test_move_selected_block_down(self):
        for r in (0, 1):
            self.sp.motion_list.item(r).setSelected(True)
        self.sp.move_step(1)
        self.assertEqual(self._secs(), [2.0, 0.0, 1.0, 3.0])


class ChartChannelsTest(unittest.TestCase):
    def test_fifteen_series_ft_default_visible(self):
        w = _shared_window()
        sp = w.sensor_panel
        self.assertEqual(len(sp.chart_series), 15)
        vis = {k for k, s in sp.chart_series.items() if s.isVisible()}
        self.assertEqual(vis, {"fx", "fy", "fz", "tx", "ty", "tz"})

    def test_toggle_channel_persists_and_updates_series(self):
        import tempfile, os
        import app.theme as theme
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        theme.SETTINGS_FILE = p
        try:
            w = _shared_window()
            sp = w.sensor_panel
            sp.channel_checks["pz"].setChecked(True)
            sp._toggle_channel("pz", True)
            self.assertTrue(sp.chart_series["pz"].isVisible())
            self.assertIn("pz", theme.CHART_CHANNELS)
            self.assertIn("pz", json.loads(Path(p).read_text())["chart_channels"])
        finally:
            os.remove(p)


class _SlowFakeK64F:
    """A K64F stand-in whose network calls take a controllable, real amount
    of wall-clock time - the point is to prove the GUI thread is free to keep
    running an event loop while they are in flight, which a truly synchronous
    fake (returning instantly) could never demonstrate either way."""

    def __init__(self, delay=0.35, start_ok=True, reconnect_ok=True):
        self.connected = True
        self.data_stream = None
        self._delay = delay
        self._start_ok = start_ok
        self._reconnect_ok = reconnect_ok
        self.calls = []

    def start_acquisition(self):
        import time
        self.calls.append("start_acquisition")
        time.sleep(self._delay)
        if self._start_ok:
            self.data_stream = types.SimpleNamespace(
                data_received=types.SimpleNamespace(
                    connect=lambda *a, **k: None,
                    disconnect=lambda *a, **k: None))

    def stop_acquisition(self):
        self.calls.append("stop_acquisition")

    def reconnect(self):
        import time
        self.calls.append("reconnect")
        time.sleep(self._delay)
        self.connected = self._reconnect_ok
        return self._reconnect_ok

    def init_ethernet(self):
        pass


class AcquisitionThreadingTest(unittest.TestCase):
    """Pressing Acquire or Reconnect against a slow/dead link used to freeze
    the whole application: K64F.start_acquisition() carries a bounded
    reconnect-with-backoff that measured ~8 s against a genuinely dead link,
    and K64F.reconnect() up to ~17 s - both ran to completion before the
    method that called them, on the GUI thread, could return. See
    app/Display.py's _K64FCall for the fix: same K64F calls, same messages,
    only the thread that invokes them changes.
    """

    def setUp(self):
        self.w = _shared_window()
        self.sp = self.w.sensor_panel
        self._orig_K64F = self.sp.K64F
        self._orig_active = self.sp.acquisition_active
        self.sp.acquisition_active = False
        self.sp.acquire_button.setChecked(False)
        self.sp.acquire_button.setEnabled(True)
        self.sp.reconnect_button.setEnabled(True)
        self.sp._k64f_worker = None

    def tearDown(self):
        # Never leave a worker thread - or its queued completion signal -
        # running past its test: wait for the OS thread, then keep pumping
        # events until the completion handler has actually cleared it (or
        # give up after a bound), so a leftover queued signal cannot land on
        # the NEXT test at a random moment instead.
        w = self.sp._k64f_worker
        if w is not None:
            w.wait(2000)
            self._pump_until(lambda: not self.sp._k64f_worker, timeout_s=1.0)
        self.sp.K64F = self._orig_K64F
        self.sp.acquisition_active = self._orig_active
        self.sp.acquire_button.setChecked(False)
        self.sp.acquire_button.setEnabled(True)
        self.sp.reconnect_button.setEnabled(True)
        self.sp._k64f_worker = None
        self.sp.watchdog_timer.stop()

    def _pump_until(self, predicate, timeout_s=3.0):
        import time
        end = time.perf_counter() + timeout_s
        while time.perf_counter() < end:
            QApplication.processEvents()
            if predicate():
                return True
        return False

    def test_start_acquisition_returns_immediately_and_keeps_the_gui_alive(self):
        import time
        from PyQt6.QtCore import QTimer
        fake = _SlowFakeK64F(delay=0.4, start_ok=True)
        self.sp.K64F = fake

        ticks = {"n": 0}
        probe = QTimer()
        probe.setInterval(5)
        probe.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
        probe.start()

        t0 = time.perf_counter()
        self.sp.acquire_button.setChecked(True)   # what a real click leaves behind
        self.sp.start_acquisition()
        call_took = time.perf_counter() - t0

        # The call must return long before the fake's own 0.4 s delay - proof
        # the network call is not running on this thread.
        self.assertLess(call_took, 0.15, "start_acquisition() blocked the caller")
        self.assertFalse(self.sp.acquire_button.isEnabled())
        self.assertFalse(self.sp.reconnect_button.isEnabled())

        ok = self._pump_until(lambda: self.sp.acquisition_active)
        probe.stop()

        self.assertTrue(ok, "acquisition never completed")
        # The event loop kept running throughout the ~0.4 s wait - a frozen
        # GUI thread would have delivered close to zero of these.
        self.assertGreater(ticks["n"], 10)
        self.assertEqual(fake.calls, ["start_acquisition"])
        self.assertTrue(self.sp.acquire_button.isEnabled())
        self.assertTrue(self.sp.reconnect_button.isEnabled())
        self.assertEqual(self.sp.acquire_button.text(), "Stop")

    def test_start_acquisition_failure_path_matches_before(self):
        """data_stream stays None (box accepted the connection but would not
        stream) - the same failure this always handled, just reached after a
        background wait instead of a blocking one."""
        fake = _SlowFakeK64F(delay=0.05, start_ok=False)
        self.sp.K64F = fake
        self.sp.acquire_button.setChecked(True)
        self.sp.start_acquisition()

        ok = self._pump_until(lambda: not self.sp._k64f_worker)
        self.assertTrue(ok)
        self.assertFalse(self.sp.acquisition_active)
        self.assertFalse(self.sp.acquire_button.isChecked())
        self.assertTrue(self.sp.acquire_button.isEnabled())

    def test_reconnect_returns_immediately_and_keeps_the_gui_alive(self):
        import time
        from PyQt6.QtCore import QTimer
        fake = _SlowFakeK64F(delay=0.4)
        fake.connected = False
        self.sp.K64F = fake

        ticks = {"n": 0}
        probe = QTimer(); probe.setInterval(5)
        probe.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
        probe.start()

        t0 = time.perf_counter()
        self.sp.reconnect_ethernet()
        call_took = time.perf_counter() - t0
        self.assertLess(call_took, 0.15, "reconnect_ethernet() blocked the caller")
        self.assertFalse(self.sp.reconnect_button.isEnabled())
        self.assertFalse(self.sp.acquire_button.isEnabled())

        ok = self._pump_until(lambda: not self.sp._k64f_worker)
        probe.stop()

        self.assertTrue(ok, "reconnect never completed")
        self.assertGreater(ticks["n"], 10)
        self.assertEqual(fake.calls, ["reconnect"])
        self.assertTrue(self.sp.reconnect_button.isEnabled())
        self.assertTrue(self.sp.acquire_button.isEnabled())

    def test_second_worker_is_refused_while_one_is_in_flight(self):
        """The mutual-exclusion guard: a K64F call in flight is never joined
        by a second one on the same object from a different button."""
        fake = _SlowFakeK64F(delay=0.3)
        self.sp.K64F = fake
        self.sp.acquire_button.setChecked(True)
        self.sp.start_acquisition()
        self.assertIsNotNone(self.sp._k64f_worker)

        # Buttons are disabled, so a real click could not reach this handler;
        # call it directly to prove the guard holds even so.
        self.sp.reconnect_ethernet()

        ok = self._pump_until(lambda: not self.sp._k64f_worker)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, ["start_acquisition"],
                         "a second K64F call ran while the first was in flight")


if __name__ == "__main__":
    unittest.main()


class StreamClockWrapTest(unittest.TestCase):
    """The K64F clock is a uint32 in us and wraps every 71.6 min. SK1_S0_R1
    (135 min, 30 Sep 2026) was the first run past it: t_rel_us jumped back
    and dt_us went hugely negative once. Both must run straight through."""

    def test_u32_delta_crosses_the_wrap(self):
        from lib.K64F import u32_delta_us
        self.assertEqual(u32_delta_us(5_000, 1_000), 4_000)
        self.assertEqual(u32_delta_us(4_000, 2**32 - 6_000), 10_000)

    def test_stream_csv_t_rel_is_continuous_across_the_wrap(self):
        import csv, struct, tempfile
        import lib.K64F as k
        start = 2**32 - 25_000
        times = [(start + i * 10_000) % 2**32 for i in range(6)]   # wraps after 2

        class FakeSock:
            def __init__(self):
                self.packets = [struct.pack("<I", t) + struct.pack("<fffffffff", *[0.0] * 9)
                                for t in times]
            def recv(self, n):
                return self.packets.pop(0) if self.packets else b""

        with tempfile.TemporaryDirectory() as d:
            old = k.paths.OUTPUT_DIR
            k.paths.OUTPUT_DIR = d
            try:
                ds = k.DataStream(FakeSock())
                ds.sequence_name = "WRAP_S0_R1"
                ds.logging_flag = True
                ds.run()
            finally:
                k.paths.OUTPUT_DIR = old
            f = next(Path(d).glob("WRAP_S0_R1_stream_*.csv"))
            with open(f, newline="") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual([int(r["t_rel_us"]) for r in rows], [0, 10_000, 20_000, 30_000, 40_000, 50_000])
        self.assertEqual([int(r["dt_us"]) for r in rows], [0] + [10_000] * 5)
        self.assertEqual([int(r["device_time_us"]) for r in rows], times)   # raw, as sent


class EthernetReconnectTest(unittest.TestCase):
    """1 Oct 2026: the K64F was still booting, the first connect timed out and
    attempts 2-6 all failed at once with WinError 10022 - the same socket was
    reused after a failed connect(). Each attempt must get a fresh socket."""

    def test_each_attempt_uses_a_fresh_socket(self):
        import lib.K64F as k
        made = []

        class FakeSocket:
            def __init__(self, *a):
                self.tried = False
                made.append(self)
            def setsockopt(self, *a): pass
            def settimeout(self, *a): pass
            def shutdown(self, *a): pass
            def close(self): pass
            def connect(self, addr):
                if self.tried:
                    raise OSError(10022, "An invalid argument was supplied")
                self.tried = True
                if len(made) < 3:
                    raise TimeoutError("timed out")

        old_socket = k.socket.socket
        k.socket.socket = FakeSocket
        try:
            dev = k.K64F()
            dev.invoke_fid = lambda *a: None
            self.assertTrue(dev.reconnect(retries=4, backoff_s=0))
        finally:
            k.socket.socket = old_socket
        self.assertTrue(dev.connected)
        self.assertTrue(all(s.tried for s in made[1:]))   # no socket asked twice


class ToolbarWrapTest(unittest.TestCase):
    """6 Oct 2026: the stage-steps row wraps onto a second line instead of
    holding the window's minimum width."""

    def test_rows_wrap_when_narrow(self):
        _APP2 = QApplication.instance() or QApplication([])
        from app.ATI import FlowLayout
        from PyQt6.QtWidgets import QWidget, QPushButton, QLabel
        host = QWidget()
        lay = FlowLayout(host, spacing=5)
        lay.addWidget(QLabel("Other:"))
        for t in ("+ Combined", "+ Pause", "+ Touch", "+ Record", "Clear", "Move up"):
            lay.addWidget(QPushButton(t))
        wide, narrow = lay.heightForWidth(2000), lay.heightForWidth(250)
        self.assertGreater(narrow, wide)                    # wrapped onto more lines
        widest = max(lay.itemAt(i).sizeHint().width() for i in range(lay.count()))
        self.assertLessEqual(lay.minimumSize().width(), widest)   # never the whole row

    def test_stages_caption_is_there(self):
        from PyQt6.QtWidgets import QLabel
        texts = [l.text() for l in _shared_window().findChildren(QLabel)]
        self.assertIn("Stages:", texts)


class LazyRowsTest(unittest.TestCase):
    """7 Oct 2026: loading a 600-step file still froze the graph on the lab
    PC - one row widget per step, all built at once. Only the rows in view
    are built now; the rest as they scroll into view."""

    def test_load_builds_few_widgets_and_keeps_every_step(self):
        import time
        w = _shared_window(); sp = w.stage_panel
        self.addCleanup(sp.motion_list.clear)
        p = _long_sequence_file()
        t0 = time.perf_counter()
        sp.load_sequence_file(str(p))
        took = time.perf_counter() - t0
        ml = sp.motion_list
        self.assertEqual(ml.count(), 683)
        built = sum(1 for i in range(ml.count()) if ml.itemWidget(ml.item(i)) is not None)
        self.assertLess(built, 120)
        self.assertLess(took, 0.5)
        ml.scrollToItem(ml.item(600))
        QApplication.processEvents()
        self.assertIsNotNone(ml.itemWidget(ml.item(600)))


class HangingIndentTest(unittest.TestCase):
    """7 Oct 2026: wrapped buttons start under the first button, not under
    the 'Stages:' / 'Other:' caption, and both rows start on the same line."""

    def test_wrapped_lines_start_after_the_caption(self):
        _APP2 = QApplication.instance() or QApplication([])
        from app.ATI import FlowLayout
        from PyQt6.QtWidgets import QWidget, QPushButton, QLabel
        from PyQt6.QtCore import QRect
        host = QWidget()
        lay = FlowLayout(host, spacing=5, hang=True)
        cap = QLabel("Stages:")
        lay.addWidget(cap)
        buttons = [QPushButton(t) for t in ("+ Combined", "+ Pause", "+ Touch", "+ Record", "Clear")]
        for b in buttons:
            lay.addWidget(b)
        lay.setGeometry(QRect(0, 0, 260, lay.heightForWidth(260)))
        first_x = buttons[0].geometry().x()
        rows = {}
        for b in buttons:
            rows.setdefault(b.geometry().y(), []).append(b.geometry().x())
        self.assertGreater(len(rows), 1)                      # it did wrap
        for y, xs in rows.items():
            self.assertEqual(min(xs), first_x, y)             # every line starts there

