"""Служба сборки батчей: достаёт живые серверы из базы и ставит в очередь.

Это ровно та служба, про которую вы говорили: «одна парсит базу данных и
собирает батчи на проверку». Она берёт из таблицы серверов записи по
фильтрам (excluded=0 и stable не ниже порога) и отправляет их в очередь
проверки.

Работает на своих часах и не ждёт ни поиска, ни проверки: подписки могут
обновляться раз в час, а очередь пополняется постоянно, и проверка не
простаивает, пока база пуста.
"""

from __future__ import annotations

import os

from config.env import SERVICE_COLLECTOR, SERVICE_COLLECTOR_INTERVAL
from pipeline.logging_setup import get_logger
from pipeline.services.base import Service

LOGGER = get_logger("svc.collector")


class CollectorService(Service):
    """Пополняет очередь проверки серверами из пула базы."""

    name = "collector"

    def __init__(self, ctx, *, interval: float | None = None,
                 enabled: bool | None = None) -> None:
        super().__init__(ctx)
        self.interval = SERVICE_COLLECTOR_INTERVAL if interval is None else interval
        self.added = 0
        self._enabled = SERVICE_COLLECTOR if enabled is None else bool(enabled)

    async def setup(self) -> None:
        self.enabled = self._enabled
        if self.enabled:
            LOGGER.info("Сборщик батчей: каждые %.0f сек", self.interval or 0)

    async def run_once(self) -> str:
        if not self.enabled:
            return "выключена"
        added = await self.db.refill_from_pool()
        self.added += added
        if added:
            LOGGER.info("Сборщик: в очередь добавлено серверов из базы: %d", added)
        return f"в очередь добавлено {added}" if added else "очередь полная"
