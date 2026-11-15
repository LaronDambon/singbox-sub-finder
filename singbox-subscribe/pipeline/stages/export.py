"""Этап экспорта: приводим файлы и внешние системы в соответствие с базой.

После того как очередь проверки опустела, база — единственный источник правды.
Этот этап:

  * исключает из ротации умерших (``purge_dead``);
  * экспортирует ``whitelist.txt`` (пинговавшиеся в последней проверке)
    — для внешних потребителей;
  * при DEPLOY_ENABLED=1 — собирает конфиг и отправляет в GitHub.

``blacklist.txt`` и ``merge.txt`` больше НЕ выгружаются: и то, и другое —
файловая проекция базы, которую никто из кода не читает. Чёрный список
остаётся в базе (колонка ``excluded`` + ``blacklist_unparsable()`` — на неё
опираются повторы, «щит» и purge), а пул проверки берётся из базы
(``check_pool()``/``check_queue``), а не из промежуточного файла.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from config.settings import get_settings
from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.context import PipelineContext
    from pipeline.database import Database

LOGGER = get_logger("stages.export")

#: Токен вшивается прямо в URL скачивания конфига. Писать такой URL в лог
#: нельзя: логи уезжают в архивы, а токен — живой секрет.
_TOKEN_IN_URL = re.compile(r"(https?://)[^/@]+(?=@)")


def redact(value: object) -> str:
    """Маскирует «https://<токен>@host» -> «https://***@host»."""
    return _TOKEN_IN_URL.sub(r"\1***", str(value))


class ExportStage:
    """Финальный этап: очистка ротации, экспорт списков, деплой."""

    name = "export"

    def __init__(self, ctx: "PipelineContext") -> None:
        self.ctx = ctx

    async def run(self, db: "Database", *, deploy: bool | None = None) -> dict:
        """Обновляет списки и, по решению, публикует конфиг.

        deploy=None -> берётся глобальная настройка DEPLOY_ENABLED.
        Служба публикации передаёт его явно: выгрузка по расписанию и
        выгрузка по кнопке — разные вещи, и режим служб по умолчанию
        деплой выключает.
        """
        result: dict = {"purged": 0, "whitelist": 0, "deploy": None}
        # deploy — список блоков; пустой список = деплой выключен.
        blocks = getattr(get_settings().deploy, "blocks", None) or []
        want_deploy = bool(blocks) if deploy is None else bool(deploy)

        # --- умершие выпадают из ротации ------------------------------------
        result["purged"] = await db.purge_dead(get_settings().core.purge_stable_below)
        LOGGER.info("Из проверки исключено серверов: %d", result["purged"])

        # --- whitelist: единственная файловая выгрузка ------------------------
        # blacklist.txt и merge.txt не пишутся: их состояние живёт в базе.
        if self.ctx.setting("export_lists", True):
            result["whitelist"] = await db.export_whitelist(
                Path(self.ctx.setting("whitelist_file")),
                global_tag=self.ctx.setting("global_tag", "Global"),
            )
            LOGGER.info("Экспорт из базы: whitelist %d", result["whitelist"])

        # --- деплой конфига в GitHub ----------------------------------------
        if want_deploy:
            result["deploy"] = await self.ctx.run_sync(self._deploy)
        return result

    @staticmethod
    def _deploy():
        """Публикация по списку блоков deploy. Ошибки не поднимаются."""
        from deploy_config import run_deploy

        res = run_deploy(silent=True)
        if not res.get("results"):
            LOGGER.info("Деплой выключен: deploy.blocks пуст")
            return None
        for item in res.get("results") or []:
            if not isinstance(item, dict):
                continue
            if item.get("ok"):
                LOGGER.info("Деплой [%s] выполнен: %s",
                            item.get("name"), item.get("repo") or item.get("dir"))
            else:
                LOGGER.warning("Деплой [%s] не выполнен: %s",
                               item.get("name"), item.get("error"))
            for sub in item.get("results") or []:
                if isinstance(sub, dict) and not sub.get("ok"):
                    LOGGER.warning("  не выложен %s: %s",
                                   sub.get("template") or "?",
                                   sub.get("error") or "причина не указана")
                if isinstance(sub, dict) and sub.get("url"):
                    sub["url"] = redact(sub["url"])
        return res
