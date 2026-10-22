# Turning ATI Rig into a Windows program

> **⚠️ Always build with:**
> ```
> packaging\build.bat
> ```
> **Never** run `pyinstaller` directly against `main.py` from the command
> line, even with flags that look equivalent (`--windowed`, `--exclude-module
> PyQt5`, etc.). `packaging\ATI_Rig.spec` is the single source of truth for
> this build: it excludes unused pylablib device drivers (Andor, Basler, PCO,
> NI...) and unused GUI toolkits (PyQt5, matplotlib, seaborn, tkinter,
> customtkinter...), points the icon and data files at the right paths, and
> sets the console window state. A manual `pyinstaller main.py ...` call skips
> all of that, silently writes a second, inconsistent `ATI_Rig.spec` into the
> **project root** (not `packaging/`), and produces a much larger, unvetted
> build that may not even start correctly. If you ever see an `ATI_Rig.spec`
> sitting next to `main.py` in the project root, it was created by such a
> manual call — it is safe to delete (`Remove-Item ATI_Rig.spec`); the real
> one lives in `packaging/ATI_Rig.spec` and should not be touched casually.

The short answer to "does it need to be an installed program?" is **no**.
An installer (Inno Setup, MSI) only copies files and makes a shortcut — it does
not make the program more reliable. For one rig PC, and for the two or three
other machines this may ever be copied to, a **portable folder plus a shortcut**
is simpler and easier to update: replace the folder, done. There is a note at
the end for the day a real installer is wanted.

---

## 1. Build it (once, on a Windows PC with the project working)

Open a terminal in the project root — the folder that holds `main.py` — using
the same Python environment PyCharm runs the app with. In PyCharm:
**Terminal** tab at the bottom, which is already in the right environment.

```
packaging\build.bat
```

The first build takes a few minutes. When it finishes you have:

```
dist\ATI_Rig\
    ATI_Rig.exe          <- this is the program
    _internal\           <- Python, PyQt6, pylablib, everything it needs
```

No Python, no PyCharm and no `pip install` is needed on any machine that runs
this folder.

## 2. Put it where it belongs

Copy the whole **`dist\ATI_Rig`** folder somewhere permanent, for example:

```
C:\ATI_Rig
```

Keep the folder together — `ATI_Rig.exe` will not run without `_internal`
beside it.

## 3. Make the shortcut

> **If Windows refuses to start the .exe** with *"Windows cannot access the
> specified device, path or file"*, or a *"From your organisation — Action
> blocked"* notification appears, the machine is under a managed security
> policy that forbids running executables from the root of `C:`. University of
> Many managed (e.g. university) machines behave this way. Install into the user
> profile instead — `C:\Users\<you>\ATI_Rig` — which the policy allows. Nothing
> else changes: `lib\paths.py` resolves `sequences\`, `output\`, `config\` and
> `logs\` against the location of the `.exe`, so the data folders follow it.

* **Desktop:** right-click `ATI_Rig.exe` → *Show more options* → *Send to* →
  *Desktop (create shortcut)*. Rename the shortcut to `ATI Rig`.
  On a machine where the desktop is redirected (e.g. OneDrive-synced, or a
  localised folder name on a non-English Windows), `[Environment]::GetFolderPath("Desktop")`
  in PowerShell gives the real path if the shortcut ends up in the wrong place.
* **Taskbar:** start the program once, then right-click its taskbar icon →
  *Pin to taskbar*.
* **Start menu:** press `Win+R`, type `shell:programs`, Enter, and drop a copy
  of the shortcut in the folder that opens. It then appears when you search
  "ATI" in the Start menu.

From then on the rig is one click away.

---

## Where the files go

On first run the program creates these **beside `ATI_Rig.exe`**:

| Folder      | Contents                                        |
|-------------|-------------------------------------------------|
| `sequences\`| saved sequence `.txt` files                      |
| `output\`   | recorded measurement CSVs                        |
| `config\`   | `ui.json` — remembered theme and font size       |
| `logs\`     | `ATI_Rig_<date>.log` — everything the app printed |

This matters: started from a shortcut, Windows does *not* set the working
directory to the program folder, so relative paths like `output/` would have
scattered files into `C:\Windows\System32`. `lib\paths.py` resolves everything
against the location of the `.exe` instead, which is also why the whole folder
can be moved anywhere.

The log folder is the one to look in when something misbehaves and no terminal
was open: it holds the same `[INFO] Stage 27253425: homed=...` lines you see in
PyCharm. Logs older than 30 days are deleted automatically.

## The console window

`SHOW_CONSOLE` at the top of `packaging\ATI_Rig.spec` controls whether a black
terminal window stays open behind the GUI:

* `True` — diagnostics appear live, exactly like running from PyCharm. Useful
  while commissioning a rig or chasing a new problem.
* `False` — **current setting.** The app looks like any normal Windows
  program, with no console window. Nothing is lost: the same `[INFO]/[WARN]/
  [SAFETY]` lines still go to `logs\ATI_Rig_<date>.log` — that log file is the
  only place to look if something goes wrong with no console to read.

Flip it back to `True` and rebuild if the program ever misbehaves in a way
that's easier to chase live than after the fact from the log.

## Updating after a code change

```
packaging\build.bat
```

then copy the new `dist\ATI_Rig` over the old folder. **Do not delete the old
folder first** if you want to keep the `sequences`, `output` and `config` that
have accumulated inside it — copy the new files *into* it and let Windows
overwrite. The shortcut keeps working; it points at the path, not the build.

## If the build fails

* **A confirmation prompt (`... will be REMOVED! Continue? (y/N)`) appears and
  the terminal seems to hang** — this is normal; PyInstaller is asking before
  it clears the previous `dist\ATI_Rig`. Type `Y` and Enter. `build.bat`
  already passes `--noconfirm`, so this should not normally appear when
  building through it — if it does, make sure no previous `ATI_Rig.exe` is
  still running (`Get-Process ATI_Rig | Stop-Process -Force`) and no terminal
  or Explorer window has `dist\ATI_Rig` open, then rebuild.
* **`PermissionError` / `Access is denied` on a file inside `dist\ATI_Rig`
  during build** — the previous `.exe` is still running in the background, or
  a terminal/Explorer window has that folder open. Close them
  (`Get-Process ATI_Rig -ErrorAction SilentlyContinue | Stop-Process -Force`)
  and rebuild.
* **`ModuleNotFoundError` when the .exe starts, but the app works in PyCharm** —
  PyInstaller missed a lazily imported module. Add its name to `hiddenimports`
  in `ATI_Rig.spec` and rebuild.
* **`Failed to execute script`, window flashes and closes** — set
  `SHOW_CONSOLE = True` in `ATI_Rig.spec` and rebuild, then run the `.exe`
  from a terminal to see the error live; or check `logs\ATI_Rig_<date>.log`
  in the built folder without rebuilding.
* **"multiple Qt bindings packages" error during build** — `pylablib` pulls in
  PyQt5 as a transitive dependency. `packaging\ATI_Rig.spec` already excludes
  it (`excludes=[..., "PyQt5", ...]`); seeing this error means `pyinstaller`
  was called some other way than `packaging\build.bat` — see the warning at
  the top of this document.
* **Antivirus quarantines the .exe** — common with PyInstaller output and
  harmless. Add the folder to the exclusions, or sign the executable.
* **Thorlabs stages not found from the .exe but fine in PyCharm** — the Kinesis
  driver DLLs come from the Thorlabs installation on the PC, not from this
  folder. Kinesis must still be installed on the rig machine.
* **The built .exe just prints "Hi, PyCharm" and exits** — opening the project
  folder in PyCharm can overwrite `main.py` with its sample script. Run
  `git status` after opening the project, and `git checkout -- main.py` if it
  shows as modified.
---

## If a real installer is wanted later

Install [Inno Setup](https://jrsoftware.org/isinfo.php) and point a short
script at the built folder. It gives a normal `Setup.exe`, an entry in
"Apps & features", and automatic Start-menu shortcuts:

```iss
[Setup]
AppName=ATI Rig
AppVersion=1.2
DefaultDirName={autopf}\ATI_Rig
DefaultGroupName=ATI Rig
OutputBaseFilename=ATI_Rig_Setup
UninstallDisplayIcon={app}\ATI_Rig.exe

[Files]
Source: "dist\ATI_Rig\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ATI Rig";           Filename: "{app}\ATI_Rig.exe"
Name: "{autodesktop}\ATI Rig";     Filename: "{app}\ATI_Rig.exe"
```

One caveat: installing into `C:\Program Files` makes the program folder
read-only for normal users, and `sequences\`, `output\` and `logs\` then cannot
be written beside the `.exe`. If you go this route, `lib\paths.py` should be
changed to put the data folders under `%LOCALAPPDATA%\ATI_Rig` instead. That is
a small edit to `_data_root()` — but it is a reason to stay with the portable
folder unless an installer is really needed.
