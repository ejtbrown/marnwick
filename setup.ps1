$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = if ($env:MARNWICK_VENV) { $env:MARNWICK_VENV } else { Join-Path $RootDir ".venv" }
$IconPng = Join-Path $RootDir "marnwick-icon.png"
$IconIco = Join-Path $RootDir "marnwick.ico"
$LamaRuntimeRequest = if ($env:MARNWICK_LAMA_RUNTIME) {
    $env:MARNWICK_LAMA_RUNTIME.ToLowerInvariant()
} else {
    "auto"
}

if ($env:OS -ne "Windows_NT") {
    throw "setup.ps1 is intended for Windows. Use ./setup.sh on Linux or macOS."
}

$WindowsBuild = try {
    [int](Get-ItemPropertyValue -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -Name CurrentBuildNumber)
} catch {
    [Environment]::OSVersion.Version.Build
}
if ($WindowsBuild -lt 17763) {
    throw "Marnwick's Qt runtime requires Windows 10 version 1809 (build 17763) or newer. This Windows build is $WindowsBuild."
}

if (-not (Test-Path -LiteralPath $IconPng)) {
    throw "Could not find Marnwick icon: $IconPng"
}

function Get-PythonInfo {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [string[]]$Arguments = @()
    )

    $Probe = 'import json, platform, struct, sys; print(json.dumps({"implementation": platform.python_implementation(), "version": platform.python_version(), "major": sys.version_info.major, "minor": sys.version_info.minor, "architecture": platform.machine().lower(), "bits": struct.calcsize("P") * 8}))'
    try {
        $Output = & $Executable @Arguments -c $Probe 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $Output) {
            return $null
        }
        $Json = @($Output)[-1]
        return ($Json | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Test-MarnwickPython {
    param($Info)

    if (-not $Info) {
        return $false
    }
    $Architecture = [string]$Info.architecture
    return (
        $Info.implementation -eq "CPython" -and
        $Info.major -eq 3 -and
        $Info.minor -ge 12 -and
        $Info.minor -lt 15 -and
        $Info.bits -eq 64 -and
        $Architecture -in @("amd64", "x86_64", "arm64", "aarch64")
    )
}

function Format-PythonInfo {
    param($Info)

    if (-not $Info) {
        return "an unusable Python executable"
    }
    return "$($Info.implementation) $($Info.version) ($($Info.architecture), $($Info.bits)-bit)"
}

$PythonExe = $null
$PythonArgs = @()
$PythonInfo = $null
if ($env:PYTHON) {
    $PythonInfo = Get-PythonInfo -Executable $env:PYTHON
    if (-not (Test-MarnwickPython -Info $PythonInfo)) {
        throw "Marnwick requires 64-bit CPython 3.12, 3.13, or 3.14. PYTHON points to $(Format-PythonInfo -Info $PythonInfo)."
    }
    $PythonExe = $env:PYTHON
} else {
    $Candidates = @()
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($Version in @("3.14", "3.13", "3.12")) {
            $Candidates += [PSCustomObject]@{
                Executable = $PyLauncher.Source
                Arguments = @("-$Version")
            }
        }
    }
    foreach ($CommandName in @("python3.14", "python3.13", "python3.12", "python3", "python")) {
        $PythonCommand = Get-Command $CommandName -ErrorAction SilentlyContinue
        if ($PythonCommand) {
            $Candidates += [PSCustomObject]@{
                Executable = $PythonCommand.Source
                Arguments = @()
            }
        }
    }
    foreach ($Candidate in $Candidates) {
        $CandidateInfo = Get-PythonInfo -Executable $Candidate.Executable -Arguments $Candidate.Arguments
        if (Test-MarnwickPython -Info $CandidateInfo) {
            $PythonExe = $Candidate.Executable
            $PythonArgs = $Candidate.Arguments
            $PythonInfo = $CandidateInfo
            break
        }
    }
}

if (-not $PythonExe) {
    throw "Could not find 64-bit CPython 3.12, 3.13, or 3.14. Install a compatible Python from python.org or set PYTHON to its executable path."
}

$PythonArchitecture = [string]$PythonInfo.architecture
$IsX64 = $PythonArchitecture -in @("amd64", "x86_64")
$InstallWebGpu = $false
switch ($LamaRuntimeRequest) {
    "auto" {
        $LamaRuntime = if ($IsX64) { "directml" } else { "cpu" }
        $InstallWebGpu = $IsX64
    }
    "cpu" {
        $LamaRuntime = "cpu"
    }
    "gpu" {
        if (-not $IsX64) {
            throw "DirectML LaMa runtime requires 64-bit Windows on x86-64."
        }
        $LamaRuntime = "directml"
        $InstallWebGpu = $true
    }
    "directml" {
        if (-not $IsX64) {
            throw "DirectML LaMa runtime requires 64-bit Windows on x86-64."
        }
        $LamaRuntime = "directml"
    }
    "webgpu" {
        if (-not $IsX64) {
            throw "WebGPU LaMa runtime requires 64-bit Windows on x86-64."
        }
        $LamaRuntime = "cpu"
        $InstallWebGpu = $true
    }
    "d3d12" {
        if (-not $IsX64) {
            throw "WebGPU over Direct3D 12 requires 64-bit Windows on x86-64."
        }
        $LamaRuntime = "cpu"
        $InstallWebGpu = $true
    }
    default {
        throw "MARNWICK_LAMA_RUNTIME must be auto, cpu, gpu, directml, webgpu, or d3d12."
    }
}

$LamaRuntimeDisplay = if ($LamaRuntime -eq "directml" -and $InstallWebGpu) {
    "directml + webgpu"
} elseif ($InstallWebGpu) {
    "webgpu"
} else {
    $LamaRuntime
}

& $PythonExe @PythonArgs -m venv $VenvDir
if ($LASTEXITCODE -ne 0) {
    throw "Python failed to create the virtual environment at: $VenvDir"
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual environment Python was not created at: $VenvPython"
}
$VenvPythonInfo = Get-PythonInfo -Executable $VenvPython
if (-not (Test-MarnwickPython -Info $VenvPythonInfo)) {
    throw "The virtual environment does not contain 64-bit CPython 3.12, 3.13, or 3.14. Remove it and rerun .\setup.ps1."
}

function Invoke-Pip {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $VenvPython -m pip @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed: $($Arguments -join ' ')"
    }
}

Invoke-Pip -Arguments @("install", "--upgrade", "pip", "setuptools", "wheel")
$LockFile = Join-Path $RootDir "requirements-dev.lock"
if (Test-Path -LiteralPath $LockFile) {
    if (-not $InstallWebGpu) {
        Invoke-Pip -Arguments @(
            "uninstall",
            "-y",
            "onnxruntime-ep-webgpu"
        )
    }
    if ($LamaRuntime -eq "cpu") {
        Invoke-Pip -Arguments @(
            "uninstall",
            "-y",
            "onnxruntime",
            "onnxruntime-gpu",
            "onnxruntime-directml"
        )
    }
    Invoke-Pip -Arguments @("install", "--require-hashes", "-r", $LockFile)
    if ($LamaRuntime -eq "directml") {
        Invoke-Pip -Arguments @(
            "uninstall",
            "-y",
            "onnxruntime",
            "onnxruntime-gpu",
            "onnxruntime-directml"
        )
        $DirectMlLockFile = Join-Path $RootDir "requirements-lama-directml.lock"
        Invoke-Pip -Arguments @(
            "install",
            "--no-deps",
            "--require-hashes",
            "-r",
            $DirectMlLockFile
        )
    } else {
        $CpuLockFile = Join-Path $RootDir "requirements-lama-cpu.lock"
        Invoke-Pip -Arguments @(
            "install",
            "--no-deps",
            "--require-hashes",
            "-r",
            $CpuLockFile
        )
    }
    if ($InstallWebGpu) {
        $WebGpuLockFile = Join-Path $RootDir "requirements-lama-webgpu.lock"
        Invoke-Pip -Arguments @(
            "install",
            "--no-deps",
            "--require-hashes",
            "-r",
            $WebGpuLockFile
        )
    }
    Invoke-Pip -Arguments @("install", "--no-deps", "-e", $RootDir)
} else {
    if ($LamaRuntime -eq "directml") {
        if ($InstallWebGpu) {
            Invoke-Pip -Arguments @("install", "-e", "${RootDir}[dev,directml,webgpu]")
        } else {
            Invoke-Pip -Arguments @("install", "-e", "${RootDir}[dev,directml]")
        }
    } elseif ($InstallWebGpu) {
        Invoke-Pip -Arguments @("install", "-e", "${RootDir}[cpu,dev,webgpu]")
    } else {
        Invoke-Pip -Arguments @("install", "-e", "${RootDir}[cpu,dev]")
    }
}

$StartPs1 = Join-Path $RootDir "start.ps1"
@'
$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = if ($env:MARNWICK_VENV) { $env:MARNWICK_VENV } else { Join-Path $RootDir ".venv" }
$Python = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Marnwick virtual environment is missing. Run .\setup.ps1 first."
    exit 1
}

& $Python -c 'import platform, sys; raise SystemExit(0 if platform.python_implementation() == "CPython" and sys.version_info[:2] in ((3, 12), (3, 13), (3, 14)) else 1)'
if ($LASTEXITCODE -ne 0) {
    Write-Error "Marnwick's Python is no longer compatible. Run .\setup.ps1 again."
    exit 1
}

& $Python -m marnwick @args
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath $StartPs1 -Encoding UTF8

$StartCmd = Join-Path $RootDir "start.cmd"
@'
@echo off
setlocal
set "ROOT_DIR=%~dp0"
if defined MARNWICK_VENV (
  set "VENV_DIR=%MARNWICK_VENV%"
) else (
  set "VENV_DIR=%ROOT_DIR%.venv"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo Marnwick virtual environment is missing. Run setup.ps1 first. 1>&2
  exit /b 1
)

"%VENV_DIR%\Scripts\python.exe" -c "import platform, sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] in ((3, 12), (3, 13), (3, 14)) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Marnwick's Python is no longer compatible. Run setup.ps1 again. 1>&2
  exit /b 1
)

if not exist "%VENV_DIR%\Scripts\pythonw.exe" (
  echo Marnwick's windowed Python launcher is missing. Run setup.ps1 again. 1>&2
  exit /b 1
)

start "" "%VENV_DIR%\Scripts\pythonw.exe" -m marnwick %*
'@ | Set-Content -LiteralPath $StartCmd -Encoding ASCII

$IconScript = @'
from pathlib import Path
from PIL import Image
import sys

source = Path(sys.argv[1])
dest = Path(sys.argv[2])
with Image.open(source) as image:
    image = image.convert("RGBA")
    image.save(dest, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
'@
$IconScript | & $VenvPython - $IconPng $IconIco
if ($LASTEXITCODE -ne 0) {
    throw "Could not generate the Windows application icon."
}

$ProgramsDir = [Environment]::GetFolderPath("Programs")
if (-not $ProgramsDir) {
    throw "Could not locate the Windows Start Menu Programs folder."
}

$ShortcutDir = Join-Path $ProgramsDir "Marnwick"
$ShortcutPath = Join-Path $ShortcutDir "Marnwick.lnk"
New-Item -ItemType Directory -Path $ShortcutDir -Force | Out-Null

$ShortcutTarget = if (Test-Path -LiteralPath $VenvPythonw) { $VenvPythonw } else { $VenvPython }
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ShortcutTarget
$Shortcut.Arguments = "-m marnwick"
$Shortcut.WorkingDirectory = $RootDir
$Shortcut.IconLocation = "$IconIco,0"
$Shortcut.Description = "Marnwick photo viewer and organizer"
$Shortcut.Save()

Write-Host "Marnwick is ready."
Write-Host "Installed LaMa runtimes: $LamaRuntimeDisplay + cpu fallback"
Write-Host "Start it with: .\start.ps1"
Write-Host "Start Menu shortcut installed at: $ShortcutPath"
