"""Служба записи: забирает вердикты и переносит их в таблицу серверов.

Проверка не пишет в базу сама. Как только sing-box ответил, вердикт уже
лежит в очереди результатов, а эта служба забирает и переносит его:
живой — в живые, мёртвый — в мёртвые, молчавший — обратно в очередь на
повтор.

Так проверка не ждёт записи и не встаёт колом, когда база занята, а
выгрузка свежих серверов получает данные сразу после появления.
"""

from __future__ import annotations

import asyncio
import os

from config.env import SERVICE_RESULTS, SERVICE_RESULTS_INTERVAL
from pipeline.logging_setup import get_logger
from pipeline.services.base import Service

LOGGER = get_logger("svc.results")


class ResultsService(Service):
    """Разбирает очередь вердиктов и применяет их к серверам."""

    name = "results"

    def __init__(self, ctx, *, interval: float | None = None,
                 limit: int = 32) -> None:
        super().__init__(ctx)
        # Пауза между разборами; если очередь пуста, ждём этой же паузы.
        self.interval = SERVICE_RESULTS_INTERVAL if interval is None else interval
        self.limit = limit
        self.applied = 0
        self.alive = 0
        self.dead = 0

    async def setup(self) -> None:
        self.enabled = SERVICE_RESULTS
        if self.enabled:
            LOGGER.info("Запись результатов: каждые %.0f сек", self.interval or 0)

    def wants_work(self) -> bool:
        """Не спать, если очередь вердиктов не пуста."""
        return False

    async def run_once(self) -> str:
        if not self.enabled:
            return "выключена"
        batches = await self.db.claim_results(self.limit)
        if not batches:
            return ""
        failed = 0
        for batch in batches:
            try:
                await self.db.apply_result(batch)
            except Exception as exc:  # noqa: BLE001
                # Порция помечается неприменённой и больше не перебирается:
                # иначе одна битая порция крутилась бы в очереди вечно.
                LOGGER.exception("Порция вердиктов #%d не применилась", batch.id)
                await self.db.mark_result_failed(batch.id, f"{type(exc).__name__}: {exc}")
                failed += 1
                continue
            self.applied += 1
            for row in batch.rows:
                if row.get("available"):
                    self.alive += 1
                elif row.get("available") is False:
                    self.dead += 1
        text = (
            f"применено порций {len(batches) - failed} "
            f"(живых {self.alive}, мёртвых {self.dead})"
        )
        if failed:
            text += f", с ошибкой {failed}"
        return text
