"""Служба 1: набор серверов из базы и запуск батчей sing-box.

Работает в паре со службой 2 (``pipeline.singbox`` + ``pipeline.collector``):

    ┌── Database ──┐claim_batch()
        │           │   ┌───────────────────────── BatchDispatcher ─────────────┐
        └───────────┘   │ plan_batches() -> [100, 15, 16]                      │
                        │   ├── батч 1: 100 серверов  -> свой конфиг/порт/дедлайн
        ┌── Database ──┐│   ├── батч 2:  15 серверов  -> свой конфиг/порт/дедлайн
        │              │◄┘   └── батч 3:  16 серверов  -> свой конфиг/порт/дедлайн
        └──────────────┘   │ collect -> rows / retry / unparsable
                           └───────────────────────────────────────────────────┘

Разделение исходов важно для базы:

    ответил (ALIVE)     -> available=1, ping, stable растёт
    не ответил (DEAD)   -> available=0, stable падает
    молчал (NO_RESULT)  -> ничего не пишем, сервер возвращается в очередь
    сломан конфиг       -> батч делится пополам, виновник ищется рекурсивно

Последние два случая раньше не отличались от «сервер мёртв» — из-за этого сбой
одного батча выглядел как массовая гибель серверов.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from pipeline.batch import BatchClock, BatchOutcome, BatchReport, plan_batches
from pipeline.collector import Verdict
from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.database import Database, QueueItem

LOGGER = get_logger("dispatcher")

#: stable нового сервера, если его ещё нет в базе.
NEW_SERVER_STABLE_DEFAULT = 5


class DispatchStats:
    """Счёт за прогон службы 1."""

    def __init__(self) -> None:
        self.claimed = 0
        self.batches = 0
        self.alive = 0
        self.dead = 0
        self.retry = 0
        self.dropped = 0
        self.unparsable = 0
        self.config_errors = 0
        self.elapsed = 0.0

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "batches": self.batches,
            "alive": self.alive,
            "dead": self.dead,
            "retry": self.retry,
            "dropped": self.dropped,
            "unparsable": self.unparsable,
            "config_errors": self.config_errors,
            "elapsed": round(self.elapsed, 2),
        }


class BatchDispatcher:
    """Забирает серверы из базы и прогоняет их батчами через sing-box."""

    def __init__(
        self,
        db: "Database | None" = None,
        *,
        batch_size: int = 100,
        slots: int = 2,
        timeout: float = 20.0,
        startup: float = 10.0,
        singbox_path: Path,
        template_path: Path,
        config_dir: Path,
        urltest: str = "",
        min_chunk: int = 8,
        max_split_depth: int = 3,
        max_attempts: int = 2,
        max_retries: int = 3,
    ) -> None:
        self.db = db
        self.batch_size = max(1, batch_size)
        self.slots = max(1, slots)
        self.timeout = timeout
        self.startup = startup
        self.singbox_path = Path(singbox_path)
        self.template_path = Path(template_path)
        self.config_dir = Path(config_dir)
        self.urltest = urltest
        self.min_chunk = max(1, min_chunk)
        self.max_split_depth = max(0, max_split_depth)
        self.max_attempts = max(1, max_attempts)
        self.max_retries = max(0, max_retries)
        self._serial = 0
        self._batch_no = 0
        self.new_server_stable = NEW_SERVER_STABLE_DEFAULT

    # ------------------------------------------------------------------ запуск
    async def run_forever(self, stop: asyncio.Event, *, idle_sleep: float = 0.4) -> DispatchStats:
        """Крутит батчи, пока stop не выставлен и очередь не пуста."""
        stats = DispatchStats()
        started = time.monotonic()
        while not stop.is_set():
            worked = await self.run_once(stats)
            if not worked:
                if stop.is_set():
                    break
                await asyncio.sleep(idle_sleep)
        stats.elapsed = time.monotonic() - started
        return stats

    async def run_once(self, stats: DispatchStats | None = None) -> int:
        """Один проход: набирает серверов и прогоняет их. Возвращает их число."""
        stats = stats if stats is not None else DispatchStats()
        want = self.batch_size * self.slots
        items = await self.db.claim_batch(want)
        if not items:
            return 0
        stats.claimed += len(items)

        reports = await self.run_lines([item.line for item in items])
        await self.persist(reports, stats, items)
        return len(items)

    async def run_lines(self, lines) -> list:
        """Прогоняет список серверов через батчи. Без обращения к базе.

        Общий кусок и для работы из базы, и для плагин-пути «чекер получил
        строки сам» — поведение, разбор и логи у них одинаковые.
        """
        lines = [line for line in lines if line]
        if not lines:
            return []
        sizes = plan_batches(len(lines), self.batch_size, self.slots, min_chunk=self.min_chunk)
        chunks = []
        offset = 0
        for size in sizes:
            chunks.append(lines[offset:offset + size])
            offset += size
        results = await asyncio.gather(
            *(self._run_chunk(chunk) for chunk in chunks), return_exceptions=True,
        )
        flat = []
        for report in results:
            if isinstance(report, BaseException):
                LOGGER.error("Батч упал целиком: %s", report)
                continue
            flat.extend(report)
        return flat

    # ------------------------------------------------------------- один батч
    def _clock(self, size: int) -> "BatchClock":
        """Личный дедлайн батча: постоянная часть на старт + доля размера."""
        return BatchClock(size, base_timeout=self.timeout, batch_size=self.batch_size,
                          min_timeout=self.startup)

    async def _run_chunk(self, lines: Sequence, *, depth: int = 0) -> list[BatchReport]:
        """Прогоняет один батч; при сбое конфига делит его пополам."""
        from pipeline.singbox import run_batch

        lines = list(lines)
        if not lines:
            return []

        clock = self._clock(len(lines))
        self._batch_no += 1
        tag_prefix = f"b{self._batch_no:05d}_"

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            report = await run_batch(
                list(lines),
                index=self._batch_no,
                singbox_path=self.singbox_path,
                template_path=self.template_path,
                clock=clock,
                config_dir=self.config_dir,
                tag_prefix=tag_prefix,
                urltest=self.urltest,
            )
            report.attempts = attempt
            # Сбой запуска (порт занят, битый бинарник) лечится повтором с
            # новым портом — делить батч тут бессмысленно.
            retryable = "порт занят" in report.error or report.outcome is BatchOutcome.SPAWN_ERROR
            if not retryable:
                last_error = report.error
                break
            last_error = report.error
            if attempt < self.max_attempts:
                LOGGER.warning(
                    "Батч %d: %s — повтор %d/%d на другом порту",
                    report.index, report.error, attempt + 1, self.max_attempts,
                )
                clock = self._clock(len(lines))

        if report.outcome is BatchOutcome.CONFIG_ERROR and len(lines) > 1 and depth < self.max_split_depth:
            LOGGER.warning(
                "Батч %d не собрался (%s): делю %d серверов пополам, чтобы найти кривую строку",
                report.index, last_error or report.error, len(lines),
            )
            middle = len(lines) // 2
            left = await self._run_chunk(lines[:middle], depth=depth + 1)
            right = await self._run_chunk(lines[middle:], depth=depth + 1)
            # Всё, что не удалось проверить ни в одной половине, возвращаем
            # в очередь через общий отчёт.
            return left + right

        if report.outcome is BatchOutcome.CONFIG_ERROR and len(lines) == 1:
            # Одна строка, батч из одного сервера не собрался — она кривая.
            report.unparsable = list(report.unparsable) or list(lines)
            report.retry_lines = []

        return [report]

    # ----------------------------------------------------------------- запись
    async def persist(self, reports, stats: "DispatchStats", items=()) -> None:
        """Переносит собранные батчами вердикты в базу.

        Снимает забранные записи с работы: вердикт получен (в том числе
        трёхзначный «не отозвался»), повторять их не нужно. Серверы без
        вердикта уходят обратно в очередь отдельной записью.
        """
        if self.db is None:
            return
        from pipeline.stages.writer import format_line

        stable_map = await self.db.load_stable_map()
        # Сколько раз сервер уже возвращали в очередь без вердикта.
        by_line: dict[str, int] = {}
        for item in items:
            by_line[item.line] = item.attempts
        rows: list[dict] = []
        retry: list[str] = []
        unparsable: list[str] = []
        seen_retry: dict[str, None] = {}

        for report in reports:
            stats.batches += 1
            stats.alive += report.alive
            stats.dead += report.dead
            if report.outcome is BatchOutcome.CONFIG_ERROR:
                stats.config_errors += 1
            unparsable.extend(report.unparsable)

            for line in report.retry_lines:
                seen_retry.setdefault(line, None)
            for line in report.unparsable:
                seen_retry.pop(line, None)

            for tag, verdict in report.verdicts.items():
                raw = report.tag_to_line.get(tag)
                if not raw or verdict.verdict is Verdict.NO_RESULT:
                    continue
                self._serial += 1
                key = _key_of(raw)
                prev = stable_map.get(key, self.new_server_stable)
                from script.server_store import compute_next_state

                new_stable = compute_next_state(prev, ok=verdict.verdict is Verdict.ALIVE)
                stable_map[key] = new_stable
                rows.append({
                    "key": key,
                    "available": verdict.verdict is Verdict.ALIVE,
                    "line": format_line(
                        raw,
                        serial=self._serial,
                        ok=verdict.verdict is Verdict.ALIVE,
                        outcome=_outcome(verdict),
                        stable=new_stable,
                    ),
                    "ping_ms": verdict.ping_ms,
                    "country": "",
                    "protocol": _protocol(raw),
                    "capabilities": "",
                })

        # Не возвращаем в очередь бесконечно: сервер, про который sing-box
        # так и не сказал ни слова, после лимита попыток снимается с проверки.
        exhausted = [line for line in seen_retry if by_line.get(line, 0) >= self.max_retries]
        for line in exhausted:
            seen_retry.pop(line, None)
        retry = list(seen_retry)
        if exhausted:
            stats.dropped += len(exhausted)
            LOGGER.warning(
                "Снято с проверки без вердикта после %d попыток: %d серверов "
                "(sing-box ни разу не ответил про них)",
                self.max_retries, len(exhausted),
            )
        # Страховка от массовой потери базы: если в чёрный список уходит почти
        # весь набор, это почти наверняка поломка разбора, а не битые ссылки.
        # Такие строки не выписываются, а просто возвращаются в очередь.
        if len(items) > 1 and len(unparsable) >= len(items) * 0.9:
            LOGGER.error(
                "НЕ отправляю в чёрный список %d строк из %d: слишком много "
                "неразбираемых, похоже на поломку разбора ссылок. Возвращаю в очередь.",
                len(unparsable), len(items),
            )
            for line in unparsable:
                seen_retry.setdefault(line, None)
            unparsable = []
            retry = list(seen_retry)

        stats.retry += len(retry)
        stats.unparsable += len(unparsable)

        if rows:
            await self.db.record_results(rows)
        if retry:
            # Молчавшие серверы не «мёртвые» — возвращаем их в очередь.
            await self.db.enqueue(retry, source="retry")
        if unparsable:
            await self.db.blacklist_unparsable(unparsable)
        if items:
            # Строки, ушедшие на повтор, закрывать как сделанные нельзя —
            # они уже стоят в очереди заново, иначе повтор пропадёт. Ключи
            # должны быть нормализованы так же, как в самой очереди.
            skip = {_key_of(line) for line in retry}
            await self.db.complete_batch(list(items), skip_keys=skip)

        if rows or retry:
            LOGGER.info(
                "Записано в базу: %d (живых %d, мёртвых %d), в очередь: %d, непригодных: %d",
                len(rows), stats.alive, stats.dead, len(retry), len(unparsable),
            )


def make_dispatcher(
    db: "Database | None" = None,
    *,
    batch_size: int = 100,
    slots: int = 2,
    timeout: float = 20.0,
    startup: float = 10.0,
    urltest: str = "",
    max_attempts: int = 2,
    max_retries: int = 3,
) -> BatchDispatcher:
    """Собирает диспетчер с настройками по умолчанию из конфигурации."""
    from config.env import (
        PIPELINE_BATCH_SIZE,
        PIPELINE_BATCH_STARTUP,
        PIPELINE_CHECK_TIMEOUT,
        PIPELINE_MAX_ATTEMPTS,
        PIPELINE_MAX_RETRIES,
        SING_BOX_OUTPUT_DIR,
        SING_BOX_PATH,
        URLTEST_TEMPLATE,
        URLTEST_URL,
    )

    return BatchDispatcher(
        db,
        batch_size=batch_size or PIPELINE_BATCH_SIZE,
        slots=slots,
        timeout=timeout or PIPELINE_CHECK_TIMEOUT,
        startup=startup or PIPELINE_BATCH_STARTUP,
        max_attempts=max_attempts or PIPELINE_MAX_ATTEMPTS,
        max_retries=max_retries or PIPELINE_MAX_RETRIES,
        singbox_path=Path(SING_BOX_PATH),
        template_path=Path(URLTEST_TEMPLATE),
        config_dir=Path(SING_BOX_OUTPUT_DIR),
        urltest=urltest or URLTEST_URL,
    )


def _key_of(raw: str) -> str:
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(raw)


def _protocol(line: str) -> str:
    from utils import tool

    return (tool.get_protocol(line) or "").lower()


def _outcome(verdict):
    from pipeline.checkers.base import CheckOutcome

    return CheckOutcome(
        ok=verdict.verdict is Verdict.ALIVE,
        ping_ms=verdict.ping_ms,
        detail=verdict.reason,
    )
