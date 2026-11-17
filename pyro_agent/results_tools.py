"""Post-process completed FDS runs with Thunderhead's theng package."""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import case_root, runtime_paths
from .events import emit_event
from . import fds_tools


def _numbers(values: Any) -> list[float]:
    """Convert a one-dimensional result channel to finite Python floats."""
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError):
        return []
    return [value for value in result if math.isfinite(value)]


def analyze_simulation_results(config: dict, workflow_id: str) -> dict:
    """Analyze only a completed workflow and keep artifacts inside its run folder."""
    from . import workflows

    record = workflows.get_simulation_status(config, workflow_id)
    if record.get("status") != "completed":
        raise fds_tools.ToolError(
            f"只能分析已完成的工作流；当前状态为 {record.get('status')}。"
        )
    result = record.get("result") or {}
    raw_run_dir = result.get("run_dir") or record.get("run_dir")
    if not raw_run_dir:
        raise fds_tools.ToolError("工作流记录中没有结果目录。")

    runs_root = (case_root(config) / "runs").resolve()
    run_dir = Path(raw_run_dir).resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise fds_tools.ToolError("工作流结果目录不在配置的 runs 工作区内。") from exc
    if not run_dir.is_dir():
        raise fds_tools.ToolError("工作流结果目录已不存在。")
    smv_files = sorted(run_dir.glob("*.smv"))
    if len(smv_files) != 1:
        raise fds_tools.ToolError(f"预期结果目录中恰好有一个 .smv 文件，实际为 {len(smv_files)} 个。")

    try:
        from theng.models import FDSModel
    except ImportError as exc:
        raise fds_tools.ToolError(
            "缺少可选依赖 theng；请安装项目的 results extra / requirements-results.txt。"
        ) from exc

    try:
        model = FDSModel(str(smv_files[0]))
        hrr_data = getattr(model, "hrr", None) or {}
        steps_data = getattr(model, "steps", None) or {}
        hrr = _numbers(hrr_data.get("HRR", []))
        hrr_times = _numbers(hrr_data.get("Time", []))
        simulation_times = _numbers(getattr(model, "times", []))
        step_times = _numbers(steps_data.get("Simulation Time", []))
        step_numbers = _numbers(steps_data.get("Step", []))
        cpu_seconds = _numbers(steps_data.get("CPU Time", []))

        peak = None
        if hrr and hrr_times and len(hrr) == len(hrr_times):
            peak_index = max(range(len(hrr)), key=hrr.__getitem__)
            peak = {"value_kw": hrr[peak_index], "time_s": hrr_times[peak_index]}
        summary = {
            "workflow_id": workflow_id,
            "case_id": record.get("case_id"),
            "model_name": model.get_model_name(),
            "fds_version": getattr(model, "fds_version", None),
            "title": getattr(model, "title", None),
            "simulation_end_time_s": max(simulation_times or step_times) if (simulation_times or step_times) else None,
            "step_count": int(max(step_numbers)) if step_numbers else None,
            "cpu_time_s": cpu_seconds[-1] if cpu_seconds else None,
            "hrr_peak": peak,
            "hrr_series_points": min(len(hrr), len(hrr_times)),
            "slice_count": len(getattr(model, "slices", [])),
            "result_files": sorted(path.name for path in run_dir.iterdir() if path.is_file()),
        }

        analysis_dir = run_dir / "analysis"
        analysis_dir.mkdir(exist_ok=True)
        report_path = analysis_dir / "summary.json"
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        plot_path = None
        if hrr and hrr_times and len(hrr) == len(hrr_times):
            import plotly.graph_objects as go

            figure = go.Figure(data=[go.Scatter(x=hrr_times, y=hrr, mode="lines", name="HRR")])
            figure.update_layout(title="Heat Release Rate", xaxis_title="Time (s)", yaxis_title="HRR (kW)")
            plot = analysis_dir / "hrr.html"
            figure.write_html(str(plot), include_plotlyjs=True, full_html=True)
            plot_path = str(plot)
        return {
            "workflow_id": workflow_id,
            "status": "analyzed",
            "summary": summary,
            "artifacts": {"summary_json": str(report_path), "hrr_plot_html": plot_path},
            "processor": "theng.FDSModel",
        }
    except fds_tools.ToolError:
        raise
    except Exception as exc:
        raise fds_tools.ToolError(f"theng 读取或分析 FDS 结果失败：{exc}") from exc


def open_simulation_visualization(config: dict, workflow_id: str) -> dict:
    """Open a completed run's SMV file in the installed PyroSim Results viewer."""
    from . import workflows

    record = workflows.get_simulation_status(config, workflow_id)
    if record.get("status") != "completed" or not (record.get("result") or {}).get("success"):
        raise fds_tools.ToolError("只能显示求解成功工作流的结果；当前工作流未完成或求解失败。")
    result = record.get("result") or {}
    run_dir = Path(result.get("run_dir") or record.get("run_dir") or "").resolve()
    runs_root = (case_root(config) / "runs").resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise fds_tools.ToolError("工作流结果目录不在配置的 runs 工作区内。") from exc
    case_id = record.get("case_id")
    smv_path = (run_dir / f"{case_id}.smv").resolve()
    if not run_dir.is_dir() or not smv_path.is_file() or smv_path.stat().st_size <= 0:
        raise fds_tools.ToolError("工作流没有可打开的非空 .smv 结果文件。")
    install_root, _, _ = runtime_paths(config)
    viewer = install_root / "PyroSimResults.exe"
    if not viewer.is_file():
        raise fds_tools.ToolError(f"找不到 PyroSim Results 查看器：{viewer}")

    emit_event(config, "results_viewer_started", f"正在 PyroSim Results 中打开 {smv_path.name}",
               workflow_id=workflow_id, case_id=case_id, smv_file=str(smv_path), viewer=str(viewer))
    try:
        process = subprocess.Popen([str(viewer), str(smv_path)], cwd=str(run_dir), close_fds=True)
    except OSError as exc:
        emit_event(config, "results_viewer_failed", f"PyroSim Results 启动失败：{exc}",
                   workflow_id=workflow_id, case_id=case_id)
        raise fds_tools.ToolError(f"无法启动 PyroSim Results：{exc}") from exc

    # Opening the viewer and inspecting UIA are intentionally separate calls.
    # Results can take time to initialize, while UIA enumeration can hang on
    # this wx/Java build. Return as soon as the OS accepts the launch request;
    # results_ui_inspect performs the bounded, read-only verification next.
    verified = False
    status = "launch_requested"
    message = f"已启动 PyroSim Results 并传入 {smv_path.name}；请使用 results_ui_inspect 回读目标窗口"
    emit_event(config, "results_viewer_unverified", message,
               workflow_id=workflow_id, case_id=case_id, smv_file=str(smv_path),
               viewer=str(viewer), viewer_pid=process.pid,
               viewer_window_title=None, verified=verified)
    return {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "status": status,
        "smv_file": str(smv_path),
        "viewer_executable": str(viewer),
        "viewer_pid": process.pid,
        "viewer_process_image": None,
        "viewer_window_title": None,
        "verified": verified,
        "note": "PyroSim Results 是 PyroSim 套件中的独立 3D 结果窗口；此工具通过 .smv 加载计算结果，不会修改模型或重新求解。",
    }
