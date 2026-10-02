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
серверами, воркеры уже берут их в работу. Ждут завершения оба, потом запускается
экспорт.

Использование:

    from pipeline import Pipeline

    async with Pipeline() as pipeline:
        report = await pipeline.run()

Всё, что делает pipeline, доступно и по отдельности:
```pipeline.discover()`, ``pipeline.check()`, ``pipeline.export()``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Sequence

from config.env import (
    PIPELINE_CHECKERS,
    PIPELINE_CUSTOM_CHECKERS_DIR,
    PIPELINE_QUEUE_PERSIST,
    PURGE_STABLE_BELOW,
    SERVERS_DB_FILE,
)
from pipeline.checkers import build_checkers, describe as describe_checkers
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
            selection = self.settings.get("checkers") or PIPELINE_CHECKERS

        self.checkers = build_checkers(
            selection, custom_dir=custom_dir or PIPELINE_CUSTOM_CHECKERS_DIR,
        )
        self.db = db or Database(db_path or SERVERS_DB_FILE)
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
            if self.settings.get("queue_persist", PIPELINE_QUEUE_PERSIST):
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

            if check_task is not None:
                await self._drain()
                self.check_stage.request_stop()
                self.report["check"] = await check_task
            else:
                self.check_stage.request_stop()
                self.report["check"] = {"skipped": True}

            # --- экспорт ----------------------------------------------------
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
            "purge_stable_below": int(PURGE_STABLE_BELOW),
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

    async def _drain(self, *, timeout: float = 3600.0) -> None:
        """Ждёт, пока очередь опустеет (производители уже закончили)."""
        deadline = time.monotonic() + timeout
        idle = 0.0
        while time.monotonic() < deadline:
            queue = await self.db.queue_stats()
            if queue.pending == 0 and queue.in_progress == 0:
                return
            idle = 0.0 if queue.pending else idle + 0.5
            await asyncio.sleep(0.5)
        LOGGER.warning("Очередь не опустела за %.0fs — проверка прервана", timeout)

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
                "Экспорт: whitelist %d, blacklist %d, merge %d, исключено %d",
                export.get("whitelist", 0), export.get("blacklist", 0),
                export.get("merge", 0), export.get("purged", 0),
            )
        LOGGER.info("Итог: %s за %.1fs", "успешно" if report.get("ok") else "с ошибками",
                    report.get("elapsed", 0.0))
        LOGGER.info("=" * 66)

    @staticmethod
    def available_checkers() -> list[dict]:
        """Все найденные чекеры (для CLI)."""
        return describe_checkers()


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
