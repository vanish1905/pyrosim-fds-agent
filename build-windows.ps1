param(
    [string]$Python = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pythonExe = (Resolve-Path -LiteralPath $Python).Path
foreach ($relative in @('build\setup-pyinstaller', 'build\iexpress', 'dist\PyroSim-FDS-Agent-Setup.exe', 'dist\PyroSim-FDS-Agent-Portable.zip', 'dist\PyroSim-FDS-Agent-Portable.zip.sha256', 'installer\payload')) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $target.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside project: $target"
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}

& $pythonExe -m pip install -r (Join-Path $root 'requirements-packaging.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing the packaging dependency failed.' }
& $pythonExe -m pip install -r (Join-Path $root 'requirements-ui.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing the optional Windows UI bridge dependencies failed.' }
& $pythonExe -m pip install -r (Join-Path $root 'requirements-results.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing the results post-processing dependencies failed.' }
& $pythonExe (Join-Path $root 'tools\make_icon.py')
if ($LASTEXITCODE -ne 0) { throw 'Generating the application icon failed.' }

$payload = Join-Path $root 'installer\payload'
& $pythonExe (Join-Path $root 'tools\package_portable.py') --output $payload
if ($LASTEXITCODE -ne 0) { throw 'Building or validating the portable Python runtime failed.' }

$setup = Join-Path $root 'dist\PyroSim-FDS-Agent-Setup.exe'
$packageSource = Join-Path $root 'build\iexpress'
$setupWorkPath = Join-Path $root 'build\setup-pyinstaller'
New-Item -ItemType Directory -Path $packageSource -Force | Out-Null
& $pythonExe -m PyInstaller --noconfirm --onefile --console `
    --name PyroSim-FDS-Agent-Setup --icon (Join-Path $root 'assets\pyrosim_agent.ico') `
    --distpath (Join-Path $root 'dist') --workpath $setupWorkPath --specpath $setupWorkPath `
    --add-data "$(Join-Path $packageSource 'app-payload.zip');." `
    --add-data "$(Join-Path $root 'installer\install.ps1');." `
    (Join-Path $root 'installer\setup_bootstrap.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to build the setup executable.' }
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'The setup executable was not created.' }
Get-Item -LiteralPath $setup,(Join-Path $root 'dist\PyroSim-FDS-Agent-Portable.zip') | Select-Object FullName,Length,LastWriteTime
