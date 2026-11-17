"""Fast Win32 discovery for visible PyroSim Results windows.

Keep process/window discovery out of pywinauto's global UIA enumeration. The
UIA layer is still used for control details, but only after a concrete HWND is
selected and under a watchdog in the read-only inspect tool.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


def visible_results_windows(viewer_exe: Path, expected_smv: Path | None = None) -> list[dict]:
    if os.name != "nt":
        return []
    from .pyrosim_ui_bridge import _process_image

    user32 = ctypes.windll.user32
    enum_window = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_window, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    expected = os.path.normcase(os.path.realpath(str(viewer_exe)))
    expected_title = os.path.normcase(os.path.realpath(str(expected_smv))) if expected_smv else None
    expected_name = expected_smv.name.casefold() if expected_smv else None
    windows: list[dict] = []

    def visit(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(min(length + 1, 4096))
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            image = _process_image(int(pid.value))
            image_matches = bool(image and os.path.normcase(os.path.realpath(image)) == expected)
            title_normalized = os.path.normcase(title_buffer.value.replace("/", "\\"))
            # Some Results builds hand the visualization to Smokeview or show
            # only the .smv name in the title. Accept that window only when the
            # requested file is explicitly named, avoiding broad process-name
            # matches that could select an unrelated viewer window.
            title_matches = bool(expected_title and expected_title in title_normalized)
            if not title_matches and expected_name:
                title_matches = expected_name in title_buffer.value.casefold()
            if image_matches or title_matches:
                windows.append({"handle": int(hwnd), "pid": int(pid.value),
                                "title": title_buffer.value, "process_image": image,
                                "viewer_process_match": image_matches,
                                "expected_smv_title_match": title_matches})
        except Exception:
            pass
        return True

    user32.EnumWindows(enum_window(visit), 0)
    return windows
