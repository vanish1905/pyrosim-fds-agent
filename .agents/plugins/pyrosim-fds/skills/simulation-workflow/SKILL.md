---
name: simulation-workflow
description: Use the local PyroSim/FDS MCP tools to inspect cases, prepare and preflight simulations, request approval before solving, and monitor or stop workflows.
---

# PyroSim / FDS simulation workflow

Use the `pyrosim_fds` MCP server for all project actions. Never use shell commands, arbitrary file access, or unapproved GUI automation to get around the MCP boundary.

## Inspect and prepare

1. Call `inspect_installation` and `inspect_adapter_capabilities` before relying on a solver. Report executable presence separately from exact-version verification. Use the profile status and evidence fields; do not infer PyroSim/FDS compatibility from installation paths.
2. Call `list_cases`, then inspect the user-selected case with `inspect_case`.
3. For requested edits, map the natural-language request to explicit parameter names and values. When clear, call `create_case_with_updates` to make a new copy, apply supported edits, and verify the saved values in one operation. It deletes that new copy if update or readback fails. If the requested field is unsupported or a value is unclear, keep the source unchanged and ask/report instead of guessing.
4. Before a solve, call `prepare_simulation`. Explain the case, solver, frozen input hash, preflight errors and warnings, and expected compute use. Do not start until the user explicitly approves.

## Run and monitor

1. After explicit approval, call `start_simulation` with the returned workflow ID and `approved: true`.
2. Poll `get_simulation_status` and report tool-provided stage, progress, and log information until a terminal state.
3. When status is `completed`, call `analyze_simulation_results` to generate the theng summary, then call `describe_result_presentation` to list the run's native and analysis artifacts. This is a version-neutral data contract for Qt/OpenCode/Codex; `data_ready` means files are present, not rendered.
4. When requested, call `open_simulation_visualization` and then `results_ui_inspect`. Report the open request, matching window, and render/display state separately. A window title or `.smv` file alone is not proof that a slice is visible. Result display toggles and time controls remain experimental unless the adapter returns verifiable before/after state.
5. Call `cancel_simulation` only when the user asks to stop. Do not delete snapshots or outputs.
6. Report success only when the tool reports exit code 0 and verified outputs. Distinguish solver errors from engineering conclusions.

Prefer `prepare_simulation` plus `start_simulation` over the synchronous `run_fds` compatibility tool. Never infer fire size, mesh, material, or boundary conditions from a vague request.

## PyroSim models

`pyrosim_open_model_copy` only copies a `.psm` file from the configured case workspace and requests PyroSim to open the copy. A visible title match does not verify that model contents were read or changed. Do not claim a model edit or save unless a supported tool confirms it.

The optional Windows UI bridge provides `pyrosim_ui_snapshot`, `pyrosim_ui_inspect`, `pyrosim_open_model_copy`, `pyrosim_import_fds`, and `pyrosim_export_fds`. Its version/capability profile is resolved from the live PyroSim window; currently only 2024.2 is profiled, while import/export remain experimental and native `.psm` editing is unsupported. An unknown version is read-only probe only. The bridge requires `requirements-ui.txt` in the MCP server's Python environment. Use `pyrosim_ui_snapshot` before and after each supported UI action to observe the current title and visible dialog/control state; the Qt companion receives each bridge step as it happens. Import always stages the source case under `pyrosim_agent_gui/working` and saves a newly named PSM under `pyrosim_agent_gui/models`. Export only accepts a PSM registered by that import tool and writes to a new case ID; it refuses to overwrite an existing `.fds` file. These operations use the interactive Windows desktop and must be explicitly requested by the user.

Do not infer that a title match or successful file write verifies the full model tree, geometry, CAD/BIM fidelity, or engineering correctness. Export readback compares supported FDS fields only. The UI bridge does not launch FDS or PyroSim solving. If an unexpected modal, unsupported version, missing dependency, or uncertain dialog appears, report the failure and stop rather than clicking through it. `theng` remains a results-processing interface, not a confirmed `.psm` authoring API.

If the user imports an FDS copy into PyroSim and exports it back into the configured case workspace, use `compare_case_inputs` to compare key simulation parameters. State clearly that this field comparison does not verify full geometry or model-tree equivalence.
