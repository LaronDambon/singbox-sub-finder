#!/usr/bin/env bash
# WRAPPER. All logic lives in installer.py next to this file.
#
# Deliberately thin: find Python, pass arguments, call the module. That way
# install/update/setup behave the same on Linux and Windows, and a fix is
# needed in one place only.
set -euo pipefail

find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! PYTHON="$(find_python)"; then
    echo
    echo "Python 3 not found."
    echo "Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip"
    echo "Fedora:         sudo dnf install python3 python3-pip"
    echo "macOS:          brew install python3"
    exit 1
fi

ARGS=()
for arg in "$@"; do
    case "$arg" in
        --browser|-b)  ARGS+=(--browser) ;;
        --upgrade|-u)  ARGS+=(--upgrade) ;;
        --recreate)    ARGS+=(--recreate-venv) ;;
        --quiet|-q)    ARGS+=(--quiet) ;;
        *)             ARGS+=("$arg") ;;
    esac
done

exec "$PYTHON" "$HERE/installer.py" install "${ARGS[@]+\"${ARGS[@]}\"}"
