$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exitCodePath = Join-Path $root 'data\eventdesk.exitcode'
Set-Location -LiteralPath $root

try {
    & python app.py
    $exitCode = $LASTEXITCODE
} catch {
    $exitCode = 1
    Write-Error $_
} finally {
    Set-Content -LiteralPath $exitCodePath -Value $exitCode -Encoding ascii
}

exit $exitCode
