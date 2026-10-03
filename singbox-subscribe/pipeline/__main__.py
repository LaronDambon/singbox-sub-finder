"""CLI ядра: python -m pipeline <команда>.

    run       полный прогон (поиск -> очередь -> проверка -> экспорт)
    serve     режим служб: все этапы крутятся постоянно и общаются через очереди
    check     только проверка (очередь наполняется из базы)
    discover  только поиск новых серверов из подписок
    export    только экспорт списков из базы
    checkers  список доступных проверяющих алгоритмов
    status    состояние базы и очереди (JSON)
    queue     счётчики очереди (JSON)
    logs      где лежат логи и сколько они занимают
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from config.settings import get_settings, init_settings
from pipeline.logging_setup import get_logger, setup_logging


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _cmd_serve(args) -> int:
    from pipeline.serve import serve

    async def main():
        await serve(
            db_path=args.db,
            checkers=args.checkers,
            duration=args.duration,
        )
        return 0

    return asyncio.run(main())


def _cmd_run(args) -> int:
    from pipeline.engine import Pipeline

    async def main():
        async with Pipeline(checkers=args.checkers, db_path=args.db) as pipeline:
            report = await pipeline.run(
                discovery=not args.no_discovery, export=not args.no_export,
            )
            if args.json:
                _dump(report)
            return 0 if report.get("ok") else 1

    return asyncio.run(main())


def _cmd_check(args) -> int:
    from pipeline.engine import Pipeline

    async def main():
        async with Pipeline(checkers=args.checkers, db_path=args.db) as pipeline:
            await pipeline.fill_queue()
            report = await pipeline.check()
            _dump(report)
            return 0

    return asyncio.run(main())


def _cmd_discover(args) -> int:
    from pipeline.engine import Pipeline

    async def main():
        async with Pipeline(checkers=args.checkers, db_path=args.db) as pipeline:
            report = await pipeline.discover()
            _dump(report)
            return 0 if report.get("ok") else 1

    return asyncio.run(main())


def _cmd_export(args) -> int:
    from pipeline.engine import Pipeline

    async def main():
        async with Pipeline(checkers=args.checkers, db_path=args.db) as pipeline:
            report = await pipeline.export()
            _dump(report)
            return 0

    return asyncio.run(main())


def _cmd_checkers(args) -> int:
    from pipeline.checkers import build_checkers, describe

    if args.verbose:
        _dump(describe())
        return 0

    active = {c.name for c in build_checkers()}
    print(f"{'ИМЯ':<18} {'РОЛЬ':<10} ОПИСАНИЕ")
    print("-" * 72)
    for item in describe():
        role = "решает" if item["decides_availability"] else "дополняет"
        mark = "*" if item["name"] in active else " "
        print(f"{mark}{item['name']:<17} {role:<10} {item['description']}")
    print()
    print(f"* — включён в PIPELINE_CHECKERS={get_settings().pipeline.checkers!r}")
    return 0


def _cmd_status(args) -> int:
    from pipeline.engine import Pipeline

    async def main():
        async with Pipeline(checkers=args.checkers, db_path=args.db) as pipeline:
            _dump(await pipeline.status())
            return 0

    return asyncio.run(main())


def _cmd_queue(args) -> int:
    from pipeline.database import Database

    async def main():
        db = Database(args.db or get_settings().paths.servers_db_file)
        try:
            stats = await db.queue_stats()
            if args.clear_done:
                removed = await db.clear_queue()
                print(f"Очередь очищена, удалено записей: {removed}")
                return 0
            _dump(stats.as_dict())
            return 0
        finally:
            await db.close()

    return asyncio.run(main())


def _cmd_logs(args) -> int:
    # Каталог нужен трижды подряд — читаем один раз, локально.
    log_dir = get_settings().log.dir_path

    print(f"Каталог:        {log_dir}")
    print(f"Уровень:        {logging.getLevelName(get_settings().log.level)}")
    print(f"Ротация:        {get_settings().log.max_bytes} байт × {get_settings().log.backup_count} копий")
    print(f"Очистка:        файлы старше {get_settings().log.retention_days} дней")
    print()
    total = 0
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.log*")):
            size = path.stat().st_size
            total += size
            print(f"  {path.name:<24} {size / 1024 / 1024:>8.2f} МБ")
    print(f"  {'ИТОГО':<24} {total / 1024 / 1024:>8.2f} МБ")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="Ядро pipeline: база, очередь проверки, чекеры",
    )
    parser.add_argument("--db", default=None, help="путь к servers.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, handler, help_text: str, *, with_json: bool = True, extra=None):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--checkers", default=None, help="список чекеров через запятую")
        p.set_defaults(func=handler)
        if extra:
            extra(p)
        if with_json:
            p.add_argument("--json", action="store_true", help="печатать отчёт в JSON")
        return p

    add("run", _cmd_run, "полный прогон",
        extra=lambda p: (
            p.add_argument("--no-discovery", action="store_true",
                           help="не искать новые серверы из подписок"),
            p.add_argument("--no-export", action="store_true",
                           help="не обновлять whitelist/blacklist/merge"),
        ))
    add("serve", _cmd_serve, "режим служб: всё крутится постоянно", with_json=False,
        extra=lambda p: p.add_argument(
            "--duration", type=float, default=None,
            help="отработать N секунд и выйти (для проверки и тестов)",
        ))
    add("check", _cmd_check, "только проверка (очередь берётся из базы)")
    add("discover", _cmd_discover, "только поиск новых серверов из подписок")
    add("export", _cmd_export, "только экспорт списков из базы")
    add("status", _cmd_status, "состояние базы и очереди", with_json=False)
    add("checkers", _cmd_checkers, "список проверяющих алгоритмов", with_json=False,
        extra=lambda p: p.add_argument("-v", "--verbose", action="store_true"))
    add("queue", _cmd_queue, "счётчики очереди", with_json=False,
        extra=lambda p: p.add_argument(
            "--clear-done", action="store_true",
            help="удалить обработанные записи очереди",
        ))
    add("logs", _cmd_logs, "где лежат логи и сколько они занимают", with_json=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Точка входа: настройки собираем явно и до всего остального — дальше
    # их читают и разбор аргументов, и логирование, и команды.
    init_settings()
    setup_logging()
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
