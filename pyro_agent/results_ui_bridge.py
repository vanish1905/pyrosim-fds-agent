"""Conservative Windows UI Automation bridge for PyroSim Results."""
from __future__ import annotations

import ctypes
import os
import multiprocessing
import re
import time
from pathlib import Path
from typing import Any

from .config import runtime_paths
from .fds_tools import ToolError
from .pyrosim_ui_bridge import _process_image

def _desktop():
    if os.name != "nt":
        raise ToolError("PyroSim Results UI 桥只支持 Windows。")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise ToolError("缺少 pywinauto；请安装 requirements-ui.txt。") from exc
    return Desktop(backend="uia")


def _viewer_exe(config: dict) -> Path:
    root, _, _ = runtime_paths(config)
    viewer = root / "PyroSimResults.exe"
    if not viewer.is_file():
        raise ToolError(f"找不到 PyroSim Results 查看器：{viewer}")
    return viewer.resolve()


def _inspect_results_ui_unbounded(config: dict, workflow_id: str | None = None) -> dict:
    """Read-only UIA inventory; called in a watchdog child process."""
    expected_smv = None
    case_id = None
    if workflow_id:
        from . import workflows
        record = workflows.get_simulation_status(config, workflow_id)
        result = record.get("result") or {}
        run_dir = Path(result.get("run_dir") or record.get("run_dir") or "").resolve()
        expected_smv = (run_dir / f"{record.get('case_id')}.smv").resolve()
        case_id = record.get("case_id")

    viewer = _viewer_exe(config)
    windows = []
    from .results_windows import visible_results_windows
    # Opening Results and creating its top-level window are asynchronous. A
    # single immediate snapshot frequently races startup and reports zero.
    deadline = time.monotonic() + (10.0 if workflow_id else 0.0)
    native_windows = visible_results_windows(viewer, expected_smv)
    while not native_windows and time.monotonic() < deadline:
        time.sleep(0.5)
        native_windows = visible_results_windows(viewer, expected_smv)
    for native_window in native_windows:
        try:
            pid = int(native_window["pid"])
            image = native_window["process_image"]
            title = str(native_window["title"] or "").strip()
            title_normalized = os.path.normcase(title.replace("/", "\\"))
            expected = os.path.normcase(str(expected_smv).replace("/", "\\")) if expected_smv else None
            title_match = bool(expected and expected in title_normalized)
            if expected and not title_match:
                # The Results title may abbreviate/append its visualization file;
                # case ID plus the exact run directory is a secondary match.
                title_match = os.path.normcase(str(expected_smv.parent).replace("/", "\\")) in title_normalized
            # Resolve only the already selected HWND instead of asking UIA to
            # enumerate every visible top-level window on the desktop.
            window = _desktop().window(handle=int(native_window["handle"]))
            controls = []
            for item in window.descendants():
                if len(controls) >= 180:
                    break
                try:
                    kind = str(item.element_info.control_type or "")
                    name = (item.window_text() or "").strip()
                    auto_id = str(getattr(item.element_info, "automation_id", "") or "")
                    if not (name or auto_id or kind in {"Tree", "TreeItem", "Slider", "Thumb", "Button", "MenuItem", "Pane"}):
                        continue
                    entry: dict[str, Any] = {
                        "type": kind, "name": name[:200], "automation_id": auto_id[:120],
                        "control_id": getattr(item.element_info, "control_id", None),
                    }
                    if kind == "TreeItem":
                        try:
                            entry["selected"] = bool(item.is_selected())
                        except Exception:
                            pass
                        item_path = [name] if name else []
                        current = item
                        for _ in range(8):
                            try:
                                current = current.parent()
                                parent_type = str(current.element_info.control_type or "")
                                if parent_type == "Tree":
                                    break
                                parent_name = (current.window_text() or "").strip()
                                if parent_type == "TreeItem" and parent_name:
                                    item_path.append(parent_name[:160])
                            except Exception:
                                break
                        entry["item_path"] = list(reversed(item_path))
                    if kind == "Slider":
                        try:
                            range_value = item.iface_range_value
                            entry["range"] = {"minimum": range_value.CurrentMinimum,
                                              "maximum": range_value.CurrentMaximum,
                                              "current": range_value.CurrentValue}
                        except Exception:
                            entry["range_error"] = str(exc)[:120]
                        try:
                            entry["range_value"] = item.get_value()
                        except Exception:
                            pass
                    controls.append(entry)
                except Exception:
                    continue
            windows.append({
                "title": title, "pid": pid, "process_image": image,
                "viewer_process_match": bool(native_window.get("viewer_process_match")),
                "enabled": bool(window.is_enabled()), "active": bool(window.is_active()),
                "handle": int(window.handle),
                "workflow_id": workflow_id, "case_id": case_id,
                "expected_smv": str(expected_smv) if expected_smv else None,
                "expected_smv_title_match": title_match if expected_smv else None,
                "controls_sampled": len(controls),
                "tree_items": [
                    {key: entry[key] for key in (
                        "type", "name", "item_path", "enabled", "selected", "font_weight", "legacy_state",
                        "supports_invoke", "supports_selection", "parent_chain") if key in entry}
                    for entry in controls if entry.get("type") == "TreeItem"
                ],
                "time_controls": [
                    {key: entry[key] for key in ("type", "name", "range", "range_value") if key in entry}
                    for entry in controls if entry.get("type") == "Slider"
                ],
                "status_texts": [
                    entry["name"] for entry in controls
                    if entry.get("type") == "Text" and entry.get("name")
                    and any(token in entry["name"].lower() for token in ("time:", "speed:", "playing", "paused", "stopped"))
                ],
                "display_commands": [
                    entry["name"] for entry in controls
                    if entry.get("type") in {"MenuItem", "Button"} and entry.get("name")
                    and any(token in entry["name"].lower() for token in ("show", "hide", "display", "显示", "隐藏"))
                ],
            })
        except Exception:
            continue
    return {
        "application": "PyroSim Results", "viewer_executable": str(viewer),
        "workflow_id": workflow_id, "case_id": case_id,
        "window_count": len(windows), "windows": windows,
        "read_only": True,
        "note": "此快照仅表示 UI Automation 暴露的窗口和控件；启动后会等待最多 10 秒；selected 只表示导航树选择状态，不证明该结果正在渲染；没有执行点击、播放或更改结果显示。",
    }


def _inspect_worker(send_pipe, config: dict, workflow_id: str | None) -> None:
    try:
        send_pipe.send({"ok": True, "result": _inspect_results_ui_unbounded(config, workflow_id)})
    except BaseException as exc:
        try:
            send_pipe.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        send_pipe.close()


def inspect_results_ui(config: dict, workflow_id: str | None = None) -> dict:
    """Read-only Results snapshot with a hard wall-clock limit around UIA."""
    if os.name != "nt":
        return _inspect_results_ui_unbounded(config, workflow_id)
    timeout_seconds = 20
    context = multiprocessing.get_context("spawn")
    receive_pipe, send_pipe = context.Pipe(duplex=False)
    worker = context.Process(target=_inspect_worker,
                             args=(send_pipe, config, workflow_id), daemon=True)
    try:
        worker.start()
        send_pipe.close()
        if not receive_pipe.poll(timeout_seconds):
            worker.terminate()
            worker.join(timeout=1)
            raise ToolError(
                f"Results UI Automation 读取超过 {timeout_seconds} 秒，已终止隔离检查进程；"
                "PyroSim Results 窗口仍保持不变。"
            )
        payload = receive_pipe.recv()
        worker.join(timeout=1)
        if not payload.get("ok"):
            raise ToolError(f"Results UI Automation 检查失败：{payload.get('error', '未知错误')}")
        return payload["result"]
    except ToolError:
        raise
    except Exception as exc:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=1)
        raise ToolError(f"Results UI Automation 隔离检查启动失败：{exc}") from exc
    finally:
        receive_pipe.close()
        try:
            send_pipe.close()
        except Exception:
            pass


def _results_window_for_workflow(config: dict, workflow_id: str):
    from . import workflows
    record = workflows.get_simulation_status(config, workflow_id)
    if record.get("status") != "completed" or not (record.get("result") or {}).get("success"):
        raise ToolError("只能控制成功完成工作流的 PyroSim Results 播放。")
    run_dir = Path((record.get("result") or {}).get("run_dir") or record.get("run_dir") or "").resolve()
    expected_smv = run_dir / f"{record.get('case_id')}.smv"
    viewer = _viewer_exe(config)
    desktop = _desktop()
    from .results_windows import visible_results_windows
    matches = []
    for native_window in visible_results_windows(viewer, expected_smv):
        try:
            title = str(native_window.get("title") or "").replace("/", "\\")
            if os.path.normcase(str(expected_smv).replace("/", "\\")) in os.path.normcase(title):
                window = desktop.window(handle=int(native_window["handle"]))
                matches.append((window, int(native_window["handle"])))
        except Exception:
            continue
    if not matches:
        raise ToolError("没有找到标题与目标工作流 .smv 匹配的 PyroSim Results 窗口。")
    if len(matches) == 1:
        return record, matches[0][0]
    foreground = int(ctypes.windll.user32.GetForegroundWindow())
    active = [entry for entry in matches if entry[1] == foreground]
    if len(active) == 1:
        return record, active[0][0]
    pids = sorted({int(window.element_info.process_id) for window, _ in matches})
    raise ToolError(
        "同一工作流有多个 Results 窗口，且无法唯一识别当前活动窗口；"
        f"匹配的进程 ID：{pids}。请在 PyroSim Results 中激活目标窗口后重试。"
    )


def _playback_readback(window) -> dict:
    texts = []
    for item in window.descendants(control_type="Text"):
        text = (item.window_text() or "").strip()
        if text:
            texts.append(text)
    state = next((text for text in texts if text in {"Playing", "Paused", "Stopped"}), None)
    time_text = next((text for text in texts if text.startswith("Time:")), None)
    speed_text = next((text for text in texts if text.startswith("Speed:")), None)
    current_s = None
    end_s = None
    if time_text:
        match = re.search(r"Time:\s*(\d+):(\d{2})\s*/\s*(\d+):(\d{2})", time_text)
        if match:
            current_s = int(match.group(1)) * 60 + int(match.group(2))
            end_s = int(match.group(3)) * 60 + int(match.group(4))
    return {"playback_status": state, "time_text": time_text, "speed_text": speed_text,
            "current_time_seconds": current_s, "displayed_end_time_seconds": end_s}


def _tree_path(item) -> list[str]:
    names = [(item.window_text() or "").strip()]
    current = item
    for _ in range(12):
        try:
            current = current.parent()
            kind = str(current.element_info.control_type or "")
            name = (current.window_text() or "").strip()
            if kind == "Tree":
                break
            if kind == "TreeItem" and name:
                names.append(name)
        except Exception:
            break
    return list(reversed([name for name in names if name]))


def _show_hide_command(window, item) -> tuple[str | None, dict, Any | None]:
    """Open the item's context menu and read its explicit Show/Hide command."""
    pid = int(window.element_info.process_id)
    item.right_click_input()
    time.sleep(0.15)
    desktop = _desktop()
    candidates = [window]
    for candidate in desktop.windows(visible_only=True):
        try:
            if int(candidate.element_info.process_id) == pid and candidate.handle != window.handle:
                candidates.append(candidate)
        except Exception:
            continue
    commands = []
    for candidate in candidates:
        try:
            for menu_item in candidate.descendants(control_type="MenuItem"):
                text = (menu_item.window_text() or "").strip()
                normalized = text.casefold().replace("&", "").replace("...", "").strip()
                if normalized in {"show", "hide", "显示", "隐藏"}:
                    commands.append((normalized, menu_item, candidate))
        except Exception:
            continue
    active_commands = []
    for normalized, menu_item, owner in commands:
        try:
            if menu_item.is_visible() and menu_item.is_enabled():
                active_commands.append((normalized, menu_item, owner))
        except Exception:
            continue
    state = None
    # Results exposes the inverse operation in its context menu: Hide means
    # the item is currently active; Show means it is inactive.
    if len(active_commands) == 1:
        label = active_commands[0][0]
        state = "active" if label in {"hide", "隐藏"} else "inactive"
    owner = active_commands[0][2] if len(active_commands) == 1 else None
    return state, {"commands": [entry[0] for entry in active_commands],
                   "menu_item_count": len(active_commands),
                   "menu_owner": (owner.window_text() or "") if owner else None}, owner


def _dismiss_context_menu(owner) -> None:
    try:
        owner.type_keys("{ESC}")
    except Exception:
        try:
            from pywinauto.keyboard import send_keys
            send_keys("{ESC}")
        except Exception:
            pass


def set_results_display(config: dict, workflow_id: str, item_path: list[str], action: str) -> dict:
    """Idempotently show/hide one Results tree item and verify via its menu."""
    if action not in {"show", "hide"}:
        raise ToolError("action 只能是 show 或 hide。")
    if not isinstance(item_path, list) or not item_path or len(item_path) > 12:
        raise ToolError("item_path 必须是 1–12 级的完整 Results 树项路径。")
    if any(not isinstance(name, str) or not name.strip() for name in item_path):
        raise ToolError("item_path 中的每一级都必须是非空名称。")
    requested = [name.strip().casefold() for name in item_path]
    record, window = _results_window_for_workflow(config, workflow_id)
    tree = [item for item in window.descendants(control_type="TreeItem")
            if [name.casefold() for name in _tree_path(item)] == requested]
    if len(tree) != 1:
        raise ToolError(f"完整树路径没有唯一匹配项：找到 {len(tree)} 个；未执行显示更改。")
    item = tree[0]
    if not item.is_enabled() or not item.is_visible():
        raise ToolError("目标 Results 树项不可见或不可用；未执行显示更改。")
    try:
        item.iface_scroll_item.ScrollIntoView()
        time.sleep(0.1)
    except Exception:
        # Some wx controls expose the item but not UIA ScrollItem. The later
        # right-click and command-state readback remain the required checks.
        pass
    desired = "active" if action == "show" else "inactive"
    owner = window
    before_state, before_menu, before_owner = _show_hide_command(window, item)
    if before_menu["menu_item_count"] != 1 or before_state is None:
        _dismiss_context_menu(before_owner or window)
        raise ToolError(f"无法从目标项的唯一 Show/Hide 菜单项确认当前状态：{before_menu}；未更改显示。")
    if before_state == desired:
        _dismiss_context_menu(before_owner or window)
        return {"workflow_id": workflow_id, "case_id": record.get("case_id"),
                "item_path": item_path, "action": action, "already_in_state": True,
                "before_state": before_state, "after_state": before_state,
                "before_menu": before_menu, "verified": True,
                "verification": "inverse_context_menu_command"}
    command_name = {"show": {"show", "显示"}, "hide": {"hide", "隐藏"}}[action]
    matches = []
    pid = int(window.element_info.process_id)
    candidates = [window]
    for candidate in _desktop().windows(visible_only=True):
        try:
            if int(candidate.element_info.process_id) == pid and candidate.handle != window.handle:
                candidates.append(candidate)
        except Exception:
            continue
    for candidate in candidates:
        try:
            for menu_item in candidate.descendants(control_type="MenuItem"):
                normalized = (menu_item.window_text() or "").casefold().replace("&", "").replace("...", "").strip()
                if normalized in command_name and menu_item.is_enabled() and menu_item.is_visible():
                    matches.append((menu_item, candidate))
        except Exception:
            continue
    if len(matches) != 1:
        _dismiss_context_menu(before_owner or window)
        raise ToolError(f"目标菜单命令 {action} 不唯一：找到 {len(matches)} 个；未执行显示更改。")
    command, owner = matches[0]
    try:
        command.invoke()
    except Exception:
        command.click_input()
    time.sleep(0.35)
    after_state, after_menu, after_owner = _show_hide_command(window, item)
    _dismiss_context_menu(after_owner or window)
    verified = after_state == desired and after_menu["menu_item_count"] == 1
    if not verified:
        raise ToolError(f"Results 显示状态回读不匹配：请求 {desired}，回读 {after_state}，菜单 {after_menu}。")
    return {"workflow_id": workflow_id, "case_id": record.get("case_id"),
            "item_path": item_path, "action": action,
            "before_state": before_state, "after_state": after_state,
            "before_menu": before_menu, "after_menu": after_menu,
            "verified": True, "verification": "inverse_context_menu_command"}


def set_results_time(config: dict, workflow_id: str, time_seconds: float) -> dict:
    """Seek the Results timeline and verify the visible status-bar time."""
    if isinstance(time_seconds, bool) or not isinstance(time_seconds, (int, float)):
        raise ToolError("time_seconds 必须是数值。")
    record, window = _results_window_for_workflow(config, workflow_id)
    end_time = float(record.get("target_time") or (record.get("preflight") or {}).get("target_time_seconds") or 0)
    if end_time <= 0 or time_seconds < 0 or time_seconds > end_time:
        raise ToolError(f"time_seconds 必须在 0–{end_time:g} 秒之间。")
    sliders = [control for control in window.descendants(control_type="Slider")
               if str(getattr(control.element_info, "automation_id", "")) == "6006"]
    if len(sliders) != 1 or not sliders[0].is_enabled():
        raise ToolError("没有找到唯一且启用的 Results 时间滑块 6006。")
    before = _playback_readback(window)
    percent = 100.0 * float(time_seconds) / end_time
    window.set_focus()
    sliders[0].set_value(percent)
    method = "range_value"
    time.sleep(0.3)
    after = _playback_readback(window)
    if after.get("current_time_seconds") is None or abs(after["current_time_seconds"] - float(time_seconds)) > 0.51:
        # This Results build exposes a RangeValue pattern but may not dispatch
        # its value-change event. Fall back to a slider-local UIA click.
        rect = sliders[0].rectangle()
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise ToolError("Results 时间滑块没有有效屏幕区域；未回读到目标时间。")
        thumbs = []
        for thumb in window.descendants(control_type="Thumb"):
            thumb_rect = thumb.rectangle()
            if thumb_rect.top >= rect.top - 4 and thumb_rect.bottom <= rect.bottom + 4 and thumb_rect.left >= rect.left - 4 and thumb_rect.right <= rect.right + 4:
                thumbs.append((thumb, thumb_rect))
        if len(thumbs) != 1:
            raise ToolError(f"时间滑块拇指控件不唯一或未暴露：{len(thumbs)} 个；停止定位。")
        thumb, thumb_rect = thumbs[0]
        thumb_width = thumb_rect.right - thumb_rect.left
        x = round(rect.left + width * percent / 100.0)
        x = max(rect.left + thumb_width // 2, min(rect.right - thumb_width // 2, x))
        y = (rect.top + rect.bottom) // 2
        sliders[0].drag_mouse_input(dst=(x, y), src=thumb, absolute=True)
        method = "uia_thumb_drag"
    deadline = time.monotonic() + 3.0
    after = _playback_readback(window)
    while time.monotonic() < deadline and after.get("current_time_seconds") is None:
        time.sleep(0.15)
        after = _playback_readback(window)
    if after.get("current_time_seconds") is None:
        raise ToolError("滑块已设置，但无法从 Results 状态栏回读当前时间。")
    verified = abs(after["current_time_seconds"] - float(time_seconds)) <= 0.51
    if not verified:
        raise ToolError(f"Results 时间回读为 {after['current_time_seconds']} 秒，与请求 {time_seconds:g} 秒不符。")
    return {"workflow_id": workflow_id, "case_id": record.get("case_id"),
            "requested_time_seconds": float(time_seconds), "slider_value_percent": percent,
            "method": method, "before": before, "after": after, "verified": True}


def set_results_playback(config: dict, workflow_id: str, action: str) -> dict:
    """Play or pause Results and verify the status bar transition."""
    if action not in {"play", "pause"}:
        raise ToolError("action 只能是 play 或 pause。")
    record, window = _results_window_for_workflow(config, workflow_id)
    before = _playback_readback(window)
    desired = "Playing" if action == "play" else "Paused"
    if before.get("playback_status") == desired:
        return {"workflow_id": workflow_id, "case_id": record.get("case_id"),
                "action": action, "already_in_state": True, "before": before,
                "after": before, "verified": True}
    # The installed toolbar exposes unlabeled wx controls. The Play button is
    # ID 5999 while stopped/paused; the same slot changes to Pause ID 6000.
    control_id = "5999" if action == "play" else "6000"
    buttons = [button for button in window.descendants(control_type="Button")
               if str(getattr(button.element_info, "automation_id", "")) == control_id]
    if len(buttons) != 1 or not buttons[0].is_enabled() or not buttons[0].is_visible():
        raise ToolError(f"找不到唯一可用的 Results {action} 控件（ID {control_id}）。")
    window.set_focus()
    try:
        buttons[0].invoke()
    except Exception:
        buttons[0].click_input()
    deadline = time.monotonic() + 2.5
    after = _playback_readback(window)
    while time.monotonic() < deadline and after.get("playback_status") != desired:
        time.sleep(0.15)
        after = _playback_readback(window)
    if after.get("playback_status") != desired:
        raise ToolError(f"已点击 Results {action} 控件，但状态栏未回读到 {desired}；当前为 {after.get('playback_status')}。")
    return {"workflow_id": workflow_id, "case_id": record.get("case_id"),
            "action": action, "control_id": control_id,
            "before": before, "after": after, "verified": True}


def adjust_results_speed(config: dict, workflow_id: str, direction: str) -> dict:
    """Change playback speed by one supported factor of two and read it back."""
    if direction not in {"slower", "faster"}:
        raise ToolError("direction 只能是 slower 或 faster。")
    record, window = _results_window_for_workflow(config, workflow_id)
    before = _playback_readback(window)
    control_id = "6003" if direction == "slower" else "6002"
    buttons = [button for button in window.descendants(control_type="Button")
               if str(getattr(button.element_info, "automation_id", "")) == control_id]
    if len(buttons) != 1 or not buttons[0].is_enabled() or not buttons[0].is_visible():
        raise ToolError(f"找不到唯一可用的 Results {direction} 控件（ID {control_id}）。")
    window.set_focus()
    buttons[0].invoke()
    deadline = time.monotonic() + 2.0
    after = _playback_readback(window)
    while time.monotonic() < deadline and after.get("speed_text") == before.get("speed_text"):
        time.sleep(0.15)
        after = _playback_readback(window)
    if not after.get("speed_text") or after.get("speed_text") == before.get("speed_text"):
        raise ToolError("已点击 Results 播放速度控件，但速度状态栏没有变化。")
    return {"workflow_id": workflow_id, "case_id": record.get("case_id"),
            "direction": direction, "control_id": control_id,
            "before": before, "after": after, "verified": True}
