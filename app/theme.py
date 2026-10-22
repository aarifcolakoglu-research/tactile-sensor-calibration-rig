"""
Shared visual language for the ATI Rig application.

One place for the palette, the series colours and the stylesheet, so the stage
panel and the sensor panel look like one instrument rather than two windows
that happen to be stacked.

Light and dark palettes, plus a font scale for small laptop screens, are
switched at runtime from the View menu and remembered in config/ui.json.
"""

import json
import os
import tempfile

from lib import paths

# Spin-box arrows are painted to small PNGs and referenced from the stylesheet.
# Qt's QSS border-triangle trick renders a pin-prick on Windows, and it has no
# reliable SVG-in-QSS path, so a real raster icon is the dependable option.
_ARROW_CACHE = {}


def _arrow_icon(direction: str, colour: str, box: int) -> str:
    key = (direction, colour, box)
    cached = _ARROW_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QPolygonF

    img = QImage(box, box, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    m = box * 0.30            # margin
    mid = box / 2.0
    if direction == "up":
        pts = [QPointF(m, box - m), QPointF(box - m, box - m), QPointF(mid, m)]
    else:
        pts = [QPointF(m, m), QPointF(box - m, m), QPointF(mid, box - m)]
    p.drawPolygon(QPolygonF(pts))
    p.end()

    path = os.path.join(tempfile.gettempdir(),
                        f"ati_arrow_{direction}_{colour.lstrip('#')}_{box}.png")
    img.save(path, "PNG")
    _ARROW_CACHE[key] = path
    return path


def _arrow_url(direction: str, colour: str, box: int) -> str:
    return _arrow_icon(direction, colour, box).replace("\\", "/")

# Beside the executable when frozen, beside the project when run from source -
# never in whatever folder a desktop shortcut happens to start in.
SETTINGS_FILE = paths.config_file("ui.json")

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------
LIGHT = {
    # "CHROME_*" is the frame: window ground, group-box titles, menu and status bars.
    # Everything else is the "panel": card interiors, lists, inputs, the chart.
    "CHROME_BG": "#eef0f4",
    "CHROME_TEXT": "#1e2430",
    "CHROME_TEXT_MUTED": "#6b7280",
    "CHROME_BORDER": "#d3d8e0",
    "BG": "#eef0f4",
    "PANEL": "#ffffff",
    "PANEL_ALT": "#f7f8fa",
    "BORDER": "#d3d8e0",
    "BORDER_STRONG": "#b9c0cb",
    "TEXT": "#1e2430",
    "TEXT_MUTED": "#6b7280",
    "TEXT_FAINT": "#9aa1ad",
    "ACCENT": "#1668c1",
    "ACCENT_DARK": "#0f4f96",
    "ACCENT_SOFT": "#eaf1fb",
    "OK": "#1a7f45",
    "WARN": "#b45309",
    "DANGER": "#b3261e",
    "DANGER_SOFT": "#fbeaea",
    "GRID": "#e8ebf0",
    # The chart is drawn in two tones: a grey surround carrying the axis
    # labels, and a lighter plot rectangle inside it.
    "CHART_FRAME": "#e9edf2",
    "PLOT_BG": "#ffffff",
    "POSITION_COLOR": "#334155",
    "SERIES": {
        "px": "#64748b", "py": "#475569", "pz": "#1e293b",
        "fx": "#d62728", "fy": "#1f6fb4", "fz": "#2e8b3d",
        "tx": "#8b5cb8", "ty": "#e07b00", "tz": "#8c564b",
        "raw_bx": "#b02a70", "raw_by": "#565c66", "raw_bz": "#0b7d88",
        "n_bx": "#d63b8f", "n_by": "#6b7280", "n_bz": "#0f9aa8",
    },
}

DARK = {
    # Neutral dark greys - easier on the eyes than a near-black ground and
    # kinder to the coloured traces.
    "CHROME_BG": "#2b2f35",
    "CHROME_TEXT": "#e8eaed",
    "CHROME_TEXT_MUTED": "#b0b6bf",
    "CHROME_BORDER": "#454a53",
    "BG": "#2b2f35",
    "PANEL": "#363b43",
    "PANEL_ALT": "#3f454e",
    "BORDER": "#4a505a",
    "BORDER_STRONG": "#5b626d",
    "TEXT": "#e8eaed",
    "TEXT_MUTED": "#b0b6bf",
    "TEXT_FAINT": "#8a919b",
    "ACCENT": "#5aa9ff",
    "ACCENT_DARK": "#3d8ae0",
    "ACCENT_SOFT": "#3a4653",
    "OK": "#5ed48c",
    "WARN": "#f0b357",
    "DANGER": "#ff7b7b",
    "DANGER_SOFT": "#4a3033",
    "GRID": "#4a505a",
    "CHART_FRAME": "#31363d",
    "PLOT_BG": "#383e46",
    "POSITION_COLOR": "#ccd3dc",
    # Brighter, higher-chroma variants so the traces stay separable on a dark ground
    "SERIES": {
        "px": "#94a3b8", "py": "#cbd5e1", "pz": "#e2e8f0",
        "fx": "#ff7b7b", "fy": "#6bb2ff", "fz": "#5ed48c",
        "tx": "#c79bff", "ty": "#ffb066", "tz": "#d09a80",
        "raw_bx": "#e56bb0", "raw_by": "#8b929c", "raw_bz": "#3ac0cc",
        "n_bx": "#ff8ccd", "n_by": "#aeb8c4", "n_bz": "#45dbe6",
    },
}

# Mixed: dark frame, light working areas. Readings and traces keep the light
# palette's contrast while the surrounding chrome stays easy on the eyes.
MIXED = dict(LIGHT)
MIXED.update({
    "CHROME_BG": DARK["CHROME_BG"],
    "CHROME_TEXT": DARK["CHROME_TEXT"],
    "CHROME_TEXT_MUTED": DARK["CHROME_TEXT_MUTED"],
    "CHROME_BORDER": DARK["CHROME_BORDER"],
    "BG": DARK["CHROME_BG"],
})

PALETTES = {"light": LIGHT, "mixed": MIXED, "dark": DARK}
PALETTE_LABELS = [("Light", "light"), ("Mixed  (dark frame)", "mixed"), ("Dark", "dark")]

MONO = "Consolas, 'DejaVu Sans Mono', 'Courier New', monospace"

# ---------------------------------------------------------------------------
# Nano17 sensing ranges (calibration SI-25-0.25)
# ---------------------------------------------------------------------------
# If the transducer is ever recalibrated, change these three numbers; the chart
# scale presets and the note under the chart follow automatically.
#   SI-12.5-0.125 : Fx/Fy +-12.5 N, Fz +-17.5 N, T +-125 N.mm
#   SI-25-0.25    : Fx/Fy +-25 N,   Fz +-35 N,   T +-250 N.mm
#   SI-50-0.5     : Fx/Fy +-50 N,   Fz +-70 N,   T +-500 N.mm
NANO17_CALIBRATION = "SI-25-0.25"
NANO17_FXY_N = 25.0
NANO17_FZ_N = 35.0
NANO17_TORQUE_NMM = 250.0

NANO17_RANGE_TEXT = (
    f"Nano17 {NANO17_CALIBRATION} — "
    f"Fx/Fy ±{NANO17_FXY_N:.0f} N · Fz ±{NANO17_FZ_N:.0f} N · "
    f"Tx/Ty/Tz ±{NANO17_TORQUE_NMM:.0f} N·mm"
)

# View ▸ Font size range: 70 %–120 %, in 5 % steps in the menu;
# values in between can be entered with View ▸ Font size ▸ Custom….
FONT_SCALE_MIN = 0.70
FONT_SCALE_MAX = 1.20
FONT_SCALE_STEP = 0.05

FONT_SCALES = []
for _i in range(int(round((FONT_SCALE_MAX - FONT_SCALE_MIN) / FONT_SCALE_STEP)) + 1):
    _v = round(FONT_SCALE_MIN + _i * FONT_SCALE_STEP, 2)
    FONT_SCALES.append((f"{_v * 100:.0f}%" + ("  (default)" if abs(_v - 1.0) < 1e-6 else ""), _v))

# ---------------------------------------------------------------------------
# Current settings (module-level so every widget reads the same values)
# ---------------------------------------------------------------------------
DEFAULT_MODE = "dark"
DEFAULT_SCALE = 0.85
MODE = DEFAULT_MODE
SCALE = DEFAULT_SCALE

# Names exported for convenience; refreshed by _apply()
BG = PANEL = PANEL_ALT = BORDER = BORDER_STRONG = ""
CHART_FRAME = PLOT_BG = ""
CHROME_BG = CHROME_TEXT = CHROME_TEXT_MUTED = CHROME_BORDER = ""
TEXT = TEXT_MUTED = TEXT_FAINT = ""
ACCENT = ACCENT_DARK = ACCENT_SOFT = OK = WARN = DANGER = DANGER_SOFT = GRID = ""
POSITION_COLOR = ""
SERIES_COLORS = {}


def _apply():
    g = globals()
    palette = PALETTES[MODE]
    for key, value in palette.items():
        if key == "SERIES":
            g["SERIES_COLORS"] = value
        else:
            g[key] = value


def px(size: float) -> int:
    """Scale a design pixel size by the current font scale."""
    return max(1, round(size * SCALE))


# How far a QGroupBox's frame starts below the top of the widget: the title
# sits in this margin, straddling the border. Shared with the stage tool
# column (ATI.StagePanel) so its buttons start at the axis cards'
# FRAME, not at the top of their titles.
GROUPBOX_MARGIN_TOP = 15


def pt(size: float) -> int:
    """Scale a point size (used for the chart's own fonts)."""
    return max(6, round(size * SCALE))


def set_mode(mode: str):
    global MODE
    if mode in PALETTES:
        MODE = mode
        _apply()


def set_scale(scale: float):
    global SCALE
    SCALE = max(FONT_SCALE_MIN, min(FONT_SCALE_MAX, float(scale)))


def is_dark() -> bool:
    return MODE == "dark"


# The status bar costs a row of height. Worth reclaiming on a small screen,
# so it is a remembered preference rather than a fixture.
SHOW_STATUS_BAR = True

# The numeric strip under the chart. Hiding it gives its height to the plot;
# the chart legend takes over the job of identifying the traces.
SHOW_READOUT = True

# Which channels are drawn on the live chart. Every readout channel has a
# checkbox; force + torque are on by default, position / raw Teensy / normalised
# Teensy are opt-in (they live on very different scales).
CHART_CHANNELS_DEFAULT = ["fx", "fy", "fz", "tx", "ty", "tz"]
CHART_CHANNELS = list(CHART_CHANNELS_DEFAULT)

# Where the window was and how big, plus how the stage/sensor splitter was
# divided, so the app reopens the way it was left. Stored as the base64 of
# QMainWindow.saveGeometry(), which also carries the maximised state and which
# monitor the window was on. None means "never saved" - use the defaults.
WINDOW_GEOMETRY = None
SPLITTER_SIZES = None          # legacy; no longer used (kept so old ui.json loads)
WINDOW_STATE = None            # QMainWindow.saveState() base64 — the dock layout


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_settings():
    global SHOW_STATUS_BAR, SHOW_READOUT, WINDOW_GEOMETRY, SPLITTER_SIZES
    global WINDOW_STATE, CHART_CHANNELS
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        set_mode(data.get("theme", DEFAULT_MODE))
        set_scale(data.get("font_scale", DEFAULT_SCALE))
        SHOW_STATUS_BAR = bool(data.get("show_status_bar", True))
        SHOW_READOUT = bool(data.get("show_readout", True))

        chans = data.get("chart_channels")
        CHART_CHANNELS = ([str(c) for c in chans] if isinstance(chans, list)
                          else list(CHART_CHANNELS_DEFAULT))

        geometry = data.get("window_geometry")
        WINDOW_GEOMETRY = geometry if isinstance(geometry, str) and geometry else None
        state = data.get("window_state")
        WINDOW_STATE = state if isinstance(state, str) and state else None
        SPLITTER_SIZES = None
    except Exception:
        set_mode(DEFAULT_MODE)
        set_scale(DEFAULT_SCALE)
        SHOW_STATUS_BAR = True
        SHOW_READOUT = True
        CHART_CHANNELS = list(CHART_CHANNELS_DEFAULT)
        WINDOW_GEOMETRY = None
        WINDOW_STATE = None
        SPLITTER_SIZES = None


def save_settings():
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"theme": MODE, "font_scale": SCALE,
                       "show_status_bar": SHOW_STATUS_BAR,
                       "show_readout": SHOW_READOUT,
                       "chart_channels": CHART_CHANNELS,
                       "window_geometry": WINDOW_GEOMETRY,
                       "window_state": WINDOW_STATE}, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save UI settings: {e}")


_apply()


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------
def stylesheet() -> str:
    # Spin-box geometry, worked out here rather than inline so the step buttons
    # land exactly on the padding box: any overflow would paint over the 1px
    # frame and hide the focus outline along the top and bottom edges.
    sb_pad_v = px(3)              # field's vertical padding
    sb_content = px(28)           # field's content height (locked below)
    sb_inner = sb_content + 2 * sb_pad_v     # == padding-box height
    sb_btn_w = px(30)             # one step button
    sb_arrow = px(15)
    # Combo boxes carry different padding; give them the content height that
    # lands on the same overall height, so a spin box and a combo sitting on one
    # row (the Pause dialog) line up.
    cmb_content = max(px(14), sb_inner - 2 * px(4))
    return f"""
    QWidget {{
        background: {CHROME_BG};
        color: {CHROME_TEXT};
        font-family: "Segoe UI", "Inter", system-ui, sans-serif;
        font-size: {px(13)}px;
    }}

    QMenuBar {{ background: {CHROME_BG}; color: {CHROME_TEXT};
                border-bottom: 1px solid {CHROME_BORDER}; }}

    /* Thin handle between the dock panes. */
    QMainWindow::separator {{ background: {CHROME_BORDER}; width: 3px; height: 3px; }}
    QMainWindow::separator:hover {{ background: {ACCENT}; }}
    QDockWidget::title {{ background: {PANEL}; padding: {px(3)}px {px(8)}px; }}
    QMenuBar::item {{ padding: {px(4)}px {px(10)}px; background: transparent; }}
    QMenuBar::item:selected {{ background: {ACCENT_SOFT}; }}
    QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER};
             padding: {px(4)}px; }}
    QMenu::item {{ padding: {px(5)}px {px(22)}px; }}
    QMenu::item:selected {{ background: {ACCENT_SOFT}; }}

    QGroupBox {{
        background: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
        margin-top: {px(GROUPBOX_MARGIN_TOP)}px;
        padding: {px(8)}px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        padding: 0 4px;
        color: {CHROME_TEXT_MUTED};
        font-size: {px(12)}px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.6px;
    }}

    QLabel {{ background: transparent; color: {TEXT}; }}
    QLabel#SectionLabel {{
        color: {TEXT_MUTED};
        font-size: {px(11)}px;
        font-weight: 700;
        letter-spacing: 0.6px;
        text-transform: uppercase;
        padding-top: {px(4)}px;
    }}
    QLabel#Hint {{ color: {TEXT_FAINT}; font-size: {px(11)}px; }}

    QPushButton {{
        background: {PANEL};
        color: {TEXT};
        border: 1px solid {BORDER_STRONG};
        border-radius: 5px;
        padding: {px(6)}px {px(11)}px;
        min-height: {px(20)}px;
    }}
    QPushButton:hover  {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; }}
    /* Pressed used to reuse :hover's light tint verbatim, so clicking a
       button looked identical to merely resting the mouse on it - no
       "give" at all. A pressed button now goes to the accent colour (a
       step darker than hover, not lighter) and its content nudges down
       and right by 2px within the same footprint (padding shifts by +2/-2
       on opposite sides, so the button's own size does not jump). */
    QPushButton:pressed {{
        background: {ACCENT}; border-color: {ACCENT_DARK}; color: #ffffff;
        padding: {px(8)}px {px(13)}px {px(4)}px {px(9)}px;
    }}
    QPushButton:disabled {{ color: {TEXT_FAINT}; background: {PANEL_ALT};
                            border-color: {BORDER}; }}
    QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT_DARK};
                           color: #ffffff; font-weight: 600; }}
    QPushButton:checked:pressed {{ background: {ACCENT_DARK}; }}

    QPushButton#Primary {{
        background: {ACCENT}; border-color: {ACCENT_DARK};
        color: #ffffff; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {ACCENT_DARK}; }}
    /* Was falling through to the generic :pressed above, i.e. ACCENT_SOFT -
       a light tint - so clicking Acquire or Run Sequence made the button
       flash LIGHTER than its own hover state: pressed looked less
       committed than merely hovering. Needs its own, darker still. */
    QPushButton#Primary:pressed {{ background: {ACCENT_DARK}; border-color: {ACCENT_DARK}; }}
    QPushButton#Primary:checked {{ background: {DANGER}; border-color: {DANGER}; }}
    QPushButton#Primary:checked:pressed {{ background: {DANGER}; border-color: {DANGER}; }}

    QPushButton#Danger {{ color: {DANGER}; }}
    QPushButton#Danger:hover {{ background: {DANGER_SOFT}; border-color: {DANGER}; }}
    /* Same fix: was flashing the accent tint on click instead of a danger
       one. */
    QPushButton#Danger:pressed {{ background: {DANGER}; border-color: {DANGER}; color: #ffffff; }}

    QPushButton#RowRemove {{
        border: none; background: transparent; color: {TEXT_FAINT};
        font-size: {px(15)}px; font-weight: 700;
        padding: 0px;
        min-width: {px(18)}px; max-width: {px(18)}px;
        min-height: {px(18)}px; max-height: {px(18)}px;
    }}
    QPushButton#RowRemove:hover {{ color: {DANGER}; background: {DANGER_SOFT};
                                   border-radius: 3px; }}
    QPushButton#RowRemove:pressed {{ color: #ffffff; background: {DANGER};
                                     border-radius: 3px; }}

    QLineEdit, QComboBox {{
        background: {PANEL};
        border: 1px solid {BORDER_STRONG};
        border-radius: 5px;
        padding: {px(4)}px {px(6)}px;
        color: {TEXT};
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
    }}
    QDialog QComboBox {{ min-height: {cmb_content}px; }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}

    /* Spin boxes: fully style the sub-controls, otherwise Qt drops the arrows
       as soon as any stylesheet touches the widget. Buttons are sized for a
       comfortable click target. */
    QAbstractSpinBox {{
        background: {PANEL};
        border: 1px solid {BORDER_STRONG};
        border-radius: 5px;
        padding: {sb_pad_v}px {px(6)}px;
        padding-right: {2 * sb_btn_w + px(4)}px;
        color: {TEXT};
        min-height: {sb_content}px;
        max-height: {sb_content}px;
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
    }}
    QAbstractSpinBox:focus {{ border-color: {ACCENT}; }}
    /* Two step buttons side by side on the right edge. They are anchored to the
       padding box and are exactly as tall as it, so the 1px frame (and the blue
       focus outline) stays visible along the top and bottom. The right corners
       take the frame's inner radius so the rounding is not squared off. */
    QAbstractSpinBox::up-button {{
        subcontrol-origin: padding;
        subcontrol-position: right;
        width: {sb_btn_w}px;
        height: {sb_inner}px;
        margin: 0px;
        background: {PANEL_ALT};
        border-left: 1px solid {BORDER_STRONG};
        border-top-right-radius: 4px;
        border-bottom-right-radius: 4px;
    }}
    QAbstractSpinBox::down-button {{
        subcontrol-origin: padding;
        subcontrol-position: right;
        width: {sb_btn_w}px;
        height: {sb_inner}px;
        margin: 0px {sb_btn_w}px 0px 0px;
        background: {PANEL_ALT};
        border: none;
    }}
    QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
        background: {ACCENT_SOFT};
    }}
    QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
        background: {ACCENT};
    }}
    QAbstractSpinBox::up-arrow {{
        image: url("{_arrow_url('up', TEXT, px(18))}");
        width: {sb_arrow}px; height: {sb_arrow}px;
    }}
    QAbstractSpinBox::down-arrow {{
        image: url("{_arrow_url('down', TEXT, px(18))}");
        width: {sb_arrow}px; height: {sb_arrow}px;
    }}
    QAbstractSpinBox::up-arrow:disabled {{
        image: url("{_arrow_url('up', TEXT_FAINT, px(18))}");
    }}
    QAbstractSpinBox::down-arrow:disabled {{
        image: url("{_arrow_url('down', TEXT_FAINT, px(18))}");
    }}
    QLineEdit#Numeric {{ font-family: {MONO}; }}
    QComboBox::drop-down {{ border: none; width: {px(16)}px; }}
    QComboBox QAbstractItemView {{ background: {PANEL}; color: {TEXT};
                                   selection-background-color: {ACCENT};
                                   selection-color: #ffffff;
                                   border: 1px solid {BORDER}; }}

    QListWidget {{
        background: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 5px;
        outline: none;
    }}
    QListWidget::item {{ border-bottom: 1px solid {BORDER}; }}
    QListWidget::item:selected {{ background: {ACCENT_SOFT}; }}

    QProgressBar {{
        background: {PANEL_ALT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        text-align: center;
    }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

    QCheckBox {{ spacing: {px(5)}px; }}
    QCheckBox::indicator {{ width: {px(14)}px; height: {px(14)}px; border-radius: 3px;
                            border: 1px solid {BORDER_STRONG}; background: {PANEL}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT_DARK}; }}

    QSplitter::handle {{ background: {CHROME_BORDER}; height: 3px; }}
    QSplitter::handle:hover {{ background: {ACCENT}; }}

    QScrollBar:vertical {{ background: transparent; width: {px(11)}px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 5px;
                                   min-height: {px(24)}px; }}
    QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QStatusBar {{ background: {CHROME_BG}; border-top: 1px solid {CHROME_BORDER};
                  color: {CHROME_TEXT_MUTED}; }}
    QStatusBar::item {{ border: none; }}

    /* The chart carries its own frame instead of sitting in a group box:
       a group box costs a title row plus padding, and that height is worth
       more to the plot than the word "Live signal" is. */
    QChartView {{
        background: {CHART_FRAME};
        border: 1px solid {BORDER};
        border-radius: 6px;
    }}
    QFrame#ReadoutStrip {{
        background: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
    }}

    QDialog {{ background: {CHROME_BG}; }}
    QDialog QLabel {{ color: {CHROME_TEXT}; }}

    QToolTip {{
        background: #22303f; color: #ffffff;
        border: none; padding: {px(5)}px {px(7)}px; border-radius: 4px;
        font-size: {px(12)}px;
    }}
    """
