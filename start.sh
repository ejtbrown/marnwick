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
