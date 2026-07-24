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

if (-not (Test-Path -LiteralPath $IconPng)) {
    throw "Could not find Marnwick icon: $IconPng"
}

$PythonExe = $null
$PythonArgs = @()
if ($env:PYTHON) {
    $PythonExe = $env:PYTHON
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonExe = $PythonCommand.Source
    } else {
        $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($PyLauncher) {
            $PythonExe = $PyLauncher.Source
            $PythonArgs = @("-3")
        }
    }
}

if (-not $PythonExe) {
    throw "Could not find Python. Install Python 3.11+ or set the PYTHON environment variable."
}

$IsX64 = $env:PROCESSOR_ARCHITECTURE -eq "AMD64"
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

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual environment Python was not created at: $VenvPython"
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

if not exist "%VENV_DIR%\Scripts\pythonw.exe" (
  echo Marnwick virtual environment is missing. Run setup.ps1 first. 1>&2
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
