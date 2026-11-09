<# Start the local photo pilot without putting credentials in command arguments. #>
param([ValidateRange(1, 65535)][int]$Port = 8080)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Assert-PhotoPilotToken([string]$Token) {
    if ([string]::IsNullOrEmpty($Token) -or $Token.Length -lt 32 -or $Token.Length -gt 512 -or
        $Token -notmatch '\A[\x21-\x7e]+\z' -or @($Token.ToCharArray() | Select-Object -Unique).Count -lt 12) {
        throw 'The private photo pilot key is invalid.'
    }
    return $Token
}

function Get-PhotoPilotTokenFromStore([string]$SecretPath) {
    if ([string]::IsNullOrEmpty($SecretPath)) {
        $SecretPath = Join-Path $env:LOCALAPPDATA 'EventDeskPhotoPilot\credentials.bin'
    }
    $plain = $null
    $encrypted = $null
    $stream = $null
    try {
        Add-Type -AssemblyName System.Security
        $item = Get-Item -LiteralPath $SecretPath -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            $item.Length -lt 1 -or $item.Length -gt 65536) { throw 'Invalid storage.' }
        $stream = [IO.File]::Open($item.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $encrypted = New-Object byte[] 65537
        $count = 0
        while ($count -lt $encrypted.Length) {
            $read = $stream.Read($encrypted, $count, $encrypted.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
        if ($count -lt 1 -or $count -gt 65536) { throw 'Invalid storage size.' }
        $cipher = New-Object byte[] $count
        [Array]::Copy($encrypted, $cipher, $count)
        $entropy = [Text.Encoding]::UTF8.GetBytes('EventDeskPhotoPilot.Secrets.v1')
        $plain = [Security.Cryptography.ProtectedData]::Unprotect($cipher, $entropy,
            [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $settings = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
        return (Assert-PhotoPilotToken $settings.PilotAccessToken)
    } catch {
        throw 'Cannot read the private photo pilot key from Windows protected storage.'
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $plain) { [Array]::Clear($plain, 0, $plain.Length) }
        if ($null -ne $encrypted) { [Array]::Clear($encrypted, 0, $encrypted.Length) }
        $settings = $null
    }
}

function Get-PhotoPilotLaunchPlan([string]$ProjectRoot, [ValidateRange(1, 65535)][int]$Port) {
    if ([string]::IsNullOrWhiteSpace($ProjectRoot) -or -not [IO.Path]::IsPathRooted($ProjectRoot)) {
        throw 'An absolute project directory is required.'
    }
    $root = [IO.Path]::GetFullPath($ProjectRoot)
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $application = Join-Path $root 'app.py'
    if (-not (Test-Path -LiteralPath $application -PathType Leaf) -or
        -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'The local application or project Python environment is missing.'
    }
    return [pscustomobject]@{ Python=$python; Application=$application; BindAddress='0.0.0.0'; Port=$Port }
}

function Invoke-PhotoPilotPython($Plan) {
    & $Plan.Python $Plan.Application | Out-Host
    return $LASTEXITCODE
}

function Invoke-PhotoPilotServer([string]$ProjectRoot, [ValidateRange(1, 65535)][int]$Port = 8080) {
    $plan = Get-PhotoPilotLaunchPlan $ProjectRoot $Port
    $pilotToken = Get-PhotoPilotTokenFromStore
    $previous = @{}
    foreach ($name in @('PARTY_PHOTO_API_TOKEN', 'PARTY_HOST', 'PARTY_PORT', 'PARTY_OPEN_STAFF')) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        $env:PARTY_PHOTO_API_TOKEN = $pilotToken
        $env:PARTY_HOST = $plan.BindAddress
        $env:PARTY_PORT = [string]$plan.Port
        $env:PARTY_OPEN_STAFF = '1'
        return (Invoke-PhotoPilotPython $plan)
    } finally {
        foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
        $pilotToken = $null
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $photoProjectRoot = Split-Path -Parent $PSScriptRoot
    exit (Invoke-PhotoPilotServer $photoProjectRoot $Port)
}
