# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build for ATI_App.

Produces  dist/ATI_App/ATI_App.exe  - a one-folder Windows application that
runs with no Python installed and no PyCharm. Build it with:

    cd <project root>
    pyinstaller packaging\\ATI_App.spec --noconfirm

The data folders (sequences, output, config, logs) are created next to the
.exe on first run, so the whole dist\\ATI_App folder can be copied anywhere -
including onto the rig PC - and stays self-contained.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller and holds the folder of THIS spec file.
# Building everything relative to it means the build works no matter which
# directory you happen to run pyinstaller from.
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# ---------------------------------------------------------------------------
# Show a console window behind the GUI?
#
#   True  - the [INFO]/[WARN]/[SAFETY] diagnostics appear in a black terminal
#           window as they do in PyCharm. Useful while commissioning.
#   False - GUI only, the way a finished Windows program looks. Nothing is
#           lost: the same output is written to logs\\ATI_App_<date>.log.
#
# Set back to True if the program ever misbehaves and you want the diagnostics
# live rather than reading them out of the log afterwards.
# ---------------------------------------------------------------------------
SHOW_CONSOLE = False

# pylablib ships device definition data and imports its backends lazily, so
# static analysis alone misses them - collect the package wholesale.
pylablib_datas, pylablib_binaries, pylablib_hidden = collect_all("pylablib")

# ...but "wholesale" means every instrument pylablib supports (Andor, Basler,
# PCO, NI, ...) plus its own Qt GUI toolkit, none of which this app touches.
# Keeping them would drag matplotlib and a pile of camera SDK wrappers into the
# build. This rig uses exactly one pylablib driver.
_UNUSED_PREFIXES = (
    "pylablib.gui", "pylablib.widgets", "pylablib.thread",
    "pylablib.aux_libs", "pylablib.legacy", "pylablib.misc",
)


def _needed(module: str) -> bool:
    if module.startswith(_UNUSED_PREFIXES):
        return False
    # Device drivers: keep Thorlabs, drop the rest.
    if module.startswith("pylablib.devices."):
        return module.startswith("pylablib.devices.Thorlabs")
    return True


hiddenimports = (
    [m for m in pylablib_hidden if _needed(m)]
    + collect_submodules("serial")          # pyserial port enumeration on Windows
    + [
        "PyQt6.QtCharts",                   # the live chart
        "serial.tools.list_ports",
        "serial.tools.list_ports_windows",
    ]
)

a = Analysis(
    [os.path.join(PROJECT_ROOT, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=pylablib_binaries,
    datas=pylablib_datas + [(os.path.join(SPECPATH, "app_icon.ico"), ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Only things nothing in the dependency graph can legitimately need.
    #
    # pandas and scipy are NOT listed: pylablib imports pandas the moment it is
    # loaded (core/utils/dictionary.py), so excluding it produced a build that
    # compiled cleanly and then died with ModuleNotFoundError on start-up.
    # Size is controlled by the hidden-import filter above instead - that is
    # the safe lever, because a module the graph never reaches is never bundled
    # whether or not it appears here.
    excludes=[
        "matplotlib", "seaborn", "pytest",
        "tkinter", "customtkinter", "PyQt5", "PySide6", "IPython", "notebook",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ATI_App",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=SHOW_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ATI_App",
)
