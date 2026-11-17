from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


def bundled_file(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / name


def message(text: str, title: str, flags: int) -> int:
    return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))


def main() -> int:
    title = "PyroSim / FDS Agent Setup"
    if message(
        "Install PyroSim / FDS Agent for the current Windows user?\n\n"
        "This installs the Agent, its local MCP service, and the bundled TEMPERATURE example.\n"
        "PyroSim/FDS and OpenCode are not included.",
        title,
        0x00000004 | 0x00000020,
    ) != 6:
        return 0

    payload = bundled_file("app-payload.zip")
    install_script = bundled_file("install.ps1")
    if not payload.is_file() or not install_script.is_file():
        message("The installer payload is incomplete.", title, 0x00000010)
        return 2

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(install_script),
    ]
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    completed = subprocess.run(
        command,
        cwd=str(payload.parent),
        startupinfo=startupinfo,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode == 0:
        return 0
    message(
        f"Installation failed (exit code {completed.returncode}). Check that this Windows account can write to its Local AppData folder.",
        title,
        0x00000010,
    )
    return completed.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
