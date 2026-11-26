"""Build a native executable or app and record its source provenance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from archive_bundle import (
    BUNDLE_NAMES, bundle_digest, native_target, source_identity, source_version, version_tag,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require this release tag to match the source version")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    target = native_target()
    source = source_identity(root)
    tag = version_tag(source_version(root), args.tag)
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    receipt = dist / "native-build.json"
    if receipt.exists():
        raise ValueError("Build output already exists. Use a fresh checkout or move dist aside.")

    output = Path(tempfile.mkdtemp(prefix=f"native-{source[:12]}-", dir=dist))
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", str(output), "--workpath", str(root / "build" / output.name),
         str(root / "packaging/marnwick.spec")],
        cwd=root,
        check=True,
    )
    if source_identity(root) != source:
        raise ValueError("Source changed during build")
    bundle = output / BUNDLE_NAMES[target]
    record = {
        "source_sha": source,
        "target": target,
        "version_tag": tag,
        "bundle": bundle.relative_to(root).as_posix(),
        "bundle_sha256": bundle_digest(bundle),
    }
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
