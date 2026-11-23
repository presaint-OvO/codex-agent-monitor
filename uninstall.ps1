[CmdletBinding()]
param(
    [switch]$Purge,
    [switch]$NoPathUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$LocalData = if ($env:LOCALAPPDATA) {
    $env:LOCALAPPDATA
} else {
    Join-Path $HOME "AppData\Local"
}
$InstallDir = Join-Path $LocalData "Programs\codex-agent-monitor"
$BinDir = Join-Path $InstallDir "bin"
$StateDir = Join-Path $LocalData "codex-agent-monitor"
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$HookConfig = Join-Path $CodexHome "hooks.json"
$VenvPython = Join-Path $InstallDir "venv\Scripts\python.exe"
$InstalledConfigurer = Join-Path $InstallDir "configure_hooks.py"
$RepoConfigurer = Join-Path $ScriptDir "scripts\configure_hooks.py"

$expected = [IO.Path]::GetFullPath((Join-Path $LocalData "Programs\codex-agent-monitor"))
if ([IO.Path]::GetFullPath($InstallDir) -ne $expected -or (Split-Path $InstallDir -Leaf) -ne "codex-agent-monitor") {
    throw "Refusing to process an unexpected install directory: $InstallDir"
}

$Configurer = if (Test-Path $RepoConfigurer) {
    $RepoConfigurer
} elseif (Test-Path $InstalledConfigurer) {
    $InstalledConfigurer
} else {
    $null
}
if ($Configurer -and (Test-Path $VenvPython) -and (Test-Path $HookConfig)) {
    & $VenvPython -B $Configurer remove --config $HookConfig --hook (Join-Path $InstallDir "hook.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to remove Codex Hooks; program files were left in place."
    }
}

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object {
        $_ -and $_.TrimEnd("\") -ine $BinDir.TrimEnd("\")
    })
    [Environment]::SetEnvironmentVariable("Path", ($entries -join ";"), "User")
}

if (Test-Path $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Host "Removed program: $InstallDir"
}
if ($Purge -and (Test-Path $StateDir)) {
    if ((Split-Path $StateDir -Leaf) -ne "codex-agent-monitor") {
        throw "Refusing to process an unexpected state directory: $StateDir"
    }
    Remove-Item -LiteralPath $StateDir -Recurse -Force
    Write-Host "Removed state data: $StateDir"
} elseif (Test-Path $StateDir) {
    Write-Host "Preserved state data: $StateDir (use -Purge to remove it)"
}

Write-Host "Codex Agent Monitor was uninstalled from Windows."
