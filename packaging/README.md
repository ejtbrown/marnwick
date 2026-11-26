# Native builds and releases

MARN-3 starts Marnwick **1.0.0** from current-main baseline
`4efbeb186dfda93fed6c37951b133082194d0a2d`. A release build identifies the actual
commit that includes the packaging changes. The earlier local release-baseline
record and artifacts retain their original identity. The upstream release
sequence starts with `v1.0.0`. Increment the minor version for
backward-compatible features, for example `1.1.0`. Increment the patch version
for backward-compatible fixes, for example `1.0.1`. Update both
`pyproject.toml` and `src/marnwick/__init__.py` when changing the version.

| Target | Build host and Python | Deliverable |
| --- | --- | --- |
| Windows x64 | Windows Server 2022 GitHub runner, CPython 3.14.4 x64 | Single `Marnwick.exe` with embedded dependencies |
| macOS arm64 | macOS 15 Apple Silicon GitHub runner, CPython 3.12.10 arm64 | ZIP containing a self-contained `Marnwick.app` |

Use Windows 11 x64 or macOS 15 on Apple Silicon for desktop smoke testing.
Older OS versions, Windows arm64, Intel macOS, and Linux native packages are
outside the validated target matrix. End users can run the packaged app
without installing Python or Qt. Windows uses a console-enabled bootloader
to support the worker pipes and may show a console window. The app includes
CPU ONNX Runtime. Local inference models remain user data and are acquired
separately via the app's existing setup. GPU-specific providers are outside
these packages.

Configuration uses `~/.config/marnwick/config.json` by default, or
`XDG_CONFIG_HOME` / `MARNWICK_CONFIG_PATH` when configured. The model store uses
`%LOCALAPPDATA%\Marnwick\models` on Windows and
`${XDG_DATA_HOME:-~/.local/share}/marnwick/models` on macOS. Put catalogs and
other writable data in user-owned directories. Windows one-file unpacking
uses the user's temporary directory. Worker requests and intermediate outputs
also use writable temporary directories; the app bundle can be read-only.

## Build on a native host

Start with a clean Git checkout of the candidate commit and the exact native
interpreter above. Use a fresh environment and output directory. On Windows
PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements-runtime.lock -r requirements-lama-cpu.lock -r packaging/requirements-build.lock
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_packaging*.py" -v
.\.venv\Scripts\python.exe packaging/build_native.py
.\.venv\Scripts\python.exe packaging/archive_bundle.py
.\.venv\Scripts\python.exe packaging/smoke_artifact.py
```

On macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --require-hashes -r requirements-runtime.lock -r requirements-lama-cpu.lock -r packaging/requirements-build.lock
.venv/bin/python -m unittest discover -s tests -p 'test_packaging*.py' -v
.venv/bin/python packaging/build_native.py
.venv/bin/python packaging/archive_bundle.py
.venv/bin/python packaging/smoke_artifact.py
```

Artifacts in `dist/artifacts/` include the version tag and full source SHA in
their names, plus a JSON manifest and SHA-256 sidecar. PR builds use the
GitHub-tested merge commit SHA and the version from source. Tag builds
require the tag to match the package version. GitHub uploads both targets
after the native smoke tests pass. The tag-only release job verifies source,
tag, and asset checksums and attaches the named assets to an existing draft
for that tag. The draft remains unpublished.

## Smoke test a downloaded deliverable

Extract the outer GitHub Actions download if needed. From a clean desktop
with a writable temporary directory and no project checkout or Python
installation, run the commands below. Replace the placeholders with the
values in the downloaded filename. Copy only the executable on Windows
or extract the app ZIP on macOS.

```powershell
$exe = (Resolve-Path '.\marnwick-<tag>-windows-x64-<sha>.exe').Path
Get-FileHash -Algorithm SHA256 $exe
$smoke = Start-Process -FilePath $exe -ArgumentList '--packaging-self-test' -Wait -NoNewWindow -PassThru
if ($smoke.ExitCode -ne 0) { throw "Packaging self-test failed: $($smoke.ExitCode)" }
Start-Process -FilePath $exe
```

```sh
zip="$PWD/marnwick-<tag>-macos-arm64-<sha>.zip"
shasum -a 256 "$zip"
smoke_dir="$(mktemp -d)"
ditto -x -k "$zip" "$smoke_dir"
"$smoke_dir/Marnwick.app/Contents/MacOS/Marnwick" --packaging-self-test &&
open "$smoke_dir/Marnwick.app"
```

Compare the checksum with the sidecar. The self-test must exit with code zero
and print `Marnwick <version> packaging self-test passed`. It exercises Qt,
PyAV, embedded image assets, external configuration writes, the LaMa
one-shot and persistent stdin protocols, and the isolated GPU diagnostic's
CPU baseline with synthetic inputs. It uses local temporary files and a
generated ONNX graph. For desktop launch, verify the window and its icons,
then close the app. Use a disposable user profile for that manual step.

Windows builds lack Authenticode signatures. macOS builds use PyInstaller
ad-hoc signing for Apple Silicon and are unnotarized. SmartScreen or
Gatekeeper may warn or refuse to run a downloaded build under local policy.
Verify the checksum, and follow the test machine's approved procedure for
reviewing unsigned apps.
