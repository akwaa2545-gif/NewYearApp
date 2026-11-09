$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$launcher = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\run-photo-pilot.ps1'
. $launcher
$script:checks = 0
function Assert-Pilot([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }; $script:checks++
}
function Assert-PilotThrows([scriptblock]$Action, [string]$Message) {
    $caught = $false
    try { & $Action | Out-Null } catch { $caught = $true }
    Assert-Pilot $caught $Message
}
$testToken = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/ABCD'
Assert-Pilot ((Assert-PhotoPilotToken $testToken) -eq $testToken) 'Valid token should be returned.'
foreach ($bad in @($null, '', ('a' * 64), ('ABCDEF012345' * 43), ($testToken + "`n"), (' ' + $testToken), ([string][char]0x100 + $testToken))) {
    Assert-PilotThrows { Assert-PhotoPilotToken $bad } 'Malformed or weak token must fail.'
}
$tempRoot = Join-Path $env:TEMP ('eventdesk-launch-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
    Add-Type -AssemblyName System.Security
    $entropy = [Text.Encoding]::UTF8.GetBytes('EventDeskPhotoPilot.Secrets.v1')
    $secretPath = Join-Path $tempRoot 'credentials.bin'
    function Save-TestSecret([string]$Json) {
        $plain = [Text.Encoding]::UTF8.GetBytes($Json)
        $cipher = [Security.Cryptography.ProtectedData]::Protect($plain, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        [IO.File]::WriteAllBytes($secretPath, $cipher)
    }
    Save-TestSecret ('{"PilotAccessToken":"' + $testToken + '","ApiToken":"unrelated"}')
    Assert-Pilot ((Get-PhotoPilotTokenFromStore $secretPath) -eq $testToken) 'Read DPAPI CurrentUser pilot token only.'
    Save-TestSecret '{"ApiToken":"unrelated"}'
    Assert-PilotThrows { Get-PhotoPilotTokenFromStore $secretPath } 'Missing pilot key must fail.'
    Save-TestSecret '{"PilotAccessToken":"short"}'
    Assert-PilotThrows { Get-PhotoPilotTokenFromStore $secretPath } 'Invalid protected key must fail.'
    [IO.File]::WriteAllText($secretPath, 'not-DPAPI')
    Assert-PilotThrows { Get-PhotoPilotTokenFromStore $secretPath } 'Unprotected content must fail.'
    [IO.File]::WriteAllBytes($secretPath, (New-Object byte[] 65537))
    Assert-PilotThrows { Get-PhotoPilotTokenFromStore $secretPath } 'Oversized secret storage must fail.'
    Assert-PilotThrows { Get-PhotoPilotTokenFromStore (Join-Path $tempRoot 'missing.bin') } 'Missing storage must fail.'
    Assert-PilotThrows { Get-PhotoPilotTokenFromStore $tempRoot } 'Directory storage must fail.'
    Assert-PilotThrows { Get-PhotoPilotLaunchPlan $tempRoot 8080 } 'Missing application must fail.'
    New-Item -ItemType Directory -Path (Join-Path $tempRoot '.venv\Scripts') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $tempRoot 'app.py'), '# fixture only')
    Assert-PilotThrows { Get-PhotoPilotLaunchPlan $tempRoot 8080 } 'Missing Python must fail.'
    [IO.File]::WriteAllText((Join-Path $tempRoot '.venv\Scripts\python.exe'), 'fixture never executed')
    $plan = Get-PhotoPilotLaunchPlan $tempRoot 8080
    Assert-Pilot ([IO.Path]::IsPathRooted($plan.Python) -and [IO.Path]::IsPathRooted($plan.Application)) 'Runtime paths must be absolute.'
    Assert-Pilot ($plan.BindAddress -eq '0.0.0.0' -and $plan.Port -eq 8080) 'Keep LAN default on port 8080.'
    Assert-PilotThrows { Get-PhotoPilotLaunchPlan $tempRoot 0 } 'Invalid port must fail.'
    Assert-PilotThrows { Get-PhotoPilotLaunchPlan $tempRoot 65536 } 'Invalid high port must fail.'

    $original = @{}
    foreach ($name in @('PARTY_PHOTO_API_TOKEN', 'PARTY_HOST', 'PARTY_PORT', 'PARTY_OPEN_STAFF')) { $original[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        function Get-PhotoPilotTokenFromStore { param([string]$SecretPath) return $testToken }
        $script:childInvoked = $false
        function Invoke-PhotoPilotPython {
            param($Plan)
            $script:childInvoked = $true
            Assert-Pilot ($env:PARTY_PHOTO_API_TOKEN -eq $testToken) 'Child must receive token through environment.'
            Assert-Pilot ($env:PARTY_HOST -eq '0.0.0.0' -and $env:PARTY_PORT -eq '8181') 'Child must receive configured LAN port.'
            Assert-Pilot ($env:PARTY_OPEN_STAFF -eq '1') 'Local pilot must allow the authorized direct staff page.'
            Assert-Pilot ($Plan.Application -eq [IO.Path]::GetFullPath((Join-Path $tempRoot 'app.py'))) 'Child receives only validated runtime plan.'
            return 7
        }
        $env:PARTY_PHOTO_API_TOKEN = 'previous'; $env:PARTY_HOST = '127.0.0.7'; $env:PARTY_OPEN_STAFF = 'sentinel'; Remove-Item Env:\PARTY_PORT -ErrorAction SilentlyContinue
        Assert-Pilot ((Invoke-PhotoPilotServer $tempRoot 8181) -eq 7) 'Preserve child exit code.'
        Assert-Pilot $script:childInvoked 'Expected child invocation.'
        Assert-Pilot ($env:PARTY_PHOTO_API_TOKEN -eq 'previous' -and $env:PARTY_HOST -eq '127.0.0.7' -and $null -eq [Environment]::GetEnvironmentVariable('PARTY_PORT', 'Process')) 'Restore previous and absent environment values.'
        Assert-Pilot ($env:PARTY_OPEN_STAFF -eq 'sentinel') 'Restore previous staff access setting after success.'
        Remove-Item Env:\PARTY_OPEN_STAFF
        Assert-Pilot ((Invoke-PhotoPilotServer $tempRoot 8181) -eq 7) 'Direct staff access should also work when previously unset.'
        Assert-Pilot ($null -eq [Environment]::GetEnvironmentVariable('PARTY_OPEN_STAFF', 'Process')) 'Restore absent staff access setting after success.'
        $env:PARTY_OPEN_STAFF = 'failure-sentinel'
        function Invoke-PhotoPilotPython { param($Plan) throw 'simulated child startup failure' }
        Assert-PilotThrows { Invoke-PhotoPilotServer $tempRoot 8181 } 'Child failure should propagate.'
        Assert-Pilot ($env:PARTY_PHOTO_API_TOKEN -eq 'previous' -and $env:PARTY_HOST -eq '127.0.0.7' -and $null -eq [Environment]::GetEnvironmentVariable('PARTY_PORT', 'Process')) 'Restore environment even after failure.'
        Assert-Pilot ($env:PARTY_OPEN_STAFF -eq 'failure-sentinel') 'Restore previous staff access setting after failure.'
        Remove-Item Env:\PARTY_OPEN_STAFF
        Assert-PilotThrows { Invoke-PhotoPilotServer $tempRoot 8181 } 'Child failure should also restore an absent staff setting.'
        Assert-Pilot ($null -eq [Environment]::GetEnvironmentVariable('PARTY_OPEN_STAFF', 'Process')) 'Restore absent staff access setting after failure.'
        function Get-PhotoPilotTokenFromStore { param([string]$SecretPath) throw 'simulated unavailable storage' }
        Assert-PilotThrows { Invoke-PhotoPilotServer $tempRoot 8181 } 'Missing secret must fail before launch.'
        Assert-Pilot ($env:PARTY_PHOTO_API_TOKEN -eq 'previous') 'Missing secrets must preserve caller environment.'
    } finally {
        foreach ($name in $original.Keys) { [Environment]::SetEnvironmentVariable($name, $original[$name], 'Process') }
    }
    $text = [IO.File]::ReadAllText($launcher)
    Assert-Pilot ($text -notmatch '(?im)^\s*(Write-(Host|Output|Verbose|Debug)|echo).*\$(token|pilotToken|secrets)') 'Launcher must not print secrets.'
    Assert-Pilot ($text -notmatch 'Start-Process|--token|ExecutionPolicy\s+Bypass') 'No secret-bearing command arguments or policy bypass.'
} finally {
    $resolved = [IO.Path]::GetFullPath($tempRoot)
    $expectedParent = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^eventdesk-launch-test-[0-9a-f]{32}$') { throw 'Unsafe test cleanup path.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
Write-Output "Photo pilot launcher checks passed: $script:checks; no application started."
