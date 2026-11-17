"""Safe entry points for opening isolated PyroSim model copies.

PyroSim's model file is a Java-serialized binary format. This module deliberately
does not patch that format; it stages a separate copy and opens it through the
Windows file association registered by PyroSim.
"""
from __future__ import annotations

import hashlib
import ctypes
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from ctypes import wintypes

from .config import case_root, runtime_paths
from .fds_tools import ToolError

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _find_pyrosim_title_for_path(model_path: Path) -> str | None:
    """Return a visible PyroSim title containing this document path, if present."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    expected = os.path.normcase(str(model_path.resolve()))
    matches: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        value = title.value
        if "pyrosim" in value.lower() and expected in os.path.normcase(value):
            matches.append(value)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return matches[0] if matches else None


def open_model_copy(config: dict, model_path: str) -> dict:
    """Copy an existing workspace PSM to an isolated folder and request opening it."""
    if os.name != "nt":
        raise ToolError("PyroSim GUI 模型入口目前只支持 Windows。")
    if not isinstance(model_path, str) or not model_path.strip():
        raise ToolError("model_path 必须是案例工作区内的 .psm 文件路径。")

    root = case_root(config).resolve()
    requested = Path(model_path).expanduser()
    source = (requested if requested.is_absolute() else root / requested).resolve()
    if not _inside(root, source):
        raise ToolError("模型必须位于配置的案例工作区内；不接受工作区外的路径。")
    if source.suffix.lower() != ".psm" or not source.is_file():
        raise ToolError("只支持工作区内现存的 .psm 模型文件。")

    pyrosim_root, _, _ = runtime_paths(config)
    pyrosim_exe = pyrosim_root / "PyroSim.exe"
    if not pyrosim_exe.is_file():
        pyrosim_exe = pyrosim_root / "pyrosim.exe"
    if not pyrosim_exe.is_file():
        raise ToolError(f"找不到 PyroSim 可执行文件：{pyrosim_root}")

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    work_root = (root / "pyrosim_agent_prototype" / "working").resolve()
    if not _inside(root, work_root):
        raise ToolError("隔离副本目录越界。")
    work_root.mkdir(parents=True, exist_ok=True)
    if not _inside(root, work_root.resolve()):
        raise ToolError("隔离副本目录不能通过符号链接逃逸出案例工作区。")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", source.stem)[:60] or "model"
    destination = work_root / f"{stem}_agent_{stamp}.psm"
    shutil.copy2(source, destination)
    copied_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    if copied_hash != source_hash:
        destination.unlink(missing_ok=True)
        raise ToolError("隔离副本校验失败；没有请求 PyroSim 打开。")

    started = time.monotonic()
    try:
        # Windows registry points .psm to the installed PyroSim executable.
        # ShellExecute also lets PyroSim route the document to an existing GUI.
        os.startfile(str(destination))  # type: ignore[attr-defined]
    except OSError as exc:
        raise ToolError(f"已生成隔离副本，但 Windows 未能请求 PyroSim 打开：{exc}") from exc

    # Confirm the exact copied document appears in a visible PyroSim title.
    # This is a bounded signal, not a claim that model contents were parsed.
    deadline = time.monotonic() + 20
    window_title = None
    while time.monotonic() < deadline:
        window_title = _find_pyrosim_title_for_path(destination)
        if window_title:
            break
        time.sleep(0.25)

    try:
        source_hash_after = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        source_hash_after = None
    source_unchanged = source_hash_after == source_hash

    return {
        "source_model": str(source),
        "source_sha256": source_hash,
        "source_sha256_after": source_hash_after,
        "working_copy": str(destination),
        "working_copy_sha256": copied_hash,
        "source_unchanged": source_unchanged,
        "pyrosim_exe": str(pyrosim_exe),
        "launch_requested": True,
        "gui_open_verified": bool(window_title),
        "window_title": window_title,
        "duration_seconds": round(time.monotonic() - started, 3),
        "note": "gui_open_verified 仅表示可见 PyroSim 窗口标题包含副本路径；当前 MCP 工具仍不会声称已读取或修改模型内容。",
    }
