param([switch]$Update)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$interpreter = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $interpreter)) {
    & py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$requirements = Join-Path $PSScriptRoot 'requirements.txt'
$stamp = Join-Path $PSScriptRoot '.venv\requirements.sha256'
$fingerprint = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash + ':' + (Get-Item -LiteralPath $interpreter).LastWriteTimeUtc.Ticks
if ($Update -or -not (Test-Path -LiteralPath $stamp) -or ([IO.File]::ReadAllText($stamp).Trim() -ne $fingerprint)) {
    $pipArguments = @('-m', 'pip', 'install', '-r', $requirements)
    $proxySettings = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($proxySettings.ProxyEnable -eq 1 -and $proxySettings.ProxyServer) {
        $proxyServer = $proxySettings.ProxyServer.Trim()
        # Windows supports either a single proxy or per-protocol entries.
        if ($proxyServer -match '=') {
            $protocolProxies = @{}
            foreach ($entry in $proxyServer.Split(';')) {
                $parts = $entry.Split('=', 2)
                if ($parts.Length -eq 2) {
                    $protocolProxies[$parts[0].Trim()] = $parts[1].Trim()
                }
            }
            $proxyServer = $protocolProxies['https']
            if (-not $proxyServer) { $proxyServer = $protocolProxies['http'] }
        }
        if ($proxyServer) {
            if ($proxyServer -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') {
                $proxyServer = 'http://' + $proxyServer
            }
            Write-Host 'Using Windows system proxy to download dependencies.'
            $pipArguments += @('--proxy', $proxyServer)
        }
    }
    & $interpreter @pipArguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    [IO.File]::WriteAllText($stamp, $fingerprint)
}
$config = Join-Path $PSScriptRoot 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'config.example.json') -Destination $config
}
exit 0
