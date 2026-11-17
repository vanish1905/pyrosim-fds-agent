from __future__ import annotations

import json
import html
import os
import re
import subprocess
import sys
import ctypes
import time
from datetime import datetime
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import fds_tools
from . import path_discovery
from .config import APP_ROOT, load_config, save_config, mcp_server_command
from . import model_support
from .opencode_environment import configure_opencode_environment


STYLESHEET = """
QWidget { background: #0b1220; color: #e5edf7; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
QMainWindow { background: #080e19; }
QLabel { background: transparent; }
QFrame#card { background: #111c2c; border: 1px solid #24344a; border-radius: 14px; }
QFrame#card:hover { border-color: #304862; }
QLabel#muted { color: #91a3ba; }
QLabel#title { color: #f4f8ff; font-size: 22pt; font-weight: 700; }
QLabel#section { color: #f4f8ff; font-size: 12pt; font-weight: 650; }
QLabel#status { background: #152337; border: 1px solid #273a52; border-radius: 9px; padding: 10px 12px; color: #d7e2f0; }
QLineEdit, QComboBox { background: #0a1422; border: 1px solid #2b3d54; border-radius: 8px; padding: 9px 11px; selection-background-color: #2563eb; min-height: 20px; }
QLineEdit:hover, QComboBox:hover { border-color: #3b536f; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #38bdf8; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox QAbstractItemView { background: #111c2c; color: #e5edf7; border: 1px solid #30445f; selection-background-color: #1d4ed8; }
QPushButton { background: #1a2a3e; border: 1px solid #30445e; border-radius: 8px; padding: 9px 14px; min-height: 19px; font-weight: 600; }
QPushButton:hover { background: #233a54; border-color: #46627f; }
QPushButton:pressed { background: #142337; }
QPushButton:disabled { background: #111b29; color: #617188; border-color: #202c3b; }
QPushButton#primary { background: #0e7490; border-color: #1594b3; color: #f0fdff; }
QPushButton#primary:hover { background: #0f8aa8; border-color: #22b4d3; }
QTextEdit { background: #09121e; border: 1px solid #223249; border-radius: 10px; padding: 10px 12px; color: #d7e2ef; font-family: Consolas, 'Cascadia Code'; font-size: 9pt; }
QTextEdit:focus { border-color: #2d7391; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px 1px; }
QScrollBar::handle:vertical { background: #344861; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #48617f; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar { background: #0a1422; border: 1px solid #26384e; border-radius: 6px; color: #dce9f7; text-align: center; min-height: 14px; max-height: 14px; }
QProgressBar::chunk { background: #0e7490; border-radius: 5px; }
QToolTip { background: #152337; color: #f4f8ff; border: 1px solid #3b536f; padding: 5px 8px; }
QLabel#localBadge { background: #0d2834; border: 1px solid #15536a; border-radius: 9px; padding: 10px 12px; color: #8be5f6; font-weight: 700; }
QTextEdit#chat { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
QTextEdit#prompt { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
"""

LIGHT_STYLESHEET = """
QWidget { background: #f4f7fb; color: #172337; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
QMainWindow { background: #eaf0f7; }
QLabel { background: transparent; }
QFrame#card { background: #ffffff; border: 1px solid #d7e0eb; border-radius: 14px; }
QFrame#card:hover { border-color: #b9c9db; }
QLabel#muted { color: #62748a; }
QLabel#title { color: #122033; font-size: 22pt; font-weight: 700; }
QLabel#section { color: #1b2b40; font-size: 12pt; font-weight: 650; }
QLabel#status { background: #edf3f9; border: 1px solid #d3deea; border-radius: 9px; padding: 10px 12px; color: #26384e; }
QLineEdit, QComboBox { background: #ffffff; border: 1px solid #c8d4e2; border-radius: 8px; padding: 9px 11px; selection-background-color: #bfdbfe; min-height: 20px; }
QLineEdit:hover, QComboBox:hover { border-color: #8ba4c0; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #0284c7; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox QAbstractItemView { background: #ffffff; color: #172337; border: 1px solid #c8d4e2; selection-background-color: #dbeafe; }
QPushButton { background: #edf2f8; border: 1px solid #cbd7e5; border-radius: 8px; padding: 9px 14px; min-height: 19px; font-weight: 600; }
QPushButton:hover { background: #e2ebf5; border-color: #9eb2c9; }
QPushButton:pressed { background: #d7e3f0; }
QPushButton:disabled { background: #f1f3f6; color: #98a4b3; border-color: #e0e5eb; }
QPushButton#primary { background: #087ea4; border-color: #087ea4; color: #ffffff; }
QPushButton#primary:hover { background: #066b8d; border-color: #066b8d; }
QTextEdit { background: #ffffff; border: 1px solid #d0dbe7; border-radius: 10px; padding: 10px 12px; color: #26384e; font-family: Consolas, 'Cascadia Code'; font-size: 9pt; }
QTextEdit:focus { border-color: #65a8c8; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px 1px; }
QScrollBar::handle:vertical { background: #c5d1df; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #aabbd0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar { background: #edf2f8; border: 1px solid #d1dce8; border-radius: 6px; color: #26384e; text-align: center; min-height: 14px; max-height: 14px; }
QProgressBar::chunk { background: #0e8aa8; border-radius: 5px; }
QToolTip { background: #ffffff; color: #172337; border: 1px solid #bac9da; padding: 5px 8px; }
QLabel#localBadge { background: #e2f5fa; border: 1px solid #a9dce8; border-radius: 9px; padding: 10px 12px; color: #075d78; font-weight: 700; }
QTextEdit#chat { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
QTextEdit#prompt { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
"""

TEMPERATURE_EXAMPLE_PROMPT = """请通过已连接的 pyrosim_fds MCP 演示一个完整的 FDS 温度结果工作流：
1. 先调用 inspect_installation 和 inspect_adapter_capabilities，报告可执行文件存在性与精确版本验证证据；然后调用 stage_official_methane_flame_demo 获取固定校验的 FDS 官方 methane_flame_simple_2 示例。若示例已存在，工具会复用校验通过的文件；若下载或校验失败就停止，不要换 URL 或自行生成输入文件。
2. 对工具返回的 case_id 调用 inspect_case 和 preflight_case，确认输入包含 TEMPERATURE 与 HRRPUV 切片、显示实际 FDS 版本、T_END、网格和检查结论。不得修改该官方示例。
3. 调用 prepare_simulation。向我报告算例、求解器、输入快照哈希、预检警告和预期计算量，然后停下来等待我明确确认；不要把本条示例请求当成求解确认。
4. 我确认后，再调用 start_simulation approved=true，并用 get_simulation_status 跟踪到完成或失败。成功后调用 analyze_simulation_results 使用 theng 生成摘要/HRR 图，再调用 describe_result_presentation 汇总本次工作流的结果文件和能力状态。
5. 再用 open_simulation_visualization 请求打开本次 .smv，并调用 results_ui_inspect 只读回读目标窗口、结果树和时间控件。明确区分“结果文件已生成”和“Results 已加载/正在渲染”；没有回读证据时不要声称已显示。可以指出 TEMPERATURE/HRRPUV，但不要自动切换显示项。逐步展示 MCP 工具及真实回读。不要使用 shell、任意文件读写、其他算例或 GUI 自动化。"""


class OpenCodeWorker(QThread):
    answer = Signal(str)
    state = Signal(str)
    failed = Signal(str)
    finished_session = Signal(str)

    def __init__(self, executable: str, project_dir: Path, prompt: str, session_id: str = "",
                 model: str = "", runtime_config: Path | None = None):
        super().__init__()
        self.executable = executable
        self.project_dir = project_dir
        self.prompt = prompt
        self.session_id = session_id
        self.model = model
        self.runtime_config = runtime_config

    def run(self):
        command = [self.executable, "run", "--format", "json", "--dir", str(self.project_dir)]
        if self.model:
            command.extend(["--model", self.model])
        if self.session_id:
            command.extend(["--session", self.session_id])
        command.append(self.prompt)
        try:
            env = configure_opencode_environment(os.environ)
            env["PYTHONUTF8"] = "1"
            if self.runtime_config:
                env["OPENCODE_CONFIG"] = str(self.runtime_config)
                # Project opencode.json has higher precedence than OPENCODE_CONFIG
                # and may still point at a stale checkout. Inline config is applied
                # last by OpenCode, so bind MCP to the active checkout reliably.
                try:
                    env["OPENCODE_CONFIG_CONTENT"] = self.runtime_config.read_text(encoding="utf-8")
                except OSError:
                    pass
            process = subprocess.Popen(command, cwd=self.project_dir, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace",
                                       bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            response_parts = []
            raw_lines = []
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                raw_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self.state.emit(line[:300])
                    continue
                sid = event.get("sessionID") or event.get("sessionId")
                if sid:
                    self.finished_session.emit(str(sid))
                event_type = event.get("type", "")
                part = event.get("part") or {}
                if event_type in {"text", "text_delta"}:
                    piece = part.get("text") or event.get("text") or event.get("delta", "")
                    if piece:
                        response_parts.append(str(piece))
                elif event_type in {"tool_use", "tool", "tool_call"}:
                    name = part.get("name") or part.get("tool") or event.get("name") or "工具"
                    self.state.emit(f"正在调用工具：{name}")
                elif event_type in {"step_start", "step_finish"}:
                    self.state.emit("Agent 正在分析并调用本地工具…")
                elif event_type in {"error", "session.error"}:
                    self.state.emit(str(event.get("error") or event.get("message") or event)[:1000])
            code = process.wait()
            if response_parts:
                self.answer.emit("".join(response_parts).strip())
            elif code != 0:
                self.failed.emit("\n".join(raw_lines[-12:]) or f"OpenCode 退出码：{code}")
            else:
                self.failed.emit("OpenCode 未返回可显示的文本。请检查 OpenCode 模型和 MCP 配置。")
        except FileNotFoundError:
            self.failed.emit(f"找不到 OpenCode：{self.executable}")
        except Exception as exc:
            self.failed.emit(f"OpenCode 启动失败：{exc}")


class LocalTemperatureWorkflowWorker(QThread):
    """Application-owned TEMPERATURE phase graph with constrained local-model gates."""
    state = Signal(str)
    prepared = Signal(object)
    answer = Signal(str)
    failed = Signal(str)

    def __init__(self, endpoint: str, model_name: str, mode: str = "prepare",
                 workflow_id: str = ""):
        super().__init__()
        self.endpoint = endpoint
        self.model_name = model_name
        self.mode = mode
        self.workflow_id = workflow_id

    def _tool(self, name: str, arguments: dict | None = None) -> dict:
        from .mcp_server import call_tool
        self.state.emit(f"本地工作流调用：{name}")
        result = call_tool(name, arguments or {})
        if not isinstance(result, dict):
            raise RuntimeError(f"{name} 返回了无效结果。")
        return result

    def _advance(self, phase: str, observation: dict):
        decision = model_support.local_workflow_decision(
            self.endpoint, self.model_name, phase, observation)
        self.state.emit(f"本地模型阶段判断：{decision['decision']} · {phase}")
        return decision

    def run(self):
        try:
            if self.mode == "prepare":
                self.state.emit("本地模型正在判断 TEMPERATURE 示例准备阶段…")
                installation = self._tool("inspect_installation")
                profiles = self._tool("inspect_adapter_capabilities")
                if not installation.get("fds_present"):
                    raise RuntimeError(f"本机 FDS 不可用，尚未准备或启动求解：{installation.get('fds_exe')}")
                solver_profile = next((item for item in profiles.get("products", [])
                                       if item.get("id") == "fds-cli"), {})
                decision = self._advance("检查本机求解环境后", {
                    "pyrosim_present": installation.get("pyrosim_present"),
                    "fds_present": installation.get("fds_present"),
                    "smokeview_present": installation.get("smokeview_present"),
                    "compatibility": installation.get("compatibility"),
                    "solver_profile_status": solver_profile.get("status"),
                    "solver_revision": solver_profile.get("version"),
                    "verification_evidence": solver_profile.get("evidence"),
                })
                if decision["decision"] == "stop":
                    self.answer.emit(f"本地模型选择停止于环境检查阶段。{decision.get('reason', '')}")
                    return
                staged = self._tool("stage_official_methane_flame_demo")
                case_id = staged.get("case_id")
                if not case_id:
                    raise RuntimeError(f"官方示例准备失败：{staged}")
                inspected = self._tool("inspect_case", {"case_id": case_id})
                preflight = self._tool("preflight_case", {"case_id": case_id})
                if not preflight.get("ready"):
                    raise RuntimeError("TEMPERATURE 示例预检未通过，软件已停止在求解前：\n" +
                                       json.dumps(preflight, ensure_ascii=False, indent=2)[:2500])
                decision = self._advance("示例预检后", {
                    "case_id": case_id,
                    "preflight_ready": preflight.get("ready"),
                    "warnings": preflight.get("warnings", []),
                    "input_records": [item.get("name") for item in inspected.get("records", [])
                                      if isinstance(item, dict) and item.get("name")],
                    "expected_slices": (staged.get("demo") or {}).get("slices", []),
                    "solver": preflight.get("solver"),
                    "target_time_seconds": preflight.get("target_time_seconds"),
                })
                if decision["decision"] == "stop":
                    self.answer.emit(f"预检已通过，但本地模型建议暂停；尚未创建求解工作流。{decision.get('reason', '')}")
                    return
                result = self._tool("prepare_simulation", {"case_id": case_id})
                if result.get("status") != "awaiting_approval" or not result.get("approval_required"):
                    raise RuntimeError("工作流未进入安全的待确认状态：\n" +
                                       json.dumps(result, ensure_ascii=False, indent=2)[:2500])
                self.prepared.emit(result)
                return

            workflow_id = self.workflow_id
            if not workflow_id:
                raise RuntimeError("缺少待确认的 workflow_id。")
            self.state.emit(f"已获用户确认，启动工作流 {workflow_id}…")
            self._tool("start_simulation", {"workflow_id": workflow_id, "approved": True})
            last_status = None
            while True:
                status = self._tool("get_simulation_status", {"workflow_id": workflow_id})
                stage_status = status.get("status")
                if stage_status != last_status:
                    self.state.emit(f"FDS 工作流 {workflow_id}：{stage_status} · {status.get('stage', '')}")
                    last_status = stage_status
                if stage_status in {"completed", "failed", "cancelled", "timeout", "interrupted"}:
                    break
                time.sleep(1.5)
            if stage_status != "completed" or not (status.get("result") or {}).get("success"):
                raise RuntimeError("FDS 工作流未成功完成：\n" +
                                   json.dumps(status, ensure_ascii=False, indent=2)[:3000])

            decision = self._advance("求解完成后", {"workflow_id": workflow_id, "status": stage_status,
                                                   "case_id": status.get("case_id")})
            if decision["decision"] == "stop":
                self.answer.emit(f"FDS 已成功完成；本地模型建议暂停后处理。{decision.get('reason', '')}")
                return
            analysis = self._tool("analyze_simulation_results", {"workflow_id": workflow_id})
            presentation = self._tool("describe_result_presentation", {"workflow_id": workflow_id})
            decision = self._advance("theng 后处理完成后", {"workflow_id": workflow_id,
                                                            "analysis_status": analysis.get("status"),
                                                            "files": analysis.get("files"),
                                                            "presentation_status": presentation.get("status"),
                                                            "artifacts": presentation.get("artifacts"),
                                                            "analysis_artifacts": (presentation.get("analysis") or {}).get("artifacts")})
            if decision["decision"] == "stop":
                self.answer.emit("FDS 求解与 theng 后处理已完成；本地模型建议不再打开 Results。")
                return
            opened = self._tool("open_simulation_visualization", {"workflow_id": workflow_id})
            results = self._tool("results_ui_inspect", {"workflow_id": workflow_id})
            self.answer.emit(
                "本地模型引导的软件编排流程完成。\n"
                f"工作流：{workflow_id} · 算例：{status.get('case_id')} · 求解：{stage_status}\n"
                f"theng 后处理：{json.dumps(analysis, ensure_ascii=False)[:1600]}\n"
                f"展示清单：{presentation.get('status')}；结果文件：{len(presentation.get('artifacts', []))} 个；"
                f"分析文件：{len((presentation.get('analysis') or {}).get('artifacts', []))} 个。\n"
                f"Results 打开请求：{opened.get('status')}；窗口数：{results.get('window_count')}；"
                f"目标 .smv 标题匹配：{any(w.get('expected_smv_title_match') for w in results.get('windows', []))}；"
                f"展示清单渲染状态：{(presentation.get('native_viewer') or {}).get('render_status', 'unknown')}。"
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class Workbench(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyroSim / FDS Agent Workbench")
        icon_path = APP_ROOT / "assets" / "pyrosim_agent.ico"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1260, 900)
        self.setMinimumSize(1000, 720)
        self.config_data = load_config()
        self._compact_style_active = False
        self.theme = self.config_data.get("theme", "dark")
        if self.theme not in {"dark", "light"}:
            self.theme = "dark"
        self.session_id = ""
        self.worker = None
        self.local_temperature_pending_id = ""
        self.local_temperature_case_id = ""
        self.local_temperature_worker = None
        self.companion_hwnd = None
        self.companion_placement = None
        self.companion_saved_min_size = None
        self.companion_attaching = False
        self.companion_was_launched = False
        self.pyro_edit = QLineEdit(self.config_data["pyrosim_root"])
        self.case_edit = QLineEdit(self.config_data["case_root"])
        self.opencode_edit = QLineEdit(self.config_data["opencode_exe"])
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setMinimumWidth(220)
        saved_model = self.config_data.get("opencode_model", "")
        if saved_model:
            self.model_combo.addItem(saved_model)
            self.model_combo.setCurrentText(saved_model)
        self.local_endpoint_edit = QLineEdit(self.config_data.get("local_model_endpoint", ""))
        self.local_endpoint_edit.setPlaceholderText("http://127.0.0.1:8080/v1")
        self.local_model_edit = QLineEdit(self.config_data.get("local_model_name", ""))
        self.local_model_edit.setPlaceholderText("例如 Qwen3-0.6B-Q8_0")
        self.cc_switch_edit = QLineEdit(self.config_data.get("cc_switch_exe", ""))
        self.cc_switch_edit.setPlaceholderText("可选：CC Switch 程序路径")
        self.model_status = QLabel("模型列表尚未刷新")
        self.model_status.setObjectName("muted")
        self.pyro_status = self._status("PyroSim：未检测")
        self.fds_status = self._status("FDS：未检测")
        self.smv_status = self._status("Smokeview：未检测")
        self.activity = QTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumHeight(105)
        self.activity.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.activity.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.activity.setPlaceholderText("Agent、PyroSim 伴随状态和 FDS 求解进度会实时显示在这里。")
        self.event_file = None
        self.event_offset = 0
        self.chat_progress_bucket = -1
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.chat = QTextEdit()
        self.chat.setObjectName("chat")
        self.chat.setReadOnly(True)
        self.chat.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.chat.setPlaceholderText("描述你想完成的 PyroSim / FDS 工作，例如：列出算例，检查 smoke_agent_test 的设置。")
        self.prompt = QTextEdit()
        self.prompt.setObjectName("prompt")
        self.prompt.setPlaceholderText("输入自然语言任务…（Enter 发送，Shift+Enter 换行）")
        self.prompt.setFixedHeight(88)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primary")
        self.follow_timer = QTimer(self)
        self.follow_timer.setInterval(500)
        self.follow_timer.timeout.connect(self._follow_pyrosim)
        self.event_timer = QTimer(self)
        self.event_timer.setInterval(200)
        self.event_timer.timeout.connect(self._poll_backend_events)
        self._build()
        self.theme_combo.setCurrentIndex(0 if self.theme == "dark" else 1)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._apply_theme()
        self._set_event_source()
        self.event_timer.start()
        self.inspect()

    @staticmethod
    def _status(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("status")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _card() -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        return frame

    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        intro = QVBoxLayout()
        title = QLabel("PyroSim / FDS Agent Workbench")
        self.title_label = title
        title.setObjectName("title")
        intro.addWidget(title)
        subtitle = QLabel("本机环境、算例工作区、本地模型与 OpenCode 仿真 Agent")
        self.subtitle_label = subtitle
        subtitle.setObjectName("muted")
        intro.addWidget(subtitle)
        heading.addLayout(intro, 1)
        self.attach_button = QPushButton("附着到 PyroSim")
        self.attach_button.setToolTip("将 Agent 伴随面板停靠在 PyroSim 窗口旁")
        self.attach_button.clicked.connect(self.toggle_companion)
        heading.addWidget(self.attach_button, 0, Qt.AlignmentFlag.AlignTop)
        self.settings_button = QPushButton("连接设置")
        self.settings_button.setToolTip("展开环境路径、模型与 MCP 连接设置")
        self.settings_button.clicked.connect(self.toggle_settings)
        heading.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignTop)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色主题", "dark")
        self.theme_combo.addItem("浅色主题", "light")
        self.theme_combo.setToolTip("切换工作台深色或浅色配色")
        self.theme_combo.setFixedWidth(112)
        heading.addWidget(self.theme_combo, 0, Qt.AlignmentFlag.AlignTop)
        badge = QLabel("LOCAL  /  MCP")
        self.local_badge = badge
        badge.setObjectName("localBadge")
        heading.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        status_card = self._card()
        status_grid = QGridLayout(status_card)
        self.status_grid = status_grid
        self.status_labels = (self.pyro_status, self.fds_status, self.smv_status)
        status_grid.setContentsMargins(16, 14, 16, 14)
        status_grid.setHorizontalSpacing(10)
        status_grid.addWidget(self.pyro_status, 0, 0)
        status_grid.addWidget(self.fds_status, 0, 1)
        status_grid.addWidget(self.smv_status, 0, 2)
        status_grid.setColumnStretch(0, 1)
        status_grid.setColumnStretch(1, 1)
        status_grid.setColumnStretch(2, 1)
        layout.addWidget(status_card)

        self.config_panel = QWidget()
        self.config_panel.setObjectName("settingsPanel")
        config_row = QHBoxLayout(self.config_panel)
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.setSpacing(12)
        config_row.addWidget(self._settings_card(), 3)
        config_row.addWidget(self._opencode_card(), 2)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setMaximumHeight(370)
        self.settings_scroll.setMinimumHeight(220)
        self.settings_scroll.setWidget(self.config_panel)
        self.settings_scroll.hide()
        layout.addWidget(self.settings_scroll)

        chat_card = self._chat_card()
        layout.addWidget(chat_card, 1)

        log_card = self._card()
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(16, 13, 16, 16)
        log_header = QHBoxLayout()
        section = QLabel("检测结果与操作日志")
        section.setObjectName("section")
        log_header.addWidget(section)
        log_header.addStretch(1)
        clear = QPushButton("清空")
        clear.clicked.connect(self.log.clear)
        log_header.addWidget(clear)
        log_layout.addLayout(log_header)
        self.log.setMinimumHeight(100)
        self.log.setMaximumHeight(150)
        self.log.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        log_layout.addWidget(self.log)
        layout.addWidget(log_card)
        self.setCentralWidget(root)

    def toggle_settings(self):
        visible = not self.settings_scroll.isVisible()
        self.settings_scroll.setVisible(visible)
        self.settings_button.setText("收起设置" if visible else "连接设置")
        self.settings_button.setToolTip(
            "收起环境与模型设置，给对话和实时操作记录留出空间" if visible
            else "展开环境路径、模型与 MCP 连接设置"
        )

    def _load_temperature_example(self):
        current = self.prompt.toPlainText().strip()
        if current and QMessageBox.question(
            self, "替换当前输入", "输入框已有内容。是否用 TEMPERATURE 全流程示例替换？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.prompt.setPlainText(TEMPERATURE_EXAMPLE_PROMPT)
        self.prompt.setFocus()
        self.chat_state.setText("已载入 TEMPERATURE 全流程示例 · 发送后由 Agent 执行")

    def _run_local_temperature_workflow(self):
        if self.local_temperature_worker and self.local_temperature_worker.isRunning():
            return
        endpoint = self.local_endpoint_edit.text().strip()
        model_name = self.local_model_edit.text().strip()
        if not endpoint or not model_name or not self.config_data.get("local_model_tools_verified", False):
            QMessageBox.warning(
                self, "先检查本地模型",
                "请在连接设置中填写本地模型服务和模型名称，点击“检查连接与工具调用”，"
                "确认支持结构化工具调用后再启动本地编排。",
            )
            return
        self._save()
        if self.local_temperature_pending_id:
            workflow_id = self.local_temperature_pending_id
            answer = QMessageBox.question(
                self, "确认启动 FDS 求解",
                f"将启动已预检并冻结输入快照的 TEMPERATURE 示例。\n\n"
                f"工作流：{workflow_id}\n算例：{self.local_temperature_case_id}\n\n"
                "确认后会运行 FDS，成功后继续 theng 后处理并尝试打开 Results。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            mode = "run"
        else:
            workflow_id = ""
            mode = "prepare"
            self._append_chat("你", "运行 TEMPERATURE 固定示例（本地模型受限阶段编排）")
        self.local_temperature_button.setEnabled(False)
        self.chat_state.setText("本地模型工作流正在执行…")
        self.local_temperature_worker = LocalTemperatureWorkflowWorker(endpoint, model_name, mode, workflow_id)
        self.local_temperature_worker.state.connect(self._on_local_temperature_state)
        self.local_temperature_worker.prepared.connect(self._on_local_temperature_prepared)
        self.local_temperature_worker.answer.connect(self._on_local_temperature_answer)
        self.local_temperature_worker.failed.connect(self._on_local_temperature_failed)
        self.local_temperature_worker.finished.connect(lambda: self.local_temperature_button.setEnabled(True))
        self.local_temperature_worker.start()

    def _on_local_temperature_state(self, state: str):
        self.chat_state.setText(state)
        self._append_activity("本地编排", state)

    def _on_local_temperature_prepared(self, result: dict):
        self.local_temperature_pending_id = str(result.get("workflow_id", ""))
        self.local_temperature_case_id = str(result.get("case_id", ""))
        preflight = result.get("preflight") or {}
        message = (
            "本地模型和工作台已完成 TEMPERATURE 示例准备，求解尚未启动。\n"
            f"算例：{self.local_temperature_case_id}\n"
            f"求解器：{preflight.get('solver')} · 目标时长：{preflight.get('target_time_seconds')} s\n"
            f"快照 SHA-256：{result.get('snapshot_sha256')}\n"
            f"预检警告：{json.dumps(preflight.get('warnings', []), ensure_ascii=False)}\n\n"
            "检查信息后，再点击“本地模型编排”并在确认框中选择“是”启动计算。"
        )
        self.local_temperature_button.setText("确认并启动 TEMPERATURE")
        self._append_chat("Agent", message)
        self.chat_state.setText("等待你确认 FDS 求解")

    def _on_local_temperature_answer(self, answer: str):
        self.local_temperature_pending_id = ""
        self.local_temperature_case_id = ""
        self.local_temperature_button.setText("本地模型编排")
        self._append_chat("Agent", answer)
        self.chat_state.setText("本地 TEMPERATURE 工作流完成")

    def _on_local_temperature_failed(self, message: str):
        if self.local_temperature_pending_id:
            self.local_temperature_pending_id = ""
            self.local_temperature_case_id = ""
            self.local_temperature_button.setText("本地模型编排")
        self._append_chat("操作失败", message)
        self._append_activity("本地编排失败", message[:400])
        self.chat_state.setText("本地工作流停止；未越过当前安全阶段")

    def _chat_card(self) -> QFrame:
        card = self._card()
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Agent 对话")
        title.setObjectName("section")
        header.addWidget(title)
        header.addStretch(1)
        self.chat_state = QLabel("OpenCode MCP 对话，或本地模型受限阶段编排")
        self.chat_state.setObjectName("muted")
        self.chat_state.setWordWrap(True)
        header.addWidget(self.chat_state)
        new_chat = QPushButton("新对话")
        new_chat.clicked.connect(self.new_chat)
        header.addWidget(new_chat)
        box.addLayout(header)
        activity_title = QLabel("实时操作 · PyroSim 伴随 / FDS 求解 / Agent 工具")
        activity_title.setObjectName("muted")
        box.addWidget(activity_title)
        example_row = QHBoxLayout()
        self.temperature_example_button = QPushButton("载入 TEMPERATURE 全流程示例")
        self.temperature_example_button.setToolTip(
            "官方 methane_flame_simple_2 算例：检查、求解确认、FDS、theng 和 Results 回读"
        )
        self.temperature_example_button.clicked.connect(self._load_temperature_example)
        example_row.addWidget(self.temperature_example_button)
        self.local_temperature_button = QPushButton("本地模型编排")
        self.local_temperature_button.setToolTip(
            "本地模型只判断受限阶段是否继续；工作台固定调用 TEMPERATURE 示例工具。求解仍需你确认。"
        )
        self.local_temperature_button.clicked.connect(self._run_local_temperature_workflow)
        example_row.addWidget(self.local_temperature_button)
        example_note = QLabel("官方甲烷火焰 · TEMPERATURE / HRRPUV 切片")
        example_note.setObjectName("muted")
        example_row.addWidget(example_note, 1)
        box.addLayout(example_row)
        self.workflow_progress = QProgressBar()
        self.workflow_progress.setRange(0, 100)
        self.workflow_progress.setValue(0)
        self.workflow_progress.setFormat("等待仿真工作流")
        box.addWidget(self.workflow_progress)
        box.addWidget(self.activity)
        box.addWidget(self.chat, 1)
        compose = QHBoxLayout()
        compose.addWidget(self.prompt, 1)
        self.send_button.clicked.connect(self.send_message)
        self.send_button.setFixedWidth(100)
        compose.addWidget(self.send_button)
        box.addLayout(compose)
        hint = QLabel("自然语言任务可由 OpenCode 调用 MCP；本地模型可运行固定 TEMPERATURE 阶段流程，确认后启动求解。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.chat_hint = hint
        box.addWidget(hint)
        self.prompt.installEventFilter(self)
        return card

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if watched is self.prompt and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.send_message()
                return True
        return super().eventFilter(watched, event)

    def _chat_html(self, role: str, message: str) -> str:
        # Break long paths, hashes, and JSON tokens so the attached pane never
        # forces a horizontal overflow in QTextEdit.
        safe = html.escape(message).replace("\n", "<br>").replace("/", "/<wbr>").replace("\\", "\\<wbr>").replace("_", "_<wbr>")
        palette = ({"你": "#0369a1", "Agent": "#15803d", "错误": "#b42332", "操作失败": "#b42332"}
                   if self.theme == "light" else
                   {"你": "#7dd3fc", "Agent": "#a7f3d0", "错误": "#fda4af", "操作失败": "#fda4af"})
        color = palette.get(role, "#9a6700" if self.theme == "light" else "#fbbf24")
        return f'<p style="margin:8px 0 3px;color:{color};font-weight:700">{role}</p><p style="margin:0 0 12px">{safe}</p>'

    def _append_chat(self, role: str, message: str):
        self.chat.append(self._chat_html(role, message))

    def new_chat(self):
        if self.worker and self.worker.isRunning():
            return
        self.session_id = ""
        self.chat.clear()
        self.chat_state.setText("新对话 · OpenCode MCP")

    def send_message(self):
        message = self.prompt.toPlainText().strip()
        if not message or (self.worker and self.worker.isRunning()):
            return
        try:
            self._save()
            exe = self.config_data["opencode_exe"]
            if not Path(exe).is_file():
                raise FileNotFoundError(f"找不到 OpenCode 程序：{exe}")
            if not (APP_ROOT / "opencode.json").is_file():
                raise FileNotFoundError(f"项目中没有 opencode.json MCP 配置：{APP_ROOT}")
        except Exception as exc:
            QMessageBox.warning(self, "无法开始对话", str(exc))
            return
        self.prompt.clear()
        self._append_chat("你", message)
        self.chat_progress_bucket = -1
        guardrails = (
            "你是 PyroSim/FDS 本地操作 Agent。只通过 pyrosim_fds MCP 工具完成 PyroSim/FDS 操作；"
            "不要使用 shell、任意文件读写、GUI 自动化或修改 MCP 工具之外的文件。"
            "涉及修改时先保留源算例并创建新副本。若请求已能映射为明确参数和值，优先一次调用 create_case_with_updates，"
            "让工具创建副本、修改并回读核验；否则只用 create_case 后再用 update_case_parameters。"
            "当前只支持 TIME.T_END、TIME.DT、MESH.IJK，"
            "不支持的参数要明确说明。运行新仿真优先调用 prepare_simulation，解释预检查结果、solver、"
            "输入快照哈希和计算影响并等待用户确认；用户确认后调用 start_simulation approved=true，"
            "随后用 get_simulation_status 跟踪直到结束，需要停止时调用 cancel_simulation。不要对同一任务改用 run_fds 绕过快照确认。"
            "开始工作前调用 inspect_installation 与 inspect_adapter_capabilities；报告已检测的产品、profile 状态和证据，不能把程序存在当成版本兼容验证。"
            "工作流显示 completed 后，调用 analyze_simulation_results 生成 theng 结果摘要与 HRR 图，并调用 describe_result_presentation 获取结果文件清单；再调用 open_simulation_visualization 请求打开 .smv，并用 results_ui_inspect 回读窗口。必须区分文件生成、打开请求和已验证渲染；"
            "若样例没有 HRR/SLCF 等输出，只报告实际可用的结果，不要推断未输出的场数据。"
            "PyroSim GUI 操作通过专用 MCP UI 桥，不得自行使用 shell 或其他未授权 GUI 控制。"
            "用户明确要求在 PyroSim 导入 FDS 时，用 pyrosim_import_fds(case_id, new_model_id)；"
            "导出时只对桥登记过的副本调用 pyrosim_export_fds(model_id, new_case_id)，不得覆盖已有案例。"
            "需要先查看 PyroSim 主窗口时调用 pyrosim_ui_snapshot（只读，应用需已运行）；首次检查安装和控件时可调用 pyrosim_ui_inspect。"
            "需要只读检查 Results 窗口、显示项树、时间和控件时，调用 MCP 工具 pyrosim_fds_results_ui_inspect(workflow_id)。"
            "该工具只读取 UI Automation 暴露的状态，不代表某结果正在渲染。切换显示项仅在能唯一定位目标 Results 窗口、"
            "能通过操作前后的上下文菜单 Show/Hide 状态回读时，使用 pyrosim_fds_results_ui_set_display(workflow_id,item_path,action)；"
            "必须复制只读快照中的完整 item_path，并用 show/hide 明确目标。无法唯一匹配或无法回读时停止，不要双击猜状态。"
            "对每个受支持的 PyroSim 操作按‘快照→单次操作工具→快照’执行，并根据返回状态决定下一步；不要假定菜单或模型树已发生变化。"
            "UI 桥当前仅启用 PyroSim 2024.2，依赖未安装或版本不匹配时如实报告。"
            "导入/导出过程只能报告窗口标题、文件哈希和 FDS 关键字段回读；不能声称完整模型树或 CAD/BIM 几何已验证。"
            "如果用户在 PyroSim 中导入/导出 FDS 文件并放回案例目录，可用 compare_case_inputs 回读比对关键参数；"
            "这不等同于验证全部几何或模型树。"
            "模糊任务先检查环境/算例并追问缺失值，"
            "不要编造火源、材料、网格或边界条件。只报告工具真实返回的结果。\n\n用户请求："
        )
        self.send_button.setEnabled(False)
        self.chat_state.setText("正在连接 OpenCode…")
        selected_model = self.model_combo.currentText().strip()
        runtime_config = model_support.write_runtime_config(
            APP_ROOT, self.local_endpoint_edit.text(), self.local_model_edit.text(),
            bool(self.config_data.get("local_model_tools_verified", False)),
        )
        self.worker = OpenCodeWorker(exe, APP_ROOT, guardrails + message, self.session_id,
                                     selected_model, runtime_config)
        self.worker.answer.connect(self._chat_answer)
        self.worker.state.connect(self._on_opencode_state)
        self.worker.failed.connect(self._chat_failed)
        self.worker.finished_session.connect(self._set_session)
        self.worker.finished.connect(lambda: self.send_button.setEnabled(True))
        self.worker.start()

    def _set_session(self, session_id: str):
        if session_id:
            self.session_id = session_id

    def _chat_answer(self, answer: str):
        self._append_chat("Agent", answer)
        self.chat_state.setText("就绪 · OpenCode MCP")

    def _chat_failed(self, message: str):
        self._append_chat("错误", message)
        self.chat_state.setText("对话失败 · 检查 OpenCode / 模型 / MCP 配置")
        self._append_activity("Agent", "OpenCode 调用失败：" + message[:300])

    def _on_opencode_state(self, message: str):
        self.chat_state.setText(message)
        if message.startswith("正在调用工具："):
            self._append_activity("Agent", message)

    @staticmethod
    def _operation_summary(payload, limit: int = 700) -> str:
        if payload is None:
            return ""
        try:
            value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            value = str(payload)
        return value if len(value) <= limit else value[:limit] + "…"

    @staticmethod
    def _tool_result_summary(tool: str, result: dict) -> str:
        """Keep live tool readbacks legible in the narrow companion transcript."""
        if not isinstance(result, dict):
            return str(result)[:240]
        if tool == "results_ui_inspect":
            windows = result.get("windows") or []
            matching_windows = [window for window in windows if window.get("expected_smv_title_match")]
            matching = len(matching_windows)
            active = sum(1 for window in matching_windows if window.get("active"))
            controls = sum(int(window.get("controls_sampled") or 0) for window in windows)
            names = []
            for window in matching_windows:
                names.extend(item.get("name", "") for item in window.get("tree_items", []) if item.get("name"))
            names = list(dict.fromkeys(names))
            time_ranges = [control.get("range") for window in matching_windows
                           for control in window.get("time_controls", []) if control.get("range")]
            time_summary = ""
            if time_ranges:
                value = time_ranges[0]
                time_summary = (f"；时间 {value.get('current')} / "
                                f"{value.get('maximum')}" if value.get("maximum") is not None else "")
            tree_summary = "；树项 " + ", ".join(names[:6]) if names else ""
            return (f"Results 窗口 {len(windows)} 个；目标工作流匹配 {matching} 个；"
                    f"活动目标窗口 {active} 个；读取控件 {controls} 个{tree_summary}{time_summary}；"
                    f"只读={result.get('read_only', True)}；渲染状态未核验")
        if tool in {"get_simulation_status", "prepare_simulation", "start_simulation"}:
            fields = ("workflow_id", "case_id", "status", "stage", "progress", "simulation_time", "success")
            return " · ".join(f"{key}={result[key]}" for key in fields if result.get(key) is not None) or "已回读"
        if tool == "list_simulation_workflows":
            workflows = result.get("workflows") or []
            return "；".join(f"{item.get('case_id')} {item.get('status')}/{item.get('stage')}"
                            for item in workflows[:5]) or "没有工作流"
        if tool == "inspect_installation":
            fds = result.get("last_observed_fds_revision") or ("就绪" if result.get("fds_present") else "未找到")
            return (f"PyroSim={'就绪' if result.get('pyrosim_present') else '未找到'}；"
                    f"FDS {fds}；Smokeview={'就绪' if result.get('smokeview_present') else '未找到'}")
        fields = ("case_id", "workflow_id", "status", "stage", "success", "read_only", "window_count")
        summary = " · ".join(f"{key}={result[key]}" for key in fields if result.get(key) is not None)
        return summary or PyroAgentWindow._operation_summary(result, 280)

    def _append_live_tool_event(self, event_type: str, data: dict, message: str):
        tool = str(data.get("tool") or "MCP 工具")
        invocation = str(data.get("invocation_id") or "")
        if event_type == "tool_started":
            args = self._operation_summary(data.get("arguments") or {}, 420)
            detail = f"调用 {tool}" + (f" · 参数 {args}" if args and args != "{}" else "")
            self._append_chat("工具执行", detail)
        elif event_type == "tool_finished":
            result = data.get("result") or {}
            summary = self._tool_result_summary(tool, result)
            duration = data.get("duration_seconds")
            detail = f"{tool} 已完成" + (f" · {summary}" if summary else "")
            if duration is not None:
                detail += f" · {duration}s"
            self._append_chat("工具回读", detail)
        else:
            self._append_chat("操作失败", f"{tool} 失败 · {message}")
        if invocation:
            self._append_activity("调用编号", invocation)

    def _set_event_source(self):
        path = Path(self.config_data["case_root"]).expanduser() / ".pyro_agent_events.jsonl"
        path = path.resolve()
        if path == self.event_file:
            return
        self.event_file = path
        try:
            self.event_offset = path.stat().st_size
        except OSError:
            self.event_offset = 0

    def _poll_backend_events(self):
        path = self.event_file
        if path is None or not path.is_file():
            return
        try:
            size = path.stat().st_size
            if size < self.event_offset:
                self.event_offset = 0
            with path.open("rb") as stream:
                stream.seek(self.event_offset)
                pending = stream.read(256 * 1024)
            boundary = pending.rfind(b"\n") + 1
            if boundary == 0:
                return
            self.event_offset += boundary
            for raw in pending[:boundary].splitlines():
                if not raw:
                    continue
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._handle_backend_event(event)
        except OSError:
            return

    def _handle_backend_event(self, event: dict):
        event_type = event.get("type", "")
        message = str(event.get("message", ""))
        data = event.get("data") or {}
        case_id = data.get("case_id")
        tool = data.get("tool")
        if event_type == "pyrosim_ui_step":
            operation = data.get("operation", "PyroSim UI")
            stage = data.get("stage", "运行中")
            if stage == "completed":
                self.pyro_status.setText(f"PyroSim · UI 在线\n{message}")
            elif stage == "failed":
                self.pyro_status.setText(f"PyroSim · UI 操作失败\n{message}")
            else:
                self.pyro_status.setText(f"PyroSim · UI 操作中\n{message}")
            self.chat_state.setText(f"PyroSim UI · {stage}")
            self._append_activity("PyroSim UI 桥", f"{operation} / {stage} · {message}")
            if stage not in {"connect", "reading"}:
                self._append_chat("PyroSim 操作", f"{operation} · {stage} · {message}")
        elif event_type == "tool_started":
            if tool not in {"prepare_simulation", "start_simulation", "get_simulation_status",
                            "list_simulation_workflows", "cancel_simulation"}:
                self.chat_state.setText(f"Agent 正在调用 {tool or '本地工具'}…")
            self._append_activity("Agent / MCP", message)
            self._append_live_tool_event(event_type, data, message)
            if case_id:
                self.pyro_status.setText(f"PyroSim · 伴随面板在线\nAgent 当前算例：{case_id}")
                self._append_activity("PyroSim 伴随", f"Agent 正在对算例 {case_id} 执行 {tool or '检查'}")
        elif event_type == "tool_finished":
            if tool not in {"prepare_simulation", "start_simulation", "get_simulation_status",
                            "list_simulation_workflows", "cancel_simulation"}:
                self.chat_state.setText(f"Agent · {tool or '本地工具'}完成")
            self._append_activity("Agent / MCP", message)
            self._append_live_tool_event(event_type, data, message)
            result = data.get("result") or {}
            if case_id:
                self.pyro_status.setText(f"PyroSim · 伴随面板在线\nAgent 当前算例：{case_id}")
            if tool == "inspect_installation":
                self._apply_installation_status(result)
            elif tool == "results_ui_inspect":
                windows = result.get("windows") or []
                matches = [window for window in windows if window.get("expected_smv_title_match")]
                active_match = any(window.get("active") for window in matches)
                controls = sum(int(window.get("controls_sampled") or 0) for window in matches)
                tree_names = list(dict.fromkeys(
                    item.get("name", "") for window in matches
                    for item in window.get("tree_items", []) if item.get("name")
                ))
                if matches:
                    self.pyro_status.setText(
                        f"PyroSim Results · 已回读，目标窗口匹配 {len(matches)} 个\n"
                        f"活动目标窗口={'是' if active_match else '否'} · 控件 {controls} 个 · UIA只读"
                    )
                    self.chat_state.setText("Results 已回读 · 窗口匹配确认；渲染状态未核验")
                    if tree_names:
                        self._append_activity("Results UIA 树项", ", ".join(tree_names[:12]))
                else:
                    self.pyro_status.setText(
                        f"PyroSim Results · 未匹配目标工作流窗口\n"
                        f"已读取 {len(windows)} 个窗口；请检查当前 .smv"
                    )
                    self.chat_state.setText("Results 回读完成 · 未匹配目标窗口")
            if tool == "run_fds":
                self._apply_run_result(result)
        elif event_type == "tool_failed":
            self.chat_state.setText("Agent · 工具调用失败")
            self._append_activity("Agent / MCP 错误", message)
            self._append_live_tool_event(event_type, data, message)
        elif event_type == "solver_started":
            solver = data.get("solver_version") or data.get("solver") or "已选 FDS"
            self.fds_status.setText(f"FDS · 求解中\n案例 {case_id} · {solver}")
            self._set_status_tone(self.fds_status, "warning")
            self.pyro_status.setText(f"PyroSim · 伴随面板已连接\n当前 Agent 算例：{case_id}")
            self.chat_state.setText(f"FDS 正在计算 · {case_id}")
            self._append_activity("FDS", message)
            self._append_chat("FDS 求解", message)
        elif event_type == "solver_progress":
            progress = data.get("progress")
            time_label = message
            if progress is not None:
                time_label += f" · {progress:g}%"
            self.fds_status.setText(f"FDS · 求解中\n{case_id} · {time_label}")
            self.chat_state.setText(f"FDS 求解中 · {case_id} · {time_label}")
            if progress is not None:
                self.workflow_progress.setValue(max(0, min(100, round(progress))))
                self.workflow_progress.setFormat(f"{case_id} · {progress:g}% · {time_label}")
            self._append_activity("FDS 进度", time_label)
        elif event_type == "solver_output":
            if re.search(r"ERROR|WARNING", message, re.I):
                self._append_activity("FDS 日志", message)
        elif event_type == "solver_finished":
            success = bool(data.get("success"))
            cancelled = bool(data.get("cancelled"))
            duration = data.get("duration_seconds")
            status = "已完成" if success else ("已停止" if cancelled else "失败")
            details = f"案例 {case_id}" + (f" · {duration}s" if duration is not None else "")
            self.fds_status.setText(f"FDS · {status}\n{details}")
            self._set_status_tone(self.fds_status, "success" if success else ("warning" if cancelled else "error"))
            if success:
                self.workflow_progress.setValue(100)
                self.workflow_progress.setFormat(f"{case_id} · 求解完成")
                self.smv_status.setText(f"Smokeview · 结果已生成\n{data.get('run_dir', '')}")
                self._set_status_tone(self.smv_status, "success")
            self.chat_state.setText(f"FDS {status} · {case_id}")
            self._append_activity("FDS", message + (f" · {duration}s" if duration is not None else ""))
            self._append_chat("FDS 结果", message + (f" · {duration}s" if duration is not None else ""))
            for output in data.get("output_files", []):
                self._append_activity("结果文件", f"{output.get('name')} · {output.get('bytes')} B")
        elif event_type == "results_viewer_started":
            self.pyro_status.setText(f"PyroSim Results · 正在载入\n{case_id or ''}")
            self.chat_state.setText(f"正在打开结果可视化 · {case_id or ''}")
            self._append_activity("PyroSim Results", message)
            self._append_chat("Results", message)
        elif event_type in {"results_viewer_opened", "results_viewer_unverified", "results_viewer_failed"}:
            if event_type == "results_viewer_opened":
                self.pyro_status.setText(f"PyroSim Results · 已打开\n{case_id or ''}")
                self.chat_state.setText(f"结果已显示 · {case_id or ''}")
            elif event_type == "results_viewer_unverified":
                self.pyro_status.setText(f"PyroSim Results · 已启动，窗口未核验\n{case_id or ''}")
                self.chat_state.setText("Results 已启动 · 等待窗口回读")
            else:
                self.pyro_status.setText(f"PyroSim Results · 启动失败\n{case_id or ''}")
                self.chat_state.setText("PyroSim Results 启动失败")
            self._append_activity("PyroSim Results", message)
            self._append_chat("Results", message)
        elif event_type == "workflow_stage":
            workflow_id = data.get("workflow_id", "")
            status = data.get("status", "")
            stage = data.get("stage", "")
            labels = {"preflight": "求解前检查", "queued": "已排队", "solving": "正在求解",
                      "results": "整理结果", "stopping": "正在停止", "stopped": "已停止",
                      "error": "失败", "cancelled": "已取消", "recovery": "等待恢复"}
            label = labels.get(stage, stage or status)
            self.chat_state.setText(f"仿真工作流 {label} · {case_id or ''}")
            self.workflow_progress.setFormat(f"{case_id or ''} · {label} · {status} · {workflow_id}")
            if status in {"awaiting_approval", "preflight_failed"}:
                self.workflow_progress.setValue(0)
                self.fds_status.setText(f"FDS · {('待确认' if status == 'awaiting_approval' else '检查未通过')}\n案例 {case_id}")
            elif status in {"queued", "running", "cancel_requested"}:
                self.fds_status.setText(f"FDS · {('排队中' if status == 'queued' else '求解中')}\n案例 {case_id}")
            elif status in {"completed", "failed", "timeout", "cancelled", "interrupted"}:
                self.fds_status.setText(f"FDS · {status}\n案例 {case_id}")
            self._append_activity("仿真工作流", message)
            self._append_chat("工作流", message)
        elif event_type == "workflow_progress":
            progress = data.get("progress")
            sim_time = data.get("simulation_time")
            if progress is not None:
                progress_value = max(0, min(100, round(progress)))
                self.workflow_progress.setValue(progress_value)
                self.workflow_progress.setFormat(f"{case_id or ''} · {progress:g}% · t={sim_time or 0:g} s")
            self.chat_state.setText(f"FDS 求解中 · {case_id or ''} · t={sim_time or 0:g} s")
            bucket = int(progress // 10) if progress is not None else None
            if bucket is not None and bucket > self.chat_progress_bucket:
                self.chat_progress_bucket = bucket
                self._append_chat("FDS 进度", f"{progress:g}% · 模拟时间 {sim_time or 0:g} s")

    def _apply_installation_status(self, result: dict):
        self._set_engine_status(self.pyro_status, "PyroSim", result.get("pyrosim_present", False),
                                 result.get("pyrosim_root") or "")
        self._set_engine_status(self.fds_status, "FDS", result.get("fds_present", False),
                                 result.get("last_observed_fds_revision") or result.get("fds_exe") or "")
        self._set_engine_status(self.smv_status, "Smokeview", result.get("smokeview_present", False),
                                 result.get("smokeview_exe") or "")

    def _apply_run_result(self, result: dict):
        if not result:
            return
        self._append_activity("FDS 运行结果", json.dumps(result, ensure_ascii=False))

    def _append_activity(self, source: str, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_source = html.escape(source)
        safe_message = html.escape(str(message)).replace("\n", " ")
        stamp_color = "#087ea4" if self.theme == "light" else "#7dd3fc"
        self.activity.append(f'<span style="color:{stamp_color}">[{timestamp}] {safe_source}</span> {safe_message}')
        scrollbar = self.activity.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @staticmethod
    def _win_types():
        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT), ("ptMinPosition", POINT),
                        ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        return RECT, WINDOWPLACEMENT, MONITORINFO

    def _find_pyrosim_window(self):
        if sys.platform != "win32":
            return None
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                       wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        install_root = Path(self.pyro_edit.text().strip()).resolve()
        normalized_root = os.path.normcase(str(install_root))
        matches = []
        root_name = install_root.name.lower()
        version_tokens = [part for part in ("2024", "2025", "2026", "2027") if part in root_name]
        enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if "pyrosim" not in title.value.lower():
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(0x1000, False, pid.value)
            score = 1  # A visible PyroSim window is a safe fallback when process lookup is restricted.
            if not process:
                matches.append((score, hwnd, title.value))
                return True
            try:
                image = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(image))
                if kernel32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                    normalized_image = os.path.normcase(str(Path(image.value).resolve()))
                    try:
                        if os.path.commonpath([normalized_root, normalized_image]) == normalized_root:
                            score = 3
                        elif version_tokens and any(token in title.value.lower() for token in version_tokens):
                            score = 2
                        else:
                            return True
                    except ValueError:
                        return True
                elif version_tokens and any(token in title.value.lower() for token in version_tokens):
                    score = 2
            finally:
                kernel32.CloseHandle(process)
            matches.append((score, hwnd, title.value))
            return True

        user32.EnumWindows(enum_type(visit), 0)
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1] if matches else None

    def toggle_companion(self):
        if self.companion_attaching:
            self._detach_companion(restore=True)
            return
        if sys.platform != "win32":
            QMessageBox.warning(self, "不可用", "PyroSim 伴随面板目前只支持 Windows 桌面。")
            return
        try:
            self._save()
            hwnd = self._find_pyrosim_window()
            if hwnd is None:
                exe = Path(self.config_data["pyrosim_root"]) / "PyroSim.exe"
                if not exe.is_file():
                    raise FileNotFoundError(f"找不到 PyroSim：{exe}")
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
                self.companion_was_launched = True
            self.companion_attaching = True
            self.attach_button.setText("正在连接…")
            self.attach_button.setEnabled(False)
            self.chat_state.setText("正在查找 PyroSim 窗口…")
            self.follow_timer.start()
            QTimer.singleShot(1500, lambda: self.attach_button.setEnabled(True))
        except Exception as exc:
            self.companion_attaching = False
            self.attach_button.setEnabled(True)
            self.attach_button.setText("附着到 PyroSim")
            QMessageBox.critical(self, "连接失败", str(exc))

    def _follow_pyrosim(self):
        hwnd = self._find_pyrosim_window()
        if not hwnd:
            if self.companion_hwnd:
                self._detach_companion(restore=False)
                self.chat_state.setText("PyroSim 已关闭 · 工作台已恢复为独立窗口")
            elif self.companion_attaching:
                self.chat_state.setText("等待 PyroSim 启动…")
            return
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        if self.companion_hwnd != hwnd:
            self.companion_hwnd = hwnd
            self.companion_placement = self._capture_placement(hwnd)
            self._enter_companion_layout(hwnd)
            self.settings_scroll.hide()
            self.settings_button.setText("设置" if self.width() < 560 else "连接设置")
            self.attach_button.setText("脱离" if self.width() < 560 else "脱离 PyroSim")
            self.attach_button.setEnabled(True)
            self.chat_state.setText("已连接 PyroSim · 对话工具仍通过本地 MCP 工作")
        if user32.IsIconic(hwnd):
            self.hide()
            return
        if not self.isVisible():
            self.show()
        self._position_next_to(hwnd)
        foreground = user32.GetForegroundWindow()
        root_foreground = user32.GetAncestor(foreground, 2) if foreground else None
        panel = wintypes.HWND(int(self.winId()))
        z_order = -1 if root_foreground == hwnd or foreground == panel else -2
        user32.SetWindowPos(panel, wintypes.HWND(z_order),
                            0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def _capture_placement(self, hwnd):
        _, placement_type, _ = self._win_types()
        user32 = ctypes.windll.user32
        user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(placement_type)]
        user32.GetWindowPlacement.restype = wintypes.BOOL
        placement = placement_type()
        placement.length = ctypes.sizeof(placement)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return placement
        return None

    def _enter_companion_layout(self, hwnd):
        user32 = ctypes.windll.user32
        _, placement_type, monitor_type = self._win_types()
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(monitor_type)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        monitor = user32.MonitorFromWindow(hwnd, 2)
        info = monitor_type()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        work = info.rcWork
        screen_width = work.right - work.left
        screen_height = work.bottom - work.top
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        qt_work = screen.availableGeometry() if screen else None
        logical_screen_width = int(screen_width / dpr)
        panel_width = min(440, max(320, int(logical_screen_width * 0.34)))
        gap = 8
        panel_width_px = round(panel_width * dpr)
        gap_px = round(gap * dpr)
        app_width = max(700, screen_width - panel_width_px - gap_px)
        if app_width + panel_width_px + gap_px > screen_width:
            panel_width = max(280, int((screen_width - 700 - gap_px) / dpr))
            panel_width_px = round(panel_width * dpr)
            app_width = screen_width - panel_width_px - gap_px
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetWindowPos(hwnd, 0, work.left, work.top, app_width, screen_height,
                            0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE
        self.companion_saved_min_size = self.minimumSize()
        self.setMinimumSize(320, 560)
        panel_height = max(600, int(screen_height / dpr))
        self.resize(panel_width, panel_height)
        qt_left = qt_work.left() if qt_work else 0
        qt_top = qt_work.top() if qt_work else 0
        self.move(qt_left + round((app_width + gap_px) / dpr), qt_top)
        self.show()

    def _position_next_to(self, hwnd):
        user32 = ctypes.windll.user32
        rect_type, _, monitor_type = self._win_types()
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(monitor_type)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(rect_type)]
        user32.GetWindowRect.restype = wintypes.BOOL
        monitor = user32.MonitorFromWindow(hwnd, 2)
        info = monitor_type()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        work = info.rcWork
        rect = rect_type()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        qt_work = screen.availableGeometry() if screen else None
        panel_width_px = round(self.width() * dpr)
        panel_height_px = round(self.height() * dpr)
        target_x = min(max(rect.right + round(8 * dpr), work.left), work.right - panel_width_px)
        if target_x + panel_width_px > work.right:
            target_x = max(work.left, rect.left - panel_width_px - round(8 * dpr))
        panel_height_px = min(panel_height_px, work.bottom - work.top)
        target_y = min(max(rect.top, work.top), work.bottom - panel_height_px)
        qt_left = qt_work.left() if qt_work else 0
        qt_top = qt_work.top() if qt_work else 0
        target_qt_x = qt_left + round((target_x - work.left) / dpr)
        target_qt_y = qt_top + round((target_y - work.top) / dpr)
        panel_height = max(560, round(panel_height_px / dpr))
        if abs(self.x() - target_qt_x) > 3 or abs(self.y() - target_qt_y) > 3 or self.height() != panel_height:
            self.setGeometry(target_qt_x, target_qt_y, self.width(), panel_height)

    def _detach_companion(self, restore=True):
        self.follow_timer.stop()
        hwnd = self.companion_hwnd
        if hwnd and restore and self.companion_placement:
            user32 = ctypes.windll.user32
            _, placement_type, _ = self._win_types()
            user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(placement_type)]
            user32.SetWindowPlacement.restype = wintypes.BOOL
            placement = placement_type.from_buffer_copy(
                ctypes.string_at(ctypes.byref(self.companion_placement), ctypes.sizeof(self.companion_placement)))
            user32.SetWindowPlacement(hwnd, ctypes.byref(placement))
        self.companion_hwnd = None
        self.companion_attaching = False
        self.attach_button.setEnabled(True)
        self.attach_button.setText("附着到 PyroSim")
        if self.companion_saved_min_size:
            self.setMinimumSize(self.companion_saved_min_size)
        else:
            self.setMinimumSize(1000, 720)
        self.resize(1260, 900)
        self.move(100, 80)
        self.show()
        self.settings_scroll.hide()
        self.settings_button.setText("连接设置")
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos(wintypes.HWND(int(self.winId())), wintypes.HWND(-2),
                            0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        self.chat_state.setText("已脱离 · 独立工作台")

    def _settings_card(self) -> QFrame:
        card = self._card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 18)
        title = QLabel("本地计算环境")
        title.setObjectName("section")
        box.addWidget(title)
        description = QLabel("选择 PyroSim 安装目录和 Agent 可访问的算例工作区。")
        description.setObjectName("muted")
        description.setWordWrap(True)
        box.addWidget(description)
        box.addSpacing(5)
        self._field(box, "PyroSim 安装目录", self.pyro_edit, self._browse_pyro)
        self._field(box, "算例工作区", self.case_edit, self._browse_cases)
        discover_button = QPushButton("自动识别失效路径")
        discover_button.setToolTip("从当前设置、环境变量、Windows 注册表和常见安装目录查找；不会搜索整盘或自动覆盖有效路径")
        discover_button.clicked.connect(self.discover_local_paths)
        box.addWidget(discover_button)
        buttons = QHBoxLayout()
        inspect_button = QPushButton("保存并检测环境")
        inspect_button.setObjectName("primary")
        inspect_button.clicked.connect(self.inspect)
        open_button = QPushButton("打开算例目录")
        open_button.clicked.connect(self.open_cases)
        buttons.addWidget(inspect_button)
        buttons.addWidget(open_button)
        box.addSpacing(5)
        box.addLayout(buttons)
        return card

    def _opencode_card(self) -> QFrame:
        card = self._card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 18)
        title = QLabel("OpenCode 接入")
        title.setObjectName("section")
        box.addWidget(title)
        description = QLabel("写入项目级 MCP 配置，让 OpenCode Agent 使用本机 FDS 工具。")
        description.setObjectName("muted")
        description.setWordWrap(True)
        box.addWidget(description)
        self._field(box, "OpenCode 程序", self.opencode_edit, self._browse_opencode)
        box.addWidget(QLabel("OpenCode 对话模型"))
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, 1)
        refresh_models = QPushButton("刷新列表")
        refresh_models.clicked.connect(self.refresh_models)
        model_row.addWidget(refresh_models)
        box.addLayout(model_row)
        box.addWidget(self.model_status)
        box.addWidget(QLabel("本地模型服务（可选，OpenAI 兼容 API）"))
        box.addWidget(self.local_endpoint_edit)
        box.addWidget(self.local_model_edit)
        local_row = QHBoxLayout()
        check_local = QPushButton("检查连接与工具调用")
        check_local.clicked.connect(self.check_local_model)
        local_row.addWidget(check_local)
        save_model = QPushButton("保存模型配置")
        save_model.setObjectName("primary")
        save_model.clicked.connect(self.save_model_configuration)
        local_row.addWidget(save_model)
        box.addLayout(local_row)
        cc_row = QHBoxLayout()
        cc_row.addWidget(self.cc_switch_edit, 1)
        browse_cc = QPushButton("定位 CC Switch…")
        browse_cc.clicked.connect(self._browse_cc_switch)
        cc_row.addWidget(browse_cc)
        open_cc = QPushButton("打开 CC Switch")
        open_cc.clicked.connect(self.open_cc_switch)
        cc_row.addWidget(open_cc)
        box.addLayout(cc_row)
        cc_tip = QLabel("CC Switch 当前提供商：公开接口暂不可读取。请在 CC Switch 中切换后刷新列表；本处显示的是 Qt 对话所选 OpenCode 模型。工作台不会改写 CC Switch 配置或密钥。")
        cc_tip.setObjectName("muted")
        cc_tip.setWordWrap(True)
        box.addWidget(cc_tip)
        install = QPushButton("为项目安装 MCP 配置…")
        install.setObjectName("primary")
        install.clicked.connect(self.install_opencode_config)
        box.addSpacing(5)
        box.addWidget(install)
        tip = QLabel("配置完成后，在 OpenCode 中打开该项目并重启会话。工具不会获得任意 Shell 或文件访问权限。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        box.addWidget(tip)
        box.addStretch(1)
        return card

    def _field(self, parent, label, edit, browse):
        parent.addWidget(QLabel(label))
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("浏览…")
        button.clicked.connect(browse)
        row.addWidget(button)
        parent.addLayout(row)

    def _browse_pyro(self):
        path = QFileDialog.getExistingDirectory(self, "选择 PyroSim 安装目录", self.pyro_edit.text())
        if path:
            self.pyro_edit.setText(path)

    def _browse_cases(self):
        path = QFileDialog.getExistingDirectory(self, "选择算例工作区", self.case_edit.text())
        if path:
            self.case_edit.setText(path)

    def _browse_opencode(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 OpenCode", self.opencode_edit.text(), "Applications (*.exe)")
        if path:
            self.opencode_edit.setText(path)

    def _browse_cc_switch(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 CC Switch 程序", self.cc_switch_edit.text(), "Applications (*.exe)")
        if path:
            self.cc_switch_edit.setText(path)

    def open_cc_switch(self):
        self._save()
        path = self.cc_switch_edit.text().strip()
        if not path:
            QMessageBox.information(self, "CC Switch", "未配置 CC Switch 程序路径。请先定位其可执行文件；切换完成后刷新 OpenCode 模型列表。")
            return
        if not Path(path).is_file():
            QMessageBox.warning(self, "CC Switch", f"找不到程序：{path}")
            return
        try:
            subprocess.Popen([path], cwd=str(Path(path).parent),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.model_status.setText("已启动 CC Switch；切换提供商后刷新模型列表。")
        except Exception as exc:
            QMessageBox.warning(self, "CC Switch 启动失败", str(exc))

    def refresh_models(self):
        try:
            self._save()
            models = model_support.list_opencode_models(
                self.config_data["opencode_exe"], APP_ROOT,
                self.config_data.get("local_model_endpoint", ""),
                self.config_data.get("local_model_name", ""),
                bool(self.config_data.get("local_model_tools_verified", False)),
            )
            selected = self.config_data.get("opencode_model", "")
            self.model_combo.clear()
            self.model_combo.addItems(models)
            if selected:
                self.model_combo.setCurrentText(selected)
            self.model_status.setText(f"已读取 {len(models)} 个 OpenCode 模型；当前对话使用所选项。")
        except Exception as exc:
            self.model_status.setText("模型列表刷新失败；仍可手动输入 provider/model。")
            QMessageBox.warning(self, "刷新模型失败", str(exc))

    def save_model_configuration(self):
        try:
            endpoint = self.local_endpoint_edit.text().strip()
            model_name = self.local_model_edit.text().strip()
            if (endpoint, model_name) != (
                self.config_data.get("local_model_endpoint", ""),
                self.config_data.get("local_model_name", ""),
            ):
                self.config_data["local_model_tools_verified"] = False
            self.config_data["opencode_model"] = self.model_combo.currentText().strip()
            self.config_data["local_model_endpoint"] = endpoint
            self.config_data["local_model_name"] = model_name
            self.config_data["cc_switch_exe"] = self.cc_switch_edit.text().strip()
            save_config(self.config_data)
            model_support.write_runtime_config(
                APP_ROOT, self.config_data["local_model_endpoint"], self.config_data["local_model_name"],
                bool(self.config_data.get("local_model_tools_verified", False)),
            )
            self.model_status.setText("模型选择和本地服务配置已保存；下次对话会应用所选模型。")
        except Exception as exc:
            QMessageBox.warning(self, "保存模型配置失败", str(exc))

    def check_local_model(self):
        try:
            endpoint = self.local_endpoint_edit.text().strip()
            model_name = self.local_model_edit.text().strip()
            result = model_support.check_local_model(endpoint, model_name)
            self.config_data["local_model_endpoint"] = endpoint
            self.config_data["local_model_name"] = model_name
            self.config_data["local_model_tools_verified"] = result["tool_call_supported"]
            self.save_model_configuration()
            state = "支持 MCP 工具调用" if result["tool_call_supported"] else "暂未验证出工具调用能力"
            self.model_status.setText(f"连接正常 · 模型 {result['model']} · {state}\n{result['tool_call_detail']}")
            if not result["model_found"]:
                self.model_status.setText(self.model_status.text() + "\n注意：配置的模型名未出现在服务模型清单中。")
        except Exception as exc:
            self.config_data["local_model_tools_verified"] = False
            self.model_status.setText("本地模型检查失败。")
            QMessageBox.warning(self, "本地模型检查失败", str(exc))

    def _save(self):
        local_model_changed = (
            self.local_endpoint_edit.text().strip() != self.config_data.get("local_model_endpoint", "")
            or self.local_model_edit.text().strip() != self.config_data.get("local_model_name", "")
        )
        if local_model_changed:
            self.config_data["local_model_tools_verified"] = False
        self.config_data.update({
            "pyrosim_root": self.pyro_edit.text().strip(),
            "case_root": self.case_edit.text().strip(),
            "opencode_exe": self.opencode_edit.text().strip(),
            "opencode_model": self.model_combo.currentText().strip(),
            "local_model_endpoint": self.local_endpoint_edit.text().strip(),
            "local_model_name": self.local_model_edit.text().strip(),
            "cc_switch_exe": self.cc_switch_edit.text().strip(),
            "theme": self.theme,
        })
        save_config(self.config_data)
        self._set_event_source()

    def _append(self, title: str, payload):
        safe_title = html.escape(title)
        safe_payload = html.escape(json.dumps(payload, ensure_ascii=False, indent=2)).replace("\n", "<br>")
        safe_payload = safe_payload.replace("/", "/<wbr>").replace("\\", "\\<wbr>").replace("_", "_<wbr>")
        self.log.append(f'<p style="margin:5px 0 2px"><b>{safe_title}</b></p>')
        self.log.append(f'<p style="margin:0 0 8px;font-family:Consolas, \'Cascadia Code\';white-space:pre-wrap">{safe_payload}</p>')

    def inspect(self):
        try:
            self._save()
            result = fds_tools.inspect_installation(self.config_data)
            self._set_engine_status(self.pyro_status, "PyroSim", result["pyrosim_present"], result["pyrosim_root"])
            self._set_engine_status(self.fds_status, "FDS", result["fds_present"], result.get("last_observed_fds_revision") or result["fds_exe"])
            self._set_engine_status(self.smv_status, "Smokeview", result["smokeview_present"], result["smokeview_exe"])
            self._append("环境检测完成", result)
        except Exception as exc:
            QMessageBox.critical(self, "检测失败", str(exc))

    def discover_local_paths(self):
        """Populate only missing/invalid fields from bounded, validated candidates."""
        current = {
            "pyrosim_root": self.pyro_edit.text().strip(),
            "case_root": self.case_edit.text().strip(),
            "opencode_exe": self.opencode_edit.text().strip(),
        }
        result = path_discovery.discover_paths({**self.config_data, **current})
        edits = {"pyrosim_root": self.pyro_edit, "case_root": self.case_edit,
                 "opencode_exe": self.opencode_edit}
        valid = {
            "pyrosim_root": lambda value: (Path(value) / "PyroSim.exe").is_file() or (Path(value) / "pyrosim.exe").is_file(),
            "case_root": lambda value: Path(value).is_dir(),
            "opencode_exe": lambda value: Path(value).is_file(),
        }
        lines = []
        changed = []
        labels = {"pyrosim_root": "PyroSim", "case_root": "算例目录", "opencode_exe": "OpenCode"}
        for key, label in labels.items():
            if current[key] and valid[key](current[key]):
                lines.append(f"{label}：保留已验证路径 {current[key]}")
                continue
            candidates = result["candidates"][key]
            selected = result["selected"][key]
            if selected and not result["ambiguous"][key]:
                edits[key].setText(selected["path"])
                changed.append(label)
                lines.append(f"{label}：识别到 {selected['path']}（来源：{selected['source']}）")
            elif candidates:
                lines.append(f"{label}：有多个候选，未自动选择：" + "；".join(item["path"] for item in candidates[:4]))
            else:
                lines.append(f"{label}：没有找到有效候选")
        if changed:
            lines.append("识别结果仅填入设置栏，尚未保存；请检查后点击‘保存并检测环境’。")
        QMessageBox.information(self, "路径识别结果", "\n".join(lines))

    def _set_status_tone(self, label, tone: str | None = None):
        if tone is None:
            text = label.text()
            if "失败" in text or "错误" in text:
                tone = "error"
            elif "已停止" in text or "等待" in text or "未找到" in text:
                tone = "warning"
            elif "已完成" in text or "已就绪" in text or "已打开" in text or "结果已生成" in text:
                tone = "success"
            else:
                tone = "info"
        tones = {
            "success": (("#e1f4e9", "#17653a") if self.theme == "light" else ("#12372f", "#a7f3d0")),
            "warning": (("#fff3d6", "#875b00") if self.theme == "light" else ("#493516", "#fde68a")),
            "error": (("#fde8e8", "#a3323b") if self.theme == "light" else ("#43262d", "#fecaca")),
            "info": (("#e4f1fb", "#245b80") if self.theme == "light" else ("#152b40", "#c5e6ff")),
        }
        background, foreground = tones[tone]
        border = "#d5dfeb" if self.theme == "light" else "#273a52"
        label.setStyleSheet(f"background:{background};color:{foreground};border:1px solid {border};border-radius:8px;padding:8px;")

    def _refresh_status_tones(self):
        for label in getattr(self, "status_labels", ()):
            self._set_status_tone(label)

    def _apply_theme(self):
        app = QApplication.instance()
        if app is None:
            return
        compact = self.width() < 560
        stylesheet = LIGHT_STYLESHEET if self.theme == "light" else STYLESHEET
        if compact:
            stylesheet = (stylesheet
                          .replace("font-size: 10pt", "font-size: 9pt")
                          .replace("font-size: 22pt", "font-size: 13pt")
                          .replace("font-size: 12pt", "font-size: 10.5pt")
                          .replace("padding: 10px 12px", "padding: 6px 8px")
                          .replace("padding: 9px 14px", "padding: 6px 8px"))
        app.setStyleSheet(stylesheet)
        self._refresh_status_tones()

    def _on_theme_changed(self, index: int):
        if not hasattr(self, "theme_combo"):
            return
        self.theme = self.theme_combo.currentData() or "dark"
        self.config_data["theme"] = self.theme
        try:
            save_config(self.config_data)
        except OSError as exc:
            self._append_activity("主题设置", f"保存失败：{exc}")
        self._apply_theme()

    def _set_engine_status(self, label, name, found, detail):
        state = "已就绪" if found else "未找到"
        detail = str(detail or "")
        version_match = re.search(r"FDS[- ](\d+\.\d+(?:\.\d+)?)", detail, re.IGNORECASE)
        compact = label.window().width() < 560
        if compact:
            title = f"{name} · {state}"
            if name == "FDS" and version_match:
                title = f"FDS {version_match.group(1)} · {state}"
            label.setText(title)
            label.setToolTip(detail)
        else:
            label.setText(f"{name} · {state}\n{detail}")
            label.setToolTip(detail)
        self._set_status_tone(label, "success" if found else "error")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < 560
        # The attached panel is narrow by design. Stack the environment cards
        # and reduce layout gutters before Qt starts clipping their contents.
        if hasattr(self, "status_grid"):
            self.status_grid.setContentsMargins(8 if compact else 16, 8 if compact else 14,
                                                8 if compact else 16, 8 if compact else 14)
            self.status_grid.setHorizontalSpacing(6 if compact else 10)
            self.status_grid.setVerticalSpacing(5 if compact else 0)
            if getattr(self, "_compact_grid_active", None) != compact:
                for index, label in enumerate(self.status_labels):
                    self.status_grid.removeWidget(label)
                    self.status_grid.addWidget(label, index if compact else 0, 0 if compact else index)
                self._compact_grid_active = compact
            root_layout = self.centralWidget().layout() if self.centralWidget() else None
            if root_layout:
                root_layout.setContentsMargins(10 if compact else 24, 10 if compact else 18,
                                               10 if compact else 24, 10 if compact else 18)
                root_layout.setSpacing(7 if compact else 12)
        if compact != self._compact_style_active:
            self._compact_style_active = compact
            self._apply_theme()
            for name, label, detail in (
                ("PyroSim", self.pyro_status, self.config_data.get("pyrosim_root", "")),
                ("FDS", self.fds_status, self.config_data.get("last_observed_fds_revision", "")),
                ("Smokeview", self.smv_status, self.config_data.get("smokeview_exe", "")),
            ):
                if label.text().startswith(name) or (name == "FDS" and label.text().startswith("FDS ")):
                    found = "已就绪" in label.text() or (name == "FDS" and bool(self.config_data.get("last_observed_fds_revision")))
                    self._set_engine_status(label, name, found, detail)
            if compact and hasattr(self, "title_label"):
                self.title_label.setText("PyroSim Agent")
                self.subtitle_label.hide()
                self.local_badge.hide()
                self.attach_button.setText("脱离")
                self.settings_button.setText("设置")
            elif hasattr(self, "title_label"):
                self.title_label.setText("PyroSim / FDS Agent Workbench")
                self.subtitle_label.show()
                self.local_badge.show()
                self.attach_button.setText("脱离 PyroSim" if self.companion_attaching else "附着到 PyroSim")
                self.settings_button.setText("连接设置")

    def install_opencode_config(self):
        try:
            self._save()
            target = QFileDialog.getExistingDirectory(self, "选择要连接到 OpenCode 的项目目录")
            if not target:
                return
            config = {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"pyrosim_fds": {
                    "type": "local",
                    "command": mcp_server_command(APP_ROOT),
                    "cwd": str(APP_ROOT),
                    "enabled": True,
                    "timeout": 30000,
                }},
                "instructions": [str(APP_ROOT / "AGENTS.md")],
            }
            path = Path(target) / "opencode.json"
            if path.exists() and not QMessageBox.question(self, "确认覆盖", f"{path} 已存在。是否替换为本项目 MCP 配置？") == QMessageBox.StandardButton.Yes:
                return
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append("OpenCode 配置已写入", {"file": str(path), "server": "pyrosim_fds"})
        except Exception as exc:
            QMessageBox.critical(self, "配置失败", str(exc))

    def open_cases(self):
        try:
            self._save()
            path = Path(self.config_data["case_root"]).resolve()
            path.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer.exe", str(path)])
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))


def main():
    if "--mcp-server" in sys.argv[1:]:
        from .mcp_server import main as mcp_main
        mcp_main()
        return
    app = QApplication(sys.argv)
    app.setApplicationName("PyroSim / FDS Agent Workbench")
    app.setWindowIcon(QIcon(str(APP_ROOT / "assets" / "pyrosim_agent.ico")))
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.families()
    ui_font = "Microsoft YaHei UI" if "Microsoft YaHei UI" in families else "Segoe UI"
    app.setFont(QFont(ui_font, 10))
    app.setStyleSheet(LIGHT_STYLESHEET if load_config().get("theme") == "light" else STYLESHEET)
    window = Workbench()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


