$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$appDir = Join-Path $env:LOCALAPPDATA 'Programs\PyroSim FDS Agent'
$archive = Join-Path $source 'app-payload.zip'

try {
    New-Item -ItemType Directory -Path $appDir -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $appDir -Force
    foreach ($staleName in @('PyroSimAgent.exe', 'launch-workbench.vbs', 'launch-workbench.ps1', 'uninstall.vbs')) {
        $stalePath = Join-Path $appDir $staleName
        if (Test-Path -LiteralPath $stalePath -PathType Leaf) {
            Remove-Item -LiteralPath $stalePath -Force
        }
    }
    $legacyInternal = Join-Path $appDir '_internal'
    if (Test-Path -LiteralPath $legacyInternal -PathType Container) {
        Remove-Item -LiteralPath $legacyInternal -Recurse -Force
    }

    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\PyroSim FDS Agent'
    New-Item -ItemType Directory -Path $programs -Force | Out-Null
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $launcher = Join-Path $appDir 'launch-workbench.cmd'
    foreach ($shortcutPath in @(
        (Join-Path $programs 'PyroSim FDS Agent.lnk'),
        (Join-Path $desktop 'PyroSim FDS Agent.lnk')
    )) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $launcher
        $shortcut.WorkingDirectory = $appDir
        $shortcut.IconLocation = (Join-Path $appDir 'assets\pyrosim_agent.ico') + ',0'
        $shortcut.Save()
    }

    $uninstallLink = $shell.CreateShortcut((Join-Path $programs '卸载 PyroSim FDS Agent.lnk'))
    $uninstallLink.TargetPath = Join-Path $appDir 'uninstall.cmd'
    $uninstallLink.WorkingDirectory = $appDir
    $uninstallLink.Save()

    $uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PyroSimFdsAgent'
    New-Item -Path $uninstallKey -Force | Out-Null
    Set-ItemProperty -Path $uninstallKey -Name DisplayName -Value 'PyroSim / FDS Agent'
    Set-ItemProperty -Path $uninstallKey -Name DisplayVersion -Value '1.0.9'
    Set-ItemProperty -Path $uninstallKey -Name Publisher -Value 'PyroSim / FDS Agent'
    Set-ItemProperty -Path $uninstallKey -Name InstallLocation -Value $appDir
    Set-ItemProperty -Path $uninstallKey -Name DisplayIcon -Value (Join-Path $appDir 'assets\pyrosim_agent.ico')
    $uninstallCmd = Join-Path $appDir 'uninstall.cmd'
    Set-ItemProperty -Path $uninstallKey -Name UninstallString -Value ('"' + (Join-Path $env:WINDIR 'System32\cmd.exe') + '" /c ""' + $uninstallCmd + '""')

    (New-Object -ComObject WScript.Shell).Popup('PyroSim / FDS Agent is installed. Start it from the Desktop or Start Menu shortcut.', 8, 'PyroSim / FDS Agent', 64) | Out-Null
} catch {
    (New-Object -ComObject WScript.Shell).Popup("Installation failed:`r`n$($_.Exception.Message)", 0, 'PyroSim / FDS Agent', 16) | Out-Null
    exit 1
}
