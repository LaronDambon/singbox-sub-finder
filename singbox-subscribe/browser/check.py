"""Проверка сервиса через конкретный сервер (прокси sing-box).

Запуск:

    python -m browser.check gemini --proxy 127.0.0.1:51234

Что делает: открывает страницу сервиса в сохранённом профиле (то есть
ИЗ-ПОД ВАШЕЙ СЕССИИ), но через прокси указанного сервера. Так видно ровно
то, что увидит человек, выбравший этот сервер.

Результат: ok / blocked / unknown + причина. Значения cookies не печатаются.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from browser import redact, services, session

VERDICT_OK = "ok"
VERDICT_BLOCKED = "blocked"
VERDICT_UNKNOWN = "unknown"


def check_service(service_name: str, *, proxy: str | None = None,
                  headless: bool = True, log=None) -> dict:
    """Открывает сервис через прокси и выносит вердикт.

    Возвращает {service, verdict, reason, url, elapsed, cookies}.
    Значения cookies НЕ возвращаются — только сводка.
    """
    spec = services.get_service(service_name)
    started = time.time()
    verdict, reason, final_url = VERDICT_UNKNOWN, "", ""

    with session.open_session(service_name, proxy=proxy,
                              headless=headless, log=log) as (ctx, page):
        try:
            page.goto(spec.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            return {
                "service": spec.name, "verdict": VERDICT_UNKNOWN,
                "reason": "не открылось: %s" % type(exc).__name__,
                "url": spec.url, "elapsed": round(time.time() - started, 1),
                "cookies": "",
            }

        # Ждём, пока SPA отрисуется: заглушка и приложение появляются не сразу.
        deadline = time.time() + spec.wait_seconds
        text = ""
        while time.time() < deadline:
            try:
                text = page.inner_text("body") or ""
            except Exception:  # noqa: BLE001 — страница могла перезагрузиться
                text = ""
            low = text.lower()
            if any(m.lower() in low for m in spec.blocked_markers):
                break
            if sum(1 for m in spec.ok_markers if m.lower() in low) >= spec.ok_threshold:
                break
            page.wait_for_timeout(1000)

        low = text.lower()
        hit_block = next((m for m in spec.blocked_markers if m.lower() in low), None)
        ok_hits = [m for m in spec.ok_markers if m.lower() in low]

        try:
            final_url = page.url
        except Exception:  # noqa: BLE001
            final_url = spec.url

        if hit_block:
            verdict, reason = VERDICT_BLOCKED, "найдена заглушка: %s" % hit_block
        elif len(ok_hits) >= spec.ok_threshold:
            verdict, reason = VERDICT_OK, "приложение отрисовалось (%s)" % ", ".join(ok_hits[:3])
        else:
            verdict = VERDICT_UNKNOWN
            preview = " ".join(text.split())[:120]
            reason = "ни заглушки, ни приложения; начало страницы: %s" % preview

        try:
            cookies = redact.describe_cookies(ctx.cookies())
        except Exception:  # noqa: BLE001
            cookies = ""

    return {
        "service": spec.name,
        "verdict": verdict,
        "reason": reason,
        "url": final_url,
        "elapsed": round(time.time() - started, 1),
        "cookies": cookies,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python browser.py check",
        description="Проверить сервис через прокси сервера",
    )
    parser.add_argument("service")
    parser.add_argument("--proxy", default=None, help="127.0.0.1:port")
    parser.add_argument("--show", action="store_true", help="показать окно браузера")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args(argv)

    result = check_service(args.service, proxy=args.proxy,
                           headless=not args.show,
                           log=(None if args.json else print))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print()
        print("Сервис : %s" % result["service"])
        print("Вердикт: %s" % result["verdict"])
        print("Причина: %s" % result["reason"])
        print("Время  : %ss" % result["elapsed"])
    return 0 if result["verdict"] == VERDICT_OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
