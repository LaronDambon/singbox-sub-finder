"""Запуск браузера и работа с профилем сервиса.

Один сервис = один persistent-контекст Playwright. Профиль лежит вне
репозитория (см. paths.py), поэтому cookies не могут уехать в git.

Проверки сервисов идут ЧЕРЕЗ ПРОКСИ конкретного сервера: sing-box уже
поднимает каждый сервер как локальный HTTP-прокси на 127.0.0.1:port.
Так проверка видит то же, что увидел бы человек с этого сервера, но
из-под сохранённой сессии.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

from browser import paths, redact

#: Разумные размеры окна: страница должна рендериться как у человека.
VIEWPORT = {"width": 1280, "height": 900}

#: Аргументы, отключающие автоматизационные маркеры Chromium.
#: Без них Google отдаёт другую разметку и ловит бота.
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]


class BrowserUnavailable(RuntimeError):
    """Playwright или браузер не установлены."""


def _require_playwright():
    """Импортирует Playwright или объясняет, что установить."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise BrowserUnavailable(
            "Playwright не установлен. Выполните:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium"
        ) from exc


def proxy_for(host_port: str) -> dict:
    """Прокси-настройка Playwright для адреса вида 127.0.0.1:1234."""
    addr = str(host_port).strip()
    if not addr:
        raise ValueError("Пустой адрес прокси")
    if not addr.startswith("http://") and not addr.startswith("socks"):
        addr = "http://" + addr
    return {"server": addr}


@contextlib.contextmanager
def open_session(service: str, *, proxy: str | None = None,
                 headless: bool = True, log=None) -> Iterator:
    """Открывает persistent-контекст сервиса. Возвращает (context, page).

    headless=False нужен только для ручного логина: человек должен видеть
    окно. В проверках всегда headless=True.
    """
    _require_playwright()
    from playwright.sync_api import sync_playwright

    profile = paths.ensure_dirs(service)
    paths.assert_outside_repo(profile)

    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    say("Профиль: %s" % profile)
    with sync_playwright() as pw:
        launch: dict = {
            "user_data_dir": str(profile),
            "headless": headless,
            "args": list(STEALTH_ARGS),
            "viewport": VIEWPORT,
            "locale": "en-US",
        }
        if proxy:
            launch["proxy"] = proxy_for(proxy)
            say("Прокси: %s" % proxy)
        ctx = pw.chromium.launch_persistent_context(**launch)
        try:
            # Скрываем самый очевидный признак автоматизации.
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield ctx, page
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def session_summary(service: str) -> str:
    """Что известно о сессии сервиса — БЕЗ значений cookies.

    Возвращает безопасную строку для лога и чата.
    """
    profile = paths.profile_dir(service)
    if not profile.exists():
        return "%s: профиля нет (нужен логин)" % service
    size = sum(f.stat().st_size for f in profile.rglob("*") if f.is_file())
    return "%s: профиль %s (%.1f МБ)" % (service, profile, size / 1024 / 1024)


def cookies_summary(service: str, *, proxy: str | None = None) -> str:
    """Сводка cookies БЕЗ значений: сколько и по каким доменам."""
    with open_session(service, proxy=proxy, headless=True) as (ctx, _page):
        cookies = ctx.cookies()
        return redact.describe_cookies(cookies)
