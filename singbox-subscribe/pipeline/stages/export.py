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
        want_deploy = get_settings().deploy.enabled if deploy is None else bool(deploy)

        # --- умершие выпадают из ротации ------------------------------------
        result["purged"] = await db.purge_dead(get_settings().core.purge_stable_below)
        LOGGER.info("Из проверки исключено серверов: %d", result["purged"])

        # --- whitelist: единственная файловая выгрузка ------------------------
        # blacklist.txt и merge.txt не пишутся: их состояние живёт в базе.
        if self.ctx.setting("export_lists", True):
            result["whitelist"] = await db.export_whitelist(
                Path(self.ctx.setting("whitelist_file")),
                global_tag=self.ctx.setting("global_tag", "Global"),
                # Лучшие и резервные: файл whitelist.txt пишется именно здесь,
                # и без этих параметров метки в него не попадали.
                best_top=int(self.ctx.setting("best_top", 0) or 0),
                best_reserve=int(self.ctx.setting("best_reserve", 0) or 0),
            )
            LOGGER.info("Экспорт из базы: whitelist %d", result["whitelist"])

        # --- деплой конфига в GitHub ----------------------------------------
        if want_deploy:
            result["deploy"] = await self.ctx.run_sync(self._deploy)
        return result

    @staticmethod
    def _deploy():
        """Публикация конфига: локально и/или в GitHub. Ошибки не поднимаются.

        Два способа независимы. Локальный пишет файл на диск этой машины и
        работает БЕЗ токенов и репозитория; GitHub-овый пушит в репозиторий.
        Включены могут быть оба сразу — тогда собирается один и тот же конфиг
        и публикуется двумя путями (см. deploy.local и deploy.repo).
        """
        from deploy_config import (deploy, deploy_multi, local_deploy,
                                   local_deploy_multi)

        # Настройки читаются здесь, в точке использования. Константы на
        # уровне модуля вернули бы имя каждой настройки в коде вторым разом —
        # ровно ту связь, которую убирает config.settings.
        settings = get_settings().deploy
        repo = settings.repo
        path = settings.path
        # deploy.templates приходит и списком (так его пишет config.yaml:
        # templates: [all]), и строкой через запятую (старая форма .env).
        # Приводим к списку, прежде чем звать .split().
        raw_templates = settings.templates
        if isinstance(raw_templates, (list, tuple, set)):
            templates = [str(t).strip() for t in raw_templates if str(t).strip()]
        else:
            templates = [t for t in str(raw_templates or "").replace(",", " ").split() if t]

        results: dict = {}
        want_local = bool(settings.local)
        # GitHub-деплой без репозитория — это не «деплой с ошибкой», а
        # незаполненная настройка: ругаться в каждом цикле незачем.
        want_github = bool(str(repo or "").strip())

        if want_local:
            local_path = settings.local_path
            try:
                if templates:
                    LOGGER.info("Деплой локальный: dir=%s templates=%s",
                                local_path, templates)
                    results["local"] = local_deploy_multi(
                        templates=templates, path=local_path,
                    )
                else:
                    LOGGER.info("Деплой локальный: dir=%s template=%s",
                                local_path, settings.template)
                    results["local"] = local_deploy(
                        path=Path(str(local_path)) / str(settings.template),
                        template=settings.template,
                    )
                res = results["local"]
                if res.get("ok"):
                    LOGGER.info("Локальный деплой выполнен: %s",
                                res.get("dir") or res.get("path"))
                else:
                    LOGGER.warning("Локальный деплой не выполнен: %s", res.get("error"))
                for item in res.get("results") or []:
                    if isinstance(item, dict) and not item.get("ok"):
                        LOGGER.warning("  не записан %s: %s",
                                       item.get("template") or "?",
                                       item.get("error") or "причина не указана")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Локальный деплой не выполнен: %s", exc)

        if not want_github:
            if not want_local:
                # Ни одного способа публикации: это не ошибка, а выключенный
                # деплой. Скажем один раз в логе, чтобы отсутствие файла не
                # выглядело загадкой.
                LOGGER.info("Деплой выключен: ни deploy.local, ни deploy.repo не заданы")
                return None
            return results

        try:
            if templates:
                LOGGER.info("Деплой (мульти): repo=%s templates=%s", repo, templates)
                res = deploy_multi(
                    repo=repo, templates=templates, path=path, silent=True,
                )
            else:
                template = settings.template
                LOGGER.info("Деплой: repo=%s template=%s", repo, template)
                res = deploy(
                    repo=repo, template=template,
                    path=path, silent=True,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Деплой не выполнен: %s", exc)
            results["github"] = None
            return results if want_local else None
        results["github"] = res
        failed_items = [i for i in (res.get("results") or [])
                        if isinstance(i, dict) and not i.get("ok")]
        if res.get("ok"):
            LOGGER.info("Деплой выполнен: %s", redact(res.get("url") or res.get("deployed")))
        else:
            LOGGER.warning("Деплой не выполнен: %s", res.get("error"))
        # Причина сбоя каждого шаблона — отдельно: сводное сообщение
        # говорит ЧИСЛО упавших, но не говорит ПОЧЕМУ, а причины у всех
        # шаблонов обычно общие и показывают, что чинить.
        for item in failed_items:
            LOGGER.warning(
                "  не выложен %s -> %s: %s",
                item.get("template") or "?",
                item.get("path") or "?",
                item.get("error") or "причина не указана",
            )
        # В отчёте наружу уходит тоже замаскированным: сырой URL с токеном
        # не должен попасть ни в лог, ни в stdout (--json).
        for item in res.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                item["url"] = redact(item["url"])
        # Возвращаем ОБА результата, если работали оба способа: иначе отчёт
        # терял бы локальную часть и выглядел как «деплой не выполнялся».
        return results if want_local else res
