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
FIELD_START_RE = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9_]*)\s*=", re.I)


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
            starts.append((candidate.start(1), candidate.end(), candidate.group(1).upper()))
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
    if raw.startswith(("'", '"')):
        quote = raw[0]
        end = raw.rfind(quote)
        if end > 0:
            return raw[1:end].replace(quote + quote, quote)
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1].strip()
    if "," in raw:
        return [_parse_namelist_scalar(item.strip()) for item in raw.split(",") if item.strip()]
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
            "key": candidate.group(1).upper(),
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

