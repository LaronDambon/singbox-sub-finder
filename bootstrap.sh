#!/usr/bin/env bash
# BOOTSTRAP: downloads the repository and runs the installer.
#
# One command, no manual cloning:
#
#   curl -fsSL https://raw.githubusercontent.com/LaronDambon/singbox-sub-finder/main/bootstrap.sh | bash
#
# Where it installs:
#   program  /opt/singbox-subs-finder                (override: SINGBOX_PROGRAM_DIR)
#   data     ~/.local/share/singbox-subs-finder      (override: SINGBOX_DATA_DIR)
#
# Data lives apart from the program on purpose: updating the code must not
# touch the server database, config.yaml or .env.
set -euo pipefail

REPO="${SINGBOX_REPO:-https://github.com/LaronDambon/singbox-sub-finder.git}"
BRANCH="${SINGBOX_BRANCH:-main}"

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

say "Installing singbox-subs-finder"
echo

if ! command -v git >/dev/null 2>&1; then
    die "git not found. Debian/Ubuntu: sudo apt install git"
fi

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    die "Python 3 not found. Debian/Ubuntu: sudo apt install python3 python3-venv"
fi

if ! "$PYTHON" -c 'import venv' 2>/dev/null; then
    die "venv module missing. Debian/Ubuntu: sudo apt install python3-venv"
fi

if [ -n "${SINGBOX_PROGRAM_DIR:-}" ]; then
    DIR="$SINGBOX_PROGRAM_DIR"
elif [ -n "${SINGBOX_DIR:-}" ]; then
    DIR="$SINGBOX_DIR"
else
    DIR="/opt/singbox-subs-finder"
fi

if [ -n "${SINGBOX_DATA_DIR:-}" ]; then
    DATA="$SINGBOX_DATA_DIR"
else
    DATA="${XDG_DATA_HOME:-$HOME/.local/share}/singbox-subs-finder"
fi

echo "  Python:    $("$PYTHON" --version 2>&1)"
echo "  git:       $(git --version)"
echo "  program:   $DIR"
echo "  data:      $DATA"
echo

SUDO=""
NEED_SUDO=""
if [ ! -d "$DIR" ]; then
    PARENT="$(dirname "$DIR")"
    if [ ! -w "$PARENT" ]; then NEED_SUDO="1"; fi
elif [ ! -w "$DIR" ]; then
    NEED_SUDO="1"
fi

if [ -n "$NEED_SUDO" ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        warn "Need write access to $DIR - using sudo"
        SUDO="sudo"
    else
        die "No write access to $DIR and no sudo. Run as root or set SINGBOX_PROGRAM_DIR."
    fi
fi

$SUDO mkdir -p "$DIR"

if [ -d "$DIR/.git" ]; then
    say "Directory already exists - updating"
    $SUDO git -C "$DIR" fetch --depth 1 origin "$BRANCH"
    $SUDO git -C "$DIR" checkout "$BRANCH" 2>/dev/null || true
    $SUDO git -C "$DIR" reset --hard "origin/$BRANCH"
else
    if [ -n "$(ls -A "$DIR" 2>/dev/null || true)" ]; then
        die "$DIR is not empty and is not a git clone. Clear it or set SINGBOX_PROGRAM_DIR."
    fi
    say "Downloading repository"
    $SUDO git clone --depth 1 --branch "$BRANCH" "$REPO" "$DIR"
fi

mkdir -p "$DATA"

say "Running installer"
echo
cd "$DIR/singbox-subscribe"
$SUDO chmod +x install.sh update.sh setup.sh 2>/dev/null || true

export SINGBOX_PROGRAM_DIR="$DIR"
export SINGBOX_DATA_DIR="$DATA"

exec $SUDO ./install.sh "$@"
