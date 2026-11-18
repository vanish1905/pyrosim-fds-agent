"""Build a relocatable Windows x64 Python runtime for the Agent workbench."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("pyro_agent", "assets", "examples", "docs")
SOURCE_FILES = (
    "run_workbench.py", "AGENTS.md", "opencode.json", "config.example.json",
    "launch-workbench.cmd", "start-mcp.cmd", "README.md",
)
IGNORE_NAMES = {
    "__pycache__", "test", "tests", "idlelib", "ensurepip", "pip",
    "setuptools", "wheel", "PyInstaller", "_pyinstaller_hooks_contrib",
    "altgraph",
}


def ignore_runtime(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORE_NAMES or name.endswith((".pyc", ".pyo"))}


def copy_tree(source: Path, target: Path, ignore=ignore_runtime) -> None:
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_zip(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source))


def build_payload(output: Path) -> None:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Build the portable distribution on Windows x64.")
    output.mkdir(parents=True, exist_ok=True)
    for name in SOURCE_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, output / name)
    for name in SOURCE_DIRS:
        source = ROOT / name
        if source.is_dir():
            copy_tree(source, output / name)

    runtime = output / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    purelib = Path(sysconfig.get_path("purelib"))
    for name in ("python.exe", "pythonw.exe", "python3.dll", "LICENSE.txt"):
        source = base / name
        if source.is_file():
            shutil.copy2(source, runtime / name)
    for pattern in ("python3*.dll", "vcruntime*.dll"):
        for source in base.glob(pattern):
            shutil.copy2(source, runtime / source.name)
    for source in base.glob("python3*.zip"):
        shutil.copy2(source, runtime / source.name)

    copy_tree(base / "DLLs", runtime / "DLLs")
    copy_tree(base / "Lib", runtime / "Lib")
    copy_tree(purelib, runtime / "Lib" / "site-packages")
    version = f"{sys.version_info.major}{sys.version_info.minor}"
    pth = runtime / f"python{version}._pth"
    pth.write_text(f".\npython{version}.zip\nLib\nDLLs\nLib/site-packages\n..\nimport site\n", encoding="ascii")

    uninstall_source = ROOT / "installer"
    for name in ("uninstall.ps1", "uninstall.cmd"):
        source = uninstall_source / name
        if source.is_file():
            shutil.copy2(source, output / name)

    with tempfile.TemporaryDirectory(prefix="pyro-agent-build-check-") as check_home:
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYROSIM_AGENT_HOME"] = check_home
        check = subprocess.run(
            [str(runtime / "python.exe"), str(output / "run_workbench.py"), "--check"],
            cwd=output, env=env, text=True, capture_output=True, check=False,
        )
    if check.returncode:
        raise RuntimeError(f"Bundled runtime check failed:\n{check.stdout}\n{check.stderr}")

    manifest = {
        "version": "1.0.4",
        "platform": "Windows-x64",
        "python": platform.python_version(),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "file_count": sum(path.is_file() for path in output.rglob("*")),
        "uncompressed_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
        "runtime_check": "passed",
        "network_required_for_startup": False,
    }
    (output / "portable-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "installer" / "payload")
    parser.add_argument("--portable-zip", type=Path, default=ROOT / "dist" / "PyroSim-FDS-Agent-Portable.zip")
    parser.add_argument("--installer-payload-zip", type=Path, default=ROOT / "build" / "iexpress" / "app-payload.zip")
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT not in output.parents:
        raise ValueError("Payload directory must remain inside the project.")
    build_payload(output)
    write_zip(output, args.portable_zip.resolve())
    write_zip(output, args.installer_payload_zip.resolve())
    print(f"Portable payload: {output}", flush=True)
    for archive in (args.portable_zip.resolve(), args.installer_payload_zip.resolve()):
        hash_path = archive.with_suffix(archive.suffix + ".sha256")
        hash_path.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
        print(f"Archive ready: {archive} ({archive.stat().st_size / 1024**2:.1f} MiB)", flush=True)


if __name__ == "__main__":
    main()
