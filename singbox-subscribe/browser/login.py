"""Ручной вход в сервис: открыть окно, дать человеку залогиниться.

Запуск:

    python -m browser.login gemini
    python -m browser.login gemini --url https://gemini.google.com/app

Окно открывается ВИДИМЫМ (headless=False) — вход в Google нельзя сделать
автоматически, и не нужно: человек логинится сам, а профиль сохраняет
сессию. После закрытия окна cookies остаются в профиле и переиспользуются
проверками.

Значения cookies не печатаются никогда — только сводка по доменам.
"""

from __future__ import annotations

import argparse
import sys
import time

from browser import paths, redact, session

#: Адрес по умолчанию для каждого известного сервиса.
DEFAULT_URLS = {
    "gemini": "https://gemini.google.com/app",
    "claude": "https://claude.ai/",
    "chatgpt": "https://chatgpt.com/",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python browser.py login",
        description="Открыть окно браузера для ручного входа в сервис",
    )
    parser.add_argument("service", help="имя сервиса, например gemini")
    parser.add_argument("--url", default=None, help="адрес (по умолчанию известный)")
    parser.add_argument("--proxy", default=None,
                        help="адрес прокси 127.0.0.1:port (если логин нужен через сервер)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="сколько секунд ждать закрытия окна (по умолчанию 600)")
    args = parser.parse_args(argv)

    url = args.url or DEFAULT_URLS.get(args.service.strip().lower())
    if not url:
        print("Не знаю адрес для %r — укажите --url" % args.service, file=sys.stderr)
        return 2

    profile = paths.profile_dir(args.service)
    print("Сервис : %s" % args.service)
    print("Профиль: %s" % profile)
    print("Адрес  : %s" % url)
    print()
    print("Откроется окно браузера. Войдите в аккаунт, убедитесь, что")
    print("страница открылась, и ЗАКРОЙТЕ окно — сессия сохранится.")
    print()

    started = time.time()
    with session.open_session(args.service, proxy=args.proxy,
                              headless=False, log=print) as (ctx, page):
        context = ctx
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001 — сеть может не пустить
            print("Не удалось открыть страницу: %s" % exc)

        # Ждём, пока человек закроет окно или выйдет таймаут.
        while time.time() - started < args.timeout:
            try:
                if not context.pages:
                    break
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001 — окно закрыто
                break

        # Сводка БЕЗ значений: сколько cookies и по каким доменам.
        try:
            summary = redact.describe_cookies(context.cookies())
        except Exception:  # noqa: BLE001 — контекст уже мог закрыться
            summary = "cookies: (не удалось прочитать)"

    print()
    print("Сессия сохранена: %s" % summary)
    print("Профиль: %s" % profile)
    print()
    print("Проверить: python -m browser.check %s --proxy 127.0.0.1:PORT" % args.service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
