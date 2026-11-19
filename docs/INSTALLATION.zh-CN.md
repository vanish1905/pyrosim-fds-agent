# PyroSim / FDS Agent 安装指南（Windows）

## 组件说明

| 组件 | 获取方式 | 说明 |
| --- | --- | --- |
| PyroSim / FDS Agent 1.0.9 | 本项目公开 GitHub Release | Qt 工作台、MCP 服务、TEMPERATURE 示例和桌面/开始菜单快捷方式；新增 OBJ/STL 网格导入为 FDS GEOM，以及 SURF/VENT 字段添加/修改。 |
| FDS + Smokeview | 同一 Release | `FDS-6.11.1_SMV-6.11.2_win.exe`。此版本尚未完成项目适配器的版本验证，首次使用应先跑小算例。 |
| Qwen3-0.6B 离线模型包 | 同一 Release，可选 | 独立安装器，包含 GGUF 权重、llama.cpp CPU/Vulkan 运行时及许可证；不要求另装 LM Studio/Ollama。 |
| Qwen3-0.6B 原始 GGUF | 同一 Release，可选 | 仅权重文件，适合已有 LM Studio、Ollama 或其他推理服务的用户。 |
| PyroSim | Thunderhead 官方渠道 | 商业软件，不包含在本仓库或 Release 中；使用者需自行取得安装包和授权。 |
| OpenCode | OpenCode 官方渠道，可选 | 需要使用 OpenCode 对话时安装；工作台里的固定 TEMPERATURE 本地模型编排可单独使用本地模型阶段判断。 |

## 方式一：使用安装脚本（推荐）

脚本从公开 Release 下载附件并校验 SHA-256，不需要 GitHub CLI 或登录。先克隆公开仓库：

```powershell
git clone https://github.com/vanish1905/pyrosim-fds-agent.git
cd pyrosim-fds-agent
```

只安装 Agent：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install-components.ps1
```

同时下载并启动 FDS/Smokeview 安装程序，并安装独立的离线 Qwen 模型包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install-components.ps1 -InstallFds -InstallLocalModel
```

只下载文件、不启动安装器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install-components.ps1 -InstallFds -InstallLocalModel -DownloadOnly
```

安装包默认下载到 `%USERPROFILE%\Downloads\PyroSim-FDS-Agent`。需要更换目录时传入 `-DownloadDirectory 'E:\Installers\PyroSimAgent'`。Agent 和本地模型安装到当前 Windows 用户目录，不需要管理员权限；FDS 安装器可能会根据所选安装位置请求权限。

## 方式二：从 Release 手动安装

打开[项目 Releases](https://github.com/vanish1905/pyrosim-fds-agent/releases)，下载 `PyroSim-FDS-Agent-Setup.exe` 后双击并确认安装。安装器会创建桌面和开始菜单快捷方式。Agent 随包提供独立 Python 运行时，不要求目标电脑另装 Python；启动错误会记录到 `%LOCALAPPDATA%\PyroSimFdsAgent\logs\startup-error.log`。FDS 安装包和离线 Qwen 模型包按需下载。

也可以下载 `PyroSim-FDS-Agent-Portable.zip`，完整解压后运行 `launch-workbench.cmd`。新版不再用 VBScript 启动主程序；若仍有启动失败提示，请把 `%LOCALAPPDATA%\PyroSimFdsAgent\logs\startup-error.log` 发给维护者。若 Windows 安全提示拦截，请先核对 Release 中的 SHA-256，再按设备策略处理；此程序包没有 Authenticode 代码签名。

## 首次配置

1. 启动 **PyroSim / FDS Agent**，进入设置页，配置已安装的 FDS/Smokeview 可执行文件路径和算例工作目录，再保存并检查环境。
2. 如果要通过工作台执行本地 FDS 求解，先用随 Agent 提供的 TEMPERATURE 示例做预检和短时验证。启动正式求解仍会要求确认。
3. 如需 PyroSim UI 导入/导出功能，单独安装并授权 PyroSim，再在工作台设置其路径。不同 PyroSim 版本的 UI 能力可能不同。
4. 使用内置 Qwen 时，运行 `PyroSim-FDS-Agent-LocalModel-Setup.exe` 安装独立模型包。重新打开工作台，在“连接设置”点击“启动内置 Qwen3-0.6B”；工作台会自动探测 Vulkan GPU、启动本机推理服务、填写回环地址并检查模型列表和工具调用，关闭工作台时会停止该服务。模型包安装在 `%LOCALAPPDATA%\Programs\PyroSim FDS Agent\agent_runtime`。
5. 如已有 LM Studio、Ollama 或其他 GGUF 兼容运行时，可继续加载原始 `Qwen3-0.6B-Q8_0.gguf`，填写本地 OpenAI 兼容服务地址和服务返回的准确模型 ID，再执行连接及工具调用检查。
6. 如需 OpenCode 对话，另行安装 OpenCode，并在 Agent 设置中选择模型；OpenCode 的 API 账号和模型访问权限由其自身配置管理。

Qwen3-0.6B 是较小的模型，适合验证受约束的流程编排；复杂仿真需求仍需核对输入、求解日志和工程结果。当前的 PyroSim 原生 `.psm` 编辑能力不等同于 FDS 输入文件编辑。

## 卸载

从 Windows“已安装的应用”卸载，或从开始菜单打开“卸载 PyroSim FDS Agent”。卸载 Agent 会删除它的安装目录（包含单独安装的本地模型运行时）；工作流算例、FDS 和 PyroSim 需要按需单独管理。
