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
    & $interpreter -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    [IO.File]::WriteAllText($stamp, $fingerprint)
}
$config = Join-Path $PSScriptRoot 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'config.example.json') -Destination $config
}
exit 0
