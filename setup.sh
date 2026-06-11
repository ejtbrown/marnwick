#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${MARNWICK_VENV:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Could not find Python executable: $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -f "$ROOT_DIR/marnwick-icon.png" ]]; then
  echo "Could not find Marnwick icon: $ROOT_DIR/marnwick-icon.png" >&2
  exit 1
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR[dev]"

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

case "$(uname -s)" in
  Linux*)
    install_linux_desktop_entry
    ;;
  *)
    echo "No app-menu integration was installed for this OS. Use ./start.sh to run Marnwick." >&2
    ;;
esac

echo "Marnwick is ready."
echo "Start it with: ./start.sh"
