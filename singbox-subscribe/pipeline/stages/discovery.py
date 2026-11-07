"""Этап поиска: новые серверы из ссылок.

Полностью асинхронный: подписки качаются параллельно (пул потоков), а сам
этап отдаёт управление event loop другим задачам. Главное его дело —
РАСШИРИТЬ центральную базу и поставить новые серверы в очередь проверки.

Шаги:

  1. прочитать список URL из config/subs/urls.json;
  2. скачать источники параллельно, пропуская актуальный кеш;
  3. собрать уникальные строки, выбросить мусор и fp=unsafe;
  4. разделить на разбираемые и неразбираемые (core.split_parsable_lines);
  5. разбираемые -> upsert в базу + очередь (source=discovery);
     неразбираемые -> blacklist, они всё равно нечем проверять.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.database import SOURCE_DISCOVERY
from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.context import PipelineContext
    from pipeline.database import Database

LOGGER = get_logger("stages.discovery")


class DiscoveryStage:
    """Ищет новые серверы в подписках и кладёт их в базу и очередь."""

    name = "discovery"

    def __init__(self, ctx: "PipelineContext") -> None:
        self.ctx = ctx

    async def run(self, db: "Database") -> dict:
        urls_file = Path(self.ctx.setting("urls_file"))
        # Куда складывать скачанные подписки. Берём от КАТАЛОГА БАЗЫ, а не
        # отсчётом «на три уровня вверх от urls.json»: тот путь был привязан
        # к тому, что urls.json лежит в config/subs внутри пакета, и после
        # переезда рабочих файлов в корень репозитория указывал бы в никуда.
        # База — сосед downloaded/ по смыслу: оба меняются прогоном.
        #
        # Путь берём у САМОЙ базы (db.path), а не из настроек: settings здесь —
        # это Settings конвейера, и в нём поля servers_db_file нет вовсе
        # (см. pipeline/context.default_settings), так что setting() вернул бы
        # None и падал бы уже Path(). У объекта базы путь всегда настоящий —
        # включая случай, когда его переопределили флагом --db.
        download_dir = Path(db.path).parent / "downloaded"
        logger = self.ctx.logger

        from script.downloader import load_urls, read_source_lines
        from script import core as core_mod

        try:
            urls = await self.ctx.run_sync(load_urls, urls_file)
        except Exception as exc:  # noqa: BLE001 — без файла источников этап пропускаем
            logger.error("Список URL не прочитан (%s): %s", urls_file, exc)
            return {"ok": False, "error": str(exc), "added": 0, "enqueued": 0}

        if not urls:
            logger.warning("Список URL пуст: %s", urls_file)
            return {"ok": True, "sources": 0, "added": 0, "enqueued": 0}

        logger.info("Поиск: %d источников подписок", len(urls))

        # --- скачивание (блокирующий код уходит в поток) ---------------------
        from script.downloader import download_sources

        try:
            files = await self.ctx.run_sync(download_sources, urls, download_dir, logger=logger)
        except Exception as exc:  # noqa: BLE001 — сеть может отвалиться
            logger.error("Не удалось скачать источники: %s", exc)
            return {"ok": False, "error": str(exc), "added": 0, "enqueued": 0}

        if not files:
            logger.warning("Ни один источник не скачался")
            return {"ok": True, "sources": 0, "added": 0, "enqueued": 0}

        # --- разбор скачанного ----------------------------------------------
        lines, duplicates = await self.ctx.run_sync(read_source_lines, files)
        logger.info(
            "Поиск: %d уникальных ссылок из %d файлов (дублей отброшено: %d)",
            len(lines), len(files), duplicates,
        )

        parsable, unparsable = await self.ctx.run_sync(core_mod.split_parsable_lines, lines)
        if unparsable:
            logger.info(
                "Поиск: %d ссылок нечем проверять — уходят в чс", len(unparsable),
            )
        logger.info(
            "Поиск: %d ссылок готовы к проверке", len(parsable),
        )

        # --- база и очередь --------------------------------------------------
        upsert = await db.upsert_lines(parsable) if parsable else {"added": 0, "updated": 0}
        blacklisted = await db.blacklist_unparsable(unparsable) if unparsable else 0
        enqueued = await db.enqueue(parsable, source=SOURCE_DISCOVERY) if parsable else 0

        logger.info(
            "Поиск: +%d новых, обновлено %d, в чс %d, в очередь %d",
            upsert.get("added", 0), upsert.get("updated", 0), blacklisted, enqueued,
        )
        return {
            "ok": True,
            "sources": len(files),
            "links": len(lines),
            "parsable": len(parsable),
            "unparsable": len(unparsable),
            "added": upsert.get("added", 0),
            "updated": upsert.get("updated", 0),
            "blacklisted": blacklisted,
            "enqueued": enqueued,
        }
