[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$NoPathUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $PSCommandPath
$LocalData = if ($env:LOCALAPPDATA) {
    $env:LOCALAPPDATA
} else {
    Join-Path $HOME "AppData\Local"
}
$InstallDir = Join-Path $LocalData "Programs\codex-agent-monitor"
$BinDir = Join-Path $InstallDir "bin"
$VenvDir = Join-Path $InstallDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$HookConfig = Join-Path $CodexHome "hooks.json"

function Resolve-PythonLauncher {
    param([string]$Requested)

    $candidates = [System.Collections.Generic.List[object]]::new()
    if ($Requested) {
        $command = Get-Command $Requested -ErrorAction SilentlyContinue
        $path = if ($command) { $command.Source } else { $Requested }
        $prefix = if ([IO.Path]::GetFileNameWithoutExtension($path) -eq "py") { @("-3") } else { @() }
        $candidates.Add([pscustomobject]@{ Path = $path; Prefix = $prefix })
    } else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            $candidates.Add([pscustomobject]@{ Path = $py.Source; Prefix = @("-3") })
        }
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            $candidates.Add([pscustomobject]@{ Path = $pythonCommand.Source; Prefix = @() })
        }
    }

    foreach ($candidate in $candidates) {
        try {
            $output = & $candidate.Path @($candidate.Prefix) -c "import sys; print(sys.executable); raise SystemExit(sys.version_info < (3, 10))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $output) {
                return $candidate
            }
        } catch {
            continue
        }
    }
    throw "Python 3.10 or newer is required. Install Windows CPython or pass -Python with a python.exe path."
}

function Add-UserPathEntry {
    param([string]$Entry)

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { $_ })
    $present = $entries | Where-Object {
        $_.TrimEnd("\") -ieq $Entry.TrimEnd("\")
    }
    if (-not $present) {
        $newPath = (@($entries) + $Entry) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    }
    if (-not (($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -ieq $Entry.TrimEnd("\") })) {
        $env:Path = "$Entry;$env:Path"
    }
}

$launcher = Resolve-PythonLauncher -Requested $Python
New-Item -ItemType Directory -Force -Path $InstallDir, $BinDir, $CodexHome | Out-Null

if (-not (Test-Path $VenvPython)) {
    & $launcher.Path @($launcher.Prefix) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Windows virtual environment."
    }
}

$copies = @{
    (Join-Path $RepoDir "src\monitor.py") = (Join-Path $InstallDir "monitor.py")
    (Join-Path $RepoDir "src\hook.py") = (Join-Path $InstallDir "hook.py")
    (Join-Path $RepoDir "scripts\configure_hooks.py") = (Join-Path $InstallDir "configure_hooks.py")
    (Join-Path $RepoDir "uninstall.ps1") = (Join-Path $InstallDir "uninstall.ps1")
    (Join-Path $RepoDir "README.md") = (Join-Path $InstallDir "README.md")
    (Join-Path $RepoDir "LICENSE") = (Join-Path $InstallDir "LICENSE")
    (Join-Path $RepoDir "VERSION") = (Join-Path $InstallDir "VERSION")
}
foreach ($source in $copies.Keys) {
    Copy-Item -LiteralPath $source -Destination $copies[$source] -Force
}

$Wrapper = Join-Path $BinDir "codex-agents.cmd"
$wrapperContent = @'
@echo off
rem Installed by codex-agent-monitor
"%~dp0..\venv\Scripts\python.exe" "%~dp0..\monitor.py" %*
'@
[IO.File]::WriteAllText($Wrapper, $wrapperContent, [Text.UTF8Encoding]::new($false))

& $VenvPython -B (Join-Path $InstallDir "configure_hooks.py") install `
    --config $HookConfig `
    --hook (Join-Path $InstallDir "hook.py") `
    --python $VenvPython
if ($LASTEXITCODE -ne 0) {
    throw "Failed to configure Codex Hooks."
}

if (-not $NoPathUpdate) {
    Add-UserPathEntry -Entry $BinDir
}

Write-Host ""
Write-Host "Codex Agent Monitor is installed for this Windows user."
Write-Host "Run: codex-agents"
if ($NoPathUpdate) {
    Write-Host "PATH was not changed. Command wrapper: $Wrapper"
} else {
    Write-Host "If this terminal cannot find the command yet, reopen PowerShell or Windows Terminal."
}
Write-Host "Reopen Codex sessions so the user-level Hooks take effect."
