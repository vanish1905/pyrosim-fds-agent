"""Entrypoint for both the desktop workbench and its MCP stdio service."""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import json
import os
from pathlib import Path
import sys
import traceback


def check_runtime() -> dict:
    for module in (
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "pywinauto",
        "theng", "numpy", "pandas", "plotly", "pyro_agent.desktop",
    ):
        importlib.import_module(module)
    from PySide6.QtWidgets import QApplication
    from pyro_agent.desktop import Workbench
    app = QApplication.instance() or QApplication([])
    window = Workbench()
    window.show()
    app.processEvents()
    window.close()
    app.quit()
    return {"python": sys.version.split()[0], "ready": True}


def _show_startup_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None, message, "PyroSim / FDS Agent 启动失败", 0x10 | 0x1000,
        )


def _log_directory() -> Path:
    root = Path(os.environ.get("PYROSIM_AGENT_HOME", Path.home() / "AppData" / "Local" / "PyroSimFdsAgent"))
    path = root / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    # Keep the MCP process independent from Qt. PyInstaller bundles Qt DLLs for
    # the GUI, but MCP clients only need the standard-library server and should
    # not fail if GUI plugins or platform libraries are unavailable.
    if "--mcp-server" in sys.argv[1:]:
        from pyro_agent.mcp_server import main as mcp_main

        mcp_main()
        return 0

    if "--check" in sys.argv[1:]:
        print(json.dumps(check_runtime(), ensure_ascii=False))
        return 0

    os.chdir(Path(__file__).resolve().parent)
    log_file = _log_directory() / "workbench.log"
    try:
        with log_file.open("a", encoding="utf-8", buffering=1) as log:
            log.write("\n--- Agent startup ---\n")
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                from pyro_agent.desktop import main as desktop_main
                try:
                    desktop_main()
                except SystemExit as exc:
                    return int(exc.code or 0)
        return 0
    except Exception:
        error = traceback.format_exc()
        error_file = _log_directory() / "startup-error.log"
        error_file.write_text(error, encoding="utf-8")
        _show_startup_error(
            f"Agent did not start. Details are in:\n{error_file}\n\n{error[-1800:]}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
