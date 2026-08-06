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
