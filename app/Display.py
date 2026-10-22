import sys
import multiprocessing

from PyQt6.QtGui import QPen, QColor
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QPushButton, QWidget, QLabel
from PyQt6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from lib.K64F import K64F, TeensyThread


class MainWindow(QMainWindow):
    def __init__(self, logging_queue, touch_data_queue):
        super().__init__()
        self.touch_data_queue = touch_data_queue

        # Window properties
        self.setWindowTitle("Sensor Display")

        # Deterministic right-aligned placement under the Stage window
        screen = QGuiApplication.primaryScreen().availableGeometry()

        MARGIN_RIGHT = 20
        MARGIN_TOP = 20
        GAP = 5
        STAGE_H = 360
        DISP_W = 800
        DISP_H = 380

        disp_x = screen.x() + screen.width() - DISP_W - MARGIN_RIGHT
        disp_y = screen.y() + MARGIN_TOP + STAGE_H + GAP
        self.setGeometry(disp_x, disp_y, DISP_W, DISP_H)

        # Data initialisation
        self.data_labels = ["fx", "fy", "fz", "tx", "ty", "tz", "n_bx", "n_by", "n_bz"]
        self.data = tuple([0] * 16)  # Must match DataStream emitted tuple length

        # Chart
        self.chart = QChart()
        self.chart.setTitle("ATI Force/Torque and Normalised Custom Sensor")
        self.chart_view = QChartView()
        self.chart_view.setChart(self.chart)

        # Axes
        self.axisY = QValueAxis()
        self.axisY.setTitleText("Force/Torque")
        self.axisY.setRange(-20, 20)
        self.chart.addAxis(self.axisY, Qt.AlignmentFlag.AlignLeft)

        self.axisX = QValueAxis()
        self.axisX.setRange(0, 1)
        self.axisX.setLabelFormat("%u")
        self.axisX.setTickType(QValueAxis.TickType.TicksDynamic)
        self.axisX.setTickInterval(10)
        self.chart.addAxis(self.axisX, Qt.AlignmentFlag.AlignBottom)

        # Add series to chart (fixed colors)
        colors = {
            "fx": "#E41A1C",
            "fy": "#377EB8",
            "fz": "#4DAF4A",
            "tx": "#984EA3",
            "ty": "#FF7F00",
            "tz": "#A65628",
            "n_bx": "#F781BF",
            "n_by": "#999999",
            "n_bz": "#17BECF",
        }
        line_width = 2

        for name in self.data_labels:
            series = QLineSeries()
            series.setName(name)

            pen = QPen(QColor(colors.get(name, "#000000")))
            pen.setWidth(line_width)
            series.setPen(pen)

            self.chart.addSeries(series)
            series.attachAxis(self.axisX)
            series.attachAxis(self.axisY)

        # Buttons
        self.acquire_button = QPushButton("Acquire")
        self.acquire_button.setCheckable(True)
        self.acquire_button.clicked.connect(self.toggle_acquisition)

        self.zero_button = QPushButton("Zero Data")
        self.zero_button.clicked.connect(self.zero_data)

        # Recording indicator
        self.recording_indicator = QLabel("Not Recording")
        self.recording_indicator.setStyleSheet("background-color: white; color: black; padding: 5px;")
        self.recording_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Layout
        self.layout = QVBoxLayout()
        indicators_layout = QVBoxLayout()
        self.indicators = []
        for _ in range(15):
            indicator_label = QLabel()
            self.indicators.append(indicator_label)
            indicators_layout.addWidget(indicator_label)

        indicators_layout.addWidget(self.zero_button)
        indicators_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Initial indicator values
        self.indicators[0].setText(f"px: {0:.3f}")
        self.indicators[1].setText(f"py: {0:.3f}")
        self.indicators[2].setText(f"pz: {0:.3f}")

        self.indicators[3].setText(f"fx: {0:.3f}")
        self.indicators[4].setText(f"fy: {0:.3f}")
        self.indicators[5].setText(f"fz: {0:.3f}")

        self.indicators[6].setText(f"tx: {0:.3f}")
        self.indicators[7].setText(f"ty: {0:.3f}")
        self.indicators[8].setText(f"tz: {0:.3f}")

        self.indicators[9].setText("raw_bx: 0")
        self.indicators[10].setText("raw_by: 0")
        self.indicators[11].setText("raw_bz: 0")

        self.indicators[12].setText(f"n_bx: {0:.3f}")
        self.indicators[13].setText(f"n_by: {0:.3f}")
        self.indicators[14].setText(f"n_bz: {0:.3f}")

        chart_layout = QHBoxLayout()
        chart_layout.addLayout(indicators_layout)
        chart_layout.addWidget(self.chart_view)

        self.layout.addLayout(chart_layout)
        self.layout.addWidget(self.acquire_button)
        self.layout.addWidget(self.recording_indicator)

        # Central widget
        self.central_widget = QWidget()
        self.central_widget.setLayout(self.layout)
        self.setCentralWidget(self.central_widget)

        # Hardware init (connect ethernet only; do not start acquisition yet)
        self.K64F = K64F()
        self.K64F.init_ethernet()

        # Teensy config (will be used when acquisition starts)
        self.teensy_cfg = dict(
            port="COM5",
            baudrate=115200,
            reset_on_connect=True,
            boot_marker="BOOT:READY",
            boot_wait_s=8.0,
            stabilize_s=2.0,
            verify_window_s=2.0,
            verify_min_packets=5,
            verbose=True,
        )

        # Runtime handles
        self.teensy_thread = None
        self.acquisition_active = False

        # Plotting helpers
        self.read_counter = 0
        self.mean_ft_data = [0.0] * 6
        self.max_x = 0

        # Logging thread
        self.logging_queue = logging_queue
        self.logging_thread = LoggingListener(self, logging_queue, self.K64F)
        self.logging_thread.recording_changed.connect(self._on_recording_changed)
        self.logging_thread.start()

    def _on_recording_changed(self, is_recording: bool):
        if is_recording:
            self.recording_indicator.setText("Recording")
            self.recording_indicator.setStyleSheet(
                "background-color: green; color: black; padding: 5px;"
            )
        else:
            self.recording_indicator.setText("Not Recording")
            self.recording_indicator.setStyleSheet(
                "background-color: white; color: black; padding: 5px;"
            )

    def toggle_acquisition(self):
        if self.acquisition_active:
            self.stop_acquisition()
        else:
            self.start_acquisition()

    def start_acquisition(self):
        # Start K64F stream
        self.K64F.start_acquisition()

        if self.K64F.data_stream is None:
            print("[ERROR] Data stream not initialized. Acquisition will not start.")
            self.acquire_button.setChecked(False)
            return

        # Avoid duplicate connections
        try:
            self.K64F.data_stream.data_received.disconnect(self.update_chart)
        except Exception:
            pass
        self.K64F.data_stream.data_received.connect(self.update_chart)

        # Start Teensy thread now that DataStream exists
        if self.teensy_thread is None:
            self.teensy_thread = TeensyThread(self.K64F.data_stream, **self.teensy_cfg)
            self.teensy_thread.start()

        self.acquisition_active = True

    def stop_acquisition(self):
        # Stop Teensy first (prevents updates into a stopping DataStream)
        if self.teensy_thread is not None:
            try:
                self.teensy_thread.stop()
            except Exception:
                pass
            self.teensy_thread = None

        # Stop K64F stream
        self.K64F.stop_acquisition()

        self.acquisition_active = False
        self.acquire_button.setChecked(False)

    def update_chart(self, data):
        # DataStream emits:
        # (time_us, fx, fy, fz, tx, ty, tz, px, py, pz, raw_bx, raw_by, raw_bz, n_bx, n_by, n_bz)
        time_us, fx, fy, fz, tx, ty, tz, px, py, pz, raw_bx, raw_by, raw_bz, n_bx, n_by, n_bz = data

        # Accumulate ATI force/torque for simple smoothing
        self.mean_ft_data[0] += fx
        self.mean_ft_data[1] += fy
        self.mean_ft_data[2] += fz
        self.mean_ft_data[3] += tx
        self.mean_ft_data[4] += ty
        self.mean_ft_data[5] += tz

        # Update position indicators every sample
        self.indicators[0].setText(f"px: {px:.3f}")
        self.indicators[1].setText(f"py: {py:.3f}")
        self.indicators[2].setText(f"pz: {pz:.3f}")

        self.read_counter += 1

        if self.read_counter == 5:
            mean_ft_data = [value / 5.0 for value in self.mean_ft_data]

            # Update ATI indicators (smoothed)
            self.indicators[3].setText(f"fx: {mean_ft_data[0]:.3f}")
            self.indicators[4].setText(f"fy: {mean_ft_data[1]:.3f}")
            self.indicators[5].setText(f"fz: {mean_ft_data[2]:.3f}")

            self.indicators[6].setText(f"tx: {mean_ft_data[3]:.3f}")
            self.indicators[7].setText(f"ty: {mean_ft_data[4]:.3f}")
            self.indicators[8].setText(f"tz: {mean_ft_data[5]:.3f}")

            # Update RAW Teensy indicators (latest, int16)
            self.indicators[9].setText(f"raw_bx: {int(raw_bx)}")
            self.indicators[10].setText(f"raw_by: {int(raw_by)}")
            self.indicators[11].setText(f"raw_bz: {int(raw_bz)}")

            # Update normalised Teensy indicators (latest)
            self.indicators[12].setText(f"n_bx: {n_bx:.3f}")
            self.indicators[13].setText(f"n_by: {n_by:.3f}")
            self.indicators[14].setText(f"n_bz: {n_bz:.3f}")

            # Append to plot series
            for i, series in enumerate(self.chart.series()):
                if i < 6:
                    series.append(self.max_x, mean_ft_data[i])
                elif i == 6:
                    series.append(self.max_x, n_bx)
                elif i == 7:
                    series.append(self.max_x, n_by)
                elif i == 8:
                    series.append(self.max_x, n_bz)

            self.max_x += 1
            min_x = max(0, self.max_x - 100)
            self.axisX.setRange(min_x, self.max_x)

            # Keep a rolling window of 100 points
            for series in self.chart.series():
                while len(series.points()) > 100:
                    series.remove(0)

            self.chart_view.update()

            # ---- Touch-probe feed (Display -> ATI) ----
            # Send (time_us, fz, pz) non-blocking; if queue is full, drop the oldest item.
            if self.touch_data_queue is not None:
                try:
                    self.touch_data_queue.put_nowait((int(time_us), float(mean_ft_data[2]), float(pz)))
                except Exception:
                    # Queue likely full; try to drop one item then retry once
                    try:
                        _ = self.touch_data_queue.get_nowait()
                        self.touch_data_queue.put_nowait((int(time_us), float(mean_ft_data[2]), float(pz)))
                    except Exception:
                        pass
            # Reset smoothing accumulators
            self.read_counter = 0
            self.mean_ft_data = [0.0] * 6

    def zero_data(self):
        self.K64F.set_data_offset()

    def closeEvent(self, event):
        try:
            # If acquisition is running, stop it cleanly
            self.stop_acquisition()
        except Exception:
            pass

        try:
            # The critical line: actually close the socket
            self.K64F.close()
        except Exception:
            pass

        super().closeEvent(event)

class LoggingListener(QThread):
    recording_changed = pyqtSignal(bool)  # True=Recording, False=Not Recording
    def __init__(self, main_window, logging_queue, K64F):
        super().__init__()
        self.main_window = main_window
        self.logging_queue = logging_queue
        self.K64F = K64F

    def run(self):
        while True:
            logging_flag, sequence_name = self.logging_queue.get()
            if logging_flag is True:
                self.K64F.set_sequence_name(sequence_name)
                self.K64F.enable_logging()
                self.recording_changed.emit(True)
            else:
                self.K64F.disable_logging()
                self.recording_changed.emit(False)


def main(logging_queue, touch_data_queue):
    app = QApplication(sys.argv)
    window = MainWindow(logging_queue, touch_data_queue)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    logging_queue = multiprocessing.Queue()
    touch_data_queue = multiprocessing.Queue()
    main(logging_queue, touch_data_queue)
