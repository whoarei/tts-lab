# Native, isolated regression for current-user install / reinstall / uninstall.
param([Parameter(Mandatory=$true)][string]$Bundle,
      [Parameter(Mandatory=$true)][string]$Installer)
$ErrorActionPreference = 'Stop'
$Bundle = (Resolve-Path $Bundle).Path
$Installer = (Resolve-Path $Installer).Path
$installPath = Join-Path $env:RUNNER_TEMP ('TTS Lab install 测试 ' + [Guid]::NewGuid().ToString('N'))
$profilePath = Join-Path $env:LOCALAPPDATA 'TTS Lab'
$sentinelName = 'installer-ci-' + [Guid]::NewGuid().ToString('N') + '.txt'
$profileSentinel = Join-Path $profilePath $sentinelName
$unrelatedFile = Join-Path $installPath 'preserve-unrelated-file.txt'
$sentinelValue = [Guid]::NewGuid().ToString('N')
$legacyInstaller = Join-Path $env:RUNNER_TEMP 'TTS-Lab-0.5.0-legacy-setup.exe'
$legacySha256 = 'e2e95bbbbddd9c9058284622a915d2a1623bebf661194050b8fcf44ad0c2ea9a'

function Run-InstallerProcess([string]$Executable, [string[]]$Arguments) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru
    if (-not $process.WaitForExit(180000)) {
        $process.Kill()
        throw "Installer process timed out: $Executable"
    }
    if ($process.ExitCode -ne 0) {
        throw "Installer process failed ($($process.ExitCode)): $Executable"
    }
}
function Check-ProfileSentinel {
    if ((Get-Content -LiteralPath $profileSentinel -Raw).Trim() -ne $sentinelValue) {
        throw 'Installer changed a profile file outside the program directory'
    }
}

try {
    New-Item -ItemType Directory -Path $profilePath -Force | Out-Null
    Set-Content -LiteralPath $profileSentinel -Value $sentinelValue
    # NSIS consumes the rest of the command line after /D=; keep it last and unquoted.
    Run-InstallerProcess $Installer @('/S', ('/D=' + $installPath))
    $installedExe = Join-Path $installPath 'TTS Lab.exe'
    if ((Get-FileHash -LiteralPath $installedExe).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Bundle 'TTS Lab.exe')).Hash) {
        throw 'Installed desktop executable differs from the verified bundle'
    }
    Check-ProfileSentinel
    Set-Content -LiteralPath $unrelatedFile -Value $sentinelValue
    # Reinstall invokes the existing uninstaller first, exercising the upgrade path.
    Run-InstallerProcess $Installer @('/S', ('/D=' + $installPath))
    if ((Get-Content -LiteralPath $unrelatedFile -Raw).Trim() -ne $sentinelValue) {
        throw 'Upgrade removed a file outside its known payload'
    }
    Check-ProfileSentinel
    $uninstaller = Join-Path $installPath 'Uninstall.exe'
    # _?= disables the temporary-process hop, so this waits for actual cleanup.
    Run-InstallerProcess $uninstaller @('/S', ('_?=' + $installPath))
    if (Test-Path -LiteralPath $installedExe) { throw 'Uninstall left the desktop executable' }
    if (Test-Path -LiteralPath (Join-Path $installPath 'resources/python/python.exe')) {
        throw 'Uninstall left the bundled Python executable'
    }
    if ((Get-Content -LiteralPath $unrelatedFile -Raw).Trim() -ne $sentinelValue) {
        throw 'Uninstall removed a file outside its known payload'
    }
    Check-ProfileSentinel
    # Exercise the actual published Electron -> Tauri transition, not just a
    # reinstall of a Tauri-generated uninstall manifest. The immutable release
    # asset's hash is pinned before anything is executed.
    if (-not (Test-Path -LiteralPath $legacyInstaller)) {
        Invoke-WebRequest -Uri 'https://github.com/whoarei/tts-lab/releases/download/v0.5.0/TTS-Lab-0.5.0-win-x64-setup.exe' -OutFile $legacyInstaller
    }
    if ((Get-FileHash -LiteralPath $legacyInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $legacySha256) {
        throw 'Published legacy Electron installer SHA256 mismatch'
    }
    Run-InstallerProcess $legacyInstaller @('/S', ('/D=' + $installPath))
    $electronFiles = @('resources.pak', 'icudtl.dat', 'resources/app/main.js', 'resources/app/preload.js')
    foreach ($name in $electronFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $installPath $name))) {
            throw "Legacy installation is missing its expected Electron member: $name"
        }
    }
    Check-ProfileSentinel
    Run-InstallerProcess $Installer @('/S', ('/D=' + $installPath))
    if ((Get-FileHash -LiteralPath $installedExe).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Bundle 'TTS Lab.exe')).Hash) {
        throw 'Electron upgrade did not install the expected Tauri executable'
    }
    foreach ($name in $electronFiles) {
        if (Test-Path -LiteralPath (Join-Path $installPath $name)) {
            throw "Electron upgrade left an obsolete runtime member: $name"
        }
    }
    if ((Get-Content -LiteralPath $unrelatedFile -Raw).Trim() -ne $sentinelValue) {
        throw 'Electron upgrade removed a file outside the known payload'
    }
    Check-ProfileSentinel
    Run-InstallerProcess $uninstaller @('/S', ('_?=' + $installPath))
    if (Test-Path -LiteralPath $installedExe) { throw 'Post-upgrade uninstall left the application executable' }
    Check-ProfileSentinel
    Write-Output 'Fresh Tauri setup, reinstall, published Electron upgrade, uninstall and user-file preservation passed.'
} finally {
    # These are this check's unique files; leave all other profile files untouched.
    Remove-Item -LiteralPath $profileSentinel -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $installPath -Recurse -Force -ErrorAction SilentlyContinue
}
