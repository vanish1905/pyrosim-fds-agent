"""Minimal MCP stdio server using Python's standard library only."""
from __future__ import annotations

import json
import sys
import time
import traceback
import uuid

from .config import load_config
from . import fds_tools
from . import pyrosim_tools
from . import pyrosim_ui_bridge
from . import workflows
from . import results_tools
from . import results_ui_bridge
from . import presentation
from .events import emit_event

TOOLS = [
    {"name": "inspect_installation", "description": "只读检测 PyroSim、FDS、Smokeview、Results 路径，并返回基于已记录成功工作流的版本/能力档案；可执行文件存在不等于版本兼容已验证。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_adapter_capabilities", "description": "返回当前本机 PyroSim/FDS/theng 的适配器能力档案，区分 available、verified、experimental、unsupported 和需要版本检测的能力；不启动软件或执行仿真。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_cases", "description": "列出配置的案例工作区根目录中的 .fds 算例。只读。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stage_official_methane_flame_demo", "description": "优先从 Agent 安装包内置的官方 methane_flame_simple_2 输入副本取出，并核验固定 SHA-256 后放入工作区；仅部署包缺少该文件时才访问 FDS 官方固定 URL，禁止任意 URL。示例为 100 kW、5 s 火源、10 s 总时长、2000 cells，含温度和 HRRPUV 切片；不会启动求解。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_case", "description": "把工作区已有算例复制成新算例，不覆盖原文件，并尝试更新 CHID。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}}, "required": ["source_id", "new_case_id"]}},
    {"name": "create_case_with_updates", "description": "从现有算例创建新副本，只应用支持的 FDS 参数修改，并回读核验保存值；任何修改或核验失败都会删除本次新建的副本。自然语言应先映射为明确字段和值；当前只允许 TIME.T_END、TIME.DT、MESH.IJK。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "updates": {"type": "object", "additionalProperties": {}}}, "required": ["source_id", "new_case_id", "updates"]}},
    {"name": "create_case_with_surface_updates", "description": "从现有算例创建全新副本，只编辑由 surface_id 唯一匹配的一条现有 &SURF 的允许数值字段：HRRPUA (0–100000 kW/m²)、TMP_FRONT (0–2000 C)、MASS_FLUX (0–100 kg/m²/s)、VEL (-100–100 m/s)、THICKNESS (0–10 m)。已有标量原位替换，缺少时追加；其他记录文本和字段保持不变。缺失或重复匹配的 SURF ID 会被拒绝；保存后解析回读并校验源文件未变；任何失败只删除本次新建的副本。不创建 &SURF、不接受任意 FDS 文本、不求解、不打开 PyroSim。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "surface_id": {"type": "string", "description": "现有 &SURF.ID 的精确值；必须唯一匹配"}, "updates": {"type": "object", "additionalProperties": {"type": "number"}, "description": "非空对象；仅允许 HRRPUA、TMP_FRONT、MASS_FLUX、VEL、THICKNESS"}}, "required": ["source_id", "new_case_id", "surface_id", "updates"]}},
    {"name": "create_case_with_burner_obstruction", "description": "从现有案例创建全新副本，并加入一个轴对齐矩形燃烧障碍物 (&OBST) 与关联燃烧表面 (&SURF, HRRPUA)；坐标必须在某个 MESH.XB 范围内，ID 唯一。保存后解析回读核对几何、表面引用和 HRRPUA；不覆盖源文件、不求解。工具不检测几何重叠，必须继续人工审查并执行 preflight。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "obstruction_id": {"type": "string"}, "surface_id": {"type": "string"}, "xb": {"type": "array", "items": {"type": "number"}, "minItems": 6, "maxItems": 6, "description": "[xmin,xmax,ymin,ymax,zmin,zmax]，单位 m"}, "hrrpua_kw_m2": {"type": "number", "exclusiveMinimum": 0, "maximum": 100000}}, "required": ["source_id", "new_case_id", "obstruction_id", "surface_id", "xb", "hrrpua_kw_m2"]}},
    {"name": "add_obstruction_to_case", "description": "从现有算例创建全新副本，仅追加一条轴对齐实体障碍物 (&OBST)，只接受 XB 六个数值边界，不接受 ID、SURF 或任意 FDS 文本；障碍物必须完整位于源 MESH.XB 内。保存后回读新增 OBST.XB；任何失败都只删除本次新建的副本。不覆盖源文件、不求解，也不检查与现有几何重叠。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "xb": {"type": "array", "items": {"type": "number"}, "minItems": 6, "maxItems": 6, "description": "[xmin,xmax,ymin,ymax,zmin,zmax]，单位 m"}}, "required": ["source_id", "new_case_id", "xb"]}},
    {"name": "add_vent_to_case", "description": "从现有算例创建全新副本，仅追加一条轴对齐平面通风口 (&VENT)，记录只含 XB 六个数值边界和 SURF_ID 引用，不接受其他字段或任意 FDS 文本。XB 必须恰好一个轴厚度为零、另外两个轴宽度大于零，并完整位于至少一个有效源 MESH.XB 内；SURF_ID 必须精确匹配源算例中唯一一条现有 &SURF.ID。保存后回读确认记录数为源算例加一、最后一条 VENT 的 XB/SURF_ID 精确一致、其他记录未变（仅允许复制的 HEAD.CHID 变化）及源文件哈希未变；任何失败只删除本次新建的副本。不覆盖源文件、不求解、不打开 PyroSim，也不检查面朝向、几何重叠或物理适用性。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "xb": {"type": "array", "items": {"type": "number"}, "minItems": 6, "maxItems": 6, "description": "[xmin,xmax,ymin,ymax,zmin,zmax]，单位 m；恰好一个轴 min=max"}, "surface_id": {"type": "string", "description": "源算例中唯一匹配的现有 &SURF.ID 精确值"}}, "required": ["source_id", "new_case_id", "xb", "surface_id"]}},
    {"name": "edit_boundary_fields", "description": "从现有算例创建副本，按 target_id 精确匹配唯一 SURF/VENT.ID，或按 inspect_boundary_records 的同类型 target_index 定位。updates 可修改已有字段，也可新增语法安全的数值/布尔/文本/数值数组；支持 SPEC_ID(1)、MASS_FLUX(1)、RAMP_MF(1) 形式的正整数索引字段。不接受任意 FDS 代码，不修改 ID/VERTS/FACES。已有值保持类型；VENT.XB 校验平面及网格范围，VENT.SURF_ID 校验引用。工具不检查新字段名是否受当前 FDS 版本支持，也不穷举参数物理范围。写后回读并检查源文件未变；失败删除副本。执行 FDS 检查并由工程师复核前，不得声称输入已验证或自动求解。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "record_type": {"type": "string", "enum": ["SURF", "VENT"]}, "target_index": {"type": "integer", "minimum": 1, "description": "同类型记录在 inspect_boundary_records 输出中的 1 起始序号"}, "target_id": {"type": "string", "description": "唯一匹配目标 SURF.ID 或 VENT.ID；与 target_index 二选一"}, "updates": {"type": "object", "minProperties": 1, "maxProperties": 20, "additionalProperties": {"anyOf": [{"type": "number"}, {"type": "boolean"}, {"type": "string"}, {"type": "array", "items": {"type": "number"}}]}}}, "required": ["source_id", "new_case_id", "record_type", "updates"], "oneOf": [{"required": ["target_index"]}, {"required": ["target_id"]}]}},
    {"name": "list_geometry_assets", "description": "只读列出配置算例工作区根目录中的 OBJ/STL 文件名、格式和大小，以便调用 CAD 网格导入工具；最多返回 200 个，不递归、不读取几何内容。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_geometry_asset", "description": "安全读取工作区根目录中的单个 OBJ/STL 网格，返回闭合/绕序基础检查、顶点和三角面数量、包围盒及 OBJ usemtl 分组统计，为缩放、MESH 包含检查和边界映射提供信息；不修改文件或读取/应用 MTL、IFC 属性。", "inputSchema": {"type": "object", "properties": {"mesh_file": {"type": "string", "description": "由 list_geometry_assets 返回的工作区根目录文件名"}}, "required": ["mesh_file"]}},
    {"name": "add_geometry_mesh_to_case", "description": "从算例工作区根目录中的 OBJ/STL 导入一个闭合、绕序一致的三角网格，在新算例副本中写为 FDS &GEOM VERTS/FACES。必须唯一引用现有 &SURF.ID；可选 material_surface_map 将 OBJ usemtl 名称映射到其他现有 SURF.ID，并按面写入 FDS SURF 序号。限 10,000 顶点、20,000 三角面、50 MiB，每个三角面须完整落入一个现有 MESH.XB。OBJ 材质参数和 IFC 语义属性不导入；不直接解析 IFC/STEP，不检查自交、碰撞、网格质量或物理适用性。不接受外部任意路径/FDS文本；写后回读并校验源文件哈希；失败删除新副本。不求解、不打开 PyroSim。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "mesh_file": {"type": "string", "description": "算例工作区根目录中的 OBJ/STL 文件名"}, "geometry_id": {"type": "string"}, "surface_id": {"type": "string", "description": "唯一匹配的现有 &SURF.ID；未映射 OBJ 面的默认表面"}, "scale": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000}, "translation": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "X/Y/Z 平移，单位 m"}, "material_surface_map": {"type": "object", "minProperties": 1, "maxProperties": 32, "additionalProperties": {"type": "string"}, "description": "可选的 OBJ usemtl 名称到现有 FDS SURF.ID 的映射；必须覆盖 OBJ 中实际使用的所有材质名"}}, "required": ["source_id", "new_case_id", "mesh_file", "geometry_id", "surface_id"]}},
    {"name": "create_case_with_obstruction_updates", "description": "从现有算例创建全新副本，只把由 obstruction_id 唯一匹配的一条现有 &OBST 的 XB 替换为新的六数值边界：[xmin,xmax,ymin,ymax,zmin,zmax]，每轴最小值必须小于最大值，且必须完整位于至少一个有效源 MESH.XB 内。保留该记录的其他文本和字段；其他记录仅允许复制的 HEAD.CHID 变化。保存后解析回读并校验目标 XB、记录数量、非目标记录和源文件哈希；缺失/重复 ID、无效边界或任何校验失败都会删除本次新建的副本。不创建 &OBST、不修改 ID 或 SURF、不接受任意 FDS 文本、不求解、不打开 PyroSim，也不检查几何重叠或网格分辨率。", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "new_case_id": {"type": "string"}, "obstruction_id": {"type": "string", "description": "现有 &OBST.ID 的精确值；必须唯一匹配"}, "xb": {"type": "array", "items": {"type": "number"}, "minItems": 6, "maxItems": 6, "description": "[xmin,xmax,ymin,ymax,zmin,zmax]，单位 m"}}, "required": ["source_id", "new_case_id", "obstruction_id", "xb"]}},
    {"name": "inspect_case", "description": "解析并查看指定算例的 FDS namelist 字段、文件校验值。只读。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]}},
    {"name": "inspect_boundary_records", "description": "只读盘点指定算例中的 SURF、VENT、GEOM 记录，返回同类型序号、ID、全部字段，以及 SURF_ID 引用是否唯一有效；结果可直接用于 edit_boundary_fields 的 target_id/target_index。不会改文件或运行 FDS。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "record_type": {"type": "string", "enum": ["SURF", "VENT", "GEOM"], "description": "可选过滤类型；省略时返回全部三类记录"}}, "required": ["case_id"]}},
    {"name": "compare_case_inputs", "description": "只读比较两个工作区 FDS 文件的关键字段，可用于比较 PyroSim 手工导出后的算例与源算例。默认比较 TIME.T_END、TIME.DT、全部 MESH.IJK 和 MESH.XB；这不证明 PyroSim 几何或模型树完全等价。", "inputSchema": {"type": "object", "properties": {"case_a": {"type": "string"}, "case_b": {"type": "string"}, "fields": {"type": "array", "items": {"type": "string"}, "maxItems": 16}}, "required": ["case_a", "case_b"]}},
    {"name": "preflight_case", "description": "求解前静态检查 FDS 输入：HEAD/TIME/MESH、网格尺寸、T_END 和本机 FDS。只读；返回错误、警告和是否可进入运行确认。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]}},
    {"name": "update_case_parameters", "description": "修改工作区内的算例副本。当前只允许 TIME.T_END、TIME.DT 和 MESH.IJK；禁止任意路径和任意 FDS 代码。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "updates": {"type": "object", "additionalProperties": {}}}, "required": ["case_id", "updates"]}},
    {"name": "run_fds", "description": "启动本机 FDS 计算。运行会占用本机资源，必须先向用户说明算例和 solver 并取得明确同意，再传 approved=true。输出在独立 runs 子目录，不覆盖源算例。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "approved": {"type": "boolean"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}}, "required": ["case_id", "approved"]}},
    {"name": "prepare_simulation", "description": "创建可持久化的仿真工作流：执行静态预检查并冻结输入哈希快照。若检查通过则等待用户确认，不启动计算。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]}},
    {"name": "start_simulation", "description": "启动已准备的后台 FDS 工作流。必须先向用户说明算例、solver、快照哈希、预检查结果和计算开销并取得明确同意，再传 approved=true。返回 workflow_id；使用状态工具跟踪。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}, "approved": {"type": "boolean"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}}, "required": ["workflow_id", "approved"]}},
    {"name": "get_simulation_status", "description": "读取持久化 FDS 工作流状态、阶段、模拟时间、进度、日志摘要和结果文件。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]}},
    {"name": "analyze_simulation_results", "description": "对已完成的仿真工作流调用 theng 读取 .smv/HRR/steps/slice 结果，生成 JSON 摘要和可选 HRR HTML 图；结果只写入该工作流 runs 目录下的 analysis 子目录。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]}},
    {"name": "describe_result_presentation", "description": "只读生成已完成工作流的结果展示清单：列出 .smv、场变量、CSV、theng 摘要和 HRR 图，并分别报告原生 viewer、切片显示和时间控制状态；不打开 viewer，也不把文件存在误报为已渲染。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]}},
    {"name": "open_simulation_visualization", "description": "将已成功完成的工作流 .smv 传给 PyroSim Results，并立即返回 launch_requested；随后调用 results_ui_inspect 对目标窗口做有时限的只读回读。不调用全桌面 UIA 枚举，不修改模型，也不运行 FDS。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]}},
    {"name": "results_ui_inspect", "description": "只读检查 PyroSim Results 可见窗口、活动窗口、目标工作流 .smv 标题匹配情况和 UI Automation 控件树；通过指定 HWND 检查并将 UIA 工作放进 20 秒 watchdog，超时会终止隔离检查进程并返回错误。不点击、不播放、不改变结果显示。树项 selected 只表示导航树选择状态，不等于已确认该结果正在渲染。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}}},
    {"name": "results_ui_set_display", "description": "对成功工作流的 PyroSim Results 执行幂等 show/hide：按完整 Navigation View 树路径唯一定位目标项，读取其上下文菜单中的 Show/Hide 命令来判定当前状态，只执行请求命令，再次读取菜单核验状态翻转。只影响 Results 可视化，不修改 FDS/PSM 模型或结果数据。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}, "item_path": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12}, "action": {"type": "string", "enum": ["show", "hide"]}}, "required": ["workflow_id", "item_path", "action"]}},
    {"name": "results_ui_set_time", "description": "在与成功工作流 .smv 标题匹配的 PyroSim Results 中设置时间滑块位置，并从状态栏回读当前时间核验。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}, "time_seconds": {"type": "number", "minimum": 0}}, "required": ["workflow_id", "time_seconds"]}},
    {"name": "results_ui_set_playback", "description": "在与成功工作流 .smv 标题匹配的 PyroSim Results 中播放或暂停，并通过状态栏回读确认状态转换。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}, "action": {"type": "string", "enum": ["play", "pause"]}}, "required": ["workflow_id", "action"]}},
    {"name": "results_ui_adjust_speed", "description": "在 PyroSim Results 中按一个二倍档位调快或调慢动画，并回读状态栏速度。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}, "direction": {"type": "string", "enum": ["slower", "faster"]}}, "required": ["workflow_id", "direction"]}},
    {"name": "cancel_simulation", "description": "请求停止指定运行中或待确认的仿真工作流；不会删除输入快照或已生成结果。", "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}, "required": ["workflow_id"]}},
    {"name": "list_simulation_workflows", "description": "列出最近的仿真工作流及其阶段、状态和实时进度。只读。", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}}},
    {"name": "pyrosim_ui_inspect", "description": "按配置启动 PyroSim（若尚未运行），然后只读检查窗口、版本和 UI Automation 暴露的控件名称；不会打开、修改或保存模型。当前只启用 PyroSim 2024.2 档案。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pyrosim_ui_snapshot", "description": "读取当前运行中的 PyroSim 窗口及可见对话框的 UI Automation 快照，包括控件名称、可用状态和可读文本；不启动应用、不点击、不输入、不保存。用于每次受支持 UI 操作前后实时观察状态。当前只启用 2024.2。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pyrosim_dismiss_update_dialog", "description": "仅关闭 PyroSim 的精确 ‘Check for Updates’ 提示中的唯一 Close/关闭按钮，然后回读提示已消失且主窗口已启用；不会检查、下载或安装更新。其他窗口状态下一律停止。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pyrosim_cancel_file_dialog", "description": "只取消当前 PyroSim 标准打开/保存文件框；如果没有明确的取消按钮则停止。不会选择文件、保存、导入或导出。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pyrosim_confirm_fds6_dialog", "description": "仅在 PyroSim 的精确 FDS Version Selection Dialog 中读取选项、选择并回读 FDS6，然后确认导入格式；不会求解。没有唯一 FDS6 选项或确认按钮时停止。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pyrosim_dismiss_missing_import_error", "description": "仅当给定 model_id 的隔离 FDS 和 PSM 文件都已不存在、PyroSim 主窗因模态框禁用且唯一标题为 PyroSim 的对话框存在时，按 Enter 关闭此前的文件缺失提示。不会确认运行/保存或修改模型。", "inputSchema": {"type": "object", "properties": {"model_id": {"type": "string"}}, "required": ["model_id"]}},
    {"name": "pyrosim_import_fds", "description": "把案例工作区中的 FDS 案例复制到 pyrosim_agent_gui/working，再通过 PyroSim 2024.2 UI 导入并另存为隔离的 PSM 副本。不会修改/覆盖源 FDS，也不会运行求解。标题和文件校验不能证明完整模型树等价；需安装 requirements-ui.txt。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "new_model_id": {"type": "string", "description": "全新的隔离模型 ID，不能复用已有 ID"}}, "required": ["case_id", "new_model_id"]}},
    {"name": "pyrosim_save_imported_copy", "description": "只恢复一个已开始的隔离 FDS 导入：要求当前 PyroSim 标题指向对应 staging FDS、另存为对话框唯一存在且目标 PSM 不存在；然后保存为新的 PSM 并登记哈希。拒绝覆盖，不运行求解。", "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}, "model_id": {"type": "string"}}, "required": ["case_id", "model_id"]}},
    {"name": "pyrosim_export_fds", "description": "仅打开由 pyrosim_import_fds 建立并登记的 PSM 副本，通过 PyroSim 2024.2 UI 导出到案例工作区一个全新 case ID；拒绝覆盖已有 FDS 文件，并与导入来源比对关键字段。不会运行求解。参数比对不能证明完整几何/模型树等价；需安装 requirements-ui.txt。", "inputSchema": {"type": "object", "properties": {"model_id": {"type": "string"}, "new_case_id": {"type": "string", "description": "全新的输出算例 ID，不能覆盖已有文件"}}, "required": ["model_id", "new_case_id"]}},
    {"name": "pyrosim_open_model_copy", "description": "把配置案例工作区内的 .psm 模型复制到 pyrosim_agent_prototype/working，校验 SHA-256 后通过 Windows 文件关联请求 PyroSim 打开副本，并等待可见窗口标题确认副本路径。源文件不修改；标题确认不代表已读取或修改模型内容。", "inputSchema": {"type": "object", "properties": {"model_path": {"type": "string", "description": "相对于案例工作区的 .psm 路径，或工作区内的绝对路径"}}, "required": ["model_path"]}},
]


def call_tool(name: str, args: dict) -> dict:
    config = load_config()
    funcs = {
        "inspect_installation": lambda: fds_tools.inspect_installation(config),
        "inspect_adapter_capabilities": lambda: fds_tools.inspect_installation(config)["adapter_profiles"],
        "list_cases": lambda: fds_tools.list_cases(config),
        "stage_official_methane_flame_demo": lambda: fds_tools.stage_official_methane_flame_demo(config),
        "create_case": lambda: fds_tools.create_case(config, args.get("source_id"), args.get("new_case_id")),
        "create_case_with_updates": lambda: fds_tools.create_case_with_updates(
            config, args.get("source_id"), args.get("new_case_id"), args.get("updates")),
        "create_case_with_surface_updates": lambda: fds_tools.create_case_with_surface_updates(
            config, args.get("source_id"), args.get("new_case_id"), args.get("surface_id"), args.get("updates")),
        "create_case_with_burner_obstruction": lambda: fds_tools.create_case_with_burner_obstruction(
            config, args.get("source_id"), args.get("new_case_id"), args.get("obstruction_id"),
            args.get("surface_id"), args.get("xb"), args.get("hrrpua_kw_m2")),
        "add_obstruction_to_case": lambda: fds_tools.add_obstruction_to_case(
            config, args.get("source_id"), args.get("new_case_id"), args.get("xb")),
        "add_vent_to_case": lambda: fds_tools.add_vent_to_case(
            config, args.get("source_id"), args.get("new_case_id"), args.get("xb"), args.get("surface_id")),
        "edit_boundary_fields": lambda: fds_tools.edit_boundary_fields(
            config, args.get("source_id"), args.get("new_case_id"), args.get("record_type"),
            args.get("target_index"), args.get("updates"), args.get("target_id")),
        "list_geometry_assets": lambda: fds_tools.list_geometry_assets(config),
        "inspect_geometry_asset": lambda: fds_tools.inspect_geometry_asset(
            config, args.get("mesh_file")),
        "add_geometry_mesh_to_case": lambda: fds_tools.add_geometry_mesh_to_case(
            config, args.get("source_id"), args.get("new_case_id"), args.get("mesh_file"),
            args.get("geometry_id"), args.get("surface_id"), args.get("scale", 1.0),
            args.get("translation"), args.get("material_surface_map")),
        "create_case_with_obstruction_updates": lambda: fds_tools.create_case_with_obstruction_updates(
            config, args.get("source_id"), args.get("new_case_id"), args.get("obstruction_id"), args.get("xb")),
        "inspect_case": lambda: fds_tools.inspect_case(config, args.get("case_id")),
        "inspect_boundary_records": lambda: fds_tools.inspect_boundary_records(
            config, args.get("case_id"), args.get("record_type")),
        "compare_case_inputs": lambda: fds_tools.compare_case_inputs(
            config, args.get("case_a"), args.get("case_b"), args.get("fields")),
        "preflight_case": lambda: fds_tools.preflight_case(config, args.get("case_id")),
        "update_case_parameters": lambda: fds_tools.update_parameters(config, args.get("case_id"), args.get("updates")),
        "run_fds": lambda: fds_tools.run_case(config, args.get("case_id"), args.get("approved", False), args.get("timeout_seconds", 3600)),
        "prepare_simulation": lambda: workflows.prepare_simulation(config, args.get("case_id")),
        "start_simulation": lambda: workflows.start_simulation(config, args.get("workflow_id"), args.get("approved", False), args.get("timeout_seconds", 3600)),
        "get_simulation_status": lambda: workflows.get_simulation_status(config, args.get("workflow_id")),
        "analyze_simulation_results": lambda: results_tools.analyze_simulation_results(config, args.get("workflow_id")),
        "describe_result_presentation": lambda: presentation.describe_result_presentation(config, args.get("workflow_id")),
        "open_simulation_visualization": lambda: results_tools.open_simulation_visualization(config, args.get("workflow_id")),
        "results_ui_inspect": lambda: results_ui_bridge.inspect_results_ui(config, args.get("workflow_id")),
        "results_ui_set_display": lambda: results_ui_bridge.set_results_display(
            config, args.get("workflow_id"), args.get("item_path"), args.get("action")),
        "results_ui_set_time": lambda: results_ui_bridge.set_results_time(config, args.get("workflow_id"), args.get("time_seconds")),
        "results_ui_set_playback": lambda: results_ui_bridge.set_results_playback(config, args.get("workflow_id"), args.get("action")),
        "results_ui_adjust_speed": lambda: results_ui_bridge.adjust_results_speed(config, args.get("workflow_id"), args.get("direction")),
        "cancel_simulation": lambda: workflows.cancel_simulation(config, args.get("workflow_id")),
        "list_simulation_workflows": lambda: workflows.list_simulation_workflows(config, args.get("limit", 20)),
        "pyrosim_ui_inspect": lambda: pyrosim_ui_bridge.inspect_ui(config),
        "pyrosim_ui_snapshot": lambda: pyrosim_ui_bridge.snapshot_ui(config),
        "pyrosim_dismiss_update_dialog": lambda: pyrosim_ui_bridge.dismiss_update_dialog(config),
        "pyrosim_cancel_file_dialog": lambda: pyrosim_ui_bridge.cancel_file_dialog(config),
        "pyrosim_confirm_fds6_dialog": lambda: pyrosim_ui_bridge.resolve_fds6_dialog(config),
        "pyrosim_dismiss_missing_import_error": lambda: pyrosim_ui_bridge.dismiss_missing_import_file_error(config, args.get("model_id")),
        "pyrosim_import_fds": lambda: pyrosim_ui_bridge.import_fds(config, args.get("case_id"), args.get("new_model_id")),
        "pyrosim_save_imported_copy": lambda: pyrosim_ui_bridge.save_imported_copy(config, args.get("case_id"), args.get("model_id")),
        "pyrosim_export_fds": lambda: pyrosim_ui_bridge.export_fds(config, args.get("model_id"), args.get("new_case_id")),
        "pyrosim_open_model_copy": lambda: pyrosim_tools.open_model_copy(config, args.get("model_path")),
    }
    if name not in funcs:
        raise fds_tools.ToolError(f"Unknown tool: {name}")
    case_id = args.get("case_id") or args.get("new_case_id") or args.get("case_a")
    invocation_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    emit_event(config, "tool_started", f"Agent 调用本地工具：{name}", tool=name,
               invocation_id=invocation_id, arguments=args, case_id=case_id)
    try:
        result = funcs[name]()
        fds_tools.write_audit(config, name, result)
        if name == "pyrosim_open_model_copy":
            message = ("PyroSim 窗口已显示隔离副本路径" if result.get("gui_open_verified")
                       else "已请求 PyroSim 打开隔离副本，尚未确认窗口标题")
            emit_event(config, "pyrosim_open_requested", message,
                       model_path=result.get("working_copy"), source_model=result.get("source_model"),
                       source_unchanged=result.get("source_unchanged"),
                       verified=result.get("gui_open_verified"), window_title=result.get("window_title"))
        emit_event(config, "tool_finished", f"本地工具完成：{name}", tool=name,
                   invocation_id=invocation_id, case_id=case_id,
                   duration_seconds=round(time.monotonic() - started, 3), result=result)
        return result
    except Exception as exc:
        emit_event(config, "tool_failed", f"本地工具失败：{name} · {exc}", tool=name,
                   invocation_id=invocation_id,
                   case_id=case_id, duration_seconds=round(time.monotonic() - started, 3))
        raise


def respond(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "pyrosim-fds-agent", "version": "0.1.0"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            result = call_tool(params.get("name", ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": False,
            }}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": str(exc) or f"{type(exc).__name__}: {exc!r}"}], "isError": True,
            }}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = respond(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

