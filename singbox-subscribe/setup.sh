#!/usr/bin/env bash
# ОБЁРТКА. Вся логика — в installer.py рядом с этим файлом.
#
# Файл намеренно тонкий: находит Python, передаёт аргументы и зовёт модуль.
# Так установка, обновление и мастер на Linux и Windows ведут себя
# одинаково, и шаг достаточно починить в одном месте.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$HERE/../.venv/bin/python"

# Мастеру нужен интерпретатор ИЗ VENV: он импортирует config/config.py, а
# зависимости (PyYAML, ruamel.yaml) стоят именно туда. Системный Python их
# не увидит.
if [ ! -x "$VENV_PYTHON" ]; then
    echo
    echo "Окружение .venv не найдено."
    echo "Сначала выполните установку:  ./install.sh"
    exit 1
fi

exec "$VENV_PYTHON" "$HERE/setup.py" "$@"
