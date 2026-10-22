import ctypes
import sys
import time
import multiprocessing
import threading
from lib.set_touch_point import TouchConfig, TouchProbeRunner

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QSizePolicy, QInputDialog,
    QMessageBox, QFileDialog
)
from PyQt6.QtCore import pyqtSignal, QThread

from lib.stageController import StageController
from . import Display

STAGE_X_ID = "27253425"
STAGE_Y_ID = "27253326"
STAGE_Z_ID = "27253356"


class LinearStageControl(QWidget):
    addMotionRequested = pyqtSignal(str, str, str)

    def __init__(self, stage_name, stage_controller, motion_list, parent=None):
        super().__init__(parent)

        self.stage_name = stage_name
        self.stage_controller = stage_controller

        self.stage_label = QLabel(stage_name)
        self.home_button = QPushButton("Home")
        self.velocity_label = QLabel("Velocity (mm/s):")
        self.velocity_edit = QLineEdit()
        self.distance_label = QLabel("Distance (mm):")
        self.distance_edit = QLineEdit()
        self.add_motion_button = QPushButton("Add Motion")
        self.run_motion_button = QPushButton("Run Motion")

        self.motion_list = motion_list

        layout = QVBoxLayout()
        layout.addWidget(self.stage_label)
        layout.addWidget(self.home_button)
        layout.addWidget(self.velocity_label)
        layout.addWidget(self.velocity_edit)
        layout.addWidget(self.distance_label)
        layout.addWidget(self.distance_edit)
        layout.addWidget(self.add_motion_button)
        layout.addWidget(self.run_motion_button)

        self.setLayout(layout)

        self.add_motion_button.clicked.connect(self.emit_add_motion_signal)
        self.run_motion_button.clicked.connect(self.run_motion)
        self.home_button.clicked.connect(self.home)

    def home(self):
        home_thread = threading.Thread(target=self.stage_controller.home)
        home_thread.start()

    def check_input_fields(self):
        velocity_text = self.velocity_edit.text().strip()
        distance_text = self.distance_edit.text().strip()

        if not velocity_text or not distance_text:
            QMessageBox.warning(self, "Empty Fields", "Velocity and distance cannot be empty.")
            return None, None

        try:
            velocity = float(velocity_text)
            if velocity < 0:
                QMessageBox.warning(self, "Invalid Velocity", "Velocity cannot be negative.")
                return None, None

            distance = float(distance_text)
            return velocity, distance
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Please enter valid numerical values for velocity and distance.")
            return None, None

    def run_motion(self):
        velocity, distance = self.check_input_fields()
        if velocity is not None and distance is not None:
            self.stage_controller.setup_velocity(0, 10, velocity)

            # Invert Y-axis direction to match rig convention
            if self.stage_name == "Y_Stage":
                distance = -distance

            self.stage_controller.move_by(distance)

    def emit_add_motion_signal(self):
        velocity, distance = self.check_input_fields()
        if velocity is not None and distance is not None:
            self.addMotionRequested.emit(self.stage_name, str(velocity), str(distance))


class MainWindow(QWidget):
    touch_finished = pyqtSignal(object)  # TouchResult
    def __init__(self, signal, logging_queue, touch_data_queue):
        super().__init__()

        self.touch_data_queue = touch_data_queue

        # latest sample from Display (time_us, fz, pz)
        self.latest_time_us = None
        self.latest_fz = None
        self.latest_pz = None

        # ---- Touch probe runtime ----
        self._touch_abort = threading.Event()
        self._touch_thread = None
        self._touch_running = False
        self.touch_pz = None

        self.setWindowTitle("Linear Stage Control")
        self.setMinimumWidth(800)

        self.motion_list = QListWidget()
        self.motion_list.setMinimumWidth(340)

        # ---- Touch data listener (Display -> ATI) ----
        self.touch_listener = TouchDataListener(self.touch_data_queue)
        self.touch_listener.sample.connect(self._on_touch_sample)
        self.touch_listener.start()

        # --- Right-aligned deterministic placement ---
        screen = QGuiApplication.primaryScreen().availableGeometry()

        MARGIN_RIGHT = 20
        MARGIN_TOP = 60
        STAGE_W = 800
        STAGE_H = 280

        stage_x = screen.x() + screen.width() - STAGE_W - MARGIN_RIGHT
        stage_y = screen.y() + MARGIN_TOP

        self.setGeometry(stage_x, stage_y, STAGE_W, STAGE_H)

        self.stageX_controller = StageController(STAGE_X_ID)
        self.stageY_controller = StageController(STAGE_Y_ID)
        self.stageZ_controller = StageController(STAGE_Z_ID)

        self.stage1_control = LinearStageControl("X_Stage", self.stageX_controller, self.motion_list)
        self.stage2_control = LinearStageControl("Y_Stage", self.stageY_controller, self.motion_list)
        self.stage3_control = LinearStageControl("Z_Stage", self.stageZ_controller, self.motion_list)

        self.stage1_control.addMotionRequested.connect(self.add_motion)
        self.stage2_control.addMotionRequested.connect(self.add_motion)
        self.stage3_control.addMotionRequested.connect(self.add_motion)

        self.pause_button = QPushButton("Add Pause")
        self.pause_button.clicked.connect(self.add_pause)

        self.sequence_name_input = QLineEdit()
        self.sequence_name_input.setPlaceholderText("Sequence Name")
        self.sequence_name_input.setMaximumWidth(300)
        self.sequence_name = ""

        self.run_sequence_button = QPushButton("Run Sequence")
        self.run_sequence_button.setCheckable(True)
        self.run_sequence_button.clicked.connect(self.toggle_sequence)

        self.load_sequence_button = QPushButton("Load Sequence")
        self.load_sequence_button.clicked.connect(self.load_sequence)
        self.load_sequence_button.setMaximumWidth(100)

        self.save_sequence_button = QPushButton("Save Sequence")
        self.save_sequence_button.clicked.connect(self.save_sequence)
        self.save_sequence_button.setMaximumWidth(100)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.sequence_name_input)
        button_layout.addWidget(self.run_sequence_button)
        button_layout.addWidget(self.load_sequence_button)
        button_layout.addWidget(self.save_sequence_button)

        # ---- Touch probe buttons (placed next to sequence buttons) ----
        self.set_touch_button = QPushButton("Set Touch Point")
        self.set_touch_button.clicked.connect(self.on_set_touch_point)

        self.abort_touch_button = QPushButton("Abort Touch")
        self.abort_touch_button.setEnabled(False)
        self.abort_touch_button.clicked.connect(self.on_abort_touch_point)

        button_layout.addSpacing(20)
        button_layout.addWidget(self.set_touch_button)
        button_layout.addWidget(self.abort_touch_button)

        layout = QHBoxLayout()
        layout.addWidget(self.stage1_control)
        layout.addWidget(self.stage2_control)
        layout.addWidget(self.stage3_control)
        layout.addWidget(self.motion_list)
        layout.addWidget(self.pause_button)

        main_layout = QVBoxLayout()
        main_layout.addLayout(layout)
        main_layout.addLayout(button_layout)

        self.setLayout(main_layout)
        self.touch_finished.connect(self._on_touch_finished)

        self.signal = signal
        self.logging_queue = logging_queue
        self.sequence_thread = None
        self.sequence_active = False

    def _on_touch_finished(self, result):
        # result: TouchResult
        try:
            if result.ok:
                self.touch_pz = result.touch_pz
                QMessageBox.information(self, "Touch Point Set", f"Touch point set at pz={result.touch_pz:.4f} mm")
            else:
                QMessageBox.warning(self, "Touch Failed", f"{result.reason}")
        finally:
            # UI unlock (GUI thread)
            self._touch_running = False
            self.set_touch_button.setEnabled(True)
            self.abort_touch_button.setEnabled(False)
            self.run_sequence_button.setEnabled(True)

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
            QMessageBox.warning(self, "No Data", "No touch data received yet. Start acquisition first.")
            return

        # UI lock
        self.set_touch_button.setEnabled(False)
        self.abort_touch_button.setEnabled(True)
        self.run_sequence_button.setEnabled(False)

        self._touch_abort.clear()
        self._touch_running = True

        # Config (later we can expose these to UI)
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
                    f"[TOUCH] result: ok={result.ok} reason={result.reason} touch_pz={result.touch_pz} "
                    f"fz0={result.fz0} fz_touch={result.fz_touch} peak_fz_net={result.peak_fz_net} peak_dFdt={result.peak_dFdt}"
                )
            except Exception as e:
                # if anything unexpected happens, return a failed result-like object
                class _Fail:
                    ok = False
                    reason = f"Touch probe crashed: {e}"
                    touch_pz = None
                result = _Fail()

            # IMPORTANT: UI work must be done on GUI thread
            self.touch_finished.emit(result)

        self._touch_thread = threading.Thread(target=worker, daemon=True)
        self._touch_thread.start()

    def on_abort_touch_point(self):
        print("[TOUCH] abort requested")
        self.abort_touch_button.setEnabled(False)
        self._touch_abort.set()
        try:
            self.stageZ_controller.stop()
        except Exception:
            pass



    def toggle_sequence(self):
        if self.sequence_active:
            self.stop_sequence()
        else:
            self.start_sequence()

    def start_sequence(self):
        self.sequence_active = True
        self.sequence_name = self.sequence_name_input.text()
        self.sequence_thread = threading.Thread(target=self.run_sequence)
        self.sequence_thread.start()

    def stop_sequence(self):
        self.sequence_active = False
        if self.sequence_thread and self.sequence_thread.is_alive():
            self.sequence_thread.join()

    def load_sequence(self):
        self.motion_list.clear()
        sequence_file, _ = QFileDialog.getOpenFileName(self, "Load Sequence", "sequences/", "Text Files (*.txt)")

        if sequence_file:
            with open(sequence_file, "r") as file:
                for line in file:
                    split_text = line.split()
                    stage_name = split_text[0]
                    if stage_name == "Pause":
                        motion_info = f"Time: {split_text[2]} seconds"
                    else:
                        velocity = split_text[2].rstrip(',')
                        distance = split_text[4]
                        motion_info = f"Velocity: {velocity}, Distance: {distance}"
                    stage_name = f"<b>{stage_name}</b>"
                    self.config_sequence_item(stage_name, motion_info)

    def save_sequence(self):
        sequence_file = QInputDialog.getText(self, "Save Sequence", "Enter sequence file name:")
        if sequence_file[1]:
            with open(f"sequences/{sequence_file[0]}.txt", "w") as file:
                for index in range(self.motion_list.count()):
                    item_widget = self.motion_list.itemWidget(self.motion_list.item(index))
                    split_text = item_widget.layout().itemAt(0).widget().text().split()
                    stage_name = split_text[0].strip('<b>').strip('</b>')
                    if stage_name == "Pause":
                        motion_info = f"Time: {split_text[2]}"
                    else:
                        velocity = split_text[2].rstrip(',')
                        distance = split_text[4]
                        motion_info = f"Velocity: {velocity}, Distance: {distance}"
                    file.write(f"{stage_name} {motion_info}\n")

    def config_sequence_item(self, motion_type, motion_info):
        item = QListWidgetItem()
        self.motion_list.addItem(item)

        remove_button = QPushButton("Remove")
        remove_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        remove_button.clicked.connect(lambda _, i=item: self.remove_motion(i))

        motion_item_text = f"<b>{motion_type}</b> {motion_info}"
        motion_label = QLabel(motion_item_text)
        motion_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        widget = QWidget()
        widget_layout = QHBoxLayout()
        widget_layout.addWidget(motion_label)
        widget_layout.addWidget(remove_button)
        widget.setLayout(widget_layout)

        item.setSizeHint(widget.sizeHint())
        self.motion_list.setItemWidget(item, widget)

    def add_motion(self, stage_name, velocity, distance):
        motion_info = f"Velocity: {velocity}, Distance: {distance}"
        self.config_sequence_item(stage_name, motion_info)

    def add_pause(self):
        pause_time, ok = QInputDialog.getDouble(self, "Pause Time", "Enter pause time (seconds):", 1.0, 0.1, 100.0, 1)
        if ok:
            motion_info = f"Time: {pause_time} seconds"
            self.config_sequence_item("Pause", motion_info)

    def remove_motion(self, item):
        row = self.motion_list.row(item)
        self.motion_list.takeItem(row)

    def run_sequence(self):
        # Zaten aktifse tekrar basma
        if getattr(self, "sequence_worker", None) is not None and self.sequence_worker.isRunning():
            return

        self.sequence_active = True
        self.run_sequence_button.setChecked(True)

        # Lock the UI (disable the manual-move controls, if any)
        # e.g. self.manual_move_buttons_enabled(False)

        self.sequence_worker = SequenceWorker(self)
        self.sequence_worker.progress.connect(self._on_sequence_progress)
        self.sequence_worker.status.connect(self._on_sequence_status)
        self.sequence_worker.finished.connect(self._on_sequence_finished)
        self.sequence_worker.start()

    def set_logging_active(self, logging_flag, sequence_name):
        self.logging_queue.put((logging_flag, sequence_name))
        print("Recording stopped")

    def _on_sequence_progress(self, index: int):
        # Highlight the row (GUI thread)
        item_widget = self.motion_list.itemWidget(self.motion_list.item(index))
        if item_widget:
            item_widget.layout().itemAt(0).widget().setStyleSheet("background-color: lightgrey;")

    def _on_sequence_status(self, msg: str):
        print(msg)

    def _on_sequence_finished(self):
        self.sequence_active = False
        self.run_sequence_button.setChecked(False)

        # UI unlock
        # e.g. self.manual_move_buttons_enabled(True)

        # highlight temizle
        for i in range(self.motion_list.count()):
            w = self.motion_list.itemWidget(self.motion_list.item(i))
            if w:
                w.layout().itemAt(0).widget().setStyleSheet("")

    def closeEvent(self, event):
        """
        Safe shutdown:
        - stop any running motion/sequence
        - stop stages (best-effort)
        - close stage handles (best-effort)
        - terminate Display process safely
        """
        # 1) Stop sequence loop (if running)
        try:
            self.sequence_active = False
            if self.sequence_thread and self.sequence_thread.is_alive():
                try:
                    # Best-effort stop any ongoing stage motion
                    try:
                        self.stageX_controller.stop()
                    except Exception:
                        pass
                    try:
                        self.stageY_controller.stop()
                    except Exception:
                        pass
                    try:
                        self.stageZ_controller.stop()
                    except Exception:
                        pass

                    self.sequence_thread.join(timeout=2.0)
                except Exception:
                    pass
        except Exception:
            pass

        # 2) Stop stages (best-effort)
        for ctrl in (getattr(self, "stageX_controller", None),
                     getattr(self, "stageY_controller", None),
                     getattr(self, "stageZ_controller", None)):
            if ctrl is None:
                continue
            try:
                ctrl.stop()
            except Exception:
                pass

        # 3) Close stage device handles (best-effort, if implemented)
        for ctrl in (getattr(self, "stageX_controller", None),
                     getattr(self, "stageY_controller", None),
                     getattr(self, "stageZ_controller", None)):
            if ctrl is None:
                continue
            try:
                close_fn = getattr(ctrl, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass

        # Stop touch listener thread
        try:
            if getattr(self, "touch_listener", None) is not None:
                self.touch_listener.stop()
                self.touch_listener.wait(500)
        except Exception:
            pass

        # If touch probe running, abort
        try:
            if getattr(self, "_touch_running", False):
                self._touch_abort.set()
                try:
                    self.stageZ_controller.stop()
                except Exception:
                    pass
        except Exception:
            pass

        # 4) Terminate Display process
        try:
            if getattr(self, "signal", None) is not None:
                if self.signal.is_alive():
                    self.signal.terminate()
                    self.signal.join(timeout=2.0)
                    # Hard kill if still alive (Windows can occasionally ignore terminate)
                    if self.signal.is_alive():
                        try:
                            self.signal.kill()
                        except Exception:
                            pass
        except Exception:
            pass

        super().closeEvent(event)


def run_Display(logging_queue, touch_data_queue):
    Display.main(logging_queue, touch_data_queue)


def main():
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000002)  # Prevent PC sleep

    try:
        logging_queue = multiprocessing.Queue()
        touch_data_queue = multiprocessing.Queue(maxsize=200)  # drop-oldest logic Display'de

        signal = multiprocessing.Process(target=run_Display, args=(logging_queue, touch_data_queue))
        signal.start()

        app = QApplication(sys.argv)
        window = MainWindow(signal, logging_queue, touch_data_queue)

        window.show()
        sys.exit(app.exec())

    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # Reset PC sleep settings


class SequenceWorker(QThread):
    finished = pyqtSignal()
    progress = pyqtSignal(int)  # index
    status = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.w = main_window  # MainWindow reference

    def run(self):
        try:
            self.w.set_logging_active(True, self.w.sequence_name)

            for index in range(self.w.motion_list.count()):
                if not self.w.sequence_active:
                    break

                item = self.w.motion_list.item(index)
                item_widget = self.w.motion_list.itemWidget(item)

                split_text = item_widget.layout().itemAt(0).widget().text().split()
                stage_name = split_text[0].strip('<b>').strip('</b>')

                self.progress.emit(index)

                if stage_name == "Pause":
                    pause_time_index = split_text.index("Time:")
                    pause_time = float(split_text[pause_time_index + 1])
                    self.status.emit(f"Pausing for {pause_time} seconds")

                    # Sleeping here is fine: this is the worker thread, so the GUI thread is not blocked
                    self.msleep(int(pause_time * 1000))

                else:
                    velocity_index = split_text.index("Velocity:")
                    velocity = float(split_text[velocity_index + 1].rstrip(','))

                    distance_index = split_text.index("Distance:")
                    distance = float(split_text[distance_index + 1])

                    if stage_name == "X_Stage":
                        controller = self.w.stageX_controller
                    elif stage_name == "Y_Stage":
                        controller = self.w.stageY_controller
                        distance = -distance  # rig convention
                    else:
                        controller = self.w.stageZ_controller

                    controller.setup_velocity(0, 10, velocity)
                    controller.move_by(distance)

                    # Poll with a short sleep instead of busy-waiting
                    while controller.is_moving():
                        if not self.w.sequence_active:
                            controller.stop()
                            break
                        self.msleep(20)

                if not self.w.sequence_active:
                    break

        finally:
            # Stop logging at the end of the sequence
            self.w.set_logging_active(False, self.w.sequence_name)
            self.finished.emit()


class TouchDataListener(QThread):
    sample = pyqtSignal(int, float, float)  # time_us, fz, pz

    def __init__(self, touch_data_queue):
        super().__init__()
        self.q = touch_data_queue
        self._running = True

    def run(self):
        while self._running:
            try:
                item = self.q.get()  # blocking
                if not item:
                    continue
                t_us, fz, pz = item
                self.sample.emit(int(t_us), float(fz), float(pz))
            except Exception:
                # keep the thread alive unless stopped
                continue

    def stop(self):
        self._running = False


if __name__ == "__main__":
    main()
