"""Куда уходят посчитанные вердикты.

Два приёмника с одинаковым контрактом:

  * DirectSink  — обычный прогон: пишем в базу сразу, очередь не нужна;
  * QueuedSink  — режим служб: кладём порцию в result_queue, а запись в
    базу делает отдельная служба.

Выбор делается при запуске, а не внутри проверки: проверка не знает,
работает она одна или в паре со службой записи.
"""

from __future__ import annotations

from typing import Any, Sequence

from pipeline.logging_setup import get_logger

LOGGER = get_logger("sinks")


class DirectSink:
    """Запись в базу сразу — так работает python -m pipeline run."""

    name = "direct"

    def __init__(self, db) -> None:
        self.db = db

    async def submit(self, *, rows: Sequence[dict], retry: Sequence[str],
                     unparsable: Sequence[str], items: Sequence[Any]) -> None:
        if rows:
            await self.db.record_results(list(rows))
        if retry:
            # Молчавшие серверы не «мёртвые» — возвращаем их в очередь.
            await self.db.enqueue(list(retry), source="retry")
        if unparsable:
            await self.db.blacklist_unparsable(list(unparsable))
        if items:
            from pipeline.database import _key_of

            # Строки, ушедшие на повтор, закрывать как сделанные нельзя —
            # они уже стоят в очереди заново, иначе повтор пропадёт.
            skip = {_key_of(line) for line in retry}
            await self.db.complete_batch(list(items), skip_keys=skip)


class QueuedSink:
    """Кладёт порцию в очередь результатов — так работают службы."""

    name = "queued"

    def __init__(self, db) -> None:
        self.db = db
        self.submitted = 0

    async def submit(self, *, rows: Sequence[dict], retry: Sequence[str],
                     unparsable: Sequence[str], items: Sequence[Any]) -> None:
        if not (rows or retry or unparsable or items):
            return
        payload = {
            "rows": list(rows),
            "retry": list(retry),
            "unparsable": list(unparsable),
            "completed": [item.key for item in items],
        }
        await self.db.push_result(payload)
        self.submitted += 1
        LOGGER.info(
            "Вердикты ждут записи: %d записей, %d на повтор, снять с работы %d",
            len(rows), len(retry), len(items),
        )
