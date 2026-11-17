param(
    [switch]$InstallFds,
    [switch]$DownloadQwen,
    [switch]$DownloadPortable,
    [switch]$InstallLocalModel,
    [switch]$DownloadOnly,
    [string]$DownloadDirectory = (Join-Path $env:USERPROFILE 'Downloads\PyroSim-FDS-Agent')
)

$ErrorActionPreference = 'Stop'
$repo = 'vanish1905/pyrosim-fds-agent'
$release = 'installers-2026-11-17'
$releaseBase = "https://github.com/$repo/releases/download/$release"
$agentAsset = 'PyroSim-FDS-Agent-Setup.exe'
$portableAsset = 'PyroSim-FDS-Agent-Portable.zip'
$fdsAsset = 'FDS-6.11.1_SMV-6.11.2_win.exe'
$qwenAsset = 'Qwen3-0.6B-Q8_0.gguf'
$localModelAsset = 'PyroSim-FDS-Agent-LocalModel-Setup.exe'
$expectedHashes = @{
    'PyroSim-FDS-Agent-Setup.exe' = '744F4C7D8D307B44EA89140D6BA38ED405340A6230CF6298CA55EE0ADB29A4D5'
    'PyroSim-FDS-Agent-Portable.zip' = 'F2844D5B6FBADE33E04D8563BB17E03033EAE967CFEBCA3CEF9318CA6CDCF94D'
    'FDS-6.11.1_SMV-6.11.2_win.exe' = '302EC837CAAEA8CB990CECE085BEE2284A0338D64FAC0B77D46749A92AB12DFC'
    'Qwen3-0.6B-Q8_0.gguf' = '9465E63A22ADD5354D9BB4B99E90117043C7124007664907259BD16D043BB031'
    'PyroSim-FDS-Agent-LocalModel-Setup.exe' = '655F4A6F8CA2C8AEEB98A690CC7D7A9C0E154992EC7E5A731C8FA5A4B8D64BC7'
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
    Write-Host "Downloading $Name from the public release ..."
    $path = Join-Path $DownloadDirectory $Name
    $uri = "$releaseBase/$([Uri]::EscapeDataString($Name))"
    try {
        Invoke-WebRequest -Uri $uri -OutFile $path -UseBasicParsing
    } catch {
        throw "Could not download $Name from $uri. Check the network connection and try again. $($_.Exception.Message)"
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Downloaded asset is missing: $path" }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToUpperInvariant()
    if ($actualHash -ne $expectedHashes[$Name]) { throw "SHA-256 verification failed for $Name. Expected $($expectedHashes[$Name]), got $actualHash." }
    Write-Host "Verified $Name (SHA-256 $actualHash)"
    return $path
}

Set-ReleaseProxy

New-Item -ItemType Directory -Path $DownloadDirectory -Force | Out-Null
$agentPath = Get-ReleaseAsset $agentAsset
$portablePath = $null
$fdsPath = $null
$qwenPath = $null
$localModelPath = $null

if ($InstallFds) { $fdsPath = Get-ReleaseAsset $fdsAsset }
if ($DownloadPortable) { $portablePath = Get-ReleaseAsset $portableAsset }
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
if ($InstallLocalModel) { $localModelPath = Get-ReleaseAsset $localModelAsset }

if (-not $DownloadOnly) {
    Write-Host 'Launching the per-user Agent installer. Follow its confirmation prompt.'
    $process = Start-Process -FilePath $agentPath -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Agent installer exited with code $($process.ExitCode)." }

    if ($InstallFds) {
        Write-Host 'Launching the FDS/Smokeview installer. Complete its interactive setup.'
        $process = Start-Process -FilePath $fdsPath -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "FDS installer exited with code $($process.ExitCode)." }
    }

    if ($InstallLocalModel) {
        Write-Host 'Launching the offline Qwen/llama.cpp model installer. Complete its confirmation prompt.'
        $process = Start-Process -FilePath $localModelPath -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Local model installer exited with code $($process.ExitCode)." }
    }
}

Write-Host ''
Write-Host "Agent installer: $agentPath"
if ($fdsPath) { Write-Host "FDS installer:   $fdsPath" }
if ($portablePath) { Write-Host "Portable bundle: $portablePath" }
if ($qwenPath) { Write-Host "Qwen model:      $qwenPath" }
if ($localModelPath) { Write-Host "Local model:     $localModelPath" }
if ($DownloadOnly) { Write-Host 'Download-only mode: no installer was launched.' }
Write-Host 'PyroSim is a separate prerequisite; see docs/INSTALLATION.zh-CN.md.'
