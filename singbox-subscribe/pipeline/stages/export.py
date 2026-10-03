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

from config.settings import setting
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
        want_deploy = setting("DEPLOY_ENABLED") if deploy is None else bool(deploy)

        # --- умершие выпадают из ротации ------------------------------------
        result["purged"] = await db.purge_dead(setting("PURGE_STABLE_BELOW"))
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
        """Деплой собранного конфига. Ошибки не поднимаются: цикл не ломаем."""
        from deploy_config import deploy, deploy_multi

        # Настройки читаются здесь, в точке использования. Константы на
        # уровне модуля вернули бы имя каждой настройки в коде вторым разом —
        # ровно ту связь, которую убирает config.settings.
        repo = setting("GH_DEPLOY_REPO")
        path = setting("DEPLOY_PATH")
        templates = [t for t in setting("DEPLOY_TEMPLATES").replace(",", " ").split() if t]
        try:
            if templates:
                LOGGER.info("Деплой (мульти): repo=%s templates=%s", repo, templates)
                res = deploy_multi(
                    repo=repo, templates=templates, path=path, silent=True,
                )
            else:
                template = setting("DEPLOY_TEMPLATE")
                LOGGER.info("Деплой: repo=%s template=%s", repo, template)
                res = deploy(
                    repo=repo, template=template,
                    path=path, silent=True,
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
