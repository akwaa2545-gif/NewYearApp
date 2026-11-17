<#
Plan only by default. -Install registers, but never starts, the SQL backend.
The reviewed manifest hash must arrive through a trusted channel. SQL migration
and driver installation are separate operations. Dot source for pure tests.
#>
param([string]$SourceDirectory, [string]$ExpectedManifestSha256,
    [string]$PythonBase = 'C:\Python314', [string]$OptionsPath, [switch]$Install)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Resolve-EventLocalPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -notmatch '^[A-Za-z]:\\' -or $Path.Substring(2).Contains(':') -or
        @($Path.Split('\') | Where-Object { $_ -eq '..' -or $_ -eq '.' }).Count -gt 0) { throw 'An absolute local Windows path is required.' }
    $absolute=[IO.Path]::GetFullPath($Path)
    if($absolute -eq [IO.Path]::GetPathRoot($absolute)){return $absolute}
    return $absolute.TrimEnd('\')
}
function Get-EventInstallPlan([string]$Source, [string]$Hash, [string]$Python, [string]$Options) {
    if ($Hash -notmatch '^[0-9a-fA-F]{64}$') { throw 'A trusted SHA256 manifest hash is required.' }
    $photoRoot = 'C:\ProgramData\EventDeskPhotoApi'
    $sourcePath = Resolve-EventLocalPath $Source
    $pythonPath = Resolve-EventLocalPath $Python
    $optionsFile = Resolve-EventLocalPath $Options
    foreach ($path in @($sourcePath, $pythonPath)) {
        if ($path.Equals($photoRoot, [StringComparison]::OrdinalIgnoreCase) -or $path.StartsWith($photoRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Stage source files outside the installation directory.' }
    }
    $binary = Join-Path $photoRoot 'bin\EventBackendService.exe'
    return [pscustomobject]@{ PhotoRoot=$photoRoot; Root=(Join-Path $photoRoot 'event-backend'); Bin=(Join-Path $photoRoot 'bin');
        PhotoBinary=(Join-Path $photoRoot 'bin\EventDeskPhotoApi.exe'); Binary=$binary; BinaryCommand=('"' + $binary + '"');
        ServiceName='EventDeskEventBackend'; PhotoServiceName='EventDeskPhotoApi'; Source=$sourcePath; PythonBase=$pythonPath;
        OptionsSource=$optionsFile; ManifestHash=$Hash.ToUpperInvariant() }
}
function Resolve-EventPackagePath([string]$Root, [string]$Relative) {
    if ($Relative -cnotmatch '^(src|packages|bin)/[^/]+(?:/[^/]+)*$' -or $Relative.Contains('\') -or $Relative.Contains(':')) { throw 'Invalid package path.' }
    foreach ($part in $Relative.Split('/')) {
        if ($part -in @('.', '..') -or $part -notmatch '^[A-Za-z0-9_.-]+$' -or $part.EndsWith('.') -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { throw 'Unsafe package path segment.' }
    }
    if ($Relative.StartsWith('bin/', [StringComparison]::Ordinal) -and $Relative -cnotin @('bin/EventDeskPhotoApi.exe', 'bin/EventBackendService.exe')) { throw 'Unexpected binary in package.' }
    if ($Relative.StartsWith('src/', [StringComparison]::Ordinal) -and $Relative -notmatch '\.(py|json)$') { throw 'Only approved Python runtime source and JSON belong in src.' }
    $base = Resolve-EventLocalPath $Root
    $absolute = [IO.Path]::GetFullPath((Join-Path $base $Relative.Replace('/', '\')))
    if (-not $absolute.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Package path escaped its root.' }
    return $absolute
}
function Assert-EventNoReparsePath([string]$Path) {
    $current = Resolve-EventLocalPath $Path
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not allowed in installation paths.' }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }; $current = $parent
    }
}
function Get-EventTreeFiles([string]$Root) {
    Assert-EventNoReparsePath $Root
    $stack = New-Object 'Collections.Generic.Stack[string]'
    $stack.Push((Resolve-EventLocalPath $Root))
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        foreach ($path in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = [IO.File]::GetAttributes($path)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are forbidden in source trees.' }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) { $stack.Push($path) }
            else { [pscustomobject]@{Path=$path;Relative=$path.Substring($Root.TrimEnd('\').Length + 1).Replace('\','/')} }
        }
    }
}
function Read-EventBytes([string]$Path, [int]$Limit, [switch]$AllowEmpty) {
    Assert-EventNoReparsePath $Path
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if (($stream.Length -eq 0 -and -not $AllowEmpty) -or $stream.Length -gt $Limit) { throw 'Source file exceeds its allowed size.' }
        $bytes = New-Object byte[] ([int]$stream.Length); $offset=0
        while ($offset -lt $bytes.Length) { $read=$stream.Read($bytes,$offset,$bytes.Length-$offset); if($read -eq 0){throw 'Incomplete source file.'};$offset+=$read }
        return ,$bytes
    } finally { $stream.Dispose() }
}
function Get-EventBytesHash([byte[]]$Bytes) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-','') } finally { $sha.Dispose() }
}
function Get-EventJsonName([Xml.XmlElement]$Element) {
    if ($Element.NamespaceURI -eq 'item' -and $Element.HasAttribute('item')) { return $Element.GetAttribute('item') }
    return $Element.LocalName
}
function Read-EventPackage([string]$Root, [string]$ExpectedHash) {
    if ($ExpectedHash -notmatch '^[0-9a-fA-F]{64}$') { throw 'Invalid manifest hash.' }
    $manifest = Join-Path (Resolve-EventLocalPath $Root) 'package_manifest.json'
    $bytes=Read-EventBytes $manifest (4*1024*1024)
    if ((Get-EventBytesHash $bytes) -ne $ExpectedHash) { throw 'Manifest SHA256 verification failed.' }
    Add-Type -AssemblyName System.Runtime.Serialization
    $document=New-Object Xml.XmlDocument; $document.XmlResolver=$null
    $reader=[Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader($bytes,[Xml.XmlDictionaryReaderQuotas]::Max)
    try { $document.Load($reader) } finally { $reader.Dispose() }
    $rootNode=$document.DocumentElement
    if ($rootNode.GetAttribute('type') -ne 'object' -or $rootNode.ChildNodes.Count -ne 2) { throw 'Invalid package manifest.' }
    $fields=New-Object 'Collections.Generic.Dictionary[string,Xml.XmlElement]' ([StringComparer]::Ordinal)
    foreach($node in $rootNode.ChildNodes){$name=Get-EventJsonName $node;if($name -cnotin @('schema_version','files') -or $fields.ContainsKey($name)){throw 'Unexpected manifest field.'};$fields.Add($name,$node)}
    if(-not $fields.ContainsKey('schema_version') -or -not $fields.ContainsKey('files') -or $fields['schema_version'].GetAttribute('type') -ne 'number' -or $fields['schema_version'].InnerText -ne '1' -or $fields['files'].GetAttribute('type') -ne 'object'){throw 'Unsupported manifest schema.'}
    $files=New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach($node in $fields['files'].ChildNodes){
        $relative=Get-EventJsonName $node; $null=Resolve-EventPackagePath $Root $relative
        if($files.ContainsKey($relative) -or $node.GetAttribute('type') -ne 'string' -or $node.InnerText -notmatch '^[a-fA-F0-9]{64}$'){throw 'Duplicate or invalid manifest entry.'}
        $files.Add($relative,$node.InnerText.ToUpperInvariant())
    }
    foreach($required in @('src/app.py','src/event_backend_entry.py','src/photo_api/event_backend.py','src/eventdesk_storage/__init__.py','bin/EventDeskPhotoApi.exe','bin/EventBackendService.exe')){if(-not $files.ContainsKey($required)){throw 'Required runtime file is missing.'}}
    $actual=@(Get-EventTreeFiles $Root)
    if($actual.Count -ne $files.Count+1){throw 'Package contains unmanifested or missing files.'}
    foreach($file in $actual){if($file.Relative -eq 'package_manifest.json'){continue};$null=Resolve-EventPackagePath $Root $file.Relative;if(-not $files.ContainsKey($file.Relative) -or (Get-FileHash -LiteralPath $file.Path -Algorithm SHA256).Hash -ne $files[$file.Relative]){throw 'Package payload SHA256 verification failed.'}}
    return [pscustomobject]@{Root=(Resolve-EventLocalPath $Root);Files=$files;ManifestHash=$ExpectedHash.ToUpperInvariant()}
}
function New-EventPrivateDirectorySecurity {
    $security=New-Object Security.AccessControl.DirectorySecurity;$security.SetAccessRuleProtection($true,$false)
    $security.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$rule=New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),[Security.AccessControl.FileSystemRights]::FullControl,([Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'),[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow);$security.AddAccessRule($rule)}
    return $security
}
function New-EventPrivateFileSecurity {
    $security=New-Object Security.AccessControl.FileSecurity;$security.SetAccessRuleProtection($true,$false)
    $security.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.AccessControlType]::Allow)))}
    return $security
}
function Assert-EventPythonAcl([Security.AccessControl.FileSystemSecurity]$Security) {
    $owner=$Security.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if($owner -notin @('S-1-5-18','S-1-5-32-544')){throw 'Python source owner must be SYSTEM or Administrators.'}
    $writes=[Security.AccessControl.FileSystemRights]'WriteData,AppendData,WriteExtendedAttributes,WriteAttributes,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    foreach($rule in $Security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
        if($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544') -and ($rule.FileSystemRights -band $writes) -ne 0){throw 'Python source is writable by an untrusted principal.'}
    }
}
function Assert-EventNamespaceAcl([Security.AccessControl.FileSystemSecurity]$Security) {
    $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $owner=$Security.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if($owner -notin $trusted){throw 'Installation ancestor has an untrusted owner.'}
    # Creating a new sibling does not permit replacement of an existing child.
    # Reject effective rights that can rename/delete a protected path or its ACL.
    $replacement=[Security.AccessControl.FileSystemRights]'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    foreach($rule in $Security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
        if(($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0){continue}
        if($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $trusted -and ($rule.FileSystemRights -band $replacement) -ne 0){throw 'Installation ancestor permits untrusted namespace replacement.'}
    }
}
function Assert-EventTrustedNamespace([string]$Path) {
    Assert-EventNoReparsePath $Path
    for($directory=Resolve-EventLocalPath $Path;$directory;$directory=[IO.Path]::GetDirectoryName($directory)){
        if(Test-Path -LiteralPath $directory -PathType Container){Assert-EventNamespaceAcl (Get-Acl -LiteralPath $directory)}
    }
}
function Get-EventPythonFiles([string]$Root,[switch]$Trusted) {
    $checked=New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    if($Trusted){Assert-EventTrustedNamespace $Root;Assert-EventPythonAcl (Get-Acl -LiteralPath $Root);$null=$checked.Add((Resolve-EventLocalPath $Root))}
    foreach($file in (Get-EventTreeFiles $Root)){
        $parts=$file.Relative.Split('/')
        if($file.Relative -match '(^|/)(__pycache__|Scripts)(/|$)' -or $file.Relative -match '^Lib/site-packages(/|$)' -or
            $parts[-1] -match '^(?i:pyvenv\.cfg|sitecustomize\.py|usercustomize\.py|python(?:\d+)?\._pth)$' -or $file.Relative -match '\.py[co]$'){continue}
        if($file.Relative -notmatch '^(DLLs|Lib)/' -and $file.Relative -notmatch '^(?i:python(?:w|\d+)?\.(exe|dll|zip)|vcruntime[^/]*\.dll|LICENSE(?:\.txt)?)$'){continue}
        if($Trusted){
            Assert-EventPythonAcl (Get-Acl -LiteralPath $file.Path)
            for($dir=[IO.Path]::GetDirectoryName($file.Path);$dir -and $dir.StartsWith($Root+'\',[StringComparison]::OrdinalIgnoreCase);$dir=[IO.Path]::GetDirectoryName($dir)){if(-not $checked.Add($dir)){break};Assert-EventPythonAcl (Get-Acl -LiteralPath $dir)}
        }
        [pscustomobject]@{Path=$file.Path;Relative=$file.Relative;Hash=(Get-FileHash -LiteralPath $file.Path -Algorithm SHA256).Hash}
    }
}
function Get-EventServiceCommands($Plan) {
    return [pscustomobject]@{Account='LocalSystem';Dependencies="EventDeskPhotoApi`0`0";
        RecoveryArguments=@('failure',$Plan.ServiceName,'reset=','86400','actions=','restart/60000/restart/60000/restart/60000');
        FailureFlagArguments=@('failureflag',$Plan.ServiceName,'1')}
}
function Initialize-EventInstallerNative {
    if('EventDeskEventInstaller.Native' -as [type]){return}
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using Microsoft.Win32.SafeHandles;
namespace EventDeskEventInstaller {
 [StructLayout(LayoutKind.Sequential)] public struct SecurityAttributes { public int Length; public IntPtr Descriptor; [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle; }
 public static class Native {
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] public static extern bool CreateDirectory(string path,ref SecurityAttributes attributes);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern SafeFileHandle CreateFile(string path,uint access,uint share,ref SecurityAttributes attributes,uint disposition,uint flags,IntPtr template);
  [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern IntPtr OpenSCManager(string machine,string database,uint access);
  [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern IntPtr CreateService(IntPtr manager,string name,string display,uint access,uint type,uint start,uint error,string binary,string group,IntPtr tag,string dependencies,string account,string password);
  [DllImport("advapi32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] public static extern bool DeleteService(IntPtr service);
  [DllImport("advapi32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] public static extern bool CloseServiceHandle(IntPtr handle);
 }
}
'@
}
function New-EventProtectedDirectory([string]$Path) {
    Assert-EventNoReparsePath $Path
    $security=New-EventPrivateDirectorySecurity;$bytes=$security.GetSecurityDescriptorBinaryForm();$pointer=[Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
    try{
        [Runtime.InteropServices.Marshal]::Copy($bytes,0,$pointer,$bytes.Length)
        $attributes=New-Object EventDeskEventInstaller.SecurityAttributes;$attributes.Length=[Runtime.InteropServices.Marshal]::SizeOf($attributes);$attributes.Descriptor=$pointer
        if(-not [EventDeskEventInstaller.Native]::CreateDirectory($Path,[ref]$attributes)){throw 'Protected directory creation failed.'}
        $actual=Get-Acl -LiteralPath $Path
        if($actual.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]'Access,Owner') -ne $security.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]'Access,Owner')){throw 'Protected directory security verification failed.'}
    }finally{[Runtime.InteropServices.Marshal]::FreeHGlobal($pointer)}
}
function Ensure-EventProtectedParent([string]$Root,[string]$Path) {
    $directory=[IO.Path]::GetDirectoryName($Path)
    if(-not $directory.Equals($Root,[StringComparison]::OrdinalIgnoreCase) -and -not $directory.StartsWith($Root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Copy target escaped protected root.'}
    if(-not (Test-Path -LiteralPath $directory)){Ensure-EventProtectedParent $Root $directory;New-EventProtectedDirectory $directory}
    Assert-EventNoReparsePath $directory
}
function Write-EventProtectedBytes([string]$Path,[byte[]]$Bytes,[Security.AccessControl.FileSecurity]$Security) {
    Assert-EventNoReparsePath $Path
    $descriptor=$Security.GetSecurityDescriptorBinaryForm();$pointer=[Runtime.InteropServices.Marshal]::AllocHGlobal($descriptor.Length);$handle=$null
    try{
        [Runtime.InteropServices.Marshal]::Copy($descriptor,0,$pointer,$descriptor.Length)
        $attributes=New-Object EventDeskEventInstaller.SecurityAttributes;$attributes.Length=[Runtime.InteropServices.Marshal]::SizeOf($attributes);$attributes.Descriptor=$pointer
        $handle=[EventDeskEventInstaller.Native]::CreateFile($Path,0x40000000,0,[ref]$attributes,1,0x80,[IntPtr]::Zero)
        if($handle.IsInvalid){throw 'Protected file creation failed.'}
        $stream=New-Object IO.FileStream($handle,[IO.FileAccess]::Write)
        try{$stream.Write($Bytes,0,$Bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    }finally{if($handle){$handle.Dispose()};[Runtime.InteropServices.Marshal]::FreeHGlobal($pointer)}
    $actual=Get-Acl -LiteralPath $Path
    if($actual.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]'Access,Owner') -ne $Security.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]'Access,Owner')){throw 'Protected file security verification failed.'}
}
function Copy-EventVerifiedFile([string]$Source,[string]$Target,[string]$Hash,[Security.AccessControl.FileSecurity]$Security) {
    $bytes=Read-EventBytes $Source (256*1024*1024) -AllowEmpty
    try{if((Get-EventBytesHash $bytes) -ne $Hash){throw 'Source changed after verification.'};Write-EventProtectedBytes $Target $bytes $Security}
    finally{[Array]::Clear($bytes,0,$bytes.Length)}
    if((Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash -ne $Hash){throw 'Copied payload SHA256 verification failed.'}
}
function Read-EventOptions([string]$Path,$Package) {
    $photo=Read-EventBytes (Resolve-EventPackagePath $Package.Root 'bin/EventDeskPhotoApi.exe') (16*1024*1024)
    $backendAssemblyBytes=Read-EventBytes (Resolve-EventPackagePath $Package.Root 'bin/EventBackendService.exe') (16*1024*1024)
    if((Get-EventBytesHash $photo) -ne $Package.Files['bin/EventDeskPhotoApi.exe'] -or (Get-EventBytesHash $backendAssemblyBytes) -ne $Package.Files['bin/EventBackendService.exe']){throw 'Validator binary changed after package verification.'}
    $null=[Reflection.Assembly]::Load($photo);$assembly=[Reflection.Assembly]::Load($backendAssemblyBytes)
    $bytes=Read-EventBytes $Path (128*1024)
    try{
        $type=$assembly.GetType('EventDesk.EventBackendHost.BackendConfiguration',$true)
        $arguments=New-Object object[] 1;$arguments[0]=[byte[]]$bytes.PSObject.BaseObject
        $null=$type.GetMethod('ParseOptions').Invoke($null,$arguments)
        return ,$bytes
    }catch{[Array]::Clear($bytes,0,$bytes.Length);throw 'Private backend options validation failed.'}
}
function Invoke-EventNative([string]$Executable,[string[]]$Arguments) {
    $null=& $Executable @Arguments 2>&1
    if($LASTEXITCODE -ne 0){throw 'Windows service recovery configuration failed.'}
}
function Invoke-EventBackendInstall([string]$Source,[string]$Hash,[string]$Python,[string]$Options,[switch]$Install) {
    $plan=Get-EventInstallPlan $Source $Hash $Python $Options
    $package=Read-EventPackage $plan.Source $plan.ManifestHash
    $optionsBytes=Read-EventOptions $plan.OptionsSource $package
    try{
        if($Install){
            if([Environment]::MachineName -ne 'PHOTO-API-HOST'){throw 'Installation is allowed only on PHOTO-API-HOST.'}
            $principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
            if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Run this installer as Administrator on the approved host.'}
        }
        $pythonFiles=@(Get-EventPythonFiles $plan.PythonBase -Trusted:$Install)
        if(@($pythonFiles | Where-Object {$_.Relative -eq 'python.exe'}).Count -ne 1 -or @($pythonFiles | Where-Object {$_.Relative -eq 'Lib/os.py' -or $_.Relative -match '^python\d+\.zip$'}).Count -lt 1){throw 'PythonBase does not contain a complete interpreter/stdlib.'}
        if(-not $Install){return [pscustomobject]@{Mode='DryRun';Target=$plan.Root;Service=$plan.ServiceName;PackageFiles=$package.Files.Count;PythonFiles=$pythonFiles.Count;BackendWillStart=$false}}
        if(Test-Path -LiteralPath $plan.Root){throw 'Backend runtime already exists; no in-place overwrite is allowed.'}
        if(Test-Path -LiteralPath $plan.Binary){throw 'Backend service binary already exists; review the existing installation.'}
        Assert-EventTrustedNamespace $plan.PhotoRoot;Assert-EventTrustedNamespace $plan.Bin
        Assert-EventPythonAcl (Get-Acl -LiteralPath $plan.PhotoRoot);Assert-EventPythonAcl (Get-Acl -LiteralPath $plan.Bin)
        $existingBackups=Join-Path $plan.PhotoRoot 'backups'
        if(Test-Path -LiteralPath $existingBackups){Assert-EventTrustedNamespace $existingBackups;Assert-EventPythonAcl (Get-Acl -LiteralPath $existingBackups)}
        $existing=Get-Service -Name $plan.ServiceName -ErrorAction SilentlyContinue
        if($existing){throw 'Event backend service already exists; no service will be overwritten.'}
        $photoService=Get-Service -Name $plan.PhotoServiceName -ErrorAction Stop
        if($photoService.Status -ne 'Running'){throw 'The original photo service must be running before installation.'}
        # The verified helper checks driver availability without reading DPAPI or SQL.
        $hostType=([AppDomain]::CurrentDomain.GetAssemblies() | Where-Object {$_.GetName().Name -eq 'EventBackendService'} | Select-Object -Last 1).GetType('EventDesk.EventBackendHost.BackendConfiguration',$true)
        $null=$hostType.GetMethod('InstalledDriver').Invoke($null,@())
        $photoSecurity=Get-Acl -LiteralPath $plan.PhotoBinary;Assert-EventPythonAcl $photoSecurity
        $oldHash=(Get-FileHash -LiteralPath $plan.PhotoBinary -Algorithm SHA256).Hash
        Initialize-EventInstallerNative
        $manager=[IntPtr]::Zero;$service=[IntPtr]::Zero;$serviceCreated=$false;$photoStopAttempted=$false;$photoChanged=$false;$backup=$null
        try{
            New-EventProtectedDirectory $plan.Root
            foreach($directory in @('python','src','temp')){New-EventProtectedDirectory (Join-Path $plan.Root $directory)}
            $private=New-EventPrivateFileSecurity
            foreach($file in $pythonFiles){$target=Join-Path (Join-Path $plan.Root 'python') $file.Relative.Replace('/','\');Ensure-EventProtectedParent $plan.Root $target;Copy-EventVerifiedFile $file.Path $target $file.Hash $private}
            foreach($relative in $package.Files.Keys){
                if($relative.StartsWith('bin/',[StringComparison]::Ordinal)){continue}
                $target=if($relative.StartsWith('src/',[StringComparison]::Ordinal)){Join-Path (Join-Path $plan.Root 'src') $relative.Substring(4).Replace('/','\')}else{Join-Path (Join-Path $plan.Root 'python\Lib\site-packages') $relative.Substring(9).Replace('/','\')}
                Ensure-EventProtectedParent $plan.Root $target;Copy-EventVerifiedFile (Resolve-EventPackagePath $plan.Source $relative) $target $package.Files[$relative] $private
            }
            Write-EventProtectedBytes (Join-Path $plan.Root 'options.json') $optionsBytes $private
            $backupRoot=Join-Path $plan.PhotoRoot 'backups'
            if(-not (Test-Path -LiteralPath $backupRoot)){New-EventProtectedDirectory $backupRoot}
            Assert-EventNoReparsePath $backupRoot
            $backupDirectory=Join-Path $backupRoot ([guid]::NewGuid().ToString('N'));New-EventProtectedDirectory $backupDirectory
            $backup=Join-Path $backupDirectory 'EventDeskPhotoApi.exe';Copy-EventVerifiedFile $plan.PhotoBinary $backup $oldHash $private
            $photoStopAttempted=$true
            Stop-Service -Name $plan.PhotoServiceName -ErrorAction Stop
            (Get-Service -Name $plan.PhotoServiceName).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(45))
            $temporary=Join-Path $plan.Bin ('EventDeskPhotoApi.'+[guid]::NewGuid().ToString('N')+'.new')
            Copy-EventVerifiedFile (Resolve-EventPackagePath $plan.Source 'bin/EventDeskPhotoApi.exe') $temporary $package.Files['bin/EventDeskPhotoApi.exe'] $photoSecurity
            $photoChanged=$true
            [IO.File]::Replace($temporary,$plan.PhotoBinary,[NullString]::Value)
            if((Get-FileHash -LiteralPath $plan.PhotoBinary -Algorithm SHA256).Hash -ne $package.Files['bin/EventDeskPhotoApi.exe']){throw 'Photo API upgrade verification failed.'}
            Copy-EventVerifiedFile (Resolve-EventPackagePath $plan.Source 'bin/EventBackendService.exe') $plan.Binary $package.Files['bin/EventBackendService.exe'] $private
            $commands=Get-EventServiceCommands $plan
            $manager=[EventDeskEventInstaller.Native]::OpenSCManager([NullString]::Value,[NullString]::Value,3)
            if($manager -eq [IntPtr]::Zero){throw 'Cannot open service manager.'}
            $service=[EventDeskEventInstaller.Native]::CreateService($manager,$plan.ServiceName,'EventDesk Event Backend',0x000F01FF,0x10,2,1,$plan.BinaryCommand,[NullString]::Value,[IntPtr]::Zero,$commands.Dependencies,$commands.Account,[NullString]::Value)
            if($service -eq [IntPtr]::Zero){throw 'Cannot register the new backend service.'};$serviceCreated=$true
            $sc=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'sc.exe';Invoke-EventNative $sc $commands.RecoveryArguments;Invoke-EventNative $sc $commands.FailureFlagArguments
            Start-Service -Name $plan.PhotoServiceName -ErrorAction Stop
            (Get-Service -Name $plan.PhotoServiceName).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(45))
            return [pscustomobject]@{Mode='InstalledNotStarted';Target=$plan.Root;Service=$plan.ServiceName;PhotoBackupSha256=$oldHash;BackendWillStart=$false}
        }catch{
            $rollbackFailed=$false
            if($serviceCreated){if(-not [EventDeskEventInstaller.Native]::DeleteService($service)){$rollbackFailed=$true}}
            if($photoStopAttempted){
                try{
                    $recovering=Get-Service -Name $plan.PhotoServiceName -ErrorAction Stop
                    if($recovering.Status -eq 'StopPending'){$recovering.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(45))}
                    if($photoChanged){
                        if((Get-Service -Name $plan.PhotoServiceName).Status -ne 'Stopped'){Stop-Service -Name $plan.PhotoServiceName -ErrorAction Stop;(Get-Service -Name $plan.PhotoServiceName).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(45))}
                        $restore=Join-Path $plan.Bin ('EventDeskPhotoApi.restore.'+[guid]::NewGuid().ToString('N')+'.new')
                        Copy-EventVerifiedFile $backup $restore $oldHash $photoSecurity;[IO.File]::Replace($restore,$plan.PhotoBinary,[NullString]::Value)
                    }
                    $recovering=Get-Service -Name $plan.PhotoServiceName -ErrorAction Stop
                    if($recovering.Status -eq 'StartPending'){$recovering.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(45))}
                    elseif($recovering.Status -ne 'Running'){Start-Service -Name $plan.PhotoServiceName -ErrorAction Stop}
                    (Get-Service -Name $plan.PhotoServiceName).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(45))
                }catch{$rollbackFailed=$true}
            }
            if($rollbackFailed){throw 'Installation failed; photo rollback needs operator review. Protected diagnostic files were retained.'}
            throw 'Installation failed; original photo binary/service retained or restored. Protected diagnostic files were retained.'
        }finally{
            if($service -ne [IntPtr]::Zero){$null=[EventDeskEventInstaller.Native]::CloseServiceHandle($service)}
            if($manager -ne [IntPtr]::Zero){$null=[EventDeskEventInstaller.Native]::CloseServiceHandle($manager)}
        }
    }finally{[Array]::Clear($optionsBytes,0,$optionsBytes.Length)}
}
if($MyInvocation.InvocationName -ne '.') { Invoke-EventBackendInstall $SourceDirectory $ExpectedManifestSha256 $PythonBase $OptionsPath -Install:$Install }
