from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    venv = tmp_path / "venv"
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_PYTHON_LOG"
if [[ "${FAKE_PIP_FAIL:-0}" == "1" && "$*" == *"-m pip"* ]]; then
  exit 42
fi
if [[ "${FAKE_PYTHON_INCOMPATIBLE:-0}" == "1" && "$*" == *"sys.implementation.name"* ]]; then
  exit 43
fi
""",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return venv, python


def launcher_environment(venv: Path, log_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["MARNWICK_VENV"] = str(venv)
    environment["FAKE_PYTHON_LOG"] = str(log_path)
    return environment


def launched_python_commands(log_path: Path) -> list[str]:
    return [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("-m ")
    ]


def write_fake_setup_python(
    path: Path,
    *,
    minor: int,
    architecture: str,
) -> None:
    script = r"""#!/usr/bin/env bash
set -euo pipefail
FAKE_MINOR=__MINOR__
FAKE_ARCH=__ARCH__
printf '%s %s\n' "$(basename "$0")" "$*" >> "$FAKE_SETUP_LOG"
if [[ "${1:-}" == "-c" ]]; then
  code="${2:-}"
  if [[ "$code" == *"minimum ="* ]]; then
    minimum="${3:-12}"
    maximum="${4:-15}"
    system_name="${5:-}"
    if [[ "$system_name" == Darwin* && "$FAKE_ARCH" == "x86_64" && "$maximum" -gt 14 ]]; then
      maximum=14
    fi
    if (( FAKE_MINOR >= minimum && FAKE_MINOR < maximum )); then
      exit 0
    fi
    exit 1
  fi
  if [[ "$code" == *"platform.python_implementation"* ]]; then
    printf 'CPython 3.%s.0 (%s, 64-bit)\n' "$FAKE_MINOR" "$FAKE_ARCH"
  elif [[ "$code" == *"platform.machine().lower"* ]]; then
    printf '%s\n' "$FAKE_ARCH"
  elif [[ "$code" == *"struct.calcsize"* ]]; then
    printf '64\n'
  fi
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  destination="${3:?missing virtual environment path}"
  mkdir -p "$destination/bin"
  cp "$0" "$destination/bin/python"
  chmod +x "$destination/bin/python"
  exit 0
fi
exit 0
"""
    path.write_text(
        script.replace("__MINOR__", str(minor)).replace("__ARCH__", architecture),
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_fake_macos_commands(fake_bin: Path, *, architecture: str) -> None:
    uname = fake_bin / "uname"
    uname.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "-s" ]]; then
  printf 'Darwin\\n'
elif [[ "${{1:-}}" == "-m" ]]; then
  printf '{architecture}\\n'
else
  printf 'Darwin test 26.6.1\\n'
fi
""",
        encoding="utf-8",
    )
    uname.chmod(0o755)
    sw_vers = fake_bin / "sw_vers"
    sw_vers.write_text(
        "#!/usr/bin/env bash\nprintf '26.6.1\\n'\n",
        encoding="utf-8",
    )
    sw_vers.chmod(0o755)


def copied_setup_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy2(PROJECT_ROOT / "setup.sh", root / "setup.sh")
    (root / "marnwick-icon.png").write_bytes(b"test icon")
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    return root


def test_setup_embeds_the_checked_in_start_script() -> None:
    setup_text = (PROJECT_ROOT / "setup.sh").read_text(encoding="utf-8")
    generated_start = setup_text.split(
        "cat > \"$ROOT_DIR/start.sh\" <<'RUNNER'\n",
        1,
    )[1].split("\nRUNNER\n", 1)[0]

    assert f"{generated_start}\n" == (PROJECT_ROOT / "start.sh").read_text(
        encoding="utf-8"
    )


def test_start_refreshes_changed_runtime_dependencies_once(tmp_path: Path) -> None:
    venv, _python = make_fake_venv(tmp_path)
    log_path = tmp_path / "python.log"
    environment = launcher_environment(venv, log_path)

    first = subprocess.run(
        [str(PROJECT_ROOT / "start.sh"), "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode == 0
    assert "dependencies changed" in first.stdout
    assert launched_python_commands(log_path) == [
        f"-m pip install --require-hashes -r {PROJECT_ROOT / 'requirements-runtime.lock'}",
        f"-m pip install --no-deps -e {PROJECT_ROOT}",
        "-m marnwick --help",
    ]
    assert (venv / ".marnwick-runtime.cksum").is_file()

    log_path.write_text("", encoding="utf-8")
    second = subprocess.run(
        [str(PROJECT_ROOT / "start.sh"), "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0
    assert "dependencies changed" not in second.stdout
    assert launched_python_commands(log_path) == [
        "-m marnwick --help"
    ]


def test_start_does_not_stamp_a_failed_dependency_refresh(tmp_path: Path) -> None:
    venv, _python = make_fake_venv(tmp_path)
    log_path = tmp_path / "python.log"
    environment = launcher_environment(venv, log_path)
    environment["FAKE_PIP_FAIL"] = "1"

    result = subprocess.run(
        [str(PROJECT_ROOT / "start.sh")],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 42
    assert not (venv / ".marnwick-runtime.cksum").exists()
    assert launched_python_commands(log_path) == [
        f"-m pip install --require-hashes -r {PROJECT_ROOT / 'requirements-runtime.lock'}"
    ]


def test_start_rejects_an_incompatible_existing_environment(tmp_path: Path) -> None:
    venv, _python = make_fake_venv(tmp_path)
    log_path = tmp_path / "python.log"
    environment = launcher_environment(venv, log_path)
    environment["FAKE_PYTHON_INCOMPATIBLE"] = "1"

    result = subprocess.run(
        [str(PROJECT_ROOT / "start.sh")],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Python is no longer compatible" in result.stderr
    assert launched_python_commands(log_path) == []


def test_macos_setup_skips_incompatible_pythons_and_selects_313_on_intel(
    tmp_path: Path,
) -> None:
    root = copied_setup_project(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_macos_commands(fake_bin, architecture="x86_64")
    write_fake_setup_python(fake_bin / "python3", minor=9, architecture="x86_64")
    write_fake_setup_python(fake_bin / "python3.14", minor=14, architecture="x86_64")
    write_fake_setup_python(fake_bin / "python3.13", minor=13, architecture="x86_64")
    log_path = tmp_path / "setup.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_SETUP_LOG": str(log_path),
            "MARNWICK_VENV": str(root / "venv"),
        }
    )

    result = subprocess.run(
        [str(root / "setup.sh")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "python3 -c" in log
    assert "python3.14 -c" in log
    assert "python3.13 -m venv" in log
    assert f"{root}[dev,macos-intel]" in log


def test_macos_setup_rejects_an_explicit_old_python_before_creating_a_venv(
    tmp_path: Path,
) -> None:
    root = copied_setup_project(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_macos_commands(fake_bin, architecture="x86_64")
    old_python = fake_bin / "old-python"
    write_fake_setup_python(old_python, minor=9, architecture="x86_64")
    log_path = tmp_path / "setup.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_SETUP_LOG": str(log_path),
            "MARNWICK_VENV": str(root / "venv"),
            "PYTHON": str(old_python),
        }
    )

    result = subprocess.run(
        [str(root / "setup.sh")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "requires 64-bit CPython 3.12 or 3.13" in result.stderr
    assert "CPython 3.9.0" in result.stderr
    assert not (root / "venv").exists()


def test_macos_setup_accepts_314_and_webgpu_on_apple_silicon(
    tmp_path: Path,
) -> None:
    root = copied_setup_project(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_macos_commands(fake_bin, architecture="arm64")
    write_fake_setup_python(fake_bin / "python3", minor=14, architecture="arm64")
    log_path = tmp_path / "setup.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_SETUP_LOG": str(log_path),
            "MARNWICK_VENV": str(root / "venv"),
        }
    )

    result = subprocess.run(
        [str(root / "setup.sh")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "python3 -m venv" in log
    assert f"{root}[cpu,dev,webgpu]" in log


def test_python_and_runtime_contracts_cover_supported_macos_and_windows() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.12,<3.15"

    dev_lock = (PROJECT_ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    cpu_lock = (PROJECT_ROOT / "requirements-lama-cpu.lock").read_text(encoding="utf-8")
    intel_lock = (PROJECT_ROOT / "requirements-lama-macos-intel.lock").read_text(
        encoding="utf-8"
    )
    assert "onnxruntime==" not in dev_lock
    assert "onnxruntime==1.27.0" in cpu_lock
    assert "onnxruntime==1.23.2" in intel_lock
    assert "coloredlogs==" in intel_lock
    assert "sympy==" in intel_lock

    powershell = (PROJECT_ROOT / "setup.ps1").read_text(encoding="utf-8")
    assert 'foreach ($Version in @("3.14", "3.13", "3.12"))' in powershell
    assert "$PythonArchitecture = [string]$PythonInfo.architecture" in powershell
    assert "PROCESSOR_ARCHITECTURE" not in powershell
    assert "$WindowsBuild -lt 17763" in powershell
    assert '"requirements-lama-cpu.lock"' in powershell
    assert "Marnwick's Python is no longer compatible" in powershell
