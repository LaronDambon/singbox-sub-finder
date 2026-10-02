"""Этап экспорта: приводим файлы и внешние системы в соответствие с базой.

После того как очередь проверки опустела, база — единственный источник правды.
Этот этап:

  * исключает из ротации умерших (``purge_dead``);
  * экспортирует ``whitelist.txt`` (пинговавшиеся в последней проверке)
    и ``blacklist.txt`` (умершие) — для внешних потребителей;
  * пишет ``merge.txt`` — тот самый пул проверки, который раньше был
    промежуточным файлом между скачиванием и проверкой;
  * при DEPLOY_ENABLED=1 — собирает конфиг и отправляет в GitHub.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from config.env import (
    DEPLOY_ENABLED,
    DEPLOY_PATH,
    DEPLOY_TEMPLATE,
    DEPLOY_TEMPLATES,
    GH_DEPLOY_REPO,
    PURGE_STABLE_BELOW,
)
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

    async def run(self, db: "Database") -> dict:
        result: dict = {"purged": 0, "whitelist": 0, "blacklist": 0, "merge": 0, "deploy": None}

        # --- умершие выпадают из ротации ------------------------------------
        result["purged"] = await db.purge_dead(PURGE_STABLE_BELOW)
        LOGGER.info("Из проверки исключено серверов: %d", result["purged"])

        # --- merge.txt: файл пула проверки ----------------------------------
        if self.ctx.setting("write_merge", True):
            result["merge"] = await db.write_pool_file(Path(self.ctx.setting("merge_file")))
            LOGGER.info("Пул проверки записан: %d серверов", result["merge"])

        # --- whitelist / blacklist -------------------------------------------
        if self.ctx.setting("export_lists", True):
            result["whitelist"] = await db.export_whitelist(
                Path(self.ctx.setting("whitelist_file")),
                global_tag=self.ctx.setting("global_tag", "Global"),
            )
            result["blacklist"] = await db.export_blacklist(
                Path(self.ctx.setting("blacklist_file")),
            )
            LOGGER.info(
                "Экспорт из базы: whitelist %d, blacklist %d",
                result["whitelist"], result["blacklist"],
            )

        # --- деплой конфига в GitHub ----------------------------------------
        if DEPLOY_ENABLED:
            result["deploy"] = await self.ctx.run_sync(self._deploy)
        return result

    @staticmethod
    def _deploy():
        """Деплой собранного конфига. Ошибки не поднимаются: цикл не ломаем."""
        from deploy_config import deploy, deploy_multi

        templates = [t for t in DEPLOY_TEMPLATES.replace(",", " ").split() if t]
        try:
            if templates:
                LOGGER.info("Деплой (мульти): repo=%s templates=%s", GH_DEPLOY_REPO, templates)
                res = deploy_multi(
                    repo=GH_DEPLOY_REPO, templates=templates, path=DEPLOY_PATH, silent=True,
                )
            else:
                LOGGER.info("Деплой: repo=%s template=%s", GH_DEPLOY_REPO, DEPLOY_TEMPLATE)
                res = deploy(
                    repo=GH_DEPLOY_REPO, template=DEPLOY_TEMPLATE,
                    path=DEPLOY_PATH, silent=True,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Деплой не выполнен: %s", exc)
            return None
        if res.get("ok"):
            LOGGER.info("Деплой выполнен: %s", redact(res.get("url") or res.get("deployed")))
        else:
            LOGGER.warning("Деплой не выполнен: %s", res.get("error"))
        # В отчёте наружу уходит тоже замаскированным: сырой URL с токеном
        # не должен попасть ни в лог, ни в stdout (--json).
        for item in res.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                item["url"] = redact(item["url"])
        return res
