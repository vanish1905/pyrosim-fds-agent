"""Version and capability profiles for the PyroSim/FDS adapter.

Presence is not compatibility: each capability carries an explicit state so
an Agent can use the solver path while keeping unverified GUI actions separate.
"""
from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
from typing import Any


PROFILE_SCHEMA_VERSION = 1

# A revision enters this set only after an actual successful solver workflow.
# Add new revisions after recording solver exit, logs, and expected artifacts.
VERIFIED_FDS_REVISIONS = {
    "FDS-6.9.1-0-g889da6a-release",
}

PYROSIM_UI_PROFILES = {
    "2024.2": {
        "id": "pyrosim-uia-2024.2",
        "version_prefix": "2024.2",
        "capabilities": {
            "inspect_window": "profiled",
            "snapshot_controls": "profiled",
            "import_fds_copy": "experimental",
            "export_fds_copy": "experimental",
            "edit_native_psm": "unsupported",
        },
    },
}

RESULTS_UI_CAPABILITIES = {
    "open_smv": "requestable_unverified",
    "inspect_window": "profiled",
    "toggle_display_item": "experimental",
    "set_time": "experimental",
    "playback_control": "experimental",
}


def _path_present(value: Any) -> bool:
    try:
        return bool(value) and Path(str(value)).is_file()
    except (OSError, TypeError, ValueError):
        return False


def _pyrosim_profile(version: str | None) -> dict[str, Any]:
    if not version:
        return {
            "id": "pyrosim-ui-undetected",
            "product": "PyroSim",
            "version": None,
            "status": "version_detection_required",
            "capabilities": {
                "inspect_window": "requires_version_check",
                "snapshot_controls": "requires_version_check",
                "import_fds_copy": "requires_version_check",
                "export_fds_copy": "requires_version_check",
                "edit_native_psm": "unsupported",
            },
            "note": "只检查安装路径不会启动 PyroSim；调用 pyrosim_ui_inspect 后才可确认运行版本和匹配的 UI 档案。",
        }

    normalized = str(version).strip()
    profile = next(
        (item for prefix, item in PYROSIM_UI_PROFILES.items()
         if normalized == prefix or normalized.startswith(prefix + ".")),
        None,
    )
    if profile is None:
        return {
            "id": "pyrosim-ui-unprofiled",
            "product": "PyroSim",
            "version": normalized,
            "status": "unsupported_profile",
            "capabilities": {
                "inspect_window": "read_only_probe_only",
                "snapshot_controls": "read_only_probe_only",
                "import_fds_copy": "blocked",
                "export_fds_copy": "blocked",
                "edit_native_psm": "unsupported",
            },
            "note": "此版本没有已配置的 UI 自动化档案；不会对它执行导入/导出操作。",
        }

    return {
        "id": profile["id"],
        "product": "PyroSim",
        "version": normalized,
        "status": "profiled_with_experimental_operations",
        "capabilities": dict(profile["capabilities"]),
        "note": "版本档案只表示 UI 路径已适配；导入/导出仍按各自回读证据判定。",
    }


def resolve_pyrosim_ui_profile(version: str) -> dict[str, Any] | None:
    """Return the configured UI profile for an observed live-window version."""
    normalized = str(version).strip()
    profile = next(
        (item for prefix, item in PYROSIM_UI_PROFILES.items()
         if normalized == prefix or normalized.startswith(prefix + ".")),
        None,
    )
    if profile is None:
        return None
    return {"id": profile["id"], "version_prefix": profile["version_prefix"],
            "capabilities": dict(profile["capabilities"])}


def build_adapter_profile_report(installation: dict[str, Any],
                                 pyrosim_version: str | None = None) -> dict[str, Any]:
    """Build a runtime-specific capability report without launching software."""
    fds_path = installation.get("fds_exe")
    fds_revision = installation.get("last_observed_fds_revision")
    fds_present = bool(installation.get("fds_present")) and _path_present(fds_path)
    fds_verified = (fds_revision in VERIFIED_FDS_REVISIONS
                    and installation.get("verified_fds_revision") == fds_revision)
    root = Path(str(installation.get("pyrosim_root") or "."))
    results_path = root / "PyroSimResults.exe"
    theng_available = find_spec("theng") is not None

    solver = {
        "id": "fds-cli",
        "product": "FDS",
        "version": fds_revision,
        "status": "verified" if fds_verified else ("available_unverified" if fds_present else "unavailable"),
        "capabilities": {
            "inspect_fds_input": "available",
            "add_burner_obstruction_copy": "available",
            "edit_allowlisted_surface_parameters_copy": "available",
            "edit_arbitrary_geometry": "unsupported",
            "edit_arbitrary_surface_parameters": "unsupported",
            "preflight_input": "available",
            "run_solver": "verified" if fds_verified else ("available_unverified" if fds_present else "unavailable"),
            "poll_progress": "available" if fds_present else "unavailable",
            "cancel_run": "available" if fds_present else "unavailable",
        },
        "evidence": installation.get("verified_fds_evidence") if fds_verified else None,
    }
    results = {
        "id": "pyrosim-results-ui",
        "product": "PyroSim Results",
        "version": pyrosim_version,
        "executable": str(results_path) if results_path.is_file() else None,
        "status": "available_unverified" if results_path.is_file() else "unavailable",
        "capabilities": dict(RESULTS_UI_CAPABILITIES),
    }
    theng = {
        "id": "theng-results",
        "product": "theng",
        "version": None,
        "status": "available" if theng_available else "unavailable",
        "capabilities": {
            "read_smv": "available" if theng_available else "unavailable",
            "summarize_hrr_steps_slices": "available" if theng_available else "unavailable",
            "native_psm_edit": "unsupported",
        },
    }
    pyrosim = _pyrosim_profile(pyrosim_version)
    pyrosim["executable"] = installation.get("pyrosim_exe")
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "status_vocabulary": [
            "available", "verified", "profiled_with_experimental_operations",
            "available_unverified", "version_detection_required",
            "unsupported_profile", "unavailable", "experimental", "unsupported", "blocked",
            "requires_version_check", "read_only_probe_only", "requestable_unverified",
            "profiled", "not_verified", "data_ready", "ready",
        ],
        "products": [solver, pyrosim, results, theng],
        "notes": [
            "Executable presence does not mean a version pair or GUI action is validated.",
            "Only the exact FDS revision in the verification registry is marked verified.",
            "PyroSim UI profiles are checked when the UI adapter reads the live window version.",
        ],
    }
