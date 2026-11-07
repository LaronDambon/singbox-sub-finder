"""Команда refine: проверить все серверы с меткой сервиса.

Отдельный модуль, а не main() внутри refine.py: сам refine — библиотека
(её вызывает чекер конвейера), и держать в ней разбор аргументов командной
строки незачем. Здесь только CLI.

Запуск: python browser.py refine gemini [--limit N] [--pause S] [--clear-cache]
"""

from __future__ import annotations

import argparse
import sys

from browser import cache as bcache
from browser import paths, refine


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python browser.py refine",
        description="Проверить браузером все серверы с меткой сервиса",
    )
    parser.add_argument("service", nargs="?", default="gemini")
    parser.add_argument("--tag", default=None,
                        help="метка в БД (по умолчанию имя сервиса)")
    parser.add_argument("--limit", type=int, default=None,
                        help="проверить не больше N серверов")
    parser.add_argument("--pause", type=float, default=refine.DEFAULT_PAUSE,
                        help="пауза между серверами, сек")
    parser.add_argument("--clear-cache", action="store_true",
                        help="забыть прошлые вердикты (куки входа не трогаются)")
    parser.add_argument("--ttl-hours", type=float, default=6.0,
                        help="срок жизни кэша")
    args = parser.parse_args(argv)

    from config.settings import get_settings
    from script.server_store import ServerStore
    from browser.db_cache import DbVerdictCache

    store = ServerStore(get_settings().paths.servers_db_file)
    cache = DbVerdictCache(args.service, store=store)

    if args.clear_cache:
        # Чистим только вердикты БРАУЗЕРА по этому сервису. Куки входа лежат
        # в профиле и не трогаются: это разные вещи.
        with store._connect() as conn:  # noqa: SLF001 — свой же класс
            cur = conn.execute(
                "DELETE FROM service_bans WHERE service = ? AND checked_by = 'browser'",
                (args.service,),
            )
            print("Удалено вердиктов браузера: %d" % (cur.rowcount or 0))
        cache = DbVerdictCache(args.service, store=store)

    print("Проверка из-под сессии. Логин не трогаем, чистим только вердикты.")
    print()

    results = refine.refine_by_tag(
        args.service, store=store, tag=args.tag, limit=args.limit,
        pause=args.pause, cache=cache, workers=1, log=print,
    )

    from collections import Counter
    counts = Counter(r.get("verdict") for r in results.values())
    print()
    print("Итог: %d серверов — %s" % (len(results), dict(counts)))
    ok = [l for l, r in results.items() if r.get("verdict") == "ok"]
    banned = [l for l, r in results.items() if r.get("verdict") == "blocked"]
    print("  работает : %d" % len(ok))
    print("  заглушка : %d" % len(banned))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
