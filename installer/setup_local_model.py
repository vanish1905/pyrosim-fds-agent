from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile


MODEL_NAME = "Qwen3-0.6B-Q8_0.gguf"
MODEL_BYTES = 639446688
MODEL_SHA256 = "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"
SERVER_SHA256 = "cacd4e880bac8f3c9a54c25c84ac15d1f85095938223d0318746898ceaa3fb43"


def bundled_file(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / name


def message(text: str, title: str, flags: int) -> int:
    return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    title = "PyroSim / FDS Agent 本地模型安装"
    appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    app_dir = appdata / "Programs" / "PyroSim FDS Agent"
    model_file = f"{MODEL_BYTES / (1024 ** 3):.2f} GiB"
    if message(
        "为当前 Windows 用户安装离线 Qwen3-0.6B 模型与 llama.cpp CPU/Vulkan 推理运行时？\n\n"
        f"模型文件：{model_file}\n"
        f"安装位置：{app_dir}\n\n"
        "安装后，启动 PyroSim / FDS Agent 并在设置中点击“启动内置 Qwen3-0.6B”。\n"
        "此安装包不会安装 PyroSim 或 FDS。",
        title,
        0x00000004 | 0x00000020,
    ) != 6:
        return 0

    archive = bundled_file("local-model-payload.zip")
    if not archive.is_file():
        message("安装包缺少模型负载文件。请重新下载完整安装包。", title, 0x00000010)
        return 2

    try:
        app_dir.mkdir(parents=True, exist_ok=True)
        drive = Path(app_dir.anchor or "C:\\")
        free_bytes = shutil.disk_usage(drive).free
        required_bytes = MODEL_BYTES + 500 * 1024 * 1024
        if free_bytes < required_bytes:
            raise OSError(f"磁盘空间不足。至少需要 {required_bytes / (1024 ** 3):.1f} GiB 可用空间。")

        stage = Path(tempfile.mkdtemp(prefix=".qwen-install-", dir=app_dir))
        try:
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    target = (stage / member.filename).resolve()
                    if stage.resolve() not in target.parents and target != stage.resolve():
                        raise ValueError(f"安装包包含无效路径：{member.filename}")
                print("Extracting Qwen3-0.6B and llama.cpp runtime…", flush=True)
                bundle.extractall(stage)

            payload = stage / "agent_runtime"
            manifest_file = payload / "manifest.json"
            model = payload / "models" / MODEL_NAME
            cpu_server = payload / "llama" / "llama-server.exe"
            vulkan_server = payload / "llama-vulkan" / "llama-server.exe"
            if not all(path.is_file() for path in (manifest_file, model, cpu_server, vulkan_server)):
                raise FileNotFoundError("模型、manifest 或 CPU/Vulkan 推理运行时不完整。")
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            model_meta = manifest.get("model", {})
            if model.stat().st_size != MODEL_BYTES or sha256(model) != MODEL_SHA256:
                raise ValueError("Qwen 模型 SHA-256 校验失败；没有安装不完整或损坏的模型。")
            if model_meta.get("sha256") != MODEL_SHA256:
                raise ValueError("模型 manifest 与当前固定校验值不匹配。")
            if (sha256(cpu_server) != SERVER_SHA256 or sha256(vulkan_server) != SERVER_SHA256
                    or manifest.get("runtime", {}).get("release") != "b11836"
                    or manifest.get("gpu_runtime", {}).get("release") != "b11836"):
                raise ValueError("llama.cpp CPU/Vulkan 运行时与固定版本校验值不匹配。")

            licenses = stage / "licenses"
            required_licenses = ("Qwen3-0.6B-LICENSE.txt", "llama.cpp-LICENSE.txt")
            if any(not (licenses / name).is_file() for name in required_licenses):
                raise FileNotFoundError("安装包缺少 Qwen3 或 llama.cpp 许可证文件。")

            final_runtime = app_dir / "agent_runtime"
            backup_runtime = app_dir / ".agent_runtime.previous"
            if backup_runtime.exists():
                shutil.rmtree(backup_runtime)
            if final_runtime.exists():
                os.replace(final_runtime, backup_runtime)
            try:
                os.replace(payload, final_runtime)
            except Exception:
                if backup_runtime.exists() and not final_runtime.exists():
                    os.replace(backup_runtime, final_runtime)
                raise
            if backup_runtime.exists():
                shutil.rmtree(backup_runtime)

            license_target = app_dir / "licenses"
            license_target.mkdir(parents=True, exist_ok=True)
            for name in required_licenses:
                shutil.copy2(licenses / name, license_target / name)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        message(
            "Qwen3-0.6B 与 llama.cpp CPU/Vulkan 运行时已安装。\n\n"
            "打开 PyroSim / FDS Agent，在“连接设置”中启动内置 Qwen。\n"
            "服务只绑定本机 127.0.0.1；关闭工作台时会停止。",
            title,
            0x00000040,
        )
        return 0
    except Exception as exc:
        message(f"安装失败：\n{exc}", title, 0x00000010)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
