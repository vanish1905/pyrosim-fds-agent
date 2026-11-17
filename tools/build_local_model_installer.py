"""Stage a verified offline Qwen/llama.cpp bundle and build its installer."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "Qwen3-0.6B-Q8_0.gguf"
MODEL_BYTES = 639446688
MODEL_SHA256 = "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"
CPU_SERVER_SHA256 = "cacd4e880bac8f3c9a54c25c84ac15d1f85095938223d0318746898ceaa3fb43"
VULKAN_SERVER_SHA256 = "cacd4e880bac8f3c9a54c25c84ac15d1f85095938223d0318746898ceaa3fb43"
DEFAULT_SOURCE = Path(r"D:\program\codex\ai_infra\vidur_study\agent-model-payload")
PAYLOAD = ROOT / "build" / "local-model-payload"
ARCHIVE = ROOT / "build" / "local-model-payload.zip"
OUTPUT = ROOT / "dist" / "PyroSim-FDS-Agent-LocalModel-Setup.exe"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_runtime(source: Path) -> Path:
    source = source.expanduser().resolve()
    return source / "agent_runtime" if (source / "agent_runtime").is_dir() else source


def stage(source: Path, target: Path = PAYLOAD) -> Path:
    runtime_source = source_runtime(source)
    license_source = source / "licenses" if (source / "licenses").is_dir() else runtime_source.parent / "licenses"
    model_source = runtime_source / "models" / MODEL_NAME
    cpu_source = runtime_source / "llama"
    vulkan_source = runtime_source / "llama-vulkan"
    manifest_source = runtime_source / "manifest.json"
    for path in (model_source, cpu_source / "llama-server.exe", vulkan_source / "llama-server.exe", manifest_source,
                 license_source / "Qwen3-0.6B-LICENSE.txt", license_source / "llama.cpp-LICENSE.txt"):
        if not path.is_file():
            raise FileNotFoundError(f"Required local model asset missing: {path}")
    if model_source.stat().st_size != MODEL_BYTES or sha256(model_source) != MODEL_SHA256:
        raise ValueError("The staged Qwen GGUF does not match the pinned model revision and SHA-256.")
    manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
    if manifest.get("model", {}).get("sha256") != MODEL_SHA256:
        raise ValueError("The source model manifest does not match the pinned GGUF SHA-256.")
    if manifest.get("runtime", {}).get("release") != "b11836" or manifest.get("gpu_runtime", {}).get("release") != "b11836":
        raise ValueError("The source llama.cpp runtimes are not the pinned b11836 build.")
    if sha256(cpu_source / "llama-server.exe") != CPU_SERVER_SHA256:
        raise ValueError("The CPU llama-server does not match the pinned b11836 binary.")
    if sha256(vulkan_source / "llama-server.exe") != VULKAN_SERVER_SHA256:
        raise ValueError("The Vulkan llama-server does not match the pinned b11836 binary.")

    target = target.resolve()
    if ROOT not in target.parents:
        raise ValueError("Payload target must be within the project build directory.")
    if target.exists():
        shutil.rmtree(target)
    runtime = target / "agent_runtime"
    (runtime / "models").mkdir(parents=True)
    shutil.copy2(model_source, runtime / "models" / MODEL_NAME)
    shutil.copy2(manifest_source, runtime / "manifest.json")
    for folder, source_folder in (("llama", cpu_source), ("llama-vulkan", vulkan_source)):
        destination = runtime / folder
        destination.mkdir(parents=True)
        # llama-server dynamically loads the companion DLLs from its own folder.
        # Keep all DLLs, the server executable, and the bundled runtime license.
        for source_file in source_folder.iterdir():
            if source_file.is_file() and (source_file.suffix.lower() == ".dll"
                                          or source_file.name.casefold() == "llama-server.exe"
                                          or source_file.name == "LICENSE-LLVM-OpenMP"):
                shutil.copy2(source_file, destination / source_file.name)
        if not (destination / "llama-server.exe").is_file():
            raise FileNotFoundError(f"llama-server was not staged for {folder}")
    (target / "licenses").mkdir(parents=True)
    for name in ("Qwen3-0.6B-LICENSE.txt", "llama.cpp-LICENSE.txt"):
        shutil.copy2(license_source / name, target / "licenses" / name)

    validation = {
        "model_bytes": MODEL_BYTES,
        "model_sha256": sha256(runtime / "models" / MODEL_NAME),
        "cpu_server_sha256": sha256(runtime / "llama" / "llama-server.exe"),
        "vulkan_server_sha256": sha256(runtime / "llama-vulkan" / "llama-server.exe"),
        "source_manifest": manifest,
    }
    (target / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def archive_payload(payload: Path = PAYLOAD, archive: Path = ARCHIVE) -> Path:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as output:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                compression = zipfile.ZIP_STORED if path.suffix.lower() == ".gguf" else zipfile.ZIP_DEFLATED
                output.write(path, path.relative_to(payload), compress_type=compression)
    return archive


def build(output: Path = OUTPUT) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("Build the Windows model installer on Windows.")
    payload = PAYLOAD
    archive_payload(payload)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "pyinstaller-local-model"
    spec = ROOT / "build" / "pyinstaller-local-model-spec"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
               "--name", "PyroSim-FDS-Agent-LocalModel-Setup", "--distpath", str(output.parent),
               "--workpath", str(work), "--specpath", str(spec), "--add-data",
               f"{ARCHIVE};.", str(ROOT / "installer" / "setup_local_model.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    if not output.is_file():
        raise FileNotFoundError(output)
    digest = sha256(output)
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"Model installer: {output}\nBytes: {output.stat().st_size}\nSHA-256: {digest}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="Verified local payload containing agent_runtime/ and licenses/")
    parser.add_argument("--stage-only", action="store_true", help="Stage and verify without building the EXE")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    stage(args.source)
    if args.stage_only:
        print(f"Verified local model payload: {PAYLOAD}")
    else:
        build(args.output)


if __name__ == "__main__":
    main()
