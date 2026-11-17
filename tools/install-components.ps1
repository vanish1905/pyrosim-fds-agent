param(
    [switch]$InstallFds,
    [switch]$DownloadQwen,
    [switch]$DownloadOnly,
    [string]$DownloadDirectory = (Join-Path $env:USERPROFILE 'Downloads\PyroSim-FDS-Agent')
)

$ErrorActionPreference = 'Stop'
$repo = 'vanish1905/pyrosim-fds-agent'
$release = 'installers-2026-11-17'
$agentAsset = 'PyroSim-FDS-Agent-Setup.exe'
$fdsAsset = 'FDS-6.11.1_SMV-6.11.2_win.exe'
$qwenAsset = 'Qwen3-0.6B-Q8_0.gguf'
$expectedHashes = @{
    'PyroSim-FDS-Agent-Setup.exe' = 'E65D996775F7C8E71A5750645E40D651117DCD6985BA18884D7079AC5BCDCEB1'
    'FDS-6.11.1_SMV-6.11.2_win.exe' = '302EC837CAAEA8CB990CECE085BEE2284A0338D64FAC0B77D46749A92AB12DFC'
    'Qwen3-0.6B-Q8_0.gguf' = '9465E63A22ADD5354D9BB4B99E90117043C7124007664907259BD16D043BB031'
}

function Set-ReleaseProxy {
    if (($env:HTTP_PROXY -or $env:http_proxy) -and ($env:HTTPS_PROXY -or $env:https_proxy)) { return }
    if (($env:HTTP_PROXY -or $env:http_proxy) -and -not ($env:HTTPS_PROXY -or $env:https_proxy)) {
        $env:HTTPS_PROXY = $env:HTTP_PROXY
        return
    }
    if (($env:HTTPS_PROXY -or $env:https_proxy) -and -not ($env:HTTP_PROXY -or $env:http_proxy)) {
        $env:HTTP_PROXY = $env:HTTPS_PROXY
        return
    }

    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    try {
        $settings = Get-ItemProperty -Path $key -ErrorAction Stop
        if (-not $settings.ProxyEnable -or -not $settings.ProxyServer) { return }
        $server = [string]$settings.ProxyServer
    } catch {
        return
    }

    $httpProxy = $server
    $httpsProxy = $server
    if ($server.Contains('=')) {
        $entries = @{}
        foreach ($part in $server.Split(';')) {
            $pair = $part.Split('=', 2)
            if ($pair.Count -eq 2 -and $pair[1].Trim()) { $entries[$pair[0].Trim().ToLowerInvariant()] = $pair[1].Trim() }
        }
        if ($entries.ContainsKey('http')) { $httpProxy = $entries['http'] }
        elseif ($entries.ContainsKey('https')) { $httpProxy = $entries['https'] }
        if ($entries.ContainsKey('https')) { $httpsProxy = $entries['https'] }
        elseif ($entries.ContainsKey('http')) { $httpsProxy = $entries['http'] }
    }

    foreach ($name in @('httpProxy', 'httpsProxy')) {
        $value = Get-Variable -Name $name -ValueOnly
        if ($value -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') { $value = 'http://' + $value }
        $authority = $value.Substring($value.IndexOf('://') + 3).Split('/')[0]
        if ($authority.Contains('@')) {
            throw 'The Windows proxy includes credentials. Set HTTP_PROXY and HTTPS_PROXY for this PowerShell session, then rerun the script.'
        }
        if ($name -eq 'httpProxy' -and -not ($env:HTTP_PROXY -or $env:http_proxy)) { $env:HTTP_PROXY = $value }
        if ($name -eq 'httpsProxy' -and -not ($env:HTTPS_PROXY -or $env:https_proxy)) { $env:HTTPS_PROXY = $value }
    }
}

function Get-ReleaseAsset([string]$Name) {
    Write-Host "Downloading $Name from private release $release ..."
    & gh release download $release --repo $repo --pattern $Name --dir $DownloadDirectory --clobber
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI could not download $Name. Check that gh is authenticated and has access to $repo." }

    $path = Join-Path $DownloadDirectory $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Downloaded asset is missing: $path" }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToUpperInvariant()
    if ($actualHash -ne $expectedHashes[$Name]) { throw "SHA-256 verification failed for $Name. Expected $($expectedHashes[$Name]), got $actualHash." }
    Write-Host "Verified $Name (SHA-256 $actualHash)"
    return $path
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI (gh) is required. Install it from https://cli.github.com/ and authenticate with: gh auth login --hostname github.com --web --scopes repo'
}

Set-ReleaseProxy
& gh auth status --hostname github.com *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'GitHub CLI is not authenticated. Run: gh auth login --hostname github.com --web --scopes repo'
}

New-Item -ItemType Directory -Path $DownloadDirectory -Force | Out-Null
$agentPath = Get-ReleaseAsset $agentAsset
$fdsPath = $null
$qwenPath = $null

if ($InstallFds) { $fdsPath = Get-ReleaseAsset $fdsAsset }
if ($DownloadQwen) {
    $modelDirectory = Join-Path $env:LOCALAPPDATA 'PyroSimFdsAgent\models'
    New-Item -ItemType Directory -Path $modelDirectory -Force | Out-Null
    $qwenPath = Get-ReleaseAsset $qwenAsset
    $modelDestination = Join-Path $modelDirectory $qwenAsset
    if (([IO.Path]::GetFullPath($qwenPath)) -ne ([IO.Path]::GetFullPath($modelDestination))) {
        Copy-Item -LiteralPath $qwenPath -Destination $modelDestination -Force
        $qwenPath = $modelDestination
    }
}

if (-not $DownloadOnly) {
    Write-Host 'Launching the per-user Agent installer. Follow its confirmation prompt.'
    $process = Start-Process -FilePath $agentPath -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Agent installer exited with code $($process.ExitCode)." }

    if ($InstallFds) {
        Write-Host 'Launching the FDS/Smokeview installer. Complete its interactive setup.'
        $process = Start-Process -FilePath $fdsPath -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "FDS installer exited with code $($process.ExitCode)." }
    }
}

Write-Host ''
Write-Host "Agent installer: $agentPath"
if ($fdsPath) { Write-Host "FDS installer:   $fdsPath" }
if ($qwenPath) { Write-Host "Qwen model:      $qwenPath" }
if ($DownloadOnly) { Write-Host 'Download-only mode: no installer was launched.' }
Write-Host 'PyroSim and the local model runtime are separate prerequisites; see docs/INSTALLATION.zh-CN.md.'
