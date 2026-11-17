$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'migrate_event_backend.ps1')
$script:checks=0
function Assert-True([bool]$Condition,[string]$Message){if(-not $Condition){throw $Message};$script:checks++}
function Assert-Throws([scriptblock]$Action,[string]$Message){$failed=$false;try{&$Action|Out-Null}catch{$failed=$true};Assert-True $failed $Message}
$hash='a'*64
$plan=Get-EventMigrationPlan 'C:\private\source snapshot.json' $hash 'dbo'
Assert-True ($plan.Python -eq 'C:\ProgramData\EventDeskPhotoApi\event-backend\python\python.exe') 'Migration uses the fixed protected interpreter.'
Assert-True ($plan.Arguments.Contains('scripts.migrate_sql_event') -and $plan.Arguments.Contains('"--dry-run"')) 'Default invokes the exact isolated dry-run module.'
Assert-True ($plan.Arguments.StartsWith('"-I" "-c" ') -and -not $plan.Arguments.Contains('--connection-stdin')) 'Dry run cannot request SQL credentials.'
$committed=Get-EventMigrationPlan 'C:\private\source.json' $hash 'dbo' -Commit
Assert-True ($committed.Arguments.Contains('"--commit" "--connection-stdin"')) 'Commit is explicit and uses stdin only.'
foreach($source in @('relative.json','\\server\share\source.json','C:\source:stream','C:\x\..\source.json')){Assert-Throws {Get-EventMigrationPlan $source $hash 'dbo'} 'Unsafe source path rejected.'}
Assert-Throws {Get-EventMigrationPlan 'C:\private\source.json' 'bad' 'dbo'} 'Source hash must be pinned.'
Assert-Throws {Get-EventMigrationPlan 'C:\private\source.json' $hash 'dbo;DROP TABLE'} 'Schema is allowlisted.'
$script:credentialCalls=0
$provider={ $script:credentialCalls++; return 'Driver={ODBC Driver 18 for SQL Server};PWD={synthetic;password}' }
$dryInput=Get-EventMigrationInput -CredentialProvider $provider
Assert-True ($dryInput.Length -eq 0 -and $script:credentialCalls -eq 0) 'Dry run never loads DPAPI or a SQL connection.'
$commitInput=Get-EventMigrationInput -Commit -CredentialProvider $provider
Assert-True ($script:credentialCalls -eq 1 -and [Text.Encoding]::UTF8.GetString($commitInput).Contains('PWD={synthetic;password}')) 'Only explicit commit requests protected connection bytes.'
Assert-True (-not $committed.Arguments.Contains('PWD') -and -not $committed.Arguments.Contains('synthetic;password')) 'Connection never enters arguments.'
$assignments=(Get-Command Invoke-EventBackendMigration).ScriptBlock.Ast.FindAll({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left -is [Management.Automation.Language.VariableExpressionAst]},$true)
$providerStatement=($assignments|Where-Object{$_.Left.VariablePath.UserPath -eq 'provider'}).Extent.Text
$inputStatement=($assignments|Where-Object{$_.Left.VariablePath.UserPath -eq 'inputBytes' -and $_.Right.Extent.Text.Contains('Get-EventMigrationInput')}).Extent.Text
$probePath=Join-Path ([IO.Path]::GetTempPath()) ('event-migration-scope-'+[Guid]::NewGuid().ToString('N')+'.ps1')
$bridgePath=Join-Path (Split-Path $PSScriptRoot -Parent) 'migrate_event_backend.ps1'
$scopeProbe=@'
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
. '__BRIDGE__'
function Get-EventProtectedMigrationConnection($Context){
    if($Context.Marker -ne 'owned-context'){throw 'Fake context was not passed.'}
    return 'Driver={ODBC Driver 18 for SQL Server};UID=synthetic-user;PWD=synthetic-only'
}
function Invoke-OwnedCredentialProbe{
    $Commit=$true;$context=[pscustomobject]@{Marker='owned-context'}
    __PROVIDER__
    __INPUT__
    try{return [Text.Encoding]::UTF8.GetString($inputBytes) -eq 'Driver={ODBC Driver 18 for SQL Server};UID=synthetic-user;PWD=synthetic-only'}
    finally{[Array]::Clear($inputBytes,0,$inputBytes.Length)}
}
Invoke-OwnedCredentialProbe
'@
$scopeProbe=$scopeProbe.Replace('__BRIDGE__',$bridgePath.Replace("'","''")).Replace('__PROVIDER__',$providerStatement).Replace('__INPUT__',$inputStatement)
try{
    [IO.File]::WriteAllText($probePath,$scopeProbe,(New-Object Text.UTF8Encoding($false)))
    Assert-True ((& $probePath) -eq $true) 'Normal &script invocation resolves the production helper and passes its parent fake context without a dynamic module.'
}finally{if([IO.File]::Exists($probePath)){[IO.File]::Delete($probePath)}}
$parentAcl=New-Object Security.AccessControl.DirectorySecurity
$parentAcl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
$parentAcl.SetAccessRuleProtection($true,$false)
$parentAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-5-18')),[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.AccessControlType]::Allow)))
$parentAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')),[Security.AccessControl.FileSystemRights]::CreateFiles,[Security.AccessControl.AccessControlType]::Allow)))
Assert-EventMigrationAcl $parentAcl 'namespace';$script:checks++
Assert-Throws {Assert-EventMigrationAcl $parentAcl 'code'} 'The immediate source parent cannot permit new untrusted WAL/SHM files.'
$counts=[ordered]@{employees=2;wristbands=2};$hashes=[ordered]@{employees=('b'*64);wristbands=('c'*64)}
$summary=@{mode='dry-run';source_sha256=('d'*64);counts=$counts;table_sha256=$hashes}|ConvertTo-Json -Depth 4
$safe=Convert-EventMigrationResult $summary 0 @('employees','wristbands') 'dbo' $hash
Assert-True ($safe.Mode -eq 'dry-run' -and $safe.Counts.employees -eq 2 -and $safe.Schema -eq 'dbo') 'Only approved migration summary fields are returned.'
Assert-Throws {Convert-EventMigrationResult '{"password":"private"}' 0 @('employees') 'dbo' $hash} 'Unexpected stdout cannot leak credential fields.'
Assert-Throws {Convert-EventMigrationResult '{"error":"private backend detail"}' 1 @('employees') 'dbo' $hash} 'Nonzero migration never exposes raw stdout.'
$bad=@{mode='dry-run';source_sha256=('d'*64);counts=@{employees='private'};table_sha256=@{employees=('e'*64)}}|ConvertTo-Json -Depth 3
Assert-Throws {Convert-EventMigrationResult $bad 0 @('employees') 'dbo' $hash} 'Counts cannot contain untrusted source text.'
$tokens=$null;$parseErrors=$null
$scriptPath=Join-Path (Split-Path $PSScriptRoot -Parent) 'migrate_event_backend.ps1'
$ast=[Management.Automation.Language.Parser]::ParseFile($scriptPath,[ref]$tokens,[ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) 'Migration bridge parses.'
$text=[IO.File]::ReadAllText($scriptPath)
Assert-True ($text -notmatch 'initialize_schema|CREATE TABLE|ALTER ROLE|Invoke-Expression|Start-Service|Stop-Service') 'Bridge cannot perform DDL, grant roles or mutate services.'
Assert-True ($text.Contains('[Environment]::MachineName')) 'Commit checks the actual OS hostname.'
Initialize-EventMigrationRunner
$start=New-EventMigrationStartInfo $plan
Assert-True (-not $start.UseShellExecute -and $start.CreateNoWindow -and $start.RedirectStandardInput -and $start.RedirectStandardOutput -and $start.RedirectStandardError) 'Child configuration is private and windowless.'
Assert-True (-not $start.EnvironmentVariables.ContainsKey('PYTHONPATH') -and -not $start.EnvironmentVariables.ContainsKey('EVENTDESK_IMPORT_CONNECTION')) 'Inherited code and credential environment is stripped.'
Add-Type -TypeDefinition @'
using System; using System.Diagnostics;
namespace EventMigrationTests {
 public sealed class FakeJob : IDisposable {
  public bool Assigned,Disposed;
  public void Assign(Process child){Assigned=child.Id>0;}
  public void Dispose(){Disposed=true;}
 }
}
'@
$fakeJob=New-Object EventMigrationTests.FakeJob
$probeStart=New-Object Diagnostics.ProcessStartInfo((Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'WindowsPowerShell\v1.0\powershell.exe'),'-NoProfile -NonInteractive -Command "[Console]::In.ReadToEnd() | Out-Null; [Console]::Out.Write(''synthetic-runner-output'')"')
$probeStart.UseShellExecute=$false;$probeStart.CreateNoWindow=$true
$probeStart.RedirectStandardInput=$true;$probeStart.RedirectStandardOutput=$true;$probeStart.RedirectStandardError=$true
$probe=[EventDeskMigrationBridge.Runner]::Run($probeStart,[Text.Encoding]::UTF8.GetBytes('synthetic-stdin'),$fakeJob,10000)
Assert-True ($probe.ExitCode -eq 0 -and $probe.Output -eq 'synthetic-runner-output') 'Owned hidden synthetic child exercises bounded stdin/stdout without a real migration.'
Assert-True ($fakeJob.Assigned -and $fakeJob.Disposed) 'Runner assigns containment before configuration and disposes it on exit.'
$slowJob=New-Object EventMigrationTests.FakeJob
$probeStart.Arguments='-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 20"'
$deadline=[Diagnostics.Stopwatch]::StartNew()
Assert-Throws {[EventDeskMigrationBridge.Runner]::Run($probeStart,(New-Object byte[] 65536),$slowJob,100)} 'Blocked startup input shares the total process deadline.'
Assert-True ($deadline.ElapsedMilliseconds -lt 2000 -and $slowJob.Disposed) 'Timed-out owned child is contained and cleaned within the deadline budget.'
Write-Output "Migration bridge checks passed: $script:checks; no SQL, DPAPI, services or private configuration accessed."
