& {
$ErrorActionPreference = 'Stop'
function Convert-PhotoSshConfig {
    param([string]$Text, [string]$TargetUser = 'example\photo-operator')
    if ($TargetUser -cne 'example\photo-operator') { throw 'Only the approved SSH account is supported.' }
    if ($Text -match '(?im)^[ \t]*#[ \t]*EventDesk per-user SSH account[ \t]*\r?$') {
        throw 'The EventDesk account rule already exists; inspect it before making further changes.'
    }
    $photoPrefix = "SyslogFacility LOCAL0`r`nLogLevel DEBUG3`r`n"
    while ($Text.StartsWith($photoPrefix, [System.StringComparison]::Ordinal)) {
        $Text = $Text.Substring($photoPrefix.Length)
    }
    $photoNewline = if ($Text.Contains("`r`n")) { "`r`n" } elseif ($Text.Contains("`n")) { "`n" } else { "`r`n" }
    $photoRule = '# EventDesk per-user SSH account' + $photoNewline +
        'Match User example\photo-operator' + $photoNewline +
        '    AuthorizedKeysFile .ssh/authorized_keys' + $photoNewline
    $photoFirstMatch = [regex]::Match($Text, '(?im)^[ \t]*Match(?:[ \t]|$)')
    if ($photoFirstMatch.Success) { return $Text.Insert($photoFirstMatch.Index, $photoRule) }
    $photoSeparator = if ($Text.Length -gt 0 -and -not $Text.EndsWith("`n")) { $photoNewline } else { '' }
    return $Text + $photoSeparator + $photoRule
}

if ([System.Net.Dns]::GetHostName() -ine 'PHOTO-API-HOST') { throw 'This block is only for PHOTO-API-HOST.' }
$photoIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$photoPrincipal = [System.Security.Principal.WindowsPrincipal]::new($photoIdentity)
if (-not $photoPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this entire block in Administrator PowerShell on PHOTO-API-HOST.'
}
$photoConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
$photoSshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
$photoUpdated = Convert-PhotoSshConfig ([System.IO.File]::ReadAllText($photoConfig))
$photoBackup = $photoConfig + '.eventdesk-account-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N') + '.bak'
[System.IO.File]::Copy($photoConfig, $photoBackup, $false)
$photoRestartAttempted = $false
try {
    [System.IO.File]::WriteAllText($photoConfig, $photoUpdated, [System.Text.UTF8Encoding]::new($false))
    & $photoSshd -t -f $photoConfig
    if ($LASTEXITCODE -ne 0) { throw 'SSH configuration syntax validation failed.' }
    $photoEffective = @(& $photoSshd -G -T -f $photoConfig -C 'user=example\photo-operator,host=SCANNER-HOST,addr=192.0.2.10')
    if ($LASTEXITCODE -ne 0) { throw 'SSH effective configuration validation failed.' }
    foreach ($photoExpected in @('authorizedkeysfile .ssh/authorized_keys', 'loglevel INFO', 'syslogfacility AUTH')) {
        if ($photoEffective -inotcontains $photoExpected) { throw ('Unexpected effective SSH setting; expected: ' + $photoExpected) }
    }
    $photoRestartAttempted = $true
    Restart-Service -Name sshd
    (Get-Service -Name sshd).WaitForStatus('Running', [timespan]::FromSeconds(20))
} catch {
    $photoFailure = $_.Exception.Message
    try { [System.IO.File]::Copy($photoBackup, $photoConfig, $true) } catch {
        throw ('SSH change failed: ' + $photoFailure + '. BACKUP RESTORE FAILED: ' + $_.Exception.Message + '. Backup: ' + $photoBackup)
    }
    try {
        if ((Get-Service -Name sshd).Status -eq 'Stopped') { Start-Service -Name sshd }
        elseif ($photoRestartAttempted) { Restart-Service -Name sshd }
        (Get-Service -Name sshd).WaitForStatus('Running', [timespan]::FromSeconds(20))
    } catch {
        throw ('SSH change failed: ' + $photoFailure + '. Configuration restored, but SSH service recovery failed: ' + $_.Exception.Message + '. Backup: ' + $photoBackup)
    }
    throw ('SSH change failed; original configuration restored: ' + $photoFailure + '. Backup: ' + $photoBackup)
}
Write-Output ('SSH account rule applied; logging restored to INFO/AUTH. Backup: ' + $photoBackup)
$photoEffective | Select-String '^(authorizedkeysfile|loglevel|syslogfacility) '
}
