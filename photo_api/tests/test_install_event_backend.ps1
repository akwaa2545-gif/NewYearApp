$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$installer = Join-Path (Split-Path $PSScriptRoot -Parent) 'install_event_backend.ps1'
. $installer
$script:checks = 0
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message }; $script:checks++ }
function Assert-Throws([scriptblock]$Action, [string]$Message) { $threw = $false; try { & $Action | Out-Null } catch { $threw = $true }; Assert-True $threw $Message }
$hash = 'a' * 64
$plan = Get-EventInstallPlan 'C:\staging\package' $hash 'C:\Python314' 'C:\private\options.json'
Assert-True ($plan.Root -eq 'C:\ProgramData\EventDeskPhotoApi\event-backend') 'Runtime target must be fixed.'
Assert-True ($plan.ServiceName -eq 'EventDeskEventBackend' -and $plan.PhotoServiceName -eq 'EventDeskPhotoApi') 'Service names must be fixed.'
Assert-True ($plan.BinaryCommand -eq '"C:\ProgramData\EventDeskPhotoApi\bin\EventBackendService.exe"') 'SCM command must be quoted and contain no arguments.'
foreach ($source in @('relative', '\\server\share', 'C:\staging\file:stream', 'C:\ProgramData\EventDeskPhotoApi\staging')) {
    Assert-Throws { Get-EventInstallPlan $source $hash 'C:\Python314' 'C:\private\options.json' } 'Unsafe source path rejected.'
}
Assert-Throws { Get-EventInstallPlan 'C:\staging' 'bad' 'C:\Python314' 'C:\private\options.json' } 'Malformed trusted hash rejected.'
foreach ($relative in @('../outside', 'src/../outside', '/src/app.py', 'C:/src/app.py', 'src/app.py:stream', 'src//app.py', 'other/app.py', 'src/app.py.', 'src\\app.py')) {
    Assert-Throws { Resolve-EventPackagePath 'C:\staging' $relative } 'Unsafe manifest path rejected.'
}
Assert-True ((Resolve-EventPackagePath 'C:\staging' 'src/photo_api/event_backend.py') -eq 'C:\staging\src\photo_api\event_backend.py') 'Canonical relative paths resolve under package.'
$acl = New-EventPrivateDirectorySecurity
Assert-True $acl.AreAccessRulesProtected 'Private root DACL must disable inheritance.'
Assert-True ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-32-544') 'Private files must have privileged owner.'
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
Assert-True ($rules.Count -eq 2) 'Private ACL must have exactly SYSTEM and Administrators.'
foreach ($rule in $rules) {
    Assert-True ($rule.IdentityReference.Value -in @('S-1-5-18', 'S-1-5-32-544')) 'No untrusted read/write grants.'
    Assert-True ($rule.FileSystemRights -eq [Security.AccessControl.FileSystemRights]::FullControl) 'Privileged principals retain full control.'
}
Assert-EventPythonAcl $acl
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')), [Security.AccessControl.FileSystemRights]::ReadAndExecute, [Security.AccessControl.AccessControlType]::Allow)))
Assert-EventPythonAcl $acl
$script:checks++
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')), [Security.AccessControl.FileSystemRights]::Write, [Security.AccessControl.AccessControlType]::Allow)))
Assert-Throws { Assert-EventPythonAcl $acl } 'Untrusted source Python write permission rejected.'
$namespaceAcl=New-EventPrivateDirectorySecurity
$usersSid=New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
$namespaceAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($usersSid,[Security.AccessControl.FileSystemRights]::CreateDirectories,[Security.AccessControl.AccessControlType]::Allow)))
Assert-EventNamespaceAcl $namespaceAcl;$script:checks++
$namespaceAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-3-0')),[Security.AccessControl.FileSystemRights]::FullControl,([Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'),[Security.AccessControl.PropagationFlags]::InheritOnly,[Security.AccessControl.AccessControlType]::Allow)))
Assert-EventNamespaceAcl $namespaceAcl;$script:checks++
$namespaceAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($usersSid,[Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles,[Security.AccessControl.AccessControlType]::Allow)))
Assert-Throws { Assert-EventNamespaceAcl $namespaceAcl } 'Writable ancestor cannot replace a protected Python tree.'
$ownerAcl=New-EventPrivateDirectorySecurity
$ownerAcl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-21-100-200-300-1001')))
Assert-Throws { Assert-EventNamespaceAcl $ownerAcl } 'Untrusted owner cannot change ancestor ACLs.'
Assert-True ((Resolve-EventLocalPath 'C:\') -eq 'C:\') 'Drive root remains absolute during ancestor checks.'
$commands = Get-EventServiceCommands $plan
Assert-True ($commands.Account -eq 'LocalSystem' -and $commands.Dependencies -eq "EventDeskPhotoApi`0`0") 'New service is SYSTEM and depends on existing photo service.'
Assert-True (($commands.RecoveryArguments -join '|') -eq 'failure|EventDeskEventBackend|reset=|86400|actions=|restart/60000/restart/60000/restart/60000') 'Recovery commands use structured nonsecret arguments.'
Assert-True (($commands.FailureFlagArguments -join '|') -eq 'failureflag|EventDeskEventBackend|1') 'SCM handles noncrash unexpected exits.'
Initialize-EventInstallerNative
Assert-True ([EventDeskEventInstaller.Native].GetMethod('CreateFile').ReturnType -eq [Microsoft.Win32.SafeHandles.SafeFileHandle]) 'Atomic file creation returns an owned safe handle.'
Add-Type -TypeDefinition @'
using System;
namespace EventBackendInstallerTests {
 public static class Probe {
  public static string Machine, Database, Binary, Dependencies, Account, Password;
  public static IntPtr OpenSCManager(string machine,string database,uint access) { Machine=machine;Database=database;return IntPtr.Zero; }
  public static IntPtr CreateService(IntPtr manager,string name,string display,uint access,uint type,uint start,uint error,string binary,string group,IntPtr tag,string dependencies,string account,string password) {Binary=binary;Dependencies=dependencies;Account=account;Password=password;return IntPtr.Zero;}
 }
}
'@
$tokens=$null;$parseErrors=$null
$installerAst=[Management.Automation.Language.Parser]::ParseFile($installer,[ref]$tokens,[ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) 'Installer AST must parse without errors.'
$manager=[IntPtr]::Zero
foreach($method in @('OpenSCManager','CreateService')){
    $calls=@($installerAst.FindAll({param($node) $node -is [Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Expression -is [Management.Automation.Language.TypeExpressionAst] -and $node.Expression.TypeName.FullName -eq 'EventDeskEventInstaller.Native' -and $node.Member.Value -eq $method},$true))
    Assert-True ($calls.Count -eq 1) "Exactly one approved $method operation is expected."
    $probeCall=$calls[0].Extent.Text.Replace('[EventDeskEventInstaller.Native]','[EventBackendInstallerTests.Probe]')
    & ([scriptblock]::Create($probeCall)) | Out-Null
}
Assert-True ($null -eq [EventBackendInstallerTests.Probe]::Machine -and $null -eq [EventBackendInstallerTests.Probe]::Database) 'SCM receives actual CLR null host/database values.'
Assert-True ([EventBackendInstallerTests.Probe]::Binary -eq $plan.BinaryCommand) 'SCM receives the exact quoted executable with no secret arguments.'
Assert-True ([EventBackendInstallerTests.Probe]::Dependencies -eq "EventDeskPhotoApi`0`0" -and [EventBackendInstallerTests.Probe]::Account -eq 'LocalSystem') 'SCM receives exact dependency MULTI_SZ and SYSTEM account.'
Assert-True ($null -eq [EventBackendInstallerTests.Probe]::Password) 'LocalSystem password is a CLR null, not an empty or plaintext value.'

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('EventBackendInstallerTests-' + [guid]::NewGuid().ToString('N'))
$junction = $null
try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $ownSecurity=New-Object Security.AccessControl.FileSecurity
    $ownSecurity.SetAccessRuleProtection($true,$false)
    $ownSid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $ownSecurity.SetOwner($ownSid)
    $ownSecurity.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($ownSid,[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.AccessControlType]::Allow)))
    $atomic=Join-Path $testRoot 'atomic-fixture.txt'
    Write-EventProtectedBytes $atomic ([Text.Encoding]::UTF8.GetBytes('synthetic atomic file fixture')) $ownSecurity
    Assert-True ([IO.File]::ReadAllText($atomic) -eq 'synthetic atomic file fixture') 'CreateFileW security descriptor and SafeFileHandle stream write work on an owned test file.'
    $replacement=Join-Path $testRoot 'replacement.txt'
    [IO.File]::WriteAllText($replacement,'synthetic replacement')
    [IO.File]::Replace($replacement,$atomic,[NullString]::Value)
    Assert-True ([IO.File]::ReadAllText($atomic) -eq 'synthetic replacement') 'Atomic replace accepts CLR null backup path on Windows PowerShell 5.1.'
    $emptySource=Join-Path $testRoot 'empty-source.py';[IO.File]::WriteAllBytes($emptySource,(New-Object byte[] 0))
    $emptyTarget=Join-Path $testRoot 'empty-target.py'
    Copy-EventVerifiedFile $emptySource $emptyTarget (Get-FileHash -LiteralPath $emptySource -Algorithm SHA256).Hash $ownSecurity
    Assert-True ((Get-Item -LiteralPath $emptyTarget).Length -eq 0) 'Zero-byte Python package modules copy and hash successfully.'
    Assert-Throws { Read-EventBytes $emptySource 1024 } 'Manifest/options readers still reject empty content.'
    $package = Join-Path $testRoot 'package'
    New-Item -ItemType Directory -Path $package | Out-Null
    $files = [ordered]@{}
    foreach ($relative in @('src/app.py', 'src/event_backend_entry.py', 'src/photo_api/event_backend.py', 'src/eventdesk_storage/__init__.py', 'packages/pyodbc.pyd', 'bin/EventDeskPhotoApi.exe', 'bin/EventBackendService.exe')) {
        $path = Resolve-EventPackagePath $package $relative
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path)) | Out-Null
        [IO.File]::WriteAllText($path, 'synthetic package content ' + $relative)
        $files[$relative] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    }
    $manifest = Join-Path $package 'package_manifest.json'
    [IO.File]::WriteAllText($manifest, (@{schema_version=1;files=$files} | ConvertTo-Json -Depth 4))
    $trusted = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    $verified = Read-EventPackage $package $trusted
    Assert-True ($verified.Files.Count -eq 7) 'Verified package returns exactly manifest files.'
    Assert-Throws { Read-EventPackage $package ('0' * 64) } 'Untrusted manifest cannot be parsed or used.'
    $app = Resolve-EventPackagePath $package 'src/app.py'
    [IO.File]::AppendAllText($app, 'tampered')
    Assert-Throws { Read-EventPackage $package $trusted } 'Payload hash mismatch rejected.'
    [IO.File]::WriteAllText($app, 'synthetic package content src/app.py')
    $extra = Join-Path $package 'src\unexpected.py'
    [IO.File]::WriteAllText($extra, 'unmanifested')
    Assert-Throws { Read-EventPackage $package $trusted } 'Unmanifested files rejected.'
    [IO.File]::Delete($extra)
    $outside = Join-Path $testRoot 'outside'; New-Item -ItemType Directory -Path $outside | Out-Null
    $junction = Join-Path $package 'src\junction'
    New-Item -ItemType Junction -Path $junction -Target $outside | Out-Null
    Assert-Throws { Read-EventPackage $package $trusted } 'Reparse directories rejected before traversal.'
    [IO.Directory]::Delete($junction); $junction = $null
    $python = Join-Path $testRoot 'python'
    foreach ($relative in @('python.exe', 'python314.dll', 'DLLs/_sqlite3.pyd', 'Lib/os.py', 'Lib/site-packages/private.py', 'Scripts/pip.exe', 'Lib/__pycache__/os.pyc', 'pyvenv.cfg', 'Lib/sitecustomize.py')) {
        $path = Join-Path $python $relative.Replace('/', '\'); [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path)) | Out-Null
        [IO.File]::WriteAllText($path, 'synthetic Python fixture')
    }
    $pythonFiles = @(Get-EventPythonFiles $python)
    Assert-True ($pythonFiles.Count -eq 4) 'Python copy includes interpreter/stdlib only.'
    Assert-True ((@($pythonFiles | Where-Object { $_.Relative -match 'site-packages|Scripts|pyvenv|__pycache__|sitecustomize' })).Count -eq 0) 'Original user packages and runtime hooks are excluded.'
    # Use the reviewed local validators for an end-to-end dry run. These CLR
    # methods only parse synthetic options; they never call ConfigStore.Load.
    foreach($name in @('EventDeskPhotoApi.exe','EventBackendService.exe')){
        $built=Join-Path (Split-Path $PSScriptRoot -Parent) ('bin\'+$name)
        $relative='bin/'+$name;$destination=Resolve-EventPackagePath $package $relative
        [IO.File]::WriteAllBytes($destination,[IO.File]::ReadAllBytes($built))
        $files[$relative]=(Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    }
    [IO.File]::WriteAllText($manifest,(@{schema_version=1;files=$files}|ConvertTo-Json -Depth 4))
    $trusted=(Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    $options=Join-Path $testRoot 'private-options.json'
    $syntheticOptions=@{PublicOrigin='https://eventdesk.pages.dev';Schema='dbo';AppConfig=@{event_name='Synthetic event';secret=('a'*64);admin_password=@{salt=('b'*32);hash=('c'*64)};staff_password=@{salt=('d'*32);hash=('e'*64)}}}
    [IO.File]::WriteAllText($options,($syntheticOptions|ConvertTo-Json -Depth 5))
    $targetExisted=[IO.Directory]::Exists($plan.Root)
    $dryRun=Invoke-EventBackendInstall $package $trusted $python $options
    Assert-True ($dryRun.Mode -eq 'DryRun' -and -not $dryRun.BackendWillStart -and $dryRun.PackageFiles -eq 7) 'Default dry run verifies a real reviewed package without mutations.'
    Assert-True ([IO.Directory]::Exists($plan.Root) -eq $targetExisted) 'Default dry run never creates the protected target.'
} finally {
    if ($junction -and [IO.Directory]::Exists($junction)) { [IO.Directory]::Delete($junction) }
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolvedTestRoot.StartsWith([IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolvedTestRoot) -notmatch '^EventBackendInstallerTests-[0-9a-f]{32}$') { throw 'Unsafe test cleanup path.' }
    if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }
}
$text = [IO.File]::ReadAllText($installer)
Assert-True ($text.Contains('[switch]$Install')) 'Mutation requires explicit Install switch.'
Assert-True ($text -notmatch '(?i)Remove-Item[^\r\n]*-Recurse|New-NetFirewallRule|msiexec|Invoke-Expression') 'Installer cannot recursively delete or install drivers/firewall resources.'
Assert-True ($text.Contains('PHOTO-API-HOST')) 'Approved-host guard missing.'
Assert-True ($text.Contains('[Environment]::MachineName') -and -not $text.Contains('$env:COMPUTERNAME')) 'Approved-host guard uses actual OS identity, not caller environment.'
Assert-True ($text.IndexOf('$photoStopAttempted=$true') -lt $text.IndexOf('Stop-Service -Name $plan.PhotoServiceName')) 'Rollback latch must precede a potentially partially successful stop.'
Assert-True ($text.Contains("`$recovering.Status -eq 'StopPending'")) 'Rollback waits for an accepted stop before restarting the original service.'
Assert-True ($text.IndexOf('$photoChanged=$true') -lt $text.IndexOf('[IO.File]::Replace($temporary')) 'Rollback covers a partially completed binary replacement.'
Assert-True ($text -notmatch 'Start-Service\s+-Name\s+\$plan\.ServiceName') 'New SQL backend must not be started by installer.'
Write-Output "Event backend installer checks passed: $script:checks; no services, SQL, DPAPI or installation changes."
