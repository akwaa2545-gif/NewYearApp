$ErrorActionPreference = 'Stop'
$photoScript = Join-Path $PSScriptRoot '..\scripts\set_photo_ssh_account.ps1'
$photoTokens = $null
$photoErrors = $null
$photoAst = [System.Management.Automation.Language.Parser]::ParseFile($photoScript, [ref]$photoTokens, [ref]$photoErrors)
if ($photoErrors.Count) { throw ($photoErrors | Out-String) }
$photoFunction = $photoAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Convert-PhotoSshConfig' }, $true)
if (-not $photoFunction) { throw 'Missing pure configuration function.' }
. ([scriptblock]::Create($photoFunction.Extent.Text))

function Assert-PhotoEqual($Actual, $Expected, $Label) {
    if ($Actual -cne $Expected) { throw ('FAIL: ' + $Label) }
}
function Assert-PhotoThrows([scriptblock]$Action, $Label) {
    $photoThrew = $false
    try { & $Action } catch { $photoThrew = $true }
    if (-not $photoThrew) { throw ('FAIL: ' + $Label) }
}

$photoCrLf = "`r`n"
$photoGlobal = '# existing globals' + $photoCrLf + 'Port 22' + $photoCrLf
$photoAdmins = 'Match Group administrators' + $photoCrLf + '    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys' + $photoCrLf
$photoRule = '# EventDesk per-user SSH account' + $photoCrLf + 'Match User example\photo-operator' + $photoCrLf + '    AuthorizedKeysFile .ssh/authorized_keys' + $photoCrLf
$photoFixture = $photoGlobal + $photoAdmins
Assert-PhotoEqual (Convert-PhotoSshConfig $photoFixture) ($photoGlobal + $photoRule + $photoAdmins) 'insert before administrator Match without changing shared settings'
$photoDebug = 'SyslogFacility LOCAL0' + $photoCrLf + 'LogLevel DEBUG3' + $photoCrLf
Assert-PhotoEqual (Convert-PhotoSshConfig ($photoDebug + $photoDebug + $photoFixture)) ($photoGlobal + $photoRule + $photoAdmins) 'strip repeated exact diagnostic prefixes'
$photoOther = 'Match User another-user' + $photoCrLf + '    PasswordAuthentication no' + $photoCrLf
Assert-PhotoEqual (Convert-PhotoSshConfig ($photoGlobal + $photoOther + $photoAdmins)) ($photoGlobal + $photoRule + $photoOther + $photoAdmins) 'preserve all existing Match scopes'
$photoUnicode = '# ' + [char]0x0E20 + [char]0x0E32 + [char]0x0E1E + ' / ' + [char]0x00E9 + $photoCrLf
Assert-PhotoEqual (Convert-PhotoSshConfig ($photoUnicode + $photoFixture)) ($photoUnicode + $photoGlobal + $photoRule + $photoAdmins) 'preserve Unicode'
$photoLfFixture = $photoFixture.Replace($photoCrLf, "`n")
Assert-PhotoEqual (Convert-PhotoSshConfig $photoLfFixture) (($photoGlobal + $photoRule + $photoAdmins).Replace($photoCrLf, "`n")) 'preserve LF line endings'
Assert-PhotoThrows { Convert-PhotoSshConfig ($photoGlobal + $photoRule + $photoAdmins) } 'reject duplicate application'
Assert-PhotoThrows { Convert-PhotoSshConfig $photoFixture 'another-user' } 'reject a different account scope'
$photoCustomLog = 'LogLevel VERBOSE' + $photoCrLf
Assert-PhotoEqual (Convert-PhotoSshConfig ($photoCustomLog + $photoFixture)) ($photoCustomLog + $photoGlobal + $photoRule + $photoAdmins) 'preserve logging outside exact diagnostic prefix'
Assert-PhotoEqual (Convert-PhotoSshConfig 'Port 22') ('Port 22' + $photoCrLf + $photoRule) 'append scoped rule after globals when no Match exists'
Write-Output 'PASS: 9 configuration cases; script syntax parsed; no SSH or service operations executed.'
