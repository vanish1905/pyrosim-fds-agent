"""Version-profiled, copy-only PyroSim UI automation exposed through MCP.

This bridge intentionally automates only PyroSim's documented FDS import,
Save As, Open, and FDS export workflows. It never edits a source .psm or .fds
file, never starts a PyroSim solve, and checks each UI transition before it
reports success. A supported application version and an interactive Windows
desktop session are required.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ctypes
import traceback
from ctypes import wintypes
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import case_root, runtime_paths
from .events import emit_event
from .fds_tools import ToolError, CASE_ID_RE, _case_file, compare_case_inputs
from .adapter_profiles import resolve_pyrosim_ui_profile

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"PyroSim\s+(\d{4}\.\d+)\.(\d+)", re.IGNORECASE)
UI_TIMEOUT_SECONDS = 15


@dataclass
class UIContext:
    app: Any
    desktop: Any
    main: Any
    pid: int
    version: str
    profile: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _layout(config: dict) -> tuple[Path, Path, Path]:
    root = case_root(config).resolve()
    bridge_root = (root / "pyrosim_agent_gui").resolve()
    if not _inside(root, bridge_root):
        raise ToolError("PyroSim UI 工作区越界。")
    bridge_root.mkdir(parents=True, exist_ok=True)
    if not _inside(root, bridge_root.resolve()):
        raise ToolError("PyroSim UI 工作区不能通过符号链接离开案例目录。")
    working = bridge_root / "working"
    models = bridge_root / "models"
    working.mkdir(exist_ok=True)
    models.mkdir(exist_ok=True)
    if not _inside(root, working.resolve()) or not _inside(root, models.resolve()):
        raise ToolError("PyroSim UI 隔离目录不能通过符号链接离开案例目录。")
    return bridge_root, working, models


def _manifest_path(bridge_root: Path, model_id: str) -> Path:
    if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id):
        raise ToolError("model_id 格式无效；只允许字母、数字、下划线和短横线。")
    return bridge_root / "models" / f"{model_id}.json"


def _load_manifest(bridge_root: Path, model_id: str) -> dict:
    path = _manifest_path(bridge_root, model_id)
    if not path.is_file():
        raise ToolError(f"找不到 UI 工作区模型：{model_id}。先调用 pyrosim_import_fds。")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"PyroSim UI 模型清单不可读：{exc}") from exc
    model_path = Path(record.get("model_path", "")).resolve()
    models_root = (bridge_root / "models").resolve()
    if not _inside(models_root, model_path) or model_path.suffix.lower() != ".psm" or not model_path.is_file():
        raise ToolError("清单中的 PSM 路径无效或不在隔离模型目录内。")
    return record


def _load_pywinauto():
    if os.name != "nt":
        raise ToolError("PyroSim UI 桥只支持 Windows。")
    try:
        from pywinauto import Application, Desktop
    except ImportError as exc:
        raise ToolError(
            "缺少可选依赖 pywinauto；请在项目虚拟环境执行 "
            ".venv\\Scripts\\python.exe -m pip install -r requirements-ui.txt。"
        ) from exc
    return Application, Desktop


def _find_main(desktop, exe_name: str):
    candidates = []
    for window in desktop.windows(visible_only=True):
        try:
            title = window.window_text()
            pid = window.element_info.process_id
            process = _process_image(int(pid))
            if process and Path(process).name.lower() == exe_name.lower() and "pyrosim" in title.lower():
                candidates.append(window)
        except Exception:
            continue
    return candidates[0] if candidates else None


def _process_image(pid: int) -> str | None:
    """Read a visible window's executable path without shelling out to tasklist."""
    query_limited = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(query_limited, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def _native_file_dialog(pid: int) -> tuple[int, str] | None:
    """Find one standard file dialog owned by PyroSim using Win32 HWND APIs."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    matches: list[tuple[int, str]] = []
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    get_owner = user32.GetWindow
    get_owner.argtypes = [wintypes.HWND, wintypes.UINT]
    get_owner.restype = wintypes.HWND
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_pid.restype = wintypes.DWORD

    def visit(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            class_buffer = ctypes.create_unicode_buffer(256)
            title_buffer = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            if class_buffer.value != "#32770" or not re.search(
                r"open|save|import|export|打开|保存|另存|导入|导出", title_buffer.value, re.IGNORECASE
            ):
                return True
            window_pid = wintypes.DWORD()
            get_pid(hwnd, ctypes.byref(window_pid))
            owner = get_owner(hwnd, 4)  # GW_OWNER
            owner_pid = wintypes.DWORD()
            if owner:
                get_pid(owner, ctypes.byref(owner_pid))
            if window_pid.value == pid or owner_pid.value == pid:
                matches.append((int(hwnd), title_buffer.value))
        except Exception:
            return True
        return True

    user32.EnumWindows(enum_callback(visit), 0)
    return matches[0] if len(matches) == 1 else None


def _native_set_file_path(pid: int, path: Path) -> tuple[int, str]:
    found = _native_file_dialog(pid)
    if found is None:
        raise ToolError("没有找到唯一且属于 PyroSim 的标准文件对话框。")
    hwnd, title = found
    user32 = ctypes.windll.user32
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.restype = ctypes.c_int
    child_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    edit_matches: list[int] = []

    def visit_child(child, _):
        if user32.GetDlgCtrlID(child) in {1001, 1148}:
            edit_matches.append(int(child))
        return True

    user32.EnumChildWindows.argtypes = [wintypes.HWND, child_callback, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.EnumChildWindows(hwnd, child_callback(visit_child), 0)
    # PyroSim 2024.2 uses ID 1001, often nested inside the filename combo;
    # older common dialogs can expose ID 1148.
    edit_hwnd = edit_matches[0] if len(edit_matches) == 1 else None
    if edit_hwnd is None:
        # Some Windows common dialogs expose the filename Edit through UIA but
        # omit it from EnumChildWindows. Resolve only the unique, known-ID edit.
        try:
            from pywinauto import Desktop
            dialog = Desktop(backend="uia").window(handle=hwnd)
            candidates = [
                item for item in dialog.descendants(control_type="Edit")
                if getattr(item.element_info, "control_id", None) in {1001, 1148}
                and getattr(item.element_info, "handle", 0)
            ]
            if len(candidates) == 1:
                edit_hwnd = int(candidates[0].element_info.handle)
        except Exception:
            pass
    if not edit_hwnd:
        raise ToolError("PyroSim 标准文件框没有已支持的文件名控件 1148/1001；未提交。")
    send_message = user32.SendMessageW
    send_message.restype = ctypes.c_ssize_t
    send_message.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_void_p]
    path_buffer = ctypes.create_unicode_buffer(str(path))
    send_message(edit_hwnd, 0x000C, 0, ctypes.cast(path_buffer, ctypes.c_void_p))  # WM_SETTEXT
    readback = ctypes.create_unicode_buffer(4096)
    send_message(edit_hwnd, 0x000D, len(readback), ctypes.cast(readback, ctypes.c_void_p))  # WM_GETTEXT
    if os.path.normcase(str(path)) not in os.path.normcase(readback.value):
        raise ToolError("PyroSim 文件名控件没有回读到目标路径；未提交。")
    button_hwnd = user32.GetDlgItem(hwnd, 1)
    if not button_hwnd or not user32.IsWindowEnabled(button_hwnd):
        raise ToolError("PyroSim 文件框的打开/保存按钮不可用；未提交。")
    send_message(button_hwnd, 0x00F5, 0, None)  # BM_CLICK
    return hwnd, title


def _native_resolve_fds6_dialog(pid: int) -> str | None:
    """Select and confirm FDS6 only in PyroSim's exact version dialog."""
    user32 = ctypes.windll.user32
    enum_window = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_child = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_window, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, enum_child, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.restype = ctypes.c_int
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    send_message = user32.SendMessageW
    send_message.restype = ctypes.c_ssize_t
    send_message.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_void_p]

    candidates: list[int] = []

    def find_dialog(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, len(title))
        if not re.search(r"^FDS Version Selection Dialog$", title.value, re.IGNORECASE):
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            candidates.append(int(hwnd))
        return True

    user32.EnumWindows(enum_window(find_dialog), 0)
    if len(candidates) != 1:
        return None
    hwnd = candidates[0]
    children: list[tuple[int, str, str, int]] = []

    def collect_child(child, _):
        class_name = ctypes.create_unicode_buffer(128)
        title = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, class_name, len(class_name))
        user32.GetWindowTextW(child, title, len(title))
        children.append((int(child), class_name.value, title.value, user32.GetDlgCtrlID(child)))
        return True

    user32.EnumChildWindows(hwnd, enum_child(collect_child), 0)
    combo_handles = [child for child, cls, _, _ in children if cls.lower() == "combobox"]
    if len(combo_handles) != 1:
        # This PyroSim profile renders the dialog in Java and may expose no
        # native child controls. The UIA/captured-window probe for 2024.2.1209
        # showed FDS6 selected and the default action focused on OK. Confirm
        # that exact visible default through the modal's Enter key; callers
        # must report that this was not a programmatic selection readback.
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        try:
            from pywinauto.keyboard import send_keys
        except ImportError as exc:
            raise ToolError("FDS 版本框控件未暴露，且 pywinauto 键盘发送不可用。") from exc
        send_keys("{ENTER}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and user32.IsWindow(hwnd):
            time.sleep(0.1)
        if user32.IsWindow(hwnd):
            raise ToolError(f"FDS 版本框未关闭；控件不可读，未确认选择：{children!r}")
        return "visible_default"
    combo = combo_handles[0]
    count = int(send_message(combo, 0x0146, 0, None))  # CB_GETCOUNT
    options = []
    for index in range(max(0, min(count, 32))):
        length = int(send_message(combo, 0x0149, index, None))  # CB_GETLBTEXTLEN
        buffer = ctypes.create_unicode_buffer(min(max(length + 1, 2), 256))
        send_message(combo, 0x0148, index, ctypes.cast(buffer, ctypes.c_void_p))  # CB_GETLBTEXT
        options.append(buffer.value)
    fds6_index = next((i for i, item in enumerate(options) if re.sub(r"\s+", "", item).lower() in {"fds6", "fds6.0"}), None)
    if fds6_index is None:
        raise ToolError(f"FDS 版本框没有明确的 FDS6 选项：{options!r}")
    send_message(combo, 0x014E, fds6_index, None)  # CB_SETCURSEL
    selected_index = int(send_message(combo, 0x0147, 0, None))  # CB_GETCURSEL
    if selected_index != fds6_index:
        raise ToolError(f"FDS 版本框未能确认 FDS6 选择：{options!r}, selected={selected_index}")
    ok = next((child for child, _, title, _ in children if title.strip().lower() in {"ok", "确定"}), None)
    if ok is None:
        raise ToolError(f"FDS 版本框没有明确的 OK/确定按钮：{children!r}")
    send_message(ok, 0x00F5, 0, None)  # BM_CLICK
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and user32.IsWindow(hwnd):
        time.sleep(0.1)
    if user32.IsWindow(hwnd):
        raise ToolError("FDS6 已选中，但 PyroSim 版本对话框未关闭确认。")
    return "win32_readback"


def _connect(config: dict, *, launch: bool = True) -> UIContext:
    Application, Desktop = _load_pywinauto()
    pyrosim_root, _, _ = runtime_paths(config)
    exe = pyrosim_root / "PyroSim.exe"
    if not exe.is_file():
        exe = pyrosim_root / "pyrosim.exe"
    if not exe.is_file():
        raise ToolError(f"找不到 PyroSim.exe：{pyrosim_root}")

    desktop = Desktop(backend="uia")
    main = _find_main(desktop, exe.name)
    app = Application(backend="uia")
    if main is None:
        if not launch:
            raise ToolError("PyroSim 当前未运行。")
        try:
            app.start(f'"{exe}"', work_dir=str(exe.parent), wait_for_idle=False)
        except Exception as exc:
            raise ToolError(f"无法启动 PyroSim：{exc}") from exc
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            main = _find_main(desktop, exe.name)
            if main is not None:
                break
            time.sleep(0.5)
        if main is None:
            raise ToolError("已请求启动 PyroSim，但 45 秒内没有发现可见主窗口。")

    pid = int(main.element_info.process_id)
    try:
        app.connect(process=pid, timeout=10)
    except Exception:
        # The process is already running; use the visible window as the app root.
        pass
    title = main.window_text()
    version_match = VERSION_RE.search(title)
    if not version_match:
        raise ToolError(f"无法从 PyroSim 窗口标题识别版本：{title!r}")
    version = version_match.group(1)
    profile = resolve_pyrosim_ui_profile(version)
    if profile is None:
        raise ToolError(
            f"PyroSim {version} 尚无经过配置的 UI 自动化版本档案；已停止 GUI 操作。"
        )
    return UIContext(app=app, desktop=desktop, main=main, pid=pid,
                     version=version, profile=profile["id"])


def _step(config: dict, operation: str, stage: str, message: str, **data) -> None:
    emit_event(config, "pyrosim_ui_step", message, operation=operation, stage=stage, **data)


def _visible_dialog(context: UIContext, timeout: float = UI_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for window in context.desktop.windows(process=context.pid, visible_only=True):
                if window.handle != context.main.handle and window.is_visible():
                    return window
        except Exception:
            pass
        time.sleep(0.2)
    # The native Windows common file dialog can be hosted outside the main
    # application process on some Windows builds. Only accept a unique visible
    # standard dialog exposing the conventional filename control (ID 1148).
    try:
        candidates = []
        for window in context.desktop.windows(visible_only=True):
            if window.handle == context.main.handle:
                continue
            title = window.window_text()
            if not re.search(r"open|save|import|export|打开|保存|另存|导入|导出", title, re.IGNORECASE):
                continue
            if window.element_info.class_name != "#32770":
                continue
            edits = window.descendants(control_type="Edit")
            if any(getattr(edit.element_info, "control_id", None) == 1148 for edit in edits):
                candidates.append(window)
        if len(candidates) == 1:
            return candidates[0]
    except Exception:
        pass
    return None


def _title(context: UIContext) -> str:
    try:
        context.main.set_focus()
        time.sleep(0.15)
    except Exception:
        pass
    return context.main.window_text()


def _dialog_text(dialog) -> str:
    try:
        return " ".join(item.window_text() for item in dialog.descendants())
    except Exception:
        return dialog.window_text()


def _abort_unexpected_modal(dialog) -> None:
    title = dialog.window_text()
    details = _dialog_text(dialog)
    if re.search(r"warning|警告", title, re.IGNORECASE):
        if "ambient pressure boundary" in details.lower() or "环境压力边界" in details:
            # In the tested PyroSim 2024.2 dialog, OK aborts the unintended
            # pre-run action. Never select Ignore, which could continue it.
            for button in dialog.descendants(control_type="Button"):
                if button.window_text().strip().lower() in {"ok", "确定"}:
                    button.click_input()
                    break
            raise ToolError("PyroSim 出现求解前环境压力边界警告；已选择退出，没有启动求解或继续导出。")
        raise ToolError(f"PyroSim 出现未识别的警告对话框 {title!r}；没有确认或继续该操作。")


def _resolve_fds_version_dialog(context: UIContext) -> bool:
    native_result = _native_resolve_fds6_dialog(context.pid)
    if native_result:
        return True
    modal = _visible_dialog(context, 0.2)
    if modal is None or "version" not in modal.window_text().lower():
        return False
    combos = modal.descendants(control_type="ComboBox")
    if not combos:
        raise ToolError("PyroSim FDS 版本选择框未暴露下拉控件，已停止导入。")
    choices = []
    try:
        choices = combos[0].item_texts()
    except Exception:
        pass
    fds6 = next((choice for choice in choices if re.sub(r"\s+", "", choice).lower() in {"fds6", "fds6.0"}), None)
    if fds6 is None:
        raise ToolError(f"PyroSim 版本选择框没有可确认的 FDS 6 选项：{choices!r}")
    combos[0].select(fds6)
    modal.type_keys("{ENTER}")
    return True


def _enter_file_dialog_path(context: UIContext, path: Path, *, timeout: float = UI_TIMEOUT_SECONDS) -> None:
    if _native_file_dialog(context.pid) is not None:
        _native_set_file_path(context.pid, path)
        return
    dialog = _visible_dialog(context, timeout)
    if dialog is None:
        raise ToolError("PyroSim 没有打开预期的文件对话框；已停止自动化。")
    _abort_unexpected_modal(dialog)
    edits = dialog.descendants(control_type="Edit")
    filename_edit = next((
        edit for edit in edits
        if getattr(edit.element_info, "control_id", None) in {1001, 1148}
    ), None)
    if filename_edit is not None:
        # The native common dialog exposes a disabled UIA ValuePattern on some
        # PyroSim installs despite reporting IsEnabled=true. Use its visible
        # filename mnemonic, then read the value back before activating Open/Save.
        try:
            filename_edit.set_focus()
            filename_edit.type_keys("^a")
            filename_edit.type_keys(str(path), with_spaces=True)
        except Exception as exc:
            raise ToolError(f"PyroSim 文件名输入框无法获得键盘焦点：{type(exc).__name__}: {exc!r}") from exc
        try:
            observed = filename_edit.get_value()
        except Exception:
            observed = filename_edit.window_text()
        if os.path.normcase(str(path)) not in os.path.normcase(str(observed)):
            raise ToolError("PyroSim 文件名输入框没有回读到目标路径；未提交文件对话框。")
        title = dialog.window_text()
        action_pattern = re.compile(r"open|打开" if re.search(r"open|打开|import|导入", title, re.IGNORECASE)
                                    else r"save|保存", re.IGNORECASE)
        buttons = dialog.descendants(control_type="Button")
        action_button = next((
            button for button in buttons
            if getattr(button.element_info, "control_id", None) == 1
        ), None)
        if action_button is None:
            action_button = next((
                button for button in buttons
                if "DropDown" not in str(getattr(button.element_info, "automation_id", ""))
                and action_pattern.search(button.window_text().replace("&", ""))
            ), None)
        if action_button is not None:
            try:
                action_button.click_input()
            except Exception as exc:
                raise ToolError(f"PyroSim 文件框确认按钮无法点击：{type(exc).__name__}: {exc!r}") from exc
        else:
            dialog.type_keys("{ENTER}")
    else:
        # Standard Windows file dialogs label their filename field with Alt+N
        # on both English and the tested Chinese installation.
        try:
            dialog.set_focus()
            dialog.type_keys("%n")
            dialog.type_keys("^a")
            dialog.type_keys(str(path), with_spaces=True)
        except Exception as exc:
            raise ToolError(f"PyroSim 文件名输入框无法获得键盘焦点：{type(exc).__name__}: {exc!r}") from exc
        dialog.type_keys("{ENTER}")


def cancel_file_dialog(config: dict) -> dict:
    """Dismiss only a standard file dialog; never confirms a file operation."""
    context = _connect(config, launch=False)
    native_dialog = _native_file_dialog(context.pid)
    if native_dialog is not None:
        hwnd, title = native_dialog
        user32 = ctypes.windll.user32
        user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetDlgItem.restype = wintypes.HWND
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        button = user32.GetDlgItem(hwnd, 2)
        if not button or not user32.IsWindowEnabled(button):
            raise ToolError("PyroSim 文件框没有可用的 Cancel 按钮；没有发送输入。")
        send_message = user32.SendMessageW
        send_message.restype = ctypes.c_ssize_t
        send_message.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_void_p]
        send_message(button, 0x00F5, 0, None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and user32.IsWindow(hwnd):
            time.sleep(0.1)
        if user32.IsWindow(hwnd):
            raise ToolError("已请求取消 PyroSim 文件框，但 Win32 未确认窗口关闭。")
        _step(config, "cancel_file_dialog", "completed", f"已取消 PyroSim 文件对话框：{title}", verified=True)
        return {"status": "cancelled", "dialog_title": title, "no_file_selected": True}
    dialog = _visible_dialog(context, timeout=2)
    if dialog is None or not _is_file_dialog(dialog):
        raise ToolError("当前没有可安全取消的标准 PyroSim 文件对话框。")
    cancel_button = next((
        button for button in dialog.descendants(control_type="Button")
        if re.search(r"cancel|取消", button.window_text(), re.IGNORECASE)
    ), None)
    if cancel_button is None:
        raise ToolError("文件对话框未暴露明确的取消按钮；没有发送输入。")
    title = dialog.window_text()
    cancel_button.click_input()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        remaining = _visible_dialog(context, timeout=0.2)
        if remaining is None or remaining.handle != dialog.handle:
            break
    else:
        try:
            cancel_button.invoke()
        except Exception as exc:
            raise ToolError(f"已请求取消文件框但窗口仍存在，Invoke 也失败：{type(exc).__name__}: {exc!r}") from exc
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            remaining = _visible_dialog(context, timeout=0.2)
            if remaining is None or remaining.handle != dialog.handle:
                break
        else:
            raise ToolError("PyroSim 文件对话框仍然存在；取消没有得到窗口关闭确认。")
    _step(config, "cancel_file_dialog", "completed", f"已取消 PyroSim 文件对话框：{title}", verified=True)
    return {"status": "cancelled", "dialog_title": title, "no_file_selected": True}


def resolve_fds6_dialog(config: dict) -> dict:
    """Resolve the exact PyroSim FDS version prompt to FDS6, with readback."""
    context = _connect(config, launch=False)
    resolution = _native_resolve_fds6_dialog(context.pid)
    if not resolution:
        raise ToolError("当前没有可确认的 PyroSim FDS Version Selection Dialog。")
    verified = resolution == "win32_readback"
    detail = "已读取控件并确认 FDS6" if verified else "控件不可读；根据可见窗口中的 FDS6 默认值确认并关闭"
    _step(config, "import_fds", "fds6_selected", detail, verified=verified)
    return {"status": "confirmed", "fds_version": "FDS6", "dialog_closed": True,
            "selection_verification": resolution}


def dismiss_missing_import_file_error(config: dict, model_id: str) -> dict:
    """Dismiss only PyroSim's modal after a staged import file went missing."""
    bridge_root, working, models = _layout(config)
    _manifest_path(bridge_root, model_id)
    staged = working / f"{model_id}.fds"
    saved = models / f"{model_id}.psm"
    if staged.exists() or saved.exists():
        raise ToolError("隔离 FDS 或 PSM 文件仍存在；不符合已知的导入文件缺失恢复条件。")
    context = _connect(config, launch=False)
    try:
        if context.main.is_enabled():
            raise ToolError("PyroSim 主窗口没有被模态框禁用；没有发送 Enter。")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"无法确认 PyroSim 主窗口的模态状态：{type(exc).__name__}: {exc!r}") from exc

    user32 = ctypes.windll.user32
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    modal_handles = []

    def find_modal(hwnd, _):
        title = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(hwnd, title, len(title))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if title.value.strip().lower() == "pyrosim" and pid.value == context.pid:
            modal_handles.append(int(hwnd))
        return True

    user32.EnumWindows(enum_callback(find_modal), 0)
    if len(modal_handles) != 1:
        raise ToolError("没有找到唯一的 PyroSim 文件缺失提示框；没有发送输入。")
    hwnd = modal_handles[0]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    from pywinauto.keyboard import send_keys
    send_keys("{ENTER}")
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and user32.IsWindow(hwnd):
        time.sleep(0.1)
    if user32.IsWindow(hwnd):
        raise ToolError("已请求关闭 PyroSim 文件缺失提示，但窗口没有关闭确认。")
    _step(config, "import_fds", "recovered_missing_file", f"已关闭模型 {model_id} 的文件缺失提示；保留原算例", verified=True)
    return {"status": "dismissed", "model_id": model_id, "staged_file_missing": True,
            "source_case_unchanged": True}


def _wait_file_dialog_transition(context: UIContext, *, output_path: Path | None = None,
                                 title_path: Path | None = None, quiet_period: float = 0,
                                 timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    last_size = None
    stable_since = None
    quiet_since = None
    while time.monotonic() < deadline:
        if output_path is not None and output_path.is_file():
            size = output_path.stat().st_size
            if size > 0 and size == last_size:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 0.8:
                    return
            else:
                stable_since = None
                last_size = size
        if _resolve_fds_version_dialog(context):
            time.sleep(0.3)
            continue
        modal = _visible_dialog(context, 0.2)
        if modal is not None:
            quiet_since = None
            if _is_file_dialog(modal):
                time.sleep(0.2)
                continue
            _abort_unexpected_modal(modal)
        if modal is None:
            if title_path is None:
                if quiet_period <= 0:
                    return
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= quiet_period:
                    return
            if title_path is not None:
                expected_title = os.path.normcase(str(title_path.resolve()))
                if expected_title in os.path.normcase(_title(context)):
                    return
        time.sleep(0.2)
    raise ToolError(f"PyroSim 未在 {timeout:g} 秒内完成文件对话框操作。")


def _open_file(context: UIContext, path: Path) -> None:
    context.main.set_focus()
    context.main.type_keys("^o")
    dialog = _visible_dialog(context)
    if dialog is None:
        raise ToolError("PyroSim 未打开 Open 对话框。")
    edits = dialog.descendants(control_type="Edit")
    filename_edit = next((
        edit for edit in edits
        if getattr(edit.element_info, "control_id", None) in {1001, 1148}
    ), None)
    if filename_edit is not None:
        filename_edit.set_edit_text(str(path))
        filename_edit.type_keys("{ENTER}")
    else:
        dialog.set_focus()
        dialog.type_keys("%n")
        dialog.type_keys("^a")
        dialog.type_keys(str(path), with_spaces=True)
        dialog.type_keys("{ENTER}")
    deadline = time.monotonic() + 20
    resolved = os.path.normcase(str(path.resolve()))
    while time.monotonic() < deadline:
        if resolved in os.path.normcase(_title(context)):
            return
        time.sleep(0.25)
    raise ToolError(f"PyroSim 窗口标题未确认打开副本：{path.name}")


def _menu_action(context: UIContext, operation: str, *, allow_unsaved: bool = False) -> None:
    """Use UIA names when available, otherwise a profiled menu route with a dialog guard."""
    title = _title(context)
    if "*" in title and not allow_unsaved:
        raise ToolError("PyroSim 当前文档有未保存修改；桥接器不会替用户决定保存或丢弃。")
    patterns = {
        "file": re.compile(r"^(file|文件)$", re.IGNORECASE),
        "import": re.compile(r"import.*(fds|cad)|(fds|cad).*import|导入.*(fds|cad)|(fds|cad).*导入", re.IGNORECASE),
        "save_as": re.compile(r"save\s*as|另存为", re.IGNORECASE),
        "export": re.compile(r"^export|^导出", re.IGNORECASE),
        "export_fds": re.compile(r"fds\s*file|file\s*\(fds\)|fds文件|fds\s*文件", re.IGNORECASE),
    }

    def visible_menu_items():
        items = []
        for window in context.desktop.windows(process=context.pid, visible_only=True):
            try:
                items.extend(window.descendants(control_type="MenuItem"))
            except Exception:
                continue
        return items

    def click_named(key: str):
        for item in visible_menu_items():
            try:
                label = (item.window_text() or "").replace("&", "").strip()
            except Exception:
                continue
            if patterns[key].search(label):
                try:
                    item.click_input()
                except Exception as exc:
                    raise ToolError(f"PyroSim UIA menu click {label!r} failed: {type(exc).__name__}: {exc!r}") from exc
                time.sleep(0.2)
                return
        raise ToolError(f"PyroSim UIA 未暴露可确认的菜单项 {key!r}；未使用盲目键盘索引继续操作。")

    def send_main_keys(stage: str, keys: str):
        try:
            context.main.type_keys(keys)
        except Exception as exc:
            raise ToolError(f"PyroSim menu key stage {stage!r} failed: {type(exc).__name__}: {exc!r}") from exc

    if operation not in {"import", "save_as", "export"}:
        raise ToolError(f"未知 PyroSim UI 操作：{operation}")

    try:
        click_named("file")
        if operation == "import":
            click_named("import")
        elif operation == "save_as":
            click_named("save_as")
        else:
            click_named("export")
            click_named("export_fds")
        return
    except ToolError as exc:
        # PyroSim 2024.2's Java menu is visible but not exposed as UIA/Win32
        # menu items. The keyboard route is enabled only with a strict modal
        # title postcondition, before the caller enters any filename.
        if "未暴露可确认的菜单项" not in str(exc):
            raise
        try:
            context.main.type_keys("{ESC}")
        except Exception:
            pass

    # Normalize to a closed menu first. In this PyroSim profile, opening File
    # preselects New, so the observed menu offsets are relative to New (0):
    # Save As=3, Import FDS/CAD=6, Export=7.
    try:
        context.main.set_focus()
    except Exception as exc:
        raise ToolError(f"PyroSim main window could not receive focus: {type(exc).__name__}: {exc!r}") from exc
    send_main_keys("close_previous_menu", "{ESC}{ESC}")
    send_main_keys("open_file_menu", "%f")
    if operation == "import":
        send_main_keys("select_import", "{DOWN 6}{ENTER}")
        expected = re.compile(r"import|fds|open|导入|打开", re.IGNORECASE)
    elif operation == "save_as":
        send_main_keys("select_save_as", "{DOWN 3}{ENTER}")
        expected = re.compile(r"save|另存|保存", re.IGNORECASE)
    else:
        send_main_keys("select_export_fds", "{DOWN 7}{RIGHT}{ENTER}")
        expected = re.compile(r"export|fds|save|导出|保存", re.IGNORECASE)

    dialog = _visible_dialog(context, timeout=3)
    if dialog is None:
        native = _native_file_dialog(context.pid)
        if native is None:
            context.main.type_keys("{ESC}{ESC}")
            raise ToolError(f"PyroSim 2024.2 {operation} 菜单路径没有打开可识别的文件对话框；已取消。")
        dialog_title = native[1]
        if not expected.search(dialog_title):
            ctypes.windll.user32.SendMessageW(
                native[0], 0x0111, 2, None
            )  # WM_COMMAND / IDCANCEL
            raise ToolError(f"PyroSim {operation} 路径打开了非预期对话框 {dialog_title!r}；已取消。")
        return
    if not _is_file_dialog(dialog) or not expected.search(dialog.window_text()):
        try:
            dialog.type_keys("{ESC}{ESC}")
        except Exception:
            context.main.type_keys("{ESC}{ESC}")
        raise ToolError(
            f"PyroSim 2024.2 {operation} 路径打开了非预期对话框 {dialog.window_text()!r}；已取消，没有输入文件名。"
        )


def _is_file_dialog(dialog) -> bool:
    title = dialog.window_text()
    if re.search(r"open|save|import|export|打开|保存|导入|导出", title, re.IGNORECASE):
        return True
    try:
        class_name = dialog.element_info.class_name
        return class_name == "#32770" and bool(dialog.descendants(control_type="Edit"))
    except Exception:
        return False


def _wait_title(context: UIContext, path: Path, timeout: float = 25) -> str:
    expected = os.path.normcase(str(path.resolve()))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        title = _title(context)
        if expected in os.path.normcase(title):
            return title
        time.sleep(0.25)
    raise ToolError(f"未能从 PyroSim 窗口标题确认当前文档路径：{path.name}")


def inspect_ui(config: dict) -> dict:
    """Read the PyroSim window inventory and limited UIA control names."""
    _step(config, "inspect_ui", "connect", "读取 PyroSim 可见窗口和 UIA 控件")
    # Launching the configured application is safe here; this inspection does
    # not open, edit, or save a model.
    context = _connect(config, launch=True)
    title = _title(context)
    names = []
    try:
        for item in context.main.descendants():
            name = (item.window_text() or "").strip()
            if name:
                names.append({"type": item.element_info.control_type, "name": name[:160]})
            if len(names) >= 80:
                break
    except Exception:
        pass
    win32_controls = []
    win32_menu_items = []
    dialogs = []
    try:
        for owned in context.desktop.windows(process=context.pid, visible_only=True):
            if owned.handle == context.main.handle:
                continue
            record = {"title": owned.window_text(), "class_name": owned.element_info.class_name, "controls": []}
            try:
                for control in owned.descendants():
                    kind = str(control.element_info.control_type)
                    if kind not in {"Edit", "ComboBox", "Button"}:
                        continue
                    info = {"type": kind, "name": control.window_text(), "control_id": getattr(control.element_info, "control_id", None)}
                    try:
                        info["enabled"] = bool(control.is_enabled())
                    except Exception:
                        info["enabled"] = None
                    if kind == "Edit":
                        try:
                            info["value"] = control.get_value()
                        except Exception:
                            pass
                    record["controls"].append(info)
                    if len(record["controls"]) >= 40:
                        break
            except Exception:
                pass
            dialogs.append(record)
        _, Desktop = _load_pywinauto()
        legacy_desktop = Desktop(backend="win32")
        legacy_main = next((
            window for window in legacy_desktop.windows(process=context.pid, visible_only=True)
            if "pyrosim" in window.window_text().lower()
        ), None)
        if legacy_main is not None:
            for item in legacy_main.descendants():
                name = (item.window_text() or "").strip()
                if name:
                    win32_controls.append({"type": str(item.element_info.control_type), "name": name[:160]})
                if len(win32_controls) >= 80:
                    break
            try:
                win32_menu_items = [str(item)[:160] for item in legacy_main.menu_items()][:40]
            except Exception:
                win32_menu_items = []
    except Exception:
        pass
    result = {
        "application": "PyroSim",
        "version": context.version,
        "window_title": title,
        "uia_control_count_sampled": len(names),
        "uia_controls": names,
        "win32_control_count_sampled": len(win32_controls),
        "win32_controls": win32_controls,
        "win32_menu_items": win32_menu_items,
        "owned_dialogs": dialogs,
        "profile": context.profile,
        "capabilities": resolve_pyrosim_ui_profile(context.version)["capabilities"],
        "note": "控件树只能证明 UIA 暴露的元素；3D 模型树未暴露时，不把窗口标题当作模型内容验证。",
    }
    _step(config, "inspect_ui", "completed", f"PyroSim {context.version} UIA 检查完成", verified=True)
    return result


def snapshot_ui(config: dict) -> dict:
    """Return a fresh, read-only UIA snapshot for an observe-act-observe loop."""
    _step(config, "snapshot_ui", "reading", "正在读取 PyroSim 当前窗口和对话框状态")
    context = _connect(config, launch=False)
    title = _title(context)
    windows = []
    embedded_windows = []
    for window in context.desktop.windows(process=context.pid, visible_only=True):
        record = {
            "title": window.window_text()[:240],
            "class_name": getattr(window.element_info, "class_name", ""),
            "handle": int(window.handle),
            "is_main": window.handle == context.main.handle,
            "enabled": None,
            "controls": [],
        }
        try:
            record["enabled"] = bool(window.is_enabled())
        except Exception:
            pass
        try:
            for item in window.descendants():
                kind = str(item.element_info.control_type)
                name = (item.window_text() or "").strip()
                if not name and kind not in {"Window", "Pane", "Group"}:
                    continue
                info = {
                    "type": kind,
                    "name": name[:240],
                    "control_id": getattr(item.element_info, "control_id", None),
                    "automation_id": str(getattr(item.element_info, "automation_id", "") or "")[:120],
                    "enabled": None,
                }
                try:
                    info["enabled"] = bool(item.is_enabled())
                except Exception:
                    pass
                if kind == "Edit":
                    try:
                        info["value"] = str(item.get_value())[:500]
                    except Exception:
                        pass
                if kind == "Window":
                    try:
                        info["children"] = [
                            {"type": str(child.element_info.control_type),
                             "name": (child.window_text() or "").strip()[:240]}
                            for child in item.children()[:40]
                            if (child.window_text() or "").strip()
                        ]
                    except Exception:
                        info["children"] = []
                    embedded_windows.append(info)
                record["controls"].append(info)
                if len(record["controls"]) >= 100:
                    break
        except Exception as exc:
            record["controls_error"] = type(exc).__name__
        windows.append(record)
        if len(windows) >= 20:
            break
    modal_windows = [window for window in windows if not window["is_main"]]
    modal_windows.extend({
        "title": item["name"],
        "class_name": "UIA Window control",
        "source": "embedded_uia",
        "controls": item.get("children", []),
    } for item in embedded_windows if item.get("name"))
    main_record = next((window for window in windows if window["is_main"]), None)
    result = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "application": "PyroSim",
        "version": context.version,
        "window_title": title,
        "main_window_enabled": main_record["enabled"] if main_record else None,
        "visible_windows": windows,
        "modal_windows": modal_windows,
        "embedded_windows": embedded_windows,
        "note": "这是当前时刻的 UI Automation 状态快照。控件缺失不代表模型数据不存在；此快照不读取 3D 几何或完整模型树。",
    }
    _step(config, "snapshot_ui", "completed", f"已读取 PyroSim 实时 UI 状态（{len(windows)} 个可见窗口）",
          verified=True, window_title=title, modal_count=len(modal_windows))
    return result


def dismiss_update_dialog(config: dict) -> dict:
    """Close only the known PyroSim update-check prompt, never accept an update."""
    context = _connect(config, launch=False)
    controls = list(context.main.descendants())
    update_indexes = [
        index for index, item in enumerate(controls)
        if str(item.element_info.control_type) == "Window"
        and (item.window_text() or "").strip().casefold() == "check for updates"
    ]
    if len(update_indexes) != 1:
        raise ToolError("当前没有唯一的 ‘Check for Updates’ 提示窗口；未执行关闭。")
    update_index = update_indexes[0]
    close_buttons = []
    for item in controls[update_index + 1:]:
        kind = str(item.element_info.control_type)
        if kind == "MenuBar":
            break
        if kind == "Button" and (item.window_text() or "").strip().casefold() in {"close", "关闭"}:
            try:
                if item.is_enabled():
                    close_buttons.append(item)
            except Exception:
                continue
    if len(close_buttons) != 1:
        raise ToolError("更新提示未暴露唯一可用的关闭按钮；未确认更新，也未发送键盘输入。")
    _step(config, "dismiss_update_dialog", "closing", "关闭 PyroSim 更新提示（不安装更新）")
    close_buttons[0].click_input()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        remaining = [
            item for item in context.main.descendants()
            if str(item.element_info.control_type) == "Window"
            and (item.window_text() or "").strip().casefold() == "check for updates"
        ]
        try:
            enabled = bool(context.main.is_enabled())
        except Exception:
            enabled = False
        if not remaining and enabled:
            title = _title(context)
            _step(config, "dismiss_update_dialog", "completed", "更新提示已关闭，PyroSim 主窗口已恢复可用", verified=True)
            return {"status": "dismissed", "dialog_title": "Check for Updates", "main_window_enabled": True,
                    "window_title": title, "update_installed": False}
        time.sleep(0.1)
    raise ToolError("已请求关闭更新提示，但未能同时回读到提示关闭和主窗口启用。")


def import_fds(config: dict, case_id: str, new_model_id: str) -> dict:
    """Import a configured FDS case into PyroSim, saving only a new PSM copy."""
    if not isinstance(new_model_id, str) or not MODEL_ID_RE.fullmatch(new_model_id):
        raise ToolError("new_model_id 格式无效；只允许字母、数字、下划线和短横线。")
    source = _case_file(config, case_id)
    bridge_root, working, models = _layout(config)
    manifest_path = _manifest_path(bridge_root, new_model_id)
    staged_fds = (working / f"{new_model_id}.fds").resolve()
    saved_model = (models / f"{new_model_id}.psm").resolve()
    if manifest_path.exists() or staged_fds.exists() or saved_model.exists():
        raise ToolError(f"model_id 已存在：{new_model_id}；请使用新的 ID。")
    if not _inside(working.resolve(), staged_fds) or not _inside(models.resolve(), saved_model):
        raise ToolError("导入输出路径越界。")

    source_hash = _sha256(source)
    shutil.copy2(source, staged_fds)
    staged_hash = _sha256(staged_fds)
    if staged_hash != source_hash:
        staged_fds.unlink(missing_ok=True)
        raise ToolError("FDS 输入副本校验失败。")

    context = _connect(config)
    ui_started = False
    try:
        initial_title = _title(context)
        if "untitled" not in initial_title.lower() or "*" in initial_title:
            raise ToolError(f"PyroSim 当前文档不是干净的新建文档，拒绝导入：{initial_title!r}")
        _step(config, "import_fds", "select_import", f"PyroSim {context.version}：打开 FDS 导入菜单", case_id=case_id, model_id=new_model_id)
        ui_started = True
        try:
            _menu_action(context, "import")
        except Exception as exc:
            raise ToolError(f"PyroSim 导入菜单阶段失败：{type(exc).__name__}: {exc!r}\n{traceback.format_exc()}") from exc
        try:
            _enter_file_dialog_path(context, staged_fds)
        except Exception as exc:
            raise ToolError(f"PyroSim 导入文件框阶段失败：{type(exc).__name__}: {exc!r}") from exc
        try:
            _wait_file_dialog_transition(context, quiet_period=1.5)
        except Exception as exc:
            raise ToolError(f"PyroSim 导入载入阶段失败：{type(exc).__name__}: {exc!r}") from exc
        _step(config, "import_fds", "save_as_psm", "PyroSim 已载入隔离 FDS 文件，正在另存为新的 PSM 副本", case_id=case_id, model_id=new_model_id)
        _menu_action(context, "save_as", allow_unsaved=True)
        _enter_file_dialog_path(context, saved_model)
        _wait_file_dialog_transition(context, output_path=saved_model, title_path=saved_model)
        title = _wait_title(context, saved_model)
        if not saved_model.is_file() or saved_model.stat().st_size <= 0:
            raise ToolError("PyroSim 没有生成有效的 PSM 副本。")
        source_hash_after = _sha256(source)
        if source_hash_after != source_hash:
            raise ToolError("导入期间源 FDS 文件哈希改变；桥接器已停止。")
        record = {
            "model_id": new_model_id,
            "source_case_id": case_id,
            "source_fds": str(source),
            "source_sha256": source_hash,
            "staged_fds": str(staged_fds),
            "staged_fds_sha256": staged_hash,
            "model_path": str(saved_model),
            "model_sha256": _sha256(saved_model),
            "pyrosim_version": context.version,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "import_dialog_completed": True,
            "saved_title_verified": True,
        }
        manifest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        _step(config, "import_fds", "completed", f"PyroSim 隔离模型已导入并保存：{new_model_id}", case_id=case_id, model_id=new_model_id, verified=True)
        return {
            "status": "saved_copy",
            **record,
            "window_title": title,
            "source_unchanged": True,
            "verification": "FDS 版本选择框已关闭，新的 PSM 文件已写入且窗口标题指向该路径；完整模型树/几何未由 UIA 读取。",
        }
    except Exception as exc:
        if not ui_started:
            try:
                if staged_fds.is_file() and _sha256(staged_fds) == staged_hash:
                    staged_fds.unlink()
            except OSError:
                pass
        detail = str(exc) or f"{type(exc).__name__}: {exc!r}"
        _step(config, "import_fds", "failed", f"PyroSim 导入失败：{detail}", case_id=case_id, model_id=new_model_id, verified=False)
        raise


def save_imported_copy(config: dict, case_id: str, model_id: str) -> dict:
    """Resume only the pending Save As step for an already staged FDS import."""
    if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id):
        raise ToolError("model_id 格式无效。")
    source = _case_file(config, case_id)
    bridge_root, working, models = _layout(config)
    staged_fds = (working / f"{model_id}.fds").resolve()
    saved_model = (models / f"{model_id}.psm").resolve()
    manifest_path = _manifest_path(bridge_root, model_id)
    if not _inside(working.resolve(), staged_fds) or not _inside(models.resolve(), saved_model):
        raise ToolError("导入输出路径越界。")
    if manifest_path.exists() or saved_model.exists():
        raise ToolError("该 model_id 已登记或已有 PSM 文件；拒绝覆盖。")
    if not staged_fds.is_file():
        raise ToolError("隔离 FDS 不存在；拒绝继续保存。")
    source_hash_before = _sha256(source)
    staged_hash = _sha256(staged_fds)
    if staged_hash != source_hash_before:
        raise ToolError("隔离 FDS 不存在或与指定源算例不一致；拒绝继续保存。")

    context = _connect(config, launch=False)
    title_before = _title(context)
    if os.path.normcase(str(staged_fds)) not in os.path.normcase(title_before):
        raise ToolError(f"PyroSim 当前窗口没有显示预期的隔离 FDS：{title_before!r}")
    dialog = _native_file_dialog(context.pid)
    if dialog is None or "另存" not in dialog[1]:
        raise ToolError("当前没有唯一的 PyroSim ‘另存为’文件框；未提交操作。")

    _step(config, "import_fds", "resume_save_as", f"继续将隔离 FDS 保存为新的 PSM：{model_id}", case_id=case_id, model_id=model_id)
    _native_set_file_path(context.pid, saved_model)
    _wait_file_dialog_transition(context, output_path=saved_model, title_path=saved_model)
    title = _wait_title(context, saved_model)
    if not saved_model.is_file() or saved_model.stat().st_size <= 0:
        raise ToolError("PyroSim 没有生成有效的 PSM 副本。")
    source_hash = _sha256(source)
    if source_hash != source_hash_before:
        raise ToolError("保存期间源 FDS 哈希发生变化；停止并保留隔离产物。")
    record = {
        "model_id": model_id,
        "source_case_id": case_id,
        "source_fds": str(source),
        "source_sha256": source_hash,
        "staged_fds": str(staged_fds),
        "staged_fds_sha256": staged_hash,
        "model_path": str(saved_model),
        "model_sha256": _sha256(saved_model),
        "pyrosim_version": context.version,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "import_dialog_completed": True,
        "saved_title_verified": True,
    }
    manifest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _step(config, "import_fds", "completed", f"PyroSim 隔离模型已导入并保存：{model_id}", case_id=case_id, model_id=model_id, verified=True)
    return {
        "status": "saved_copy",
        **record,
        "window_title": title,
        "source_unchanged": source_hash == source_hash_before,
        "verification": "已回读隔离 FDS 哈希、确认另存为对话框和新 PSM 路径；标题及文件存在性通过。完整模型树/几何未由 UIA 读取。",
    }


def export_fds(config: dict, model_id: str, new_case_id: str) -> dict:
    """Export a bridge-managed PSM into a new case ID and compare FDS fields."""
    if not isinstance(new_case_id, str) or not CASE_ID_RE.fullmatch(new_case_id):
        raise ToolError("new_case_id 格式无效。")
    root = case_root(config).resolve()
    source_export = (root / f"{new_case_id}.fds").resolve()
    if source_export.parent != root:
        raise ToolError("导出目标路径越界。")
    if source_export.exists():
        raise ToolError(f"导出目标已存在，拒绝覆盖：{new_case_id}")
    bridge_root, _, _ = _layout(config)
    record = _load_manifest(bridge_root, model_id)
    model_path = Path(record["model_path"]).resolve()
    if _sha256(model_path) != record.get("model_sha256"):
        raise ToolError("PSM 副本自上次确认后发生变化；请重新导入或创建新的 model_id。")
    context = _connect(config)
    try:
        current_title = _title(context)
        if os.path.normcase(str(model_path)) not in os.path.normcase(current_title):
            _step(config, "export_fds", "open_model_copy", f"在 PyroSim 中打开隔离 PSM 副本：{model_id}", model_id=model_id)
            _open_file(context, model_path)
        _wait_title(context, model_path)
        _step(config, "export_fds", "select_export", f"PyroSim {context.version}：导出 FDS 到新算例 {new_case_id}", model_id=model_id, case_id=new_case_id)
        _menu_action(context, "export")
        _enter_file_dialog_path(context, source_export)
        _wait_file_dialog_transition(context, output_path=source_export)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (not source_export.is_file() or source_export.stat().st_size <= 0):
            # Detect and abort the known safety warning instead of accepting it.
            dialog = _visible_dialog(context, 0.2)
            if dialog is not None and re.search(r"warning|警告", dialog.window_text(), re.IGNORECASE):
                text = " ".join(item.window_text() for item in dialog.descendants())
                if "ambient pressure boundary" in text.lower() or "环境压力边界" in text:
                    for button in dialog.descendants(control_type="Button"):
                        if button.window_text().strip().lower() in {"ok", "确定"}:
                            button.click_input()
                            break
                    raise ToolError("PyroSim 显示求解前边界警告；已退出该操作，没有运行求解。")
            time.sleep(0.2)
        if not source_export.is_file() or source_export.stat().st_size <= 0:
            raise ToolError("PyroSim 未在限定时间内生成 FDS 导出文件。")
        readback = None
        source_case_id = record.get("source_case_id")
        if source_case_id:
            readback = compare_case_inputs(config, source_case_id, new_case_id)
        model_hash_after = _sha256(model_path)
        return {
            "status": "exported",
            "model_id": model_id,
            "model_path": str(model_path),
            "model_unchanged": model_hash_after == record.get("model_sha256"),
            "new_case_id": new_case_id,
            "exported_fds": str(source_export),
            "exported_bytes": source_export.stat().st_size,
            "source_case_id": source_case_id,
            "parameter_comparison": readback,
            "pyrosim_version": context.version,
            "note": "参数比对只覆盖 FDS 文件中的受支持字段，不证明完整几何、模型树或工程语义等价。",
        }
    except Exception as exc:
        # Preserve any partial output for inspection; never delete a path after
        # UI automation because another process may have created it meanwhile.
        _step(config, "export_fds", "failed", f"PyroSim FDS 导出失败：{exc}", model_id=model_id, case_id=new_case_id, verified=False)
        raise
