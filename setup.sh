#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${MARNWICK_VENV:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON:-python3}"
LAMA_RUNTIME_REQUEST="${MARNWICK_LAMA_RUNTIME:-auto}"
INSTALL_WEBGPU=0
SYSTEM_NAME="$(uname -s)"
MACHINE_ARCH="$(uname -m)"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Could not find Python executable: $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -f "$ROOT_DIR/marnwick-icon.png" ]]; then
  echo "Could not find Marnwick icon: $ROOT_DIR/marnwick-icon.png" >&2
  exit 1
fi

webgpu_supported() {
  if [[ "$SYSTEM_NAME" == Linux* && "$MACHINE_ARCH" == "x86_64" ]]; then
    return 0
  fi
  if [[ "$SYSTEM_NAME" == Darwin* && "$MACHINE_ARCH" == "arm64" ]]; then
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
    && [[ "$MACHINE_ARCH" == "x86_64" ]] \
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
    if [[ "$SYSTEM_NAME" != Linux* || "$MACHINE_ARCH" != "x86_64" ]]; then
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
    if [[ "$SYSTEM_NAME" != Linux* || "$MACHINE_ARCH" != "x86_64" ]]; then
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
  fi
  if [[ "$INSTALL_WEBGPU" == "1" ]]; then
    "$VENV_DIR/bin/python" -m pip install \
      --no-deps \
      --require-hashes \
      -r "$ROOT_DIR/requirements-lama-webgpu.lock"
  fi
  "$VENV_DIR/bin/python" -m pip install --no-deps -e "$ROOT_DIR"
else
  if [[ "$LAMA_RUNTIME" == "nvidia" ]]; then
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

cat > "$ROOT_DIR/start.sh" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${MARNWICK_VENV:-$ROOT_DIR/.venv}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Marnwick virtual environment is missing. Run ./setup.sh first." >&2
  exit 1
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
