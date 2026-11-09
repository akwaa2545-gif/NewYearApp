& {
$ErrorActionPreference = 'Stop'
$photoIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$photoPrincipal = [System.Security.Principal.WindowsPrincipal]::new($photoIdentity)
if (-not $photoPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this block in Administrator PowerShell on PHOTO-API-HOST.'
}
if ([System.Net.Dns]::GetHostName() -ne 'PHOTO-API-HOST') {
    throw 'This block is only for PHOTO-API-HOST.'
}
$photoConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
$photoBackup = $photoConfig + '.eventdesk-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fffffff') + '.bak'
$photoSshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
$photoOriginal = [System.IO.File]::ReadAllText($photoConfig)
Copy-Item -LiteralPath $photoConfig -Destination $photoBackup
try {
    $photoLogging = "SyslogFacility LOCAL0`r`nLogLevel DEBUG3`r`n" + $photoOriginal
    [System.IO.File]::WriteAllText($photoConfig, $photoLogging, [System.Text.UTF8Encoding]::new($false))
    & $photoSshd -t
    if ($LASTEXITCODE -ne 0) { throw 'SSH configuration check failed.' }
    Restart-Service -Name sshd
} catch {
    Copy-Item -LiteralPath $photoBackup -Destination $photoConfig -Force
    try {
        if ((Get-Service -Name sshd).Status -eq 'Stopped') { Start-Service -Name sshd }
    } catch {
        Write-Warning ('Original settings restored, but SSH needs administrator attention: ' + $_.Exception.Message)
    }
    throw
}
Write-Output ('Backup: ' + $photoBackup)
& $photoSshd -G -T -C 'user=example\photo-operator,host=SCANNER-HOST,addr=192.0.2.10' |
    Select-String '^(syslogfacility|loglevel|authorizedkeysfile) '
}
