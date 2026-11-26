"""Build provenance and release artifact naming shared by the native build tools."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tomllib

PYTHON_VERSIONS = {"windows-x64": "3.14.4", "macos-arm64": "3.12.10"}
BUNDLE_NAMES = {"windows-x64": "Marnwick.exe", "macos-arm64": "Marnwick.app"}


def native_target() -> str:
    target = {
        ("win32", "amd64"): "windows-x64",
        ("darwin", "arm64"): "macos-arm64",
    }.get((sys.platform, platform.machine().lower()))
    if target is None:
        raise ValueError("Build requires native Windows x64 or macOS arm64")
    if platform.python_version() != PYTHON_VERSIONS[target]:
        raise ValueError(f"{target} requires Python {PYTHON_VERSIONS[target]}")
    return target


def version_tag(version: str, requested_tag: str | None = None) -> str:
    if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None:
        raise ValueError("Expected a stable MAJOR.MINOR.PATCH package version")
    tag = f"v{version}"
    if requested_tag is not None and requested_tag != tag:
        raise ValueError(f"Release tag {requested_tag!r} does not match package version {version}")
    return tag


def source_version(root: Path) -> str:
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    runtime = runpy.run_path(str(root / "src/marnwick/__init__.py"))["__version__"]
    if runtime != version:
        raise ValueError("Runtime version differs from pyproject.toml")
    return version


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_identity(root: Path) -> str:
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, text=True,
    ).strip():
        raise ValueError("Build provenance requires a clean checkout")
    return source


def bundle_digest(bundle: Path) -> str:
    if bundle.is_symlink():
        raise ValueError(f"Unexpected bundle symlink: {bundle}")
    if bundle.is_file():
        return sha256(bundle)
    if not bundle.is_dir():
        raise ValueError(f"Missing bundle: {bundle}")
    digest = hashlib.sha256()
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            entry = [relative, "symlink", str(path.readlink())]
        elif path.is_file():
            entry = [relative, "file", path.stat().st_mode & 0o777, sha256(path)]
        elif path.is_dir():
            entry = [relative, "directory", path.stat().st_mode & 0o777]
        else:
            raise ValueError(f"Unsupported bundle entry: {path}")
        digest.update((json.dumps(entry) + "\n").encode())
    return digest.hexdigest()


def verified_bundle(root: Path, source: str, target: str, tag: str) -> Path:
    dist = root / "dist"
    receipt = json.loads((dist / "native-build.json").read_bytes())
    if (receipt["source_sha"], receipt["target"], receipt["version_tag"]) != (source, target, tag):
        raise ValueError("Build receipt does not match current source, target and version")
    bundle = root / receipt["bundle"]
    if (
        bundle.name != BUNDLE_NAMES[target]
        or bundle.is_symlink()
        or not bundle.resolve().is_relative_to(dist.resolve())
    ):
        raise ValueError("Build receipt references an unexpected output path")
    if bundle_digest(bundle) != receipt["bundle_sha256"]:
        raise ValueError("Bundle changed after build")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require this release tag to match the source version")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    target = native_target()
    source = source_identity(root)
    version = source_version(root)
    tag = version_tag(version, args.tag)
    bundle = verified_bundle(root, source, target, tag)
    output = root / "dist/artifacts"
    output.mkdir(parents=True, exist_ok=True)
    extension = "zip" if target == "macos-arm64" else "exe"
    artifact = output / f"marnwick-{tag}-{target}-{source}.{extension}"
    if artifact.exists() or artifact.with_suffix(".json").exists():
        raise ValueError(f"Artifact already exists: {artifact}")

    if target == "macos-arm64":
        subprocess.run(
            ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
             str(bundle), str(artifact)],
            check=True,
        )
    else:
        shutil.copyfile(bundle, artifact)

    checksum = sha256(artifact)
    artifact.with_suffix(".json").write_text(json.dumps({
        "artifact": artifact.name,
        "sha256": checksum,
        "source_sha": source,
        "package_version": version,
        "version_tag": tag,
        "target": target,
        "python": platform.python_version(),
        "signing": "ad-hoc" if target == "macos-arm64" else "unsigned",
    }, indent=2) + "\n", encoding="utf-8")
    artifact.with_suffix(".sha256").write_text(
        f"{checksum}  {artifact.name}\n", encoding="utf-8", newline="\n",
    )
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as outputs:
            outputs.write(f"name={artifact.stem}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
