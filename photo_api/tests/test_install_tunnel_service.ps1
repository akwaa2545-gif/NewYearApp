$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$installer = Join-Path (Split-Path $PSScriptRoot -Parent) 'install_tunnel_service.ps1'
. $installer
$script:checks = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }; $script:checks++
}
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    $threw = $false; try { & $Action | Out-Null } catch { $threw = $true }
    Assert-True $threw $Message
}
$hash = 'a' * 64
$plan = Get-TunnelInstallPlan 'C:\stage\cloudflared.exe' $hash
Assert-True ($plan.Root -eq 'C:\ProgramData\EventDeskPhotoTunnel') 'Wrong isolated root.'
Assert-True ($plan.Name -eq 'Cloudflared') 'cloudflared requires its fixed Windows service name.'
Assert-True ($plan.Identity -eq 'NT SERVICE\Cloudflared') 'Expected a virtual identity.'
Assert-True ($plan.BinaryCommand -eq '"C:\ProgramData\EventDeskPhotoTunnel\bin\cloudflared.exe" --no-autoupdate tunnel --loglevel info --logfile "C:\ProgramData\EventDeskPhotoTunnel\logs\cloudflared.log" --metrics 127.0.0.1:20242 run --token-file "C:\ProgramData\EventDeskPhotoTunnel\token.txt"') 'Unexpected service arguments.'
Assert-True ($plan.BinaryCommand -notmatch '--token\s') 'Token value must never enter SCM arguments.'
Assert-Throws { Get-TunnelInstallPlan 'relative.exe' $hash } 'Relative paths must fail.'
Assert-Throws { Get-TunnelInstallPlan '\\host\share\tool.exe' $hash } 'UNC paths must fail.'
Assert-Throws { Get-TunnelInstallPlan 'C:\ProgramData\EventDeskPhotoTunnel\tool.exe' $hash } 'Installation source must be disjoint.'
Assert-Throws { Get-TunnelInstallPlan 'C:\stage\tool.exe' 'bad' } 'Bad digest must fail.'
Assert-True ((Get-TunnelServiceSid 'TrustedInstaller') -eq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464') 'Service SID algorithm mismatch.'
$tokens = $null; $parseErrors = $null
$installerAst = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) 'Installer syntax must parse.'
# Run the actual production call expressions against CLR string parameters without
# invoking native code. Windows PowerShell binds $null to an empty string here.
Add-Type -TypeDefinition @'
using System;
namespace EventDeskTunnelInstallerTests {
    public static class ArgumentProbe {
        public static string Machine, Database, Group, Dependencies, Password;
        public static IntPtr Tag;
        public static IntPtr OpenSCManager(string machine, string database, uint access) {
            Machine = machine; Database = database; return IntPtr.Zero;
        }
        public static IntPtr CreateService(IntPtr manager, string name, string display,
            uint access, uint type, uint start, uint error, string binary, string group,
            IntPtr tag, string dependencies, string account, string password) {
            Group = group; Tag = tag; Dependencies = dependencies; Password = password;
            return IntPtr.Zero;
        }
    }
}
'@
$manager = [IntPtr]::Zero
foreach ($method in @('OpenSCManager', 'CreateService')) {
    $calls = @($installerAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $node.Expression -is [System.Management.Automation.Language.TypeExpressionAst] -and
        $node.Expression.TypeName.FullName -eq 'EventDeskTunnelInstaller.Native' -and
        $node.Member.Value -eq $method
    }, $true))
    Assert-True ($calls.Count -eq 1) "Expected one production $method call."
    $probeCall = $calls[0].Extent.Text.Replace('[EventDeskTunnelInstaller.Native]', '[EventDeskTunnelInstallerTests.ArgumentProbe]')
    & ([scriptblock]::Create($probeCall)) | Out-Null
}
Assert-True ($null -eq [EventDeskTunnelInstallerTests.ArgumentProbe]::Machine) 'SCM machine must receive a CLR null, not an empty string.'
Assert-True ($null -eq [EventDeskTunnelInstallerTests.ArgumentProbe]::Database) 'SCM database must receive a CLR null, not an empty string.'
Assert-True ($null -eq [EventDeskTunnelInstallerTests.ArgumentProbe]::Group) 'Service load order group must receive a CLR null.'
Assert-True ($null -eq [EventDeskTunnelInstallerTests.ArgumentProbe]::Dependencies) 'Service dependencies must receive a CLR null.'
Assert-True ($null -eq [EventDeskTunnelInstallerTests.ArgumentProbe]::Password) 'Virtual service account password must receive a CLR null.'
Assert-True ([EventDeskTunnelInstallerTests.ArgumentProbe]::Tag -eq [IntPtr]::Zero) 'Service tag pointer must remain zero.'
$security = New-TunnelDirectorySecurity $plan.Sid
$rules = @($security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
Assert-True ($security.AreAccessRulesProtected -and $rules.Count -eq 3) 'Root must have exactly three protected grants.'
Assert-True ($security.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-32-544') 'Root owner must be administrators.'
foreach ($sid in @('S-1-5-18','S-1-5-32-544',$plan.Sid)) {
    $rule = @($rules | Where-Object { $_.IdentityReference.Value -eq $sid })
    Assert-True ($rule.Count -eq 1 -and $rule[0].AccessControlType -eq 'Allow') 'Unexpected ACL principal.'
    $expected = [Security.AccessControl.FileSystemRights]::FullControl
    if ($sid -eq $plan.Sid) { $expected = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize }
    Assert-True ($rule[0].FileSystemRights -eq $expected) 'Unexpected root permissions.'
}
$fileSecurity = New-TunnelTokenSecurity $plan.Sid
$fileRules = @($fileSecurity.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
Assert-True ($fileSecurity.AreAccessRulesProtected -and $fileRules.Count -eq 3) 'Token ACL must be protected.'
$serviceRule = @($fileRules | Where-Object { $_.IdentityReference.Value -eq $plan.Sid })
Assert-True ($serviceRule[0].FileSystemRights -eq ([Security.AccessControl.FileSystemRights]::Read -bor [Security.AccessControl.FileSystemRights]::Synchronize)) 'Token must be service read only.'
$id = 'fa22e9a7-218e-4125-b728-963a9fcb50dd'
$payload = @{a=('1'*32);t=$id;s=[Convert]::ToBase64String((New-Object byte[] 32))} | ConvertTo-Json -Compress
$token = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
Assert-TunnelToken $token $id; $script:checks++
$apiPayload = @{a=('1'*32);t=$id;s=[Convert]::ToBase64String((New-Object byte[] 64))} | ConvertTo-Json -Compress
$apiToken = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($apiPayload))
Assert-TunnelToken $apiToken $id; $script:checks++
foreach ($badLength in @(0,31,257)) {
    $badPayload = @{a=('1'*32);t=$id;s=[Convert]::ToBase64String((New-Object byte[] $badLength))} | ConvertTo-Json -Compress
    $badToken = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($badPayload))
    Assert-Throws { Assert-TunnelToken $badToken $id } "Secret length $badLength must fail."
}
Assert-Throws { Assert-TunnelToken $token 'fa22e9a7-218e-4125-b728-963a9fcb50de' } 'Wrong tunnel token must fail.'
Assert-Throws { Assert-TunnelToken ($token + "`n") $id } 'Whitespace in token must fail.'
Assert-Throws { Assert-TunnelToken ('a' * 9000) $id } 'Oversize token must fail.'
Assert-Throws { Assert-TunnelToken 'invalid' $id } 'Malformed token must fail.'
Assert-Throws { Assert-TunnelToken $token 'not-guid' } 'Expected tunnel ID must be validated.'
$text = [IO.File]::ReadAllText($installer)
Assert-True ($text -notmatch '(?im)^\s*Start-Service\b') 'Installer must not start services.'
Assert-True ($text -notmatch 'New-NetFirewallRule|Remove-Item.+-Recurse|netsh.+advfirewall') 'No firewall or recursive deletion.'
Assert-True ($text.Contains('[NullString]::Value')) 'Native string nulls must be explicit.'
Assert-True ($text.Contains('EventCreate.exe')) 'cloudflared EventLog source missing.'
Assert-True ($text.Contains('FileMode]::CreateNew')) 'Token must never overwrite existing files.'
Write-Output "Tunnel installer checks passed: $script:checks; no SCM, registry or network changes performed."
