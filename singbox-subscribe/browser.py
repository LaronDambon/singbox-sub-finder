"""Единый вход во все инструменты браузерной проверки.

Зачем один файл. Пакет проверок лежит внутри singbox-subscribe/browser, поэтому
"python -m browser.login" работает только из той папки. Шесть тонких обёрток в
корне решали это, но засоряли репозиторий: шесть почти одинаковых файлов ради
одной строки настройки пути. Здесь то же самое одной командой.

Запуск (из папки singbox-subscribe — там же, где этот файл):

    python browser.py login    gemini
    python browser.py check    gemini --proxy 127.0.0.1:51490
    python browser.py refine   gemini --pause 2
    python browser.py trust    gemini --dry-run
    python browser.py session  export gemini --out gemini.session

Каждая подкоманда — отдельный модуль пакета browser: подробности и ключи
смотрите в "python browser.py <команда> --help".
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Папка пакета singbox-subscribe. Файл лежит ВНУТРИ неё (переехал из корня
#: репозитория): браузерные инструменты — часть программы, а не файлы
#: пользователя, и место им рядом с пакетом, а не рядом со start.py.
PACKAGE_DIR = Path(__file__).resolve().parent


def bootstrap() -> None:
    """Делает пакет browser импортируемым из корня репозитория."""
    target = str(PACKAGE_DIR)
    if target not in sys.path:
        sys.path.insert(0, target)


#: Подкоманда -> (модуль пакета, короткое описание).
COMMANDS = {
    "login": ("browser.login",
              "войти в сервис: открыть окно и дать человеку залогиниться"),
    "check": ("browser.check",
              "проверить сервис через прокси сервера (один сервер)"),
    "refine": ("browser.refine_cli",
               "проверить все серверы с меткой сервиса"),
    "trust": ("browser.trust",
              "снять метки, не подтверждённые браузером"),
    "session": ("browser.portable",
                "перенести сессию на другую машину (шифрованный файл)"),
    "guard": ("browser.guard",
              "проверить индекс git на cookies и токены; --install ставит хук"),
}


def usage() -> int:
    print("Инструменты браузерной проверки сервисов")
    print()
    print("Использование: python browser.py <команда> [ключи]")
    print()
    print("Команды:")
    width = max(len(name) for name in COMMANDS)
    for name, (_module, desc) in COMMANDS.items():
        print("  %-*s  %s" % (width, name, desc))
    print()
    print("Подробности по команде: python browser.py <команда> --help")
    print()
    print("Типичный порядок:")
    print("  1. python browser.py login gemini     # один раз")
    print("  2. python browser.py refine gemini    # проверка по метке")
    print("  3. python browser.py trust gemini     # снять лишние метки")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        return usage()

    name = args[0]
    if name not in COMMANDS:
        print("Неизвестная команда: %s" % name, file=sys.stderr)
        print(file=sys.stderr)
        usage()
        return 2

    module_name, _desc = COMMANDS[name]
    bootstrap()
    try:
        module = __import__(module_name, fromlist=["main"])
    except ImportError as exc:
        # Чаще всего это отсутствующий playwright: говорим, что поставить.
        print("Не удалось загрузить инструмент %s: %s" % (name, exc), file=sys.stderr)
        print("Установите зависимости: pip install -r requirements.txt",
              file=sys.stderr)
        return 1

    main_fn = getattr(module, "main", None)
    if main_fn is None:
        print("В модуле %s нет точки входа main()" % module_name, file=sys.stderr)
        return 1
    # Ключи передаём как есть, без имени команды: каждый модуль разбирает свои.
    return int(main_fn(args[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
