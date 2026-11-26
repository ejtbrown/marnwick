import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "packaging/entry.py")],
    pathex=[str(root / "src"), str(root / "packaging")],
    binaries=collect_dynamic_libs("onnxruntime"),
    datas=[
        (str(path), "marnwick/assets")
        for path in sorted((root / "src/marnwick/assets").glob("*.png"))
    ],
    hiddenimports=[
        "marnwick.assets", "marnwick.lama_worker", "marnwick.gpu_test",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf",
    ],
)
pyz = PYZ(a.pure)
icon = str(root / "src/marnwick/assets/marnwick-icon.png")
# Console bootloaders keep the persistent worker's piped stdin usable.
if sys.platform == "win32":
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Marnwick",
              console=True, strip=False, upx=False, icon=icon)
elif sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Marnwick",
              console=True, strip=False, upx=False, target_arch="arm64")
    coll = COLLECT(exe, a.binaries, a.datas, name="Marnwick", strip=False, upx=False)
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    app = BUNDLE(
        coll,
        name="Marnwick.app",
        icon=icon,
        bundle_identifier="com.ejtbrown.marnwick",
        version=metadata["project"]["version"],
        # PyInstaller defaults console bundles to background-only.
        info_plist={"LSBackgroundOnly": False, "NSHighResolutionCapable": True},
    )
else:
    raise SystemExit("Build on a supported native host")
