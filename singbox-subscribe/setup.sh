#!/usr/bin/env bash
# WRAPPER. All logic lives in installer.py next to this file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$HERE/../.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    echo
    echo "Virtual environment .venv not found."
    echo "Run the installer first:  ./install.sh"
    exit 1
fi

exec "$VENV_PYTHON" "$HERE/setup.py" "$@"
