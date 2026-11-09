$ErrorActionPreference = 'Stop'

$ruleName = 'EventDesk LAN TCP 8080'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdministrator) {
    throw 'Run PowerShell as Administrator, then run this script again.'
}

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Set-NetFirewallRule -DisplayName $ruleName -Enabled True -Direction Inbound `
        -Action Allow -Profile Any
    Set-NetFirewallPortFilter -AssociatedNetFirewallRule $existing -Protocol TCP -LocalPort 8080
    Set-NetFirewallAddressFilter -AssociatedNetFirewallRule $existing -RemoteAddress LocalSubnet
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort 8080 -Profile Any -RemoteAddress LocalSubnet | Out-Null
}

Write-Host 'EventDesk LAN access is allowed on TCP port 8080 from the local subnet.'
