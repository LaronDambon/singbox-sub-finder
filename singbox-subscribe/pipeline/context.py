"""Контекст pipeline — общий «ящик» для всех этапов.

Один объект создаётся в ``main.py` и проходит через все этапы: чекеры получают
из него базу, настройки и логгер, а движок — список чекеров и счётчики.

Все настройки приходят из окружения (config/env.py), но любое значение можно
переопределить прямо в коде:

    PipelineContext(settings={"checkers": "url_probe", "batch_size": 50})

Атрибуты доступны и как ``ctx.settings.batch_size``, и как
``ctx.setting("batch_size", 100)``, и как обычный словарь ``ctx.settings["..."]`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.checkers.base import Checker
    from pipeline.database import Database


class Settings(dict):
    """Словарь настроек с доступом через точку и через чтение окружения."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover
        self[name] = value

    def merged(self, overrides: dict[str, Any] | None) -> "Settings":
        result = Settings(self)
        result.update({k: v for k, v in (overrides or {}).items() if v is not None})
        return result


def default_settings() -> Settings:
    """Настройки pipeline из окружения (значения по умолчанию — в config/env)."""
    from config import env

    return Settings(
        # файлы
        urls_file=env.URLS_FILE,
        merge_file=env.MERGE_FILE,
        whitelist_file=env.WHITELIST_FILE,
        blacklist_file=env.BLACKLIST_FILE,
        # проверка
        checkers=env.PIPELINE_CHECKERS,
        batch_size=env.PIPELINE_BATCH_SIZE,
        check_workers=env.PIPELINE_CHECK_WORKERS,
        check_timeout=env.PIPELINE_CHECK_TIMEOUT,
        enrich_timeout=env.PIPELINE_ENRICH_TIMEOUT,
        batch_startup=env.PIPELINE_BATCH_STARTUP,
        max_attempts=env.PIPELINE_MAX_ATTEMPTS,
        max_retries=env.PIPELINE_MAX_RETRIES,
        queue_persist=env.PIPELINE_QUEUE_PERSIST,
        # поиск
        discovery=env.PIPELINE_DISCOVERY,
        discovery_concurrency=env.PIPELINE_DISCOVERY_CONCURRENCY,
        # экспорт
        export_lists=env.PIPELINE_EXPORT_LISTS,
        write_merge=env.PIPELINE_WRITE_MERGE,
        # sing-box
        singbox_path=Path(env.SING_BOX_PATH),
        global_tag=env.REACHABILITY_GLOBAL_TAG,
    )


@dataclass
class PipelineContext:
    """Всё, чем пользуются этапы и чекеры."""

    settings: Settings = field(default_factory=default_settings)
    db: "Database | None" = None
    checkers: list["Checker"] = field(default_factory=list)
    logger: Any = None
    # Счётчики прогона — наполняются движком, доступны чекерам и отчёту.
    stats: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.settings, Settings):
            self.settings = Settings(self.settings or {})
        if self.logger is None:
            self.logger = get_logger("pipeline")

    def setting(self, name: str, default: Any = None) -> Any:
        value = self.settings.get(name)
        return default if value is None else value

    # ------------------------------------------------------------ помощники
    @staticmethod
    async def run_sync(func, *args, **kwargs):
        """Блокирующий вызов в потоке, не блокируя event loop.

        Этапы (например экспорт с деплоем в GitHub) используют его для
        синхронного кода. Проверяющим алгоритмам он доступен как
        ``ctx.run_sync(...)`` в CheckContext.
        """
        import asyncio
        from functools import partial

        loop = asyncio.get_running_loop()
        if kwargs:
            func = partial(func, **kwargs)
        return await loop.run_in_executor(None, func, *args)

    @property
    def lines_per_batch(self) -> int:
        return max(1, int(self.setting("batch_size", 100)))

    @property
    def workers_count(self) -> int:
        return max(1, int(self.setting("check_workers", 2)))

    @property
    def checker_names(self) -> list[str]:
        return [c.name for c in self.checkers]

    def describe(self) -> dict:
        """Краткое описание конфигурации прогона (для лога в начале)."""
        return {
            "checkers": self.checker_names,
            "batch_size": self.lines_per_batch,
            "workers": self.workers_count,
            "discovery": bool(self.setting("discovery", True)),
            "db": str(self.db.path) if self.db else None,
        }
