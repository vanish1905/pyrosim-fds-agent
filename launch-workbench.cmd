@echo off
setlocal
cd /d "%~dp0"
set "PYROSIM_AGENT_HOME=%LOCALAPPDATA%\PyroSimFdsAgent"
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" "%~dp0run_workbench.py"
  exit /b 0
)
echo Bundled Python runtime is missing. Reinstall the complete Agent package.
pause
exit /b 1
