param(
    [string]$Origin = 'https://127.0.0.1:8080'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exitCodePath = Join-Path $root 'data\cloudflared.exitcode'
$cloudflared = (Get-Command cloudflared.exe -ErrorAction Stop).Source

try {
    & $cloudflared tunnel --url $Origin --no-tls-verify --loglevel info
    $exitCode = $LASTEXITCODE
} catch {
    $exitCode = 1
    Write-Error $_
} finally {
    Set-Content -LiteralPath $exitCodePath -Value $exitCode -Encoding ascii
}

exit $exitCode
