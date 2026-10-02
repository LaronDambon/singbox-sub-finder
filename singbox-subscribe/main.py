#!/usr/bin/env python3
"""Точка входа. Загружает ядро pipeline и вызывает его функции.

Здесь НЕТ логики проверки серверов и нет работы с базой — всё это живёт в
ядре ``singbox-subscribe/pipeline``. Файл делает три вещи:

  1. настраивает логирование;
  2. собирает ``Pipeline`` с нужными настройками;
  3. запускает прогон и печатает итог.

Вся настройка — переменные окружения (`.env`), см. `.env.example`.

Запуск:

    python singbox-subscribe/main.py                  # полный цикл
    python singbox-subscribe/main.py --checkers url_probe,country
    python singbox-subscribe/main.py --no-discovery   # только проверка и экспорт
    python singbox-subscribe/main.py --interval 1800  # крутить цикл каждые 30 мин

Отдельные стадии ядра доступны напрямую:

    python -m pipeline run | check | discover | export | checkers | status | queue | logs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import Pipeline, get_logger, setup_logging

LOGGER = get_logger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Один цикл pipeline: поиск серверов -> очередь -> проверка -> экспорт",
    )
    parser.add_argument(
        "--checkers", default=None,
        help="проверяющие алгоритмы через запятую (перекрывает PIPELINE_CHECKERS)",
    )
    parser.add_argument(
        "--no-discovery", action="store_true",
        help="не искать новые серверы из подписок, только проверить очередь",
    )
    parser.add_argument(
        "--no-export", action="store_true",
        help="не обновлять whitelist.txt / blacklist.txt / merge.txt",
    )
    parser.add_argument(
        "--batch-size", type=int, default=None, help="серверов в одном запуске sing-box",
    )
    parser.add_argument(
        "--workers", type=int, default=None, help="сколько батчей проверять одновременно",
    )
    parser.add_argument(
        "--interval", type=float, default=0.0,
        help="если > 0 — повторять цикл с такой паузой в секундах (0 = один раз)",
    )
    parser.add_argument("--json", action="store_true", help="печатать отчёт в JSON")
    return parser


def settings_from_args(args: argparse.Namespace) -> dict:
    """Перекрытия настроек из аргументов командной строки."""
    overrides = {
        "discovery": False if args.no_discovery else None,
        "write_merge": False if args.no_export else None,
        "export_lists": False if args.no_export else None,
        "batch_size": args.batch_size,
        "check_workers": args.workers,
    }
    return {k: v for k, v in overrides.items() if v is not None}


async def run_once(pipeline: Pipeline, *, discovery: bool, export: bool) -> dict:
    """Один полный прогон ядра."""
    return await pipeline.run(discovery=discovery, export=export)


async def run_forever(pipeline: Pipeline, *, interval: float, discovery: bool,
                      export: bool) -> None:
    """Крутит цикл с паузой. Ошибка цикла не убивает процесс."""
    from pipeline.logging_setup import log_throttle_summary

    while True:
        try:
            report = await run_once(pipeline, discovery=discovery, export=export)
            if not report.get("ok"):
                LOGGER.error("Цикл завершился с ошибкой: %s", report.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сервис не должен падать из-за цикла
            LOGGER.exception("Непойманная ошибка цикла — продолжаю")
        log_throttle_summary(LOGGER)
        LOGGER.info("Следующий цикл через %.0f с", interval)
        await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    LOGGER.info("Запуск main.py (логирование настроено)")

    async def amain() -> int:
        async with Pipeline(
            settings=settings_from_args(args), checkers=args.checkers,
        ) as pipeline:
            if args.json:
                report = await run_once(
                    pipeline, discovery=not args.no_discovery, export=not args.no_export,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
                return 0 if report.get("ok") else 1

            if args.interval > 0:
                LOGGER.info("Режим постоянной работы: цикл каждые %.0f с", args.interval)
                await run_forever(
                    pipeline, interval=args.interval,
                    discovery=not args.no_discovery, export=not args.no_export,
                )
                return 0

            report = await run_once(
                pipeline, discovery=not args.no_discovery, export=not args.no_export,
            )
            return 0 if report.get("ok") else 1

    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        LOGGER.info("Остановлено пользователем (Ctrl+C)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
