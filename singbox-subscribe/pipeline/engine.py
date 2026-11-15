"""Движок pipeline: собирает этапы и гоняет их по расписанию.

Схема одного прогона:

    ┌──────────────┐   upsert + enqueue   ┌───────────────┐
    │  discovery   │ ───────────────────► │ check_queue   │
    │ (поиск по    │   refill_from_pool   │   (в базе!)   │
    │  подпискам)  │ ───────────────────► │               │
    └──────────────┘                      └───────┬───────┘
                                                │ claim_batch
                                         ┌──────▼───────┐
                                         │  check × N   │ воркеры, каждый
                                         │ (батчи)      │ со своим sing-box
                                         └──────┬───────┘
                                                │ record_results
                                         ┌──────▼───────┐
                                         │    export    │
                                         └──────────────┘

Поиск и проверка идут ОДНОВРЕМЕННО: как только база пополнилась новыми
серверами, воркеры уже берут их в работу.

Экспорт и деплой НЕ останавливают проверку. Пока очередь проверки не
опустела, публикация идёт параллельно по таймеру (publisher_interval) и
берёт то, что уже найдено. Проверка 200 тысяч серверов не должна
обрываться на середине из-за того, что пришло время выложить конфиг.

Использование:

    from pipeline import Pipeline

    async with Pipeline() as pipeline:
        report = await pipeline.run()

Всё, что делает pipeline, доступно и по отдельности:
```pipeline.discover()`, ``pipeline.check()`, ``pipeline.export()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any, Sequence

from config.settings import get_settings
from pipeline.checkers import build_checkers
from pipeline.context import PipelineContext, Settings, default_settings
from pipeline.database import (
    QUEUE_FAILED,
    QUEUE_IN_PROGRESS,
    QUEUE_PENDING,
    Database,
)
from pipeline.logging_setup import get_logger, log_throttle_summary, setup_logging
from pipeline.stages import CheckStage, DiscoveryStage, ExportStage

LOGGER = get_logger("engine")


class Pipeline:
    """Асинхронный прогон: поиск -> очередь -> проверка -> экспорт."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        checkers: str | Sequence[str] | None = None,
        custom_dir: str | Path | None = None,
        db: Database | None = None,
        logger: Any | None = None,
    ) -> None:
        setup_logging()
        self.logger = logger or get_logger("engine")
        self.settings: Settings = default_settings().merged(settings)

        if checkers is not None:
            selection = checkers if isinstance(checkers, str) else ",".join(checkers)
            self.settings["checkers"] = selection
        else:
            selection = self.settings.get("checkers") or get_settings().pipeline.checkers

        self.checkers = build_checkers(
            selection, custom_dir=custom_dir or get_settings().paths.custom_checkers_dir,
        )
        self.db = db or Database(db_path or get_settings().paths.servers_db_file)
        self.ctx = PipelineContext(
            settings=self.settings, db=self.db, checkers=self.checkers, logger=self.logger,
        )
        self.discovery = DiscoveryStage(self.ctx)
        self.check_stage = CheckStage(self.ctx, self.checkers)
        self.export_stage = ExportStage(self.ctx)
        self.report: dict[str, Any] = {}

    # ------------------------------------------------------- жизненный цикл
    async def __aenter__(self) -> "Pipeline":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        for checker in self.checkers:
            try:
                await checker.teardown()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Завершение чекера %s не удалось: %s", checker.name, exc)
        await self.db.close()

    # ---------------------------------------------------------------- прогон
    async def run(
        self,
        *,
        discovery: bool | None = None,
        check: bool = True,
        export: bool = True,
    ) -> dict:
        """Полный прогон. Возвращает отчёт (в лог уходит сводка)."""
        started = time.monotonic()
        LOGGER.info("=" * 66)
        LOGGER.info("Старт pipeline: %s", self.ctx.describe())
        LOGGER.info("Чекеры: %s", ", ".join(self.checker_names) or "(нет)")

        self.report = {"started_at": started, "ok": True, "error": None}
        try:
            if self.settings.get("queue_persist", get_settings().pipeline.queue_persist):
                requeued = await self.db.requeue_stale(
                    statuses=(QUEUE_IN_PROGRESS, QUEUE_FAILED),
                )
                if requeued:
                    LOGGER.info("В очередь возвращено незавершённых записей: %d", requeued)

            for checker in self.checkers:
                try:
                    await checker.setup(self.ctx)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.error("Подготовка чекера %s не удалась: %s", checker.name, exc)

            # --- поиск и пополнение очереди, параллельно с проверкой ---------
            do_discovery = (
                self.settings.get("discovery", True) if discovery is None else discovery
            )
            producers: list[asyncio.Task] = []
            if do_discovery:
                producers.append(asyncio.create_task(
                    self._run_discovery(), name="pipeline.discovery",
                ))
            else:
                LOGGER.info("Этап поиска выключен")
            producers.append(asyncio.create_task(
                self._refill_pool(), name="pipeline.refill",
            ))

            check_task: asyncio.Task | None = None
            if check:
                check_task = asyncio.create_task(
                    self.check_stage.run(), name="pipeline.check",
                )

            results = await asyncio.gather(*producers, return_exceptions=True)
            self.report["discovery"] = {}
            for result in results:
                if isinstance(result, BaseException):
                    LOGGER.error("Производитель очереди упал: %s", result)
                    self.report["ok"] = False
                    continue
                payload = result or {}
                self.report["discovery"].update(payload)
                # Упавший этап поиска/наполнения делает прогон неуспешным:
                # иначе в отчёте «успешно», а база осталась нерасширенной.
                if payload.get("ok") is False:
                    self.report["ok"] = False
                    if not self.report.get("error"):
                        self.report["error"] = payload.get("error")

            # --- проверка + параллельная публикация --------------------------
            #
            # Публикация (и деплой внутри неё) идёт ОТДЕЛЬНОЙ задачей и не
            # останавливает проверку. Раньше здесь стоял _drain() с часовым
            # лимитом: на очереди в 200 тысяч серверов он истекал, проверка
            # глушилась, и деплой уезжал с тем, что успели проверить.
            # Теперь деплой берёт снимок базы и проверка продолжается.
            publish_task: asyncio.Task | None = None
            if export:
                publish_task = asyncio.create_task(
                    self._publish_loop(), name="pipeline.publish",
                )

            try:
                if check_task is not None:
                    drained = await self._drain()
                    if not drained:
                        LOGGER.warning(
                            "Проверка остановлена по лимиту drain_timeout; "
                            "непроверенные серверы останутся в очереди"
                        )
                    self.check_stage.request_stop()
                    self.report["check"] = await check_task
                else:
                    self.check_stage.request_stop()
                    self.report["check"] = {"skipped": True}
            finally:
                if publish_task is not None:
                    publish_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await publish_task

            # --- финальный экспорт -------------------------------------------
            # Последний проход уже ПОСЛЕ проверки: в нём попадают результаты,
            # которых не было ни в одной публикации по таймеру.
            if export:
                self.report["export"] = await self.export_stage.run(self.db)
            else:
                self.report["export"] = {"skipped": True}

            self.report["queue"] = (await self.db.queue_stats()).as_dict()
            self.report["stats"] = await self.db.stats()

        except Exception as exc:  # noqa: BLE001 — прогон не должен ронять сервис
            LOGGER.exception("Прогон pipeline прерван")
            self.report["ok"] = False
            self.report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self.report["elapsed"] = time.monotonic() - started
            self._log_summary()
            log_throttle_summary(self.logger)
        return self.report

    # ------------------------------------------------------- отдельные этапы
    async def discover(self) -> dict:
        return await self.discovery.run(self.db)

    async def check(self) -> dict:
        return await self.check_stage.run()

    async def export(self) -> dict:
        return await self.export_stage.run(self.db)

    async def status(self) -> dict:
        """Состояние базы и очереди (для CLI и HTTP API)."""
        queue = await self.db.queue_stats()
        stats = await self.db.stats()
        return {
            "checkers": self.checker_names,
            "queue": queue.as_dict(),
            "db": stats,
            "purge_stable_below": int(get_settings().core.purge_stable_below),
        }

    async def fill_queue(self, *, limit: int | None = None) -> int:
        """Наполнить очередь живым пулом из базы (без этапа поиска)."""
        return await self.db.refill_from_pool(limit=limit)

    # ------------------------------------------------------------- внутреннее
    @property
    def checker_names(self) -> list[str]:
        return [c.name for c in self.checkers]

    async def _run_discovery(self) -> dict:
        try:
            return await self.discovery.run(self.db)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Этап поиска упал")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _refill_pool(self) -> dict:
        """Добирает в очередь живые серверы, уже лежащие в базе.

        Именно это «собирает по фильтрам из базы список на проверку»: берутся
        записи с excluded=0 и stable >= PURGE_STABLE_BELOW.
        """
        try:
            added = await self.db.refill_from_pool()
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Наполнение очереди из базы упало")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        LOGGER.info("В очередь добавлено серверов из пула базы: %d", added)
        return {"ok": True, "from_pool": added}

    async def _publish_loop(self) -> None:
        """Публикует результаты по таймеру, НЕ останавливая проверку.

        Пока очередь проверки не опустела, деплой и выгрузка идут отдельной
        задачей: каждый проход берёт текущий снимок базы. Останавливается
        только отменой задачи из run().
        """
        try:
            interval = float(get_settings().services.publisher_interval or 1800)
        except (TypeError, ValueError):
            interval = 1800.0
        interval = max(60.0, interval)
        LOGGER.info("Публикация параллельно проверке: каждые %.0f с", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                report = await self.export_stage.run(self.db)
                self.report["publish"] = report
                LOGGER.info(
                    "Публикация по таймеру: whitelist %d, исключено %d, деплой %s",
                    report.get("whitelist", 0), report.get("purged", 0),
                    "выполнен" if report.get("deploy") else "нет",
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — публикация не должна ронять проверку
                LOGGER.exception("Публикация по таймеру не удалась — проверка продолжается")

    async def _drain(self, *, timeout: float | None = None) -> bool:
        """Ждёт, пока очередь опустеет (производители уже закончили).

        Возвращает True, если очередь опустела, и False, если сработал
        лимит ожидания.

        По умолчанию лимита НЕТ: проверка двухсот тысяч серверов занимает
        часы, и обрывать её по часам нельзя — именно из-за этого деплой
        уезжал с половиной данных, а остальные серверы так и оставались
        непроверенными. Лимит включается только явно через
        pipeline.drain_timeout (секунды, 0 = без лимита).
        """
        limit = timeout
        if limit is None:
            try:
                limit = float(self.ctx.setting("drain_timeout", 0) or 0)
            except (TypeError, ValueError):
                limit = 0.0
        deadline = time.monotonic() + limit if limit and limit > 0 else None
        last_log = time.monotonic()
        while True:
            queue = await self.db.queue_stats()
            if queue.pending == 0 and queue.in_progress == 0:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            # Раз в 5 минут напоминаем, сколько осталось: очередь в 200 тысяч
            # серверов идёт часами, и молчание в логе выглядит как зависание.
            now = time.monotonic()
            if now - last_log >= 300.0:
                last_log = now
                LOGGER.info(
                    "Ожидание проверки: в очереди %d, в работе %d",
                    queue.pending, queue.in_progress,
                )
            await asyncio.sleep(0.5)

    def _log_summary(self) -> None:
        report = self.report or {}
        discovery = report.get("discovery") or {}
        check = report.get("check") or {}
        export = report.get("export") or {}
        LOGGER.info("-" * 66)
        if discovery:
            LOGGER.info(
                "Поиск: +%d серверов, %d ссылок в очередь, %d в чс",
                discovery.get("added", 0), discovery.get("enqueued", 0),
                discovery.get("blacklisted", 0),
            )
        if check and not check.get("skipped"):
            LOGGER.info(
                "Проверка: батчей %d, живых %d, мёртвых %d, без вердикта %d",
                check.get("batches", 0), check.get("ok", 0),
                check.get("failed", 0), check.get("unknown", 0),
            )
        if export and not export.get("skipped"):
            LOGGER.info(
                "Экспорт: whitelist %d, исключено %d",
                export.get("whitelist", 0), export.get("purged", 0),
            )
        LOGGER.info("Итог: %s за %.1fs", "успешно" if report.get("ok") else "с ошибками",
                    report.get("elapsed", 0.0))
        LOGGER.info("=" * 66)


def run_pipeline(
    *,
    db_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
    checkers: str | Sequence[str] | None = None,
    custom_dir: str | Path | None = None,
    discovery: bool | None = None,
    export: bool = True,
) -> dict:
    """Синхронная обёртка: один полный прогон pipeline.

    ```python
    from pipeline import run_pipeline
    report = run_pipeline(checkers="url_probe,country")
    ```
    """
    return asyncio.run(
        Pipeline(
            db_path=db_path, settings=settings, checkers=checkers, custom_dir=custom_dir,
        ).run(discovery=discovery, export=export)
    )


__all__ = ["Pipeline", "run_pipeline", "QUEUE_PENDING", "QUEUE_IN_PROGRESS", "QUEUE_FAILED"]
