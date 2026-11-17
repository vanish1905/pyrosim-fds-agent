param(
    [string]$Python = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pythonExe = (Resolve-Path -LiteralPath $Python).Path
foreach ($relative in @('build\pyinstaller', 'build\setup-pyinstaller', 'build\iexpress', 'dist\package', 'dist\PyroSim-FDS-Agent-Setup.exe', 'installer\payload')) {
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

$distPath = Join-Path $root 'dist\package'
$workPath = Join-Path $root 'build\pyinstaller'
& $pythonExe -m PyInstaller --noconfirm --onedir --console `
    --name PyroSimAgent --icon (Join-Path $root 'assets\pyrosim_agent.ico') `
    --distpath $distPath --workpath $workPath --specpath $workPath `
    --add-data "$(Join-Path $root 'AGENTS.md');." `
    --add-data "$(Join-Path $root 'opencode.json');." `
    --collect-submodules pyro_agent --collect-submodules theng --hidden-import pyro_agent.mcp_server `
    (Join-Path $root 'run_workbench.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

$builtApp = Join-Path $distPath 'PyroSimAgent'
$payload = Join-Path $root 'installer\payload'
New-Item -ItemType Directory -Path $payload -Force | Out-Null
Copy-Item -Path (Join-Path $builtApp '*') -Destination $payload -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root 'launch-workbench.vbs') -Destination $payload -Force
Copy-Item -LiteralPath (Join-Path $root 'AGENTS.md') -Destination $payload -Force
Copy-Item -LiteralPath (Join-Path $root 'opencode.json') -Destination $payload -Force
Copy-Item -LiteralPath (Join-Path $root 'assets') -Destination $payload -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root 'installer\uninstall.ps1') -Destination $payload -Force
Copy-Item -LiteralPath (Join-Path $root 'installer\uninstall.vbs') -Destination $payload -Force
$demo = Join-Path $root 'examples\temperature_methane_demo'
if (Test-Path -LiteralPath $demo -PathType Container) {
    Copy-Item -LiteralPath $demo -Destination (Join-Path $payload 'examples\temperature_methane_demo') -Recurse -Force
}

$setup = Join-Path $root 'dist\PyroSim-FDS-Agent-Setup.exe'
$packageSource = Join-Path $root 'build\iexpress'
$setupWorkPath = Join-Path $root 'build\setup-pyinstaller'
New-Item -ItemType Directory -Path $packageSource -Force | Out-Null
$archive = Join-Path $packageSource 'app-payload.zip'
Compress-Archive -Path (Join-Path $payload '*') -DestinationPath $archive -CompressionLevel Optimal
& $pythonExe -m PyInstaller --noconfirm --onefile --console `
    --name PyroSim-FDS-Agent-Setup --icon (Join-Path $root 'assets\pyrosim_agent.ico') `
    --distpath (Join-Path $root 'dist') --workpath $setupWorkPath --specpath $setupWorkPath `
    --add-data "$(Join-Path $packageSource 'app-payload.zip');." `
    --add-data "$(Join-Path $root 'installer\install.ps1');." `
    (Join-Path $root 'installer\setup_bootstrap.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to build the setup executable.' }
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'The setup executable was not created.' }
Get-Item -LiteralPath $setup | Select-Object FullName,Length,LastWriteTime
