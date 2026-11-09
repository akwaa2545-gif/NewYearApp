<#
Install once, from an elevated Windows PowerShell 5.1 session on the approved host:
  .\install_service.ps1 -SourceBinary C:\Users\USER\AppData\Local\Temp\EventDeskPhotoApi.exe -ExpectedSha256 HEX64
The source must be a previously reviewed binary. This script never configures secrets
or starts the service. Configure via the executable's --configure stdin interface,
then start separately after configuration succeeds. No passwords belong in arguments.
Dot sourcing exposes pure helpers for tests without changing the machine.
#>
param([string]$SourceBinary, [string]$ExpectedSha256)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-PhotoServiceSid([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) { throw 'A service name is required.' }
    $sha = [System.Security.Cryptography.SHA1]::Create()
    try { $bytes = $sha.ComputeHash([System.Text.Encoding]::Unicode.GetBytes($Name.ToUpperInvariant())) }
    finally { $sha.Dispose() }
    $parts = for ($offset = 0; $offset -lt 20; $offset += 4) { [BitConverter]::ToUInt32($bytes, $offset) }
    return 'S-1-5-80-' + ($parts -join '-')
}

function Get-PhotoInstallPlan([string]$Source, [string]$Hash) {
    if ([string]::IsNullOrWhiteSpace($Source) -or $Source -notmatch '^[A-Za-z]:\\' -or
        -not [System.IO.Path]::IsPathRooted($Source)) { throw 'SourceBinary must be an absolute local Windows path.' }
    if ($Hash -notmatch '^[0-9a-fA-F]{64}$') { throw 'ExpectedSha256 must contain exactly 64 hexadecimal characters.' }
    $root = 'C:\ProgramData\EventDeskPhotoApi'
    $absolute = [System.IO.Path]::GetFullPath($Source)
    if ($absolute.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $absolute.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'SourceBinary must be staged outside the installation directory.'
    }
    $sid = Get-PhotoServiceSid 'EventDeskPhotoApi'
    $binary = Join-Path $root 'bin\EventDeskPhotoApi.exe'
    return [pscustomobject]@{
        Root = $root; Bin = (Join-Path $root 'bin'); Logs = (Join-Path $root 'logs')
        Secrets = (Join-Path $root 'secrets'); Binary = $binary; BinaryCommand = ('"' + $binary + '"')
        Source = $absolute; Hash = $Hash.ToUpperInvariant(); Name = 'EventDeskPhotoApi'
        Identity = 'NT SERVICE\EventDeskPhotoApi'; Sid = $sid
        Url = 'http://127.0.0.1:8765/'; UrlSddl = "D:(A;;GX;;;$sid)"
    }
}

function Assert-PhotoBinaryHash([string]$Path, [string]$Expected) {
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    if (-not $actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SHA256 verification failed for $Path."
    }
}

function New-PhotoDirectorySecurity([string]$ServiceSid, [switch]$Logs) {
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

function Initialize-PhotoInstallerNative {
    if ('EventDeskPhotoInstaller.Native' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace EventDeskPhotoInstaller {
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

function Get-PhotoWin32Failure([string]$Operation) {
    $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    $reason = New-Object ComponentModel.Win32Exception($code)
    return "$Operation failed (Win32 $code): $($reason.Message)"
}

function Close-PhotoServiceHandle([IntPtr]$Handle) {
    if ($Handle -ne [IntPtr]::Zero -and -not [EventDeskPhotoInstaller.Native]::CloseServiceHandle($Handle)) {
        throw (Get-PhotoWin32Failure 'CloseServiceHandle')
    }
}

function New-PhotoProtectedDirectory([string]$Path, [System.Security.AccessControl.DirectorySecurity]$Security) {
    # CreateDirectoryW fails if the path already exists and applies the DACL atomically.
    # No permissive directory or configuration file exists even briefly.
    $bytes = $Security.GetSecurityDescriptorBinaryForm()
    $descriptor = [Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
    try {
        [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $descriptor, $bytes.Length)
        $attributes = New-Object EventDeskPhotoInstaller.SecurityAttributes
        $attributes.Length = [Runtime.InteropServices.Marshal]::SizeOf($attributes)
        $attributes.Descriptor = $descriptor
        $attributes.InheritHandle = $false
        if (-not [EventDeskPhotoInstaller.Native]::CreateDirectory($Path, [ref]$attributes)) {
            throw (Get-PhotoWin32Failure "Create protected directory $Path")
        }
        $actual = Get-Acl -LiteralPath $Path
        $section = [System.Security.AccessControl.AccessControlSections]::Access
        if ($actual.GetSecurityDescriptorSddlForm($section) -ne $Security.GetSecurityDescriptorSddlForm($section)) {
            throw "Protected DACL verification failed for $Path."
        }
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($descriptor) }
}

function Invoke-PhotoNativeCommand([string]$Executable, [string[]]$Arguments) {
    $output = & $Executable @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "$Executable failed (exit $code): $($output -join [Environment]::NewLine)" }
    if ($output) { Write-Verbose ($output -join [Environment]::NewLine) }
}

function Install-PhotoService([string]$Source, [string]$Hash) {
    if ($env:COMPUTERNAME -ne 'PHOTO-API-HOST') { throw 'Installation is allowed only on PHOTO-API-HOST.' }
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run Windows PowerShell as Administrator on PHOTO-API-HOST.'
    }
    $plan = Get-PhotoInstallPlan $Source $Hash
    if (-not (Test-Path -LiteralPath $plan.Source -PathType Leaf)) { throw 'SourceBinary does not exist as a file.' }
    Assert-PhotoBinaryHash $plan.Source $plan.Hash
    if (Test-Path -LiteralPath $plan.Root) { throw "Installation root already exists; nothing was changed: $($plan.Root)" }
    Initialize-PhotoInstallerNative
    $manager = [IntPtr]::Zero
    $serviceCreated = $false
    $urlCreated = $false
    $sc = Join-Path $env:WINDIR 'System32\sc.exe'
    $netsh = Join-Path $env:WINDIR 'System32\netsh.exe'
    try {
        # PowerShell 5.1 converts $null to an empty string for CLR string parameters.
        $manager = [EventDeskPhotoInstaller.Native]::OpenSCManager([NullString]::Value, [NullString]::Value, 0x00000003)
        if ($manager -eq [IntPtr]::Zero) { throw (Get-PhotoWin32Failure 'OpenSCManager') }
        $existing = [EventDeskPhotoInstaller.Native]::OpenService($manager, $plan.Name, 0x00000004)
        if ($existing -ne [IntPtr]::Zero) {
            Close-PhotoServiceHandle $existing
            throw 'EventDeskPhotoApi service already exists; no SCM resource will be overwritten.'
        }
        $lookupError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($lookupError -ne 1060) { throw "Service lookup failed (Win32 $lookupError); installation stopped." }

        $security = New-PhotoDirectorySecurity $plan.Sid
        New-PhotoProtectedDirectory $plan.Root $security
        New-PhotoProtectedDirectory $plan.Bin $security
        New-PhotoProtectedDirectory $plan.Secrets $security
        New-PhotoProtectedDirectory $plan.Logs (New-PhotoDirectorySecurity $plan.Sid -Logs)
        # Copy with overwrite=false. Recheck both sides; the expected digest is the trust anchor.
        Assert-PhotoBinaryHash $plan.Source $plan.Hash
        [System.IO.File]::Copy($plan.Source, $plan.Binary, $false)
        Assert-PhotoBinaryHash $plan.Binary $plan.Hash

        # SERVICE_WIN32_OWN_PROCESS; SERVICE_AUTO_START (0x00000002).
        # A virtual service account is SCM managed and receives a null password.
        $created = [EventDeskPhotoInstaller.Native]::CreateService($manager, $plan.Name,
            'EventDesk Photo API', 0x00010000, 0x00000010, 0x00000002, 0x00000001,
            $plan.BinaryCommand, [NullString]::Value, [IntPtr]::Zero, [NullString]::Value, $plan.Identity, [NullString]::Value)
        if ($created -eq [IntPtr]::Zero) { throw (Get-PhotoWin32Failure 'CreateService') }
        $serviceCreated = $true
        Close-PhotoServiceHandle $created
        Invoke-PhotoNativeCommand $sc @('sidtype', $plan.Name, 'unrestricted')
        # add fails if any owner already reserved this exact URL; never replace it.
        Invoke-PhotoNativeCommand $netsh @('http', 'add', 'urlacl', ('url=' + $plan.Url), ('sddl=' + $plan.UrlSddl))
        $urlCreated = $true
        $metadata = [ordered]@{
            Service = $plan.Name; Identity = $plan.Identity; ServiceSid = $plan.Sid
            BinarySha256 = $plan.Hash; InstalledUtc = [DateTime]::UtcNow.ToString('o')
            Url = $plan.Url; ConfigurationRequired = $true
        } | ConvertTo-Json
        [System.IO.File]::WriteAllText((Join-Path $plan.Root 'installation.json'), $metadata)
        [pscustomobject]@{
            Service = $plan.Name; State = 'Stopped'; Identity = $plan.Identity
            Binary = $plan.Binary; BinarySha256 = $plan.Hash; Root = $plan.Root
            ServiceSid = $plan.Sid; Url = $plan.Url
            NextStep = 'Configure secrets through --configure JSON stdin; start separately only after success.'
        }
    } catch {
        $originalFailure = $_.Exception.Message
        $rollbackFailures = @()
        if ($urlCreated) {
            try { Invoke-PhotoNativeCommand $netsh @('http', 'delete', 'urlacl', ('url=' + $plan.Url)) }
            catch { $rollbackFailures += $_.Exception.Message }
        }
        if ($serviceCreated) {
            $remove = [EventDeskPhotoInstaller.Native]::OpenService($manager, $plan.Name, 0x00010000)
            if ($remove -eq [IntPtr]::Zero) { $rollbackFailures += (Get-PhotoWin32Failure 'Open service for rollback') }
            else {
                try {
                    if (-not [EventDeskPhotoInstaller.Native]::DeleteService($remove)) {
                        $rollbackFailures += (Get-PhotoWin32Failure 'Delete newly created service')
                    }
                } finally {
                    try { Close-PhotoServiceHandle $remove } catch { $rollbackFailures += $_.Exception.Message }
                }
            }
        }
        $message = "Installation failed: $originalFailure Protected files, if created, were retained; no recursive deletion was attempted."
        if ($rollbackFailures.Count -gt 0) { $message += ' ROLLBACK INCOMPLETE: ' + ($rollbackFailures -join '; ') }
        throw $message
    } finally { Close-PhotoServiceHandle $manager }
}

if ($MyInvocation.InvocationName -ne '.') { Install-PhotoService $SourceBinary $ExpectedSha256 }
