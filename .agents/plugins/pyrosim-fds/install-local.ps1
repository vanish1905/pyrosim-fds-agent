$ErrorActionPreference = 'Stop'
$PluginRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $PluginRoot '..\..\..')).Path
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Project virtual environment was not found: $PythonExe"
}
$Mcp = @{
    mcp_servers = @{
        pyrosim_fds = @{
            command = $PythonExe
            args = @('-m', 'pyro_agent.mcp_server')
            cwd = $ProjectRoot
        }
    }
}
$Json = $Mcp | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText((Join-Path $PluginRoot '.mcp.json'), $Json, [System.Text.UTF8Encoding]::new($false))
Write-Output "Configured local MCP server for $ProjectRoot"
