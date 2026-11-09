$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$installer = Join-Path (Split-Path $PSScriptRoot -Parent) 'install_service.ps1'
$tokens = $null
$parseErrors = $null
$installerAst = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
. $installer
$script:checks = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    $threw = $false
    try { & $Action | Out-Null } catch { $threw = $true }
    Assert-True $threw $Message
}

$sid = Get-PhotoServiceSid 'EventDeskPhotoApi'
Assert-True ($sid -match '^S-1-5-80-(\d+-){4}\d+$') 'Invalid service SID format.'
Assert-True ($sid -eq (Get-PhotoServiceSid 'eventdeskphotoapi')) 'Service SID must be case insensitive.'
Assert-True ((Get-PhotoServiceSid 'TrustedInstaller') -eq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464') 'Service SID derivation does not match Windows.'
$source = Join-Path $env:TEMP 'EventDeskPhotoApi-source.exe'
$hash = 'a' * 64
$plan = Get-PhotoInstallPlan $source $hash
# Run the actual production call expressions against CLR string parameters without
# invoking native code. Windows PowerShell binds $null to an empty string here.
Add-Type -TypeDefinition @'
using System;
namespace EventDeskPhotoInstallerTests {
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
        $node.Expression.TypeName.FullName -eq 'EventDeskPhotoInstaller.Native' -and
        $node.Member.Value -eq $method
    }, $true))
    Assert-True ($calls.Count -eq 1) "Expected one production $method call."
    $probeCall = $calls[0].Extent.Text.Replace('[EventDeskPhotoInstaller.Native]', '[EventDeskPhotoInstallerTests.ArgumentProbe]')
    & ([scriptblock]::Create($probeCall)) | Out-Null
}
Assert-True ($null -eq [EventDeskPhotoInstallerTests.ArgumentProbe]::Machine) 'SCM machine must receive a CLR null, not an empty string.'
Assert-True ($null -eq [EventDeskPhotoInstallerTests.ArgumentProbe]::Database) 'SCM database must receive a CLR null, not an empty string.'
Assert-True ($null -eq [EventDeskPhotoInstallerTests.ArgumentProbe]::Group) 'Service load order group must receive a CLR null.'
Assert-True ($null -eq [EventDeskPhotoInstallerTests.ArgumentProbe]::Dependencies) 'Service dependencies must receive a CLR null.'
Assert-True ($null -eq [EventDeskPhotoInstallerTests.ArgumentProbe]::Password) 'Virtual service account password must receive a CLR null.'
Assert-True ([EventDeskPhotoInstallerTests.ArgumentProbe]::Tag -eq [IntPtr]::Zero) 'Service tag pointer must remain zero.'
Assert-True ($plan.Root -eq 'C:\ProgramData\EventDeskPhotoApi') 'Unexpected installation root.'
Assert-True ($plan.Binary -eq 'C:\ProgramData\EventDeskPhotoApi\bin\EventDeskPhotoApi.exe') 'Unexpected binary path.'
Assert-True ($plan.BinaryCommand -eq '"C:\ProgramData\EventDeskPhotoApi\bin\EventDeskPhotoApi.exe"') 'Service command must contain only the quoted executable.'
Assert-True ($plan.Identity -eq 'NT SERVICE\EventDeskPhotoApi') 'Service identity must be virtual.'
Assert-True ($plan.Url -eq 'http://127.0.0.1:8765/') 'URL must bind IPv4 loopback only.'
Assert-True ($plan.UrlSddl -eq "D:(A;;GX;;;$sid)") 'URL reservation must grant only the service SID.'
Assert-Throws { Get-PhotoInstallPlan 'relative.exe' $hash } 'Relative sources must be rejected.'
Assert-Throws { Get-PhotoInstallPlan '\\server\share\api.exe' $hash } 'UNC sources must be rejected.'
Assert-Throws { Get-PhotoInstallPlan $source 'not-a-hash' } 'Malformed hashes must be rejected.'
Assert-Throws { Get-PhotoInstallPlan 'C:\ProgramData\EventDeskPhotoApi\bin\api.exe' $hash } 'Sources inside the installation must be rejected.'

$security = New-PhotoDirectorySecurity $sid
$rules = @($security.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
Assert-True $security.AreAccessRulesProtected 'Root DACL must disable inheritance.'
Assert-True ($rules.Count -eq 3) 'Root DACL must have exactly three grants.'
foreach ($principal in @('S-1-5-18', 'S-1-5-32-544', $sid)) {
    $rule = @($rules | Where-Object { $_.IdentityReference.Value -eq $principal })
    Assert-True ($rule.Count -eq 1) "Missing or duplicate ACL for $principal."
    Assert-True ($rule[0].AccessControlType -eq 'Allow') 'Only explicit allow rules are expected.'
    Assert-True ($rule[0].InheritanceFlags -eq 'ContainerInherit, ObjectInherit') 'ACL must cover child files and directories.'
    $expected = [System.Security.AccessControl.FileSystemRights]::FullControl
    if ($principal -eq $sid) {
        $expected = [System.Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [System.Security.AccessControl.FileSystemRights]::Synchronize
    }
    Assert-True ($rule[0].FileSystemRights -eq $expected) "Unexpected permissions for $principal."
}
$logSecurity = New-PhotoDirectorySecurity $sid -Logs
$logRule = @($logSecurity.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq $sid })
Assert-True ($logRule[0].FileSystemRights -eq ([System.Security.AccessControl.FileSystemRights]::Modify -bor [System.Security.AccessControl.FileSystemRights]::Synchronize)) 'Logs must grant service modify only.'

$temporary = [System.IO.Path]::GetTempFileName()
try {
    [System.IO.File]::WriteAllText($temporary, 'known installer hash fixture')
    $actual = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash
    Assert-PhotoBinaryHash $temporary $actual.ToLowerInvariant()
    $script:checks++
    Assert-Throws { Assert-PhotoBinaryHash $temporary ('0' * 64) } 'Binary hash mismatch must fail.'
} finally { [System.IO.File]::Delete($temporary) }

$text = [System.IO.File]::ReadAllText($installer)
Assert-True ($text -notmatch '(?im)^\s*Start-Service\b') 'Installer must never start the service.'
Assert-True ($text -notmatch '(?i)New-NetFirewallRule|Remove-Item.+-Recurse|netsh.+advfirewall') 'Installer must not alter firewall or recursively delete files.'
Assert-True ($text.Contains('PHOTO-API-HOST')) 'Approved-host guard is missing.'
Assert-True ($text.Contains('0x00000002')) 'Automatic startup configuration is missing.'
Write-Output "Installer checks passed: $script:checks (PowerShell $($PSVersionTable.PSVersion)); no SCM/network changes performed."
