"""Служба проверки: берёт батчи из очереди и гоняет sing-box без остановки.

Работает постоянно. Пока в очереди есть серверы, берутся батчи и
проверяются; когда очередь пуста, служба не завершается, а ждёт: новые
серверы положит туда и поиск, и сборщик батчей, независимо от неё.

Считает вердикты и НЕ пишет их в таблицу серверов: результаты уходят в
очередь result_queue, откуда их забирает служба записи. Поэтому долгая
запись в базу не подвешивает проверку, а свежие серверы появляются в базе
сразу после подтверждения вердикта.
"""

from __future__ import annotations

from config.settings import get_settings
from pipeline.logging_setup import get_logger
from pipeline.services.base import Service

LOGGER = get_logger("svc.checker")


class CheckerService(Service):
    """Бесконечный цикл проверки батчей из очереди."""

    name = "checker"

    def __init__(self, ctx, checkers=None) -> None:
        super().__init__(ctx)
        self._checkers = checkers if checkers is not None else ctx.checkers
        # None -> без пауз между проходами: сам CheckStage выбирает батчи,
        # пока очередь не опустеет, а базовый цикл ждёт только при простое.
        self.interval = None
        self._stage = None

    async def setup(self) -> None:
        from pipeline.services.sinks import QueuedSink
        from pipeline.stages.check import CheckStage

        self.enabled = get_settings().services.checker and bool(self._checkers)
        self._stage = CheckStage(self.ctx, self._checkers, sink=QueuedSink(self.db))
        if self.enabled:
            LOGGER.info(
                "Проверка: батч до %d, %d процессов sing-box, вердикты -> очередь",
                self._stage.batch_size, self._stage.workers_count,
            )
        else:
            LOGGER.warning("Служба проверки выключена или не выбрано ни одного чекера")

    def wants_work(self) -> bool:
        """После разбора батчей ждём новых серверов, а не крутимся впустую."""
        return False

    async def run_once(self) -> str:
        if not self.enabled or self._stage is None:
            return "выключена"
        report = await self._stage.run()
        return (
            f"батчей {report.get('batches', 0)}, "
            f"живых {report.get('ok', 0)}, мёртвых {report.get('failed', 0)}, "
            f"обогащено {report.get('enriched', 0)}"
        )
