"""Verify and smoke-test the finished artifact from an isolated directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from archive_bundle import native_target, sha256, source_identity, source_version, version_tag


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    target = native_target()
    source = source_identity(root)
    version = source_version(root)
    name = f"marnwick-{version_tag(version)}-{target}-{source}"
    output = root / "dist/artifacts"
    artifact = output / f"{name}{'.zip' if target == 'macos-arm64' else '.exe'}"
    manifest = json.loads((output / f"{name}.json").read_bytes())
    if (
        manifest["artifact"] != artifact.name
        or manifest["source_sha"] != source
        or manifest["target"] != target
        or manifest["version_tag"] != version_tag(version)
        or sha256(artifact) != manifest["sha256"]
    ):
        raise ValueError("Artifact metadata or checksum differs from its manifest")
    with tempfile.TemporaryDirectory(prefix="Marnwick artifact café ") as temp:
        clean = Path(temp)
        if target == "macos-arm64":
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(artifact), temp], check=True)
            executable = clean / "Marnwick.app/Contents/MacOS/Marnwick"
        else:
            executable = clean / "Marnwick.exe"
            shutil.copyfile(artifact, executable)
        environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith(("PYTHON", "QT_", "QML", "MARNWICK_", "_PYI_"))
        }
        # Windows needs its system directories to discover native system DLLs.
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        environment["PATH"] = (
            os.pathsep.join(map(str, (system / "System32", system)))
            if target == "windows-x64" else "/usr/bin:/bin"
        )
        run_dir = clean / "external working directory"
        run_dir.mkdir()
        result = subprocess.run(
            [str(executable), "--packaging-self-test"],
            cwd=run_dir,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
        if f"Marnwick {version} packaging self-test passed" not in result.stdout.splitlines():
            raise ValueError("Executable did not report a successful packaging self-test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
