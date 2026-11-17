"""Private loopback llama.cpp server for the separately installed Qwen model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request


MODEL_NAME = "Qwen3-0.6B-Q8_0.gguf"
MODEL_SHA256 = "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"


def resolve_model_root(root: Path, environ: dict | None = None) -> Path:
    """Find an installed optional model root for a portable or installed Workbench.

    Explicit ``PYROSIM_AGENT_RUNTIME`` wins, then the Workbench directory, the
    development staging directory, and finally the default per-user install.
    The first complete candidate is returned; otherwise the primary location is
    returned so callers can report a useful "not installed" state.
    """
    root = Path(root).resolve()
    environment = os.environ if environ is None else environ
    primary = root / "agent_runtime"
    candidates = []
    override = environment.get("PYROSIM_AGENT_RUNTIME")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend((primary, root / "agent-model-payload" / "agent_runtime"))
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "PyroSim FDS Agent" / "agent_runtime")
    for candidate in candidates:
        if (candidate / "models" / MODEL_NAME).is_file() and (candidate / "llama" / "llama-server.exe").is_file():
            return candidate.resolve()
    return (Path(override).expanduser().resolve() if override else primary)


class LocalModelError(RuntimeError):
    """The optional local model could not be started or verified."""


class LocalQwenRuntime:
    """Starts and owns a loopback-only llama.cpp process with GPU-to-CPU fallback."""

    def __init__(self, root: Path, *, model_root: Path | None = None, log_root: Path | None = None):
        self.root = Path(root).resolve()
        self.runtime_root = Path(model_root).resolve() if model_root else self.root / "agent_runtime"
        self.log_root = Path(log_root).resolve() if log_root else self.root / "logs"
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.api_key = ""
        self.backend = "stopped"
        self.device = ""
        self.fallback_reason = ""
        self._lock = threading.RLock()
        self._verified: tuple[int, int] | None = None
        self._log = None

    @property
    def endpoint(self) -> str:
        if not self.port:
            raise LocalModelError("本地模型尚未启动。")
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def model_path(self) -> Path:
        return self.runtime_root / "models" / MODEL_NAME

    @property
    def cpu_server(self) -> Path:
        return self.runtime_root / "llama" / "llama-server.exe"

    @property
    def vulkan_server(self) -> Path:
        return self.runtime_root / "llama-vulkan" / "llama-server.exe"

    def installed(self) -> bool:
        return self.model_path.is_file() and self.cpu_server.is_file()

    @staticmethod
    def _loopback_request(url: str, api_key: str, payload: dict | None = None, timeout: float = 2):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=body, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def parse_vulkan_devices(output: str) -> list[tuple[int, str, str]]:
        devices = []
        for line in output.splitlines():
            match = re.match(r"^\s*(Vulkan\d+):\s*(.+)$", line)
            if not match:
                continue
            device, label = match.groups()
            if re.search(r"llvmpipe|lavapipe|swiftshader|software|basic render", label, re.I):
                continue
            memory = re.search(r"(\d+)\s*MiB free", label)
            devices.append((int(memory.group(1)) if memory else 0, device, label))
        return sorted(devices, reverse=True)

    def _select_gpu(self, timeout: float = 15) -> tuple[str, str] | None:
        if not self.vulkan_server.is_file():
            self.fallback_reason = "未安装 Vulkan 推理运行时"
            return None
        try:
            probe = subprocess.run(
                [str(self.vulkan_server), "--list-devices"], cwd=self.vulkan_server.parent,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = probe.stdout.decode("utf-8", "replace")
            devices = self.parse_vulkan_devices(output) if probe.returncode == 0 else []
            if devices:
                _, key, label = devices[0]
                return key, label
            self.fallback_reason = "未检测到可用 Vulkan GPU" if probe.returncode == 0 else "Vulkan 驱动初始化失败"
        except (OSError, subprocess.TimeoutExpired):
            self.fallback_reason = "Vulkan GPU 检测失败或超时"
        return None

    def _wait_ready(self, process: subprocess.Popen, deadline: float, cancel=None) -> bool:
        while time.monotonic() < deadline:
            if cancel and cancel.is_set():
                raise LocalModelError("已取消模型启动。")
            if process.poll() is not None:
                return False
            try:
                status = self._loopback_request(f"http://127.0.0.1:{self.port}/health", self.api_key, timeout=1)
                if status.get("status") == "ok":
                    return True
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pass
            time.sleep(.2)
        return False

    def _launch(self, backend: str, server: Path, device: str = "none", label: str = "", cancel=None,
                progress=None, timeout: float = 90):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        # Bind to loopback only so existing OpenAI-compatible clients can connect
        # without exposing a bearer token or accepting remote connections.
        self.api_key = ""
        log_dir = self.log_root
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"llama-local-{os.getpid()}.log"
        self._log = log_path.open("ab")
        args = [
            str(server), "-m", str(self.model_path), "--host", "127.0.0.1", "--port", str(self.port),
            "--alias", "Qwen3-0.6B", "-c", "8192", "-np", "1",
            "-t", str(min(6, max(2, (os.cpu_count() or 4) // 2))), "-b", "256", "-ub", "128",
            "--jinja", "--reasoning", "off", "--chat-template-kwargs", '{"enable_thinking":false}',
            "--no-webui", "--no-warmup", "--verbosity", "4", "--log-colors", "off",
            "--device", device, "--gpu-layers", "all" if backend == "vulkan" else "0",
        ]
        self.process = subprocess.Popen(
            args, cwd=server.parent, stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if progress:
            progress(f"正在加载 Qwen3-0.6B · {('Vulkan GPU · ' + label) if backend == 'vulkan' else 'CPU'}…")
        if not self._wait_ready(self.process, time.monotonic() + timeout, cancel):
            self.stop()
            raise LocalModelError("本地推理服务未能启动；请查看 logs/llama-local-*.log。")
        if backend == "vulkan":
            # Do not label a GPU route successful unless llama.cpp confirms offloaded layers.
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if not re.search(r"offloaded\s+[1-9]\d*/\d+\s+layers to GPU", text):
                self.stop()
                raise LocalModelError("Vulkan 已响应，但日志没有确认模型层实际卸载到 GPU。")
        self.backend = backend
        self.device = label if backend == "vulkan" else ""

    def start(self, *, cancel=None, progress=None, force_cpu: bool = False, timeout: float = 90):
        with self._lock:
            if self.process and self.process.poll() is None:
                return {"endpoint": self.endpoint, "model": "Qwen3-0.6B", **self.route_info()}
            self.stop()
            if not self.installed():
                raise LocalModelError("未安装本地模型包。请先安装 Qwen3 Agent 模型包，再重启工作台。")
            stamp = (self.model_path.stat().st_size, self.model_path.stat().st_mtime_ns)
            if stamp != self._verified:
                digest = hashlib.sha256()
                with self.model_path.open("rb") as stream:
                    for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        if cancel and cancel.is_set():
                            raise LocalModelError("已取消模型校验。")
                        digest.update(block)
                if digest.hexdigest() != MODEL_SHA256:
                    raise LocalModelError("Qwen 模型 SHA-256 校验失败，已停止启动。")
                self._verified = stamp

            self.fallback_reason = ""
            if not force_cpu:
                if progress:
                    progress("正在检测可用 Vulkan GPU…")
                candidate = self._select_gpu()
                if candidate:
                    device, label = candidate
                    try:
                        self._launch("vulkan", self.vulkan_server, device, label, cancel, progress, timeout)
                        return {"endpoint": self.endpoint, "model": "Qwen3-0.6B", **self.route_info()}
                    except (OSError, LocalModelError) as exc:
                        if cancel and cancel.is_set():
                            raise
                        self.fallback_reason = str(exc)
                        self.stop()
                        if progress:
                            progress("GPU 启动失败，正在回退 CPU…")
            if cancel and cancel.is_set():
                raise LocalModelError("已取消模型启动。")
            self._launch("cpu", self.cpu_server, progress=progress, cancel=cancel, timeout=timeout)
            return {"endpoint": self.endpoint, "model": "Qwen3-0.6B", **self.route_info()}

    def route_info(self) -> dict:
        return {"backend": self.backend, "device": self.device, "fallback_reason": self.fallback_reason}

    def stop(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.port = None
        self.backend = "stopped"
        self.device = ""
        if self._log:
            self._log.close()
            self._log = None
