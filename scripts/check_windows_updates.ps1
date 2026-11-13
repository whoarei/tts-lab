# Native update-helper regression. Only this test's unique profile files are touched.
param([Parameter(Mandatory=$true)][string]$Bundle,
      [Parameter(Mandatory=$true)][string]$Installer,
      [Parameter(Mandatory=$true)][string]$PortableZIP)
$ErrorActionPreference = 'Stop'
$Bundle = (Resolve-Path -LiteralPath $Bundle).Path
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$PortableZIP = (Resolve-Path -LiteralPath $PortableZIP).Path
$version = (Get-Content -LiteralPath (Join-Path $Bundle 'resources/app/package.json') -Raw | ConvertFrom-Json).version
if ([version]$version -le [version]'0.6.0') {
    throw 'Native update regression requires a candidate newer than the published 0.6.0 baseline'
}
$identity = [Guid]::NewGuid().ToString('N')
$temporary = Join-Path $env:RUNNER_TEMP ('TTS Lab update 测试 ' + $identity)
$profile = Join-Path $env:LOCALAPPDATA 'TTS Lab'
# Helper requests are confined to the real profile's update staging directory.
$requests = Join-Path $profile ('updates/ci-native-update-' + $identity)
$profileSentinel = Join-Path $profile ('update-ci-' + $identity + '.txt')
$sentinel = [Guid]::NewGuid().ToString('N')
$portableTarget = Join-Path $temporary 'Portable 便携 app with spaces'
$installedTarget = Join-Path $temporary 'Installed 安装 app with spaces'
$helper = Join-Path $requests 'update-helper.exe'
$installedActive = $false
$legacyInstaller = Join-Path $temporary 'TTS-Lab-0.6.0-win-x64-setup.exe'
$legacyZIP = Join-Path $temporary 'TTS-Lab-0.6.0-win-x64.zip'
$legacyInstallerSHA = 'ad2208e31d9f71d1e232b02f5dca2e0c565192e83a31150161f236301160f06e'
$legacyZIPSHA = 'c6da9cebaf1e51e9b0da680af6fb6c2cee51a1ada8a280db0344ae2da907bb1b'
$utf8 = [Text.UTF8Encoding]::new($false)

function Assert-FileHash([string]$Path, [string]$Expected, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected.ToLowerInvariant()) {
        throw "$Label SHA256 mismatch: $Path"
    }
}
function Run-Process([string]$Executable, [string[]]$Arguments, [int]$ExpectedExit=0) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru
    if (-not $process.WaitForExit(240000)) {
        $process.Kill()
        throw "Process timed out: $Executable"
    }
    if ($process.ExitCode -ne $ExpectedExit) {
        throw "Unexpected process exit ($($process.ExitCode), expected $ExpectedExit): $Executable"
    }
}
function Assert-Sentinels([string]$Target) {
    if ((Get-Content -LiteralPath $profileSentinel -Raw).Trim() -ne $sentinel) {
        throw 'Updater changed a profile file outside its staging directory'
    }
    if ((Get-Content -LiteralPath (Join-Path $Target 'preserve-unrelated-user-file.txt') -Raw).Trim() -ne $sentinel) {
        throw 'Updater removed or changed an unrelated file in the application folder'
    }
}
function Assert-Version([string]$Target, [string]$Expected) {
    $metadata = Get-Content -LiteralPath (Join-Path $Target 'resources/app/package.json') -Raw | ConvertFrom-Json
    if ($metadata.version -ne $Expected) { throw "Unexpected application version: $($metadata.version), expected $Expected" }
}
function Assert-NewPayload([string]$Target) {
    Assert-Version $Target $version
    $expectedExecutableHash = (Get-FileHash -LiteralPath (Join-Path $Bundle 'TTS Lab.exe') -Algorithm SHA256).Hash
    Assert-FileHash (Join-Path $Target 'TTS Lab.exe') $expectedExecutableHash 'Updated desktop executable'
    $manifest = Get-Content -LiteralPath (Join-Path $Bundle 'BUILD-MANIFEST.json') -Raw | ConvertFrom-Json
    $installedManifest = Get-Content -LiteralPath (Join-Path $Target 'BUILD-MANIFEST.json') -Raw | ConvertFrom-Json
    if ($installedManifest.version -ne $version) { throw 'Updated manifest has the wrong version' }
    Assert-FileHash (Join-Path $Target 'BUILD-MANIFEST.json') (Get-FileHash -LiteralPath (Join-Path $Bundle 'BUILD-MANIFEST.json') -Algorithm SHA256).Hash 'Updated manifest'
    $prefix = [IO.Path]::GetFullPath($Target).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    foreach ($item in $manifest.files) {
        $destination = [IO.Path]::GetFullPath((Join-Path $Target $item.path))
        if (-not $destination.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Payload manifest escapes the app folder' }
        Assert-FileHash $destination $item.sha256 'Updated payload member'
    }
    Assert-Sentinels $Target
}
function Assert-OldPortableUnchanged {
    Assert-Version $portableTarget '0.6.0'
    Assert-FileHash (Join-Path $portableTarget 'BUILD-MANIFEST.json') $legacyPortableManifestHash 'Unchanged old portable manifest'
    foreach ($item in $legacyPortableManifest.files) {
        Assert-FileHash (Join-Path $portableTarget $item.path) $item.sha256 'Unchanged old portable payload member'
    }
    $currentPaths = @(Get-ChildItem -LiteralPath $portableTarget -Recurse -File -Force | ForEach-Object {
        [IO.Path]::GetRelativePath($portableTarget, $_.FullName)
    } | Sort-Object)
    if (Compare-Object -ReferenceObject $legacyPortablePaths -DifferenceObject $currentPaths) {
        throw 'Rejected update changed the old portable file set'
    }
    Assert-Sentinels $portableTarget
}
function Invoke-UpdateHelper([string]$Case, [string]$Kind, [string]$Package, [string]$SHA256,
                             [string]$Target, [bool]$Success) {
    $directory = Join-Path $requests $Case
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    # An already-finished child PID exercises the parent-exited path without
    # letting the helper terminate an application or any unrelated process.
    $parent = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/c', 'exit 0') -PassThru
    if (-not $parent.WaitForExit(10000)) { throw 'Short-lived parent fixture did not exit' }
    $request = [ordered]@{
        schema = 1
        kind = $Kind
        package = [IO.Path]::GetFullPath($Package)
        sha256 = $SHA256.ToLowerInvariant()
        expected_version = $version
        target_dir = [IO.Path]::GetFullPath($Target)
        parent_pid = $parent.Id
        result_path = Join-Path $directory 'result.json'
        no_restart = $true
    }
    $requestPath = Join-Path $directory 'request.json'
    [IO.File]::WriteAllText($requestPath, ($request | ConvertTo-Json), $utf8)
    $arguments = @('--apply-update', ('"' + $requestPath + '"'))
    Run-Process $helper $arguments $(if ($Success) { 0 } else { 1 })
    if (-not (Test-Path -LiteralPath $request.result_path -PathType Leaf)) { throw "Missing helper result: $Case" }
    $result = Get-Content -LiteralPath $request.result_path -Raw | ConvertFrom-Json
    if ($result.schema -ne 1) { throw "Malformed helper result: $Case" }
    $ready = Join-Path $directory 'ready.json'
    if ($Success) {
        if ($result.status -ne 'success' -or $result.version -ne $version) { throw "Helper did not report success: $Case ($($result.message))" }
        if (-not (Test-Path -LiteralPath $ready -PathType Leaf)) { throw "Missing pre-apply readiness: $Case" }
        $ack = Get-Content -LiteralPath $ready -Raw | ConvertFrom-Json
        if ($ack.schema -ne 1 -or $ack.status -ne 'ready' -or $ack.version -ne $version) { throw "Malformed ready acknowledgment: $Case" }
    } else {
        if ($result.status -ne 'failed') { throw "Invalid update did not fail: $Case" }
        if ($result.version -notin @('', $version)) { throw "Unexpected rejected-update version: $Case" }
        if (Test-Path -LiteralPath $ready) { throw "Invalid update wrote a ready acknowledgment: $Case" }
    }
    Write-Output "Update-helper case $Case returned expected status $($result.status)."
}

try {
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    New-Item -ItemType Directory -Path $requests -Force | Out-Null
    [IO.File]::WriteAllText($profileSentinel, $sentinel, $utf8)
    Copy-Item -LiteralPath (Join-Path $Bundle 'TTS Lab.exe') -Destination $helper
    Invoke-WebRequest -Uri 'https://github.com/whoarei/tts-lab/releases/download/v0.6.0/TTS-Lab-0.6.0-win-x64-setup.exe' -OutFile $legacyInstaller
    Invoke-WebRequest -Uri 'https://github.com/whoarei/tts-lab/releases/download/v0.6.0/TTS-Lab-0.6.0-win-x64.zip' -OutFile $legacyZIP
    Assert-FileHash $legacyInstaller $legacyInstallerSHA 'Published 0.6.0 installer'
    Assert-FileHash $legacyZIP $legacyZIPSHA 'Published 0.6.0 portable ZIP'
    $unpacked = Join-Path $temporary 'legacy portable archive'
    Expand-Archive -LiteralPath $legacyZIP -DestinationPath $unpacked
    Move-Item -LiteralPath (Join-Path $unpacked 'TTS-Lab-0.6.0-win-x64') -Destination $portableTarget
    Assert-Version $portableTarget '0.6.0'
    [IO.File]::WriteAllText((Join-Path $portableTarget 'preserve-unrelated-user-file.txt'), $sentinel, $utf8)
    $legacyPortableManifest = Get-Content -LiteralPath (Join-Path $portableTarget 'BUILD-MANIFEST.json') -Raw | ConvertFrom-Json
    $legacyPortableManifestHash = (Get-FileHash -LiteralPath (Join-Path $portableTarget 'BUILD-MANIFEST.json') -Algorithm SHA256).Hash
    $legacyPortablePaths = @(Get-ChildItem -LiteralPath $portableTarget -Recurse -File -Force | ForEach-Object {
        [IO.Path]::GetRelativePath($portableTarget, $_.FullName)
    } | Sort-Object)
    Invoke-UpdateHelper 'reject-bad-sha' 'portable' $PortableZIP ('0' * 64) $portableTarget $false
    Assert-OldPortableUnchanged
    $invalidZIP = Join-Path $temporary 'invalid-update.zip'
    [IO.File]::WriteAllBytes($invalidZIP, [Text.Encoding]::ASCII.GetBytes('not a ZIP archive'))
    Invoke-UpdateHelper 'reject-invalid-archive' 'portable' $invalidZIP (Get-FileHash -LiteralPath $invalidZIP -Algorithm SHA256).Hash $portableTarget $false
    Assert-OldPortableUnchanged
    $zipHash = (Get-FileHash -LiteralPath $PortableZIP -Algorithm SHA256).Hash
    Invoke-UpdateHelper 'portable-upgrade' 'portable' $PortableZIP $zipHash $portableTarget $true
    Assert-NewPayload $portableTarget
    # /D= is deliberately last and unquoted: NSIS consumes the remaining command line.
    Run-Process $legacyInstaller @('/S', ('/D=' + $installedTarget))
    $installedActive = $true
    Assert-Version $installedTarget '0.6.0'
    [IO.File]::WriteAllText((Join-Path $installedTarget 'preserve-unrelated-user-file.txt'), $sentinel, $utf8)
    $setupHash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
    Invoke-UpdateHelper 'installer-upgrade' 'installer' $Installer $setupHash $installedTarget $true
    Assert-NewPayload $installedTarget
    Run-Process (Join-Path $installedTarget 'Uninstall.exe') @('/S', ('_?=' + $installedTarget))
    $installedActive = $false
    if (Test-Path -LiteralPath (Join-Path $installedTarget 'TTS Lab.exe')) { throw 'Updated application remained after uninstall' }
    Assert-Sentinels $installedTarget
    Write-Output 'Pinned 0.6.0 portable and installed upgrades, rejection before readiness, payload hashes and user-file preservation passed.'
} finally {
    if ($installedActive -and (Test-Path -LiteralPath (Join-Path $installedTarget 'Uninstall.exe'))) {
        try { Run-Process (Join-Path $installedTarget 'Uninstall.exe') @('/S', ('_?=' + $installedTarget)) } catch { Write-Warning $_ }
    }
    Remove-Item -LiteralPath $profileSentinel -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $requests -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
