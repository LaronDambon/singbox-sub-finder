"""Служба выгрузки: раз в N минут достаёт свежие рабочие серверы.

Отдельная служба, чтобы не ждать конца проверки. Раз в полчаса (по
настройке) она берёт из базы серверы по фильтрам — живые, свежие, с
нужными тэгами — и обновляет whitelist, а дальше деплой.

По умолчанию деплой выключен: выгрузка обновляет списки, но не публикует
их, пока это не разрешено явно.
"""

from __future__ import annotations

import os
import time

from config.env import (
    SERVICE_PUBLISHER,
    SERVICE_PUBLISHER_DEPLOY,
    SERVICE_PUBLISHER_INTERVAL,
)
from pipeline.logging_setup import get_logger
from pipeline.services.base import Service

LOGGER = get_logger("svc.publisher")


class PublisherService(Service):
    """Периодически обновляет списки серверов из базы."""

    name = "publisher"

    def __init__(self, ctx, *, interval: float | None = None,
                 deploy: bool | None = None) -> None:
        super().__init__(ctx)
        self.interval = SERVICE_PUBLISHER_INTERVAL if interval is None else interval
        self.deploy = SERVICE_PUBLISHER_DEPLOY if deploy is None else deploy
        self._stage = None
        self.publishes = 0

    async def setup(self) -> None:
        from pipeline.stages.export import ExportStage

        self._stage = ExportStage(self.ctx)
        self.enabled = SERVICE_PUBLISHER
        if self.enabled:
            LOGGER.info(
                "Публикатор: каждые %.0f сек, деплой %s",
                self.interval or 0, "включён" if self.deploy else "выключен",
            )

    async def run_once(self) -> str:
        if not self.enabled or self._stage is None:
            return "выключена"
        started = time.monotonic()
        report = await self._stage.run(self.db, deploy=self.deploy)
        self.publishes += 1
        wl = report.get("whitelist", 0)
        return (
            f"выгрузка #{self.publishes}: whitelist {wl} "
            f"({time.monotonic() - started:.1f}s)"
        )
