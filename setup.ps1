$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = if ($env:MARNWICK_VENV) { $env:MARNWICK_VENV } else { Join-Path $RootDir ".venv" }
$IconPng = Join-Path $RootDir "marnwick-icon.png"
$IconIco = Join-Path $RootDir "marnwick.ico"

if ($env:OS -ne "Windows_NT") {
    throw "setup.ps1 is intended for Windows. Use ./setup.sh on Linux."
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

& $PythonExe @PythonArgs -m venv $VenvDir

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual environment Python was not created at: $VenvPython"
}

& $VenvPython -m pip install --upgrade pip setuptools wheel
& $VenvPython -m pip install -e "${RootDir}[dev]"

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
Write-Host "Start it with: .\start.ps1"
Write-Host "Start Menu shortcut installed at: $ShortcutPath"
