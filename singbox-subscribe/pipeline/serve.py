"""Запуск всех служб как одного долгоживущего процесса.

Службы не вызывают друг друга. Они общаются только через очереди в базе,
поэтому порядок запуска не важен: discovery может начать докладывать
серверы раньше, чем проснётся checker, и это ничего не ломает — серверы
просто подождут в очереди.

    python -m pipeline serve

Остановка — Ctrl+C: надзорник гасит службы по одной, давая дописать
последний батч и применять вердикты, которые уже в очереди результатов.
"""

from __future__ import annotations

import asyncio

from config.settings import init_settings, setting
from pipeline.checkers.registry import build as build_checkers
from pipeline.context import PipelineContext, default_settings
from pipeline.database import QUEUE_FAILED, QUEUE_IN_PROGRESS, Database
from pipeline.logging_setup import get_logger, setup_logging
from pipeline.services.base import Service, Supervisor
from pipeline.services.checker import CheckerService
from pipeline.services.collector import CollectorService
from pipeline.services.discovery import DiscoveryService
from pipeline.services.publisher import PublisherService
from pipeline.services.results import ResultsService

LOGGER = get_logger("serve")

#: Порядок только для читаемого лога старта; службы независимы.
SERVICE_ORDER = ("discovery", "collector", "checker", "results", "publisher")


def build_services(ctx: "PipelineContext") -> list[Service]:
    """Собирает список служб. Ни одна не знает о существовании остальных."""
    services: list[Service] = [
        DiscoveryService(ctx),
        CollectorService(ctx),
        CheckerService(ctx),
        ResultsService(ctx),
        PublisherService(ctx),
    ]
    order = {name: i for i, name in enumerate(SERVICE_ORDER)}
    services.sort(key=lambda s: order.get(s.name, 99))
    return services


async def serve(
    *,
    db_path=None,
    checkers=None,
    settings=None,
    duration: float | None = None,
) -> None:
    """Поднимает службы и держит их, пока не остановят или не выйдет duration."""
    # Настройки собираем здесь, явно и до всего остального: дальше их
    # читают и логирование, и сборка чекеров, и службы.
    init_settings()
    setup_logging()
    # Настройки берём те же, что и обычный прогон: иначе этапы получат
    # пустые пути (urls_file, whitelist_file) и упадут на первом же проходе.
    resolved = default_settings().merged(settings)
    if checkers:
        resolved["checkers"] = checkers
    built = build_checkers(resolved.get("checkers") or setting("PIPELINE_CHECKERS"),
                           custom_dir=setting("PIPELINE_CUSTOM_CHECKERS_DIR"))
    db = Database(db_path) if db_path else Database()
    ctx = PipelineContext(settings=resolved, db=db, checkers=built, logger=LOGGER)

    if setting("PIPELINE_QUEUE_PERSIST"):
        # Процесс мог быть убит посреди батча — без этого строки залипли бы
        # в in_progress навсегда.
        requeued = await db.requeue_stale(statuses=(QUEUE_IN_PROGRESS, QUEUE_FAILED))
        if requeued:
            LOGGER.info("В очередь возвращено незавершённых записей: %d", requeued)

    services = build_services(ctx)
    supervisor = Supervisor(services)

    LOGGER.info("=" * 66)
    LOGGER.info("Режим служб. Чекеры: %s", ", ".join(c.name for c in built) or "(нет)")
    for svc in services:
        period = "без остановки" if svc.interval is None else f"каждые {svc.interval:.0f} с"
        LOGGER.info("  %-11s %s", svc.name, period)
    LOGGER.info("=" * 66)

    supervisor.start()
    try:
        if duration:
            await asyncio.sleep(duration)
            LOGGER.info("Отработано %.0f с, останавливаюсь", duration)
        else:
            # Ждём, пока кто-то из служб не упадёт насмерть, либо Ctrl+C.
            while True:
                await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        LOGGER.info("Остановка по Ctrl+C")
    finally:
        await supervisor.stop_all()
        for checker in built:
            try:
                await checker.teardown()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Завершение чекера %s не удалось: %s", checker.name, exc)
        await db.close()
        LOGGER.info("Остановлено")
