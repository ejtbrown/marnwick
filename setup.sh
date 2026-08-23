#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${MARNWICK_VENV:-$ROOT_DIR/.venv}"
LAMA_RUNTIME_REQUEST="${MARNWICK_LAMA_RUNTIME:-auto}"
INSTALL_WEBGPU=0
INSTALL_INTEL_MAC_CPU=0
SYSTEM_NAME="$(uname -s)"
MACHINE_ARCH="$(uname -m)"
MIN_PYTHON_MINOR=12
MAX_PYTHON_MINOR_EXCLUSIVE=15

if [[ "$SYSTEM_NAME" == Darwin* && "$MACHINE_ARCH" == "x86_64" ]]; then
  # ONNX Runtime 1.23.2 is the final release with Intel-macOS wheels, and
  # those wheels stop at CPython 3.13.
  MAX_PYTHON_MINOR_EXCLUSIVE=14
fi

python_is_supported() {
  local candidate="$1"
  "$candidate" -c '
import platform
import sys
minimum = (3, int(sys.argv[1]))
maximum = (3, int(sys.argv[2]))
if sys.argv[3].startswith("Darwin") and platform.machine().lower() in ("amd64", "x86_64"):
    maximum = min(maximum, (3, 14))
supported = (
    sys.implementation.name == "cpython"
    and minimum <= sys.version_info[:2] < maximum
)
raise SystemExit(0 if supported else 1)
' "$MIN_PYTHON_MINOR" "$MAX_PYTHON_MINOR_EXCLUSIVE" "$SYSTEM_NAME" >/dev/null 2>&1
}

python_description() {
  "$1" -c '
import platform
import struct
import sys
architecture = platform.machine() or "unknown architecture"
print(
    f"{platform.python_implementation()} {platform.python_version()} "
    f"({architecture}, {struct.calcsize(chr(80)) * 8}-bit)"
)
' 2>/dev/null || printf 'an unusable Python executable'
}

python_requirement() {
  if (( MAX_PYTHON_MINOR_EXCLUSIVE == 14 )); then
    printf '64-bit CPython 3.12 or 3.13'
  else
    printf '64-bit CPython 3.12, 3.13, or 3.14'
  fi
}

select_python() {
  local candidate
  if [[ -n "${PYTHON:-}" ]]; then
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
      echo "Could not find Python executable: $PYTHON" >&2
      exit 1
    fi
    if ! python_is_supported "$PYTHON"; then
      echo "Marnwick requires $(python_requirement)." >&2
      echo "PYTHON points to $(python_description "$PYTHON")." >&2
      exit 1
    fi
    PYTHON_BIN="$PYTHON"
    return
  fi

  for candidate in python3 python3.14 python3.13 python3.12; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && python_is_supported "$candidate"; then
      PYTHON_BIN="$candidate"
      return
    fi
  done

  echo "Could not find $(python_requirement)." >&2
  if [[ "$SYSTEM_NAME" == Darwin* ]]; then
    echo "Install a compatible Python from python.org or Homebrew, then rerun ./setup.sh." >&2
  else
    echo "Install a compatible Python or set PYTHON to its executable path." >&2
  fi
  exit 1
}

select_python

PYTHON_ARCH="$("$PYTHON_BIN" -c 'import platform; print(platform.machine().lower())')"
PYTHON_BITS="$("$PYTHON_BIN" -c 'import struct; print(struct.calcsize(chr(80)) * 8)')"
case "$PYTHON_ARCH" in
  amd64)
    PYTHON_ARCH="x86_64"
    ;;
  aarch64)
    PYTHON_ARCH="arm64"
    ;;
esac
if [[ "$PYTHON_BITS" != "64" ]]; then
  echo "Marnwick requires a 64-bit Python; selected $(python_description "$PYTHON_BIN")." >&2
  exit 1
fi
if [[ "$SYSTEM_NAME" == Darwin* \
  && "$PYTHON_ARCH" != "x86_64" \
  && "$PYTHON_ARCH" != "arm64" ]]; then
  echo "Marnwick supports x86-64 and Apple-silicon Python on macOS; selected $(python_description "$PYTHON_BIN")." >&2
  exit 1
fi
if [[ "$SYSTEM_NAME" == Darwin* && "$PYTHON_ARCH" == "x86_64" ]]; then
  INSTALL_INTEL_MAC_CPU=1
  MAX_PYTHON_MINOR_EXCLUSIVE=14
fi

if [[ ! -f "$ROOT_DIR/marnwick-icon.png" ]]; then
  echo "Could not find Marnwick icon: $ROOT_DIR/marnwick-icon.png" >&2
  exit 1
fi

webgpu_supported() {
  if [[ "$SYSTEM_NAME" == Linux* && "$PYTHON_ARCH" == "x86_64" ]]; then
    return 0
  fi
  if [[ "$SYSTEM_NAME" == Darwin* && "$PYTHON_ARCH" == "arm64" ]]; then
    local macos_major
    macos_major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
    [[ "$macos_major" =~ ^[0-9]+$ ]] && (( macos_major >= 14 ))
    return
  fi
  return 1
}

select_automatic_runtimes() {
  if webgpu_supported; then
    INSTALL_WEBGPU=1
  fi
  if [[ "$SYSTEM_NAME" == Linux* ]] \
    && [[ "$PYTHON_ARCH" == "x86_64" ]] \
    && command -v nvidia-smi >/dev/null 2>&1 \
    && nvidia-smi -L >/dev/null 2>&1; then
    LAMA_RUNTIME="nvidia"
  else
    LAMA_RUNTIME="cpu"
  fi
}

case "$LAMA_RUNTIME_REQUEST" in
  auto)
    select_automatic_runtimes
    ;;
  cpu)
    LAMA_RUNTIME="cpu"
    ;;
  gpu)
    if ! webgpu_supported; then
      echo "GPU LaMa setup requires x86-64 Linux or Apple silicon macOS 14 or newer." >&2
      exit 1
    fi
    select_automatic_runtimes
    ;;
  nvidia)
    if [[ "$SYSTEM_NAME" != Linux* || "$PYTHON_ARCH" != "x86_64" ]]; then
      echo "NVIDIA LaMa runtime requires x86-64 Linux." >&2
      exit 1
    fi
    LAMA_RUNTIME="nvidia"
    ;;
  webgpu)
    if ! webgpu_supported; then
      echo "WebGPU LaMa runtime requires x86-64 Linux or Apple silicon macOS 14 or newer." >&2
      exit 1
    fi
    LAMA_RUNTIME="cpu"
    INSTALL_WEBGPU=1
    ;;
  vulkan)
    if [[ "$SYSTEM_NAME" != Linux* || "$PYTHON_ARCH" != "x86_64" ]]; then
      echo "WebGPU over Vulkan requires x86-64 Linux." >&2
      exit 1
    fi
    LAMA_RUNTIME="cpu"
    INSTALL_WEBGPU=1
    ;;
  metal)
    if [[ "$SYSTEM_NAME" != Darwin* ]] || ! webgpu_supported; then
      echo "WebGPU over Metal requires Apple silicon macOS 14 or newer." >&2
      exit 1
    fi
    LAMA_RUNTIME="cpu"
    INSTALL_WEBGPU=1
    ;;
  *)
    echo "MARNWICK_LAMA_RUNTIME must be auto, cpu, gpu, nvidia, webgpu, vulkan, or metal." >&2
    exit 1
    ;;
esac

if [[ "$LAMA_RUNTIME" == "nvidia" && "$INSTALL_WEBGPU" == "1" ]]; then
  LAMA_RUNTIME_DISPLAY="nvidia + webgpu"
elif [[ "$INSTALL_WEBGPU" == "1" ]]; then
  LAMA_RUNTIME_DISPLAY="webgpu"
else
  LAMA_RUNTIME_DISPLAY="$LAMA_RUNTIME"
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
if ! python_is_supported "$VENV_DIR/bin/python"; then
  echo "The virtual environment does not contain $(python_requirement). Remove it and rerun ./setup.sh." >&2
  exit 1
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
if [[ -f "$ROOT_DIR/requirements-dev.lock" ]]; then
  if [[ "$INSTALL_WEBGPU" == "0" ]]; then
    "$VENV_DIR/bin/python" -m pip uninstall -y \
      onnxruntime-ep-webgpu >/dev/null
  fi
  if [[ "$LAMA_RUNTIME" == "cpu" ]]; then
    "$VENV_DIR/bin/python" -m pip uninstall -y \
      onnxruntime onnxruntime-gpu onnxruntime-directml >/dev/null
  fi
  "$VENV_DIR/bin/python" -m pip install --require-hashes -r "$ROOT_DIR/requirements-dev.lock"
  if [[ "$LAMA_RUNTIME" == "nvidia" ]]; then
    "$VENV_DIR/bin/python" -m pip uninstall -y \
      onnxruntime onnxruntime-gpu onnxruntime-directml >/dev/null
    "$VENV_DIR/bin/python" -m pip install \
      --no-deps \
      --require-hashes \
      -r "$ROOT_DIR/requirements-lama-nvidia.lock"
  elif [[ "$INSTALL_INTEL_MAC_CPU" == "1" ]]; then
    "$VENV_DIR/bin/python" -m pip uninstall -y \
      onnxruntime onnxruntime-gpu onnxruntime-directml >/dev/null
    "$VENV_DIR/bin/python" -m pip install \
      --no-deps \
      --require-hashes \
      -r "$ROOT_DIR/requirements-lama-macos-intel.lock"
  else
    "$VENV_DIR/bin/python" -m pip install \
      --no-deps \
      --require-hashes \
      -r "$ROOT_DIR/requirements-lama-cpu.lock"
  fi
  if [[ "$INSTALL_WEBGPU" == "1" ]]; then
    "$VENV_DIR/bin/python" -m pip install \
      --no-deps \
      --require-hashes \
      -r "$ROOT_DIR/requirements-lama-webgpu.lock"
  fi
  "$VENV_DIR/bin/python" -m pip install --no-deps -e "$ROOT_DIR"
else
  if [[ "$INSTALL_INTEL_MAC_CPU" == "1" ]]; then
    "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[dev,macos-intel]"
  elif [[ "$LAMA_RUNTIME" == "nvidia" ]]; then
    if [[ "$INSTALL_WEBGPU" == "1" ]]; then
      "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[dev,nvidia,webgpu]"
    else
      "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[dev,nvidia]"
    fi
  elif [[ "$INSTALL_WEBGPU" == "1" ]]; then
    "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[cpu,dev,webgpu]"
  else
    "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[cpu,dev]"
  fi
fi

dependency_fingerprint() {
  cksum < "$ROOT_DIR/pyproject.toml"
  if [[ -f "$ROOT_DIR/requirements-runtime.lock" ]]; then
    cksum < "$ROOT_DIR/requirements-runtime.lock"
  fi
}

dependency_fingerprint > "$VENV_DIR/.marnwick-runtime.cksum"

cat > "$ROOT_DIR/start.sh" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${MARNWICK_VENV:-$ROOT_DIR/.venv}"
RUNTIME_LOCK="$ROOT_DIR/requirements-runtime.lock"
DEPENDENCY_STAMP="$VENV_DIR/.marnwick-runtime.cksum"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Marnwick virtual environment is missing. Run ./setup.sh first." >&2
  exit 1
fi

MAX_PYTHON_MINOR_EXCLUSIVE=15
VENV_PYTHON_ARCH="$("$VENV_DIR/bin/python" -c 'import platform; print(platform.machine().lower())' 2>/dev/null || true)"
if [[ "$(uname -s)" == Darwin* \
  && ( "$VENV_PYTHON_ARCH" == "x86_64" || "$VENV_PYTHON_ARCH" == "amd64" ) ]]; then
  MAX_PYTHON_MINOR_EXCLUSIVE=14
fi
if ! "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and (3, 12) <= sys.version_info[:2] < (3, int(sys.argv[1])) else 1)' "$MAX_PYTHON_MINOR_EXCLUSIVE" >/dev/null 2>&1; then
  echo "Marnwick's Python is no longer compatible. Run ./setup.sh again." >&2
  exit 1
fi

dependency_fingerprint() {
  cksum < "$ROOT_DIR/pyproject.toml"
  if [[ -f "$RUNTIME_LOCK" ]]; then
    cksum < "$RUNTIME_LOCK"
  fi
}

CURRENT_DEPENDENCIES="$(dependency_fingerprint)"
INSTALLED_DEPENDENCIES="$(cat "$DEPENDENCY_STAMP" 2>/dev/null || true)"
if [[ "$CURRENT_DEPENDENCIES" != "$INSTALLED_DEPENDENCIES" ]]; then
  echo "Marnwick dependencies changed; updating the virtual environment..."
  if [[ -f "$RUNTIME_LOCK" ]]; then
    "$VENV_DIR/bin/python" -m pip install --require-hashes -r "$RUNTIME_LOCK"
    "$VENV_DIR/bin/python" -m pip install --no-deps -e "$ROOT_DIR"
  else
    "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"
  fi
  printf '%s\n' "$CURRENT_DEPENDENCIES" > "$DEPENDENCY_STAMP"
fi

exec "$VENV_DIR/bin/python" -m marnwick "$@"
RUNNER

chmod +x "$ROOT_DIR/start.sh"

install_linux_desktop_entry() {
  local desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  local desktop_file="$desktop_dir/marnwick.desktop"
  local exec_path icon_path

  exec_path="$(printf '%s' "$ROOT_DIR/start.sh" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  icon_path="$(printf '%s' "$ROOT_DIR/marnwick-icon.png" | sed 's/\\/\\\\/g')"

  mkdir -p "$desktop_dir"
  cat > "$desktop_file" <<RUNNER
[Desktop Entry]
Type=Application
Name=Marnwick
Comment=Fast photo viewer and organizer
Exec="$exec_path"
Icon=$icon_path
Terminal=false
Categories=Graphics;Photography;Viewer;
StartupNotify=true
StartupWMClass=marnwick
RUNNER

  chmod +x "$desktop_file"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
  fi
  echo "Desktop launcher installed at: $desktop_file"
}

case "$SYSTEM_NAME" in
  Linux*)
    install_linux_desktop_entry
    ;;
  *)
    echo "No app-menu integration was installed for this OS. Use ./start.sh to run Marnwick." >&2
    ;;
esac

echo "Marnwick is ready."
echo "Installed LaMa runtimes: $LAMA_RUNTIME_DISPLAY + cpu fallback"
echo "Start it with: ./start.sh"
