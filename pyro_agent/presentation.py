"""Version-neutral result presentation manifest for completed FDS workflows."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import case_root, runtime_paths
from .fds_tools import ToolError


def _within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def describe_result_presentation(config: dict, workflow_id: str) -> dict[str, Any]:
    """Describe available result artifacts separately from GUI render state.

    This read-only contract can be consumed by Qt, OpenCode, Codex, or another
    viewer. It never opens Results and never claims that an item is rendered.
    """
    from . import workflows

    record = workflows.get_simulation_status(config, workflow_id)
    if record.get("status") != "completed" or not (record.get("result") or {}).get("success"):
        raise ToolError("结果展示清单只接受成功完成的 FDS 工作流。")

    result = record.get("result") or {}
    raw_run_dir = result.get("run_dir") or record.get("run_dir")
    if not raw_run_dir:
        raise ToolError("工作流记录中没有结果目录。")
    runs_root = (case_root(config) / "runs").resolve()
    run_dir = Path(raw_run_dir).resolve()
    if not _within(runs_root, run_dir) or not run_dir.is_dir():
        raise ToolError("工作流结果目录无效或不在配置的 runs 工作区内。")

    case_id = str(record.get("case_id") or "")
    smv = (run_dir / f"{case_id}.smv").resolve()
    if not _within(run_dir, smv) or not smv.is_file() or smv.stat().st_size <= 0:
        raise ToolError("成功工作流缺少有效的 .smv 结果描述文件。")

    analysis_dir = (run_dir / "analysis").resolve()
    summary_path = (analysis_dir / "summary.json").resolve()
    summary: dict[str, Any] | None = None
    if (_within(run_dir, analysis_dir) and _within(run_dir, summary_path)
            and summary_path.is_file()):
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("workflow_id") == workflow_id:
                summary = loaded
        except (OSError, json.JSONDecodeError):
            summary = None

    artifacts: list[dict[str, Any]] = [{
        "id": "fds-smv",
        "kind": "native_simulation_descriptor",
        "format": ".smv",
        "path": str(smv),
        "size_bytes": smv.stat().st_size,
        "status": "ready",
        "viewer": "PyroSim Results / Smokeview",
    }]
    for path in sorted(run_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        resolved_path = path.resolve()
        if not _within(run_dir, resolved_path):
            continue
        suffix = path.suffix.casefold()
        if suffix in {".sf", ".s3d"}:
            artifacts.append({
                "id": path.name,
                "kind": "fds_field_output",
                "format": suffix,
                "path": str(resolved_path),
                "size_bytes": resolved_path.stat().st_size,
                "status": "ready",
                "viewer": "PyroSim Results / Smokeview",
            })
        elif suffix == ".csv":
            artifacts.append({
                "id": path.name,
                "kind": "tabular_output",
                "format": suffix,
                "path": str(resolved_path),
                "size_bytes": resolved_path.stat().st_size,
                "status": "ready",
                "viewer": "CSV-compatible viewer",
            })

    analysis_artifacts = []
    if summary is not None:
        analysis_artifacts.append({
            "id": "analysis-summary",
            "kind": "machine_readable_summary",
            "format": ".json",
            "path": str(summary_path),
            "status": "ready",
        })
    hrr_plot = (analysis_dir / "hrr.html").resolve()
    if _within(run_dir, hrr_plot) and hrr_plot.is_file():
        analysis_artifacts.append({
            "id": "hrr-plot",
            "kind": "interactive_plot",
            "format": ".html",
            "path": str(hrr_plot),
            "size_bytes": hrr_plot.stat().st_size,
            "status": "ready",
        })

    install_root, _, _ = runtime_paths(config)
    results_exe = install_root / "PyroSimResults.exe"
    return {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "status": "data_ready",
        "run_dir": str(run_dir),
        "simulation": {
            "solver": record.get("solver"),
            "solver_revision": record.get("solver_revision"),
            "end_time_seconds": record.get("target_time"),
            "exit_code": result.get("exit_code"),
        },
        "analysis": {
            "status": "ready" if summary is not None else "not_generated",
            "summary": summary,
            "artifacts": analysis_artifacts,
        },
        "native_viewer": {
            "id": "pyrosim-results",
            "executable": str(results_exe) if results_exe.is_file() else None,
            "status": "available_unverified" if results_exe.is_file() else "unavailable",
            "open_action": "open_simulation_visualization" if results_exe.is_file() else None,
            "render_status": "not_verified",
            "display_item_control": "experimental",
            "time_control": "experimental",
        },
        "artifacts": artifacts,
        "notes": [
            "Artifact readiness means the file exists and belongs to this completed workflow.",
            "It does not prove that PyroSim Results loaded or rendered the data.",
            "Use the viewer adapter's separate readback before reporting a displayed result.",
        ],
    }
