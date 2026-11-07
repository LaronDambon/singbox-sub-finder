#!/usr/bin/env bash
# ЗАГРУЗЧИК: скачивает репозиторий и запускает установку.
#
# Предназначен для запуска ОДНОЙ КОМАНДОЙ, без клонирования вручную:
#
#   curl -fsSL https://raw.githubusercontent.com/LaronDambon/singbox-sub-finder/main/bootstrap.sh | bash
#
# Что делает:
#   1. проверяет python3 и git;
#   2. клонирует репозиторий в ./singbox-subs-finder (или в $SINGBOX_DIR);
#   3. запускает внутри install.sh.
#
# Повторный запуск не ломает установку: если каталог уже есть, он
# обновляется, а не затирается — config.yaml, .env и база остаются.
#
# Переменные:
#   SINGBOX_DIR     куда ставить (по умолчанию ./singbox-subs-finder)
#   SINGBOX_BRANCH  ветка (по умолчанию main)
#   SINGBOX_REPO    адрес репозитория
set -euo pipefail

REPO="${SINGBOX_REPO:-https://github.com/LaronDambon/singbox-sub-finder.git}"
BRANCH="${SINGBOX_BRANCH:-main}"
DIR="${SINGBOX_DIR:-singbox-subs-finder}"

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

say "Установка singbox-subs-finder"
echo

# --- что нужно --------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    die "git не найден. Debian/Ubuntu: sudo apt install git"
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
    die "Python 3 не найден. Debian/Ubuntu: sudo apt install python3 python3-venv"
fi

# venv нужен для установки, но в минимальных системах его нет отдельно.
if ! "$PYTHON" -c 'import venv' 2>/dev/null; then
    die "модуль venv отсутствует. Debian/Ubuntu: sudo apt install python3-venv"
fi

echo "  Python: $("$PYTHON" --version 2>&1)"
echo "  git:    $(git --version)"
echo "  каталог: $DIR"
echo

# --- получение кода ---------------------------------------------------------
if [ -d "$DIR/.git" ]; then
    say "Каталог уже есть — обновляю"
    git -C "$DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$DIR" checkout "$BRANCH" 2>/dev/null || true
    git -C "$DIR" reset --hard "origin/$BRANCH"
else
    say "Скачиваю репозиторий"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$DIR"
fi

# --- установка --------------------------------------------------------------
say "Запускаю установку"
echo
cd "$DIR/singbox-subscribe"
chmod +x install.sh update.sh setup.sh 2>/dev/null || true
exec ./install.sh "$@"
