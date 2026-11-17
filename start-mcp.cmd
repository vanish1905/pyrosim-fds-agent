@echo off
setlocal
set "ROOT=%~dp0"
set "PYROSIM_AGENT_HOME=%LOCALAPPDATA%\PyroSimFdsAgent"
if exist "%ROOT%runtime\python.exe" (
  "%ROOT%runtime\python.exe" -m pyro_agent.mcp_server
  exit /b %ERRORLEVEL%
)
"%ROOT%.venv\Scripts\python.exe" -m pyro_agent.mcp_server
