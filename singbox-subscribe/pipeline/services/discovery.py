"""Служба поиска: подписки -> новые серверы -> очередь проверки.

Как только список обновился и серверы нашлись, они сразу оказываются в
очереди — их тут же подхватит служба проверки. Никакого ожидания «начала
следующего прогона».
"""

from __future__ import annotations

from config.settings import get_settings
from pipeline.logging_setup import get_logger
from pipeline.services.base import Service

LOGGER = get_logger("svc.discovery")


class DiscoveryService(Service):
    """Периодически скачивает подписки и кладёт новые серверы в очередь."""

    name = "discovery"

    def __init__(self, ctx, *, interval: float | None = None) -> None:
        super().__init__(ctx)
        self.interval = (
            get_settings().services.discovery_interval if interval is None else interval
        )
        self._stage = None

    async def setup(self) -> None:
        from pipeline.stages.discovery import DiscoveryStage

        self._stage = DiscoveryStage(self.ctx)
        self.enabled = bool(
            self.ctx.settings.get("discovery", True)
        ) and get_settings().services.discovery
        if self.enabled:
            LOGGER.info("Поиск: каждые %.0f сек", self.interval or 0)

    async def run_once(self) -> str:
        if not self.enabled or self._stage is None:
            return "выключена"
        report = await self._stage.run(self.db)
        if report.get("ok") is False:
            return f"ошибка: {report.get('error')}"
        return (
            f"новых {report.get('added', 0)}, "
            f"в очереди {report.get('enqueued', 0)}, "
            f"в чёрном списке {report.get('blacklisted', 0)}"
        )
