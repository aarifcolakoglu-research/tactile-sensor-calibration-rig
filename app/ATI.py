import collections
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import replace as _dc_replace
from datetime import datetime

from PyQt6.QtGui import QGuiApplication, QAction, QActionGroup, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QSizePolicy,
    QInputDialog, QMessageBox, QFileDialog, QGroupBox, QDockWidget,
    QCheckBox, QDialog, QDoubleSpinBox, QSpinBox, QComboBox, QDialogButtonBox,
    QFormLayout, QProgressBar, QMenu, QStyle, QAbstractItemView, QLayout
)
from PyQt6.QtCore import pyqtSignal, QThread, QTimer, QSize, Qt, QByteArray, QRect, QPoint

from lib import applog, paths
from lib.set_touch_point import TouchConfig, TouchProbeRunner
from lib.stageController import (
    StageController,
    HomingAborted,
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
# Touch Point timing, fitted to a recorded calibration stream: see
# StagePanel._estimate_touch_seconds().
TOUCH_RETURN_STEP_OVERHEAD_S = 0.2
TOUCH_FIXED_OVERHEAD_S = 1.0

ETA_IDLE_TEXT = "Idle — press Run Sequence to start"

# A Touch Point step that fails to latch is retried this many times (the probe
# retracts itself between tries) before the sequence is safety-stopped.
TOUCH_STEP_RETRIES = 2
TOUCH_RETRY_PAUSE_S = 2.0   # let the sensor stream / stage settle before retrying

# Per-axis homing direction, remembered between runs. An axis whose '−' limit
# switch is dead can be set to "forward" so Home and Home All reference against
# the '+' switch instead, rather than grinding into an end stop it cannot see.
STAGE_CONFIG_FILE = paths.config_file("stages.json")

# Default homing end per axis, used only when config/stages.json has no entry.
# X_Stage's '−' switch is dead (see the README, "Known hardware issues"), so it
# homes from the '+' end out of the box.
DEFAULT_HOME_DIRECTIONS = {"X_Stage": "forward"}


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


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto the next line when the
    width runs out (Qt's flow-layout example), so a toolbar row never sets
    the window's minimum width."""

    def __init__(self, parent=None, spacing=5, hang=False):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)
        # hang=True: the first item is a caption (e.g. "Stages:"); lines
        # after the first start where the first line's second item does, so
        # the space under the caption stays empty (7 Oct 2026).
        self._hang = hang

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        if self._hang and len(self._items) > 1:
            widest = max(it.minimumSize().width() for it in self._items[1:])
            size.setWidth(max(size.width(),
                              self._items[0].sizeHint().width() + self.spacing() + widest))
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        """Fill lines left to right; each line's items are centred vertically
        on it, so a caption label sits level with the buttons."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        gap = self.spacing()
        visible = [it for it in self._items
                   if it.widget() is None or not it.widget().isHidden()]
        indent = 0
        if self._hang and len(visible) > 1:
            indent = visible[0].sizeHint().width() + gap
        lines, line, line_w = [], [], 0
        for item in visible:
            hint = item.sizeHint()
            start = 0 if not lines else indent      # where this line begins
            need = start + hint.width() if not line else line_w + gap + hint.width()
            if line and need > area.width():
                lines.append(line)
                line = []
                need = indent + hint.width()
            line.append((item, hint))
            line_w = need
        if line:
            lines.append(line)
        y = area.y()
        for i, ln in enumerate(lines):
            h = max(hint.height() for _, hint in ln)
            if not test_only:
                x = area.x() + (indent if i else 0)
                for item, hint in ln:
                    item.setGeometry(QRect(QPoint(x, y + (h - hint.height()) // 2), hint))
                    x += hint.width() + gap
            y += h + (gap if i < len(lines) - 1 else 0)
        return y - rect.y() + m.bottom()


class _SeqRow(QWidget):
    """A sequence-list row. Clicking anywhere on it selects the item (the child
    QLabel would otherwise swallow the click and nothing would highlight)."""

    def __init__(self, list_widget, item):
        super().__init__()
        self.setObjectName("SeqRow")
        # A plain QWidget subclass ignores `background` / `border` from a style
        # sheet unless this is set, which silently turned the running-step and
        # selection highlights in _restyle_seq_rows into a no-op.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._list = list_widget
        self._item = item

    def mousePressEvent(self, event):
        lst = self._list
        row = lst.row(self._item)
        mods = event.modifiers()
        idx = lst.model().index(row, 0)
        if mods & Qt.KeyboardModifier.ShiftModifier:
            anchor = lst.currentRow() if lst.currentRow() >= 0 else row
            lo, hi = sorted((anchor, row))
            lst.clearSelection()
            for r in range(lo, hi + 1):
                lst.item(r).setSelected(True)
            lst.setCurrentIndex(idx)
        elif mods & Qt.KeyboardModifier.ControlModifier:
            self._item.setSelected(not self._item.isSelected())
            lst.setCurrentIndex(idx)
        else:
            lst.clearSelection()
            self._item.setSelected(True)
            lst.setCurrentItem(self._item)
        super().mousePressEvent(event)


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

        # On the card itself: tick 'combine' on the axes that should move
        # together, then press '+ Combined' in the Sequence pane.
        self.sync_check = QCheckBox("combine")
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
        layout.addWidget(self.sync_check)
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
            except HomingAborted as e:
                # The operator's emergency stop, which reports its own outcome
                # - not a homing fault worth an error dialog.
                print(f"[HOME] {self.stage_name}: {e}")
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
            # A jog does not block, so nothing will release this: it counts
            # as in flight for as long as the move should take, plus margin
            # for acceleration. An emergency stop inside that window undoes
            # the jog; one after it does not.
            self.stage_controller.arm_return_point(
                hold=False, expected_s=abs(distance) / max(velocity, 1e-3) + 2.0)
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
    coarse_find_finished = pyqtSignal(object)
    estop_finished = pyqtSignal(object)   # list of per-axis result lines
    home_all_finished = pyqtSignal(str)
    status_message = pyqtSignal(str)
    touch_prompt = pyqtSignal(str)     # failed-touch operator dialog (reason)

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

        # ---- emergency stop (on_emergency_stop) ----
        # Every press bumps the generation; a worker that sees it change
        # knows a newer press has taken over. The lock makes "is a return
        # already running?" and the final clear_abort() atomic with respect
        # to a press, so an abort can never be left set with nobody to clear it.
        self._estop_lock = threading.Lock()
        self._estop_generation = 0
        self._estop_running = False
        self._estop_thread = None

        # ---- calibration-point bookkeeping ----
        self._cal_log = None               # open calibration-point CSV during a run
        self._cal_log_path = None
        self._touch_prompt_result = None   # operator's choice on a failed touch
        self._touch_prompt_done = threading.Event()

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

        # Apply the homing end for each axis: config/stages.json wins, otherwise
        # the built-in default (X homes from the '+' end — its '−' switch is dead).
        self.stage_config = load_stage_config()
        for c in self.stage_controls:
            saved = (self.stage_config.get(c.stage_name) or {}).get("home_direction")
            direction = saved if saved in ("reverse", "forward") \
                else DEFAULT_HOME_DIRECTIONS.get(c.stage_name)
            if direction in ("reverse", "forward"):
                try:
                    c.stage_controller.set_home_direction(direction)
                except Exception as e:
                    print(f"[WARN] {c.stage_name}: could not set home direction ({e})")
                c.refresh_home_direction()

        for c in self.stage_controls:
            c.addMotionRequested.connect(self.add_motion)
            c.homeFailed.connect(self._on_single_home_failed)
            c.homeDirectionChanged.connect(self._on_home_direction_changed)

        # ---- sequence list ----
        self.motion_list = QListWidget()
        self._running_step = -1
        self._pulse_on = False
        self.motion_list.setUniformItemSizes(True)
        self.motion_list.setSpacing(0)
        self.motion_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.motion_list.currentRowChanged.connect(lambda _: self._restyle_seq_rows())
        self.motion_list.itemSelectionChanged.connect(self._restyle_seq_rows)
        # Row widgets are built only for the rows in view (see _add_steps);
        # scrolling or resizing builds the ones that come into view.
        self.motion_list.verticalScrollBar().valueChanged.connect(
            lambda _: self._ensure_visible_rows())
        self.motion_list.verticalScrollBar().rangeChanged.connect(
            lambda *_: self._ensure_visible_rows())
        QShortcut(QKeySequence.StandardKey.Delete, self.motion_list,
                  activated=self._remove_selected)

        self.pause_button = QPushButton("+ Pause")
        self.pause_button.setToolTip("Append a timed pause to the sequence")
        self.pause_button.clicked.connect(self.add_pause)

        self.touch_step_button = QPushButton("+ Touch")
        self.touch_step_button.setToolTip(
            "Append a Touch Point step: Z seeks contact, latches pz as the "
            "reference, then optionally backs off. Needs Acquire when it runs.")
        self.touch_step_button.clicked.connect(self._add_touch_step)

        self.record_step_button = QPushButton("+ Record")
        self.record_step_button.setToolTip(
            "Append a Record Point step: logs the mean F/T over a short window "
            "at the current position to the calibration CSV (force calibration).")
        self.record_step_button.clicked.connect(self._add_record_step)

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

        # The row wraps onto a second line when the window is narrowed, so it
        # never holds the window's minimum width (6 Oct 2026).
        seq_tools = FlowLayout(spacing=5, hang=True)
        stages_caption = QLabel("Stages:")
        stages_caption.setObjectName("Hint")
        # one width for the row captions, so every row's buttons start on
        # the same line
        self._row_captions = [stages_caption]
        seq_tools.addWidget(stages_caption)
        seq_tools.addWidget(self.combined_button)
        seq_tools.addWidget(self.pause_button)
        seq_tools.addWidget(self.touch_step_button)
        seq_tools.addWidget(self.record_step_button)
        seq_tools.addWidget(self.move_up_button)
        seq_tools.addWidget(self.move_down_button)
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

        # The 'combine' ticks live on the axis cards themselves (Stages pane).
        eta_row = QHBoxLayout()
        eta_row.setSpacing(5)
        eta_row.addWidget(self.eta_label, 1)

        # No group box / title here — the dock is already titled "Sequence".
        seq_box = QWidget()
        seq_layout = QVBoxLayout()
        seq_layout.setContentsMargins(0, 0, 0, 0)
        seq_layout.setSpacing(5)
        seq_layout.addLayout(seq_tools)
        seq_layout.addWidget(self.motion_list, 1)
        seq_layout.addWidget(self.sequence_progress)
        seq_layout.addLayout(eta_row)
        seq_box.setLayout(seq_layout)

        # ---- stage-pane toolbar (a column beside the Z card) ----
        self.home_all_button = QPushButton("Home All\n(Z→X→Y)")
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

        self.find_surface_button = QPushButton("Find Surface\n(Coarse)")
        self.find_surface_button.setToolTip(
            "Wide, slower search from wherever Z is now - use this once after "
            "homing or a cold start, before Set Touch Point. Not a substitute "
            "for it: this only locates the surface roughly and does not set "
            "the touch point used by a sequence."
        )
        self.find_surface_button.clicked.connect(self.on_find_surface_coarse)

        self.set_touch_button = QPushButton("Set Touch\nPoint")
        self.set_touch_button.clicked.connect(self.on_set_touch_point)

        # Was "Abort" / on_abort_touch_point, scoped to just the touch probe.
        # Renamed and widened to a real emergency stop: it now also reaches a
        # running sequence's plain moves (those carry no force interlock of
        # their own) and a manual jog from a stage card,
        # both of which go through StageController.move_by() same as the
        # probe does. "Stop" was avoided because the Acquire panel already
        # has its own Stop button with an unrelated meaning; this one is
        # named for what it does everywhere in the app, not just here.
        self.emergency_stop_button = QPushButton("EMERGENCY\nSTOP")
        self.emergency_stop_button.setObjectName("Danger")
        self.emergency_stop_button.setToolTip(
            "Stop every stage's motion immediately, then return each one to "
            "where it was before its most recent move - whatever commanded "
            "that move: a sequence step, a manual jog, Set Touch Point or "
            "Find Surface. Always available, not just while the touch probe "
            "is running."
        )
        # Unlike Set Touch Point / Find Surface, this is never disabled - an
        # emergency stop has to be available at all times, including mid-jog
        # or mid-sequence, not just while a touch-probe pass owns the axes.
        self.emergency_stop_button.clicked.connect(self.on_emergency_stop)

        # ---- assembly: two panes the main window docks independently ----
        #   stages_pane   : the X/Y/Z cards + a Home All / Find Surface /
        #                   Set Touch Point / E-STOP column beside the Z card
        #   sequence_pane : the sequence list + name / Load / Run / Save
        # StagePanel itself is the logic owner and is never shown directly.
        self._stage_tool_buttons = (self.home_all_button, self.find_surface_button,
                                    self.set_touch_button, self.emergency_stop_button)

        stage_tools = self._stage_tools_layout = QVBoxLayout()
        for b in self._stage_tool_buttons:
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
            stage_tools.addWidget(b, 1)
        self._size_stage_tools()
        self._size_row_captions()

        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(8)
        for c in self.stage_controls:
            cards_row.addWidget(c)
        cards_row.addSpacing(theme.px(6))
        cards_row.addLayout(stage_tools)

        cards_holder = QWidget()
        cards_holder.setLayout(cards_row)
        cards_holder.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # Block hugs the top with next to no margin; no height cap so the
        # Stages/Graph divider still drags, and a floor so Home All can't clip.
        stages_layout = QVBoxLayout()
        stages_layout.setContentsMargins(2, 0, 2, 0)
        stages_layout.setSpacing(0)
        stages_layout.addWidget(cards_holder, 0,
                                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        stages_layout.addStretch(1)
        self.stages_pane = QWidget()
        self.stages_pane.setLayout(stages_layout)
        self._stages_block_h = cards_holder.sizeHint().height() + theme.px(2)
        self.stages_pane.setMinimumHeight(self._stages_block_h)

        # Wraps like the step rows above (7 Oct 2026): as one fixed row the
        # name box + Load + Run Sequence + Save held the pane at ~510 px.
        seq_run_tools = FlowLayout(spacing=6)
        seq_run_tools.addWidget(self.sequence_name_input)
        seq_run_tools.addWidget(self.load_sequence_button)
        seq_run_tools.addWidget(self.run_sequence_button)
        seq_run_tools.addWidget(self.save_sequence_button)

        sequence_layout = QVBoxLayout()
        sequence_layout.setContentsMargins(3, 2, 3, 3)
        sequence_layout.setSpacing(5)
        sequence_layout.addWidget(seq_box, 1)
        sequence_layout.addLayout(seq_run_tools)
        self.sequence_pane = QWidget()
        self.sequence_pane.setLayout(sequence_layout)

        # StagePanel stays a QWidget (it carries the signals and owns the logic)
        # but is never shown itself — its content lives in the two panes above,
        # which the main window puts into docks.
        self.setLayout(QVBoxLayout())

        self.touch_finished.connect(self._on_touch_finished)
        self.coarse_find_finished.connect(self._on_coarse_find_finished)
        self.estop_finished.connect(self._on_estop_finished)
        self.home_all_finished.connect(self._on_home_all_finished)
        self.touch_prompt.connect(self._show_touch_prompt)

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

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(500)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        self.position_timer = QTimer(self)
        self.position_timer.setInterval(1000)
        self.position_timer.timeout.connect(self._refresh_positions)
        self.position_timer.start()
        self._refresh_positions()

        QTimer.singleShot(400, self._startup_homing_check)

    def _dialog_parent(self):
        """StagePanel is never shown itself (its content is in the docks), so a
        dialog parented to it would open with no owner and can hide behind the
        main window. Parent to the real top-level window instead."""
        return self.stages_pane.window()

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------
    def _stages_busy(self) -> bool:
        if self.sequence_active or self._touch_running or self._estop_running:
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
        self._size_stage_tools()
        self._size_row_captions()
        self.sequence_name_input.setMinimumWidth(theme.px(200))
        self.sequence_name_input.setMaximumWidth(theme.px(280))
        self.sequence_progress.setFixedHeight(theme.px(8))
        for i in range(self.motion_list.count()):
            item = self.motion_list.item(i)
            item.setSizeHint(QSize(0, theme.px(SEQUENCE_ROW_HEIGHT)))
            widget = self.motion_list.itemWidget(item)
            if widget:
                label = widget.layout().itemAt(0).widget()
                label.setText(self._row_html(item.data(STEP_ROLE)))
                label.setStyleSheet(f"font-size: {theme.px(12)}px;")
        self._restyle_seq_rows()

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
        QMessageBox.critical(self._dialog_parent(), f"{stage_name}: Homing Failed", message)

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
            self._dialog_parent(), "Reference Required",
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
            QMessageBox.information(self._dialog_parent(), "Busy",
                                    "Stages are busy; wait for the current operation to finish.")
            return

        self._set_controls_enabled(False)
        self.status_message.emit("Homing Z → X → Y …")

        gen = self._estop_generation

        def worker():
            msgs = []
            for name in HOME_ORDER:
                if self._estop_generation != gen:
                    # An emergency stop happened: never go on to home the
                    # next axis behind the operator's back.
                    msgs.append("Remaining axes not homed: EMERGENCY STOP")
                    break
                ctrl = self._controller_for(name)
                try:
                    end = "'+'" if ctrl.home_direction == "forward" else "'−'"
                    print(f"[HOME] {name}: homing towards the {end} switch "
                          f"({ctrl.home_direction})...")
                    ctrl.home()
                    pos = ctrl.get_position_mm()
                    msgs.append(f"{name}: homed (pos {pos:.3f} mm)" if pos is not None
                                else f"{name}: homed")
                except HomingAborted:
                    msgs.append(f"{name}: homing interrupted by EMERGENCY STOP")
                    break
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

    def _size_row_captions(self) -> None:
        caps = getattr(self, "_row_captions", [])
        for c in caps:
            c.setMinimumWidth(0)
            c.setMaximumWidth(16777215)
        if caps:
            w = max(c.sizeHint().width() for c in caps)
            for c in caps:
                c.setFixedWidth(w)

    def _size_stage_tools(self) -> None:
        """
        The Home All / Find Surface / Set Touch Point / EMERGENCY STOP
        column: equal heights, equal gaps, top and bottom level with the
        axis cards' FRAME.

        The buttons stretch (stretch=1, Expanding) to fill whatever height
        the row settles at, so they track the cards live. What they must
        not fill is the strip above the cards' frame: a QGroupBox's title
        sits in its margin-top, straddling the border, so the widget's top
        edge is higher than the frame the eye lines things up with. Without
        this top margin the column started level with the "X Stage" title
        and stood taller than the cards. Re-run from restyle() because every
        number here scales with the interface.
        """
        layout = self._stage_tools_layout
        layout.setContentsMargins(0, theme.px(theme.GROUPBOX_MARGIN_TOP), 0, 0)
        layout.setSpacing(theme.px(6))
        for b in self._stage_tool_buttons:
            b.setFixedWidth(theme.px(104))
            # A floor so a short row cannot squash a two-line label.
            b.setMinimumHeight(theme.px(32))

    def _set_controls_enabled(self, enabled: bool):
        # While an emergency stop is driving axes back, nothing re-enables
        # the controls - not a Home All or touch pass finishing underneath
        # it. _on_estop_finished() does that once the return is done.
        if enabled and self._estop_running:
            return
        self.home_all_button.setEnabled(enabled)
        self.run_sequence_button.setEnabled(enabled)
        self.find_surface_button.setEnabled(enabled)
        self.set_touch_button.setEnabled(enabled)
        for c in self.stage_controls:
            c.home_button.setEnabled(enabled)
            c.set_zero_button.setEnabled(enabled)
            c.run_motion_button.setEnabled(enabled)

    def _on_home_all_finished(self, summary: str):
        self._set_controls_enabled(True)
        self._refresh_positions()
        if "EMERGENCY STOP" in summary:
            # The emergency stop reports its own outcome; a second
            # "Homing Incomplete" error on top would read as a fault.
            self.status_message.emit("Homing interrupted by emergency stop")
            print("[HOME] " + summary.replace("\n", " | "))
        elif "FAILED" in summary:
            self.status_message.emit("Homing incomplete")
            QMessageBox.critical(self._dialog_parent(), "Homing Incomplete", summary)
        else:
            self.status_message.emit("Homing complete")
            QMessageBox.information(self._dialog_parent(), "Homing Complete", summary)

    # ------------------------------------------------------------------
    # Touch probe
    # ------------------------------------------------------------------
    def _on_touch_sample(self, time_us: int, fz: float, pz: float):
        self.latest_time_us = time_us
        self.latest_fz = fz
        self.latest_pz = pz

    def _get_latest_touch_sample(self):
        # Prefer the listener thread's own copy — it is updated the instant a
        # sample arrives, independent of the GUI event loop, which can lag
        # during a long sequence and make the stream look stale.
        direct = self.touch_listener.latest_sample()
        if direct is not None:
            return direct
        if self.latest_time_us is None or self.latest_fz is None or self.latest_pz is None:
            return None
        return (int(self.latest_time_us), float(self.latest_fz), float(self.latest_pz))

    @staticmethod
    def _touch_config():
        """The touch-probe settings, shared by the manual button and the
        ``Touch Point`` sequence step so both behave identically."""
        return TouchConfig(
            # Search range: covers the parking clearance between successive
            # touches plus margin; a search only runs this far when it finds
            # nothing, so the extra range costs nothing on a normal touch.
            z_max_mm=9.0,
            # One approach speed. A two-stage (coarse/fine) approach was
            # evaluated in tests/test_two_stage_touch.py: on a stiff specimen
            # at 100 Hz the force rises by more than the pre-trigger between
            # consecutive samples, so the stage cannot slow down before it has
            # already pressed. fine_vel_mm_s and pre_trigger_n stay in place,
            # but with equal speeds the two-stage path is inert.
            approach_vel_mm_s=0.15,
            fine_vel_mm_s=0.15,
            pre_trigger_n=0.005,
            overtravel_mm=0.2,
            retract_mm=0.8,
            retract_emergency_mm=1.0,
            return_vel_mm_s=0.15,       # back toward the surface: stays slow
            return_step_mm=0.02,
            return_tol_mm=0.01,
            # Moves away from the surface only (post-touch retract and a
            # step's "retract" option); they are not part of any measurement,
            # so they run faster.
            retract_vel_mm_s=1.0,
            f_touch_n=0.015,
            confirm_ms=50,
            f_abs_limit_n=0.08,
            dFdt_limit_n_s=1.0,
            baseline_s=0.6,
            # Bounds the whole approach loop: a full z_max_mm search without
            # contact takes 9.0 / 0.15 = 60 s, plus margin for stop/settle.
            probe_timeout_s=70.0,
            # The F/T stream over Ethernet occasionally drops a sample; 0.5 s
            # was too tight in long runs.
            sample_stale_s=1.5,
        )

    def _coarse_find_config(self) -> TouchConfig:
        """
        A wide search that locates the surface from wherever Z currently sits
        (e.g. a cold start with Z homed to 0), beyond the envelope of the
        regular Touch Point.

        Every safety guard (f_touch_n, f_abs_limit_n, dFdt_limit_n_s,
        sample_stale_s, confirm_ms, ...) is inherited unchanged from
        _touch_config(); only the search range, the speeds and the matching
        timeout differ:

        - z_max_mm is capped to the travel left in front of the stage from its
          CURRENT position, so a coarse find cannot overrun the stage.
        - probe_timeout_s is z_max_mm at the slower of the two speeds plus a
          fixed margin (the worst case if contact is never felt).
        - approach_vel_mm_s=1.0 crosses empty air fast; fine_vel_mm_s=0.5
          takes over once pre_trigger_n is felt. Run it once without a
          specimen before relying on it near delicate hardware. This pass only
          needs to stop safely and report roughly where the surface is
          (TouchResult.last_pz); the force and force-slope guards protect the
          specimen at either speed.
        """
        fine = self._touch_config()

        travel = getattr(self.stageZ_controller, "travel_mm", None)
        pos = self.stageZ_controller.get_position_mm() if hasattr(
            self.stageZ_controller, "get_position_mm") else None

        if travel is None or pos is None:
            # Position unknown (not referenced yet, say) - fall back to the
            # fine envelope rather than guess at a distance that could
            # overrun the stage.
            z_max_mm = fine.z_max_mm
        else:
            z_max_mm = max(0.0, float(travel) - SAFETY_MARGIN_MM - float(pos))

        approach_vel_mm_s = 1.0
        fine_vel_mm_s = 0.5
        slowest = min(approach_vel_mm_s, fine_vel_mm_s)
        probe_timeout_s = (z_max_mm / slowest if slowest > 0 else 0.0) + 10.0

        return _dc_replace(
            fine,
            z_max_mm=z_max_mm,
            approach_vel_mm_s=approach_vel_mm_s,
            fine_vel_mm_s=fine_vel_mm_s,
            probe_timeout_s=probe_timeout_s,
        )

    def _wait_sensor_fresh(self, timeout_s, should_continue):
        """Block until the sensor timestamp advances (stream alive) or timeout."""
        def _t():
            s = self.touch_listener.latest_sample()
            return s[0] if s else self.latest_time_us
        start = _t()
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if not should_continue():
                return False
            now = _t()
            if now is not None and now != start:
                return True
            time.sleep(0.05)
        now = _t()
        return now is not None and now != start

    def _run_touch_step(self, params, should_continue, step_index=0):
        """Run one ``Touch Point`` step on the SequenceWorker thread (blocking).
        Retries a failed probe TOUCH_STEP_RETRIES times (it retracts itself
        between tries), then asks the operator: retry / skip / abort. Every
        attempt is a row in the calibration CSV.
        Returns (ok, message); ``should_continue()`` False aborts."""
        if self.latest_time_us is None and self.touch_listener.latest_sample() is None:
            return False, "Touch Point needs live sensor data — press Acquire first"

        class _Abort:
            @staticmethod
            def is_set():
                return not should_continue()

        retract = float(params.get("retract_mm", 0.0) or 0.0)
        last_reason = "unknown"

        # Each failed probe approaches +z_max and only partly retracts, so a run
        # of failures would walk Z forward into the sample. Remember where the
        # step started and return there on any non-success exit.
        try:
            z_start = self.stageZ_controller.get_position_mm()
        except Exception:
            z_start = None

        def _z_home():
            if z_start is None:
                return
            try:
                cur = self.stageZ_controller.get_position_mm()
                if cur is not None and abs(cur - z_start) > 0.02:
                    self.stageZ_controller.setup_velocity(
                        0, 10, self._touch_config().return_vel_mm_s)
                    self.stageZ_controller.move_by(z_start - cur)
                    wait_until_all_stopped([self.stageZ_controller],
                                           should_continue=lambda: True)
            except Exception as e:
                print(f"[TOUCH] could not return Z to the step start: {e}")

        while True:
            for attempt in range(1, TOUCH_STEP_RETRIES + 2):
                if not should_continue():
                    return False, "stopped before Touch Point"
                try:
                    result = TouchProbeRunner(
                        stage_z=self.stageZ_controller,
                        get_latest_sample=self._get_latest_touch_sample,
                        cfg=self._touch_config(),
                        abort_event=_Abort(),
                        status_cb=lambda m: print(f"[TOUCH] {m}"),
                    ).run()
                except Exception as e:
                    result = None
                    last_reason = f"probe crashed: {e}"

                if result is not None and result.ok:
                    self.touch_pz = result.touch_pz
                    ft = self.touch_listener.ft_window(1.0)
                    self._write_cal_row("touch", step_index, ft=ft,
                                        touch_pz=result.touch_pz, touch_ok=True,
                                        touch_note=f"attempt {attempt}")

                    if retract > 0 and should_continue():
                        try:
                            self.stageZ_controller.setup_velocity(
                                0, 10, self._touch_config().retract_vel_mm_s)
                            self.stageZ_controller.move_by(-abs(retract))
                            wait_until_all_stopped(
                                [self.stageZ_controller],
                                should_continue=lambda: should_continue())
                        except Exception as e:
                            return False, f"Touch retract failed: {e}"

                    return True, (f"Touch point pz={result.touch_pz:.4f} mm"
                                  + (f"  (attempt {attempt})" if attempt > 1 else "")
                                  + (f", retracted {retract:g} mm" if retract > 0 else ""))

                if result is not None:
                    last_reason = result.reason
                self._write_cal_row("touch-fail", step_index, touch_ok=False,
                                    touch_note=f"attempt {attempt}: {last_reason}")
                if not should_continue():
                    return False, "stopped during Touch Point"
                if attempt <= TOUCH_STEP_RETRIES:
                    self.status_message.emit(
                        f"Touch failed ({last_reason}) — retry {attempt}/{TOUCH_STEP_RETRIES}")
                    if not self._sleep_while(TOUCH_RETRY_PAUSE_S, should_continue):
                        return False, "stopped during Touch Point"
                    self._wait_sensor_fresh(5.0, should_continue)

            # All automatic tries used up — hand it to the operator. Acquisition
            # keeps running (the dialog is a nested event loop), so the sample
            # rate is unaffected.
            choice = self._ask_touch_operator(last_reason, should_continue)
            if choice == "retry":
                self.status_message.emit("Touch Point — operator asked to retry")
                _z_home()
                self._wait_sensor_fresh(5.0, should_continue)
                continue
            if choice == "skip":
                _z_home()
                self._write_cal_row("touch-skip", step_index, touch_ok=False,
                                    touch_note=f"operator skipped: {last_reason}")
                return True, f"Touch Point skipped by operator ({last_reason})"
            _z_home()
            return False, f"Touch failed, operator aborted: {last_reason}"

    def _run_record_step(self, params, should_continue, step_index=0):
        """Capture one force-calibration point: mean F/T over a short window at
        the current (settled) position. Writes a calibration CSV row. (ok, msg)."""
        if self.latest_time_us is None and self.touch_listener.latest_sample() is None:
            return False, "Record Point needs live sensor data — press Acquire first"
        window = float(params.get("window_s", 2.0) or 2.0)
        label = params.get("label") or "force"
        if not self._sleep_while(window, should_continue):
            return False, "stopped during Record Point"
        ft = self.touch_listener.ft_window(window)
        if ft is None:
            return False, "Record Point: no sensor samples in the window"
        self._write_cal_row(label, step_index, ft=ft,
                            touch_pz=self.touch_pz, touch_note=f"window {window:g}s")
        return True, (f"Recorded '{label}': fz={ft['fz_N']:.4f} N "
                      f"(±{ft['fz_sd_N']:.4f}, n={ft['n_samples']})")

    def _ask_touch_operator(self, reason, should_continue):
        """Block the worker thread until the operator answers the failed-touch
        dialog (shown on the GUI thread). Returns 'retry' | 'skip' | 'abort'."""
        self._touch_prompt_result = None
        self._touch_prompt_done.clear()
        self.touch_prompt.emit(reason)
        while not self._touch_prompt_done.wait(0.2):
            if not should_continue():
                return "abort"
        return self._touch_prompt_result or "abort"

    def _show_touch_prompt(self, reason):
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Touch Point failed")
        box.setText(f"Touch Point failed after {TOUCH_STEP_RETRIES + 1} tries:\n{reason}")
        box.setInformativeText(
            "Acquisition is still running. Fix the probe / clear the obstruction, "
            "then choose:")
        b_retry = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Skip this point", QMessageBox.ButtonRole.DestructiveRole)
        b_abort = box.addButton("Abort sequence", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_retry)
        box.exec()
        clicked = box.clickedButton()
        self._touch_prompt_result = (
            "retry" if clicked is b_retry else
            "abort" if clicked is b_abort else "skip")
        self._touch_prompt_done.set()

    # ------------------------------------------------------------------
    # Calibration-point log: one clean row per data point (Touch Point and
    # Record Point steps), so point-wise calibrations do not need the main
    # high-rate CSV to be averaged by hand.
    # ------------------------------------------------------------------
    CAL_COLUMNS = (
        ["iso_time", "seq_step", "label"]
        + ["n_samples", "window_s",
           "fx_N", "fy_N", "fz_N", "tx_Nmm", "ty_Nmm", "tz_Nmm", "fz_sd_N",
           "px_mm", "py_mm", "pz_mm",
           "touch_pz_mm", "touch_ok", "touch_note"]
    )

    # A '#'-prefixed legend written as line 1 so the file explains itself when
    # opened. pandas: read_csv(path, comment='#').
    CAL_LEGEND = (
        "# ATI Rig calibration points.  "
        "label: touch = displacement point (touch_pz_mm) | "
        "force / <your label> = force point (fz_N) | "
        "touch-fail / touch-skip = failed / operator-skipped attempt.  "
        "fx_N..tz_Nmm = mean ATI Nano17 force[N] / torque[N.mm] over the window "
        "(F/T = 6-axis force/torque).  "
        "fz_sd_N = std-dev of fz in the window: low = steady reading = trustworthy point, "
        "high = not settled / vibration -> lengthen the Pause before it.  "
        "n_samples / window_s = points averaged and window length.  "
        "px/py/pz_mm = mean stage position."
    )

    def _open_cal_log(self, sequence_name):
        """output/<name>_calibration_<ts>.csv — Touch Point and Record Point
        steps append one row each."""
        self._close_cal_log()
        try:
            paths.ensure_dirs()
            stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_"
                           for c in (sequence_name or "sequence").strip()) or "sequence"
            self._cal_log_path = os.path.join(
                paths.OUTPUT_DIR, f"{safe}_calibration_{stamp}.csv")
            self._cal_log = open(self._cal_log_path, "a", encoding="utf-8", buffering=1)
            self._cal_log.write(self.CAL_LEGEND + "\n")
            self._cal_log.write(",".join(self.CAL_COLUMNS) + "\n")
            self.status_message.emit(
                f"Calibration points -> {os.path.basename(self._cal_log_path)}")
        except Exception as e:
            print(f"[CAL] could not open calibration log: {e}")
            self._cal_log = None

    def _write_cal_row(self, label, seq_step, ft=None,
                       touch_pz=None, touch_ok=None, touch_note=""):
        if self._cal_log is None:
            return
        ft = ft or {}

        def g(k, fmt="{:.5f}"):
            v = ft.get(k)
            return "" if v is None else fmt.format(v)

        row = {
            "iso_time": datetime.now().isoformat(timespec="milliseconds"),
            "seq_step": seq_step,
            "label": str(label).replace(",", ";"),
            "n_samples": ft.get("n_samples", ""),
            "window_s": ft.get("window_s", ""),
            "fx_N": g("fx_N"), "fy_N": g("fy_N"), "fz_N": g("fz_N"),
            "tx_Nmm": g("tx_Nmm"), "ty_Nmm": g("ty_Nmm"), "tz_Nmm": g("tz_Nmm"),
            "fz_sd_N": g("fz_sd_N", "{:.6f}"),
            "px_mm": g("px_mm"), "py_mm": g("py_mm"), "pz_mm": g("pz_mm"),
            "touch_pz_mm": "" if touch_pz is None else f"{touch_pz:.5f}",
            "touch_ok": "" if touch_ok is None else str(int(bool(touch_ok))),
            "touch_note": str(touch_note or "").replace(",", ";"),
        }
        try:
            self._cal_log.write(
                ",".join(str(row.get(c, "")) for c in self.CAL_COLUMNS) + "\n")
        except Exception as e:
            print(f"[CAL] could not write calibration row: {e}")

    def _close_cal_log(self):
        if self._cal_log is not None:
            try:
                self._cal_log.close()
            except Exception:
                pass
        self._cal_log = None
        self._cal_log_path = None

    def _begin_touch_pass(self):
        """Common start of Set Touch Point / Find Surface: Z is theirs until
        _touch_pass_done(), and an emergency stop brings it back here."""
        self.stageZ_controller.arm_return_point(hold=True)
        self.find_surface_button.setEnabled(False)
        self.set_touch_button.setEnabled(False)
        self.run_sequence_button.setEnabled(False)
        self._touch_abort.clear()
        self._touch_running = True

    def _touch_pass_done(self):
        self._touch_running = False
        if self._estop_running:
            # The emergency stop owns Z's return point and the controls now.
            return
        self.stageZ_controller.release_return_point()
        self.find_surface_button.setEnabled(True)
        self.set_touch_button.setEnabled(True)
        self.run_sequence_button.setEnabled(True)

    def on_find_surface_coarse(self):
        """
        Wide search from the current Z position - see _coarse_find_config().

        Shares _touch_running / _touch_abort / _touch_thread with Set Touch
        Point: the two are mutually exclusive (only one probe motion at a
        time) and E-STOP already targets those, so reusing them means it
        works for this pass too without any extra wiring.
        """
        if self._touch_running:
            return

        if self.latest_time_us is None:
            QMessageBox.warning(self._dialog_parent(), "No Data",
                                "No touch data received yet. Start acquisition first.")
            return

        self._begin_touch_pass()
        self.status_message.emit("Coarse surface search running…")

        cfg = self._coarse_find_config()

        def status_cb(msg: str):
            print(f"[FIND] {msg}")

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
                    f"[FIND] result: ok={result.ok} reason={result.reason} "
                    f"touch_pz={result.touch_pz} last_pz={result.last_pz} "
                    f"z_max_mm={cfg.z_max_mm:.2f} approach_vel_mm_s={cfg.approach_vel_mm_s:g}"
                )
            except Exception as e:
                class _Fail:
                    ok = False
                    reason = f"Coarse find crashed: {e}"
                    touch_pz = None
                    last_pz = None
                result = _Fail()

            self.coarse_find_finished.emit(result)

        self._touch_thread = threading.Thread(target=worker, daemon=True)
        self._touch_thread.start()

    def _on_coarse_find_finished(self, result):
        try:
            where = result.touch_pz if result.touch_pz is not None else result.last_pz
            if result.ok:
                self.status_message.emit(f"Surface found near pz={where:.4f} mm - now Set Touch Point")
                QMessageBox.information(
                    self._dialog_parent(), "Surface Found",
                    f"Contact near pz={where:.4f} mm. The stage has retracted "
                    f"clear. Run Set Touch Point next for a precise reading."
                )
            else:
                self.status_message.emit("Coarse find did not confirm contact")
                detail = f" (last position pz={where:.4f} mm)" if where is not None else ""
                QMessageBox.warning(
                    self._dialog_parent(), "Coarse Find",
                    f"{result.reason}{detail}\n\n"
                    f"A hard-limit stop here still means something was found "
                    f"roughly at that position - the stage retracted safely. "
                    f"\"Reached Z_MAX without touch\" means nothing was found "
                    f"within reach from the current position."
                )
        finally:
            self._touch_pass_done()

    def on_set_touch_point(self):
        if self._touch_running:
            return

        if self.latest_time_us is None:
            QMessageBox.warning(self._dialog_parent(), "No Data",
                                "No touch data received yet. Start acquisition first.")
            return

        self._begin_touch_pass()
        self.status_message.emit("Touch probe running…")

        cfg = self._touch_config()

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
                QMessageBox.information(self._dialog_parent(), "Touch Point Set",
                                        f"Touch point set at pz={result.touch_pz:.4f} mm")
            else:
                self.status_message.emit("Touch failed")
                QMessageBox.warning(self._dialog_parent(), "Touch Failed", f"{result.reason}")
        finally:
            self._touch_pass_done()

    def _axes(self):
        return (("X", self.stageX_controller), ("Y", self.stageY_controller),
                ("Z", self.stageZ_controller))

    def on_emergency_stop(self):
        """
        Stop every stage NOW, then put back whatever the interrupted command
        had moved - to where each axis was when that command began.

        "The command" is what the operator started: a jog, Home / Home All,
        a sequence step, Set Touch Point, Find Surface. Only axes that
        command was moving are returned; an axis sitting still is just
        stopped. A home that is interrupted before it reaches its limit
        switch leaves the counter untouched, so the axis goes back and gets
        its reference back too (the controller clears its homed bit the
        moment homing starts - that was the '?' after an interrupted Home
        All). If homing had already reached the switch, the controller may
        have re-zeroed, and the axis is left where it stopped instead.

        Phase 1, here on the GUI thread, under _estop_lock:
          - all three axes abort() - an abrupt, non-blocking stop, so they
            halt together rather than one after the other, and every
            blocking loop in StageController gives up;
          - every worker that drives an axis is told to stand down.
        Phase 2, _estop_worker(), off the GUI thread: wait for those workers
        to actually exit (so none of them issues a stop() over the return
        move), then drive the axes back.

        Pressing it AGAIN while that return is running cancels the return:
        everything stops where it is.
        """
        print("[ESTOP] emergency stop requested")
        self.status_message.emit("EMERGENCY STOP")

        with self._estop_lock:
            self._estop_generation += 1
            gen = self._estop_generation

            for name, ctrl in self._axes():
                try:
                    ctrl.abort()
                except Exception as e:
                    print(f"[ESTOP] {name}: abort failed: {e}")

            self._touch_abort.set()
            was_sequence = self.sequence_active
            self.sequence_active = False

            if self._estop_running:
                print("[ESTOP] pressed again during the return - return cancelled")
                self.status_message.emit(
                    "EMERGENCY STOP pressed again - return cancelled, stages left where they stopped")
                start_worker = False
            else:
                pending = [(name, ctrl, ctrl.return_point_mm, ctrl.return_point_referenced)
                           for name, ctrl in self._axes() if ctrl.return_point_pending()]
                self._estop_running = True
                start_worker = True

        self._set_controls_enabled(False)
        if was_sequence:
            self.stop_sequence()

        if start_worker:
            self._estop_thread = threading.Thread(
                target=self._estop_worker, args=(gen, pending), daemon=True)
            self._estop_thread.start()

    def _join_motion_workers(self, timeout_s: float = 5.0) -> None:
        """Wait for every thread that commands stage motion to exit."""
        threads = [self._touch_thread, self._home_all_thread]
        threads += [getattr(c, "_home_thread", None) for c in self.stage_controls]
        for t in threads:
            if t is not None and t is not threading.current_thread() and t.is_alive():
                t.join(timeout_s)
        sw = self.sequence_worker
        if sw is not None and sw.isRunning():
            sw.wait(int(timeout_s * 1000))

    def _estop_worker(self, gen, pending):
        self._join_motion_workers()
        lines = []

        with self._estop_lock:
            superseded = gen != self._estop_generation
            if not superseded:
                for _, ctrl in self._axes():
                    ctrl.clear_abort()

        for name, ctrl, target, was_referenced in pending:
            if gen != self._estop_generation:
                lines.append(f"{name}: return cancelled (EMERGENCY STOP pressed again)")
                continue
            try:
                ok, reason = ctrl.return_to_point(target, was_referenced)
            except Exception as e:
                ok, reason = False, f"return failed: {e}"
            lines.append(f"{name}: {reason}")
            print(f"[ESTOP] {name}: {'ok' if ok else 'NOT returned'} - {reason}")

        with self._estop_lock:
            for _, ctrl in self._axes():
                ctrl.clear_abort()
                # Whatever happened, the command is over: a later press must
                # not try to "undo" it again.
                ctrl.release_return_point()
            self._estop_running = False

        self.estop_finished.emit(lines)

    def _on_estop_finished(self, lines):
        self._set_controls_enabled(True)
        self.set_touch_button.setEnabled(True)
        self.find_surface_button.setEnabled(True)
        if not lines:
            self.status_message.emit("Emergency stop complete - nothing was moving; stages stopped")
            return
        self.status_message.emit("Emergency stop complete")
        QMessageBox.information(self._dialog_parent(), "Emergency Stop",
                                "Stages stopped.\n\n" + "\n".join(lines))

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
        if kind == "touch":
            r = float(step[1].get("retract_mm", 0.0) or 0.0)
            detail = "seek Z contact, set pz reference"
            if r:
                detail += f", then back off {r:g} mm"
            return "Touch Point", detail
        if kind == "record":
            return "Record Point", f"log mean F/T  ({step[1].get('label', 'force')})"

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
        elif step[0] == "touch":
            colour = theme.OK
        elif step[0] == "record":
            colour = theme.ACCENT_DARK
        elif len(step[1]) > 1:
            colour = theme.WARN
        else:
            colour = theme.ACCENT
        return (f"<b><span style='color:{colour}'>{title}</span></b>"
                f"<span style='color:{theme.TEXT}'>  {detail}</span>")

    def _sequence_running(self) -> bool:
        """A run owns the list: the worker is executing a copy of it, and
        _on_sequence_progress() highlights rows by index. Editing it now would
        point the highlight at the wrong step - or at no row at all, which
        raised inside a Qt slot, and an unhandled exception in a PyQt6 slot
        aborts the whole application mid-run."""
        worker = getattr(self, "sequence_worker", None)
        return bool(getattr(self, "sequence_active", False)
                    or (worker is not None and worker.isRunning()))

    def _refuse_edit_while_running(self) -> bool:
        if self._sequence_running():
            self.status_message.emit("The sequence cannot be edited while it runs - Stop it first")
            return True
        return False

    def _add_steps(self, steps):
        """Many rows at once (Load, reorder, remove).

        add_step() on its own restyles every row and re-costs the whole
        sequence, so adding n rows one by one did that n times: loading the
        longest (675-step) sequence froze the window for ~42 s, and with
        it the charts, the stage buttons and EMERGENCY STOP. Here the rows are
        built with painting off and styled and costed once: ~0.3 s.

        7 Oct 2026: still ~1 s (offscreen) to several on the lab PC for a
        600-step file, nearly all of it one row widget per step, during which
        the graph froze. Now only the items are added here; a row's widget is
        built when the row comes into view (_ensure_visible_rows), so a load
        costs ~40 widgets whatever the length.
        """
        ml = self.motion_list
        ml.setUpdatesEnabled(False)
        try:
            for s in steps:
                self.add_step(s, refresh=False, build_row=False)
        finally:
            ml.setUpdatesEnabled(True)
        self._ensure_visible_rows()
        self._refresh_idle_estimate()

    def _ensure_visible_rows(self, margin=20):
        """Build the row widgets for the rows in view (plus a margin)."""
        ml = self.motion_list
        n = ml.count()
        if n == 0:
            return
        first = ml.indexAt(QPoint(1, 1)).row()
        last = ml.indexAt(QPoint(1, max(1, ml.viewport().height() - 2))).row()
        first = 0 if first < 0 else first
        if last < 0:
            rows_in_view = max(1, ml.viewport().height() // max(1, theme.px(SEQUENCE_ROW_HEIGHT)))
            last = first + rows_in_view
        built = False
        for i in range(max(0, first - margin), min(n, last + margin + 1)):
            item = ml.item(i)
            if ml.itemWidget(item) is None:
                self._build_row(item)
                built = True
        if built:
            self._restyle_seq_rows()

    def add_step(self, step, refresh=True, build_row=True):
        if refresh and self._refuse_edit_while_running():
            return
        item = QListWidgetItem()
        item.setData(STEP_ROLE, step)
        item.setSizeHint(QSize(0, theme.px(SEQUENCE_ROW_HEIGHT)))
        self.motion_list.addItem(item)
        if build_row:
            self._build_row(item)
        if refresh:
            self._restyle_seq_rows()
            self._refresh_idle_estimate()

    def _build_row(self, item):
        step = item.data(STEP_ROLE)

        label = QLabel(self._row_html(step))
        label.setStyleSheet(f"font-size: {theme.px(12)}px;")
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        remove_button = QPushButton("✕")
        remove_button.setObjectName("RowRemove")
        remove_button.setToolTip("Remove this step")
        remove_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        remove_button.clicked.connect(lambda _, i=item: self.remove_motion(i))

        widget = _SeqRow(self.motion_list, item)
        widget_layout = QHBoxLayout()
        widget_layout.setContentsMargins(7, 0, 5, 0)
        widget_layout.setSpacing(4)
        widget_layout.addWidget(label, 1)
        widget_layout.addWidget(remove_button)
        widget.setLayout(widget_layout)
        self.motion_list.setItemWidget(item, widget)

    def _restyle_seq_rows(self):
        """Colour every sequence row: the running step pulses with a left accent
        bar, selected rows get a steady tint, the rest are plain."""
        running = getattr(self, "_running_step", -1)
        for i in range(self.motion_list.count()):
            item = self.motion_list.item(i)
            w = self.motion_list.itemWidget(item)
            if not w:
                continue
            if i == running:
                # The one row the operator watches: a thick accent bar that is
                # always on, plus a tint that blinks, so it reads at a glance.
                bg = theme.ACCENT_SOFT if self._pulse_on else theme.PANEL_ALT
                bar, width, weight = theme.ACCENT, theme.px(5), 700
            elif item.isSelected():
                bg = theme.PANEL_ALT
                bar, width, weight = theme.ACCENT_DARK, theme.px(3), 400
            else:
                bg = theme.PANEL
                bar, width, weight = "transparent", theme.px(3), 400
            style = (f"#SeqRow {{ background: {bg}; "
                     f"border-left: {width}px solid {bar}; }}"
                     f"#SeqRow QLabel {{ font-weight: {weight}; }}")
            # setStyleSheet re-resolves the widget's style even when nothing
            # changed; skipping unchanged rows makes the 0.5 s pulse cost one
            # or two rows instead of the whole list (~75 ms for Run 04).
            if getattr(w, "_seq_style", None) != style:
                w.setStyleSheet(style)
                w._seq_style = style

    def _pulse_tick(self):
        self._pulse_on = not self._pulse_on
        self._restyle_seq_rows()

    def steps(self):
        return [self.motion_list.item(i).data(STEP_ROLE)
                for i in range(self.motion_list.count())]

    # ------------------------------------------------------------------
    def _selected_rows(self):
        rows = sorted(i.row() for i in self.motion_list.selectedIndexes())
        if not rows and self.motion_list.currentRow() >= 0:
            rows = [self.motion_list.currentRow()]
        return rows

    def _rebuild_rows(self, steps, select=None):
        # Checked before clear(): refusing halfway would empty the list.
        if self._refuse_edit_while_running():
            return
        self.motion_list.blockSignals(True)
        self.motion_list.clear()
        self._add_steps(steps)
        self.motion_list.blockSignals(False)
        if select:
            for r in select:
                if 0 <= r < self.motion_list.count():
                    self.motion_list.item(r).setSelected(True)
            self.motion_list.setCurrentRow(select[0])
            self.motion_list.scrollToItem(self.motion_list.item(select[0]))
        self._restyle_seq_rows()
        self._refresh_idle_estimate()

    def _insert_step(self, step):
        """Add a step right after the selected row (or at the end if nothing is
        selected), and select the new row."""
        sel = self._selected_rows()
        at = (sel[-1] + 1) if sel else self.motion_list.count()
        steps = self.steps()
        steps.insert(at, step)
        self._rebuild_rows(steps, select=[at])

    def _remove_selected(self):
        rows = sorted(self._selected_rows(), reverse=True)
        if not rows:
            return
        steps = self.steps()
        for r in rows:
            if 0 <= r < len(steps):
                steps.pop(r)
        keep = min(rows[-1], len(steps) - 1)
        self._rebuild_rows(steps, select=[keep] if steps else None)

    def _duplicate_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        steps = self.steps()
        block = [steps[r] for r in rows if 0 <= r < len(steps)]
        at = rows[-1] + 1
        for k, s in enumerate(block):
            steps.insert(at + k, s)
        self._rebuild_rows(steps, select=list(range(at, at + len(block))))

    def add_motion(self, stage_name, velocity, distance):
        self._insert_step(("move", [(stage_name, float(velocity), float(distance))]))

    def add_combined_motion(self):
        chosen = [c for c in self.stage_controls if c.combined_selected()]
        if not chosen:
            QMessageBox.information(
                self._dialog_parent(), "Combined Move",
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

        self._insert_step(("move", axes))
        if len(axes) == 1:
            self.status_message.emit(
                f"Only {axes[0][0]} was ticked — added as a normal single-axis step")
        else:
            self.status_message.emit(
                "Combined step added, moving together: "
                + ", ".join(n.split("_")[0] for n, _, _ in axes))

    def add_pause(self):
        seconds = PauseDialog.get_seconds(self._dialog_parent())
        if seconds is not None:
            self._insert_step(("pause", seconds))

    def _add_touch_step(self):
        dlg = QInputDialog(self._dialog_parent())
        dlg.setWindowTitle("Touch Point")
        dlg.setLabelText("Back off after touch (mm, 0 = stay at contact):")
        dlg.setInputMode(QInputDialog.InputMode.DoubleInput)
        dlg.setDoubleRange(0.0, 10.0)
        dlg.setDoubleDecimals(1)
        dlg.setDoubleStep(0.5)
        dlg.setMinimumWidth(theme.px(360))
        ok = dlg.exec() == QDialog.DialogCode.Accepted
        retract = dlg.doubleValue()
        if ok:
            self._insert_step(("touch", {"retract_mm": retract} if retract else {}))

    def _add_record_step(self):
        dlg = QInputDialog(self._dialog_parent())
        dlg.setWindowTitle("Record Point")
        dlg.setLabelText("Label for this calibration point:")
        dlg.setTextValue("force")
        dlg.setMinimumWidth(theme.px(360))
        ok = dlg.exec() == QDialog.DialogCode.Accepted
        label = dlg.textValue()
        if ok:
            self._insert_step(("record", {"label": label.strip() or "force"}))

    def move_step(self, delta: int):
        """Move the selected step(s) up or down as a block. The rows carry
        widgets, so the list is rebuilt from the step payloads rather than
        shuffled in place — cheap for a sequence of a few dozen steps."""
        if self.sequence_active:
            self.status_message.emit("Cannot reorder while a sequence is running")
            return

        rows = self._selected_rows()
        if not rows:
            self.status_message.emit("Select a step to move it")
            return

        steps = self.steps()
        if delta < 0 and rows[0] == 0:
            return
        if delta > 0 and rows[-1] == len(steps) - 1:
            return

        block = [steps[r] for r in rows]
        for r in reversed(rows):
            steps.pop(r)
        at = rows[0] + delta
        for k, s in enumerate(block):
            steps.insert(at + k, s)
        self._rebuild_rows(steps, select=list(range(at, at + len(block))))

    def remove_motion(self, item):
        row = self.motion_list.row(item)
        steps = self.steps()
        if 0 <= row < len(steps):
            steps.pop(row)
        self._rebuild_rows(steps, select=[min(row, len(steps) - 1)] if steps else None)

    def clear_sequence(self):
        if self._refuse_edit_while_running():
            return
        if self.motion_list.count() == 0:
            return
        ans = QMessageBox.question(
            self._dialog_parent(), "Clear Sequence",
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
    #   Touch Point          : "Touch Point"  /  "Touch Point retract 2.5"
    #   Record Point         : "Record Point force"   (force-calibration point)
    # The first two are the original format, so old files still load.
    # Blank lines and lines starting with '#' are ignored.
    # ------------------------------------------------------------------
    @staticmethod
    def _step_to_line(step) -> str:
        if step[0] == "pause":
            return f"Pause Time: {step[1]:g}"
        if step[0] == "touch":
            r = float(step[1].get("retract_mm", 0.0) or 0.0)
            return f"Touch Point retract {r:g}" if r else "Touch Point"
        if step[0] == "record":
            return f"Record Point {step[1].get('label', 'force')}".rstrip()
        axes = step[1]
        if len(axes) == 1:
            name, velocity, distance = axes[0]
            return f"{name} Velocity: {velocity:g}, Distance: {distance:g}"
        body = " ".join(f"{n}:{v:g},{d:g}" for n, v, d in axes)
        return f"Combined {body}"

    @staticmethod
    def _line_to_step(line: str):
        words = line.split()
        if not words or words[0].startswith("#"):
            return None
        try:
            return StagePanel._parse_step_words(words)
        except (ValueError, IndexError, KeyError, TypeError):
            return None      # an unreadable line is skipped, never a crash

    @staticmethod
    def _parse_step_words(words):
        if words[0] == "Pause":
            return ("pause", float(words[2]))

        if words[0] in ("Touch", "TouchPoint"):
            retract = 0.0
            low = [w.lower() for w in words]
            if "retract" in low:
                i = low.index("retract")
                if i + 1 < len(words):
                    try:
                        retract = float(words[i + 1])
                    except ValueError:
                        pass
            return ("touch", {"retract_mm": retract} if retract else {})

        if words[0] in ("Record", "RecordPoint"):
            rest = words[1:]
            if rest and rest[0].lower() == "point":
                rest = rest[1:]
            label = " ".join(rest).strip() or "force"
            return ("record", {"label": label})

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
        if self._refuse_edit_while_running():
            return
        sequence_file, _ = QFileDialog.getOpenFileName(
            self._dialog_parent(), "Load Sequence", paths.SEQUENCES_DIR, "Text Files (*.txt)")
        if not sequence_file:
            return
        self.load_sequence_file(sequence_file)

    def load_sequence_file(self, sequence_file):
        """Parse the whole file first, then build the rows in one batch."""
        with open(sequence_file, "rb") as fh:
            raw = fh.read()
        try:
            # The generator writes UTF-8 (its comments use −, →, ≈).
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # save_sequence() writes in the Windows default, as it always has.
            text = raw.decode("cp1252", errors="replace")

        steps, skipped = [], 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                step = self._line_to_step(line)
            except Exception:
                step = None
            if step is None:
                skipped += 1
                continue
            steps.append(step)

        self.motion_list.clear()
        self._add_steps(steps)

        msg = f"Loaded {self.motion_list.count()} steps"
        if skipped:
            msg += f" ({skipped} unreadable lines skipped)"
        self.status_message.emit(msg)

    def save_sequence(self):
        name, ok = QInputDialog.getText(self._dialog_parent(), "Save Sequence", "Enter sequence file name:")
        if not ok or not name.strip():
            return
        target = paths.sequence_file(name.strip())
        try:
            with open(target, "w") as file:
                for step in self.steps():
                    file.write(self._step_to_line(step) + "\n")
        except Exception as e:
            QMessageBox.critical(self._dialog_parent(), "Save Failed", f"Could not write {target}:\n{e}")
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

        needs_sensor = any(s[0] in ("touch", "record") for s in steps)
        if needs_sensor and self.latest_time_us is None \
                and self.touch_listener.latest_sample() is None:
            QMessageBox.critical(
                self._dialog_parent(), "Sequence Blocked",
                "This sequence has Touch Point / Record Point steps but no sensor "
                "data is arriving.\n\nPress Acquire first, then run the sequence.")
            return False

        for i, step in enumerate(steps, start=1):
            if step[0] in ("pause", "touch", "record"):
                continue   # Touch Point drives Z itself, force-limited;
                           #  Record Point does not move

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
                self._dialog_parent(), "Sequence Blocked",
                "The sequence would exceed stage limits and was NOT started:\n\n"
                + "\n".join(problems[:15]) + ("\n..." if len(problems) > 15 else "")
            )
            print("[SAFETY] Sequence blocked:\n  " + "\n  ".join(problems))
            return False

        if not_homed:
            QMessageBox.critical(
                self._dialog_parent(), "Reference Required",
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

        if step[0] == "touch":
            return StagePanel._estimate_touch_seconds(
                float(step[1].get("retract_mm", 0.0) or 0.0))

        if step[0] == "record":
            return float(step[1].get("window_s", 2.0) or 2.0) + 1.0

        longest = 0.0
        for _name, velocity, box_distance in step[1]:
            try:
                v = abs(float(velocity))
                d = abs(float(box_distance))
            except Exception:
                continue
            longest = max(longest, d / v if v > 0 else 0.0)
        return longest + MOVE_OVERHEAD_S

    @staticmethod
    def _estimate_touch_seconds(retract_mm: float) -> float:
        """
        A typical Touch Point step, phase by phase - not its worst case.

        This used probe_timeout_s, the approach loop's ceiling, which is 45 s
        since the approach slowed to 0.15 mm/s: every touch was costed at
        ~66 s and a recorded run showed 42 min against the 34.6 it took. The
        phases, as the stream of that run shows them:

          approach     the search. In a sequence the tip starts `retract_mm`
                       from the last contact, so that is the typical search;
                       without one, half the probe's range.
          overtravel   +overtravel at the approach speed (in contact)
          retract      -retract at retract_vel_mm_s
          return       back to the latched touch in return_step_mm steps;
                       each step costs its travel plus ~0.2 s of command /
                       settle overhead - measured 0.34 s per 0.02 mm step.
          sequence     the step's own retract_mm, at retract_vel_mm_s
          fixed        baseline, velocity setup, the F/T window, CSV row.
        """
        cfg = StagePanel._touch_config()
        search = retract_mm if retract_mm > 0 else cfg.z_max_mm / 2
        return_mm = max(0.0, cfg.retract_mm - cfg.overtravel_mm)
        return_steps = return_mm / cfg.return_step_mm if cfg.return_step_mm > 0 else 0.0
        return (cfg.baseline_s
                + search / cfg.approach_vel_mm_s
                + cfg.overtravel_mm / cfg.approach_vel_mm_s
                + cfg.retract_mm / cfg.retract_vel_mm_s
                + return_steps * (cfg.return_step_mm / cfg.return_vel_mm_s + TOUCH_RETURN_STEP_OVERHEAD_S)
                + retract_mm / cfg.retract_vel_mm_s
                + TOUCH_FIXED_OVERHEAD_S)

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
            QMessageBox.information(self._dialog_parent(), "Empty Sequence", "Add motions or pauses first.")
            self.run_sequence_button.setChecked(False)
            return

        if not self._preflight_sequence(steps):
            self.run_sequence_button.setChecked(False)
            return

        self.sequence_active = True
        self._set_sequence_editable(False)
        self.sequence_name = self.sequence_name_input.text()
        self.run_sequence_button.setChecked(True)
        self.run_sequence_button.setText("Stop Sequence")

        if any(s[0] in ("touch", "record") for s in steps):
            self._open_cal_log(self.sequence_name)

        # Countdown: cost every step up front, then tick against the clock.
        self._step_seconds = [self._estimate_step_seconds(s) for s in steps]
        self._total_seconds = sum(self._step_seconds)
        self._current_step = 0
        self._sequence_started_at = self._step_started_at = time.monotonic()
        self.sequence_progress.setValue(0)
        self.eta_timer.start()
        self._running_step = 0
        self._pulse_on = True
        self._pulse_timer.start()
        self._restyle_seq_rows()
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

    @staticmethod
    def _sleep_while(seconds, should_continue) -> bool:
        """Sleep in 0.2 s slices so operator Stop stays responsive.
        Returns False if the run was stopped before the time elapsed."""
        remaining = float(seconds)
        while remaining > 0:
            if not should_continue():
                return False
            time.sleep(min(0.2, remaining))
            remaining -= 0.2
        return should_continue()

    def _on_sequence_progress(self, index: int):
        self._current_step = index
        self._step_started_at = time.monotonic()
        self._update_eta()

        self._running_step = index
        self._pulse_on = True
        self._restyle_seq_rows()
        item = self.motion_list.item(index)
        if item is not None:     # never raise in a slot: PyQt6 would abort the app
            self.motion_list.scrollToItem(item)

    def _on_sequence_status(self, msg: str):
        print(msg)

    def _on_sequence_safety_stop(self, msg: str):
        self.status_message.emit("Sequence stopped (safety)")
        QMessageBox.critical(self._dialog_parent(), "Sequence Stopped (Safety)", msg)

    def _set_sequence_editable(self, editable: bool):
        """Grey out what edits the list while a run owns it. The handlers
        refuse on their own as well (_refuse_edit_while_running); this is so
        the operator sees it before clicking. Save stays available."""
        for b in (self.combined_button, self.pause_button, self.touch_step_button,
                  self.record_step_button, self.move_up_button, self.move_down_button,
                  self.load_sequence_button, self.clear_sequence_button):
            b.setEnabled(editable)

    def _on_sequence_finished(self):
        self.sequence_active = False
        self._set_sequence_editable(True)
        self._running_step = -1
        self._pulse_timer.stop()
        self._restyle_seq_rows()
        if self._cal_log_path:
            self.status_message.emit(
                f"Calibration points saved: {os.path.basename(self._cal_log_path)}")
        self._close_cal_log()
        self.run_sequence_button.setChecked(False)
        self.run_sequence_button.setText("Run Sequence")

        took = time.monotonic() - self._sequence_started_at if self._sequence_started_at else 0.0
        self._reset_eta(f"Finished in {self._format_clock(took)} "
                        f"(estimated {self._format_clock(self._total_seconds)})")
        self.status_message.emit(f"Sequence finished in {self._format_clock(took)}")

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

        # Three independent, movable panes (drag by the title bar, dock anywhere,
        # float, resize, or hide via View). The central widget is empty so all
        # three can be arranged freely.
        self.setDockNestingEnabled(True)
        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks)
        _centre = QWidget()
        _centre.setFixedSize(0, 0)
        self.setCentralWidget(_centre)

        self.dock_stages = self._make_dock("Stages", "dock_stages",
                                           self.stage_panel.stages_pane)
        self.dock_sequence = self._make_dock("Sequence", "dock_sequence",
                                             self.stage_panel.sequence_pane)
        self.dock_sensor = self._make_dock("Live Graph", "dock_sensor",
                                           self.sensor_panel)
        self._docks = (self.dock_stages, self.dock_sequence, self.dock_sensor)
        self._apply_default_dock_layout()

        self._build_menu()
        self._centre_range_note()

        self.status = self.statusBar()
        self.status.showMessage("Ready")
        self.status.setVisible(theme.SHOW_STATUS_BAR)
        self.stage_panel.status_message.connect(lambda m: self.status.showMessage(m, 8000))
        self.sensor_panel.status_message.connect(lambda m: self.status.showMessage(m, 8000))

        if not self._restore_window_geometry():
            self._apply_default_geometry()
        # Default dock arrangement is already in place; a saved layout overrides it.
        if theme.WINDOW_STATE:
            self._restore_dock_state()

    # ------------------------------------------------------------------
    # Dock panes
    # ------------------------------------------------------------------
    def _make_dock(self, title: str, obj_name: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(obj_name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable
                         | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        return dock

    def _apply_default_dock_layout(self):
        """Stages top-left, Sensor under it, Sequence full-height on the right."""
        for d in self._docks:
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_stages)
        self.splitDockWidget(self.dock_stages, self.dock_sensor,
                             Qt.Orientation.Vertical)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_sequence)
        # Start the Stages dock just tall enough for its block (+ the dock's own
        # title bar), so all the rest of the height goes to the Live Graph.
        title_h = self.dock_stages.style().pixelMetric(
            QStyle.PixelMetric.PM_TitleBarHeight, None, self.dock_stages)
        stages_h = getattr(self.stage_panel, "_stages_block_h", 240) + title_h + 2
        self.resizeDocks([self.dock_stages, self.dock_sensor],
                         [stages_h, max(300, 1000 - stages_h)],
                         Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_sequence], [420], Qt.Orientation.Horizontal)

    def _restore_dock_state(self) -> bool:
        try:
            raw = QByteArray.fromBase64(theme.WINDOW_STATE.encode("ascii"))
            return bool(self.restoreState(raw))
        except Exception as e:
            print(f"[WARN] Saved pane layout could not be read ({e}); using defaults.")
            return False

    # ------------------------------------------------------------------
    # Window size and position
    # ------------------------------------------------------------------
    def _apply_default_geometry(self):
        """Left-aligned, comfortably inside the primary screen."""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        width = min(1360, screen.width() - 80)
        height = min(820, screen.height() - 80)
        self.resize(width, height)
        self.move(screen.x(),
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
        theme.WINDOW_STATE = bytes(self.saveState().toBase64()).decode("ascii")
        theme.save_settings()

    def reset_window_layout(self):
        """Escape hatch if the remembered geometry / pane layout is unhelpful."""
        theme.WINDOW_GEOMETRY = None
        theme.WINDOW_STATE = None
        self.showNormal()
        self._apply_default_geometry()
        self._apply_default_dock_layout()
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
        panes = view.addMenu("Panes")
        panes.setToolTip("Show / hide the Stages, Sequence and Live Graph panes. "
                         "Drag a pane's title bar to re-dock, split or float it.")
        for dock in self._docks:
            act = dock.toggleViewAction()
            panes.addAction(act)

        self.readout_action = QAction("Readout strip", self, checkable=True)
        self.readout_action.setChecked(theme.SHOW_READOUT)
        self.readout_action.setToolTip(
            "The numbers under the chart. Hiding them gives that height to the "
            "plot; the chart legend appears instead so the traces stay identifiable.")
        self.readout_action.toggled.connect(self.set_readout_visible)
        view.addAction(self.readout_action)

        reset_layout = QAction("Reset window layout", self)
        reset_layout.setToolTip(
            "Forget the remembered window size, position and pane layout, and go "
            "back to the default arrangement.")
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


def _prep_step_dialog(dlg, form):
    """Shared look for the little sequence-step dialogs: roomy inputs, a clear
    button row, sensible minimum width so nothing is cramped."""
    dlg.setMinimumWidth(theme.px(320))
    form.setSpacing(theme.px(10))
    form.setContentsMargins(theme.px(14), theme.px(14), theme.px(14), theme.px(6))
    for w in dlg.findChildren((QDoubleSpinBox, QSpinBox)):
        # Height is the stylesheet's business: it sizes the step buttons to the
        # padding box, and a widget minimum here would stretch the field past
        # them and break that fit.
        w.setMinimumWidth(theme.px(180))
        w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    for w in dlg.findChildren(QComboBox):
        w.setMinimumWidth(theme.px(110))


def _prep_dialog_buttons(buttons):
    for b in buttons.buttons():
        b.setMinimumHeight(theme.px(32))
        b.setMinimumWidth(theme.px(90))


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
        _prep_step_dialog(self, form)
        _prep_dialog_buttons(buttons)
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

                if step[0] == "touch":
                    z = self.w.stageZ_controller
                    z.arm_return_point(hold=True)
                    ok, message = self.w._run_touch_step(
                        step[1], should_continue=lambda: self.w.sequence_active,
                        step_index=index + 1)
                    if not self.w._estop_running:
                        z.release_return_point()
                    self.status.emit(message)
                    if not ok:
                        self._abort(index, message)
                        break
                    continue

                if step[0] == "record":
                    ok, message = self.w._run_record_step(
                        step[1], should_continue=lambda: self.w.sequence_active,
                        step_index=index + 1)
                    self.status.emit(message)
                    if not ok:
                        self._abort(index, message)
                        break
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
                            # This step owns the axis until it finishes; an
                            # emergency stop before then brings it back to
                            # where the STEP began (not the whole sequence).
                            controller.arm_return_point(hold=True)
                            controller.setup_velocity(0, 10, velocity)
                            controller.move_by(distance)
                            started.append(controller)
                    except Exception as e:
                        for c in started:
                            try:
                                c.stop()
                            except Exception:
                                pass
                        if not self.w._estop_running:
                            for controller, _, _, _ in controllers:
                                controller.release_return_point()
                        self._abort(index, f"could not start motion: {e}")
                        break

                    if len(started) > 1:
                        self.status.emit(
                            "Moving together: "
                            + ", ".join(n.split("_")[0] for _, n, _, _ in controllers))

                    ok, reason = wait_until_all_stopped(
                        started, should_continue=lambda: self.w.sequence_active)
                    if not self.w._estop_running:
                        for controller, _, _, _ in controllers:
                            controller.release_return_point()
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

    FT_KEYS = ("fx_N", "fy_N", "fz_N", "tx_Nmm", "ty_Nmm", "tz_Nmm")

    def __init__(self, touch_data_queue):
        super().__init__()
        self.q = touch_data_queue
        self._running = True
        # Latest sample + a short rolling buffer kept on the listener itself, so
        # the touch probe and the calibration steps read the F/T stream WITHOUT
        # the GUI thread (a busy event loop otherwise makes it look stale).
        # Buffer entry: (wall_s, t_us, fx, fy, fz, tx, ty, tz, px, py, pz)
        self._latest = None
        self._buf = collections.deque(maxlen=1200)   # ~60 s at 20 Hz

    def latest_sample(self):
        """(t_us, fz, pz) — kept for the touch probe."""
        return self._latest

    def ft_window(self, seconds):
        """Mean of every channel over the last `seconds`, plus fz spread.
        Returns a dict, or None if no samples in the window."""
        now = time.monotonic()
        rows = [r for r in list(self._buf) if now - r[0] <= seconds]
        if not rows:
            return None
        n = len(rows)
        cols = list(zip(*rows))          # cols[2..7] = fx..tz, [8..10] = px,py,pz
        out = {"n_samples": n, "window_s": round(seconds, 2)}
        for i, key in enumerate(self.FT_KEYS):
            out[key] = sum(cols[2 + i]) / n
        fz = cols[4]
        mean_fz = out["fz_N"]
        out["fz_sd_N"] = (sum((v - mean_fz) ** 2 for v in fz) / n) ** 0.5
        for i, key in enumerate(("px_mm", "py_mm", "pz_mm")):
            out[key] = sum(cols[8 + i]) / n
        return out

    def run(self):
        while self._running:
            try:
                item = self.q.get(timeout=0.5)
            except Exception:
                continue
            if not item:
                continue
            try:
                t_us, fx, fy, fz, tx, ty, tz, px, py, pz = item
                self._latest = (int(t_us), float(fz), float(pz))
                self._buf.append((time.monotonic(), int(t_us), float(fx), float(fy),
                                  float(fz), float(tx), float(ty), float(tz),
                                  float(px), float(py), float(pz)))
                self.sample.emit(int(t_us), float(fz), float(pz))
            except Exception:
                continue

    def stop(self):
        self._running = False


# Windows groups taskbar buttons and picks the pinned icon by this id. Without
# an explicit one a PyQt app inherits the host Python's taskbar button.
APP_USER_MODEL_ID = "AhmetArifColakoglu.ATI_Rig"


def _set_app_user_model_id() -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
    except Exception:
        pass


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

        _set_app_user_model_id()
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
