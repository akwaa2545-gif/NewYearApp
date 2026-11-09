<#
Install reviewed cloudflared once on the approved host. Never starts services.
Configure the remote-managed tunnel token separately using -ConfigureToken and
-ExpectedTunnelId; its only secret input is stdin. SCM arguments contain a path.
Official cloudflared 2026.10.7 windows_service.go requires the fixed Cloudflared
service name and Application EventLog source, and logs its command arguments.
Dot sourcing exposes helpers without making machine changes.
#>
param([string]$SourceBinary, [string]$ExpectedSha256, [switch]$ConfigureToken, [string]$ExpectedTunnelId)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
function Get-TunnelServiceSid([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) { throw 'A service name is required.' }
    $sha = [System.Security.Cryptography.SHA1]::Create()
    try { $bytes = $sha.ComputeHash([System.Text.Encoding]::Unicode.GetBytes($Name.ToUpperInvariant())) }
    finally { $sha.Dispose() }
    $parts = for ($offset = 0; $offset -lt 20; $offset += 4) { [BitConverter]::ToUInt32($bytes, $offset) }
    return 'S-1-5-80-' + ($parts -join '-')
}

function Assert-TunnelBinaryHash([string]$Path, [string]$Expected) {
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    if (-not $actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SHA256 verification failed for $Path."
    }
}

function New-TunnelDirectorySecurity([string]$ServiceSid, [switch]$Logs) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $none = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    foreach ($principal in @('S-1-5-18', 'S-1-5-32-544', $ServiceSid)) {
        $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
        if ($principal -eq $ServiceSid) {
            $rights = [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
            if ($Logs) { $rights = [System.Security.AccessControl.FileSystemRights]::Modify }
        }
        $identity = New-Object System.Security.Principal.SecurityIdentifier($principal)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, $rights, $inherit, $none, $allow)
        $acl.AddAccessRule($rule)
    }
    return $acl
}

function Initialize-TunnelInstallerNative {
    if ('EventDeskTunnelInstaller.Native' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace EventDeskTunnelInstaller {
    [StructLayout(LayoutKind.Sequential)]
    public struct SecurityAttributes {
        public int Length;
        public IntPtr Descriptor;
        [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
    }
    public static class Native {
        [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern IntPtr OpenSCManager(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern IntPtr OpenService(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern IntPtr CreateService(IntPtr manager, string name, string display,
            uint access, uint type, uint start, uint error, string binary, string group,
            IntPtr tag, string dependencies, string account, string password);
        [DllImport("advapi32.dll", SetLastError=true)]
        [return:MarshalAs(UnmanagedType.Bool)] public static extern bool DeleteService(IntPtr service);
        [DllImport("advapi32.dll", SetLastError=true)]
        [return:MarshalAs(UnmanagedType.Bool)] public static extern bool CloseServiceHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        [return:MarshalAs(UnmanagedType.Bool)]
        public static extern bool CreateDirectory(string path, ref SecurityAttributes attributes);
    }
}
'@
}

function Get-TunnelWin32Failure([string]$Operation) {
    $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    $reason = New-Object ComponentModel.Win32Exception($code)
    return "$Operation failed (Win32 $code): $($reason.Message)"
}

function Close-TunnelServiceHandle([IntPtr]$Handle) {
    if ($Handle -ne [IntPtr]::Zero -and -not [EventDeskTunnelInstaller.Native]::CloseServiceHandle($Handle)) {
        throw (Get-TunnelWin32Failure 'CloseServiceHandle')
    }
}

function New-TunnelProtectedDirectory([string]$Path, [System.Security.AccessControl.DirectorySecurity]$Security) {
    # CreateDirectoryW fails if the path already exists and applies the DACL atomically.
    # No permissive directory or configuration file exists even briefly.
    $bytes = $Security.GetSecurityDescriptorBinaryForm()
    $descriptor = [Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
    try {
        [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $descriptor, $bytes.Length)
        $attributes = New-Object EventDeskTunnelInstaller.SecurityAttributes
        $attributes.Length = [Runtime.InteropServices.Marshal]::SizeOf($attributes)
        $attributes.Descriptor = $descriptor
        $attributes.InheritHandle = $false
        if (-not [EventDeskTunnelInstaller.Native]::CreateDirectory($Path, [ref]$attributes)) {
            throw (Get-TunnelWin32Failure "Create protected directory $Path")
        }
        $actual = Get-Acl -LiteralPath $Path
        $section = [System.Security.AccessControl.AccessControlSections]::Access
        if ($actual.GetSecurityDescriptorSddlForm($section) -ne $Security.GetSecurityDescriptorSddlForm($section)) {
            throw "Protected DACL verification failed for $Path."
        }
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($descriptor) }
}

function Invoke-TunnelNativeCommand([string]$Executable, [string[]]$Arguments) {
    $output = & $Executable @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "$Executable failed (exit $code): $($output -join [Environment]::NewLine)" }
    if ($output) { Write-Verbose ($output -join [Environment]::NewLine) }
}

function Get-TunnelInstallPlan([string]$Source, [string]$Hash) {
    if ([string]::IsNullOrWhiteSpace($Source) -or $Source -notmatch '^[A-Za-z]:\\' -or
        -not [IO.Path]::IsPathRooted($Source)) { throw 'SourceBinary must be an absolute local Windows path.' }
    if ($Hash -notmatch '^[0-9a-fA-F]{64}$') { throw 'ExpectedSha256 must be 64 hexadecimal characters.' }
    $root = 'C:\ProgramData\EventDeskPhotoTunnel'
    $absolute = [IO.Path]::GetFullPath($Source)
    if ($absolute.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $absolute.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Source must be staged outside the installation.' }
    $binary = Join-Path $root 'bin\cloudflared.exe'
    $token = Join-Path $root 'token.txt'
    $log = Join-Path $root 'logs\cloudflared.log'
    [pscustomobject]@{
        Root=$root; Bin=(Join-Path $root 'bin'); Logs=(Join-Path $root 'logs')
        Binary=$binary; Token=$token; Source=$absolute; Hash=$Hash.ToUpperInvariant()
        Name='Cloudflared'; Identity='NT SERVICE\Cloudflared'; Sid=(Get-TunnelServiceSid 'Cloudflared')
        BinaryCommand=('"' + $binary + '" --no-autoupdate tunnel --loglevel info --logfile "' + $log + '" --metrics 127.0.0.1:20242 run --token-file "' + $token + '"')
        EventLogKey='SYSTEM\CurrentControlSet\Services\EventLog\Application\Cloudflared'
    }
}

function Assert-TunnelHost {
    if ($env:COMPUTERNAME -ne 'PHOTO-API-HOST') { throw 'Allowed only on PHOTO-API-HOST.' }
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'An elevated Administrator session is required.' }
}

function Assert-TunnelProtectedDirectory([string]$Path, [string]$Sid) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unsafe tunnel directory.' }
    $acl = Get-Acl -LiteralPath $Path
    $expected = New-TunnelDirectorySecurity $Sid
    $access = [Security.AccessControl.AccessControlSections]::Access
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne 'S-1-5-32-544' -or
        $acl.GetSecurityDescriptorSddlForm($access) -ne $expected.GetSecurityDescriptorSddlForm($access)) { throw 'Unsafe tunnel directory permissions.' }
}

function New-TunnelTokenSecurity([string]$Sid) {
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    foreach ($principal in @('S-1-5-18','S-1-5-32-544',$Sid)) {
        $rights = [Security.AccessControl.FileSystemRights]::FullControl
        if ($principal -eq $Sid) { $rights = [Security.AccessControl.FileSystemRights]::Read }
        $identity = New-Object Security.Principal.SecurityIdentifier($principal)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, $rights, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    return $acl
}

function Assert-TunnelToken([string]$Token, [string]$TunnelId) {
    $parsedId = [Guid]::Empty
    if (-not [Guid]::TryParseExact($TunnelId, 'D', [ref]$parsedId) -or $parsedId -eq [Guid]::Empty) { throw 'A canonical expected tunnel ID is required.' }
    if ($Token.Length -lt 100 -or $Token.Length -gt 8192 -or $Token -notmatch '\A[A-Za-z0-9+/]+={0,2}\z') { throw 'Invalid tunnel token.' }
    try {
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Token)) | ConvertFrom-Json
        $fields = @($json.PSObject.Properties.Name)
        # cloudflared accepts secrets of at least 32 bytes; API-issued tokens can
        # contain 64 bytes. The 256-byte maximum is this pilot's defensive bound.
        $secretLength = [Convert]::FromBase64String($json.s).Length
        if ($fields.Count -ne 3 -or ($fields | Where-Object { $_ -notin @('a','t','s') }) -or
            $json.a -notmatch '^[0-9a-f]{32}$' -or $json.t -ne $TunnelId -or
            $secretLength -lt 32 -or $secretLength -gt 256) { throw 'Invalid token fields.' }
    } catch { throw 'Tunnel token did not match the expected tunnel.' }
}

function Set-TunnelTokenFromStandardInput([string]$TunnelId) {
    Assert-TunnelHost
    $expectedId = [Guid]::Empty
    if (-not [Guid]::TryParseExact($TunnelId, 'D', [ref]$expectedId) -or $expectedId -eq [Guid]::Empty) { throw 'A canonical expected tunnel ID is required.' }
    $plan = Get-TunnelInstallPlan 'C:\staged-placeholder.exe' ('0' * 64)
    # Reject unsafe state before accepting secret input. Only a fresh token file is allowed.
    Assert-TunnelProtectedDirectory $plan.Root $plan.Sid
    if (Test-Path -LiteralPath $plan.Token) { throw 'Token file already exists; no credentials changed.' }
    $service = Get-CimInstance Win32_Service -Filter "Name='Cloudflared'"
    if ($null -eq $service -or $service.State -ne 'Stopped' -or $service.StartName -ne $plan.Identity -or
        $service.PathName -ne $plan.BinaryCommand) { throw 'Expected stopped tunnel service is required.' }
    $inputStream = [Console]::OpenStandardInput()
    $memory = New-Object IO.MemoryStream
    $buffer = New-Object byte[] 1024
    $bytes = $null
    try {
        while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($memory.Length + $count -gt 8192) { throw 'Tunnel token input exceeds limit.' }
            $memory.Write($buffer, 0, $count)
        }
        $bytes = $memory.ToArray()
        $token = [Text.Encoding]::ASCII.GetString($bytes)
        Assert-TunnelToken $token $TunnelId
        $security = New-TunnelTokenSecurity $plan.Sid
        # FileSecurity is applied atomically to CREATE_NEW; no permissive secret exists briefly.
        $file = New-Object IO.FileStream($plan.Token, [IO.FileMode]::CreateNew,
            [Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None,
            4096, [IO.FileOptions]::WriteThrough, $security)
        try { $file.Write($bytes, 0, $bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
        $actual = Get-Acl -LiteralPath $plan.Token
        $section = [Security.AccessControl.AccessControlSections]'Access,Owner'
        if ($actual.GetSecurityDescriptorSddlForm($section) -ne $security.GetSecurityDescriptorSddlForm($section)) { throw 'Token ACL verification failed; do not start the service.' }
        Write-Output 'Protected tunnel token saved. Service remains stopped.'
    } finally {
        if ($null -ne $bytes) { [Array]::Clear($bytes,0,$bytes.Length) }
        [Array]::Clear($buffer,0,$buffer.Length)
        $memory.Dispose(); $inputStream.Dispose(); $token = $null
    }
}

function Install-TunnelService([string]$Source, [string]$Hash) {
    Assert-TunnelHost
    $plan = Get-TunnelInstallPlan $Source $Hash
    if (-not (Test-Path -LiteralPath $plan.Source -PathType Leaf)) { throw 'Source binary is missing.' }
    Assert-TunnelBinaryHash $plan.Source $plan.Hash
    if (Test-Path -LiteralPath $plan.Root) { throw 'Tunnel root already exists; nothing was changed.' }
    $registry = [Microsoft.Win32.Registry]::LocalMachine
    $existingLog = $registry.OpenSubKey($plan.EventLogKey)
    if ($null -ne $existingLog) { $existingLog.Dispose(); throw 'Cloudflared EventLog source already exists; nothing was changed.' }
    Initialize-TunnelInstallerNative
    $manager = [IntPtr]::Zero; $serviceCreated = $false; $logCreated = $false
    try {
        $manager = [EventDeskTunnelInstaller.Native]::OpenSCManager([NullString]::Value, [NullString]::Value, 3)
        if ($manager -eq [IntPtr]::Zero) { throw (Get-TunnelWin32Failure 'OpenSCManager') }
        $existing = [EventDeskTunnelInstaller.Native]::OpenService($manager, $plan.Name, 4)
        if ($existing -ne [IntPtr]::Zero) { Close-TunnelServiceHandle $existing; throw 'Cloudflared service already exists; nothing was changed.' }
        if ([Runtime.InteropServices.Marshal]::GetLastWin32Error() -ne 1060) { throw 'Cannot safely check existing Cloudflared service.' }
        $security = New-TunnelDirectorySecurity $plan.Sid
        New-TunnelProtectedDirectory $plan.Root $security
        New-TunnelProtectedDirectory $plan.Bin $security
        New-TunnelProtectedDirectory $plan.Logs (New-TunnelDirectorySecurity $plan.Sid -Logs)
        Assert-TunnelBinaryHash $plan.Source $plan.Hash
        [IO.File]::Copy($plan.Source, $plan.Binary, $false)
        Assert-TunnelBinaryHash $plan.Binary $plan.Hash
        # Fixed service name is required by cloudflared's svc.Run implementation.
        $created = [EventDeskTunnelInstaller.Native]::CreateService($manager, $plan.Name,
            'EventDesk Photo Tunnel', 0x00010000, 0x00000010, 0x00000002, 1,
            $plan.BinaryCommand, [NullString]::Value, [IntPtr]::Zero, [NullString]::Value, $plan.Identity, [NullString]::Value)
        if ($created -eq [IntPtr]::Zero) { throw (Get-TunnelWin32Failure 'CreateService') }
        $serviceCreated = $true; Close-TunnelServiceHandle $created
        $sc = Join-Path $env:WINDIR 'System32\sc.exe'
        Invoke-TunnelNativeCommand $sc @('sidtype', $plan.Name, 'unrestricted')
        $key = $registry.CreateSubKey($plan.EventLogKey)
        $logCreated = $true
        try {
            $key.SetValue('CustomSource', 1, [Microsoft.Win32.RegistryValueKind]::DWord)
            $key.SetValue('TypesSupported', 7, [Microsoft.Win32.RegistryValueKind]::DWord)
            $key.SetValue('EventMessageFile', '%SystemRoot%\System32\EventCreate.exe', [Microsoft.Win32.RegistryValueKind]::ExpandString)
        } finally { $key.Dispose() }
        Invoke-TunnelNativeCommand $sc @('failure', $plan.Name, 'reset=', '86400', 'actions=', 'restart/20000/restart/20000/restart/20000')
        Invoke-TunnelNativeCommand $sc @('failureflag', $plan.Name, '1')
        [pscustomobject]@{Service=$plan.Name;State='Stopped';Identity=$plan.Identity;Binary=$plan.Binary;BinarySha256=$plan.Hash;Root=$plan.Root;ServiceSid=$plan.Sid;NextStep='Configure token through stdin; start separately.'}
    } catch {
        $original = $_.Exception.Message; $rollback = @()
        if ($logCreated) { try { $registry.DeleteSubKey($plan.EventLogKey, $true) } catch { $rollback += 'EventLog rollback failed.' } }
        if ($serviceCreated) {
            $remove = [EventDeskTunnelInstaller.Native]::OpenService($manager, $plan.Name, 0x00010000)
            if ($remove -eq [IntPtr]::Zero) { $rollback += 'Service rollback lookup failed.' }
            else {
                try { if (-not [EventDeskTunnelInstaller.Native]::DeleteService($remove)) { $rollback += 'Service rollback failed.' } }
                finally { Close-TunnelServiceHandle $remove }
            }
        }
        throw ('Tunnel installation failed: ' + $original + '. Protected files retained. ' + ($rollback -join ' '))
    } finally { Close-TunnelServiceHandle $manager }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($ConfigureToken) { Set-TunnelTokenFromStandardInput $ExpectedTunnelId }
    else { Install-TunnelService $SourceBinary $ExpectedSha256 }
}

