"""Persistent, isolated FDS workflow preparation and background run control."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .config import case_root
from .events import emit_event
from . import fds_tools

WORKFLOW_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_LOCK = threading.RLock()
_RUNTIMES: dict[str, dict] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _workflow_root(config: dict) -> Path:
    return case_root(config) / ".pyro_agent_workflows"


def _workflow_file(config: dict, workflow_id: str) -> Path:
    if not isinstance(workflow_id, str) or not WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise fds_tools.ToolError("workflow_id 格式无效。")
    return _workflow_root(config) / workflow_id / "workflow.json"


def _read(config: dict, workflow_id: str) -> dict:
    with _LOCK:
        path = _workflow_file(config, workflow_id)
        if not path.is_file():
            raise fds_tools.ToolError(f"找不到工作流：{workflow_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise fds_tools.ToolError(f"工作流状态文件不可读：{exc}") from exc


def _write(config: dict, record: dict) -> None:
    with _LOCK:
        path = _workflow_file(config, record["workflow_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        record["updated_at"] = _now()
        temp = path.with_name(f"workflow.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _stage(config: dict, record: dict, stage: str, status: str, message: str, **extra) -> None:
    with _LOCK:
        record.update({"stage": stage, "status": status, **extra})
        _write(config, record)
    emit_event(config, "workflow_stage", message, workflow_id=record["workflow_id"],
               case_id=record["case_id"], stage=stage, status=status,
               progress=record.get("progress"), simulation_time=record.get("simulation_time"),
               run_dir=record.get("run_dir"))


def prepare_simulation(config: dict, case_id: str) -> dict:
    """Preflight a case and freeze the exact input that awaits user approval."""
    preflight = fds_tools.preflight_case(config, case_id)
    workflow_id = uuid.uuid4().hex[:12]
    workflow_dir = _workflow_root(config) / workflow_id
    snapshot_dir = workflow_dir / "input"
    record = {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "status": "preflight_failed" if not preflight["ready"] else "awaiting_approval",
        "stage": "preflight",
        "created_at": _now(),
        "updated_at": _now(),
        "owner_pid": os.getpid(),
        "source_file": preflight["file"],
        "source_sha256": preflight["sha256"],
        "preflight": preflight,
        "snapshot_file": None,
        "solver": preflight["solver"],
        "solver_revision": preflight["solver_revision"],
        "target_time": preflight["target_time_seconds"],
        "progress": 0.0,
        "simulation_time": 0.0,
        "result": None,
        "error": None,
    }
    if preflight["ready"]:
        source = Path(preflight["file"])
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        snapshot = snapshot_dir / source.name
        shutil.copy2(source, snapshot)
        snapshot_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if snapshot_hash != preflight["sha256"]:
            shutil.rmtree(workflow_dir, ignore_errors=True)
            raise fds_tools.ToolError("预检后算例发生变化，快照校验失败；没有启动求解。")
        record["snapshot_file"] = str(snapshot)
        record["snapshot_sha256"] = snapshot_hash
        message = f"算例预检查通过，已冻结输入快照，等待运行确认：{case_id}"
    else:
        message = f"算例预检查未通过：{case_id}"
    _write(config, record)
    emit_event(config, "workflow_stage", message, workflow_id=workflow_id, case_id=case_id,
               stage=record["stage"], status=record["status"], errors=preflight["errors"],
               warnings=preflight["warnings"])
    return {"workflow_id": workflow_id, "status": record["status"], "case_id": case_id,
            "preflight": preflight, "snapshot_file": record["snapshot_file"],
            "snapshot_sha256": record.get("snapshot_sha256"),
            "approval_required": bool(preflight["ready"]),
            "message": "只有用户明确确认该算例、求解器和计算范围后，才能启动求解。"}


def _run_job(config: dict, workflow_id: str, cancel_event: threading.Event, timeout_seconds: int) -> None:
    record = _read(config, workflow_id)
    try:
        snapshot = Path(record["snapshot_file"]).resolve()
        expected_root = (_workflow_root(config) / workflow_id / "input").resolve()
        if snapshot.parent != expected_root or not snapshot.is_file():
            raise fds_tools.ToolError("工作流输入快照丢失或路径无效。")
        actual_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if actual_hash != record.get("snapshot_sha256"):
            raise fds_tools.ToolError("工作流输入快照哈希变化，拒绝启动。")

        _stage(config, record, "solving", "running", f"FDS 求解已启动：{record['case_id']}",
               started_at=_now(), owner_pid=os.getpid())

        def register_process(process: subprocess.Popen):
            with _LOCK:
                runtime = _RUNTIMES.get(workflow_id)
                if runtime:
                    runtime["process"] = process
            if cancel_event.is_set() and process.poll() is None:
                process.terminate()

        def progress(update: dict):
            with _LOCK:
                record.update({"step": update.get("step"), "simulation_time": update.get("simulation_time", 0.0),
                               "progress": update.get("progress"), "run_dir": update.get("run_dir")})
                _write(config, record)
            # The callback payload also contains case_id; keep the persistent
            # record's authoritative value and avoid passing the keyword twice.
            event_update = {key: value for key, value in update.items() if key != "case_id"}
            emit_event(config, "workflow_progress", f"工作流 {workflow_id} 求解进度更新",
                       workflow_id=workflow_id, case_id=record["case_id"], stage="solving",
                       status="running", **event_update)

        result = fds_tools.run_case(config, record["case_id"], approved=True, timeout_seconds=timeout_seconds,
                                    source_override=snapshot, cancel_event=cancel_event,
                                    process_callback=register_process, progress_callback=progress)
        solver_revision = None
        log_path = result.get("log")
        if isinstance(log_path, str):
            try:
                log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
                revision_match = re.search(r"(?im)^\s*Revision\s*:\s*(FDS-[^\s]+)", log_text)
                solver_revision = revision_match.group(1) if revision_match else None
            except OSError:
                solver_revision = None
        if solver_revision:
            record["solver_revision"] = solver_revision
            result["solver_revision"] = solver_revision
        if result.get("cancelled"):
            status, stage = "cancelled", "stopped"
        elif result.get("timeout"):
            status, stage = "timeout", "finished"
        elif result.get("success"):
            status, stage = "completed", "results"
        else:
            status, stage = "failed", "results"
        _stage(config, record, stage, status,
               f"工作流 {workflow_id}：" + {"completed": "求解完成", "cancelled": "求解已停止",
                                             "timeout": "求解超时", "failed": "求解失败"}[status],
               finished_at=_now(), result=result, run_dir=result.get("run_dir"),
               solver_revision=solver_revision or record.get("solver_revision"),
               progress=100.0 if status == "completed" else record.get("progress"),
               error=None if status in {"completed", "cancelled"} else result.get("log_tail"))
    except Exception as exc:
        _stage(config, record, "error", "failed", f"工作流 {workflow_id} 失败：{exc}",
               finished_at=_now(), error=str(exc))
    finally:
        with _LOCK:
            _RUNTIMES.pop(workflow_id, None)


def start_simulation(config: dict, workflow_id: str, approved: bool = False,
                     timeout_seconds: int = 3600) -> dict:
    if approved is not True:
        raise fds_tools.ToolError("启动计算前必须先向用户说明算例、solver、输入快照和预检查结果，并取得明确确认。")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 86400:
        raise fds_tools.ToolError("timeout_seconds 必须在 1–86400 之间。")
    record = _read(config, workflow_id)
    if record.get("status") != "awaiting_approval":
        raise fds_tools.ToolError(f"工作流当前状态为 {record.get('status')}，不能启动。")
    if not record.get("snapshot_file"):
        raise fds_tools.ToolError("工作流没有有效的输入快照。")
    cancel_event = threading.Event()
    thread = threading.Thread(target=_run_job, args=(config, workflow_id, cancel_event, timeout_seconds),
                              name=f"fds-workflow-{workflow_id}", daemon=True)
    with _LOCK:
        if workflow_id in _RUNTIMES:
            raise fds_tools.ToolError("该工作流已经在运行。")
        _RUNTIMES[workflow_id] = {"thread": thread, "cancel": cancel_event, "process": None}
        _stage(config, record, "queued", "queued", f"FDS 工作流已排队：{record['case_id']}",
               timeout_seconds=timeout_seconds)
        thread.start()
    return {"workflow_id": workflow_id, "status": "queued", "case_id": record["case_id"],
            "message": "求解已在后台启动；使用 get_simulation_status 查询实时状态。"}


def get_simulation_status(config: dict, workflow_id: str) -> dict:
    record = _read(config, workflow_id)
    if record.get("status") in {"queued", "running", "cancel_requested"}:
        with _LOCK:
            runtime = _RUNTIMES.get(workflow_id)
        if not runtime and record.get("owner_pid") != os.getpid():
            record.update({"status": "interrupted", "stage": "recovery",
                           "error": "运行工作进程已退出；可从保存的输入快照重新准备/启动。"})
            _write(config, record)
    return record


def cancel_simulation(config: dict, workflow_id: str) -> dict:
    record = _read(config, workflow_id)
    with _LOCK:
        runtime = _RUNTIMES.get(workflow_id)
        if runtime:
            runtime["cancel"].set()
            process = runtime.get("process")
            if process and process.poll() is None:
                process.terminate()
            record.update({"status": "cancel_requested", "stage": "stopping"})
            _write(config, record)
        elif record.get("status") == "awaiting_approval":
            _stage(config, record, "cancelled", "cancelled", f"工作流已取消：{workflow_id}")
        else:
            raise fds_tools.ToolError(f"工作流状态为 {record.get('status')}，当前不能停止。")
    emit_event(config, "workflow_stage", f"已请求停止工作流 {workflow_id}", workflow_id=workflow_id,
               case_id=record["case_id"], stage="stopping", status=record["status"])
    return {"workflow_id": workflow_id, "status": record["status"],
            "message": "已请求停止；状态将在求解器进程退出后更新。"}


def list_simulation_workflows(config: dict, limit: int = 20) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise fds_tools.ToolError("limit 必须为 1–100。")
    root = _workflow_root(config)
    items = []
    if root.is_dir():
        for path in sorted(root.glob("*/workflow.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                with _LOCK:
                    record = json.loads(path.read_text(encoding="utf-8"))
                items.append({k: record.get(k) for k in ("workflow_id", "case_id", "status", "stage", "created_at",
                                                          "updated_at", "progress", "simulation_time", "run_dir")})
            except (OSError, json.JSONDecodeError):
                continue
    return {"workflows": items}
