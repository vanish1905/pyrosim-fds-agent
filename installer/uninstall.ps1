$ErrorActionPreference = 'SilentlyContinue'
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\PyroSim FDS Agent'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'PyroSim FDS Agent.lnk'
Remove-Item -LiteralPath $desktopLink -Force
Remove-Item -LiteralPath $programs -Recurse -Force
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PyroSimFdsAgent' -Recurse -Force
$deleteCommand = "Start-Sleep -Seconds 2; Remove-Item -LiteralPath '$($appDir.Replace("'", "''"))' -Recurse -Force"
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($deleteCommand))
Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encodedCommand)
