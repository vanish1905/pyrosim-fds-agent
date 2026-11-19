from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import threading
import queue
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import APP_ROOT, case_root, runtime_paths
from .events import emit_event
from .adapter_profiles import build_adapter_profile_report

CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
EDITABLE = {
    "TIME.T_END": ("TIME", "T_END", float, 0.01, 86400.0),
    "TIME.DT": ("TIME", "DT", float, 1e-6, 10.0),
    "MESH.IJK": ("MESH", "IJK", tuple, 1, 100000),
}
SURFACE_EDITABLE = {
    "HRRPUA": ("HRRPUA", 0.0, 100000.0),
    "TMP_FRONT": ("TMP_FRONT", 0.0, 2000.0),
    "MASS_FLUX": ("MASS_FLUX", 0.0, 100.0),
    "VEL": ("VEL", -100.0, 100.0),
    "THICKNESS": ("THICKNESS", 0.0, 10.0),
}
RECORD_RE = re.compile(r"&([A-Z][A-Z0-9_]*)\b(.*?)/", re.I | re.S)
FIELD_START_RE = re.compile(
    r"(?<![A-Z0-9_])([A-Z][A-Z0-9_]*(?:\s*\(\s*[1-9][0-9]{0,2}\s*\))?)\s*=", re.I)


class ToolError(ValueError):
    pass


def _case_id(value: str) -> str:
    if not isinstance(value, str) or not CASE_ID_RE.fullmatch(value):
        raise ToolError("case_id 只能包含字母、数字、下划线和短横线，且不能以符号开头。")
    return value


def _case_file(config: dict, case_id: str) -> Path:
    case_id = _case_id(case_id)
    root = case_root(config)
    path = (root / f"{case_id}.fds").resolve()
    if path.parent != root:
        raise ToolError("算例路径越界。")
    if not path.is_file():
        raise ToolError(f"找不到算例：{case_id}")
    return path


def inspect_installation(config: dict) -> dict:
    root, fds, smokeview = runtime_paths(config)
    pyrosim = root / "PyroSim.exe"
    if not pyrosim.exists():
        pyrosim = root / "pyrosim.exe"
    detected_revision = None
    runs = case_root(config) / "runs"
    if runs.is_dir():
        logs = sorted(runs.glob("*/fds.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for log in logs:
            match = re.search(r"Revision\s*:\s*(FDS-[^\s]+)", log.read_text(encoding="utf-8", errors="replace"))
            if match:
                detected_revision = match.group(1)
                break
    verified_workflow = _latest_verified_workflow_revision(config, fds)
    verified_revision = verified_workflow.get("solver_revision") if verified_workflow else None
    if verified_revision:
        detected_revision = verified_revision
    result = {
        "pyrosim_root": str(root),
        "pyrosim_present": pyrosim.is_file(),
        "pyrosim_exe": str(pyrosim) if pyrosim.is_file() else None,
        "fds_present": fds.is_file(),
        "fds_exe": str(fds) if fds.is_file() else None,
        "smokeview_present": smokeview.is_file(),
        "smokeview_exe": str(smokeview) if smokeview.is_file() else None,
        "pyrosim_results_present": (root / "PyroSimResults.exe").is_file(),
        "pyrosim_results_exe": str(root / "PyroSimResults.exe") if (root / "PyroSimResults.exe").is_file() else None,
        "case_root": str(case_root(config)),
        "last_observed_fds_revision": detected_revision,
        "verified_fds_revision": verified_revision,
        "verified_fds_evidence": verified_workflow,
        "compatibility": (
            "FDS revision verified by a successful workflow; inspect the live PyroSim window before claiming a UI version match"
            if verified_revision else
            "FDS executable is present, but no successful workflow is recorded for this exact configured solver"
        ),
        "version_note": "Executable presence and installation-folder labels are not treated as version or compatibility evidence.",
    }
    result["adapter_profiles"] = build_adapter_profile_report(result)
    return result


def _latest_verified_workflow_revision(config: dict, solver: Path) -> dict | None:
    """Return evidence only when a successful saved workflow used this solver."""
    workflow_root = case_root(config) / ".pyro_agent_workflows"
    if not workflow_root.is_dir() or not solver.is_file():
        return None
    expected = os.path.normcase(os.path.realpath(str(solver)))
    records = sorted(workflow_root.glob("*/workflow.json"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for path in records:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            result = record.get("result") or {}
            actual_solver = os.path.normcase(os.path.realpath(str(record.get("solver") or "")))
            if (record.get("status") == "completed" and result.get("success") is True
                    and actual_solver == expected and record.get("solver_revision")):
                return {
                    "workflow_id": str(record.get("workflow_id") or path.parent.name),
                    "case_id": str(record.get("case_id") or ""),
                    "solver_revision": str(record["solver_revision"]),
                    "completed_at": record.get("finished_at"),
                    "exit_code": result.get("exit_code"),
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def list_cases(config: dict) -> dict:
    root = case_root(config)
    cases = [{"case_id": p.stem, "file": str(p), "bytes": p.stat().st_size}
             for p in sorted(root.glob("*.fds")) if CASE_ID_RE.fullmatch(p.stem)]
    return {"case_root": str(root), "cases": cases}


def stage_official_methane_flame_demo(config: dict) -> dict:
    """Stage the pinned FDS verification example from the bundled official copy.

    The official input is shipped with the workbench, so a destination machine
    does not need GitHub access or an outbound proxy to start the example.
    A fixed official URL remains as a fallback for source checkouts missing the
    bundled example; either path must match the same SHA-256 and content checks.
    """
    case_id = "methane_flame_simple_2"
    source_url = "https://raw.githubusercontent.com/firemodels/fds/master/Verification/Species/methane_flame_simple_2.fds"
    expected_sha256 = "e92e89d650b37d9d9f87af4ddba33a5795c6a7fdf0448a7f8a2865f83cccfbc6"
    bundled_source = APP_ROOT / "examples" / "temperature_methane_demo" / "run" / f"{case_id}.fds"
    destination = case_root(config) / f"{case_id}.fds"
    if destination.exists():
        current_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        if current_hash != expected_sha256:
            raise ToolError(f"目标算例已存在且不是此官方版本：{case_id}")
        return {"case_id": case_id, "file": str(destination), "sha256": current_hash,
                "source_url": source_url, "source_kind": "verified_workspace_copy", "already_staged": True}
    if bundled_source.is_file():
        content = bundled_source.read_bytes()
        source_kind = "bundled_official_copy"
    else:
        request = urllib.request.Request(source_url, headers={"User-Agent": "pyrosim-fds-agent/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read(20_001)
        except (OSError, urllib.error.URLError) as exc:
            raise ToolError(f"未找到随包官方算例，且无法从固定官方地址下载：{exc}") from exc
        source_kind = "downloaded_official_copy"
    if len(content) > 20_000 or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ToolError("算例大小或 SHA-256 与固定官方样例不符；未写入算例目录。")
    text = content.decode("utf-8-sig")
    if not all(token in text for token in ("&HEAD CHID='methane_flame_simple_2'", "HRRPUA = 625.",
                                             "T_END=10.0", "QUANTITY = 'TEMPERATURE'", "QUANTITY = 'HRRPUV'")):
        raise ToolError("官方样例内容未通过结构核验；未写入算例目录。")
    destination.write_bytes(content)
    return {"case_id": case_id, "file": str(destination), "sha256": expected_sha256,
            "source_url": source_url, "source_kind": source_kind, "already_staged": False,
            "demo": {"burner_hrrpua_kw_m2": 625, "burner_area_m2": 0.16,
                     "nominal_peak_hrr_kw": 100, "fire_duration_s": 5,
                     "t_end_s": 10, "mesh_cells": 2000,
                     "slices": ["TEMPERATURE", "HRRPUV"]}}


def create_case(config: dict, source_id: str, new_case_id: str) -> dict:
    source = _case_file(config, source_id)
    new_case_id = _case_id(new_case_id)
    destination = case_root(config) / f"{new_case_id}.fds"
    if destination.exists():
        raise ToolError("目标算例已存在；请使用新的 case_id。")
    text = source.read_text(encoding="utf-8-sig")
    text = re.sub(r"(?i)(\bCHID\s*=\s*)('[^']*'|\"[^\"]*\")", rf"\g<1>'{new_case_id}'", text, count=1)
    destination.write_text(text, encoding="utf-8")
    return {"case_id": new_case_id, "source_id": source_id, "file": str(destination), "copied": True}


def inspect_case(config: dict, case_id: str) -> dict:
    path = _case_file(config, case_id)
    text = path.read_text(encoding="utf-8-sig")
    records = []
    for match in RECORD_RE.finditer(text):
        fields = _parse_namelist_fields(match.group(2))
        records.append({"record": match.group(1).upper(), "fields": fields})
    return {"case_id": case_id, "file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "records": records}


def inspect_boundary_records(config: dict, case_id: str, record_type: str | None = None) -> dict:
    """Return an addressable inventory of SURF/VENT/GEOM boundaries and references."""
    if record_type is not None:
        record_type = record_type.upper() if isinstance(record_type, str) else record_type
        if record_type not in {"SURF", "VENT", "GEOM"}:
            raise ToolError("record_type 仅支持 SURF、VENT 或 GEOM。")
    case = inspect_case(config, case_id)
    surface_ids = [record["fields"].get("ID") for record in case["records"]
                   if record["record"] == "SURF" and record["fields"].get("ID")]
    indexes = {name: 0 for name in ("SURF", "VENT", "GEOM")}
    records = []
    for global_index, record in enumerate(case["records"], 1):
        name = record["record"]
        if name not in indexes or (record_type is not None and name != record_type):
            continue
        indexes[name] += 1
        fields = record["fields"]
        identifier = fields.get("ID")
        reference = fields.get("SURF_ID")
        if reference is None:
            reference_state = None
        elif str(reference).upper() == "OPEN":
            reference_state = "open"
        else:
            matching = sum(1 for value in surface_ids if value == reference)
            reference_state = "unique" if matching == 1 else "missing" if matching == 0 else "duplicate"
        records.append({"record_type": name, "type_index": indexes[name], "record_index": global_index,
                        "id": identifier, "id_unique": bool(identifier) and sum(
                            1 for item in case["records"] if item["record"] == name
                            and item["fields"].get("ID") == identifier) == 1,
                        "surface_id": reference, "surface_reference_state": reference_state,
                        "fields": fields})
    return {"case_id": case_id, "file": case["file"], "sha256": case["sha256"],
            "surface_id_count": len(surface_ids), "records": records,
            "note": "只读盘点 SURF/VENT/GEOM。type_index 与 edit_boundary_fields 的同类型索引对应；若记录有唯一 ID，也可按 ID 定位。字段名是否受当前 FDS 版本支持仍由 FDS 解析器确认。"}


def compare_boundary_records(config: dict, case_a: str, case_b: str,
                              record_type: str | None = None) -> dict:
    """Compare boundary records between cases without returning full GEOM arrays."""
    if record_type is not None:
        record_type = record_type.upper() if isinstance(record_type, str) else record_type
        if record_type not in {"SURF", "VENT", "GEOM"}:
            raise ToolError("record_type 仅支持 SURF、VENT 或 GEOM。")
    left = inspect_boundary_records(config, case_a, record_type)
    right = inspect_boundary_records(config, case_b, record_type)
    changes = []
    types = (record_type,) if record_type else ("SURF", "VENT", "GEOM")

    def keyed(records, kind):
        selected = [item for item in records if item["record_type"] == kind]
        counts = {}
        for item in selected:
            if item["id"] is not None:
                counts[item["id"]] = counts.get(item["id"], 0) + 1
        result = {}
        for item in selected:
            key = ("id", item["id"]) if item["id"] is not None and counts[item["id"]] == 1 \
                else ("index", item["type_index"])
            result[key] = item
        return result

    def fields_for(item):
        fields = item["fields"]
        if item["record_type"] != "GEOM":
            return fields
        vertices = fields.get("VERTS", [])
        faces = fields.get("FACES", [])
        summary = {key: value for key, value in fields.items() if key not in {"VERTS", "FACES"}}
        summary["_mesh_summary"] = {
            "vertices": len(vertices) // 3,
            "triangles": len(faces) // 4,
            "vertices_sha256": hashlib.sha256(json.dumps(
                vertices, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
            "faces_sha256": hashlib.sha256(json.dumps(
                faces, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
        }
        return summary

    for kind in types:
        a_items, b_items = keyed(left["records"], kind), keyed(right["records"], kind)
        for key in sorted(a_items.keys() - b_items.keys(), key=repr):
            item = a_items[key]
            changes.append({"change": "removed", "record_type": kind, "id": item["id"],
                            "type_index": item["type_index"], "before": fields_for(item)})
        for key in sorted(b_items.keys() - a_items.keys(), key=repr):
            item = b_items[key]
            changes.append({"change": "added", "record_type": kind, "id": item["id"],
                            "type_index": item["type_index"], "after": fields_for(item)})
        for key in sorted(a_items.keys() & b_items.keys(), key=repr):
            before, after = a_items[key], b_items[key]
            before_fields, after_fields = fields_for(before), fields_for(after)
            field_changes = [{"field": field, "before": before_fields.get(field),
                              "after": after_fields.get(field)}
                             for field in sorted(before_fields.keys() | after_fields.keys())
                             if before_fields.get(field) != after_fields.get(field)]
            if field_changes:
                changes.append({"change": "modified", "record_type": kind,
                                "id": after["id"] or before["id"],
                                "type_index_before": before["type_index"],
                                "type_index_after": after["type_index"],
                                "fields": field_changes})
    return {"case_a": case_a, "sha256_a": left["sha256"],
            "case_b": case_b, "sha256_b": right["sha256"],
            "record_type_filter": record_type, "equal": not changes,
            "change_count": len(changes), "changes": changes,
            "note": "只比较 SURF/VENT/GEOM namelist。GEOM 的顶点和面数组以数量与 SHA-256 摘要呈现，不返回大数组；相同 ID 优先匹配，否则使用各类型序号。此对比不验证 FDS 物理有效性。"}


def compare_case_inputs(config: dict, case_a: str, case_b: str,
                        fields: list[str] | None = None) -> dict:
    """Compare a small set of simulation-critical values between two workspace cases."""
    default_fields = ["TIME.T_END", "TIME.DT", "MESH.IJK", "MESH.XB"]
    fields = default_fields if fields is None else fields
    if not isinstance(fields, list) or not fields or len(fields) > 16:
        raise ToolError("fields 必须是 1–16 个可比较字段组成的数组。")
    allowed = {"TIME.T_END", "TIME.DT", "MESH.IJK", "MESH.XB", "HEAD.CHID"}
    unknown = [field for field in fields if field not in allowed]
    if unknown:
        raise ToolError(f"不支持比较字段：{', '.join(map(str, unknown))}。当前支持 {', '.join(sorted(allowed))}。")

    left = inspect_case(config, case_a)
    right = inspect_case(config, case_b)

    def extract(model: dict, dotted: str):
        record_name, field_name = dotted.split(".", 1)
        values = [record["fields"][field_name] for record in model["records"]
                  if record["record"] == record_name and field_name in record["fields"]]
        if not values:
            return None
        return values if record_name == "MESH" else values[0]

    differences = []
    for dotted in fields:
        value_a, value_b = extract(left, dotted), extract(right, dotted)
        equal = value_a == value_b
        differences.append({"field": dotted, "case_a": value_a, "case_b": value_b, "equal": equal})
    return {"case_a": case_a, "file_a": left["file"], "sha256_a": left["sha256"],
            "case_b": case_b, "file_b": right["file"], "sha256_b": right["sha256"],
            "fields": differences, "equal": all(item["equal"] for item in differences),
            "note": "这是 FDS 输入字段比对；它不证明 PyroSim 几何或模型树与源模型完全等价。"}


def preflight_case(config: dict, case_id: str) -> dict:
    """Run conservative, read-only checks before creating a solver job."""
    path = _case_file(config, case_id)
    text = path.read_text(encoding="utf-8-sig")
    records = []
    for match in RECORD_RE.finditer(text):
        records.append({"record": match.group(1).upper(), "fields": _parse_namelist_fields(match.group(2))})
    by_name: dict[str, list[dict]] = {}
    for record in records:
        by_name.setdefault(record["record"], []).append(record["fields"])

    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str, severity: str = "error"):
        checks.append({"name": name, "passed": passed, "detail": detail, "severity": severity})
        if not passed:
            (errors if severity == "error" else warnings).append(detail)

    heads = by_name.get("HEAD", [])
    times = by_name.get("TIME", [])
    meshes = by_name.get("MESH", [])
    check("HEAD", len(heads) == 1, "需要且只能有一条 &HEAD 记录。" if len(heads) != 1 else "&HEAD 记录存在。")
    check("TIME", len(times) == 1, "需要且只能有一条 &TIME 记录。" if len(times) != 1 else "&TIME 记录存在。")
    check("MESH", len(meshes) > 0, "至少需要一条 &MESH 记录。" if not meshes else f"发现 {len(meshes)} 个网格。")

    chid = heads[0].get("CHID") if len(heads) == 1 else None
    check("CHID", isinstance(chid, str) and bool(chid.strip()), "&HEAD.CHID 缺失或为空。" if not chid else f"CHID={chid}。")
    t_end = times[0].get("T_END") if len(times) == 1 else None
    try:
        t_end_value = float(t_end)
        valid_t_end = t_end_value > 0 and t_end_value < float("inf")
    except (TypeError, ValueError):
        t_end_value, valid_t_end = None, False
    check("T_END", valid_t_end, "&TIME.T_END 必须是大于 0 的有限数值。" if not valid_t_end else f"结束时间 {t_end_value:g} s。")

    mesh_summaries = []
    total_cells = 0
    for index, mesh in enumerate(meshes, 1):
        ijk = mesh.get("IJK")
        xb = mesh.get("XB")
        valid_ijk = isinstance(ijk, list) and len(ijk) == 3 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) == value and value > 0
            for value in ijk
        )
        valid_xb = isinstance(xb, list) and len(xb) == 6 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and abs(float(value)) < float("inf")
            for value in xb
        )
        if valid_xb:
            valid_xb = all(float(xb[i + 1]) > float(xb[i]) for i in (0, 2, 4))
        check(f"MESH[{index}].IJK", valid_ijk, f"第 {index} 个网格 IJK 需要 3 个正整数。" if not valid_ijk else f"网格 {index} 单元数 {int(ijk[0])}×{int(ijk[1])}×{int(ijk[2])}。")
        check(f"MESH[{index}].XB", valid_xb, f"第 {index} 个网格 XB 需要 6 个有限值且各轴上限大于下限。" if not valid_xb else f"网格 {index} 坐标范围有效。")
        cells = int(ijk[0]) * int(ijk[1]) * int(ijk[2]) if valid_ijk else None
        if cells is not None:
            total_cells += cells
            if cells > 2_000_000:
                warnings.append(f"第 {index} 个网格约 {cells:,} 个单元；请核对内存和计算时间预算。")
            mesh_summaries.append({"index": index, "ijk": [int(v) for v in ijk], "cells": cells,
                                   "xb": [float(v) for v in xb] if valid_xb else None})
        else:
            mesh_summaries.append({"index": index, "ijk": ijk, "cells": None,
                                   "xb": [float(v) for v in xb] if valid_xb else None})

    root, fds, _ = runtime_paths(config)
    check("FDS", fds.is_file(), f"找不到 FDS 求解器：{fds}" if not fds.is_file() else f"求解器：{fds}")
    return {
        "case_id": case_id,
        "file": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "solver": str(fds),
        "solver_revision": _latest_fds_revision(config),
        "record_count": len(records),
        "mesh_count": len(meshes),
        "total_cells": total_cells,
        "target_time_seconds": t_end_value,
        "meshes": mesh_summaries,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "ready": not errors,
        "note": "这是静态预检查；最终语法、数值稳定性和物理适用性仍需由匹配版本的 FDS 求解器和工程判断确认。",
    }


def _parse_namelist_fields(body: str) -> dict:
    """Parse comma-separated FDS values without truncating arrays at their first comma."""
    starts = []
    quote = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "!":
            newline = body.find("\n", i)
            i = len(body) if newline < 0 else newline + 1
            continue
        candidate = FIELD_START_RE.match(body, i)
        if candidate:
            starts.append((candidate.start(1), candidate.end(),
                           re.sub(r"\s+", "", candidate.group(1)).upper()))
            i = candidate.end()
            continue
        i += 1

    fields = {}
    for index, (_, value_start, key) in enumerate(starts):
        value_end = starts[index + 1][0] if index + 1 < len(starts) else len(body)
        raw = body[value_start:value_end].strip().rstrip(",").strip()
        fields[key] = _parse_namelist_value(raw)
    return fields


def _parse_namelist_value(raw: str):
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1].strip()
    items = []
    quote = None
    start = 0
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote:
            if char == quote:
                if index + 1 < len(raw) and raw[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ",":
            items.append(raw[start:index].strip())
            start = index + 1
        index += 1
    if items:
        items.append(raw[start:].strip())
        return [_parse_namelist_scalar(item) for item in items if item]
    return _parse_namelist_scalar(raw)


def _parse_namelist_scalar(raw: str):
    value = raw.strip().strip(",")
    if not value:
        return value
    if value.startswith(("'", '"')) and len(value) >= 2 and value[-1] == value[0]:
        quote = value[0]
        return value[1:-1].replace(quote + quote, quote)
    if value.upper() in {".TRUE.", ".FALSE."}:
        return value.upper() == ".TRUE."
    try:
        number = float(value.replace("D", "E").replace("d", "e"))
        return int(number) if number.is_integer() and not any(c in value.lower() for c in (".", "e", "d")) else number
    except ValueError:
        return value


def _namelist_field_tokens(body: str) -> list[dict]:
    """Locate field names and simple value tokens outside quotes and comments."""
    tokens = []
    quote = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "!":
            newline = body.find("\n", i)
            i = len(body) if newline < 0 else newline + 1
            continue
        candidate = FIELD_START_RE.match(body, i)
        if not candidate:
            i += 1
            continue
        value_start = candidate.end()
        while value_start < len(body) and body[value_start] in " \t\r\n":
            value_start += 1
        value_end = value_start
        while value_end < len(body) and body[value_end] not in ",/! \t\r\n":
            value_end += 1
        tokens.append({
            "key": re.sub(r"\s+", "", candidate.group(1)).upper(),
            "field_start": candidate.start(1),
            "value_span": (value_start, value_end) if value_end > value_start else None,
        })
        i = candidate.end()
    return tokens


def _namelist_content_end(body: str) -> int:
    """Return the offset just past the last non-comment, non-whitespace character."""
    quote = None
    comment = False
    last = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if comment:
            if ch == "\n":
                comment = False
            i += 1
            continue
        if quote:
            if ch == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 2
                    last = i
                    continue
                quote = None
            i += 1
            last = i
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            last = i
            continue
        if ch == "!":
            comment = True
            i += 1
            continue
        if not ch.isspace():
            i += 1
            last = i
            continue
        i += 1
    return last


def _replace_or_append_scalar_field(body: str, field_name: str, replacement: str) -> str:
    """Replace one simple scalar token in place, or append the field to the record."""
    tokens = _namelist_field_tokens(body)
    matches = [(index, token) for index, token in enumerate(tokens) if token["key"] == field_name]
    if len(matches) > 1:
        raise ToolError(f"&SURF 记录中的 {field_name} 出现多次，无法确定要替换哪一个。")
    if matches:
        index, token = matches[0]
        span = token["value_span"]
        if span is None:
            raise ToolError(f"&SURF 记录中的 {field_name} 没有可替换的标量值。")
        start, end = span
        if body[start] in "('\"":
            raise ToolError(f"&SURF 记录中的 {field_name} 不是简单标量值，无法安全替换。")
        next_start = tokens[index + 1]["field_start"] if index + 1 < len(tokens) else len(body)
        tail = re.sub(r"![^\n]*", "", body[end:next_start])
        if tail.strip(" \t\r\n,"):
            raise ToolError(f"&SURF 记录中的 {field_name} 不是单个标量值，无法安全替换。")
        return body[:start] + replacement + body[end:]
    cut = _namelist_content_end(body)
    if cut and not body[:cut].rstrip().endswith(","):
        # Put the separator before any trailing comment so the appended field,
        # which goes after the comment/newline, is not swallowed by that comment.
        body = body[:cut] + "," + body[cut:]
    return body + " " + f"{field_name}={replacement}" + " "


def update_parameters(config: dict, case_id: str, updates: dict) -> dict:
    path = _case_file(config, case_id)
    if not isinstance(updates, dict) or not updates or len(updates) > 12:
        raise ToolError("updates 必须是 1–12 个字段的对象。")
    text = path.read_text(encoding="utf-8-sig")
    changed = []
    for dotted, value in updates.items():
        if dotted not in EDITABLE:
            raise ToolError(f"不允许修改 {dotted}；当前仅开放 {', '.join(EDITABLE)}。")
        record_name, field_name, kind, low, high = EDITABLE[dotted]
        record = next((m for m in RECORD_RE.finditer(text) if m.group(1).upper() == record_name), None)
        if not record:
            raise ToolError(f"算例没有 &{record_name} 记录；请先使用包含该记录的模板。")
        if kind is tuple:
            if not isinstance(value, list) or len(value) != 3 or any(isinstance(x, bool) or not isinstance(x, int) or x < low or x > high for x in value):
                raise ToolError("MESH.IJK 必须是 3 个正整数的数组。")
            replacement = ",".join(map(str, value))
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
                raise ToolError(f"{dotted} 必须在 {low}–{high} 范围内。")
            replacement = f"{float(value):g}"
        body = record.group(2)
        if dotted == "MESH.IJK":
            pattern = re.compile(r"\bIJK\s*=\s*(?:\(?\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)?|\d+)", re.I)
        else:
            pattern = re.compile(rf"\b{field_name}\s*=\s*('[^']*'|\([^)]*\)|[^,\s/]+)", re.I)
        if pattern.search(body):
            body = pattern.sub(f"{field_name}={replacement}", body, count=1)
        else:
            body = body.rstrip() + f", {field_name}={replacement}"
        text = text[:record.start(2)] + body + text[record.end(2):]
        changed.append({"field": dotted, "value": value})
    path.write_text(text, encoding="utf-8")
    return {"case_id": case_id, "updated": changed, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def create_case_with_updates(config: dict, source_id: str, new_case_id: str, updates: dict) -> dict:
    """Create an isolated case, apply allowlisted edits, and verify the saved values."""
    created = create_case(config, source_id, new_case_id)
    destination = Path(created["file"])
    try:
        update_result = update_parameters(config, new_case_id, updates)
        readback = inspect_case(config, new_case_id)
        actual = {}
        records = readback["records"]
        for dotted in updates:
            record_name, field_name, *_ = EDITABLE[dotted]
            record = next((r["fields"] for r in records if r["record"] == record_name), None)
            actual[dotted] = record.get(field_name) if record else None
        mismatches = {field: {"expected": value, "actual": actual[field]}
                      for field, value in updates.items() if actual[field] != value}
        if mismatches:
            raise ToolError(f"保存后回读校验不一致：{mismatches}")
        return {
            "case_id": new_case_id,
            "source_id": source_id,
            "file": created["file"],
            "source_unchanged": True,
            "updated": update_result["updated"],
            "readback": actual,
            "sha256": readback["sha256"],
            "verified": True,
        }
    except Exception:
        # This destination was created by this call. Remove it if any edit or
        # readback check fails so a partial case cannot be mistaken as complete.
        destination.unlink(missing_ok=True)
        raise


def create_case_with_surface_updates(config: dict, source_id: str, new_case_id: str,
                                     surface_id: str, updates: dict) -> dict:
    """Clone a case and edit allowlisted numeric fields on one existing &SURF.

    The edit surface is intentionally narrow: exactly one existing SURF is
    selected by ID, and only the allowlisted scalar fields below can change.
    No SURF is created, no other field or record is edited, and no arbitrary
    FDS text is accepted. Any failure removes only the copy made by this call.
    """
    import math

    if not isinstance(surface_id, str) or not surface_id or len(surface_id) > 128:
        raise ToolError("surface_id 必须是非空字符串，用于精确匹配已有 &SURF.ID。")
    if not isinstance(updates, dict) or not updates or len(updates) > len(SURFACE_EDITABLE):
        raise ToolError(f"updates 必须包含 1–{len(SURFACE_EDITABLE)} 个允许的表面字段。")
    normalized: dict[str, float] = {}
    for field, value in updates.items():
        if not isinstance(field, str) or field not in SURFACE_EDITABLE:
            raise ToolError(f"不允许修改表面字段 {field}；当前仅开放 {', '.join(SURFACE_EDITABLE)}。")
        _, low, high = SURFACE_EDITABLE[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{field} 必须是 {low:g}–{high:g} 范围内的有限数值。")
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            raise ToolError(f"{field} 必须是 {low:g}–{high:g} 范围内的有限数值。") from None
        if not math.isfinite(numeric) or not low <= numeric <= high:
            raise ToolError(f"{field} 必须在 {low:g}–{high:g} 范围内。")
        normalized[field] = numeric

    source_path = _case_file(config, source_id)
    source_text = source_path.read_text(encoding="utf-8-sig")
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_records = [{"record": match.group(1).upper(),
                       "fields": _parse_namelist_fields(match.group(2))}
                      for match in RECORD_RE.finditer(source_text)]
    source_match_objects = list(RECORD_RE.finditer(source_text))

    created = create_case(config, source_id, new_case_id)
    destination = Path(created["file"])
    try:
        text = destination.read_text(encoding="utf-8-sig")
        matches = list(RECORD_RE.finditer(text))
        records = [{"record": match.group(1).upper(),
                    "fields": _parse_namelist_fields(match.group(2))}
                   for match in matches]
        surf_indexes = [index for index, record in enumerate(records)
                        if record["record"] == "SURF" and record["fields"].get("ID") == surface_id]
        if not surf_indexes:
            raise ToolError(f"复制后的算例中找不到 ID 为 {surface_id!r} 的 &SURF 记录。")
        if len(surf_indexes) > 1:
            raise ToolError(f"复制后的算例中存在 {len(surf_indexes)} 条 ID 为 {surface_id!r} 的 &SURF 记录，"
                            "无法确定唯一目标。")
        target_index = surf_indexes[0]
        target_match = matches[target_index]
        if (target_index >= len(source_records)
                or source_records[target_index]["record"] != "SURF"
                or source_records[target_index]["fields"].get("ID") != surface_id):
            raise ToolError("源算例与复制算例的 &SURF 记录位置不一致；已回滚新副本。")

        body = target_match.group(2)
        changes = []
        for field, numeric in normalized.items():
            existed = any(token["key"] == field for token in _namelist_field_tokens(body))
            body = _replace_or_append_scalar_field(body, field, repr(numeric))
            changes.append({"field": field, "value": numeric,
                            "action": "replaced" if existed else "appended"})
        text = text[:target_match.start(2)] + body + text[target_match.end(2):]
        destination.write_text(text, encoding="utf-8")

        readback = inspect_case(config, new_case_id)
        readback_surfaces = [record for record in readback["records"]
                             if record["record"] == "SURF" and record["fields"].get("ID") == surface_id]
        if len(readback_surfaces) != 1:
            raise ToolError(f"回读得到 {len(readback_surfaces)} 条 ID 为 {surface_id!r} 的 &SURF 记录，"
                            "无法确认唯一目标。")
        surface = readback_surfaces[0]["fields"]
        mismatches = {field: {"expected": value, "actual": surface.get(field)}
                      for field, value in normalized.items() if surface.get(field) != value}
        if mismatches:
            raise ToolError(f"保存后表面字段回读不一致：{mismatches}；已回滚新副本。")

        source_surf_count = sum(1 for record in source_records if record["record"] == "SURF")
        readback_surf_count = sum(1 for record in readback["records"] if record["record"] == "SURF")
        if readback_surf_count != source_surf_count:
            raise ToolError("回读发现 &SURF 记录数量发生变化；已回滚新副本。")
        if len(readback["records"]) != len(source_records):
            raise ToolError("回读发现 FDS 记录数量发生变化；已回滚新副本。")

        new_matches = list(RECORD_RE.finditer(text))
        if len(new_matches) != len(source_match_objects):
            raise ToolError("回读发现 FDS 记录边界发生变化；已回滚新副本。")
        for index, (before, after) in enumerate(zip(source_records, readback["records"])):
            if index == target_index:
                if set(after["fields"]) != set(before["fields"]) | set(normalized):
                    raise ToolError("目标 &SURF 的字段集合被意外改变；已回滚新副本。")
                for key, value in before["fields"].items():
                    if key in normalized:
                        continue
                    if after["fields"].get(key) != value:
                        raise ToolError(f"目标 &SURF 的非更新字段 {key} 被意外改变；已回滚新副本。")
                continue
            if before["record"] == "HEAD":
                before_fields = dict(before["fields"])
                after_fields = dict(after["fields"])
                before_fields.pop("CHID", None)
                after_fields.pop("CHID", None)
                if before_fields != after_fields:
                    raise ToolError("回读发现 &HEAD 的非 CHID 字段被意外改变；已回滚新副本。")
                continue
            if before["fields"] != after["fields"]:
                raise ToolError(f"回读发现 &{before['record']} 记录字段被意外改变；已回滚新副本。")
            if source_match_objects[index].group(0) != new_matches[index].group(0):
                raise ToolError(f"回读发现 &{before['record']} 记录文本被意外改变；已回滚新副本。")

        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
            raise ToolError("源算例在操作期间发生变化；已回滚新副本。")
        return {
            "case_id": new_case_id,
            "source_id": source_id,
            "file": created["file"],
            "source_sha256": source_sha256,
            "source_unchanged": True,
            "surface_id": surface_id,
            "updates": changes,
            "surface": {"record": "SURF", "fields": surface},
            "readback": {field: surface.get(field) for field in normalized},
            "record_count_before": len(source_records),
            "record_count_after": len(readback["records"]),
            "sha256": readback["sha256"],
            "verified": True,
            "note": ("只修改了一个现有 &SURF 的允许数值字段；未创建新 &SURF，也不接受任意 FDS 文本。"
                     "已验证源文件哈希未变、其他记录未被改动并完成解析回读。未启动求解或 PyroSim；"
                     "字段物理合理性、表面引用和整体模型仍需人工审查并执行 preflight。"),
        }
    except Exception:
        # `created` is set only after create_case returns, so this removes just
        # the copy this call made and never the source or any other case.
        destination.unlink(missing_ok=True)
        raise


def create_case_with_burner_obstruction(config: dict, source_id: str, new_case_id: str,
                                       obstruction_id: str, surface_id: str,
                                       xb: list[float], hrrpua_kw_m2: float) -> dict:
    """Clone a case and append one bounded burner block (SURF + OBST), then read it back."""
    import math

    def identifier(value: str, label: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value):
            raise ToolError(f"{label} 只能以字母开头，并包含最多 32 个字母、数字、_ 或 -。")
        return value

    obstruction_id = identifier(obstruction_id, "obstruction_id")
    surface_id = identifier(surface_id, "surface_id")
    if not isinstance(xb, list) or len(xb) != 6 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in xb):
        raise ToolError("xb 必须是 6 个有限数值：[xmin,xmax,ymin,ymax,zmin,zmax]。")
    coords = [float(v) for v in xb]
    if any(coords[i] >= coords[i + 1] for i in (0, 2, 4)):
        raise ToolError("xb 每个轴的最小值都必须小于最大值。")
    if isinstance(hrrpua_kw_m2, bool) or not isinstance(hrrpua_kw_m2, (int, float)) or not math.isfinite(hrrpua_kw_m2) or not 0 < hrrpua_kw_m2 <= 100000:
        raise ToolError("hrrpua_kw_m2 必须在 (0, 100000] kW/m² 范围内。")

    created = create_case(config, source_id, new_case_id)
    destination = Path(created["file"])
    try:
        text = destination.read_text(encoding="utf-8-sig")
        records = [{"record": m.group(1).upper(), "fields": _parse_namelist_fields(m.group(2))}
                   for m in RECORD_RE.finditer(text)]
        meshes = [r["fields"].get("XB") for r in records if r["record"] == "MESH"]
        if not meshes or any(not isinstance(mesh, list) or len(mesh) != 6 for mesh in meshes):
            raise ToolError("需要可解析的 MESH.XB 才能安全验证几何范围。")
        inside = any(all(float(mesh[i]) <= coords[i] and coords[i + 1] <= float(mesh[i + 1])
                         for i in (0, 2, 4)) for mesh in meshes)
        if not inside:
            raise ToolError("障碍物必须完整位于至少一个网格的 MESH.XB 范围内。")

        existing_ids = set()
        for record in records:
            fields = record["fields"]
            if record["record"] == "OBST" and fields.get("ID"):
                existing_ids.add(str(fields["ID"]))
            if record["record"] == "SURF" and fields.get("ID"):
                existing_ids.add(str(fields["ID"]))
        if obstruction_id in existing_ids or surface_id in existing_ids:
            raise ToolError("障碍物或表面 ID 已存在；请使用新的 ID。")

        surface = f"&SURF ID='{surface_id}', HRRPUA={float(hrrpua_kw_m2):g} /"
        obstruction = f"&OBST ID='{obstruction_id}', XB={','.join(f'{v:g}' for v in coords)}, SURF_ID='{surface_id}' /"
        tail = re.search(r"(?im)^\s*&TAIL\b", text)
        insertion = surface + "\n" + obstruction + "\n"
        if tail:
            text = text[:tail.start()] + insertion + text[tail.start():]
        else:
            text = text.rstrip() + "\n" + insertion
        destination.write_text(text, encoding="utf-8")

        readback = inspect_case(config, new_case_id)
        surf = next((r["fields"] for r in readback["records"] if r["record"] == "SURF" and r["fields"].get("ID") == surface_id), None)
        obst = next((r["fields"] for r in readback["records"] if r["record"] == "OBST" and r["fields"].get("ID") == obstruction_id), None)
        if not surf or not obst or surf.get("HRRPUA") != float(hrrpua_kw_m2) or obst.get("XB") != coords or obst.get("SURF_ID") != surface_id:
            raise ToolError("新增障碍物/边界写入后回读不一致；已回滚新副本。")
        return {"case_id": new_case_id, "source_id": source_id, "file": created["file"],
                "source_unchanged": True, "obstruction": obst, "surface": surf,
                "sha256": readback["sha256"], "verified": True,
                "note": "已验证 FDS 输入记录和值；未验证几何是否与现有障碍物重叠，也未启动求解。请检查网格、重叠和物理设置后再预检。"}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def add_obstruction_to_case(config: dict, source_id: str, new_case_id: str, xb: list[float]) -> dict:
    """Clone a case and append one XB-only axis-aligned &OBST, then read it back.

    The edit surface is intentionally narrow: coordinates are the only input, so
    no arbitrary FDS text, identifiers, or SURF fields can be injected. The new
    block must fit fully inside at least one source MESH.XB domain. The source
    file is never written; any failure removes only the copy made by this call.
    """
    import math

    invalid_xb = ("xb 必须是 6 个有限数值：[xmin,xmax,ymin,ymax,zmin,zmax]。")
    if not isinstance(xb, list) or len(xb) != 6:
        raise ToolError(invalid_xb)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in xb):
        raise ToolError(invalid_xb)
    try:
        coords = [float(value) for value in xb]
    except (OverflowError, ValueError):
        raise ToolError(invalid_xb) from None
    if not all(math.isfinite(value) for value in coords):
        raise ToolError(invalid_xb)
    if any(coords[i] >= coords[i + 1] for i in (0, 2, 4)):
        raise ToolError("xb 每个轴的最小值都必须小于最大值。")
    new_case_id = _case_id(new_case_id)

    source_path = _case_file(config, source_id)
    source_text = source_path.read_text(encoding="utf-8-sig")
    source_records = [{"record": m.group(1).upper(), "fields": _parse_namelist_fields(m.group(2))}
                      for m in RECORD_RE.finditer(source_text)]
    source_meshes = [record["fields"] for record in source_records if record["record"] == "MESH"]
    if not source_meshes:
        raise ToolError("源算例没有 &MESH 记录，无法验证障碍物是否位于计算域内。")
    mesh_domains = []
    for index, fields in enumerate(source_meshes, 1):
        mesh_xb = fields.get("XB")
        valid = (isinstance(mesh_xb, list) and len(mesh_xb) == 6
                 and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
                         for v in mesh_xb)
                 and all(float(mesh_xb[i]) < float(mesh_xb[i + 1]) for i in (0, 2, 4)))
        if not valid:
            raise ToolError(f"源算例第 {index} 个 &MESH 的 XB 缺失或无效，无法安全验证障碍物范围。")
        mesh_domains.append([float(v) for v in mesh_xb])
    containing_meshes = [index for index, domain in enumerate(mesh_domains, 1)
                         if all(domain[i] <= coords[i] and coords[i + 1] <= domain[i + 1] for i in (0, 2, 4))]
    if not containing_meshes:
        raise ToolError("障碍物必须完整位于至少一个源 &MESH.XB 范围内（边界可重合）。")

    destination = case_root(config) / f"{new_case_id}.fds"
    if destination.exists():
        raise ToolError("目标算例已存在；请使用新的 case_id。")
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    created = None
    try:
        created = create_case(config, source_id, new_case_id)
        destination = Path(created["file"])
        text = destination.read_text(encoding="utf-8-sig")
        record_text = "&OBST XB=" + ",".join(repr(value) for value in coords) + " /"
        tail = re.search(r"(?im)^\s*&TAIL\b", text)
        insertion = record_text + "\n"
        if tail:
            text = text[:tail.start()] + insertion + text[tail.start():]
        else:
            text = text.rstrip() + "\n" + insertion
        destination.write_text(text, encoding="utf-8")

        readback = inspect_case(config, new_case_id)
        source_obst_count = sum(1 for record in source_records if record["record"] == "OBST")
        appended_obstructions = [record for record in readback["records"] if record["record"] == "OBST"]
        if len(appended_obstructions) != source_obst_count + 1:
            raise ToolError("回读发现 &OBST 记录数量不是源算例加一；已回滚新副本。")
        appended = appended_obstructions[-1]
        if set(appended["fields"]) != {"XB"} or appended["fields"].get("XB") != coords:
            raise ToolError(
                f"新增 &OBST 的 XB 回读不一致：{appended['fields'].get('XB')}，应为 {coords}；已回滚新副本。")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
            raise ToolError("源算例在操作期间发生变化；已回滚新副本。")
        return {
            "case_id": new_case_id,
            "source_id": source_id,
            "file": created["file"],
            "source_sha256": source_sha256,
            "source_unchanged": True,
            "obstruction": {"record": "OBST", "fields": appended["fields"],
                            "record_text": record_text, "mesh_indices": containing_meshes},
            "readback": {"XB": appended["fields"]["XB"]},
            "record_count_before": len(source_records),
            "record_count_after": len(readback["records"]),
            "sha256": readback["sha256"],
            "verified": True,
            "note": ("只追加了一条仅含 XB 的实体 &OBST，未设置 ID、SURF 或任意 FDS 文本；"
                     "已验证源文件未变、XB 位于指定网格域内并回读一致。未检查与现有障碍物重叠，"
                     "也未验证网格分辨率或物理适用性；请继续人工审查并执行 preflight，本工具不启动求解。"),
        }
    except Exception:
        # `created` is only set after create_case returns, so this removes just
        # the copy that this call made and leaves the source and other files alone.
        if created is not None:
            Path(created["file"]).unlink(missing_ok=True)
        raise


def _parse_surface_mesh(path: Path) -> tuple[
        list[tuple[float, float, float]], list[tuple[int, int, int]], list[str | None]]:
    """Read a bounded OBJ or STL triangle mesh without loading third-party code."""
    import math
    import struct

    if path.stat().st_size > 50 * 1024 * 1024:
        raise ToolError("CAD 网格文件超过 50 MiB 限制。")
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    face_materials: list[str | None] = []

    def vertex_index(point):
        point = tuple(float(v) for v in point)
        if len(point) != 3 or not all(math.isfinite(v) for v in point):
            raise ToolError("网格包含非有限或非三维顶点。")
        vertices.append(point)
        return len(vertices)

    def add_polygon(indices, material=None):
        if len(indices) != 3:
            raise ToolError("OBJ 必须已三角化；请在 CAD/BIM 工具中先三角化再导出，以避免改变多边形边界。")
        faces.append(tuple(indices))
        face_materials.append(material)
        if len(faces) > 20000:
            raise ToolError("CAD 网格三角面超过 20,000 个限制；请在 CAD 工具中先简化网格。")

    if path.suffix.lower() == ".obj":
        source_vertices = []
        current_material = None
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                parts = line.partition("#")[0].split()
                if not parts:
                    continue
                if parts[0] == "v":
                    if len(parts) < 4:
                        raise ToolError(f"OBJ 第 {line_number} 行的 v 记录缺少坐标。")
                    point = tuple(float(v) for v in parts[1:4])
                    if not all(math.isfinite(v) for v in point):
                        raise ToolError(f"OBJ 第 {line_number} 行含非有限坐标。")
                    source_vertices.append(point)
                    if len(source_vertices) > 10000:
                        raise ToolError("CAD 网格顶点超过 10,000 个限制；请先简化网格。")
                elif parts[0] == "usemtl":
                    current_material = " ".join(parts[1:]) if len(parts) > 1 else None
                    if current_material is not None and (len(current_material) > 128
                                                         or any(ord(char) < 32 for char in current_material)):
                        raise ToolError(f"OBJ 第 {line_number} 行的 usemtl 名称无效或过长。")
                elif parts[0] == "f":
                    indexes = []
                    for item in parts[1:]:
                        raw = item.split("/", 1)[0]
                        index = int(raw)
                        if index == 0:
                            raise ToolError(f"OBJ 第 {line_number} 行包含非法的 0 起始顶点索引。")
                        index = index - 1 if index > 0 else len(source_vertices) + index
                        if index < 0 or index >= len(source_vertices):
                            raise ToolError(f"OBJ 第 {line_number} 行的面引用了不存在的顶点。")
                        indexes.append(index + 1)
                    add_polygon(indexes, current_material)
        except (UnicodeError, ValueError, OverflowError) as exc:
            raise ToolError(f"无法解析 OBJ 网格：{exc}") from None
        used = sorted({index for face in faces for index in face})
        remap = {old: new for new, old in enumerate(used, 1)}
        vertices = [source_vertices[index - 1] for index in used]
        faces = [tuple(remap[index] for index in face) for face in faces]
    else:
        raw = path.read_bytes()
        triangles = []
        if len(raw) >= 84:
            count = struct.unpack_from("<I", raw, 80)[0]
            if 84 + count * 50 == len(raw):
                for offset in range(84, len(raw), 50):
                    values = struct.unpack_from("<12fH", raw, offset)
                    triangles.append((values[3:6], values[6:9], values[9:12]))
        if not triangles:
            try:
                text = raw.decode("ascii")
                current = []
                for line in text.splitlines():
                    parts = line.strip().split()
                    if parts and parts[0].lower() == "vertex":
                        if len(parts) != 4:
                            raise ToolError("ASCII STL vertex 必须包含 3 个坐标。")
                        current.append(tuple(float(v) for v in parts[1:]))
                        if len(current) == 3:
                            triangles.append(tuple(current))
                            current = []
                if current:
                    raise ToolError("ASCII STL 的 facet 顶点数量不是 3 的倍数。")
            except (UnicodeError, ValueError, OverflowError):
                raise ToolError("无法识别为有效的二进制或 ASCII STL 网格。") from None
        if not triangles or len(triangles) > 20000:
            raise ToolError("STL 网格为空或超过 20,000 个三角面限制。")
        deduplicated = {}
        for triangle in triangles:
            face = []
            for point in triangle:
                key = tuple(float(v) for v in point)
                if len(key) != 3 or not all(math.isfinite(v) for v in key):
                    raise ToolError("STL 网格包含非有限或非三维顶点。")
                if key not in deduplicated:
                    deduplicated[key] = len(vertices) + 1
                    vertices.append(key)
                face.append(deduplicated[key])
            faces.append(tuple(face))
            face_materials.append(None)
        if len(vertices) > 10000:
            raise ToolError("CAD 网格顶点超过 10,000 个限制；请先简化网格。")

    if not vertices or not faces or len(vertices) > 10000:
        raise ToolError("网格没有有效三角面，或顶点超过 10,000 个限制。")
    # Closed, consistently wound manifold is a useful minimum for an imported solid.
    edges = {}
    signed_volume = 0.0
    origin = vertices[0]
    for a, b, c in faces:
        if len({a, b, c}) != 3:
            raise ToolError("CAD 网格包含重复顶点的退化三角面。")
        pa, pb, pc = (vertices[index - 1] for index in (a, b, c))
        cross = ((pb[1]-pa[1])*(pc[2]-pa[2])-(pb[2]-pa[2])*(pc[1]-pa[1]),
                 (pb[2]-pa[2])*(pc[0]-pa[0])-(pb[0]-pa[0])*(pc[2]-pa[2]),
                 (pb[0]-pa[0])*(pc[1]-pa[1])-(pb[1]-pa[1])*(pc[0]-pa[0]))
        if sum(value * value for value in cross) <= 1e-24:
            raise ToolError("CAD 网格包含面积为零的退化三角面。")
        pa0, pb0, pc0 = (tuple(point[axis] - origin[axis] for axis in range(3))
                         for point in (pa, pb, pc))
        signed_volume += (pa0[0] * (pb0[1] * pc0[2] - pb0[2] * pc0[1])
                          + pa0[1] * (pb0[2] * pc0[0] - pb0[0] * pc0[2])
                          + pa0[2] * (pb0[0] * pc0[1] - pb0[1] * pc0[0])) / 6.0
        for start, end in ((a, b), (b, c), (c, a)):
            key = (min(start, end), max(start, end))
            direction = 1 if start < end else -1
            count, winding = edges.get(key, (0, 0))
            edges[key] = (count + 1, winding + direction)
    if any(count != 2 or winding != 0 for count, winding in edges.values()):
        raise ToolError("CAD 网格必须是闭合、边流形且面绕序一致的三角面实体；请在 CAD/BIM 软件中修复后导出 OBJ/STL。")
    if not math.isfinite(signed_volume) or abs(signed_volume) < 1e-18:
        raise ToolError("CAD 网格包围体积无效或为零。")
    if signed_volume < 0:
        faces = [(a, c, b) for a, b, c in faces]
    return vertices, faces, face_materials


def _typed_fds_value(value, original, field: str) -> tuple[str, object]:
    """Format a JSON primitive/list while preserving the existing FDS value type."""
    import math

    def scalar(new, old):
        if isinstance(old, bool):
            if not isinstance(new, bool):
                raise ToolError(f"{field} 原值是布尔值，新值也必须是布尔值。")
            return (".TRUE." if new else ".FALSE."), new
        if isinstance(old, (int, float)) and not isinstance(old, bool):
            if isinstance(new, bool) or not isinstance(new, (int, float)) or not math.isfinite(new):
                raise ToolError(f"{field} 原值是数值，新值必须是有限数值。")
            numeric = int(new) if isinstance(old, int) and float(new).is_integer() else float(new)
            return repr(numeric), numeric
        if isinstance(old, str):
            if (not isinstance(new, str) or any(ord(char) < 32 for char in new)
                    or len(new) > 256):
                raise ToolError(f"{field} 原值是文本；新值必须是最多 256 字符且不含控制字符的文本。")
            return "'" + new.replace("'", "''") + "'", new
        raise ToolError(f"{field} 的现有 FDS 值类型暂不支持。")

    if isinstance(original, list):
        if not isinstance(value, list) or len(value) != len(original) or not original:
            raise ToolError(f"{field} 必须保持为长度 {len(original)} 的数组。")
        values = [scalar(item, before) for item, before in zip(value, original)]
        if any("'" in item[0] or item[0] in {".TRUE.", ".FALSE."} for item in values):
            raise ToolError(f"{field} 的数组类型不支持字符串或布尔值。")
        return ",".join(item[0] for item in values), [item[1] for item in values]
    return scalar(value, original)


def _new_fds_value(value, field: str) -> tuple[str, object]:
    """Serialize a primitive/list safely; never accept raw FDS expressions."""
    import math

    if isinstance(value, bool):
        return (".TRUE." if value else ".FALSE."), value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ToolError(f"{field} 必须是有限数值。")
        return repr(value), value
    if isinstance(value, str):
        if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise ToolError(f"{field} 文本必须为 1–256 字符且不含控制字符。")
        return "'" + value.replace("'", "''") + "'", value
    if isinstance(value, list):
        if not value or len(value) > 64 or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
                for item in value):
            raise ToolError(f"{field} 新增数组只支持 1–64 个有限数值。")
        return ",".join(repr(item) for item in value), value
    raise ToolError(f"{field} 只支持数值、布尔、文本或有限数值数组。")


def _replace_existing_namelist_field(body: str, field: str, replacement: str) -> str:
    """Replace one existing simple field value, preserving its surrounding record text."""
    assignments = []
    quote = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "!":
            newline = body.find("\n", i)
            i = len(body) if newline < 0 else newline + 1
            continue
        candidate = FIELD_START_RE.match(body, i)
        if candidate:
            assignments.append((re.sub(r"\s+", "", candidate.group(1)).upper(),
                                candidate.start(), candidate.end()))
            i = candidate.end()
            continue
        i += 1
    matches = [(index, item) for index, item in enumerate(assignments) if item[0] == field]
    if len(matches) != 1:
        raise ToolError(f"目标 FDS 记录中的 {field} 字段出现 {len(matches)} 次，必须恰好出现一次。")
    index, (_, _, value_start) = matches[0]
    limit = assignments[index + 1][1] if index + 1 < len(assignments) else len(body)
    while value_start < limit and body[value_start].isspace():
        value_start += 1
    quote = None
    comment = False
    last_data = None
    i = value_start
    while i < limit:
        ch = body[i]
        if comment:
            if ch == "\n":
                comment = False
            i += 1
            continue
        if quote:
            if ch == quote:
                if i + 1 < limit and body[i + 1] == quote:
                    i += 2
                    last_data = i
                    continue
                quote = None
            i += 1
            last_data = i
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            last_data = i
            continue
        if ch == "!":
            comment = True
            i += 1
            continue
        if not ch.isspace() and ch != ",":
            i += 1
            last_data = i
            continue
        i += 1
    if last_data is None:
        raise ToolError(f"目标 FDS 字段 {field} 没有可替换的值。")
    return body[:value_start] + replacement + body[last_data:]


def edit_boundary_fields(config: dict, source_id: str, new_case_id: str,
                         record_type: str, target_index: int | None, updates: dict,
                         target_id: str | None = None) -> dict:
    """Add/update typed simple fields on one SURF or VENT in a new case copy.

    Field names and FDS source are never accepted from arbitrary text. Existing
    simple fields keep their type; new fields use only JSON primitive values.
    """
    import math

    if record_type not in {"SURF", "VENT"}:
        raise ToolError("record_type 仅支持 SURF 或 VENT。")
    if target_index is not None and target_id is not None:
        raise ToolError("target_index 与 target_id 只能提供其中一个。")
    if target_id is None and (isinstance(target_index, bool) or not isinstance(target_index, int) or target_index < 1):
        raise ToolError("必须提供 target_id 或 inspect_case 中同类型记录的 1 起始 target_index。")
    if target_id is not None and (not isinstance(target_id, str) or not target_id or len(target_id) > 128
                                  or any(ord(char) < 32 for char in target_id)):
        raise ToolError("target_id 必须是 1–128 个字符且不含控制字符。")
    if not isinstance(updates, dict) or not updates or len(updates) > 20:
        raise ToolError("updates 必须包含 1–20 个 FDS 字段。")
    normalized_fields = {}
    for field, value in updates.items():
        if (not isinstance(field, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:\([1-9][0-9]{0,2}\))?", field)):
            raise ToolError(f"不支持字段名 {field!r}；ID 和 GEOM 网格拓扑不能通过此工具修改。")
        field = field.upper()
        field_base = field.split("(", 1)[0]
        if field_base in {"ID", "VERTS", "FACES"} or ("(" in field and field_base in {"XB", "SURF_ID"}):
            raise ToolError(f"不支持修改保护字段 {field!r}。")
        normalized_fields[field] = value

    source = _case_file(config, source_id)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_text = source.read_text(encoding="utf-8-sig")
    original_matches = list(RECORD_RE.finditer(original_text))
    original_records = [{"record": m.group(1).upper(), "fields": _parse_namelist_fields(m.group(2))}
                        for m in original_matches]
    candidates = [index for index, item in enumerate(original_records) if item["record"] == record_type]
    if target_id is not None:
        matching = [index for index in candidates
                    if original_records[index]["fields"].get("ID") == target_id]
        if len(matching) != 1:
            raise ToolError(f"target_id 必须唯一匹配一条 &{record_type}.ID；匹配数量：{len(matching)}。")
        target_record_index = matching[0]
        target_index = candidates.index(target_record_index) + 1
    else:
        if target_index > len(candidates):
            raise ToolError(f"源算例只有 {len(candidates)} 条 &{record_type}，target_index={target_index} 不存在。")
        target_record_index = candidates[target_index - 1]
    target_before = original_records[target_record_index]
    target_original_body = original_matches[target_record_index].group(2)
    prepared = {}
    for field, value in normalized_fields.items():
        field_exists = field in target_before["fields"]
        if (not field_exists and re.search(rf"\b{re.escape(field)}\s*\(", target_original_body, re.I)):
            raise ToolError(f"字段 {field} 在目标记录中以索引数组形式出现；此工具不修改索引式字段。")
        field_base = field.split("(", 1)[0]
        if field_base == "XB" and record_type != "VENT":
            raise ToolError("此工具只允许编辑 VENT.XB；实体 OBST.XB 使用专用几何工具。")
        if field_base == "SURF_ID" and record_type != "VENT":
            raise ToolError("SURF_ID 只能在 VENT 上设置；SURF 记录不能通过此字段建立引用。")
        if field_base == "XB" and not field_exists:
            raise ToolError("目标 VENT 当前没有 XB；请使用专用通风口工具创建几何，不在此处混加字段。")
        if field_base == "XB" and record_type == "VENT":
            if not isinstance(value, list) or len(value) != 6 or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
                raise ToolError("VENT.XB 必须是 6 个有限数值。")
            if any(value[i] >= value[i + 1] for i in (0, 2, 4)) or sum(
                    value[i] == value[i + 1] for i in (0, 2, 4)) != 1:
                raise ToolError("VENT.XB 必须恰好有一个轴厚度为零，另外两轴宽度必须大于零。")
            meshes = [r["fields"].get("XB") for r in original_records if r["record"] == "MESH"]
            valid_meshes = [mesh for mesh in meshes if isinstance(mesh, list) and len(mesh) == 6
                            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                    and math.isfinite(v) for v in mesh)
                            and all(mesh[axis] < mesh[axis + 1] for axis in (0, 2, 4))]
            if not any(all(mesh[axis] <= value[axis] and value[axis + 1] <= mesh[axis + 1]
                           for axis in (0, 2, 4)) for mesh in valid_meshes):
                raise ToolError("VENT.XB 必须完整位于至少一个源 MESH.XB 内。")
        prepared[field] = (_typed_fds_value(value, target_before["fields"][field], field)
                           if field_exists else _new_fds_value(value, field))

    created = None
    try:
        created = create_case(config, source_id, new_case_id)
        destination = Path(created["file"])
        text = destination.read_text(encoding="utf-8-sig")
        matches = list(RECORD_RE.finditer(text))
        target_match = matches[target_record_index]
        body = target_match.group(2)
        for field, (formatted, _) in prepared.items():
            if field in target_before["fields"]:
                body = _replace_existing_namelist_field(body, field, formatted)
            else:
                body = _replace_or_append_scalar_field(body, field, formatted)
        text = text[:target_match.start(2)] + body + text[target_match.end(2):]
        destination.write_text(text, encoding="utf-8")
        readback = inspect_case(config, created["case_id"])
        target_after = readback["records"][target_record_index]
        if target_after["record"] != record_type:
            raise ToolError("副本中的目标记录顺序发生变化；已回滚。")
        changes = []
        for field, (formatted, expected) in prepared.items():
            actual = target_after["fields"].get(field)
            if actual != expected:
                raise ToolError(f"{field} 写入回读不一致：期望 {expected!r}，实际 {actual!r}；已回滚。")
            changes.append({"field": field, "before": target_before["fields"].get(field), "after": actual,
                            "action": "replaced" if field in target_before["fields"] else "added"})
        if set(target_after["fields"]) != set(target_before["fields"]) | set(prepared):
            raise ToolError("目标 FDS 记录字段集合与请求不一致；已回滚。")
        if record_type == "VENT" and any(field.split("(", 1)[0] == "SURF_ID" for field in prepared):
            ref = target_after["fields"].get("SURF_ID")
            if (not isinstance(ref, str) or (ref.upper() != "OPEN" and sum(
                    1 for record in original_records
                    if record["record"] == "SURF" and record["fields"].get("ID") == ref) != 1)):
                raise ToolError("VENT.SURF_ID 必须精确引用源算例中唯一一条现有 &SURF.ID；已回滚。")
        expected_records = len(original_records)
        if len(readback["records"]) != expected_records:
            raise ToolError("更新边界字段时 FDS 记录数量变化；已回滚。")
        new_matches = list(RECORD_RE.finditer(destination.read_text(encoding="utf-8-sig")))
        if len(new_matches) != len(original_matches):
            raise ToolError("保存后 FDS 记录边界发生变化；已回滚。")
        for index, (before, after) in enumerate(zip(original_records, readback["records"])):
            if index == target_record_index:
                for key, value in before["fields"].items():
                    if key not in prepared and after["fields"].get(key) != value:
                        raise ToolError(f"目标 &{record_type} 的非目标字段 {key} 改变；已回滚。")
                continue
            if before["record"] == "HEAD":
                before_fields, after_fields = dict(before["fields"]), dict(after["fields"])
                before_fields.pop("CHID", None)
                after_fields.pop("CHID", None)
                if before_fields != after_fields:
                    raise ToolError("副本中 HEAD.CHID 以外字段被意外改变；已回滚。")
            elif before["fields"] != after["fields"]:
                raise ToolError(f"副本中的 &{before['record']} 非目标记录被意外改变；已回滚。")
            if index != target_record_index and before["record"] != "HEAD":
                if original_matches[index].group(0) != new_matches[index].group(0):
                    raise ToolError(f"副本中的非目标 &{before['record']} 文本被意外改变；已回滚。")
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise ToolError("修改期间源算例发生变化；已回滚。")
        return {"case_id": created["case_id"], "source_id": source_id, "file": created["file"],
                "record_type": record_type, "target_index": target_index, "target_id": target_id,
                "updated": changes,
                "source_sha256": source_hash, "source_unchanged": True,
                "sha256": readback["sha256"], "readback_verified": True,
                "note": "只在新副本中修改 SURF/VENT 字段，允许新增类型明确的数值、布尔、文本或数值数组，但不接受原始 FDS 代码，也不修改 ID/GEOM 拓扑。已有字段保持类型/数组长度；VENT.XB 会校验平面形状/网格范围，VENT.SURF_ID 会校验引用。工具不会验证字段名是否属于当前 FDS 版本，也未穷举字段物理范围；必须执行匹配版本 FDS 的输入检查并由工程师复核，未核验前不要宣称可求解。"}
    except Exception:
        if created is not None:
            Path(created["file"]).unlink(missing_ok=True)
        raise


def add_geometry_mesh_to_case(config: dict, source_id: str, new_case_id: str,
                              mesh_file: str, geometry_id: str, surface_id: str,
                              scale: float = 1.0, translation: list[float] | None = None,
                              material_surface_map: dict[str, str] | None = None) -> dict:
    """Import one closed OBJ/STL solid into FDS GEOM on a new case copy."""
    import math

    root = case_root(config).resolve()
    if (not isinstance(mesh_file, str) or Path(mesh_file).is_absolute()
            or Path(mesh_file).name != mesh_file or mesh_file in {".", ".."}):
        raise ToolError("mesh_file 必须是算例工作区根目录中 OBJ/STL 文件名。")
    asset_path = (root / mesh_file).resolve()
    if asset_path.parent != root or not asset_path.is_file() or asset_path.suffix.lower() not in {".obj", ".stl"}:
        raise ToolError("仅支持算例工作区根目录中的 .obj 或 .stl 文件，禁止路径越界。")
    if not isinstance(geometry_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", geometry_id):
        raise ToolError("geometry_id 只能以字母开头，并包含最多 32 个字母、数字、_ 或 -。")
    if not isinstance(surface_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", surface_id):
        raise ToolError("surface_id 只能以字母开头，并包含最多 32 个字母、数字、_ 或 -。")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0 or scale > 10000:
        raise ToolError("scale 必须是 (0, 10000] 范围内的有限正数。")
    if translation is None:
        translation = [0.0, 0.0, 0.0]
    if (not isinstance(translation, list) or len(translation) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in translation)):
        raise ToolError("translation 必须是 3 个有限数值组成的数组，单位 m。")

    mesh_sha = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    vertices, faces, face_materials = _parse_surface_mesh(asset_path)
    if material_surface_map is not None:
        if (not isinstance(material_surface_map, dict) or not material_surface_map
                or len(material_surface_map) > 32):
            raise ToolError("material_surface_map 必须是 1–32 项的 OBJ usemtl 名称到 FDS SURF.ID 映射。")
        for material, mapped_surface in material_surface_map.items():
            if (not isinstance(material, str) or not material or len(material) > 128
                    or any(ord(char) < 32 for char in material)):
                raise ToolError("material_surface_map 中的 OBJ 材质名无效。")
            if not isinstance(mapped_surface, str) or not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_-]{0,31}", mapped_surface):
                raise ToolError("材质映射的 SURF.ID 必须以字母开头，且仅含字母、数字、_ 或 -（最多 32 字符）。")
        used_materials = {material for material in face_materials if material is not None}
        unmapped = sorted(used_materials - set(material_surface_map))
        unused = sorted(set(material_surface_map) - used_materials)
        if unmapped:
            raise ToolError(f"OBJ 中这些 usemtl 名称没有映射到 FDS SURF.ID：{', '.join(unmapped)}。")
        if unused:
            raise ToolError(f"映射包含 OBJ 中未使用的材质名：{', '.join(unused)}。")
    elif any(material is not None for material in face_materials):
        # Backward-compatible single-surface mode deliberately applies the
        # provided surface_id to every face, even when the OBJ has usemtl tags.
        material_surface_map = {}
    else:
        material_surface_map = {}
    transformed = [tuple(float(f"{float(value) * float(scale) + float(translation[axis]):.12g}")
                         for axis, value in enumerate(vertex)) for vertex in vertices]
    source_path = _case_file(config, source_id)
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_text = source_path.read_text(encoding="utf-8-sig")
    source_matches = list(RECORD_RE.finditer(source_text))
    source_records = [{"record": m.group(1).upper(), "fields": _parse_namelist_fields(m.group(2))} for m in source_matches]
    surface_ids = [surface_id]
    for material in face_materials:
        if material is not None and material in material_surface_map:
            mapped_id = material_surface_map[material]
            if mapped_id not in surface_ids:
                surface_ids.append(mapped_id)
    for selected_surface_id in surface_ids:
        surfaces = [r for r in source_records if r["record"] == "SURF"
                    and r["fields"].get("ID") == selected_surface_id]
        if len(surfaces) != 1:
            raise ToolError(f"surface_id {selected_surface_id!r} 必须唯一匹配现有 &SURF.ID；匹配数量：{len(surfaces)}。")
    if any(r["record"] == "GEOM" and r["fields"].get("ID") == geometry_id for r in source_records):
        raise ToolError(f"源算例已存在 geometry_id={geometry_id!r}。")
    meshes = [r["fields"].get("XB") for r in source_records if r["record"] == "MESH"]
    if not meshes:
        raise ToolError("源算例没有 &MESH，无法验证 CAD 网格位置。")
    for mesh in meshes:
        if (not isinstance(mesh, list) or len(mesh) != 6
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in mesh)
                or any(mesh[i] >= mesh[i + 1] for i in (0, 2, 4))):
            raise ToolError("源算例含缺失/无效的 MESH.XB；无法安全导入 CAD 网格。")
    mesh_boxes = [[float(v) for v in mesh] for mesh in meshes]
    for face in faces:
        contained = any(
            all(all(box[box_axis] <= transformed[index - 1][coordinate_axis] <= box[box_axis + 1]
                    for coordinate_axis, box_axis in enumerate((0, 2, 4))) for index in face)
            for box in mesh_boxes
        )
        if not contained:
            raise ToolError("导入网格的每个三角面都必须完整位于同一个源 MESH.XB 内。")

    vert_rows = [", ".join(f"{value:.12g}" for value in vertex) for vertex in transformed]
    face_surface_indexes = [surface_ids.index(material_surface_map[material]) + 1
                            if material is not None and material in material_surface_map else 1
                            for material in face_materials]
    face_rows = [", ".join(str(value) for value in (*face, surface_index))
                 for face, surface_index in zip(faces, face_surface_indexes)]
    surface_text = ",".join(f"'{value}'" for value in surface_ids)
    geom_text = (f"&GEOM ID='{geometry_id}', SURF_ID={surface_text}, VERTS=\n"
                 + ",\n".join(vert_rows) + ",\nFACES=\n"
                 + ",\n".join(face_rows) + " /\n")
    created = None
    try:
        created = create_case(config, source_id, new_case_id)
        dest = Path(created["file"])
        text = dest.read_text(encoding="utf-8-sig")
        tail = re.search(r"(?im)^\s*&TAIL\b", text)
        text = (text[:tail.start()] + geom_text + "\n" + text[tail.start():]) if tail else text.rstrip() + "\n" + geom_text + "\n"
        dest.write_text(text, encoding="utf-8")
        readback = inspect_case(config, new_case_id)
        new_matches = list(RECORD_RE.finditer(dest.read_text(encoding="utf-8-sig")))
        source_tail_index = next((i for i, record in enumerate(source_records)
                                  if record["record"] == "TAIL"), len(source_records))
        geom_index = source_tail_index
        geoms = [r for r in readback["records"] if r["record"] == "GEOM" and r["fields"].get("ID") == geometry_id]
        source_geom_count = sum(1 for r in source_records if r["record"] == "GEOM")
        expected_surface_value = surface_ids if len(surface_ids) > 1 else surface_id
        if (len(geoms) != 1 or geoms[0]["fields"].get("SURF_ID") != expected_surface_value
                or geoms[0]["fields"].get("VERTS") != [v for vertex in transformed for v in vertex]
                or geoms[0]["fields"].get("FACES") != [v for face, surface_index
                                                          in zip(faces, face_surface_indexes)
                                                          for v in (*face, surface_index)]
                or sum(1 for r in readback["records"] if r["record"] == "GEOM") != source_geom_count + 1
                or len(readback["records"]) != len(source_records) + 1
                or len(new_matches) != len(source_matches) + 1
                or readback["records"][geom_index]["record"] != "GEOM"
                or readback["records"][geom_index]["fields"].get("ID") != geometry_id):
            raise ToolError("CAD 网格写入后回读不一致，已删除新副本。")
        for index, before in enumerate(source_records):
            after_index = index if index < geom_index else index + 1
            after = readback["records"][after_index]
            if before["record"] == "HEAD":
                old_fields, new_fields = dict(before["fields"]), dict(after["fields"])
                old_fields.pop("CHID", None)
                new_fields.pop("CHID", None)
                if old_fields != new_fields:
                    raise ToolError("CAD 导入时副本中 HEAD.CHID 以外字段被改变；已回滚。")
                continue
            if before["fields"] != after["fields"]:
                raise ToolError(f"CAD 导入时副本中的非目标 &{before['record']} 字段被改变；已回滚。")
            if source_matches[index].group(0) != new_matches[after_index].group(0):
                raise ToolError(f"CAD 导入时副本中的非目标 &{before['record']} 文本被改变；已回滚。")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha:
            raise ToolError("导入期间源算例发生变化，已删除新副本。")
        if hashlib.sha256(asset_path.read_bytes()).hexdigest() != mesh_sha:
            raise ToolError("CAD 网格在导入期间发生变化，已删除新副本。")
        return {"case_id": new_case_id, "source_id": source_id, "file": created["file"],
                "source_sha256": source_sha, "source_unchanged": True,
                "mesh_file": str(asset_path), "mesh_sha256": mesh_sha,
                "format": asset_path.suffix.lower(), "geometry_id": geometry_id,
                "surface_ids": surface_ids,
                "material_surface_map": material_surface_map,
                "vertices": len(transformed), "triangles": len(faces), "scale": float(scale),
                "translation": translation, "readback_verified": True,
                "note": "已把闭合三角网格写为 FDS &GEOM VERTS/FACES 并在副本中回读。未检查自交、碰撞、材料适用性或网格质量；OBJ 材质组/IFC 构件属性不保留。需执行 FDS preflight 并由工程师检查。"}
    except Exception:
        if created is not None:
            Path(created["file"]).unlink(missing_ok=True)
        raise


def list_geometry_assets(config: dict) -> dict:
    """List direct-workspace OBJ/STL files that can be imported by the mesh tool."""
    root = case_root(config).resolve()
    assets = []
    candidates = {path.resolve() for pattern in ("*.obj", "*.stl") for path in root.glob(pattern)}
    for resolved in sorted(candidates, key=lambda item: item.name.casefold()):
        if resolved.parent != root or not resolved.is_file():
            continue
        size = resolved.stat().st_size
        assets.append({"mesh_file": resolved.name, "format": resolved.suffix.lower(),
                       "bytes": size, "within_size_limit": size <= 50 * 1024 * 1024})
        if len(assets) >= 200:
            break
    return {"case_root": str(root), "assets": assets,
            "note": "只列工作区根目录中最多 200 个 OBJ/STL；不读取网格几何、不递归子目录。"}


def inspect_geometry_asset(config: dict, mesh_file: str) -> dict:
    """Inspect a workspace mesh and report its material groups and geometric bounds."""
    import math

    root = case_root(config).resolve()
    if (not isinstance(mesh_file, str) or Path(mesh_file).is_absolute()
            or Path(mesh_file).name != mesh_file or mesh_file in {".", ".."}):
        raise ToolError("mesh_file 必须是算例工作区根目录中的 OBJ/STL 文件名。")
    asset_path = (root / mesh_file).resolve()
    if (asset_path.parent != root or not asset_path.is_file()
            or asset_path.suffix.lower() not in {".obj", ".stl"}):
        raise ToolError("仅支持算例工作区根目录中的 .obj 或 .stl 文件，禁止路径越界。")
    vertices, faces, face_materials = _parse_surface_mesh(asset_path)
    bounds = [[min(vertex[axis] for vertex in vertices), max(vertex[axis] for vertex in vertices)]
              for axis in range(3)]
    spans = [upper - lower for lower, upper in bounds]
    if not all(math.isfinite(value) for value in spans):
        raise ToolError("网格包围盒无效。")
    material_counts = {}
    for material in face_materials:
        if material is not None:
            material_counts[material] = material_counts.get(material, 0) + 1
    return {"mesh_file": asset_path.name, "format": asset_path.suffix.lower(),
            "bytes": asset_path.stat().st_size,
            "sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
            "vertices": len(vertices), "triangles": len(faces), "closed_manifold": True,
            "bounds_xyz": bounds, "material_groups": [
                {"name": name, "triangles": count} for name, count in sorted(material_counts.items())],
            "unassigned_triangles": sum(1 for material in face_materials if material is None),
            "note": "闭合性仅指边流形和面绕序检查通过；不会读取/套用 MTL 参数、IFC 属性，也不检查自交、碰撞或网格分辨率。"}


def add_vent_to_case(config: dict, source_id: str, new_case_id: str, xb: list[float],
                     surface_id: str) -> dict:
    """Clone a case and append one XB+SURF_ID-only axis-aligned &VENT.

    The edit surface is intentionally narrow: the record contains only six
    numeric plane bounds and a reference to one existing SURF ID. Exactly one
    axis must have zero thickness; the plane must fit inside one source MESH.XB
    domain. No arbitrary fields or FDS text are accepted, and any failure
    removes only the copy made by this call.
    """
    import math

    invalid_xb = "xb 必须是 6 个有限数值：[xmin,xmax,ymin,ymax,zmin,zmax]。"
    if not isinstance(xb, list) or len(xb) != 6:
        raise ToolError(invalid_xb)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in xb):
        raise ToolError(invalid_xb)
    try:
        coords = [float(value) for value in xb]
    except (OverflowError, ValueError):
        raise ToolError(invalid_xb) from None
    if not all(math.isfinite(value) for value in coords):
        raise ToolError(invalid_xb)
    if any(coords[i] > coords[i + 1] for i in (0, 2, 4)):
        raise ToolError("xb 每个轴的最小值都必须小于等于最大值。")
    zero_axes = [i for i in (0, 2, 4) if coords[i] == coords[i + 1]]
    if len(zero_axes) != 1:
        raise ToolError("xb 必须恰好在一个轴上厚度为零，另外两个轴的宽度必须大于零。")

    if not isinstance(surface_id, str) or not surface_id or len(surface_id) > 128:
        raise ToolError("surface_id 必须是非空字符串，用于精确匹配已有 &SURF.ID。")
    if any(char in surface_id for char in "'\"/,!") or any(ord(char) < 32 for char in surface_id):
        raise ToolError("surface_id 含有无法安全写入 FDS 记录的字符。")

    new_case_id = _case_id(new_case_id)
    source_path = _case_file(config, source_id)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_text = source_path.read_text(encoding="utf-8-sig")
    source_match_objects = list(RECORD_RE.finditer(source_text))
    source_records = [{"record": match.group(1).upper(),
                       "fields": _parse_namelist_fields(match.group(2))}
                      for match in source_match_objects]

    matching_surfaces = [record for record in source_records
                         if record["record"] == "SURF" and record["fields"].get("ID") == surface_id]
    if not matching_surfaces:
        raise ToolError(f"源算例中找不到 ID 为 {surface_id!r} 的 &SURF 记录。")
    if len(matching_surfaces) > 1:
        raise ToolError(f"源算例中存在 {len(matching_surfaces)} 条 ID 为 {surface_id!r} 的 &SURF 记录，"
                        "无法确定唯一引用。")

    source_meshes = [record["fields"] for record in source_records if record["record"] == "MESH"]
    if not source_meshes:
        raise ToolError("源算例没有 &MESH 记录，无法验证通风平面是否位于计算域内。")
    mesh_domains = []
    for index, fields in enumerate(source_meshes, 1):
        mesh_xb = fields.get("XB")
        valid = (isinstance(mesh_xb, list) and len(mesh_xb) == 6
                 and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
                         for v in mesh_xb)
                 and all(float(mesh_xb[i]) < float(mesh_xb[i + 1]) for i in (0, 2, 4)))
        if not valid:
            raise ToolError(f"源算例第 {index} 个 &MESH 的 XB 缺失或无效，无法安全验证通风平面范围。")
        mesh_domains.append([float(v) for v in mesh_xb])
    containing_meshes = [index for index, domain in enumerate(mesh_domains, 1)
                         if all(domain[i] <= coords[i] and coords[i + 1] <= domain[i + 1] for i in (0, 2, 4))]
    if not containing_meshes:
        raise ToolError("通风平面必须完整位于至少一个源 &MESH.XB 范围内（边界可重合）。")

    destination = case_root(config) / f"{new_case_id}.fds"
    if destination.exists():
        raise ToolError("目标算例已存在；请使用新的 case_id。")
    created = None
    try:
        created = create_case(config, source_id, new_case_id)
        destination = Path(created["file"])
        text = destination.read_text(encoding="utf-8-sig")
        record_text = ("&VENT XB=" + ",".join(repr(value) for value in coords)
                       + f", SURF_ID='{surface_id}' /")
        tail = re.search(r"(?im)^\s*&TAIL\b", text)
        insertion = record_text + "\n"
        if tail:
            text = text[:tail.start()] + insertion + text[tail.start():]
        else:
            text = text.rstrip() + "\n" + insertion
        destination.write_text(text, encoding="utf-8")

        readback = inspect_case(config, new_case_id)
        new_matches = list(RECORD_RE.finditer(destination.read_text(encoding="utf-8-sig")))
        if len(readback["records"]) != len(source_records) + 1:
            raise ToolError("回读发现 FDS 记录数量不是源算例加一；已回滚新副本。")
        if len(new_matches) != len(source_match_objects) + 1:
            raise ToolError("回读发现 FDS 记录边界数量不是源算例加一；已回滚新副本。")

        source_tail_index = next((index for index, record in enumerate(source_records)
                                  if record["record"] == "TAIL"), len(source_records))
        appended = readback["records"][source_tail_index]
        vent_records = [record for record in readback["records"] if record["record"] == "VENT"]
        if appended["record"] != "VENT" or not vent_records or vent_records[-1] != appended:
            raise ToolError("回读发现新增 &VENT 不是最后一条 VENT 记录；已回滚新副本。")
        source_vent_count = sum(1 for record in source_records if record["record"] == "VENT")
        if len(vent_records) != source_vent_count + 1:
            raise ToolError("回读发现 &VENT 记录数量不是源算例加一；已回滚新副本。")
        if set(appended["fields"]) != {"XB", "SURF_ID"}:
            raise ToolError(f"新增 &VENT 的字段集合不是 XB 和 SURF_ID：{sorted(appended['fields'])}；已回滚新副本。")
        if appended["fields"].get("XB") != coords:
            raise ToolError(
                f"新增 &VENT 的 XB 回读不一致：{appended['fields'].get('XB')}，应为 {coords}；已回滚新副本。")
        if appended["fields"].get("SURF_ID") != surface_id:
            raise ToolError("新增 &VENT 的 SURF_ID 回读不一致；已回滚新副本。")

        for index, before in enumerate(source_records):
            readback_index = index if index < source_tail_index else index + 1
            after = readback["records"][readback_index]
            if before["record"] == "HEAD":
                before_fields = dict(before["fields"])
                after_fields = dict(after["fields"])
                before_fields.pop("CHID", None)
                after_fields.pop("CHID", None)
                if before_fields != after_fields:
                    raise ToolError("回读发现 &HEAD 的非 CHID 字段被意外改变；已回滚新副本。")
                chid_pattern = re.compile(r"(?i)(\bCHID\s*=\s*)(?:'[^']*'|\"[^\"]*\")")
                if (chid_pattern.sub(r"\1'<CHID>'", source_match_objects[index].group(0))
                        != chid_pattern.sub(r"\1'<CHID>'", new_matches[readback_index].group(0))):
                    raise ToolError("回读发现 &HEAD 的 CHID 以外文本被意外改变；已回滚新副本。")
                continue
            if before["fields"] != after["fields"]:
                raise ToolError(f"回读发现 &{before['record']} 记录字段被意外改变；已回滚新副本。")
            if source_match_objects[index].group(0) != new_matches[readback_index].group(0):
                raise ToolError(f"回读发现 &{before['record']} 记录文本被意外改变；已回滚新副本。")

        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
            raise ToolError("源算例在操作期间发生变化；已回滚新副本。")
        return {
            "case_id": new_case_id,
            "source_id": source_id,
            "file": created["file"],
            "source_sha256": source_sha256,
            "source_unchanged": True,
            "vent": {"record": "VENT", "fields": appended["fields"],
                     "record_text": record_text, "mesh_indices": containing_meshes},
            "readback": {"XB": appended["fields"]["XB"],
                         "SURF_ID": appended["fields"]["SURF_ID"],
                         "last_vent": True},
            "record_count_before": len(source_records),
            "record_count_after": len(readback["records"]),
            "sha256": readback["sha256"],
            "verified": True,
            "note": ("只追加了一条仅含 XB 和 SURF_ID 的 &VENT，引用源算例中唯一匹配的现有 &SURF；"
                     "已验证源文件未变、平面恰好一个轴厚度为零、其他两轴宽度为正、位于指定网格域内，"
                     "并回读确认新增记录是最后一条 VENT 且其他记录未被改动（仅允许复制的 HEAD.CHID 变化）。"
                     "未检查与现有几何重叠、面朝向或物理适用性；未启动求解或 PyroSim，"
                     "请继续人工审查并执行 preflight。"),
        }
    except Exception:
        # `created` is only set after create_case returns, so this removes just
        # the copy that this call made and leaves the source and other files alone.
        if created is not None:
            Path(created["file"]).unlink(missing_ok=True)
        raise


def create_case_with_obstruction_updates(config: dict, source_id: str, new_case_id: str,
                                         obstruction_id: str, xb: list[float]) -> dict:
    """Clone a case and replace only XB on one existing &OBST matched by ID.

    The edit surface is intentionally narrow: one existing OBST is selected by
    its parsed ID, only a simple existing XB value is replaced in place, and no
    other text or field is touched. Any failure removes only the copy made by
    this call.
    """
    import math

    invalid_xb = "xb 必须是 6 个有限数值：[xmin,xmax,ymin,ymax,zmin,zmax]。"
    if not isinstance(obstruction_id, str) or not obstruction_id or len(obstruction_id) > 128:
        raise ToolError("obstruction_id 必须是非空字符串，用于精确匹配已有 &OBST.ID。")
    if not isinstance(xb, list) or len(xb) != 6:
        raise ToolError(invalid_xb)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in xb):
        raise ToolError(invalid_xb)
    try:
        coords = [float(value) for value in xb]
    except (OverflowError, ValueError):
        raise ToolError(invalid_xb) from None
    if not all(math.isfinite(value) for value in coords):
        raise ToolError(invalid_xb)
    if any(coords[i] >= coords[i + 1] for i in (0, 2, 4)):
        raise ToolError("xb 每个轴的最小值都必须小于最大值。")
    new_case_id = _case_id(new_case_id)

    source_path = _case_file(config, source_id)
    source_text = source_path.read_text(encoding="utf-8-sig")
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_match_objects = list(RECORD_RE.finditer(source_text))
    source_records = [{"record": match.group(1).upper(),
                       "fields": _parse_namelist_fields(match.group(2))}
                      for match in source_match_objects]

    source_meshes = [record["fields"] for record in source_records if record["record"] == "MESH"]
    if not source_meshes:
        raise ToolError("源算例没有 &MESH 记录，无法验证障碍物是否位于计算域内。")
    mesh_domains = []
    for index, fields in enumerate(source_meshes, 1):
        mesh_xb = fields.get("XB")
        valid = (isinstance(mesh_xb, list) and len(mesh_xb) == 6
                 and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
                         for v in mesh_xb)
                 and all(float(mesh_xb[i]) < float(mesh_xb[i + 1]) for i in (0, 2, 4)))
        if not valid:
            raise ToolError(f"源算例第 {index} 个 &MESH 的 XB 缺失或无效，无法安全验证障碍物范围。")
        mesh_domains.append([float(v) for v in mesh_xb])
    containing_meshes = [index for index, domain in enumerate(mesh_domains, 1)
                         if all(domain[i] <= coords[i] and coords[i + 1] <= domain[i + 1] for i in (0, 2, 4))]
    if not containing_meshes:
        raise ToolError("障碍物必须完整位于至少一个源 &MESH.XB 范围内（边界可重合）。")

    replacement = ",".join(repr(value) for value in coords)

    def locate_xb(body: str) -> tuple[int, int]:
        tokens = _namelist_field_tokens(body)
        indexes = [index for index, token in enumerate(tokens) if token["key"] == "XB"]
        if not indexes:
            raise ToolError("目标 &OBST 没有 XB 字段；本工具只替换已有 XB，不追加字段。")
        if len(indexes) > 1:
            raise ToolError("目标 &OBST 的 XB 字段出现多次，无法确定要替换哪一个。")
        index = indexes[0]
        span = tokens[index]["value_span"]
        if span is None:
            raise ToolError("目标 &OBST 的 XB 没有可替换的值。")
        start = span[0]
        end = tokens[index + 1]["field_start"] if index + 1 < len(tokens) else _namelist_content_end(body)
        raw = body[start:end]
        comment = raw.find("!")
        if comment >= 0:
            raw = raw[:comment]
        value_text = raw.strip()
        if value_text.endswith(","):
            value_text = value_text[:-1].rstrip()
        if not value_text:
            raise ToolError("目标 &OBST 的 XB 没有可替换的值。")
        inner = value_text[1:-1].strip() if value_text.startswith("(") and value_text.endswith(")") else value_text
        parts = [part.strip() for part in inner.split(",")]
        if len(parts) != 6:
            raise ToolError("目标 &OBST 的现有 XB 不是 6 个简单数值，无法安全替换。")
        for part in parts:
            try:
                float(part)
            except ValueError:
                raise ToolError("目标 &OBST 的现有 XB 不是 6 个简单数值，无法安全替换。") from None
        return start, start + len(value_text)

    created = create_case(config, source_id, new_case_id)
    destination = Path(created["file"])
    try:
        text = destination.read_text(encoding="utf-8-sig")
        matches = list(RECORD_RE.finditer(text))
        records = [{"record": match.group(1).upper(),
                    "fields": _parse_namelist_fields(match.group(2))}
                   for match in matches]
        target_indexes = [index for index, record in enumerate(records)
                          if record["record"] == "OBST" and record["fields"].get("ID") == obstruction_id]
        if not target_indexes:
            raise ToolError(f"复制后的算例中找不到 ID 为 {obstruction_id!r} 的 &OBST 记录。")
        if len(target_indexes) > 1:
            raise ToolError(f"复制后的算例中存在 {len(target_indexes)} 条 ID 为 {obstruction_id!r} 的 &OBST 记录，"
                            "无法确定唯一目标。")
        target_index = target_indexes[0]
        if (target_index >= len(source_records)
                or source_records[target_index]["record"] != "OBST"
                or source_records[target_index]["fields"].get("ID") != obstruction_id):
            raise ToolError("源算例与复制算例的 &OBST 记录位置不一致；已回滚新副本。")

        target_match = matches[target_index]
        body = target_match.group(2)
        source_body = source_match_objects[target_index].group(2)
        if body != source_body:
            raise ToolError("复制后的目标 &OBST 文本与源算例不一致；已回滚新副本。")
        id_tokens = [token for token in _namelist_field_tokens(body) if token["key"] == "ID"]
        if len(id_tokens) != 1:
            raise ToolError("目标 &OBST 的 ID 字段缺失或重复，无法确定唯一目标；已回滚新副本。")
        start, end = locate_xb(body)
        text = text[:target_match.start(2)] + body[:start] + replacement + body[end:] + text[target_match.end(2):]
        destination.write_text(text, encoding="utf-8")

        readback = inspect_case(config, new_case_id)
        readback_targets = [record for record in readback["records"]
                            if record["record"] == "OBST" and record["fields"].get("ID") == obstruction_id]
        if len(readback_targets) != 1:
            raise ToolError(f"回读得到 {len(readback_targets)} 条 ID 为 {obstruction_id!r} 的 &OBST 记录，"
                            "无法确认唯一目标；已回滚新副本。")
        target_fields = readback_targets[0]["fields"]
        if target_fields.get("XB") != coords:
            raise ToolError(f"保存后 &OBST.XB 回读不一致：{target_fields.get('XB')}，应为 {coords}；已回滚新副本。")

        source_obst_count = sum(1 for record in source_records if record["record"] == "OBST")
        readback_obst_count = sum(1 for record in readback["records"] if record["record"] == "OBST")
        if readback_obst_count != source_obst_count:
            raise ToolError("回读发现 &OBST 记录数量发生变化；已回滚新副本。")
        if len(readback["records"]) != len(source_records):
            raise ToolError("回读发现 FDS 记录数量发生变化；已回滚新副本。")

        new_matches = list(RECORD_RE.finditer(text))
        if len(new_matches) != len(source_match_objects):
            raise ToolError("回读发现 FDS 记录边界发生变化；已回滚新副本。")
        new_body = new_matches[target_index].group(2)
        if new_body != source_body[:start] + replacement + source_body[end:]:
            raise ToolError("目标 &OBST 的 XB 以外文本被意外改变；已回滚新副本。")
        for index, (before, after) in enumerate(zip(source_records, readback["records"])):
            if index == target_index:
                if set(after["fields"]) != set(before["fields"]):
                    raise ToolError("目标 &OBST 的字段集合被意外改变；已回滚新副本。")
                for key, value in before["fields"].items():
                    if key == "XB":
                        continue
                    if after["fields"].get(key) != value:
                        raise ToolError(f"目标 &OBST 的非 XB 字段 {key} 被意外改变；已回滚新副本。")
                continue
            if before["record"] == "HEAD":
                before_fields = dict(before["fields"])
                after_fields = dict(after["fields"])
                before_fields.pop("CHID", None)
                after_fields.pop("CHID", None)
                if before_fields != after_fields:
                    raise ToolError("回读发现 &HEAD 的非 CHID 字段被意外改变；已回滚新副本。")
                continue
            if before["fields"] != after["fields"]:
                raise ToolError(f"回读发现 &{before['record']} 记录字段被意外改变；已回滚新副本。")
            if source_match_objects[index].group(0) != new_matches[index].group(0):
                raise ToolError(f"回读发现 &{before['record']} 记录文本被意外改变；已回滚新副本。")

        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
            raise ToolError("源算例在操作期间发生变化；已回滚新副本。")
        return {
            "case_id": new_case_id,
            "source_id": source_id,
            "file": created["file"],
            "source_sha256": source_sha256,
            "source_unchanged": True,
            "obstruction_id": obstruction_id,
            "obstruction": {"record": "OBST", "fields": target_fields},
            "readback": {"XB": target_fields.get("XB")},
            "mesh_indices": containing_meshes,
            "record_count_before": len(source_records),
            "record_count_after": len(readback["records"]),
            "sha256": readback["sha256"],
            "verified": True,
            "note": ("只替换了一条现有 &OBST 的 XB 值；未修改 ID、SURF 引用或其他字段，"
                     "也不接受任意 FDS 文本。已验证源文件哈希未变、其他记录未被改动并完成解析回读。"
                     "未检查与现有障碍物重叠、网格分辨率或物理适用性；未启动求解或 PyroSim，"
                     "请继续人工审查并执行 preflight。"),
        }
    except Exception:
        # `created` is only set after create_case returns, so this removes just
        # the copy that this call made and leaves the source and other files alone.
        destination.unlink(missing_ok=True)
        raise


def run_case(config: dict, case_id: str, approved: bool = False, timeout_seconds: int = 3600, *,
             source_override: Path | None = None, cancel_event: threading.Event | None = None,
             process_callback=None, progress_callback=None) -> dict:
    if approved is not True:
        raise ToolError("运行会启动本地计算；请先获得用户明确确认，再传 approved=true。")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 86400:
        raise ToolError("timeout_seconds 必须在 1–86400 之间。")
    source = _case_file(config, case_id) if source_override is None else Path(source_override).resolve()
    if not source.is_file() or source.suffix.lower() != ".fds":
        raise ToolError("求解输入必须是现存的 .fds 文件。")
    root, fds, _ = runtime_paths(config)
    if not fds.is_file():
        raise ToolError(f"找不到 FDS 可执行文件：{fds}")
    run_dir = case_root(config) / "runs" / f"{case_id}_{datetime.now():%Y%m%d_%H%M%S_%f}"
    run_dir.mkdir(parents=True, exist_ok=False)
    run_input = run_dir / source.name
    shutil.copy2(source, run_input)
    log_path = run_dir / "fds.log"
    started = time.time()
    emit_event(config, "solver_started", f"FDS 求解已启动：{case_id}", case_id=case_id,
               run_dir=str(run_dir), solver=str(fds), solver_version=_latest_fds_revision(config))
    target_time = _case_end_time(source)
    output_tail = []
    try:
        env = dict(os.environ)
        env["PATH"] = str(root / "fds") + ";" + str(root / "fds" / "mpi") + ";" + str(root) + ";" + env.get("PATH", "")
        # The bundled Intel MPI build can fail to initialize its default OFI
        # provider in restricted/local Windows network environments. For this
        # single-machine solver runner, shared-memory fabric is a safe fallback;
        # respect any administrator/user fabric selection already in the env.
        if os.name == "nt":
            env.setdefault("I_MPI_FABRICS", "shm")
        process = subprocess.Popen([str(fds), run_input.name], cwd=run_dir, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        if process_callback:
            process_callback(process)
        lines = queue.Queue()

        def collect_output():
            try:
                for output_line in process.stdout:
                    lines.put(output_line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=collect_output, name=f"fds-log-{case_id}", daemon=True)
        reader.start()
        with log_path.open("w", encoding="utf-8", errors="replace") as log_stream:
            stream_closed = False
            while True:
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    line = ""
                if line is None:
                    stream_closed = True
                elif line:
                    log_stream.write(line)
                    log_stream.flush()
                    output_tail.append(line.rstrip())
                    output_tail = output_tail[-40:]
                    _emit_solver_line(config, case_id, line.rstrip(), target_time, run_dir,
                                      progress_callback=progress_callback)
                if cancel_event and cancel_event.is_set() and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if stream_closed and process.poll() is not None:
                    break
                if time.time() - started > timeout_seconds:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired([str(fds), run_input.name], timeout_seconds,
                                                    output="\n".join(output_tail))
        exit_code = process.wait()
        cancelled = bool(cancel_event and cancel_event.is_set())
        files = [{"name": p.name, "bytes": p.stat().st_size} for p in run_dir.iterdir() if p.is_file()]
        success = not cancelled and exit_code == 0 and any(p.suffix.lower() == ".smv" for p in run_dir.iterdir())
        result = {"case_id": case_id, "run_dir": str(run_dir), "exit_code": exit_code,
                "duration_seconds": round(time.time() - started, 2), "log": str(log_path),
                "success": success, "cancelled": cancelled, "files": files, "log_tail": "\n".join(output_tail)}
        finish_message = "FDS 求解已停止" if cancelled else ("FDS 求解完成" if success else "FDS 求解失败")
        emit_event(config, "solver_finished", finish_message,
                   case_id=case_id, run_dir=str(run_dir), success=success, exit_code=exit_code,
                   cancelled=cancelled,
                   duration_seconds=result["duration_seconds"], output_files=files,
                   log=str(log_path), log_tail=result["log_tail"])
        return result
    except subprocess.TimeoutExpired as exc:
        tail = exc.stdout or "\n".join(output_tail)
        if isinstance(tail, bytes):
            tail = tail.decode(errors="replace")
        result = {"case_id": case_id, "run_dir": str(run_dir), "success": False,
                  "timeout": True, "log": str(log_path), "log_tail": "\n".join(str(tail).splitlines()[-40:])}
        emit_event(config, "solver_finished", "FDS 求解超时", case_id=case_id, run_dir=str(run_dir),
                   success=False, timeout=True, log=str(log_path), log_tail=result["log_tail"])
        return result


def _latest_fds_revision(config: dict) -> str | None:
    return inspect_installation(config).get("last_observed_fds_revision")


def _case_end_time(path: Path) -> float | None:
    try:
        details = inspect_case({"case_root": str(path.parent), **{}}, path.stem)
        for record in details["records"]:
            if record["record"] == "TIME":
                value = record["fields"].get("T_END")
                return float(value) if value is not None else None
    except (ToolError, OSError, TypeError, ValueError):
        return None
    return None


def _emit_solver_line(config: dict, case_id: str, line: str, target_time: float | None, run_dir: Path,
                      progress_callback=None):
    if not line:
        return
    step_match = re.search(r"Time Step:\s*(\d+),\s*Simulation Time:\s*([0-9.Ee+-]+)\s*s", line)
    if step_match:
        step = int(step_match.group(1))
        simulation_time = float(step_match.group(2))
        progress = min(100.0, simulation_time / target_time * 100.0) if target_time and target_time > 0 else None
        payload = {"case_id": case_id, "step": step, "simulation_time": simulation_time,
                   "target_time": target_time, "progress": round(progress, 1) if progress is not None else None,
                   "run_dir": str(run_dir)}
        emit_event(config, "solver_progress", f"FDS 第 {step} 步 · t={simulation_time:g} s",
                   **payload)
        if progress_callback:
            progress_callback(payload)
    elif re.search(r"ERROR|WARNING|STOP:|Revision\s*:", line, re.I):
        emit_event(config, "solver_output", line[:1000], case_id=case_id, run_dir=str(run_dir))


def write_audit(config: dict, operation: str, result: dict) -> None:
    root = case_root(config)
    path = root / "agent_audit.jsonl"
    entry = {"timestamp": datetime.now().astimezone().isoformat(), "operation": operation, "result": result}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

