from __future__ import annotations

import os
from pathlib import Path
import subprocess


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
    assert log_path.read_text(encoding="utf-8").splitlines() == [
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
    assert log_path.read_text(encoding="utf-8").splitlines() == [
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
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"-m pip install --require-hashes -r {PROJECT_ROOT / 'requirements-runtime.lock'}"
    ]
