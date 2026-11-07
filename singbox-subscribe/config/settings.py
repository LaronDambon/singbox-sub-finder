"""Конфигурация проекта: точка сборки настроек для всего кода.

ЗДЕСЬ НЕТ ЗНАЧЕНИЙ. Обычные настройки живут в config.yaml, их схема — в
config/config.py. Секреты (токены и ключи API) остаются в .env, как и были:
этот файл в .gitignore, и он единственное место, где лежат секреты.

    from config.settings import get_settings

    s = get_settings()
    s.reach.global_tag          # историческое имя группы
    s.reachability.timeout      # то же самое: имя инструмента
    s["singbox.checkers.reachability.timeout"]   # полный путь
    s["core"]                   # группа словарём
    s["pipeline.batch_size"]    # старое имя секции — перенаправляется

ПОЧЕМУ НЕСКОЛЬКО ФОРМ, А НЕ ОДНА. Исторические имена групп (reach, urltest,
sub, core, log) уже разошлись по коду сотней обращений, а новый конфиг
называет вещи своими именами (reachability, url_probe). Ломать вызовы ради
красоты имени дороже, чем дать обе формы: s.reach и s.reachability — один и
тот же объект группы. Где переезд развёл одну старую секцию по разным
инструментам (pipeline.batch_size -> url_probe.batch_size), работает таблица
_FIELD_MOVES.

СЕКРЕТЫ. Токены и ключи помечены в схеме как secret: они есть в объекте
настроек — коду они нужны, — но вырезаны из as_dict() и замаскированы в
describe(). Печатать объект настроек целиком безопасно.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from config.config import (
    BY_LEGACY,
    BY_PATH,
    BY_SHORT,
    CONFIG_FILE,
    ENV_FILE,
    GROUPS,
    ROOT,
    Config,
    ConfigError,
    Group,
    as_bool,
    as_float,
    as_int,
    as_list,
    as_path,
    as_str,
    as_table,
    build_config,
    get_config,
    init_config,
    logging_name,
    parse_level,
    read_env_file,
    render_config,
    reset_config,
    write_config,
)

# ============================================================================
# ГРУППЫ: короткое имя -> путь в схеме
# ============================================================================

#: Группа, поле -> поле в НОВОЙ схеме. Нужно там, где переезд развёл одну
#: старую секцию по разным инструментам: pipeline.batch_size теперь
#: принадлежит url_probe, а sub.concurrency — это pipeline.download_concurrency.
_FIELD_MOVES: dict[tuple[str, str], str] = {
    ("pipeline", "checkers"): "singbox.enabled_checkers",
    ("pipeline", "check_workers"): "singbox.check_workers",
    ("pipeline", "batch_size"): "url_probe.batch_size",
    ("pipeline", "check_timeout"): "url_probe.timeout",
    ("pipeline", "batch_startup"): "url_probe.batch_startup",
    ("pipeline", "max_attempts"): "url_probe.max_attempts",
    ("pipeline", "max_retries"): "url_probe.max_retries",
    ("sub", "concurrency"): "pipeline.download_concurrency",
    ("paths", "custom_checkers_dir"): "pipeline.custom_checkers_dir",
    ("paths", "reachability_targets_file"): "reachability.urls_file",
    ("paths", "sing_box_path"): "singbox.path",
    ("urltest", "url"): "url_probe.url",
    ("urltest", "batch_size"): "url_probe.batch_size",
}

#: Как группа называется в схеме (слева) и как её зовёт старый код (справа).
#: Оба имени работают: это НЕ копии значений, а два ярлыка одного объекта.
_ALIASES: dict[str, str] = {
    "core": "core",
    "paths": "paths",
    "pipeline": "pipeline",
    "log": "log",
    "web": "web",
    "deploy": "deploy",
    "services": "services",
    "singbox": "singbox",
    "secrets": "secrets",
    "reach": "singbox.checkers.reachability",
    "reachability": "singbox.checkers.reachability",
    "url_probe": "singbox.checkers.url_probe",
    "urltest": "singbox.checkers.url_probe",
    "api_service": "singbox.checkers.api_service",
    "service": "singbox.checkers.api_service",
    "browser_service": "singbox.checkers.browser_service",
    "country": "singbox.checkers.country",
    "speed": "singbox.checkers.speed",
    "sub": "pipeline",
}


def _register_fallbacks() -> None:
    """Переносит _FIELD_MOVES в реестр схемы.

    Обращение к группе точкой (s.pipeline.checkers) идёт мимо Settings — прямо
    в объект группы, — поэтому карта переездов должна лежать там же, где
    группа: иначе старое имя работало бы через подписку и падало через точку.
    """
    from config import config as _cfg

    for (section, field), target in _FIELD_MOVES.items():
        group = _ALIASES.get(section, section)
        _cfg._FALLBACK.setdefault(group, {})[field] = _cfg.get_config().value(
            target, _MISSING
        )


def group_path(name: str) -> str | None:
    """Короткое имя группы -> её путь в схеме."""
    if name in _ALIASES:
        return _ALIASES[name]
    for group in GROUPS:
        if group == name or group.rsplit(".", 1)[-1] == name:
            return group
    return None


class _Missing:
    """Маркер «значения нет» — чтобы отличить его от None."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()

#: Последние уровни всех настроек: нужны для проверки «есть ли такая настройка».
_SHORT_KEYS = {f.short.split(".")[-1] for f in BY_PATH.values()}

#: Реестр переездов заполняется один раз за процесс.
_registered = [False]


class Settings:
    """Настройки процесса: группы, доступные точкой и подпиской.

    Обёртка над Config: Config знает значения, Settings знает, как их
    называет код. Раньше это был дата-класс с тринадцатью секциями и
    отдельной таблицей имён; теперь таблица имён одна и живёт в схеме.
    """

    __slots__ = ("_config",)

    def __init__(self, config: Config) -> None:
        object.__setattr__(self, "_config", config)

    # --------------------------------------------------------------- доступ
    @property
    def config(self) -> Config:
        """Исходный объект конфига: значения, источники, describe()."""
        return self._config

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        path = group_path(name)
        if path is not None:
            return self._config.group(path)
        raise AttributeError(name)

    def __getitem__(self, key: str) -> Any:
        """Подписка: группа по имени, значение — по короткому или полному пути.

        Различаем по СХЕМЕ, а не по наличию точки в имени: группа
        "singbox.checkers.reachability" тоже содержит точки. Отдельно
        разбирается старая форма "секция.поле" — её знает _FIELD_MOVES.
        """
        path = group_path(key)
        if path is not None:
            return self._config.group(path)
        if "." in key:
            section, _, field = key.partition(".")
            moved = _FIELD_MOVES.get((section, field))
            if moved is not None:
                return self._config.value(moved, _MISSING)
        value = self._config.value(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if group_path(key) is not None:
            return True
        return key in BY_PATH or key in BY_SHORT or key.split(".")[-1] in _SHORT_KEYS

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def setting(self, path: str, default: Any = None) -> Any:
        """Значение по короткому или полному пути (без исключений)."""
        return self._config.value(path, default)

    # --------------------------------------------------------------- вывод
    def as_dict(self) -> dict[str, Any]:
        """Все НЕсекретные значения. Секреты вырезаны намеренно."""
        return self._config.as_dict()

    def describe(self) -> dict[str, Any]:
        """Значения с источниками; секреты замаскированы."""
        return self._config.describe()

    def legacy_dict(self) -> dict[str, Any]:
        """Значения под старыми именами .env — отчёт для `config show`.

        Нужен при переходе: видно, что именно ещё приходит из .env и что из
        этого пора перенести в config.yaml.
        """
        out: dict[str, Any] = {}
        for name, field in BY_LEGACY.items():
            value = self._config.value(field.short)
            out[name] = "***" if field.secret else value
        return out

    def origin(self) -> str:
        """Из каких файлов собраны настройки — для лога при старте."""
        files = [p.name for p in (CONFIG_FILE, ENV_FILE) if p.exists()]
        return ", ".join(files) if files else "значения по умолчанию"

    def __repr__(self) -> str:  # pragma: no cover — удобство отладки
        return f"<Settings {len(GROUPS)} групп, источник: {self.origin()}>"


__all__ = [
    "BY_LEGACY",
    "BY_PATH",
    "CONFIG_FILE",
    "Config",
    "ConfigError",
    "ENV_FILE",
    "Group",
    "Settings",
    "as_bool",
    "as_float",
    "as_int",
    "as_list",
    "as_path",
    "as_str",
    "as_table",
    "build_config",
    "get_config",
    "get_settings",
    "group_path",
    "init_config",
    "init_settings",
    "logging_name",
    "parse_level",
    "read_env_file",
    "render_config",
    "reset_config",
    "write_config",
]


def get_settings() -> Settings:
    """Настройки процесса. Собираются при первом обращении.

    Ленивое создание здесь — единственное место автосоздания во всём проекте:
    настройка логирования и разбор аргументов командной строки обязаны
    работать ДО инициализации. Раньше таких мест было по одному на каждую
    константу.
    """
    config = get_config()
    if not _registered[0]:
        _register_fallbacks()
        _registered[0] = True
    return Settings(config)


def init_settings(**overrides: Any) -> Settings:
    """Собирает настройки ОДИН раз и запоминает их на процесс.

    Вызывается точкой входа (main.py / pipeline.__main__ / тест). Всё
    остальное берёт настройки отсюда, а не импортирует константы.
    """
    return Settings(init_config(**overrides))
