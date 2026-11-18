<# Validate/import a frozen private export through the protected installed Python.
Dry run is the default and never loads SQL credentials. Schema preparation is a
separate approved step. No connection string belongs in arguments or environment.
#>
param([string]$SourcePath,[string]$ExpectedSourceSha256,[switch]$Commit)
Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'

function Resolve-EventMigrationPath([string]$Path){
    if([string]::IsNullOrWhiteSpace($Path) -or $Path -notmatch '^[A-Za-z]:\\' -or $Path.Substring(2).Contains(':') -or $Path.Contains('"') -or @($Path.Split('\')|Where-Object{$_ -in @('.','..')}).Count -gt 0){throw 'An absolute local source path is required.'}
    return [IO.Path]::GetFullPath($Path)
}
function Quote-EventMigrationArgument([string]$Value){
    if($Value.IndexOfAny([char[]]@([char]0,[char]10,[char]13)) -ge 0){throw 'Invalid process argument.'}
    $builder=New-Object Text.StringBuilder;$null=$builder.Append('"');$slashes=0
    foreach($character in $Value.ToCharArray()){
        if($character -eq '\'){$slashes++;continue}
        if($character -eq '"'){$null=$builder.Append(('\'*(2*$slashes+1)));$null=$builder.Append('"')}
        else{$null=$builder.Append(('\'*$slashes));$null=$builder.Append($character)}
        $slashes=0
    }
    $null=$builder.Append(('\'*(2*$slashes)));$null=$builder.Append('"');return $builder.ToString()
}
function Get-EventMigrationPlan([string]$Source,[string]$Hash,[string]$Schema,[switch]$Commit){
    if($Hash -notmatch '^[a-fA-F0-9]{64}$'){throw 'A trusted source SHA256 is required.'}
    if($Schema -cnotin @('dbo','eventdesk')){throw 'Unsupported migration schema.'}
    $root='C:\ProgramData\EventDeskPhotoApi\event-backend';$src=Join-Path $root 'src'
    $bootstrap="import runpy,sys;sys.path.insert(0,r'C:\ProgramData\EventDeskPhotoApi\event-backend\src');runpy.run_module('scripts.migrate_sql_event',run_name='__main__')"
    $arguments=@('-I','-c',$bootstrap,'--source',(Resolve-EventMigrationPath $Source),'--schema',$Schema)
    $arguments+=if($Commit){@('--commit','--connection-stdin')}else{@('--dry-run')}
    return [pscustomobject]@{Root=$root;SourceRoot=$src;Python=(Join-Path $root 'python\python.exe');Temp=(Join-Path $root 'temp');
        Source=(Resolve-EventMigrationPath $Source);Hash=$Hash.ToUpperInvariant();Schema=$Schema;Commit=[bool]$Commit;
        Arguments=(($arguments|ForEach-Object{Quote-EventMigrationArgument $_})-join ' ')}
}
function New-EventMigrationStartInfo($Plan){
    $start=New-Object Diagnostics.ProcessStartInfo($Plan.Python,$Plan.Arguments)
    $start.WorkingDirectory=$Plan.SourceRoot;$start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardInput=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $start.StandardOutputEncoding=New-Object Text.UTF8Encoding($false,$true)
    $start.StandardErrorEncoding=New-Object Text.UTF8Encoding($false)
    $windows=[Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
    $start.EnvironmentVariables.Clear();$start.EnvironmentVariables['SystemRoot']=$windows;$start.EnvironmentVariables['WINDIR']=$windows
    $start.EnvironmentVariables['PATH']=[IO.Path]::GetDirectoryName($Plan.Python)+';'+(Join-Path $windows 'System32')+';'+$windows
    $start.EnvironmentVariables['TEMP']=$Plan.Temp;$start.EnvironmentVariables['TMP']=$Plan.Temp
    return $start
}
function Get-EventMigrationInput([switch]$Commit,[scriptblock]$CredentialProvider,$Context){
    if(-not $Commit){return ,(New-Object byte[] 0)}
    try{$connection=&$CredentialProvider $Context;if($connection -isnot [string] -or $connection.Length -lt 1 -or $connection.Length -gt 8192){throw 'Invalid connection.'};return ,[Text.Encoding]::UTF8.GetBytes($connection)}
    catch{throw 'Protected SQL migration credentials are unavailable.'}
}
function Assert-EventMigrationAcl([Security.AccessControl.FileSystemSecurity]$Security,[ValidateSet('private','code','namespace')][string]$Scope){
    $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $privileged=@('S-1-5-18','S-1-5-32-544')
    $namespace=[Security.AccessControl.FileSystemRights]'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    $writes=[Security.AccessControl.FileSystemRights]'WriteData,AppendData,WriteExtendedAttributes,WriteAttributes,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    $owner=$Security.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if($owner -notin $trusted -or ($Scope -ne 'namespace' -and $owner -notin $privileged)){throw 'Migration path owner is not trusted.'}
    foreach($rule in $Security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
        if($rule.AccessControlType -ne 'Allow' -or ($rule.PropagationFlags-band[Security.AccessControl.PropagationFlags]::InheritOnly)-ne 0){continue}
        $sid=$rule.IdentityReference.Value
        if($Scope -eq 'namespace'){
            if($sid -notin $trusted -and ($rule.FileSystemRights-band$namespace)-ne 0){throw 'Migration ancestor can be replaced by an untrusted principal.'}
        }elseif($sid -notin $privileged -and ($Scope -eq 'private' -or ($rule.FileSystemRights-band$writes)-ne 0)){throw 'Migration file permissions are unsafe.'}
    }
}
function Assert-EventMigrationPathSecurity([string]$Path,[switch]$Private){
    $full=Resolve-EventMigrationPath $Path
    $first=$true
    for($current=$full;$current;$current=[IO.Path]::GetDirectoryName($current)){
        $attributes=[IO.File]::GetAttributes($current)
        if(($attributes-band[IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Migration paths cannot contain reparse points.'}
        $scope=if(-not $first){'namespace'}elseif($Private){'private'}else{'code'}
        Assert-EventMigrationAcl (Get-Acl -LiteralPath $current) $scope
        $first=$false
    }
}
function Get-EventMigrationContext{
    $bin='C:\ProgramData\EventDeskPhotoApi\bin'
    foreach($path in @($bin,(Join-Path $bin 'EventDeskPhotoApi.exe'),(Join-Path $bin 'EventBackendService.exe'))){Assert-EventMigrationPathSecurity $path}
    $photo=[Reflection.Assembly]::LoadFrom((Join-Path $bin 'EventDeskPhotoApi.exe'))
    $assembly=[Reflection.Assembly]::LoadFrom((Join-Path $bin 'EventBackendService.exe'))
    $layout=[Activator]::CreateInstance($assembly.GetType('EventDesk.EventBackendHost.EventBackendLayout',$true))
    $null=$assembly.GetType('EventDesk.EventBackendHost.BackendHostSecurity',$true).GetMethod('ValidateInstallation').Invoke($null,@($layout))
    $raw=[IO.File]::ReadAllBytes($layout.OptionsPath)
    try{
        if($raw.Length -gt 128*1024){throw 'Options too large.'}
        $arguments=New-Object object[] 1;$arguments[0]=[byte[]]$raw
        $type=$assembly.GetType('EventDesk.EventBackendHost.BackendConfiguration',$true)
        $options=$type.GetMethod('ParseOptions').Invoke($null,$arguments)
        $catalogPath=Join-Path $layout.SourceRoot 'eventdesk_storage\table_catalog.json'
        $catalog=([IO.File]::ReadAllText($catalogPath)|ConvertFrom-Json)
        return [pscustomobject]@{Assembly=$assembly;PhotoAssembly=$photo;ConfigurationType=$type;Schema=$options.Schema;Tables=@($catalog.PSObject.Properties.Name)}
    }finally{[Array]::Clear($raw,0,$raw.Length)}
}
function Get-EventProtectedMigrationConnection($Context){
    $settings=$Context.PhotoAssembly.GetType('EventDesk.PhotoApi.ConfigStore',$true).GetMethod('Load').Invoke($null,@())
    $driver=$Context.ConfigurationType.GetMethod('InstalledDriver').Invoke($null,@())
    return $Context.ConfigurationType.GetMethod('ToOdbc').Invoke($null,@($settings.ConnectionString,$driver))
}
function Convert-EventMigrationResult([string]$Output,[int]$ExitCode,[string[]]$Tables,[string]$Schema,[string]$FileHash){
    if($ExitCode -ne 0){throw 'Migration was not confirmed; review the target before retrying.'}
    try{
        $value=$Output|ConvertFrom-Json
        foreach($name in $value.PSObject.Properties.Name){if($name -cnotin @('mode','source_sha256','counts','table_sha256','already_imported')){throw 'Unexpected field.'}}
        if($value.mode -cnotin @('dry-run','commit') -or $value.source_sha256 -notmatch '^[a-f0-9]{64}$'){throw 'Invalid summary.'}
        $counts=[ordered]@{};$hashes=[ordered]@{}
        if(@($value.counts.PSObject.Properties).Count -ne $Tables.Count -or @($value.table_sha256.PSObject.Properties).Count -ne $Tables.Count){throw 'Invalid table summary.'}
        foreach($table in $Tables){
            if($table -notmatch '^[a-z_]{1,80}$'){throw 'Invalid catalog table.'}
            $count=$value.counts.$table;$digest=$value.table_sha256.$table
            if(($count -isnot [int] -and $count -isnot [long]) -or $count -lt 0 -or $count -gt 100000 -or $digest -notmatch '^[a-f0-9]{64}$'){throw 'Invalid table summary.'}
            $counts[$table]=$count;$hashes[$table]=$digest
        }
        $already=$false
        if($value.PSObject.Properties['already_imported']){if($value.already_imported -isnot [bool]){throw 'Invalid receipt summary.'};$already=$value.already_imported}
        return [pscustomobject]@{Mode=$value.mode;Schema=$Schema;SourceFileSha256=$FileHash;SnapshotSha256=$value.source_sha256;Counts=$counts;TableSha256=$hashes;AlreadyImported=$already}
    }catch{throw 'Unexpected migration result; no automatic retry was attempted.'}
}
function Initialize-EventMigrationRunner{
    if('EventDeskMigrationBridge.Runner'-as[type]){return}
    Add-Type -TypeDefinition @'
using System; using System.Diagnostics; using System.IO; using System.Reflection; using System.Text; using System.Threading.Tasks;
namespace EventDeskMigrationBridge {
 public sealed class Result { public readonly int ExitCode; public readonly string Output; public Result(int exit,string output){ExitCode=exit;Output=output;} }
 public static class Runner {
  public static Result Run(ProcessStartInfo info,byte[] input,IDisposable job,int timeoutMilliseconds){
   if(info==null||input==null||job==null||input.Length>65536||timeoutMilliseconds<1||timeoutMilliseconds>600000)throw new ArgumentException("Invalid migration process.");
   Stopwatch deadline=Stopwatch.StartNew();
   Process child=new Process();child.StartInfo=info;
   Action abort=delegate{try{((IDisposable)job).Dispose();}catch{}try{if(!child.HasExited)child.Kill();}catch(InvalidOperationException){}catch(System.ComponentModel.Win32Exception){}};
   try{
    if(!child.Start())throw new InvalidOperationException("Migration process failed.");
    job.GetType().GetMethod("Assign").Invoke(job,new object[]{child});
    Task<string> output=Task.Factory.StartNew(delegate{try{return ReadOutput(child.StandardOutput);}catch{abort();throw;}});
    Task error=Task.Factory.StartNew(delegate{char[] discard=new char[1024];try{while(child.StandardError.Read(discard,0,discard.Length)>0)Array.Clear(discard,0,discard.Length);}catch(IOException){}finally{Array.Clear(discard,0,discard.Length);}});
    Observe(output);Observe(error);
    if(input.Length==0)child.StandardInput.Close();
    else{
     Stream pipe=child.StandardInput.BaseStream;
     Task writing=Task.Factory.StartNew(delegate{pipe.Write(input,0,input.Length);pipe.Flush();});
     Observe(writing);
     if(!writing.Wait(Math.Min(15000,Remaining(deadline,timeoutMilliseconds)))){abort();writing.ContinueWith(delegate(Task done){GC.KeepAlive(done.Exception);},TaskContinuationOptions.OnlyOnFaulted);throw new TimeoutException("Migration input timed out.");}
     pipe.Dispose();
    }
    if(!child.WaitForExit(Remaining(deadline,timeoutMilliseconds))){abort();throw new TimeoutException("Migration process timed out.");}
    if(!Task.WaitAll(new Task[]{output,error},5000))throw new TimeoutException("Migration output timed out.");
    return new Result(child.ExitCode,output.Result);
   }catch{throw new InvalidOperationException("Migration process did not complete; inspect its receipt before retrying.");}
   finally{abort();child.Dispose();}
  }
  private static int Remaining(Stopwatch elapsed,int total){long remaining=total-elapsed.ElapsedMilliseconds;if(remaining<=0)throw new TimeoutException("Migration process timed out.");return (int)remaining;}
  private static void Observe(Task task){task.ContinueWith(delegate(Task done){GC.KeepAlive(done.Exception);},TaskContinuationOptions.OnlyOnFaulted);}
  private static string ReadOutput(StreamReader stream){char[] buffer=new char[4096];StringBuilder text=new StringBuilder();int read;while((read=stream.Read(buffer,0,buffer.Length))>0){if(text.Length+read>1048576)throw new InvalidDataException("Migration output exceeds its limit.");text.Append(buffer,0,read);}return text.ToString();}
 }
}
'@
}
function Invoke-EventBackendMigration([string]$Source,[string]$Hash,[switch]$Commit){
    if($Commit){
        if([Environment]::MachineName -ne 'PHOTO-API-HOST'){throw 'Commit is permitted only on the approved API host.'}
        $principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'An elevated migration session is required.'}
        if((Get-Service -Name 'EventDeskEventBackend' -ErrorAction Stop).Status -ne 'Stopped'){throw 'Keep the event backend stopped during migration.'}
    }
    $context=Get-EventMigrationContext
    $plan=Get-EventMigrationPlan $Source $Hash $context.Schema -Commit:$Commit
    Assert-EventMigrationPathSecurity $plan.Source -Private
    # A pinned SQLite main file must not acquire attacker-controlled WAL sidecars
    # after the sidecar check. Its closest parent therefore forbids all writes.
    Assert-EventMigrationPathSecurity ([IO.Path]::GetDirectoryName($plan.Source))
    $file=[IO.File]::Open($plan.Source,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $inputBytes=$null;$job=$null
    try{
        if($file.Length -lt 1 -or $file.Length -gt 128*1024*1024){throw 'Migration source is empty or exceeds its size limit.'}
        $sha=[Security.Cryptography.SHA256]::Create()
        try{$actual=([BitConverter]::ToString($sha.ComputeHash($file))).Replace('-','')}finally{$sha.Dispose()}
        if($actual -ne $plan.Hash){throw 'Migration source SHA256 does not match the trusted export.'}
        $file.Position=0;$magic=New-Object byte[] 16;$read=$file.Read($magic,0,16)
        if($read -eq 16 -and [Text.Encoding]::ASCII.GetString($magic) -eq "SQLite format 3`0"){
            foreach($suffix in @('-wal','-shm','-journal')){if(Test-Path -LiteralPath ($plan.Source+$suffix)){throw 'Use a standalone SQLite backup or complete JSON export without sidecars.'}}
        }
        $provider={param($ProtectedContext) Get-EventProtectedMigrationConnection $ProtectedContext}
        $inputBytes=Get-EventMigrationInput -Commit:$Commit -CredentialProvider $provider -Context $context
        Initialize-EventMigrationRunner
        $job=[Activator]::CreateInstance($context.Assembly.GetType('EventDesk.EventBackendHost.ChildJob',$true),$true)
        $result=[EventDeskMigrationBridge.Runner]::Run((New-EventMigrationStartInfo $plan),$inputBytes,$job,300000)
        $safe=Convert-EventMigrationResult $result.Output $result.ExitCode $context.Tables $context.Schema $plan.Hash
        if(($Commit -and $safe.Mode -ne 'commit') -or (-not $Commit -and $safe.Mode -ne 'dry-run')){throw 'Migration mode acknowledgment does not match the requested action.'}
        return $safe
    }finally{if($job){$job.Dispose()};if($inputBytes){[Array]::Clear($inputBytes,0,$inputBytes.Length)};$file.Dispose()}
}
if($MyInvocation.InvocationName -ne '.'){Invoke-EventBackendMigration $SourcePath $ExpectedSourceSha256 -Commit:$Commit}
