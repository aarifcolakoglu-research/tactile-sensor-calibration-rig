"""
Keep the diagnostics when there is no console.

Run from PyCharm, every [INFO]/[WARN]/[SAFETY] line goes to the terminal and
you can read it while troubleshooting. Built as a windowed Windows program
there is no terminal at all, and those lines - which is exactly what we look at
when an axis will not home or the Ethernet board is stuck - would be lost.

start_logging() mirrors stdout and stderr into logs/ATI_Rig_<date>.log beside
the application, keeping the console output as well when there is one.
"""

from __future__ import annotations

import os
import sys
import time

from lib import paths

LOG_DIR = os.path.join(paths.DATA_ROOT, "logs")
KEEP_DAYS = 30


class _Tee:
    """Write to the original stream (if any) and to the log file."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, text):
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                pass
        try:
            self._log.write(text)
            self._log.flush()          # a crash must not lose the last lines
        except Exception:
            pass

    def flush(self):
        for s in (self._stream, self._log):
            try:
                if s is not None:
                    s.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._stream is not None and self._stream.isatty()
        except Exception:
            return False


def _prune_old_logs():
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for name in os.listdir(LOG_DIR):
            path = os.path.join(LOG_DIR, name)
            if name.endswith(".log") and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def start_logging() -> str | None:
    """Begin mirroring output to a dated log file. Returns its path, or None."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"ATI_Rig_{time.strftime('%Y-%m-%d')}.log")
        log_file = open(path, "a", encoding="utf-8", buffering=1)
    except Exception as e:
        print(f"[WARN] Could not open a log file: {e}")
        return None

    log_file.write(f"\n{'=' * 70}\n"
                   f"Session started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"{'=' * 70}\n")

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)

    _prune_old_logs()
    return path
