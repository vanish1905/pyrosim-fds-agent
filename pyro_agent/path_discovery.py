"""Conservative local path discovery for the PyroSim/FDS Agent workbench."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _exe_from_pyrosim_root(value: str | Path) -> Path | None:
    path = Path(value).expanduser()
    if path.is_file() and path.name.casefold() == "pyrosim.exe":
        return path.resolve()
    if path.is_dir():
        for name in ("PyroSim.exe", "pyrosim.exe"):
            candidate = path / name
            if candidate.is_file():
                return candidate.resolve()
    return None


def _windows_pyrosim_registry_paths() -> list[tuple[Path, str]]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    found: list[tuple[Path, str]] = []
    app_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\PyroSim.exe"
    for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU App Paths"),
                        (winreg.HKEY_LOCAL_MACHINE, "HKLM App Paths")):
        try:
            with winreg.OpenKey(hive, app_key) as key:
                value, _ = winreg.QueryValueEx(key, None)
                exe = Path(os.path.expandvars(str(value).strip('"')))
                if exe.is_file():
                    found.append((exe.resolve(), label))
        except OSError:
            pass
    for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU Uninstall"),
                        (winreg.HKEY_LOCAL_MACHINE, "HKLM Uninstall")):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        try:
            with winreg.OpenKey(hive, base) as parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        child_name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, child_name) as child:
                            display, _ = winreg.QueryValueEx(child, "DisplayName")
                            if "pyrosim" not in str(display).casefold():
                                continue
                            location = None
                            for key_name in ("InstallLocation", "DisplayIcon"):
                                try:
                                    location, _ = winreg.QueryValueEx(child, key_name)
                                    break
                                except OSError:
                                    continue
                            if location:
                                location = os.path.expandvars(str(location).strip('"'))
                                candidate = Path(location)
                                exe = candidate if candidate.is_file() else candidate / "PyroSim.exe"
                                if exe.is_file():
                                    found.append((exe.resolve(), label))
                    except OSError:
                        continue
        except OSError:
            pass
    return found


def discover_paths(config: dict) -> dict:
    """Return validated candidates with provenance; never changes configuration.

    Discovery is intentionally bounded to configured paths, environment values,
    PATH, Windows application registration, and standard installation folders.
    It does not crawl drives or infer a case workspace from arbitrary folders.
    """
    candidates: dict[str, list[dict]] = {key: [] for key in ("pyrosim_root", "opencode_exe", "case_root")}

    def add(key: str, value: str | Path | None, source: str, priority: int, validate):
        if not value:
            return
        try:
            normalized = validate(value)
        except (OSError, ValueError, RuntimeError):
            return
        if not normalized:
            return
        text = str(normalized)
        if any(item["path"].casefold() == text.casefold() for item in candidates[key]):
            return
        candidates[key].append({"path": text, "source": source, "priority": priority})

    def pyrosim_root(value):
        exe = _exe_from_pyrosim_root(value)
        return exe.parent if exe else None

    def opencode_exe(value):
        path = Path(value).expanduser()
        if path.is_file() and path.name.casefold() in {"opencode.exe", "opencode"}:
            return path.resolve()
        return None

    def existing_case_root(value):
        path = Path(value).expanduser()
        if path.is_dir():
            return path.resolve()
        return None

    add("pyrosim_root", config.get("pyrosim_root"), "saved setting", 100, pyrosim_root)
    add("pyrosim_root", os.environ.get("PYROSIM_ROOT"), "PYROSIM_ROOT", 95, pyrosim_root)
    for exe, source in _windows_pyrosim_registry_paths():
        add("pyrosim_root", exe, source, 90, pyrosim_root)
    for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env_key)
        if base:
            for relative in (r"Thunderhead Engineering\PyroSim2024", r"Thunderhead Engineering\PyroSim"):
                add("pyrosim_root", Path(base) / relative, f"{env_key} standard install", 50, pyrosim_root)

    add("opencode_exe", config.get("opencode_exe"), "saved setting", 100, opencode_exe)
    which = shutil.which("opencode")
    add("opencode_exe", which, "PATH", 85, opencode_exe)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for relative in (r"Programs\OpenCode\opencode.exe", r"OpenCode\opencode.exe"):
            add("opencode_exe", Path(local) / relative, "LOCALAPPDATA standard install", 55, opencode_exe)

    add("case_root", config.get("case_root"), "saved setting", 100, existing_case_root)
    add("case_root", os.environ.get("PYRO_FDS_CASE_ROOT"), "PYRO_FDS_CASE_ROOT", 90, existing_case_root)

    for items in candidates.values():
        items.sort(key=lambda item: (-item["priority"], item["path"].casefold()))
    return {
        "candidates": candidates,
        "selected": {key: (items[0] if items else None) for key, items in candidates.items()},
        "ambiguous": {key: len(items) > 1 and items[0]["priority"] == items[1]["priority"]
                      for key, items in candidates.items()},
        "policy": "validated bounded discovery; no drive crawl and no automatic config writes",
    }
