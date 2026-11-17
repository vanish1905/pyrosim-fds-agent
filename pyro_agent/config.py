from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "config.json"
if getattr(sys, "frozen", False):
    CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", str(APP_ROOT))) / "PyroSimFdsAgent" / "config.json"
DEFAULT_CONFIG = {
    "pyrosim_root": "",
    "case_root": str(Path.home() / "Documents" / "PyroSimFdsCases"),
    "opencode_exe": "opencode",
    "theme": "dark",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **data}
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    root = Path(data["case_root"]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)


def runtime_paths(config: dict) -> tuple[Path, Path, Path]:
    root = Path(config["pyrosim_root"]).expanduser().resolve()
    return root, root / "fds" / "fds.exe", root / "fds" / "smokeview.exe"


def case_root(config: dict) -> Path:
    root = Path(config["case_root"]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def mcp_server_command(app_root: Path = APP_ROOT) -> list[str]:
    """Return an MCP command that works from both source and frozen installs."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--mcp-server"]
    return [sys.executable, "-m", "pyro_agent.mcp_server"]

