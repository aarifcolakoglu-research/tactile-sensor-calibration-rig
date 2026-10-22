import ctypes
import json
import os
import queue
import re
import sys
import threading
import time

from PyQt6.QtGui import QGuiApplication, QAction, QActionGroup, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QSizePolicy,
    QInputDialog, QMessageBox, QFileDialog, QGroupBox, QSplitter,
    QCheckBox, QDialog, QDoubleSpinBox, QComboBox, QDialogButtonBox, QFormLayout,
    QProgressBar, QMenu
)
from PyQt6.QtCore import pyqtSignal, QThread, QTimer, QSize, Qt, QByteArray

from lib import applog, paths
from lib.set_touch_point import TouchConfig, TouchProbeRunner
from lib.stageController import (
    StageController,
    wait_until_all_stopped,
    MAX_VELOCITY_MM_S,
    RECOMMENDED_VELOCITY_MM_S,
    RECOMMENDED_DISTANCE_MM,
    SAFETY_MARGIN_MM,
    HOME_TOLERANCE_MM,
    HOME_DIRECTION,
)
from . import theme
from .Display import SensorPanel

STAGE_X_ID = "27253425"
STAGE_Y_ID = "27253326"
STAGE_Z_ID = "27253356"

# Largest single move allowed while a stage is NOT homed (position unknown).
UNHOMED_JOG_LIMIT_MM = 2.0

# Homing order: Z first (Z- lifts the probe away from the sample), then X, Y.
HOME_ORDER = ("Z_Stage", "X_Stage", "Y_Stage")

AXIS_CARD_WIDTH = 200      # design px, scaled by the font setting
SEQUENCE_ROW_HEIGHT = 26
STEP_ROLE = Qt.ItemDataRole.UserRole

# ---------------------------------------------------------------------------
# Sequence time estimate
# ---------------------------------------------------------------------------
# A move takes distance/velocity plus the time the controller spends ramping up
# and down and answering the "are you still moving?" poll. MOVE_OVERHEAD_S is
# measured on this rig and is what stops a sequence of many short 0.1 mm steps
# from being wildly under-estimated.
MOVE_OVERHEAD_S = 1.2
ETA_IDLE_TEXT = "Idle — press Run Sequence to start"

# Per-axis homing direction, remembered between runs. An axis whose '−' limit
# switch is dead can be set to "forward" so Home and Home All reference against
# the '+' switch instead, rather than grinding into an end stop it cannot see.
STAGE_CONFIG_FILE = paths.config_file("stages.json")


def load_stage_config() -> dict:
    try:
        with open(STAGE_CONFIG_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[WARN] Could not read {STAGE_CONFIG_FILE}: {e}")
        return {}


def save_stage_config(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STAGE_CONFIG_FILE), exist_ok=True)
        with open(STAGE_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save {STAGE_CONFIG_FILE}: {e}")


class LinearStageControl(QGroupBox):
    """One axis card: live position, safety envelope, jog controls."""

    addMotionRequested = pyqtSignal(str, str, str)
    homeFailed = pyqtSignal(str, str)          # stage name, message
    homeDirectionChanged = pyqtSignal(str, str)  # stage name, "reverse"/"forward"

    def __init__(self, stage_name, stage_controller, parent=None):
        super().__init__(stage_name.replace("_", " "), parent)

        self.stage_name = stage_name
        self.stage_controller = stage_controller
        self._home_thread = None

        self.setFixedWidth(theme.px(AXIS_CARD_WIDTH))

        # ---- live position ----
        self.position_label = QLabel("—")
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_label.setMinimumHeight(theme.px(30))
        self._style_position("—", theme.TEXT_MUTED)

        # ---- controls ----
        self.home_button = QPushButton("Home")
        # A plain lambda, not self.home: clicked() passes a bool that would
        # otherwise land in the direction argument.
        self.home_button.clicked.connect(lambda: self.home())
        self.home_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.home_button.customContextMenuRequested.connect(self._home_button_menu)

        self.set_zero_button = QPushButton("Set 0")
        self.set_zero_button.setToolTip(
            "Declare the CURRENT position without moving — a fallback for an axis "
            "that will not home.\n"
            "Only as accurate as your knowledge of where the stage really is.")
        self.set_zero_button.clicked.connect(self.set_reference_here)

        self.velocity_edit = QLineEdit(str(RECOMMENDED_VELOCITY_MM_S))
        self.velocity_edit.setObjectName("Numeric")
        self.velocity_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.velocity_edit.setToolTip(
            f"Recommended {RECOMMENDED_VELOCITY_MM_S} mm/s. "
            f"Hard limit {MAX_VELOCITY_MM_S} mm/s (MTS50-Z8)."
        )

        self.distance_edit = QLineEdit(str(RECOMMENDED_DISTANCE_MM))
        self.distance_edit.setObjectName("Numeric")
        self.distance_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.distance_edit.setToolTip(
            f"Relative move. Recommended step {RECOMMENDED_DISTANCE_MM} mm. "
            f"Negative reverses. Moves outside the safe range are blocked."
        )

        # Lives next to the '+ Combined' button rather than on this card: the
        # tick is only meaningful when that button is pressed, and keeping it
        # off the card saves a row of height on all three axes.
        self.sync_check = QCheckBox(stage_name.split("_")[0])
        self.sync_check.setToolTip(
            f"Include {stage_name.replace('_', ' ')} in the next '+ Combined' step, so "
            f"the ticked axes move at the same time instead of one after another.\n"
            f"Velocity and distance come from this axis' own boxes. Leave it unticked "
            f"and the axis stays still during that step."
        )

        self.add_motion_button = QPushButton("Add")
        self.add_motion_button.setToolTip("Append this move to the sequence")
        self.add_motion_button.clicked.connect(self.emit_add_motion_signal)

        self.run_motion_button = QPushButton("Run")
        self.run_motion_button.setObjectName("Primary")
        self.run_motion_button.setToolTip("Run this move now")
        self.run_motion_button.clicked.connect(self.run_motion)

        def field(caption, widget):
            row = QHBoxLayout()
            row.setSpacing(4)
            cap = QLabel(caption)
            cap.setFixedWidth(theme.px(72))
            cap.setObjectName("Hint")
            row.addWidget(cap)
            row.addWidget(widget, 1)
            return row

        layout = QVBoxLayout()
        layout.setContentsMargins(8, 2, 8, 8)
        layout.setSpacing(5)
        layout.addWidget(self.position_label)

        reference_row = QHBoxLayout()
        reference_row.setSpacing(5)
        reference_row.addWidget(self.home_button, 1)
        reference_row.addWidget(self.set_zero_button)
        layout.addLayout(reference_row)

        layout.addLayout(field("vel mm/s", self.velocity_edit))
        layout.addLayout(field("dist mm", self.distance_edit))

        buttons = QHBoxLayout()
        buttons.setSpacing(5)
        buttons.addWidget(self.add_motion_button)
        buttons.addWidget(self.run_motion_button)
        layout.addLayout(buttons)
        layout.addStretch(1)

        self.setLayout(layout)
        self.refresh_home_direction()

    # ------------------------------------------------------------------
    def refresh_home_direction(self):
        """
        Nothing about the homing end is printed on the card.

        Travel and maximum velocity are identical on all three axes and are
        stated once in the menu bar. The homing end is carried by the position
        readout instead: an axis referenced from the far end shows '⇥'. Saying
        it again in words cost a row of height on every card.
        """
        travel = self.stage_controller.travel_mm
        forward = self.stage_controller.home_direction == "forward"

        self.setToolTip(
            f"{self.stage_name.replace('_', ' ')} — travel 0–{travel:.0f} mm, "
            f"maximum {MAX_VELOCITY_MM_S:.1f} mm/s.\n"
            + ("Homes against the '+' switch (fallback)."
               if forward else
               "Homes against the '−' switch, which defines 0 mm."
               + (" On Z that is upwards, away from the sample."
                  if self.stage_name == "Z_Stage" else "")))

        end = "'+' end" if forward else "'−' end"
        self.home_button.setToolTip(
            f"Drive to the {end} limit switch and reference there.\n"
            + ("Fallback direction — this axis is set to home from the '+' end.\n"
               if forward else "The reliable reference, and the recommended one.\n")
            + "\nRight-click for the other switch.")

    def _style_position(self, text, colour, bold=True):
        self.position_label.setText(text)
        self.position_label.setStyleSheet(
            f"font-family: {theme.MONO}; font-size: {theme.px(16)}px;"
            f"font-weight: {'700' if bold else '400'}; color: {colour};"
            f"background: {theme.PANEL_ALT}; border: 1px solid {theme.BORDER};"
            f"border-radius: 4px; padding: 3px;"
        )

    def restyle(self):
        self.setFixedWidth(theme.px(AXIS_CARD_WIDTH))
        self.position_label.setMinimumHeight(theme.px(30))
        self.refresh_position()

    def is_busy(self) -> bool:
        return self._home_thread is not None and self._home_thread.is_alive()

    def home(self, direction=None):
        """direction=None uses whichever switch this axis is configured for."""
        if self.is_busy():
            return

        def worker():
            try:
                self.stage_controller.home(direction=direction)
            except Exception as e:
                # Never let this escape as a raw traceback in the console.
                self.homeFailed.emit(self.stage_name, str(e))

        self._home_thread = threading.Thread(target=worker, daemon=True)
        self._home_thread.start()

    def _home_button_menu(self, point):
        """Right-click on Home: which limit switch to reference against."""
        forward = self.stage_controller.home_direction == "forward"

        menu = QMenu(self)
        once = menu.addAction("Home against the '+' switch, just this once")
        once.setToolTip(
            "Diagnostic. If the axis homes cleanly here but not at the '−' end, "
            "the '−' switch is the fault.")
        once.setEnabled(not forward)

        menu.addSeparator()
        always = menu.addAction("Always home this axis from the '+' end")
        always.setCheckable(True)
        always.setChecked(forward)
        always.setToolTip(
            "For an axis whose '−' switch is dead. Home and Home All then use "
            "the '+' switch, so the axis is never driven into a stop it cannot "
            "detect. Remembered between runs.")

        chosen = menu.exec(self.home_button.mapToGlobal(point))
        if chosen is once:
            self.home_from_far_end()
        elif chosen is always:
            self.set_permanent_home_direction("reverse" if forward else "forward")

    def set_permanent_home_direction(self, direction: str):
        """Switch which end this axis references against, from now on."""
        if direction == "forward":
            travel = self.stage_controller.travel_mm
            ans = QMessageBox.question(
                self, f"{self.stage_name}: Always Home From The '+' End",
                f"Set {self.stage_name.replace('_', ' ')} to home against the '+' "
                f"switch from now on?\n\n"
                f"Do this only when the '−' switch has been shown not to work — the "
                f"symptom is the axis driving past the '−' end and the controller "
                f"cutting out, while a '+' home completes normally.\n\n"
                f"What changes:\n"
                f"  •  Home and Home All drive this axis to the '+' end, then bring it "
                f"back to 0 mm — so homing ends where it always did, just slower.\n"
                f"  •  0 mm is placed one nominal travel from that switch rather than "
                f"measured, so it may sit a fraction of a millimetre out. Relative "
                f"moves are unaffected, and the axis shows '⇥' as a reminder.\n\n"
                f"The setting is remembered between runs, and this is not a repair — "
                f"the '−' switch still needs fixing.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        try:
            self.stage_controller.set_home_direction(direction)
        except Exception as e:
            QMessageBox.critical(self, f"{self.stage_name}: Could Not Change Direction",
                                 str(e))
            return

        self.refresh_home_direction()
        self.homeDirectionChanged.emit(self.stage_name, direction)

    def home_from_far_end(self):
        """
        Reference against the '+' limit switch instead of the '−' one.

        Worth trying when an axis will not home: if it homes cleanly at this
        end, the '−' switch (or the mechanics right at that end) is the fault,
        not the motor or the controller.
        """
        if self.is_busy():
            return

        travel = self.stage_controller.travel_mm
        warning = (
            f"This drives {self.stage_name.replace('_', ' ')} to the FAR ('+') end "
            f"of its travel — about {travel:.0f} mm away — and references there.\n\n"
        )
        if self.stage_name == "Z_Stage":
            warning += (
                "⚠  On Z the '+' direction is DOWN, towards the sample. The probe "
                "will be driven onto anything in its path and the Nano17 can be "
                "damaged. Remove the sample first, or do not use this.\n\n")
        else:
            warning += "Check nothing is in the way over the whole travel.\n\n"

        warning += (
            "0 mm is then placed using the stage's nominal travel rather than "
            "measured at the switch, so the reference is good enough to work with "
            "but slightly less exact than a normal home. The axis shows '⇥' while "
            "it is referenced this way.\n\n"
            "Continue?")

        ans = QMessageBox.warning(
            self, f"{self.stage_name}: Home From The '+' End",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        print(f"[HOME] {self.stage_name}: fallback homing against the '+' switch")
        self.home(direction="forward")

    def set_reference_here(self):
        """Declare the current position by hand, for an axis that will not home."""
        pos, referenced, source, _, _ = self.stage_controller.snapshot()
        current = "unknown" if pos is None else f"{pos:.3f} mm"

        value, ok = QInputDialog.getDouble(
            self, f"{self.stage_name}: Set Position Reference",
            f"Declare where this axis is now, without moving it.\n\n"
            f"Controller currently reads: {current}\n"
            f"Reference so far: {source or 'none'}\n\n"
            f"Position to assign (mm):",
            0.0, -50.0, 50.0, 3)
        if not ok:
            return

        ans = QMessageBox.question(
            self, f"{self.stage_name}: Confirm Reference",
            f"Set the current position to {value:.3f} mm?\n\n"
            f"Travel limits will be measured from here, so a wrong value means the "
            f"limits protect the wrong range. Homing is the reliable alternative.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        try:
            self.stage_controller.set_reference_here(value)
        except Exception as e:
            QMessageBox.critical(self, f"{self.stage_name}: Reference Failed", str(e))
            return
        self.refresh_position()

    def refresh_position(self):
        """Called periodically from the stage panel (GUI thread) when idle."""
        pos, referenced, source, limit, faults = self.stage_controller.snapshot()
        value = "—" if pos is None else f"{pos:.3f}"

        if faults:
            self._style_position(f"{value}  ⚠", theme.DANGER)
            self.position_label.setToolTip(", ".join(faults))
        elif not referenced:
            self._style_position(f"{value}  ?", theme.WARN)
            self.position_label.setToolTip(
                "No reference — position unknown. Home the axis, or use 'Set 0'.")
        elif limit:
            self._style_position(f"{value}  ⤒", theme.DANGER)
            self.position_label.setToolTip(f"At {limit} hardware limit")
        elif source == "manual":
            self._style_position(f"{value} mm ✎", theme.WARN)
            self.position_label.setToolTip(
                "Reference set by hand, not homed — limits are only as good as that value.")
        elif source == "homed_forward":
            # Green: the position IS known and repeatable. The switch at the '+'
            # end is referenced exactly as precisely as the one at the '−' end,
            # so every relative move is unaffected. Only the absolute zero can
            # carry a small fixed offset, because it is placed one nominal
            # travel away instead of being measured — hence the marker.
            self._style_position(f"{value} mm ⇥", theme.OK)
            self.position_label.setToolTip(
                "Homed — position known, referenced at the '+' switch.\n"
                "Repeatable, and relative moves are exact. The absolute zero is placed "
                "one nominal travel from that switch, so it may sit a fraction of a "
                "millimetre from the true mechanical zero.")
        else:
            self._style_position(f"{value} mm", theme.OK)
            self.position_label.setToolTip("Homed — position known")

    # ------------------------------------------------------------------
    def check_input_fields(self):
        velocity_text = self.velocity_edit.text().strip()
        distance_text = self.distance_edit.text().strip()

        if not velocity_text or not distance_text:
            QMessageBox.warning(self, "Empty Fields", "Velocity and distance cannot be empty.")
            return None, None

        try:
            velocity = float(velocity_text)
            distance = float(distance_text)
        except ValueError:
            QMessageBox.warning(self, "Invalid Input",
                                "Please enter valid numerical values for velocity and distance.")
            return None, None

        if velocity <= 0:
            QMessageBox.warning(self, "Invalid Velocity", "Velocity must be positive.")
            return None, None

        if velocity > MAX_VELOCITY_MM_S:
            QMessageBox.critical(
                self, "Velocity Too High",
                f"{velocity:.3f} mm/s exceeds the stage limit of {MAX_VELOCITY_MM_S:.1f} mm/s "
                f"(MTS50-Z8). Recommended: {RECOMMENDED_VELOCITY_MM_S} mm/s."
            )
            return None, None

        travel = self.stage_controller.travel_mm
        if abs(distance) > travel:
            QMessageBox.critical(
                self, "Distance Too Large",
                f"|{distance:.3f}| mm exceeds the total stage travel of {travel:.0f} mm."
            )
            return None, None

        return velocity, distance

    def run_motion(self):
        velocity, distance = self.check_input_fields()
        if velocity is None or distance is None:
            return

        # Invert Y-axis direction to match rig convention
        if self.stage_name == "Y_Stage":
            distance = -distance

        chk = self.stage_controller.check_move(distance)
        if not chk.ok:
            if chk.homed:
                QMessageBox.critical(
                    self, f"{self.stage_name}: Move Blocked",
                    f"{chk.reason}\n\nThe move was NOT sent to the stage."
                )
                print(f"[SAFETY] {self.stage_name} move blocked: {chk.reason}")
                return

            if abs(distance) > UNHOMED_JOG_LIMIT_MM:
                QMessageBox.critical(
                    self, f"{self.stage_name}: Reference Required",
                    f"{self.stage_name} has no position reference, so where it is is "
                    f"unknown.\n\n"
                    f"Moves larger than ±{UNHOMED_JOG_LIMIT_MM:.1f} mm are not allowed until "
                    f"the axis is homed or given a reference with 'Set 0'. "
                    f"Requested: {distance:+.3f} mm."
                )
                print(f"[SAFETY] {self.stage_name} unhomed move blocked "
                      f"(> {UNHOMED_JOG_LIMIT_MM} mm)")
                return

            ans = QMessageBox.question(
                self, f"{self.stage_name}: Position Unknown",
                f"{chk.reason}\n\n"
                f"Requested move: {distance:+.3f} mm. Move anyway?\n"
                f"(Home, or use 'Set 0', to enable automatic limit checking.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            print(f"[SAFETY] {self.stage_name} unverified move accepted by user: "
                  f"{distance:+.3f} mm")

        try:
            self.stage_controller.setup_velocity(0, 10, velocity)
            self.stage_controller.move_by(distance)
        except Exception as e:
            QMessageBox.critical(self, f"{self.stage_name}: Motion Error", str(e))

    def emit_add_motion_signal(self):
        velocity, distance = self.check_input_fields()
        if velocity is not None and distance is not None:
            self.addMotionRequested.emit(self.stage_name, str(velocity), str(distance))

    def combined_selected(self) -> bool:
        return self.sync_check.isChecked()

    def current_move(self):
        """(velocity, box_distance) after validation, or None."""
        velocity, distance = self.check_input_fields()
        if velocity is None or distance is None:
            return None
        return velocity, distance


class StagePanel(QWidget):
    """Stage control half of the main window."""

    touch_finished = pyqtSignal(object)
    home_all_finished = pyqtSignal(str)
    status_message = pyqtSignal(str)

    def __init__(self, logging_queue, touch_data_queue, parent=None):
        super().__init__(parent)

        self.logging_queue = logging_queue
        self.touch_data_queue = touch_data_queue

        # latest sample from the sensor panel (time_us, fz, pz)
        self.latest_time_us = None
        self.latest_fz = None
        self.latest_pz = None

        # ---- touch probe runtime ----
        self._touch_abort = threading.Event()
        self._touch_thread = None
        self._touch_running = False
        self._home_all_thread = None
        self.touch_pz = None

        self.touch_listener = TouchDataListener(self.touch_data_queue)
        self.touch_listener.sample.connect(self._on_touch_sample)
        self.touch_listener.start()

        # ---- stages ----
        self.stageX_controller = StageController(STAGE_X_ID)
        self.stageY_controller = StageController(STAGE_Y_ID)
        self.stageZ_controller = StageController(STAGE_Z_ID)

        self.stage1_control = LinearStageControl("X_Stage", self.stageX_controller)
        self.stage2_control = LinearStageControl("Y_Stage", self.stageY_controller)
        self.stage3_control = LinearStageControl("Z_Stage", self.stageZ_controller)
        self.stage_controls = (self.stage1_control, self.stage2_control, self.stage3_control)

        # Restore any axis that has been set to home from the '+' end.
        self.stage_config = load_stage_config()
        for c in self.stage_controls:
            saved = (self.stage_config.get(c.stage_name) or {}).get("home_direction")
            if saved in ("reverse", "forward"):
                try:
                    c.stage_controller.set_home_direction(saved)
                except Exception as e:
                    print(f"[WARN] {c.stage_name}: could not restore home direction ({e})")
                c.refresh_home_direction()

        for c in self.stage_controls:
            c.addMotionRequested.connect(self.add_motion)
            c.homeFailed.connect(self._on_single_home_failed)
            c.homeDirectionChanged.connect(self._on_home_direction_changed)

        # ---- sequence list ----
        self.motion_list = QListWidget()
        self.motion_list.setUniformItemSizes(True)
        self.motion_list.setSpacing(0)

        self.pause_button = QPushButton("+ Pause")
        self.pause_button.setToolTip("Append a timed pause to the sequence")
        self.pause_button.clicked.connect(self.add_pause)

        self.combined_button = QPushButton("+ Combined")
        self.combined_button.setToolTip(
            "Add ONE step in which every axis ticked 'combine' moves at the same time, "
            "using the velocity and distance in its own boxes.\n"
            "Tick any two or all three — an unticked axis simply stays where it is."
        )
        self.combined_button.clicked.connect(self.add_combined_motion)

        self.move_up_button = QPushButton("↑")
        self.move_up_button.setToolTip("Move the selected step one place earlier")
        self.move_up_button.clicked.connect(lambda: self.move_step(-1))

        self.move_down_button = QPushButton("↓")
        self.move_down_button.setToolTip("Move the selected step one place later")
        self.move_down_button.clicked.connect(lambda: self.move_step(+1))

        self.load_sequence_button = QPushButton("Load")
        self.load_sequence_button.clicked.connect(self.load_sequence)

        self.save_sequence_button = QPushButton("Save")
        self.save_sequence_button.clicked.connect(self.save_sequence)

        self.clear_sequence_button = QPushButton("Clear")
        self.clear_sequence_button.setObjectName("Danger")
        self.clear_sequence_button.clicked.connect(self.clear_sequence)

        seq_tools = QHBoxLayout()
        seq_tools.setSpacing(5)
        seq_tools.addWidget(self.combined_button)
        seq_tools.addWidget(self.pause_button)
        seq_tools.addSpacing(6)
        seq_tools.addWidget(self.move_up_button)
        seq_tools.addWidget(self.move_down_button)
        seq_tools.addStretch(1)
        seq_tools.addWidget(self.clear_sequence_button)

        # ---- progress / countdown ----
        self.sequence_progress = QProgressBar()
        self.sequence_progress.setRange(0, 1000)
        self.sequence_progress.setValue(0)
        self.sequence_progress.setTextVisible(False)
        self.sequence_progress.setFixedHeight(theme.px(8))

        self.eta_label = QLabel(ETA_IDLE_TEXT)
        self.eta_label.setObjectName("Hint")
        # Status text must not dictate how narrow the window can get: let it be
        # clipped rather than have it hold the whole layout open.
        self.eta_label.setMinimumWidth(theme.px(40))
        self.eta_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                     QSizePolicy.Policy.Preferred)
        self.eta_label.setToolTip(
            "Estimated from the distance and velocity of every step plus the pauses.\n"
            "A rough guide: acceleration and controller overhead are approximated.")

        # The axis ticks sit on this row rather than beside '+ Combined': the
        # progress line is the one row in the box with width to spare, so they
        # cost no minimum width here, whereas up in the tool row they pushed
        # the whole window ~100 px wider.
        combine_caption = QLabel("combine:")
        combine_caption.setObjectName("Hint")
        combine_caption.setToolTip(
            "Tick the axes that should move together in the next '+ Combined' step.")

        eta_row = QHBoxLayout()
        eta_row.setSpacing(5)
        eta_row.addWidget(combine_caption)
        for c in self.stage_controls:
            eta_row.addWidget(c.sync_check)
        eta_row.addSpacing(10)
        eta_row.addWidget(self.eta_label, 1)

        seq_box = QGroupBox("Sequence")
        seq_layout = QVBoxLayout()
        seq_layout.setContentsMargins(8, 2, 8, 8)
        seq_layout.setSpacing(5)
        seq_layout.addLayout(seq_tools)
        seq_layout.addWidget(self.motion_list, 1)
        seq_layout.addWidget(self.sequence_progress)
        seq_layout.addLayout(eta_row)
        seq_box.setLayout(seq_layout)

        # ---- bottom toolbar ----
        self.home_all_button = QPushButton("Home All  (Z→X→Y)")
        self.home_all_button.setToolTip(
            f"Home every stage towards its '{HOME_DIRECTION}' (−) limit switch. "
            f"Z first, so the probe lifts away from the sample before X/Y move."
        )
        self.home_all_button.clicked.connect(self.home_all)

        self.sequence_name_input = QLineEdit()
        self.sequence_name_input.setPlaceholderText("Sequence name → CSV")
        self.sequence_name_input.setMinimumWidth(theme.px(200))
        self.sequence_name_input.setMaximumWidth(theme.px(280))
        self.sequence_name_input.setToolTip("Used to name the recorded CSV in output/")
        self.sequence_name = ""

        self.run_sequence_button = QPushButton("Run Sequence")
        self.run_sequence_button.setObjectName("Primary")
        self.run_sequence_button.setCheckable(True)
        self.run_sequence_button.clicked.connect(self.toggle_sequence)

        self.set_touch_button = QPushButton("Set Touch Point")
        self.set_touch_button.clicked.connect(self.on_set_touch_point)

        self.abort_touch_button = QPushButton("Abort")
        self.abort_touch_button.setObjectName("Danger")
        self.abort_touch_button.setEnabled(False)
        self.abort_touch_button.clicked.connect(self.on_abort_touch_point)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(self.home_all_button)
        toolbar.addSpacing(10)
        toolbar.addWidget(self.sequence_name_input)
        # Load / Run / Save reads as one operation on the sequence beside it.
        toolbar.addWidget(self.load_sequence_button)
        toolbar.addWidget(self.run_sequence_button)
        toolbar.addWidget(self.save_sequence_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.set_touch_button)
        toolbar.addWidget(self.abort_touch_button)

        # ---- assembly ----
        axes = QHBoxLayout()
        axes.setSpacing(8)
        for c in self.stage_controls:
            axes.addWidget(c)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addLayout(axes)
        top.addWidget(seq_box, 1)

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addLayout(top, 1)
        root.addLayout(toolbar)
        self.setLayout(root)

        self.touch_finished.connect(self._on_touch_finished)
        self.home_all_finished.connect(self._on_home_all_finished)

        self.sequence_worker = None
        self.sequence_active = False

        # ---- countdown state ----
        self._step_seconds = []      # estimate for each step, in order
        self._total_seconds = 0.0
        self._current_step = 0
        self._step_started_at = 0.0
        self._sequence_started_at = 0.0
        self.eta_timer = QTimer(self)
        self.eta_timer.setInterval(250)
        self.eta_timer.timeout.connect(self._update_eta)

        self.position_timer = QTimer(self)
        self.position_timer.setInterval(1000)
        self.position_timer.timeout.connect(self._refresh_positions)
        self.position_timer.start()
        self._refresh_positions()

        QTimer.singleShot(400, self._startup_homing_check)

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------
    def _stages_busy(self) -> bool:
        if self.sequence_active or self._touch_running:
            return True
        if self._home_all_thread is not None and self._home_all_thread.is_alive():
            return True
        return any(c.is_busy() for c in self.stage_controls)

    def _refresh_positions(self):
        if self._stages_busy():
            return
        for c in self.stage_controls:
            try:
                c.refresh_position()
            except Exception:
                pass

    def restyle(self):
        for c in self.stage_controls:
            c.restyle()
        self.sequence_name_input.setMinimumWidth(theme.px(200))
        self.sequence_name_input.setMaximumWidth(theme.px(280))
        self.sequence_progress.setFixedHeight(theme.px(8))
        for i in range(self.motion_list.count()):
            item = self.motion_list.item(i)
            item.setSizeHint(QSize(0, theme.px(SEQUENCE_ROW_HEIGHT)))
            widget = self.motion_list.itemWidget(item)
            if widget:
                widget.setStyleSheet(f"background: {theme.PANEL};")
                label = widget.layout().itemAt(0).widget()
                label.setText(self._row_html(item.data(STEP_ROLE)))
                label.setStyleSheet(f"font-size: {theme.px(12)}px;")

    def travel_summary(self) -> str:
        """
        One line describing the stage envelope, for the menu bar.

        Collapsed to a single figure while the axes match, which they do on
        this rig; if a stage is ever swapped for a different travel the line
        spells each one out rather than quietly reporting the wrong number.
        """
        travels = {c.stage_name.split("_")[0]: c.stage_controller.travel_mm
                   for c in self.stage_controls}
        limit = f"max {MAX_VELOCITY_MM_S:.1f} mm/s"
        if len(set(travels.values())) == 1:
            return f"Stages 0–{next(iter(travels.values())):.0f} mm · {limit}"
        parts = ", ".join(f"{n} 0–{t:.0f} mm" for n, t in travels.items())
        return f"Stages {parts} · {limit}"

    def _unreferenced_stages(self):
        return [c.stage_name for c in self.stage_controls
                if not c.stage_controller.is_referenced()]

    def _on_home_direction_changed(self, stage_name: str, direction: str):
        self.stage_config.setdefault(stage_name, {})["home_direction"] = direction
        save_stage_config(self.stage_config)
        end = "'+'" if direction == "forward" else "'−'"
        self.status_message.emit(f"{stage_name}: now homes against the {end} switch")

    def _on_single_home_failed(self, stage_name: str, message: str):
        self.status_message.emit(f"{stage_name}: homing failed")
        QMessageBox.critical(self, f"{stage_name}: Homing Failed", message)

    def _control_for(self, stage_name):
        if stage_name == "X_Stage":
            return self.stage1_control
        if stage_name == "Y_Stage":
            return self.stage2_control
        return self.stage3_control

    def _controller_for(self, stage_name):
        if stage_name == "X_Stage":
            return self.stageX_controller
        if stage_name == "Y_Stage":
            return self.stageY_controller
        return self.stageZ_controller

    # ------------------------------------------------------------------
    # Homing
    # ------------------------------------------------------------------
    def _startup_homing_check(self):
        unhomed = self._unreferenced_stages()
        if not unhomed:
            print("[INFO] All stages have a position reference.")
            self.status_message.emit("All stages referenced")
            return

        print(f"[WARN] No position reference at startup: {unhomed}")
        self.status_message.emit(f"No reference: {', '.join(unhomed)}")
        ans = QMessageBox.question(
            self, "Reference Required",
            f"{', '.join(unhomed)} have no position reference - absolute position is "
            f"unknown, so travel limits cannot be checked.\n\n"
            f"Home all stages now?\n"
            f"Order: Z first (Z− = up, away from the sample), then X, then Y.\n\n"
            f"Make sure nothing obstructs the stages. You can also press 'Home All' later, "
            f"or use 'Set 0' on an axis that refuses to home.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.home_all()

    def home_all(self):
        if self._stages_busy():
            QMessageBox.information(self, "Busy",
                                    "Stages are busy; wait for the current operation to finish.")
            return

        self._set_controls_enabled(False)
        self.status_message.emit("Homing Z → X → Y …")

        def worker():
            msgs = []
            for name in HOME_ORDER:
                ctrl = self._controller_for(name)
                try:
                    end = "'+'" if ctrl.home_direction == "forward" else "'−'"
                    print(f"[HOME] {name}: homing towards the {end} switch "
                          f"({ctrl.home_direction})...")
                    ctrl.home()
                    pos = ctrl.get_position_mm()
                    msgs.append(f"{name}: homed (pos {pos:.3f} mm)" if pos is not None
                                else f"{name}: homed")
                except Exception as e:
                    print(f"[HOME] {name} failed: {e}")
                    if ctrl.is_homed():
                        pos = ctrl.get_position_mm()
                        msgs.append(f"{name}: homed after a comm glitch (pos {ctrl._fmt(pos)} mm)")
                        print(f"[HOME] {name}: controller reports homed despite the error; "
                              f"continuing.")
                        continue

                    msgs.append(f"{name}: HOMING FAILED\n    {e}")
                    if name == "Z_Stage":
                        msgs.append("Stopped: X/Y not homed because Z did not retract.")
                        break
            self.home_all_finished.emit("\n".join(msgs))

        self._home_all_thread = threading.Thread(target=worker, daemon=True)
        self._home_all_thread.start()

    def _set_controls_enabled(self, enabled: bool):
        self.home_all_button.setEnabled(enabled)
        self.run_sequence_button.setEnabled(enabled)
        self.set_touch_button.setEnabled(enabled)
        for c in self.stage_controls:
            c.home_button.setEnabled(enabled)
            c.set_zero_button.setEnabled(enabled)
            c.run_motion_button.setEnabled(enabled)

    def _on_home_all_finished(self, summary: str):
        self._set_controls_enabled(True)
        self._refresh_positions()
        if "FAILED" in summary:
            self.status_message.emit("Homing incomplete")
            QMessageBox.critical(self, "Homing Incomplete", summary)
        else:
            self.status_message.emit("Homing complete")
            QMessageBox.information(self, "Homing Complete", summary)

    # ------------------------------------------------------------------
    # Touch probe
    # ------------------------------------------------------------------
    def _on_touch_sample(self, time_us: int, fz: float, pz: float):
        self.latest_time_us = time_us
        self.latest_fz = fz
        self.latest_pz = pz

    def _get_latest_touch_sample(self):
        if self.latest_time_us is None or self.latest_fz is None or self.latest_pz is None:
            return None
        return (int(self.latest_time_us), float(self.latest_fz), float(self.latest_pz))

    def on_set_touch_point(self):
        if self._touch_running:
            return

        if self.latest_time_us is None:
            QMessageBox.warning(self, "No Data",
                                "No touch data received yet. Start acquisition first.")
            return

        self.set_touch_button.setEnabled(False)
        self.abort_touch_button.setEnabled(True)
        self.run_sequence_button.setEnabled(False)

        self._touch_abort.clear()
        self._touch_running = True
        self.status_message.emit("Touch probe running…")

        cfg = TouchConfig(
            z_max_mm=2.5,
            approach_vel_mm_s=0.5,
            overtravel_mm=0.2,
            retract_mm=0.8,
            retract_emergency_mm=1.0,
            return_vel_mm_s=0.15,
            return_step_mm=0.02,
            return_tol_mm=0.01,
            f_touch_n=0.015,
            confirm_ms=50,
            f_abs_limit_n=0.08,
            dFdt_limit_n_s=1.0,
            baseline_s=0.6,
            probe_timeout_s=10.0,
        )

        def status_cb(msg: str):
            print(f"[TOUCH] {msg}")

        def worker():
            try:
                runner = TouchProbeRunner(
                    stage_z=self.stageZ_controller,
                    get_latest_sample=self._get_latest_touch_sample,
                    cfg=cfg,
                    abort_event=self._touch_abort,
                    status_cb=status_cb,
                )
                result = runner.run()
                print(
                    f"[TOUCH] result: ok={result.ok} reason={result.reason} "
                    f"touch_pz={result.touch_pz} fz0={result.fz0} fz_touch={result.fz_touch} "
                    f"peak_fz_net={result.peak_fz_net} peak_dFdt={result.peak_dFdt}"
                )
            except Exception as e:
                class _Fail:
                    ok = False
                    reason = f"Touch probe crashed: {e}"
                    touch_pz = None
                result = _Fail()

            self.touch_finished.emit(result)

        self._touch_thread = threading.Thread(target=worker, daemon=True)
        self._touch_thread.start()

    def _on_touch_finished(self, result):
        try:
            if result.ok:
                self.touch_pz = result.touch_pz
                self.status_message.emit(f"Touch point set at pz={result.touch_pz:.4f} mm")
                QMessageBox.information(self, "Touch Point Set",
                                        f"Touch point set at pz={result.touch_pz:.4f} mm")
            else:
                self.status_message.emit("Touch failed")
                QMessageBox.warning(self, "Touch Failed", f"{result.reason}")
        finally:
            self._touch_running = False
            self.set_touch_button.setEnabled(True)
            self.abort_touch_button.setEnabled(False)
            self.run_sequence_button.setEnabled(True)

    def on_abort_touch_point(self):
        print("[TOUCH] abort requested")
        self.abort_touch_button.setEnabled(False)
        self._touch_abort.set()
        try:
            self.stageZ_controller.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sequence
    # ------------------------------------------------------------------
    # --- steps are stored on the item itself, never parsed back out of the UI ---
    #     ("pause", seconds)
    #     ("move", [(stage_name, velocity, box_distance), ...])   1 or more axes

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds >= 60:
            minutes = seconds / 60.0
            return f"{minutes:g} min  ({seconds:g} s)"
        return f"{seconds:g} s"

    @staticmethod
    def _describe(step):
        kind = step[0]
        if kind == "pause":
            return "Pause", StagePanel._format_duration(step[1])

        axes = step[1]
        if len(axes) == 1:
            name, velocity, distance = axes[0]
            return name, f"Velocity: {velocity:g}, Distance: {distance:g}"

        parts = " · ".join(f"{n.split('_')[0]} {v:g}/{d:g}" for n, v, d in axes)
        return f"Combined ×{len(axes)}", parts

    @staticmethod
    def _row_html(step) -> str:
        title, detail = StagePanel._describe(step)
        if step[0] == "pause":
            colour = theme.TEXT_MUTED
        elif len(step[1]) > 1:
            colour = theme.WARN
        else:
            colour = theme.ACCENT
        return (f"<b><span style='color:{colour}'>{title}</span></b>"
                f"<span style='color:{theme.TEXT}'>  {detail}</span>")

    def add_step(self, step):
        item = QListWidgetItem()
        item.setData(STEP_ROLE, step)
        self.motion_list.addItem(item)

        label = QLabel(self._row_html(step))
        label.setStyleSheet(f"font-size: {theme.px(12)}px;")
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        remove_button = QPushButton("✕")
        remove_button.setObjectName("RowRemove")
        remove_button.setToolTip("Remove this step")
        remove_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        remove_button.clicked.connect(lambda _, i=item: self.remove_motion(i))

        widget = QWidget()
        widget.setObjectName("SeqRow")
        # The row container sits inside the list, not on the window, so it must
        # take the panel colour explicitly (Qt style sheets are not inherited).
        widget.setStyleSheet(f"background: {theme.PANEL};")
        widget_layout = QHBoxLayout()
        widget_layout.setContentsMargins(7, 0, 5, 0)
        widget_layout.setSpacing(4)
        widget_layout.addWidget(label, 1)
        widget_layout.addWidget(remove_button)
        widget.setLayout(widget_layout)

        item.setSizeHint(QSize(0, theme.px(SEQUENCE_ROW_HEIGHT)))
        self.motion_list.setItemWidget(item, widget)
        self._refresh_idle_estimate()

    def steps(self):
        return [self.motion_list.item(i).data(STEP_ROLE)
                for i in range(self.motion_list.count())]

    # ------------------------------------------------------------------
    def add_motion(self, stage_name, velocity, distance):
        self.add_step(("move", [(stage_name, float(velocity), float(distance))]))

    def add_combined_motion(self):
        chosen = [c for c in self.stage_controls if c.combined_selected()]
        if not chosen:
            QMessageBox.information(
                self, "Combined Move",
                "No axis is ticked.\n\n"
                "Tick 'combine' on the axes that should move together — two is enough, "
                "and any axis left unticked simply stays where it is."
            )
            return

        axes = []
        for c in chosen:
            move = c.current_move()
            if move is None:      # the axis card already explained why
                return
            axes.append((c.stage_name, move[0], move[1]))

        self.add_step(("move", axes))
        if len(axes) == 1:
            self.status_message.emit(
                f"Only {axes[0][0]} was ticked — added as a normal single-axis step")
        else:
            self.status_message.emit(
                "Combined step added, moving together: "
                + ", ".join(n.split("_")[0] for n, _, _ in axes))

    def add_pause(self):
        seconds = PauseDialog.get_seconds(self)
        if seconds is not None:
            self.add_step(("pause", seconds))

    def move_step(self, delta: int):
        """
        Reorder the selected step.

        The rows carry widgets, which takeItem() would discard, so the list is
        rebuilt from the step payloads instead of being shuffled in place. A
        sequence is a few dozen steps at most, so that is cheap and it keeps
        one code path for building rows.
        """
        if self.sequence_active:
            self.status_message.emit("Cannot reorder while a sequence is running")
            return

        row = self.motion_list.currentRow()
        if row < 0:
            self.status_message.emit("Select a step to move it")
            return

        target = row + delta
        if not 0 <= target < self.motion_list.count():
            return

        steps = self.steps()
        steps[row], steps[target] = steps[target], steps[row]

        self.motion_list.clear()
        for step in steps:
            self.add_step(step)
        self.motion_list.setCurrentRow(target)
        self.motion_list.scrollToItem(self.motion_list.item(target))

    def remove_motion(self, item):
        self.motion_list.takeItem(self.motion_list.row(item))
        self._refresh_idle_estimate()

    def clear_sequence(self):
        if self.motion_list.count() == 0:
            return
        ans = QMessageBox.question(
            self, "Clear Sequence",
            f"Remove all {self.motion_list.count()} steps?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.motion_list.clear()
            self._refresh_idle_estimate()

    # ------------------------------------------------------------------
    # Sequence files
    #   Pause                : "Pause Time: 10.0"
    #   Single axis          : "X_Stage Velocity: 0.5, Distance: 0.1"
    #   Simultaneous axes    : "Combined X_Stage:0.5,0.1 Y_Stage:0.5,0.2"
    # The first two are the original format, so old files still load.
    # ------------------------------------------------------------------
    @staticmethod
    def _step_to_line(step) -> str:
        if step[0] == "pause":
            return f"Pause Time: {step[1]:g}"
        axes = step[1]
        if len(axes) == 1:
            name, velocity, distance = axes[0]
            return f"{name} Velocity: {velocity:g}, Distance: {distance:g}"
        body = " ".join(f"{n}:{v:g},{d:g}" for n, v, d in axes)
        return f"Combined {body}"

    @staticmethod
    def _line_to_step(line: str):
        words = line.split()
        if not words:
            return None

        if words[0] == "Pause":
            return ("pause", float(words[2]))

        if words[0] == "Combined":
            axes = []
            for token in words[1:]:
                name, _, values = token.partition(":")
                velocity, _, distance = values.partition(",")
                axes.append((name, float(velocity), float(distance)))
            return ("move", axes)

        # original single-axis format
        return ("move", [(words[0], float(words[2].rstrip(",")), float(words[4]))])

    def load_sequence(self):
        sequence_file, _ = QFileDialog.getOpenFileName(
            self, "Load Sequence", paths.SEQUENCES_DIR, "Text Files (*.txt)")
        if not sequence_file:
            return

        self.motion_list.clear()
        skipped = 0
        with open(sequence_file, "r") as file:
            for line in file:
                try:
                    step = self._line_to_step(line)
                except Exception:
                    step = None
                if step is None:
                    if line.strip():
                        skipped += 1
                    continue
                self.add_step(step)

        msg = f"Loaded {self.motion_list.count()} steps"
        if skipped:
            msg += f" ({skipped} unreadable lines skipped)"
        self.status_message.emit(msg)
        self._refresh_idle_estimate()

    def save_sequence(self):
        name, ok = QInputDialog.getText(self, "Save Sequence", "Enter sequence file name:")
        if not ok or not name.strip():
            return
        target = paths.sequence_file(name.strip())
        try:
            with open(target, "w") as file:
                for step in self.steps():
                    file.write(self._step_to_line(step) + "\n")
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", f"Could not write {target}:\n{e}")
            return
        self.status_message.emit(f"Saved {os.path.basename(target)}")

    # ------------------------------------------------------------------
    def _signed(self, stage_name, box_distance):
        """Box value -> actual distance (Y is inverted by rig convention)."""
        return -box_distance if stage_name == "Y_Stage" else box_distance

    def _preflight_sequence(self, steps) -> bool:
        problems, not_homed, sim_pos = [], [], {}

        for name in ("X_Stage", "Y_Stage", "Z_Stage"):
            ctrl = self._controller_for(name)
            sim_pos[name] = ctrl.get_position_mm() if ctrl.is_referenced() else None

        for i, step in enumerate(steps, start=1):
            if step[0] == "pause":
                continue

            axes = step[1]
            seen = set()
            for name, velocity, box_distance in axes:
                if name in seen:
                    problems.append(f"Step {i}: {name} appears twice in the same "
                                    f"combined move")
                    continue
                seen.add(name)

                ctrl = self._controller_for(name)

                if velocity > MAX_VELOCITY_MM_S:
                    problems.append(f"Step {i}: {name} velocity {velocity:g} mm/s > "
                                    f"{MAX_VELOCITY_MM_S} mm/s limit")
                    continue

                if sim_pos[name] is None:
                    if name not in not_homed:
                        not_homed.append(name)
                    continue

                hi = ctrl.travel_mm - SAFETY_MARGIN_MM
                target = sim_pos[name] + self._signed(name, box_distance)
                if target < -HOME_TOLERANCE_MM or target > hi:
                    problems.append(f"Step {i}: {name} would reach {target:.3f} mm "
                                    f"(safe range 0–{hi:.1f} mm)")
                sim_pos[name] = target

        if problems:
            QMessageBox.critical(
                self, "Sequence Blocked",
                "The sequence would exceed stage limits and was NOT started:\n\n"
                + "\n".join(problems[:15]) + ("\n..." if len(problems) > 15 else "")
            )
            print("[SAFETY] Sequence blocked:\n  " + "\n  ".join(problems))
            return False

        if not_homed:
            QMessageBox.critical(
                self, "Reference Required",
                f"{', '.join(not_homed)} have no position reference: position unknown, "
                f"travel limits cannot be verified.\n\n"
                f"Sequences only run on referenced stages. Press 'Home All' first, or use "
                f"'Set 0' on an axis that refuses to home."
            )
            print(f"[SAFETY] Sequence blocked, unreferenced stages: {not_homed}")
            return False

        return True

    # ------------------------------------------------------------------
    # Time estimate / countdown
    # ------------------------------------------------------------------
    @staticmethod
    def _format_clock(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @staticmethod
    def _estimate_step_seconds(step) -> float:
        """
        How long one step should take.

        A pause is exact. A move is travel time plus a fixed overhead for the
        velocity ramp and the completion poll; axes moving together finish
        with the slowest of them, so the group costs the maximum, not the sum.
        """
        if step[0] == "pause":
            return float(step[1])

        longest = 0.0
        for _name, velocity, box_distance in step[1]:
            try:
                v = abs(float(velocity))
                d = abs(float(box_distance))
            except Exception:
                continue
            longest = max(longest, d / v if v > 0 else 0.0)
        return longest + MOVE_OVERHEAD_S

    def estimate_sequence_seconds(self, steps) -> float:
        return sum(self._estimate_step_seconds(s) for s in steps)

    def _reset_eta(self, message: str = ETA_IDLE_TEXT):
        self.eta_timer.stop()
        self.sequence_progress.setValue(0)
        self.eta_label.setText(message)

    def _refresh_idle_estimate(self):
        """Show what the sequence will cost, before it is started."""
        if getattr(self, "sequence_active", False):
            return
        steps = self.steps()
        if not steps:
            self.eta_label.setText(ETA_IDLE_TEXT)
            return
        total = self.estimate_sequence_seconds(steps)
        self.eta_label.setText(
            f"{len(steps)} steps  ·  estimated run time ≈ {self._format_clock(total)}")

    def _update_eta(self):
        if not self._step_seconds:
            return

        now = time.monotonic()
        elapsed_total = now - self._sequence_started_at
        index = min(self._current_step, len(self._step_seconds) - 1)

        # Time still expected in the running step, plus every step after it.
        in_step = now - self._step_started_at
        remaining = max(0.0, self._step_seconds[index] - in_step)
        remaining += sum(self._step_seconds[index + 1:])

        done = max(0.0, self._total_seconds - remaining)
        fraction = done / self._total_seconds if self._total_seconds > 0 else 0.0
        self.sequence_progress.setValue(int(min(1.0, fraction) * 1000))

        # A step running past its estimate shows "≈" rather than a fake 00:00.
        overrun = "≈ " if remaining <= 0.5 and index < len(self._step_seconds) - 1 else ""
        self.eta_label.setText(
            f"Step {index + 1}/{len(self._step_seconds)}  ·  "
            f"elapsed {self._format_clock(elapsed_total)}  ·  "
            f"remaining {overrun}{self._format_clock(remaining)}  ·  "
            f"total ≈ {self._format_clock(self._total_seconds)}"
        )

    def toggle_sequence(self):
        if self.sequence_active:
            self.stop_sequence()
        else:
            self.start_sequence()

    def start_sequence(self):
        if self.sequence_worker is not None and self.sequence_worker.isRunning():
            return

        steps = self.steps()

        if not steps:
            QMessageBox.information(self, "Empty Sequence", "Add motions or pauses first.")
            self.run_sequence_button.setChecked(False)
            return

        if not self._preflight_sequence(steps):
            self.run_sequence_button.setChecked(False)
            return

        self.sequence_active = True
        self.sequence_name = self.sequence_name_input.text()
        self.run_sequence_button.setChecked(True)
        self.run_sequence_button.setText("Stop Sequence")

        # Countdown: cost every step up front, then tick against the clock.
        self._step_seconds = [self._estimate_step_seconds(s) for s in steps]
        self._total_seconds = sum(self._step_seconds)
        self._current_step = 0
        self._sequence_started_at = self._step_started_at = time.monotonic()
        self.sequence_progress.setValue(0)
        self.eta_timer.start()
        self._update_eta()

        self.status_message.emit(
            f"Running sequence ({len(steps)} steps, ≈ "
            f"{self._format_clock(self._total_seconds)})")

        self.sequence_worker = SequenceWorker(self, steps)
        self.sequence_worker.progress.connect(self._on_sequence_progress)
        self.sequence_worker.status.connect(self._on_sequence_status)
        self.sequence_worker.safety_stop.connect(self._on_sequence_safety_stop)
        self.sequence_worker.finished.connect(self._on_sequence_finished)
        self.sequence_worker.start()

    def stop_sequence(self):
        self.sequence_active = False
        self.status_message.emit("Stopping sequence…")

    def set_logging_active(self, logging_flag, sequence_name):
        self.logging_queue.put((logging_flag, sequence_name))
        print("Recording started" if logging_flag else "Recording stopped")

    def _on_sequence_progress(self, index: int):
        self._current_step = index
        self._step_started_at = time.monotonic()
        self._update_eta()

        for i in range(self.motion_list.count()):
            w = self.motion_list.itemWidget(self.motion_list.item(i))
            if w:
                w.setStyleSheet(f"background: "
                                f"{theme.PANEL_ALT if i == index else theme.PANEL};")
        self.motion_list.scrollToItem(self.motion_list.item(index))

    def _on_sequence_status(self, msg: str):
        print(msg)

    def _on_sequence_safety_stop(self, msg: str):
        self.status_message.emit("Sequence stopped (safety)")
        QMessageBox.critical(self, "Sequence Stopped (Safety)", msg)

    def _on_sequence_finished(self):
        self.sequence_active = False
        self.run_sequence_button.setChecked(False)
        self.run_sequence_button.setText("Run Sequence")

        took = time.monotonic() - self._sequence_started_at if self._sequence_started_at else 0.0
        self._reset_eta(f"Finished in {self._format_clock(took)} "
                        f"(estimated {self._format_clock(self._total_seconds)})")
        self.status_message.emit(f"Sequence finished in {self._format_clock(took)}")
        for i in range(self.motion_list.count()):
            w = self.motion_list.itemWidget(self.motion_list.item(i))
            if w:
                w.setStyleSheet(f"background: {theme.PANEL};")

    # ------------------------------------------------------------------
    def shutdown(self):
        try:
            self.position_timer.stop()
        except Exception:
            pass

        try:
            self.sequence_active = False
            if self.sequence_worker is not None and self.sequence_worker.isRunning():
                for ctrl in (self.stageX_controller, self.stageY_controller,
                             self.stageZ_controller):
                    try:
                        ctrl.stop()
                    except Exception:
                        pass
                self.sequence_worker.wait(2000)
        except Exception:
            pass

        if getattr(self, "_touch_running", False):
            self._touch_abort.set()

        for ctrl in (self.stageX_controller, self.stageY_controller, self.stageZ_controller):
            try:
                ctrl.stop()
            except Exception:
                pass
            try:
                ctrl.close()
            except Exception:
                pass

        try:
            self.touch_listener.stop()
            self.touch_listener.wait(800)
        except Exception:
            pass


class MainWindow(QMainWindow):
    """Stage control on top, sensor display underneath, in one window."""

    def __init__(self, logging_queue, touch_data_queue):
        super().__init__()
        self.setWindowTitle("ATI Rig — Stage Control & Sensor Display")

        self.stage_panel = StagePanel(logging_queue, touch_data_queue)
        self.sensor_panel = SensorPanel(logging_queue, touch_data_queue)

        self.splitter = splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.stage_panel)
        splitter.addWidget(self.sensor_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes(theme.SPLITTER_SIZES or [268, 500])
        splitter.setChildrenCollapsible(False)

        container = QWidget()
        layout = QVBoxLayout()
        # Barely any gap under the menu bar: the group boxes already reserve a
        # band above themselves for their titles, so a wide margin here only
        # pushed the whole window down. Kept at 2 rather than 0 so the cards
        # do not look welded to the menu bar.
        layout.setContentsMargins(10, 2, 10, 6)
        layout.addWidget(splitter)
        container.setLayout(layout)
        self.setCentralWidget(container)

        self._build_menu()
        self._centre_range_note()

        self.status = self.statusBar()
        self.status.showMessage("Ready")
        self.status.setVisible(theme.SHOW_STATUS_BAR)
        self.stage_panel.status_message.connect(lambda m: self.status.showMessage(m, 8000))
        self.sensor_panel.status_message.connect(lambda m: self.status.showMessage(m, 8000))

        if not self._restore_window_geometry():
            self._apply_default_geometry()

    # ------------------------------------------------------------------
    # Window size and position
    # ------------------------------------------------------------------
    def _apply_default_geometry(self):
        """Centred, comfortably inside the primary screen."""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        width = min(1360, screen.width() - 80)
        height = min(820, screen.height() - 80)
        self.resize(width, height)
        self.move(screen.x() + (screen.width() - width) // 2,
                  screen.y() + max(0, (screen.height() - height) // 2))

    def _restore_window_geometry(self) -> bool:
        """
        Reopen at the size and place the window was last closed.

        Returns False when there is nothing saved, or when what was saved is
        no longer usable - a window remembered on a monitor that is no longer
        attached, or one larger than the screen now in use, would otherwise
        open off-screen or with its controls out of reach.
        """
        if not theme.WINDOW_GEOMETRY:
            return False

        try:
            raw = QByteArray.fromBase64(theme.WINDOW_GEOMETRY.encode("ascii"))
            if not self.restoreGeometry(raw):
                return False
        except Exception as e:
            print(f"[WARN] Saved window geometry could not be read ({e}); using defaults.")
            return False

        if self.isMaximized() or self.isFullScreen():
            return True

        available = None
        for screen in QGuiApplication.screens():
            area = screen.availableGeometry()
            overlap = area.intersected(self.frameGeometry())
            # A sliver on screen is not enough: the title bar has to be grabbable.
            if overlap.width() > 120 and overlap.height() > 40:
                available = area
                break
        if available is None:
            print("[WARN] Saved window position is off-screen; using defaults.")
            return False

        # Shrink to fit a smaller screen than the one it was saved on.
        size = self.size().boundedTo(available.size())
        if size != self.size():
            self.resize(size)
        frame = self.frameGeometry()
        if not available.contains(frame):
            frame.moveTo(max(available.x(), min(frame.x(), available.right() - frame.width())),
                         max(available.y(), min(frame.y(), available.bottom() - frame.height())))
            self.move(frame.topLeft())
        return True

    def _save_window_geometry(self):
        theme.WINDOW_GEOMETRY = bytes(self.saveGeometry().toBase64()).decode("ascii")
        theme.SPLITTER_SIZES = self.splitter.sizes()
        theme.save_settings()

    def reset_window_layout(self):
        """Escape hatch if the remembered geometry ever becomes unhelpful."""
        theme.WINDOW_GEOMETRY = None
        theme.SPLITTER_SIZES = None
        self.showNormal()
        self._apply_default_geometry()
        self.splitter.setSizes([268, 500])
        theme.save_settings()
        self.status.showMessage("Window layout reset", 4000)

    # ------------------------------------------------------------------
    # View menu: theme and font size
    # ------------------------------------------------------------------
    def _build_menu(self):
        view = self.menuBar().addMenu("&View")

        # The sensor range is reference information that never changes, so it
        # sits right-aligned in the menu bar rather than taking width away from
        # the controls underneath. The menu bar row is otherwise empty, which
        # is what lets the window be narrowed further.
        self.range_note = QLabel()
        self.range_note.setObjectName("Hint")
        self.range_note.setContentsMargins(0, 0, 10, 0)
        self.range_note.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.range_note.setToolTip(
            "Left: travel and maximum velocity of the linear stages.\n"
            "Right: measurement range of the fitted ATI Nano17 — readings beyond "
            "these values are outside the sensor's rating.")
        self._refresh_range_note()
        self.menuBar().setCornerWidget(self.range_note, Qt.Corner.TopRightCorner)

        theme_menu = view.addMenu("Theme")
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        for label, mode in theme.PALETTE_LABELS:
            action = QAction(label, self, checkable=True)
            action.setChecked(theme.MODE == mode)
            action.triggered.connect(lambda _, m=mode: self.set_theme(m))
            self._theme_group.addAction(action)
            theme_menu.addAction(action)

        size_menu = view.addMenu("Font size")
        self._scale_group = QActionGroup(self)
        self._scale_group.setExclusive(True)
        self._scale_actions = []
        for label, value in theme.FONT_SCALES:
            action = QAction(label, self, checkable=True)
            action.setChecked(abs(theme.SCALE - value) < 1e-6)
            action.triggered.connect(lambda _, v=value: self.set_font_scale(v))
            self._scale_group.addAction(action)
            size_menu.addAction(action)
            self._scale_actions.append((value, action))

        size_menu.addSeparator()
        custom = QAction("Custom…", self)
        custom.setToolTip("Type any percentage in the supported range.")
        custom.triggered.connect(self.pick_font_scale)
        size_menu.addAction(custom)

        view.addSeparator()
        bigger = QAction("Larger text", self)
        bigger.setShortcut(QKeySequence("Ctrl++"))
        bigger.triggered.connect(lambda: self.step_font_scale(+1))
        view.addAction(bigger)

        smaller = QAction("Smaller text", self)
        smaller.setShortcut(QKeySequence("Ctrl+-"))
        smaller.triggered.connect(lambda: self.step_font_scale(-1))
        view.addAction(smaller)

        view.addSeparator()
        self.readout_action = QAction("Readout strip", self, checkable=True)
        self.readout_action.setChecked(theme.SHOW_READOUT)
        self.readout_action.setToolTip(
            "The numbers under the chart. Hiding them gives that height to the "
            "plot; the chart legend appears instead so the traces stay identifiable.")
        self.readout_action.toggled.connect(self.set_readout_visible)
        view.addAction(self.readout_action)

        reset_layout = QAction("Reset window layout", self)
        reset_layout.setToolTip(
            "Forget the remembered window size, position and split, and go back "
            "to the default arrangement.")
        reset_layout.triggered.connect(self.reset_window_layout)
        view.addAction(reset_layout)

        self.status_bar_action = QAction("Status bar", self, checkable=True)
        self.status_bar_action.setChecked(theme.SHOW_STATUS_BAR)
        self.status_bar_action.setToolTip(
            "The message line along the bottom. Hiding it reclaims a row of "
            "height on a small screen; messages still go to the log file.")
        self.status_bar_action.toggled.connect(self.set_status_bar_visible)
        view.addAction(self.status_bar_action)

    def set_readout_visible(self, visible: bool):
        theme.SHOW_READOUT = bool(visible)
        self.sensor_panel.set_readout_visible(theme.SHOW_READOUT)
        theme.save_settings()

    def set_status_bar_visible(self, visible: bool):
        theme.SHOW_STATUS_BAR = bool(visible)
        self.status.setVisible(theme.SHOW_STATUS_BAR)
        theme.save_settings()

    def set_theme(self, mode):
        theme.set_mode(mode)
        self._apply_appearance()
        label = dict((m, l) for l, m in theme.PALETTE_LABELS).get(mode, mode)
        self.status.showMessage(f"{label.strip()} theme", 4000)

    def set_font_scale(self, value):
        theme.set_scale(value)
        self._apply_appearance()
        self._sync_scale_actions()
        self.status.showMessage(f"Text size {theme.SCALE * 100:.0f}%", 4000)

    def _sync_scale_actions(self):
        # Exclusive group won't allow "nothing checked", so relax it briefly.
        self._scale_group.setExclusive(False)
        for value, action in self._scale_actions:
            action.setChecked(abs(value - theme.SCALE) < 1e-6)
        self._scale_group.setExclusive(True)

    def step_font_scale(self, direction):
        self.set_font_scale(round(theme.SCALE + direction * theme.FONT_SCALE_STEP, 2))

    def pick_font_scale(self):
        lo = int(round(theme.FONT_SCALE_MIN * 100))
        hi = int(round(theme.FONT_SCALE_MAX * 100))
        value, ok = QInputDialog.getInt(
            self, "Text size", f"Text size ({lo}–{hi} %):",
            int(round(theme.SCALE * 100)), lo, hi, 1)
        if ok:
            self.set_font_scale(value / 100.0)

    def _refresh_range_note(self):
        """Stage envelope and sensor range, stated once for the whole window."""
        self.range_note.setText(
            f"{self.stage_panel.travel_summary()}      {theme.NANO17_RANGE_TEXT}")

    def _centre_range_note(self):
        """
        Qt gives a menu-bar corner widget only the height of its own text and
        pins it to the top of the bar, so the label reads as stuck to the
        window frame. Making it as tall as the menu bar lets its own
        AlignVCenter put the text on the same line as the menu titles.
        """
        self.range_note.setMinimumHeight(self.menuBar().sizeHint().height())

    def _apply_appearance(self):
        QApplication.instance().setStyleSheet(theme.stylesheet())
        self._refresh_range_note()
        self.range_note.setMinimumHeight(0)      # let the menu bar shrink first
        QTimer.singleShot(0, self._centre_range_note)
        self.stage_panel.restyle()
        self.sensor_panel.restyle()
        theme.save_settings()

    def closeEvent(self, event):
        try:
            self._save_window_geometry()
        except Exception as e:
            print(f"[WARN] Could not save the window layout: {e}")

        try:
            self.stage_panel.shutdown()
        except Exception:
            pass
        try:
            self.sensor_panel.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


class PauseDialog(QDialog):
    """Pause length in seconds or minutes; the value is stored in seconds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Pause")

        self.value = QDoubleSpinBox()
        self.value.setDecimals(2)
        self.value.setRange(0.1, 100000.0)
        self.value.setValue(10.0)
        self.value.setSingleStep(1.0)
        self.value.setMinimumWidth(120)

        self.unit = QComboBox()
        self.unit.addItems(["seconds", "minutes"])
        self.unit.currentIndexChanged.connect(self._unit_changed)

        self.summary = QLabel()
        self.summary.setObjectName("Hint")
        self.value.valueChanged.connect(self._refresh)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.value, 1)
        row.addWidget(self.unit)

        form = QFormLayout()
        form.addRow("Pause for:", row)
        form.addRow("", self.summary)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)
        self._refresh()

    def _unit_changed(self):
        # keep the same real duration when switching units
        if self.unit.currentText() == "minutes":
            self.value.setValue(self.value.value() / 60.0)
        else:
            self.value.setValue(self.value.value() * 60.0)
        self._refresh()

    def seconds(self) -> float:
        v = self.value.value()
        return v * 60.0 if self.unit.currentText() == "minutes" else v

    def _refresh(self):
        s = self.seconds()
        self.summary.setText(f"= {s:g} s" + (f"  ({s / 60.0:g} min)" if s >= 60 else ""))

    @staticmethod
    def get_seconds(parent=None):
        dialog = PauseDialog(parent)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.seconds()
        return None


class SequenceWorker(QThread):
    finished = pyqtSignal()
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    safety_stop = pyqtSignal(str)

    def __init__(self, stage_panel, steps):
        super().__init__()
        self.w = stage_panel
        self.steps = steps

    def _abort(self, index, message):
        msg = f"Step {index + 1}: {message}"
        self.status.emit(f"[SAFETY] {msg}")
        self.safety_stop.emit(msg)
        self.w.sequence_active = False

    def run(self):
        try:
            self.w.set_logging_active(True, self.w.sequence_name)

            for index, step in enumerate(self.steps):
                if not self.w.sequence_active:
                    break

                self.progress.emit(index)

                if step[0] == "pause":
                    pause_time = step[1]
                    self.status.emit(f"Pausing for {pause_time:g} s")
                    # sleep in slices so Stop stays responsive on long pauses
                    remaining = pause_time
                    while remaining > 0 and self.w.sequence_active:
                        chunk = min(0.2, remaining)
                        self.msleep(int(chunk * 1000))
                        remaining -= chunk
                    continue

                axes = step[1]
                controllers = []

                # 1) check every axis BEFORE anything starts moving
                for name, velocity, box_distance in axes:
                    controller = self.w._controller_for(name)
                    distance = self.w._signed(name, box_distance)
                    chk = controller.check_move(distance)
                    if not chk.ok:
                        self._abort(index, f"{name}: {chk.reason}")
                        break
                    controllers.append((controller, name, velocity, distance))
                else:
                    # 2) launch them (single axis or several at once)
                    started = []
                    try:
                        for controller, name, velocity, distance in controllers:
                            controller.setup_velocity(0, 10, velocity)
                            controller.move_by(distance)
                            started.append(controller)
                    except Exception as e:
                        for c in started:
                            try:
                                c.stop()
                            except Exception:
                                pass
                        self._abort(index, f"could not start motion: {e}")
                        break

                    if len(started) > 1:
                        self.status.emit(
                            "Moving together: "
                            + ", ".join(n.split("_")[0] for _, n, _, _ in controllers))

                    ok, reason = wait_until_all_stopped(
                        started, should_continue=lambda: self.w.sequence_active)
                    if not ok:
                        self._abort(index, reason)
                        break

                    hit = [n for c, n, _, _ in controllers if c.at_hard_limit()]
                    if hit:
                        self._abort(index, f"{', '.join(hit)}: hardware limit switch reached")
                        break

                if not self.w.sequence_active:
                    break

        except Exception as e:
            self.status.emit(f"[ERROR] Sequence failed: {e}")
            self.safety_stop.emit(f"Sequence aborted: {e}")

        finally:
            self.w.set_logging_active(False, self.w.sequence_name)
            self.finished.emit()


class TouchDataListener(QThread):
    sample = pyqtSignal(int, float, float)

    def __init__(self, touch_data_queue):
        super().__init__()
        self.q = touch_data_queue
        self._running = True

    def run(self):
        while self._running:
            try:
                item = self.q.get(timeout=0.5)
            except Exception:
                continue
            if not item:
                continue
            try:
                t_us, fz, pz = item
                self.sample.emit(int(t_us), float(fz), float(pz))
            except Exception:
                continue

    def stop(self):
        self._running = False


def _set_sleep_prevention(prevent: bool) -> None:
    """Keep Windows awake during a long sequence. No-op elsewhere."""
    if not hasattr(ctypes, "windll"):
        return
    try:
        flags = 0x80000000 | 0x00000002 if prevent else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def main():
    _set_sleep_prevention(True)

    try:
        # Same queues as before, now inside a single process.
        logging_queue = queue.Queue()
        touch_data_queue = queue.Queue(maxsize=200)

        # Started from a shortcut the working directory is not the app folder,
        # so resolve and create the data folders explicitly.
        paths.ensure_dirs()

        # Built without a console window, print() has nowhere to go; mirror it
        # to a file so the diagnostics survive.
        log_path = applog.start_logging()
        print(f"[INFO] Data folder: {paths.DATA_ROOT}")
        if log_path:
            print(f"[INFO] Log file: {log_path}")

        theme.load_settings()

        app = QApplication(sys.argv)
        from PyQt6.QtGui import QIcon
        from lib.paths import APP_ICON
        if os.path.exists(APP_ICON):
            app.setWindowIcon(QIcon(APP_ICON))
        app.setStyleSheet(theme.stylesheet())

        window = MainWindow(logging_queue, touch_data_queue)
        window.show()
        sys.exit(app.exec())

    finally:
        _set_sleep_prevention(False)


if __name__ == "__main__":
    main()
