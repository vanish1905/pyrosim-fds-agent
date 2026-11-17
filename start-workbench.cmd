@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
"%ROOT%.venv\Scripts\python.exe" -m pyro_agent.desktop
