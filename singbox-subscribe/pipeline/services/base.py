"""Службы: вместо одного прогона — несколько независимых работников.

Прогон pipeline строго последователен: поиск, потом проверка, потом экспорт.
Каждый шаг ждёт предыдущий, между шагами система простаивает, а свежие
серверы ждут своей очереди.

Службы устроены иначе. Каждая — отдельный бесконечный цикл с своей
очередью в базе, и они крутятся ОДНОВРЕМЕННО:

    discovery  --(нашёл серверы)-->  check_queue
    collector  --(добрал из базы)--->  check_queue
    checker    --(взял батч)------>  result_queue
    results    <--(забрал вердикты)-  result_queue  --> запись в servers
    publisher  <--(раз в N минут)--  servers  --> whitelist/merge/деплой

Ни одна служба не ждёт окончания другой: discovery может докладывать
серверы в очередь, пока checker уже гоняет батчи, а publisher выгружать
свежие результаты, не дожидаясь, пока опустеет очередь проверки.

Надзорник (Supervisor) поднимает службы, следит, чтобы упавшая поднялась
обратно, и гасит их все по Ctrl+C.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.context import PipelineContext

LOGGER = get_logger("services")

#: Сколько ждать перед повтором упавшей службы, сек.
RESTART_DELAY = 5.0
#: Сколько ждать первой попытки упавшей службы перед повтором, сек (затем x2).
BACKOFF_START = 2.0
BACKOFF_MAX = 120.0
#: Сколько ждать завершения служб при остановке, сек.
GRACE_SECONDS = 20.0


class Service:
    """Одна служба: бесконечный цикл работы с интервалом.

    Включённость службы задаётся один раз: либо конструктором (enabled=),
    либо настройкой из объекта настроек при создании службы (get_settings().
    services.<флажок>). Полагаться на os.environ прямо в момент setup()
    смысла нет: настройки и так собираются в один объект, а читать их по
    дороге в коде — значит завести ещё одно место, где значение живёт.

    interval=None означает «работать без остановки»: цикл не ждёт паузы
    между проходами, а служба сама решает, когда ей пора (обычно — когда
    очередь пуста, см. idle()).
    """

    #: Имя для логов и отчёта.
    name: str = "service"
    #: Пауза между проходами, сек. None — без остановки.
    interval: float | None = 60.0
    #: Служба выключена и не запускается.
    enabled: bool = True

    def __init__(self, ctx: "PipelineContext") -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.runs = 0
        self.errors = 0
        self.last_started: float = 0.0
        self.last_duration: float = 0.0
        self.last_error: str = ""

    async def setup(self) -> None:
        """Одноразовая подготовка. Ошибка здесь не должна ронять службу."""

    async def run_once(self) -> Any:
        """Один проход работы. Возвращает что угодно для логов."""
        raise NotImplementedError

    def wants_work(self) -> bool:
        """Есть ли смысл в очередном проходе (для служб без интервала)."""
        return True

    async def sleep(self, seconds: float, stop: asyncio.Event) -> None:
        """Пауза, которая просыпается по Ctrl+C, а не по таймауту."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def loop(self, stop: asyncio.Event) -> None:
        """Бесконечный цикл службы с устойчивостью к ошибкам."""
        LOGGER.info("Служба %s: запущена", self.name)
        delay = BACKOFF_START
        while not stop.is_set():
            try:
                await self.setup()
                delay = BACKOFF_START
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("Служба %s: подготовка не удалась", self.name)
                await self.sleep(delay, stop)
                delay = min(delay * 2, BACKOFF_MAX)
                continue

            if not self.enabled:
                LOGGER.info("Служба %s: выключена, жду", self.name)
                await self.sleep(30.0, stop)
                continue

            try:
                started = time.monotonic()
                self.last_started = started
                result = await self.run_once()
                self.runs += 1
                self.last_duration = time.monotonic() - started
                self._log_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — служба не должна умирать
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("Служба %s: проход не удался", self.name)
                await self.sleep(delay, stop)
                delay = min(delay * 2, BACKOFF_MAX)
                continue

            if self.interval is None:
                if self.wants_work():
                    continue
                await self.sleep(2.0, stop)
            else:
                await self.sleep(self.interval, stop)

    def _log_result(self, result: Any) -> None:
        if result is None:
            return
        text = result if isinstance(result, str) else str(result)
        if text and text != "None":
            LOGGER.info("Служба %s: %s (%.2fs)", self.name, text, self.last_duration)

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "interval": self.interval,
            "runs": self.runs,
            "errors": self.errors,
            "last_duration": round(self.last_duration, 2),
            "last_error": self.last_error,
        }


class Supervisor:
    """Держит службы включёнными и выключает их вместе."""

    def __init__(self, services: list[Service], *, on_tick: Callable | None = None) -> None:
        self.services = services
        self.on_tick = on_tick
        self.stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._reporter: asyncio.Task | None = None

    def start(self) -> None:
        for service in self.services:
            self._tasks.append(
                asyncio.create_task(service.loop(self.stop), name=f"svc.{service.name}")
            )
        self._reporter = asyncio.create_task(self._report_loop(), name="svc.report")
        LOGGER.info("Служб запущено: %d", len(self.services))

    async def stop_all(self, *, grace: float = GRACE_SECONDS) -> None:
        """Гасит службы, дав дописать начатое.

        Отмена задачи не останавливает sing-box и сетевые запросы, уже
        запущенные в потоке: без паузы на завершение процесс уходил с
        открытыми каналами subprocess и печатал ошибки в самом конце.
        """
        LOGGER.info("Останавливаю службы (до %.0f с на завершение)...", grace)
        self.stop.set()
        if self._reporter is not None:
            self._reporter.cancel()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=grace)
            if pending:
                LOGGER.warning(
                    "Службы не успели завершиться за %.0f с, прерываю: %s",
                    grace, ", ".join(t.get_name() for t in pending),
                )
                for task in pending:
                    task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        LOGGER.info("Службы остановлены")

    async def _report_loop(self) -> None:
        """Раз в минуту — короткая сводка по службам."""
        while not self.stop.is_set():
            await asyncio.sleep(60.0)
            if self.stop.is_set():
                break
            try:
                if self.on_tick is not None:
                    await self.on_tick()
                self.log_status()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Сводка по службам не собралась")

    def log_status(self) -> None:
        for service in self.services:
            if service.errors and service.last_error:
                LOGGER.warning(
                    "  %-10s проходов %-5d ошибок %-4d последняя: %s",
                    service.name, service.runs, service.errors, service.last_error,
                )
            else:
                LOGGER.info(
                    "  %-10s проходов %-5d ошибок %-4d последний %.1fs",
                    service.name, service.runs, service.errors, service.last_duration,
                )

    def stats(self) -> list[dict[str, Any]]:
        return [s.stats() for s in self.services]
