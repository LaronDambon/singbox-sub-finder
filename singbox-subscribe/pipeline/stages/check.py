"""Этап проверки: две службы и два прохода.

Служба 1 — диспетчер (pipeline.dispatcher.BatchDispatcher)

    Забирает серверы из базы по фильтрам, режет список на ПАРАЛЛЕЛЬНЫЕ батчи
    (полные по PIPELINE_BATCH_SIZE, а остаток дробится, чтобы не гонять один
    недобранный батч) и запускает по процессу sing-box на каждый. У батча свой
    конфиг, свой свободный порт и свой дедлайн.

Служба 2 — сборщик ответа (pipeline.singbox + pipeline.collector)

    Читает вывод КАЖДОГО запущенного sing-box построчно, на лету разбирает
    ответы и ведёт счёт по батчу. Результат не ждёт конца запуска: батч,
    убитый по таймауту, отдаёт всё, что успел получить.

Два прохода за цикл

  1. Живость. Службы 1 и 2 прогоняют батчи и пишут в базу.
  2. Обогащение. Страна и reachability считаются отдельным проходом только по
     тем, кто выжил: так sing-box не запускается ради мёртвых.

Три состояния сервера

    ответил      -> available=1, пинг, stable растёт;
    не ответил   -> available=0, stable падает;
    не отозвался -> в базу НЕ пишется, сервер возвращается в очередь.

Последнее состояние — то, чего раньше не было. Молчание sing-box (таймаут
батча, сбой сборки конфига) больше не выглядит как смерть сервера: такие
серверы просто проверяются ещё раз. Лимит попыток хранится в очереди
(attempts), поэтому вечного круга нет.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Sequence

from config.env import NEW_SERVER_STABLE, URLTEST_URL
from pipeline.batch import BatchOutcome
from pipeline.checkers.base import CheckContext, CheckResult
from pipeline.collector import Verdict
from pipeline.dispatcher import DispatchStats, make_dispatcher
from pipeline.logging_setup import get_logger
from pipeline.stages.writer import build_rows, count_results

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.context import PipelineContext
    from pipeline.database import Database, QueueItem

LOGGER = get_logger("stages.check")

#: Пауза между опросами пустой очереди, сек.
IDLE_SLEEP = 0.4
#: Сколько раз сервер возвращается в очередь, прежде чем быть снятым с проверки.
MAX_BATCH_RETRIES = 2


class CheckStage:
    """Прогоняет серверы через службы проверки и пишет результаты в базу."""

    name = "check"

    def __init__(self, ctx: "PipelineContext", checkers, sink=None) -> None:
        self.ctx = ctx
        self.db: "Database" = ctx.db
        # Куда уходят посчитанные вердикты. По умолчанию — сразу в базу
        # (обычный прогон); в режиме служб — в очередь result_queue, и
        # записью занимается отдельная служба.
        if sink is None:
            from pipeline.services.sinks import DirectSink

            sink = DirectSink(self.db)
        self.sink = sink
        self.checkers = list(checkers)
        self.availability = [c for c in self.checkers if c.decides_availability]
        self.enrichers = [c for c in self.checkers if not c.decides_availability]
        self.batch_size = ctx.lines_per_batch
        self.workers_count = ctx.workers_count
        self.timeout = float(ctx.setting("check_timeout", 180))
        # Обогащение живёт по своим часам: профиль достижимости гоняет сервер
        # через несколько целевых сайтов и спокойно занимает минуту даже на
        # трёх серверах. Смешивать его с коротким таймаутом батча нельзя.
        self.enrich_timeout = float(ctx.setting("enrich_timeout", 180))
        self.startup = float(ctx.setting("batch_startup", 10.0))
        self.urltest = str(ctx.setting("urltest", URLTEST_URL) or URLTEST_URL)
        self.max_retries = int(ctx.setting("max_retries", MAX_BATCH_RETRIES))
        # Служба 1 (диспетчер) умеет гонять батчи только через sing-box. Если
        # решающий чекер — свой алгоритм (uses_dispatcher не выставлен), идём
        # обычным путём через чекеры, иначе свой чекер был бы молча пропущен.
        self.use_dispatcher = bool(self.availability) and all(
            getattr(c, "uses_dispatcher", False) for c in self.availability
        )
        # Карта key -> stable: заполняется один раз, чтобы считать переходы
        # без запроса в базу на каждый сервер.
        self.stable_map: dict[str, int] = {}
        self._serial = 0
        self._serial_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        # Счётчики прогона.
        self._batches = 0
        self._ok = 0
        self._failed = 0
        self._unknown = 0
        self._enriched = 0

    # ------------------------------------------------------------------ запуск
    def request_stop(self) -> None:
        """Просит проверке завершиться.

        Вызывается движком, когда производители закончили и очередь пуста —
        до этого момента серверы продолжают браться из базы.
        """
        self._stop.set()

    async def run(self) -> dict:
        if not self.checkers:
            LOGGER.error("Не выбран ни один чекер — этап проверки пропущен")
            return {"batches": 0, "checked": 0, "ok": 0, "failed": 0, "unknown": 0}

        # Этап можно запускать повторно (например, по расписанию): сбрасываем
        # состояние прошлого прогона, иначе воркеры сразу увидят поднятый
        # _stop и не возьмут ни одного батча.
        self._stop = asyncio.Event()
        self._serial = 0
        self._batches = 0
        self._ok = 0
        self._failed = 0
        self._unknown = 0
        self._enriched = 0
        self.stable_map = await self.db.stable_map()

        LOGGER.info(
            "Этап проверки: батч до %d серверов, %s, решающие: %s, дополнения: %s",
            self.batch_size,
            f"{self.workers_count} процессов sing-box параллельно"
            if self.use_dispatcher else f"{self.workers_count} воркеров",
            ", ".join(c.name for c in self.availability) or "нет",
            ", ".join(c.name for c in self.enrichers) or "нет",
        )
        started = time.monotonic()
        try:
            if self.use_dispatcher:
                LOGGER.info(
            "Проверка живости: диспетчер батчей + живой разбор вывода sing-box, "
            "вердикты -> %s",
            getattr(self.sink, "name", "база"),
        )
                await self._dispatch_loop()
            elif self.checkers:
                await asyncio.gather(*(self._worker(i) for i in range(self.workers_count)))
        finally:
            self._stop.set()
        elapsed = time.monotonic() - started

        stats = await self.db.stats()
        queue = await self.db.queue_stats()
        LOGGER.info(
            "Этап проверки за %.1fs: батчей %d, проверено %d "
            "(живых %d, мёртвых %d, без вердикта %d, обогащено %d)",
            elapsed, self._batches, self._ok + self._failed,
            self._ok, self._failed, self._unknown, self._enriched,
        )
        LOGGER.info(
            "Очередь: ожидают %d, в работе %d, обработано %d, с ошибкой %d",
            queue.pending, queue.in_progress, queue.done, queue.failed,
        )
        return {
            "batches": self._batches,
            "checked": self._ok + self._failed,
            "ok": self._ok,
            "failed": self._failed,
            "unknown": self._unknown,
            "enriched": self._enriched,
            "queue": queue.as_dict(),
            "db": stats,
        }

    # ------------------------------------------------- служба 1 + служба 2
    async def _dispatch_loop(self) -> None:
        """Набирает серверы из базы и гоняет их батчами через sing-box."""
        dispatcher = make_dispatcher(
            db=self.db,
            batch_size=self.batch_size,
            slots=self.workers_count,
            timeout=self.timeout,
            startup=self.startup,
            urltest=self.urltest,
            max_retries=self.max_retries,
            sink=self.sink,
        )
        stats = DispatchStats()

        while not self._stop.is_set():
            want = self.batch_size * self.workers_count
            items = await self.db.claim_batch(want)
            if not items:
                if self._stop.is_set():
                    break
                await asyncio.sleep(IDLE_SLEEP)
                continue

            self._batches += 1
            batch_no = self._batches
            lines = [item.line for item in items]
            LOGGER.info(
                "Батч %d: %d серверов из базы, до %d процессов sing-box одновременно",
                batch_no, len(lines), self.workers_count,
            )

            before = (stats.alive, stats.dead, stats.retry, stats.config_errors)
            try:
                reports = await dispatcher.run_lines(lines)
            except Exception as exc:  # noqa: BLE001 — цикл не должен падать
                LOGGER.exception("Батч %d: непойманная ошибка запуска", batch_no)
                await self.db.complete_batch(items, error=f"{type(exc).__name__}: {exc}")
                self._unknown += len(items)
                continue

            await dispatcher.persist(reports, stats, items)
            alive = stats.alive - before[0]
            dead = stats.dead - before[1]
            retry = stats.retry - before[2]
            failed_batches = stats.config_errors - before[3]
            self._ok += alive
            self._failed += dead
            self._unknown += retry + failed_batches
            LOGGER.info(
                "Батч %d: живых %d, мёртвых %d, не отозвались %d (вернулись в очередь)%s",
                batch_no, alive, dead, retry,
                f", сорванных батчей {failed_batches}" if failed_batches else "",
            )

            await self._enrich(reports, batch_no)

    async def _enrich(self, reports, batch_no: int) -> None:
        """Считает страну и профиль для выживших серверов батча."""
        if not self.enrichers:
            return
        alive: list[str] = []
        seen: set[str] = set()
        for report in reports:
            if report.outcome is BatchOutcome.CONFIG_ERROR:
                continue
            for tag, verdict in report.verdicts.items():
                if verdict.verdict is not Verdict.ALIVE:
                    continue
                raw = report.tag_to_line.get(tag)
                if raw and raw not in seen:
                    seen.add(raw)
                    alive.append(raw)
        if not alive:
            return

        ctx = self._make_ctx(alive, batch_no)
        rows: list[dict] = []
        for checker in self.enrichers:
            targets = list(alive)
            try:
                result = await asyncio.wait_for(checker.check(ctx), timeout=self.enrich_timeout)
            except asyncio.TimeoutError:
                LOGGER.error(
                    "Батч %d: чекер %s не уложился в таймаут на %d серверах",
                    batch_no, checker.name, len(targets),
                )
                continue
            except Exception as exc:  # noqa: BLE001 — обогащение не влияет на живость
                LOGGER.exception(
                    "Батч %d: чекер-дополнение %s упал", batch_no, checker.name,
                )
                continue
            LOGGER.info(
                "Батч %d: %s -> %s (%d серверов)",
                batch_no, checker.name, result.summary, len(targets),
            )
            for line, outcome in result.outcomes.items():
                if outcome is None:
                    continue
                data = outcome.data or {}
                # Само измерение скорости едет в базу цифрой: по одному тегу
                # вроде speed-slow нельзя отличить 0.4 МБ/с от 0.9, а для
                # отбора лучших нужна настоящая величина.
                speed_mbps = data.get("speed_mbps")
                speed_down = data.get("speed_down")
                speed_up = data.get("speed_up")
                has_speed = any(v is not None for v in (speed_mbps, speed_down, speed_up))
                if not (outcome.country or outcome.capabilities or has_speed):
                    continue
                row = {
                    "key": _key_of(line),
                    "country": outcome.country or "",
                    "capabilities": outcome.capabilities or "",
                    "capabilities_replace": list(outcome.capabilities_replace or ()),
                }
                if has_speed:
                    row["speed_mbps"] = speed_mbps
                    row["speed_down"] = speed_down
                    row["speed_up"] = speed_up
                rows.append(row)

        if rows:
            self._enriched += await self.db.record_enrichment(rows)
            LOGGER.info("Батч %d: обогащено записей в базе: %d", batch_no, self._enriched)

    # ------------------------------------------------------ режим без решающих
    async def _worker(self, index: int) -> None:
        worker_name = f"check-{index}"
        while not self._stop.is_set():
            items = await self.db.claim_batch(self.batch_size)
            if not items:
                if self._stop.is_set():
                    return
                await asyncio.sleep(IDLE_SLEEP)
                continue
            await self._process(worker_name, items)

    async def _process(self, worker_name: str, items: "Sequence[QueueItem]") -> None:
        self._batches += 1
        batch_no = self._batches
        lines = [item.line for item in items]
        LOGGER.info("[%s] Батч %d: %d серверов", worker_name, batch_no, len(lines))

        try:
            result, error = await self._run_checkers(worker_name, lines, batch_no)
        except Exception as exc:  # noqa: BLE001 — воркер не должен падать
            LOGGER.exception("[%s] Батч %d: непойманая ошибка", worker_name, batch_no)
            result, error = CheckResult(), f"{type(exc).__name__}: {exc}"

        unresolved = [
            item for item in items
            if (result.outcomes.get(item.line) is None
                or result.outcomes[item.line].ok is None)
        ]
        if unresolved:
            attempts = max((item.attempts for item in items), default=1)
            if attempts <= MAX_BATCH_RETRIES:
                await self.db.complete_batch(items, error=error or "неполный результат")
                await self.db.enqueue([i.line for i in unresolved], source="retry")
                LOGGER.warning(
                    "[%s] Батч %d: %d серверов без вердикта — возврат в очередь "
                    "(попытка %d/%d)",
                    worker_name, batch_no, len(unresolved), attempts + 1,
                    MAX_BATCH_RETRIES + 1,
                )
                return
            LOGGER.error(
                "[%s] Батч %d: %d серверов осталось без вердикта после %d попыток — "
                "снимаем с проверки", worker_name, batch_no, len(unresolved), attempts,
            )

        await self._persist(worker_name, batch_no, items, result, error)

    async def _persist(
        self, worker_name: str, batch_no: int,
        items: "Sequence[QueueItem]", result: CheckResult, error: str | None,
    ) -> None:
        async with self._serial_lock:
            self._serial += len(items)
            start_serial = self._serial - len(items)

        rows, self.stable_map = build_rows(
            items, result,
            stable_map=self.stable_map,
            start_serial=start_serial,
            new_server_stable=int(NEW_SERVER_STABLE),
        )
        await self.db.record_results(rows)

        if result.unparsable:
            banned = await self.db.blacklist_unparsable(result.unparsable)
            LOGGER.info(
                "[%s] Батч %d: %d неразбираемых строк ушли в чс",
                worker_name, batch_no, banned,
            )

        await self.db.complete_batch(items, error=error)

        counts = count_results(result.outcomes)
        self._ok += counts["ok"]
        self._failed += counts["failed"]
        self._unknown += counts["unknown"]
        LOGGER.info(
            "[%s] Батч %d: живых %d, мёртвых %d, без вердикта %d, в базу записано %d",
            worker_name, batch_no, counts["ok"], counts["failed"],
            counts["unknown"], len(rows),
        )

    # ---------------------------------------------------------------- чекеры
    async def _run_checkers(
        self, worker_name: str, lines: list[str], batch_no: int,
    ) -> tuple:
        """Прогоняет чекеры по батчу. Возвращает (результат, текст ошибок)."""
        base_ctx = self._make_ctx(lines, batch_no)
        merged = CheckResult()
        verdict_errors: list[str] = []
        enrich_errors: list[str] = []
        collected: list[CheckResult] = []

        def note_failure(checker_name: str, exc: BaseException | None,
                         basket: list[str]) -> None:
            basket.append(
                f"{checker_name}: timeout" if exc is None else f"{checker_name}: {exc}"
            )

        for checker in self.availability:
            try:
                result = await asyncio.wait_for(checker.check(base_ctx), timeout=self.enrich_timeout)
            except asyncio.TimeoutError:
                LOGGER.error(
                    "[%s] Батч %d: чекер %s не уложился в таймаут (%.0fs)",
                    worker_name, batch_no, checker.name, self.timeout,
                )
                note_failure(checker.name, None, verdict_errors)
                continue
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "[%s] Батч %d: чекер %s упал", worker_name, batch_no, checker.name,
                )
                note_failure(checker.name, exc, verdict_errors)
                continue
            collected.append(result)
            merged.merge(result)
            LOGGER.info(
                "[%s] Батч %d: %s -> %s", worker_name, batch_no, checker.name, result.summary,
            )

        for checker in self.enrichers:
            if checker.run_on_dead:
                target_lines = list(lines)
            else:
                target_lines = [
                    line for line in lines
                    if (outcome := merged.outcomes.get(line)) is not None and outcome.ok is True
                ]
            if not target_lines:
                continue
            try:
                result = await asyncio.wait_for(
                    checker.check(self._make_ctx(target_lines, batch_no)),
                    timeout=self.enrich_timeout,
                )
            except asyncio.TimeoutError:
                LOGGER.error(
                    "[%s] Батч %d: чекер %s не уложился в таймаут",
                    worker_name, batch_no, checker.name,
                )
                note_failure(checker.name, None, enrich_errors)
                continue
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "[%s] Батч %d: чекер %s упал", worker_name, batch_no, checker.name,
                )
                note_failure(checker.name, exc, enrich_errors)
                continue
            collected.append(result)
            merged.merge(result)
            LOGGER.info(
                "[%s] Батч %d: %s -> %s (%d серверов)",
                worker_name, batch_no, checker.name, result.summary, len(target_lines),
            )

        merged.unparsable = list(dict.fromkeys(
            line for result in collected for line in result.unparsable
        ))
        if enrich_errors:
            LOGGER.warning(
                "[%s] Батч %d: чекеры-дополнения отработали с ошибками (%s) — "
                "на результат проверки не влияет",
                worker_name, batch_no, ", ".join(enrich_errors),
            )
        return merged, "; ".join(verdict_errors) if verdict_errors else None

    def _make_ctx(self, lines: list[str], batch_no: int) -> CheckContext:
        return CheckContext(
            lines,
            index=batch_no,
            total_batches=0,
            settings=self.ctx.settings,
            db=self.db,
            logger=self.ctx.logger,
        )


def _key_of(line: str) -> str:
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(line)
