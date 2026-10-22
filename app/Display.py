import sys
import queue

from PyQt6.QtGui import QPen, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QWidget, QLabel, QGroupBox, QComboBox, QSizePolicy,
    QMessageBox, QFrame
)
from PyQt6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMargins, QTimer

from lib.K64F import K64F, TeensyThread, IP_ADDRESS, TCP_PORT
from lib.find_ports import find_teensy_port
from . import theme

# Rolling plot window (samples shown on screen)
PLOT_WINDOW = 100

# ---------------------------------------------------------------------------
# Ethernet / K64F link
# ---------------------------------------------------------------------------
# The K64F only accepts a new TCP connection after its supply board has been
# power-cycled: if the app was closed while the device still believed a client
# was attached, every later connect attempt is refused or hangs. The fix is
# always the same, so the app states it in full rather than printing a socket
# error and leaving the operator guessing.
ETHERNET_HELP = (
    "The sensor controller (K64F) at {ip}:{port} is not responding.\n\n"
    "This is normally the board's supply, not the software. Do this:\n\n"
    "  1.  Close this application.\n"
    "  2.  Switch the power supply feeding the Ethernet board OFF.\n"
    "  3.  Wait about 5 seconds.\n"
    "  4.  Switch it back ON and wait for the board to boot (~5 s).\n"
    "  5.  Start the application again.\n\n"
    "'Reconnect' next to Acquire can be tried first — it re-opens the socket "
    "without restarting the app — but if the board was left in a stuck state "
    "only the power cycle will clear it.\n\n"
    "Also check: the Ethernet cable is seated, and the PC adapter is still set "
    "to the 192.168.5.x network."
).format(ip=IP_ADDRESS, port=TCP_PORT)

# If acquisition is running and no sample arrives for this long, the link is
# considered dead (the classic "the program froze" symptom).
DATA_WATCHDOG_S = 8.0

# Y-axis presets. "Auto" fits the visible data.
def _y_range_presets():
    """Scales, with the sensor's own maxima marked."""
    fz = theme.NANO17_FZ_N
    torque = theme.NANO17_TORQUE_NMM
    fixed = [5.0, 10.0, 20.0, 40.0, 50.0, 100.0, 200.0, 300.0, 400.0, 500.0]

    marks = {}
    marks.setdefault(fz, []).append("Fz max")
    marks.setdefault(torque, []).append("torque max")

    values = sorted(set(fixed) | set(marks))
    presets = [("Auto (fit data)", None)]
    for v in values:
        note = f"  ({' / '.join(marks[v])})" if v in marks else ""
        presets.append((f"± {v:g}{note}", v))
    return presets


Y_RANGE_PRESETS = _y_range_presets()
# ± 20 by default, wherever it lands in the generated list
DEFAULT_Y_RANGE_INDEX = next(
    (i for i, (_, v) in enumerate(Y_RANGE_PRESETS) if v == 20.0), 1)


class SensorPanel(QWidget):
    """
    Live sensor readout: numeric indicators plus a rolling chart.

    Designed to sit inside the main window underneath the stage controls, but
    it owns its own hardware handles (K64F + Teensy) exactly as before.
    """

    status_message = pyqtSignal(str)
    recording_changed = pyqtSignal(bool)

    def __init__(self, logging_queue, touch_data_queue, parent=None):
        super().__init__(parent)
        self.touch_data_queue = touch_data_queue

        self.data_labels = ["fx", "fy", "fz", "tx", "ty", "tz", "n_bx", "n_by", "n_bz"]

        # ---------------- chart ----------------
        self.chart = QChart()
        self.chart.setTitle("")            # no title: that row belongs to the plot
        self.chart.setMargins(QMargins(2, 2, 2, 2))
        self.chart.setBackgroundRoundness(0)
        self.chart.setBackgroundBrush(QColor(theme.CHART_FRAME))
        self.chart.setPlotAreaBackgroundBrush(QColor(theme.PLOT_BG))
        self.chart.setPlotAreaBackgroundVisible(True)

        legend = self.chart.legend()
        legend.setVisible(True)
        legend.setAlignment(Qt.AlignmentFlag.AlignBottom)
        legend.setFont(QFont("Segoe UI", theme.pt(9)))
        legend.setLabelColor(QColor(theme.TEXT_MUTED))
        legend.setMarkerShape(legend.MarkerShape.MarkerShapeCircle)

        self.chart_view = QChartView()
        self.chart_view.setChart(self.chart)
        self.chart_view.setRenderHint(self.chart_view.renderHints().Antialiasing)
        self.chart_view.setFrameShape(self.chart_view.Shape.NoFrame)
        self.chart_view.setBackgroundBrush(QColor(theme.CHART_FRAME))
        self.chart_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        axis_font = QFont("Segoe UI", theme.pt(9))

        self.axisY = QValueAxis()
        self.axisY.setTitleText("")
        self.axisY.setRange(-20, 20)
        self.axisY.setLabelsFont(axis_font)
        self.axisY.setLabelsColor(QColor(theme.TEXT_MUTED))
        self.axisY.setGridLineColor(QColor(theme.GRID))
        self.axisY.setLinePenColor(QColor(theme.BORDER))
        self.chart.addAxis(self.axisY, Qt.AlignmentFlag.AlignLeft)

        self.axisX = QValueAxis()
        self.axisX.setRange(0, 1)
        self.axisX.setLabelFormat("%u")
        self.axisX.setTickType(QValueAxis.TickType.TicksDynamic)
        self.axisX.setTickInterval(10)
        self.axisX.setLabelsFont(axis_font)
        self.axisX.setLabelsColor(QColor(theme.TEXT_MUTED))
        self.axisX.setGridLineColor(QColor(theme.GRID))
        self.axisX.setLinePenColor(QColor(theme.BORDER))
        self.chart.addAxis(self.axisX, Qt.AlignmentFlag.AlignBottom)

        for name in self.data_labels:
            series = QLineSeries()
            series.setName(name)
            pen = QPen(QColor(theme.SERIES_COLORS.get(name, "#000000")))
            pen.setWidth(2)
            series.setPen(pen)
            self.chart.addSeries(series)
            series.attachAxis(self.axisX)
            series.attachAxis(self.axisY)

        # ---------------- controls ----------------
        self.acquire_button = QPushButton("Acquire")
        self.acquire_button.setObjectName("Primary")
        self.acquire_button.setCheckable(True)
        self.acquire_button.clicked.connect(self.toggle_acquisition)

        self.reconnect_button = QPushButton("Reconnect")
        self.reconnect_button.setToolTip(
            "Re-open the Ethernet connection to the sensor controller.\n"
            "If this does not help, power-cycle the board's supply and restart the app.")
        self.reconnect_button.clicked.connect(self.reconnect_ethernet)

        self.link_label = QLabel("LINK ?")
        self.link_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.link_label.setFixedWidth(theme.px(96))

        self.zero_button = QPushButton("Zero")
        self.zero_button.setToolTip("Subtract the current reading as an offset "
                                    "(force/torque, encoders and Teensy).")
        self.zero_button.clicked.connect(self.zero_data)

        self.y_range_combo = QComboBox()
        for label, _ in Y_RANGE_PRESETS:
            self.y_range_combo.addItem(label)
        self.y_range_combo.setCurrentIndex(DEFAULT_Y_RANGE_INDEX)
        self.y_range_combo.setToolTip("Vertical scale of the chart.")
        self.y_range_combo.currentIndexChanged.connect(self.apply_y_range)
        self.y_range_combo.setMinimumWidth(theme.px(160))
        self.y_range_combo.setMaximumWidth(theme.px(210))

        self.recording_indicator = QLabel("IDLE")
        self.recording_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recording_indicator.setFixedWidth(theme.px(96))
        self._recording = False
        self._set_recording_style(False)

        # The sensor's measurement range is reference information, not a
        # control. It lives in the menu bar (MainWindow) so this row stays
        # narrow and the window can be resized down further.
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self.acquire_button)
        controls.addWidget(self.reconnect_button)
        controls.addWidget(self.zero_button)
        controls.addSpacing(8)
        controls.addWidget(self.link_label)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Y scale:"))
        controls.addWidget(self.y_range_combo)
        controls.addStretch(1)
        controls.addWidget(self.recording_indicator)

        # ---------------- indicators ----------------
        # Laid out as five side-by-side groups rather than one tall column.
        # The column form needed 20 rows of height and was by far the largest
        # fixed element in the window; this needs four, and its width is shared
        # with the chart, which stretches anyway. Same numbers, same order.
        self.indicators = {}
        self.indicator_names = {}
        self.readout_box = readout = QFrame()
        readout.setObjectName("ReadoutStrip")
        grid = QGridLayout()
        grid.setContentsMargins(10, 4, 10, 6)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)

        for col, (section, channels, unit) in enumerate((
            ("Position", [("px", None), ("py", None), ("pz", None)], "mm"),
            ("Force", [("fx", "fx"), ("fy", "fy"), ("fz", "fz")], "N"),
            ("Torque", [("tx", "tx"), ("ty", "ty"), ("tz", "tz")], "N·mm"),
            ("Teensy raw", [("raw_bx", None), ("raw_by", None), ("raw_bz", None)], ""),
            ("Teensy norm.", [("n_bx", "n_bx"), ("n_by", "n_by"), ("n_bz", "n_bz")], ""),
        )):
            # Three columns per group: label, value, then a spacer that takes
            # the slack. Stretching the VALUE column instead would push every
            # number to the far right of its group, leaving a gap between a
            # label and the figure it belongs to.
            name_col, value_col, gap_col = col * 3, col * 3 + 1, col * 3 + 2

            header = QLabel(f"{section}  {unit}".strip())
            header.setObjectName("SectionLabel")
            grid.addWidget(header, 0, name_col, 1, 2)

            for i, (key, colour_key) in enumerate(channels, start=1):
                name = QLabel(key)
                self.indicator_names[key] = (name, colour_key)
                value = QLabel("—")
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                value.setMinimumWidth(theme.px(58))
                grid.addWidget(name, i, name_col)
                grid.addWidget(value, i, value_col)
                self.indicators[key] = value

            grid.setColumnMinimumWidth(gap_col, theme.px(6))
            grid.setColumnStretch(gap_col, 1)

        readout.setLayout(grid)
        readout.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._restyle_indicators()

        # ---------------- assembly ----------------
        # No group box around the chart: its title row and padding cost ~30 px
        # that the plot itself makes better use of. The chart draws its own
        # frame (see the QChartView rule in theme.py).
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(self.chart_view, 1)
        root.addWidget(readout)
        root.addLayout(controls)
        self.setLayout(root)

        # The readout labels are coloured to match the traces, so they already
        # say which line is which. The chart legend would only repeat that, and
        # it costs a row of plot height - so it appears only when the readout
        # strip is hidden.
        self.set_readout_visible(theme.SHOW_READOUT)

        # ---------------- hardware ----------------
        self.K64F = K64F()
        self.K64F.init_ethernet()
        self._set_link_state(bool(self.K64F.connected))

        if not self.K64F.connected:
            # Do not block the constructor: warn once the window is on screen.
            QTimer.singleShot(600, lambda: self.show_ethernet_help(
                "Sensor Controller Not Connected",
                "The application started, but it could not reach the sensor "
                "controller. Stage motion still works; live force/torque data "
                "does not."))

        teensy_port = find_teensy_port(verbose=False)
        if teensy_port:
            print(f"[INFO] Teensy detected on {teensy_port}")
        else:
            print("[WARN] No Teensy device found on this system (VID 0x16C0 not detected). "
                  "The app will run WITHOUT Teensy data; plug it in before pressing Acquire.")

        self.teensy_cfg = dict(
            port="auto",
            baudrate=115200,
            reset_on_connect=True,
            boot_marker="BOOT:READY",
            boot_wait_s=8.0,
            stabilize_s=2.0,
            verify_window_s=2.0,
            verify_min_packets=5,
            verbose=True,
        )

        self.teensy_thread = None
        self.acquisition_active = False

        # Link watchdog: counts samples so a silent stream can be detected.
        self._sample_count = 0
        self._last_watchdog_count = -1
        self._watchdog_warned = False
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(int(DATA_WATCHDOG_S * 1000))
        self.watchdog_timer.timeout.connect(self._check_data_flow)

        # plotting helpers
        self.read_counter = 0
        self.mean_ft_data = [0.0] * 6
        self.max_x = 0
        self._auto_lo = -1.0
        self._auto_hi = 1.0

        self.logging_queue = logging_queue
        self.logging_thread = LoggingListener(logging_queue, self.K64F)
        self.logging_thread.recording_changed.connect(self._on_recording_changed)
        self.logging_thread.start()

        self.apply_y_range()

    # ------------------------------------------------------------------
    # Theme / font-scale support
    # ------------------------------------------------------------------
    def _restyle_indicators(self):
        for key, (label, colour_key) in self.indicator_names.items():
            colour = theme.SERIES_COLORS.get(colour_key, theme.POSITION_COLOR)
            label.setStyleSheet(f"color: {colour}; font-weight: 600; "
                                f"font-size: {theme.px(13)}px;")
            self.indicators[key].setStyleSheet(
                f"font-family: {theme.MONO}; font-size: {theme.px(13)}px;")

    def restyle(self):
        """Re-apply colours and font sizes after the theme or scale changes."""
        self._restyle_indicators()
        self._set_recording_style(self._recording)
        self.link_label.setFixedWidth(theme.px(96))
        self._set_link_state(getattr(self, "_link_ok", False))

        self.chart.setBackgroundBrush(QColor(theme.CHART_FRAME))
        self.chart.setPlotAreaBackgroundBrush(QColor(theme.PLOT_BG))
        self.chart_view.setBackgroundBrush(QColor(theme.CHART_FRAME))
        legend = self.chart.legend()
        legend.setFont(QFont("Segoe UI", theme.pt(9)))
        legend.setLabelColor(QColor(theme.TEXT_MUTED))

        axis_font = QFont("Segoe UI", theme.pt(9))
        for axis in (self.axisX, self.axisY):
            axis.setLabelsFont(axis_font)
            axis.setLabelsColor(QColor(theme.TEXT_MUTED))
            axis.setGridLineColor(QColor(theme.GRID))
            axis.setLinePenColor(QColor(theme.BORDER))

        for series in self.chart.series():
            pen = series.pen()
            pen.setColor(QColor(theme.SERIES_COLORS.get(series.name(), "#888888")))
            series.setPen(pen)


    # ------------------------------------------------------------------
    def _set_recording_style(self, recording: bool):
        self._recording = recording
        if recording:
            self.recording_indicator.setText("● REC")
            self.recording_indicator.setStyleSheet(
                f"background:{theme.DANGER}; color:#fff; border-radius:4px;"
                f"padding:{theme.px(4)}px; font-weight:700; letter-spacing:0.5px;"
            )
        else:
            self.recording_indicator.setText("IDLE")
            self.recording_indicator.setStyleSheet(
                f"background:{theme.PANEL_ALT}; color:{theme.TEXT_MUTED};"
                f"border:1px solid {theme.BORDER}; border-radius:4px;"
                f"padding:{theme.px(4)}px; font-weight:600; letter-spacing:0.5px;"
            )

    def _on_recording_changed(self, is_recording: bool):
        self._set_recording_style(is_recording)
        self.recording_changed.emit(is_recording)

    # ------------------------------------------------------------------
    def apply_y_range(self):
        _, span = Y_RANGE_PRESETS[self.y_range_combo.currentIndex()]
        if span is None:
            lo, hi = self._auto_lo, self._auto_hi
            if hi - lo < 1e-6:
                lo, hi = lo - 0.5, hi + 0.5
            pad = (hi - lo) * 0.1
            self.axisY.setRange(lo - pad, hi + pad)
        else:
            self.axisY.setRange(-span, span)

    def _update_auto_range(self):
        lo, hi = None, None
        for series in self.chart.series():
            for p in series.points():
                v = p.y()
                lo = v if lo is None else min(lo, v)
                hi = v if hi is None else max(hi, v)
        if lo is not None:
            self._auto_lo, self._auto_hi = lo, hi

    def set_readout_visible(self, visible: bool):
        """
        Show or hide the numeric strip under the chart.

        Hiding it hands its height to the plot. The chart legend is switched on
        in its place, so the traces can still be told apart.
        """
        visible = bool(visible)
        self.readout_box.setVisible(visible)
        self.chart.legend().setVisible(not visible)

    # ------------------------------------------------------------------
    # Ethernet link
    # ------------------------------------------------------------------
    def _set_link_state(self, connected: bool):
        """Colour the LINK pill: green when the K64F answers, red when it does not."""
        self._link_ok = connected
        text = "LINK OK" if connected else "NO LINK"
        colour = theme.OK if connected else theme.DANGER
        self.link_label.setText(text)
        self.link_label.setToolTip(
            f"Sensor controller {IP_ADDRESS}:{TCP_PORT} — "
            + ("connected." if connected else
               "not reachable. Power-cycle the Ethernet board's supply and restart."))
        self.link_label.setStyleSheet(
            f"color: {colour}; font-weight: 700; font-size: {theme.px(11)}px;"
            f"border: 1px solid {colour}; border-radius: 3px; padding: 2px;")

    def show_ethernet_help(self, title: str, lead: str = ""):
        """The one dialog that explains the power-cycle fix."""
        self.status_message.emit("Sensor controller not reachable")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(lead or "Lost contact with the sensor controller.")
        box.setInformativeText(ETHERNET_HELP)
        box.exec()

    def reconnect_ethernet(self):
        """Re-open the socket without restarting the app."""
        if self.acquisition_active:
            self.stop_acquisition()

        self.reconnect_button.setEnabled(False)
        self.status_message.emit("Reconnecting to sensor controller…")
        QApplication.processEvents()
        try:
            ok = self.K64F.reconnect()
        except Exception as e:
            print(f"[ERROR] Reconnect failed: {e}")
            ok = False
        finally:
            self.reconnect_button.setEnabled(True)

        self._set_link_state(ok)
        self._watchdog_warned = False
        if ok:
            self.status_message.emit("Sensor controller reconnected")
        else:
            self.show_ethernet_help(
                "Reconnect Failed",
                "Re-opening the connection did not work, so the board is most "
                "likely stuck and needs its power cycled.")

    def _check_data_flow(self):
        """No samples since the last tick while acquiring = the link is dead."""
        if not self.acquisition_active:
            return

        if self._sample_count != self._last_watchdog_count:
            self._last_watchdog_count = self._sample_count
            return

        if self._watchdog_warned:
            return
        self._watchdog_warned = True

        print(f"[ERROR] No sensor data for {DATA_WATCHDOG_S:.0f} s — stopping acquisition.")
        self.stop_acquisition()
        self._set_link_state(False)
        self.show_ethernet_help(
            "No Data From Sensor Controller",
            f"Acquisition was running but no sample arrived for "
            f"{DATA_WATCHDOG_S:.0f} seconds, so it has been stopped.")

    # ------------------------------------------------------------------
    def toggle_acquisition(self):
        if self.acquisition_active:
            self.stop_acquisition()
        else:
            self.start_acquisition()

    def start_acquisition(self):
        if not self.K64F.connected:
            self.acquire_button.setChecked(False)
            self._set_link_state(False)
            self.show_ethernet_help(
                "Cannot Start Acquisition",
                "There is no connection to the sensor controller, so there is "
                "nothing to acquire. Try 'Reconnect' first.")
            return

        self.K64F.start_acquisition()

        if self.K64F.data_stream is None:
            print("[ERROR] Data stream not initialized. Acquisition will not start.")
            self.acquire_button.setChecked(False)
            self._set_link_state(False)
            self.show_ethernet_help(
                "Acquisition Failed",
                "The sensor controller accepted the connection but would not "
                "start streaming.")
            return

        try:
            self.K64F.data_stream.data_received.disconnect(self.update_chart)
        except Exception:
            pass
        self.K64F.data_stream.data_received.connect(self.update_chart)

        if self.teensy_thread is None:
            self.teensy_thread = TeensyThread(self.K64F.data_stream, **self.teensy_cfg)
            self.teensy_thread.start()

        self.acquisition_active = True
        self.acquire_button.setText("Stop")
        self.status_message.emit("Acquiring")

        self._last_watchdog_count = -1
        self._watchdog_warned = False
        self.watchdog_timer.start()

    def stop_acquisition(self):
        self.watchdog_timer.stop()

        if self.teensy_thread is not None:
            try:
                self.teensy_thread.stop()
            except Exception:
                pass
            self.teensy_thread = None

        self.K64F.stop_acquisition()

        self.acquisition_active = False
        self.acquire_button.setChecked(False)
        self.acquire_button.setText("Acquire")
        self.status_message.emit("Acquisition stopped")

    # ------------------------------------------------------------------
    def update_chart(self, data):
        # (time_us, fx, fy, fz, tx, ty, tz, px, py, pz,
        #  raw_bx, raw_by, raw_bz, n_bx, n_by, n_bz)
        (time_us, fx, fy, fz, tx, ty, tz, px, py, pz,
         raw_bx, raw_by, raw_bz, n_bx, n_by, n_bz) = data

        # Proof of life for the watchdog.
        self._sample_count += 1
        if not self._link_ok:
            self._set_link_state(True)

        self.mean_ft_data[0] += fx
        self.mean_ft_data[1] += fy
        self.mean_ft_data[2] += fz
        self.mean_ft_data[3] += tx
        self.mean_ft_data[4] += ty
        self.mean_ft_data[5] += tz

        self.indicators["px"].setText(f"{px:8.3f}")
        self.indicators["py"].setText(f"{py:8.3f}")
        self.indicators["pz"].setText(f"{pz:8.3f}")

        self.read_counter += 1
        if self.read_counter < 5:
            return

        mean_ft = [v / 5.0 for v in self.mean_ft_data]

        for key, value in zip(("fx", "fy", "fz", "tx", "ty", "tz"), mean_ft):
            self.indicators[key].setText(f"{value:8.3f}")

        self.indicators["raw_bx"].setText(f"{int(raw_bx):8d}")
        self.indicators["raw_by"].setText(f"{int(raw_by):8d}")
        self.indicators["raw_bz"].setText(f"{int(raw_bz):8d}")

        self.indicators["n_bx"].setText(f"{n_bx:8.3f}")
        self.indicators["n_by"].setText(f"{n_by:8.3f}")
        self.indicators["n_bz"].setText(f"{n_bz:8.3f}")

        for i, series in enumerate(self.chart.series()):
            if i < 6:
                series.append(self.max_x, mean_ft[i])
            elif i == 6:
                series.append(self.max_x, n_bx)
            elif i == 7:
                series.append(self.max_x, n_by)
            elif i == 8:
                series.append(self.max_x, n_bz)

        self.max_x += 1
        self.axisX.setRange(max(0, self.max_x - PLOT_WINDOW), self.max_x)

        for series in self.chart.series():
            while len(series.points()) > PLOT_WINDOW:
                series.remove(0)

        if Y_RANGE_PRESETS[self.y_range_combo.currentIndex()][1] is None:
            self._update_auto_range()
            self.apply_y_range()

        # ---- touch-probe feed (sensor panel -> stage panel) ----
        if self.touch_data_queue is not None:
            try:
                self.touch_data_queue.put_nowait((int(time_us), float(mean_ft[2]), float(pz)))
            except Exception:
                try:
                    _ = self.touch_data_queue.get_nowait()
                    self.touch_data_queue.put_nowait((int(time_us), float(mean_ft[2]), float(pz)))
                except Exception:
                    pass

        self.read_counter = 0
        self.mean_ft_data = [0.0] * 6

    def zero_data(self):
        self.K64F.set_data_offset()
        self.status_message.emit("Zeroed")

    # ------------------------------------------------------------------
    def shutdown(self):
        """Stop acquisition and release the Ethernet socket."""
        try:
            self.stop_acquisition()
        except Exception:
            pass
        try:
            self.K64F.close()
        except Exception:
            pass


class LoggingListener(QThread):
    recording_changed = pyqtSignal(bool)

    def __init__(self, logging_queue, K64F):
        super().__init__()
        self.logging_queue = logging_queue
        self.K64F = K64F
        self._running = True

    def run(self):
        while self._running:
            try:
                logging_flag, sequence_name = self.logging_queue.get()
            except Exception:
                continue
            if logging_flag is True:
                self.K64F.set_sequence_name(sequence_name)
                self.K64F.enable_logging()
                self.recording_changed.emit(True)
            else:
                self.K64F.disable_logging()
                self.recording_changed.emit(False)

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Standalone use (sensor panel only) - the normal entry point is main.py
# ---------------------------------------------------------------------------
def main(logging_queue=None, touch_data_queue=None):
    app = QApplication(sys.argv)
    app.setStyleSheet(theme.stylesheet())

    window = QMainWindow()
    window.setWindowTitle("Sensor Display")
    panel = SensorPanel(logging_queue or queue.Queue(),
                        touch_data_queue or queue.Queue(maxsize=200))
    window.setCentralWidget(panel)
    window.resize(1000, 460)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
