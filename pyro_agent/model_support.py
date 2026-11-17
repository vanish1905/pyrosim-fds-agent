"""OpenCode model configuration and OpenAI-compatible local model checks."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .opencode_environment import configure_opencode_environment
from .config import mcp_server_command


def generated_config_path(app_root: Path) -> Path:
    return app_root / ".runtime" / "opencode.generated.json"


def write_runtime_config(app_root: Path, endpoint: str = "", model_name: str = "",
                         tools_verified: bool = False) -> Path:
    """Write an isolated config for OpenCode; keep project/user config untouched."""
    base_path = app_root / "opencode.json"
    config = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    # The checked-in config may have been copied from another checkout. Keep
    # the generated runtime config bound to this checkout's venv and server.
    mcp = config.setdefault("mcp", {})
    if isinstance(mcp, dict):
        mcp["pyrosim_fds"] = {
            "type": "local",
            "command": mcp_server_command(app_root),
            "cwd": str(app_root.resolve()),
            "enabled": True,
            "timeout": 30000,
        }
    instructions = config.get("instructions", [])
    if isinstance(instructions, list):
        config["instructions"] = [
            str((app_root / item).resolve()) if isinstance(item, str) and not Path(item).is_absolute() else item
            for item in instructions
        ]
    endpoint = endpoint.strip().rstrip("/")
    if endpoint and not endpoint.endswith("/v1"):
        endpoint += "/v1"
    model_name = model_name.strip()
    if endpoint and model_name:
        model_key = re.sub(r"[^A-Za-z0-9._-]+", "-", model_name).strip("-") or "local-model"
        providers = config.setdefault("providers", {})
        providers["pyro-local"] = {
            "name": "本地模型",
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": endpoint},
            "models": {
                model_key: {
                    "name": model_name,
                    "modelID": model_name,
                    "capabilities": {"tools": bool(tools_verified), "input": ["text"], "output": ["text"]},
                    "limit": {"context": 32768, "output": 4096},
                }
            },
        }
    path = generated_config_path(app_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def model_ref(model_name: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", model_name.strip()).strip("-") or "local-model"
    return f"pyro-local/{key}"


def list_opencode_models(executable: str, app_root: Path, endpoint: str = "",
                         model_name: str = "", tools_verified: bool = False) -> list[str]:
    config = write_runtime_config(app_root, endpoint, model_name, tools_verified)
    import os
    env = configure_opencode_environment(os.environ)
    env["OPENCODE_CONFIG"] = str(config)
    env["OPENCODE_CONFIG_CONTENT"] = config.read_text(encoding="utf-8")
    env["PYTHONUTF8"] = "1"
    result = subprocess.run([executable, "models"], cwd=app_root, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail[-2000:] or f"OpenCode models 退出码 {result.returncode}")
    models = []
    for line in result.stdout.splitlines():
        value = line.strip().strip("* ")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+", value):
            models.append(value)
    if endpoint.strip() and model_name.strip():
        local_ref = model_ref(model_name)
        if local_ref not in models:
            models.append(local_ref)
    return list(dict.fromkeys(models))


def check_local_model(endpoint: str, model_name: str, timeout: float = 15) -> dict:
    base = endpoint.strip().rstrip("/")
    name = model_name.strip()
    if not base or not name:
        raise ValueError("请填写本地服务地址和模型名称。")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("为保护本机服务，当前仅允许连接 localhost / 127.0.0.1 的本地模型服务。")
    url = base if base.endswith("/v1") else base + "/v1"
    request = urllib.request.Request(url + "/models", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            inventory = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法连接模型服务 {url}/models：{exc}") from exc
    ids = [str(item.get("id", "")) for item in inventory.get("data", []) if item.get("id")]
    matched = name in ids
    tool_payload = {
        "model": name,
        "messages": [{"role": "user", "content": "Call the report_ready tool now."}],
        "tools": [{"type": "function", "function": {
            "name": "report_ready", "description": "Report that the model can call tools.",
            "parameters": {"type": "object", "properties": {"ready": {"type": "boolean"}},
                           "required": ["ready"], "additionalProperties": False},
        }}],
        "tool_choice": "required",
        "temperature": 0,
        "max_tokens": 48,
    }
    request = urllib.request.Request(url + "/chat/completions", data=json.dumps(tool_payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    tool_supported = False
    tool_detail = "模型未返回 tool_calls。"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            completion = json.loads(response.read().decode("utf-8"))
        choices = completion.get("choices", [])
        if choices:
            calls = (choices[0].get("message") or {}).get("tool_calls") or []
            tool_supported = bool(calls)
            tool_detail = "成功返回结构化 tool_calls。" if tool_supported else tool_detail
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", errors="replace")
        tool_detail = f"服务支持模型列表，但工具调用探测失败：HTTP {exc.code} {body}"
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        tool_detail = f"模型列表可访问，工具调用探测失败：{exc}"
    return {"endpoint": url, "model": name, "available_models": ids,
            "model_found": matched, "tool_call_supported": tool_supported, "tool_call_detail": tool_detail}


def local_workflow_decision(endpoint: str, model_name: str, phase: str, observation: dict,
                            timeout: float = 45) -> dict:
    """Ask a local model for one constrained continue/stop decision.

    The model never chooses MCP tools or their arguments. The application owns
    the phase graph and invokes only its fixed, allowlisted workflow steps.
    """
    base = endpoint.strip().rstrip("/")
    name = model_name.strip()
    if not base or not name:
        raise ValueError("请先配置本地模型服务地址和模型名称。")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("本地工作流仅连接 localhost / 127.0.0.1 模型服务。")
    url = base if base.endswith("/v1") else base + "/v1"
    tool = {"type": "function", "function": {
        "name": "advance_workflow",
        "description": "Choose whether to continue the fixed workflow or stop. Never run FDS.",
        "parameters": {
            "type": "object",
            "properties": {"decision": {"type": "string", "enum": ["continue", "stop"]},
                           "reason": {"type": "string"}},
            "required": ["decision"], "additionalProperties": False,
        },
    }}
    phase_labels = {
        "检查本机求解环境后": "local environment check",
        "示例预检后": "case preflight",
        "求解完成后": "FDS run",
        "theng 后处理完成后": "theng post-processing",
    }
    payload = {
        "model": name,
        "messages": [
            {"role": "system", "content": (
                "You are a safety-gated workflow judge. Call advance_workflow exactly once. "
                "Continue only when the reported checks passed and the next step remains in this fixed workflow. "
                "Never start FDS; the app requires separate user approval before solving."
            )},
            {"role": "user", "content": json.dumps({
                "completed_phase": phase_labels.get(phase, phase),
                "observation": observation,
            }, ensure_ascii=False)},
        ],
        "tools": [tool],
        # The existing connection probe uses this mode, so models are only
        # enabled for the phase gate after the server has demonstrated it.
        "tool_choice": "required",
        "temperature": 0,
        "max_tokens": 96,
    }
    request = urllib.request.Request(url + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            completion = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"本地模型阶段判断失败：{exc}") from exc
    try:
        call = completion["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(call["function"]["arguments"])
        decision = args.get("decision")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("本地模型没有返回有效的 continue/stop 阶段判断。") from exc
    if decision not in {"continue", "stop"}:
        raise RuntimeError("本地模型返回了不支持的阶段判断；流程已停止。")
    return {"decision": decision, "reason": str(args.get("reason", ""))[:300]}
