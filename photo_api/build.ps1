param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The Windows .NET Framework 4.x 64-bit C# compiler is required.' }
$outputDirectory = Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$application = Join-Path $outputDirectory 'EventDeskPhotoApi.exe'
$source = Join-Path $PSScriptRoot 'EventDeskPhotoApi.cs'
& $compiler /nologo /warnaserror+ /optimize+ /target:exe /platform:x64 "/out:$application" /r:System.Data.dll /r:System.ServiceProcess.dll /r:System.Security.dll /r:System.Runtime.Serialization.dll $source
if ($LASTEXITCODE -ne 0) { throw 'Photo API compilation failed.' }
$importApplication = Join-Path $outputDirectory 'ImportPhotos.exe'
$importSource = Join-Path $PSScriptRoot 'ImportPhotos.cs'
& $compiler /nologo /warnaserror+ /optimize+ /target:exe /platform:x64 "/out:$importApplication" /r:System.Drawing.dll /r:System.Data.dll /r:System.Runtime.Serialization.dll $importSource
if ($LASTEXITCODE -ne 0) { throw 'Photo importer compilation failed.' }
if (-not $SkipTests) {
    $testApplication = Join-Path $outputDirectory 'PhotoApiTests.exe'
    $testSource = Join-Path $PSScriptRoot 'tests\PhotoApiTests.cs'
    & $compiler /nologo /warnaserror+ /optimize+ /target:exe /platform:x64 "/out:$testApplication" "/r:$application" /r:System.Data.dll /r:System.Runtime.Serialization.dll $testSource
    if ($LASTEXITCODE -ne 0) { throw 'Photo API test compilation failed.' }
    & $testApplication
    if ($LASTEXITCODE -ne 0) { throw 'Photo API tests failed.' }
    $importTestApplication = Join-Path $outputDirectory 'ImportPhotosTests.exe'
    $importTestSource = Join-Path $PSScriptRoot 'tests\ImportPhotosTests.cs'
    & $compiler /nologo /warnaserror+ /optimize+ /target:exe /platform:x64 "/out:$importTestApplication" "/r:$importApplication" /r:System.Drawing.dll /r:System.Data.dll /r:System.Runtime.Serialization.dll $importTestSource
    if ($LASTEXITCODE -ne 0) { throw 'Photo importer test compilation failed.' }
    & $importTestApplication
    if ($LASTEXITCODE -ne 0) { throw 'Photo importer tests failed.' }
}
Write-Output "Built $application"
Write-Output "Built $importApplication"
