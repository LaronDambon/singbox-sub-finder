"""Ядро конфигурации: схема, слои, значения и запись YAML.

ЗАЧЕМ ЭТОТ МОДУЛЬ
-----------------
Настройки проекта лежали в двух местах: .env (плоские ИМЯ=значение) и
config/settings.py (код, который эти имена знал). Читать такое можно,
а РЕДАКТИРОВАТЬ — нет: у настройки нет ни описания, ни единицы измерения,
ни допустимого диапазона, ни признака «это секрет». Строка вида
REACHABILITY_MAX_PING_MS=500 ничего не говорит о том, что это порог
задержки до цели и что он измеряется в миллисекундах.

Как устроено теперь. Есть ОДНА схема — таблица _SCHEMA ниже, — и из неё
выводится всё остальное:

  * разбор config.yaml (обычный конфиг, коммитится);
  * разбор .env (секреты и устаревшие имена, читается как есть);
  * значения по умолчанию, типы и приведение значений;
  * файл, который пишет `python -m pipeline config init` — с комментариями,
    группами и документацией прямо над каждой настройкой.

У каждой настройки в схеме есть:

    группа   — куда её положить в YAML; это и есть «разделение по файлам
               на инструменты»: group="singbox.checkers.reachability";
    ключ     — имя последнего уровня (urls, max_ping_ms, concurrency);
    тип      — str/int/float/bool/list/table/path/level, приводится на чтении;
    default  — значение по умолчанию;
    описания — «что это», «единица измерения», «допустимые значения»;
    secret   — True, если значение НЕЛЬЗЯ печатать (ключи, токены).

ГРУППЫ — ЭТО ПУТЬ В YAML
------------------------
Группа singbox.checkers.reachability превращается в

    singbox:
      checkers:
        reachability:
          urls: [...]
          max_ping_ms: 500

Приложение читает значение по короткому пути (reachability.max_ping_ms)
или по полному (singbox.checkers.reachability.max_ping_ms). Работает и
точкой, и подпиской:

    settings.reachability.max_ping_ms
    settings["reachability.max_ping_ms"]

СЕКРЕТЫ ЖИВУТ В .env
--------------------
Ключи и токены (GH_DEPLOY_TOKEN, GH_READ_TOKEN, GEMINI_API_KEY,
OPENROUTER_API_KEY) остаются в .env, как и были: этот файл в .gitignore,
и он единственное место, где лежат секреты. В config.yaml их нет и быть
не должно — конфиг коммитится.

В схеме у таких настроек стоит secret=True, и это НЕ способ хранения, а
страховка от утечки: значение вырезается из as_dict() и заменяется на ***
в describe(). Объект настроек печатают целиком при отладке, и токен не
должен уехать в лог только потому, что кто-то вывел конфиг.

ПРИОРИТЕТ СЛОЁВ (сверху вниз)
-----------------------------
    1. явные перекрытия из кода (аргументы CLI, тесты);
    2. переменные системного окружения (иначе значение не подменить в CI);
    3. .env (секреты и значения, оставшиеся от прежней установки);
    4. config.yaml;
    5. значение по умолчанию из схемы.

ИСКЛЮЧЕНИЕ ДЛЯ СЕКРЕТОВ: .env для них сильнее config.yaml. Секретов в
config.yaml нет, поэтому спорить им не о чем, но правило важное — если
секрет когда-нибудь попадёт в конфиг по ошибке, .env всё равно победит.

Неизвестный ключ в YAML — предупреждение, а не падение: конфиг правят
руками, и опечатка не должна ронять службу.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

# ============================================================================
# ПУТИ
# ============================================================================

ROOT = Path(__file__).resolve().parent.parent          # …/singbox-subscribe

#: Где лежит код, а где — изменяемые данные. Разложено по разным местам
#: намеренно: программу обновляют (и она живёт в /opt или Program Files),
#: а данные обновление трогать не должно. Всё это — в config/layout.py,
#: чтобы у установщика, preflight и самой программы был ОДИН ответ.
from .layout import PACKAGE_ROOT, config_file, data_root, env_file

DATA_ROOT = data_root()                                # корень изменяемых данных

#: Прежнее имя корня: у СТАРОЙ установки это клон репозитория, у новой —
#: каталог данных в профиле пользователя. Оставлено, потому что от него
#: считаются пути к рабочим файлам в схеме ниже, и потому что им пользуются
#: скрипты, которым нужен «каталог, где всё лежит».
PROJECT_ROOT = DATA_ROOT

#: Основной конфиг: то, что можно править и коммитить.
CONFIG_FILE = config_file()
#: Секреты (ключи, токены) живут ТОЛЬКО здесь: файл в .gitignore.
ENV_FILE = env_file()

#: Значения, которыми может быть обёрнуто значение в .env.
_QUOTES = '"', "'"


# ============================================================================
# ПРИВЕДЕНИЕ ТИПОВ
# ============================================================================

_TRUE = {"1", "true", "yes", "on", "да", "вкл"}
_FALSE = {"0", "false", "no", "off", "нет", "выкл", ""}

#: Человекочитаемые имена уровней -> числа logging.
LEVELS: dict[str, int] = {
    "CRITICAL": 50, "FATAL": 50, "ERROR": 40, "WARN": 30, "WARNING": 30,
    "INFO": 20, "DEBUG": 10, "NOTSET": 0, "OFF": 100,
}


def as_bool(value: Any, default: bool = False) -> bool:
    """Значение -> bool. Понимает 1/0, true/false, yes/no, on/off, да/нет."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def as_int(value: Any, default: int = 0) -> int:
    """Значение -> int. Мусор не роняет процесс, а даёт default."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    """Значение -> float. Мусор не роняет процесс, а даёт default."""
    if isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def as_list(value: Any, default: Iterable[Any] = ()) -> list:
    """Значение -> список.

    Понимает настоящий список YAML, строку через запятую
    ("cloudflare,ovh" -> ["cloudflare", "ovh"]) и одиночное значение.
    Пустая строка и None дают пустой список, а не [""].
    """
    if value is None:
        return list(default)
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if str(item).strip() != ""]
    text = str(value).strip()
    if not text:
        return []
    if text.lower() in {"none", "-", "off"}:
        return []
    return [item.strip() for item in text.replace(";", ",").split(",") if item.strip()]


def as_table(value: Any, default: Mapping | None = None) -> dict:
    """Значение -> словарь (таблица «ключ -> значение»)."""
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    if isinstance(value, str) and value.strip():
        # Форма "fast=speed-fast, ok=speed-ok": из .env иначе не записать.
        out: dict[str, Any] = {}
        for chunk in value.replace(";", ",").split(","):
            if "=" in chunk:
                key, _, val = chunk.partition("=")
                out[key.strip()] = val.strip()
        return out or dict(default or {})
    return dict(default or {})


def parse_level(value: Any, default: str = "INFO") -> int:
    """Строка "DEBUG" / "info" / число 25 -> числовой уровень logging.

    Неизвестное имя не роняет процесс — берётся значение по умолчанию:
    опечатка в конфиге не должна мешать запуску службы.
    """
    raw = str(value if value is not None else "").strip().upper()
    if not raw:
        raw = str(default).upper()
    if raw.isdigit():
        return int(raw)
    return LEVELS.get(raw, LEVELS.get(str(default).upper(), LEVELS["INFO"]))


def as_path(value: Any, base: Path = PROJECT_ROOT) -> Path:
    """Значение -> Path. Относительный путь считается от корня репозитория."""
    text = str(value or "").strip()
    if not text:
        return base
    raw = Path(os.path.expanduser(os.path.expandvars(text)))
    return raw if raw.is_absolute() else (base / raw)


def resolve_binary(value: Any, base: Path = PROJECT_ROOT) -> Path:
    """Путь к БИНАРНИКУ sing-box, а не к папке поставки.

    Настройка singbox.path исторически указывала на папку (в ней лежат и
    sing-box.exe, и sing-box, и libcronet.so). Но запускать надо файл:
    create_subprocess_exec на каталоге падает с WinError 5 «Отказано в
    доступе», и выглядит это как проблема прав, хотя это неверный путь.

    Принимаем и папку, и файл:
      * папка   -> ищем внутри sing-box.exe (Windows) или sing-box (иначе);
      * файл    -> берём как есть;
      * пусто   -> папка поставки по умолчанию.
    """
    # База — корень репозитория: в config.yaml пути пишутся относительно
    # него (singbox-subscribe/sing-box), и повторно приклеивать их к папке
    # нельзя — получалось singbox-subscribe\sing-box\singbox-subscribe\sing-box.
    raw = as_path(value, PROJECT_ROOT)
    if raw.is_dir():
        candidates = (
            ("sing-box.exe", "sing-box")
            if os.name == "nt"
            else ("sing-box", "sing-box.exe")
        )
        for name in candidates:
            candidate = raw / name
            if candidate.exists():
                return candidate
        # Ничего не нашли — возвращаем ожидаемое имя: сообщение об ошибке
        # должно называть файл, которого нет, а не папку.
        return raw / candidates[0]
    return raw


_CASTS = {
    "str": as_str,
    "int": as_int,
    "float": as_float,
    "bool": as_bool,
    "list": as_list,
    "table": as_table,
    "path": as_path,
    "binary": resolve_binary,
    "level": parse_level,
}

# ============================================================================
# СХЕМА
# ============================================================================

@dataclass(frozen=True)
class Field:
    """Одна настройка: где лежит, какого типа, что значит."""

    group: str          # путь в YAML: singbox.checkers.reachability
    key: str            # имя последнего уровня: max_ping_ms
    kind: str           # str | int | float | bool | list | table | path | level
    default: Any
    doc: str = ""       # что это
    unit: str = ""      # единица измерения (сек, мс, МБ…)
    values: str = ""    # допустимые значения словами
    secret: bool = False
    legacy: tuple = ()  # имена в старом .env

    @property
    def path(self) -> str:
        """Полный путь настройки в YAML."""
        return f"{self.group}.{self.key}" if self.group else self.key

    @property
    def short(self) -> str:
        """Короткий путь для кода: reachability.max_ping_ms.

        Совпадает с полным, если имя группы уже короткое (core, speed, log).
        Именно по нему настройку ищут через settings["..."].
        """
        parts = self.path.split(".")
        if len(parts) <= 2:
            return self.path
        # Служебные ветки (singbox.checkers) в имени не нужны: чекер и так
        # знает, что он чекер. Оставляем "инструмент.настройка".
        return ".".join(parts[-2:])

    @property
    def env_name(self) -> str:
        """Ведущее имя в старом .env (первое из legacy)."""
        return self.legacy[0] if self.legacy else ""

    def cast(self, value: Any) -> Any:
        """Приводит значение к типу поля. Мусор -> значение по умолчанию."""
        if self.kind == "path":
            return as_path(value) if value is not None else as_path(self.default)
        if self.kind == "table":
            return as_table(value, self.default)
        fn = _CASTS.get(self.kind, as_str)
        return fn(value, self.default)

    def comment(self) -> str:
        """Строка-комментарий, которая встаёт над настройкой в YAML.

        Имена из старого .env здесь НЕ упоминаются: обычные настройки
        читаются из config.yaml и больше ниоткуда, а .env остался хранилищем
        секретов. Строка «(в .env это ИМЯ)» выглядела как рабочая инструкция
        и подсказывала править то, что давно ничего не решает.

        legacy остаётся в схеме — по нему читаются СЕКРЕТЫ (.env) и ключи
        командной строки, но в человекочитаемый конфиг это не выносится.
        """
        parts = [self.doc] if self.doc else []
        if self.unit:
            parts.append(f"({self.unit})")
        if self.values:
            parts.append(f"[{self.values}]")
        if self.secret:
            parts.append("— СЕКРЕТ")
        return "\n".join(parts).strip() or self.key


#: Таблица настроек. ЕДИНСТВЕННОЕ место, где настройка определена.
#:
#: Порядок строк = порядок групп и настроек в сгенерированном конфиге.
#:
#: legacy — имена из старого .env. Обычные настройки по ним БОЛЬШЕ НЕ
#: читаются: config.yaml — единственный их источник. legacy остался ради
#: секретов (они и правда живут в .env) и ключей командной строки, поэтому
#: старые установки не отваливаются, но подмешать устаревшее имя в конфиг
#: уже нельзя.
_SCHEMA: tuple = (

    # ------------------------------- CORE: центральная база, зоны stable
    Field("core", "purge_stable_below", "int", 0, "Нижний порог stable: серверы ниже этого значения не импортируются в проверку", legacy=("PURGE_STABLE_BELOW",)),
    Field("core", "new_server_stable", "int", 5, "stable нового сервера до первого удачного пинга: сколько неудач он переживёт", legacy=("NEW_SERVER_STABLE",)),
    Field("core", "shield_cycles", "int", 96, "«Щит»: после удачного пинга stable поднимается не ниже этого значения", legacy=("SHIELD_CYCLES",)),
    Field("core", "best_top", "int", 3, "Сколько лучших в каждой группе получают тег [<группа>-best]", legacy=("BEST_TOP",)),
    Field("core", "best_reserve", "int", 3, "Сколько следующих за лучшими получают тег [<группа>-reserve]", legacy=("BEST_RESERVE",)),

    # --------------- SINGBOX: ядро проверки, общее для всех инструментов
    # Умолчание — минимум, который реально нужен: жив ли сервер, куда он
    # дотягивается и не закрыт ли сервис по региону. Остальные чекеры
    # (country, service_access, speed) остаются доступными: их достаточно
    # вписать в этот список, но по умолчанию они не тратят время.
    Field("singbox", "enabled_checkers", "list",
          ["url_probe", "reachability", "api_service", "browser_service"],
          "Какие проверяющие алгоритмы запускать и в каком порядке. Это список"
          " включённых инструментов; настройки каждого — в singbox.checkers.<имя>",
          values="url_probe, country, reachability, api_service, browser_service, "
                 "service_access, speed, all, none",
          legacy=("PIPELINE_CHECKERS",)),
    Field("singbox", "check_workers", "int", 2, "Сколько процессов sing-box работают ОДНОВРЕМЕННО (главный рычаг скорости)", legacy=("PIPELINE_CHECK_WORKERS",)),
    Field("singbox", "port", "int", 7891, "Базовый порт mixed-inbound sing-box: каждый батч берёт свой свободный", legacy=("SING_BOX_PORT",)),
    Field("singbox", "path", "binary", ROOT / "sing-box",
          "Путь к sing-box: можно указать папку поставки (бинарник найдётся сам)"
          " или файл напрямую", legacy=("SING_BOX_PATH",)),

    # ------------------------- url_probe: жив ли сервер (решающий чекер)
    Field("singbox.checkers.url_probe", "url", "str", "https://cp.cloudflare.com/gen_204", "URL проверки доступности: лёгкий 204-ответ", legacy=("URLTEST_URL",)),
    Field("singbox.checkers.url_probe", "batch_size", "int", 100, "Сколько серверов уходит в один запуск sing-box", legacy=("URLTEST_BATCH_SIZE", "PIPELINE_BATCH_SIZE")),
    Field("singbox.checkers.url_probe", "timeout", "float", 20.0, "Запас времени сверх расчётного дедлайна батча", unit="сек", legacy=("PIPELINE_CHECK_TIMEOUT",)),
    Field("singbox.checkers.url_probe", "batch_startup", "float", 10.0, "Нижняя граница дедлайна батча (время на запуск sing-box)", unit="сек", legacy=("PIPELINE_BATCH_STARTUP",)),
    Field("singbox.checkers.url_probe", "max_attempts", "int", 3, "Сколько раз повторять батч при сбое ЗАПУСКА sing-box", legacy=("PIPELINE_MAX_ATTEMPTS",)),
    Field("singbox.checkers.url_probe", "max_retries", "int", 3, "Сколько раз сервер возвращается в очередь, если вердикта не было", legacy=("PIPELINE_MAX_RETRIES",)),

    # ----------------- reachability: до каких целей дозванивается прокси
    Field("singbox.checkers.reachability", "enabled", "bool", True, "Профилировать, до каких целевых сайтов дотягивается прокси", legacy=("REACHABILITY_ENABLED",)),
    Field("singbox.checkers.reachability", "urls_file", "path", PROJECT_ROOT / "config" / "reachability_targets.json", "Файл со списком целей (urls) и правилами разбора их ответов", legacy=("REACHABILITY_TARGETS_FILE",)),
    Field("singbox.checkers.reachability", "concurrency", "int", 16, "Сколько прокси обрабатывать параллельно в одном процессе sing-box", legacy=("REACHABILITY_CONCURRENCY",)),
    Field("singbox.checkers.reachability", "per_proxy_concurrency", "int", 6, "Сколько целей ОДНОГО прокси опрашивается одновременно", legacy=("REACHABILITY_PER_PROXY_CONCURRENCY",)),
    Field("singbox.checkers.reachability", "timeout", "float", 20.0, "Таймаут одного целевого запроса. Мягкий по умолчанию: 6 с отсекали медленные, но рабочие прокси", unit="сек", legacy=("REACHABILITY_TIMEOUT",)),
    Field("singbox.checkers.reachability", "max_ping_ms", "int", 3000, "Максимальный TTFB, при котором цель считается достигнутой. 500 мс отсекали живые прокси с дальней задержкой", unit="мс", legacy=("REACHABILITY_MAX_PING_MS",)),
    Field("singbox.checkers.reachability", "body_bytes", "int", 64 * 1024, "Сколько байт тела ответа дочитывать для разбора правил", unit="байт", legacy=("REACHABILITY_BODY_BYTES",)),
    Field("singbox.checkers.reachability", "body_seconds", "float", 8.0, "Потолок времени на дочитывание тела ответа", unit="сек", legacy=("REACHABILITY_BODY_SECONDS",)),
    Field("singbox.checkers.reachability", "min_stable", "int", 0, "С какого stable начинать профилировать (ниже — не трогаем)", legacy=("REACHABILITY_MIN_STABLE",)),
    Field("singbox.checkers.reachability", "global_tag", "str", "Global", "Тег сервера, набравшего группу merge с этим именем", legacy=("REACHABILITY_GLOBAL_TAG",)),

    # -------------------- api_service: работает ли сервис с этого адреса
    Field("singbox.checkers.api_service", "enabled", "bool", True, "Проверять сервис HTTP-запросом с ключом (дёшево, без браузера)", legacy=("SERVICE_CHECK_ENABLED",)),
    Field("singbox.checkers.api_service", "min_stable", "int", 0, "С какого stable проверять сервисы", legacy=("SERVICE_CHECK_MIN_STABLE",)),
    Field("singbox.checkers.api_service", "concurrency", "int", 8, "Сколько серверов проверять параллельно", legacy=("SERVICE_CHECK_CONCURRENCY",)),
    Field("singbox.checkers.api_service", "tag_open", "str", "service", "Тег сервера, у которого сервис открыт", legacy=("SERVICE_CHECK_TAG_OPEN",)),

    # ------------------------------------------- country: страна сервера
    Field("singbox.checkers.country", "enabled", "bool", True, "Определять страну для серверов без известной страны", legacy=("COUNTRY_CHECK_ENABLED",)),
    Field("singbox.checkers.country", "concurrency", "int", 8, "Сколько прокси проверять параллельно", legacy=("COUNTRY_CHECK_CONCURRENCY",)),
    Field("singbox.checkers.country", "timeout", "float", 10.0, "Таймаут чтения одного geo-запроса (пробуется до 3 эндпоинтов)", unit="сек", legacy=("COUNTRY_CHECK_TIMEOUT",)),

    # ---------------------------- speed: замер скорости и доступ к сайту
    Field("singbox.checkers.speed", "enabled", "bool", True, "Мерить скорость и проверять доступ к целевому сайту", legacy=("SPEED_ENABLED",)),
    Field("singbox.checkers.speed", "site_url", "str", "https://gemini.google.com/", "Сайт для проверки доступа (Gemini режет дата-центровые и VPN-адреса)", legacy=("SPEED_SITE_URL",)),
    Field("singbox.checkers.speed", "sources", "list", [], "Источники замера; пусто = все (cloudflare, ovh, google, microsoft, github)", legacy=("SPEED_SOURCES",)),
    Field("singbox.checkers.speed", "probe_bytes", "int", 2 * 1024 * 1024, "Объём выкачки на КАЖДЫЙ источник", unit="байт", legacy=("SPEED_PROBE_BYTES",)),
    Field("singbox.checkers.speed", "upload_bytes", "int", 1024 * 1024, "Объём отдачи (адрес для POST есть только у Cloudflare)", unit="байт", legacy=("SPEED_UPLOAD_BYTES",)),
    Field("singbox.checkers.speed", "min_sources", "int", 2, "Сколько источников должно ответить, чтобы замер засчитали", legacy=("SPEED_MIN_SOURCES",)),
    Field("singbox.checkers.speed", "read_seconds", "float", 4.0, "Потолок ЧТЕНИЯ, а не размера: медленный сервер отдаёт своё и уходит", unit="сек", legacy=("SPEED_READ_SECONDS",)),
    Field("singbox.checkers.speed", "min_mbps", "float", 1.0, "Ниже этого — speed-slow", unit="МБ/с", legacy=("SPEED_MIN_MBPS",)),
    Field("singbox.checkers.speed", "good_mbps", "float", 5.0, "От этого и выше — speed-fast", unit="МБ/с", legacy=("SPEED_GOOD_MBPS",)),
    Field("singbox.checkers.speed", "concurrency", "int", 8, "Сколько серверов мерить параллельно", legacy=("SPEED_CONCURRENCY",)),
    Field("singbox.checkers.speed", "tag_prefix", "str", "speed-", "Префикс тегов скорости", legacy=("SPEED_TAG_PREFIX",)),
    Field("singbox.checkers.speed", "tier_tags", "table", {"fast": "speed-fast", "ok": "speed-ok", "slow": "speed-slow", "blocked": "speed-blocked", "geo": "speed-geo"}, "Тег для каждой категории скорости", legacy=("SPEED_TAG_FAST", "SPEED_TAG_OK", "SPEED_TAG_SLOW", "SPEED_TAG_BLOCKED", "SPEED_TAG_GEO")),

    # ------------- browser_service: проверка сервиса настоящим браузером
    Field("singbox.checkers.browser_service", "enabled", "bool", True, "Включить браузерную проверку сервиса", legacy=("BROWSER_SERVICE_ENABLED",)),
    Field("singbox.checkers.browser_service", "service", "str", "gemini",
          "Имя сервиса из browser/services.py: задаёт URL, профиль сессии"
          " и правила разбора страницы (gemini, claude, chatgpt)", legacy=("BROWSER_SERVICE_NAME",)),
    Field("singbox.checkers.browser_service", "tag", "str", "gemini-chat",
          "Метка сервера, у которого сервис РАБОТАЕТ (проверено браузером)."
          " Имя сервиса и имя метки — разные вещи: сервис остаётся gemini,"
          " а метки показывают, КАКИМ транспортом доступ подтверждён:"
          " gemini-api — отвечает API (HTTP-проверка с ключом),"
          " gemini-chat — открывается сам чат в браузере", legacy=("BROWSER_SERVICE_TAG",)),
    Field("singbox.checkers.browser_service", "max_checks", "int", 0, "Лимит проверок за прогон; 0 = без лимита (доводит список до конца)", legacy=("PIPELINE_BROWSER_MAX_CHECKS",)),
    Field("singbox.checkers.browser_service", "pause", "float", 1.0, "Пауза между серверами: заходы последовательные, иначе стена трафика", unit="сек", legacy=("PIPELINE_BROWSER_PAUSE",)),

    # --------------------- PIPELINE: очередь, этапы, проверки-дополнения
    Field("pipeline", "discovery", "bool", True, "Искать новые серверы из подписок (асинхронно, расширяет базу)", legacy=("PIPELINE_DISCOVERY",)),
    Field("pipeline", "discovery_concurrency", "int", 6, "Сколько источников подписок качать одновременно на этапе поиска", legacy=("PIPELINE_DISCOVERY_CONCURRENCY",)),
    Field("pipeline", "download_concurrency", "int", 6, "Сколько источников качать параллельно при сборке merge.txt", legacy=("SUB_DOWNLOAD_CONCURRENCY",)),
    Field("pipeline", "enrich_timeout", "float", 180.0, "Таймаут чекеров-ДОПОЛНЕНИЙ (страна, достижимость, браузер)", unit="сек", legacy=("PIPELINE_ENRICH_TIMEOUT",)),
    Field("pipeline", "queue_persist", "bool", True, "Возвращать незавершённые записи очереди в работу при старте", legacy=("PIPELINE_QUEUE_PERSIST",)),
    Field("pipeline", "export_lists", "bool", True, "Экспортировать whitelist.txt после цикла", legacy=("PIPELINE_EXPORT_LISTS",)),
    Field("pipeline", "write_merge", "bool", True, "Записывать merge.txt (файл пула проверки) после наполнения очереди", legacy=("PIPELINE_WRITE_MERGE",)),
    Field("pipeline", "custom_checkers_dir", "path", ROOT / "pipeline" / "checkers" / "custom", "Папка с ПОЛЬЗОВАТЕЛЬСКИМИ проверяющими алгоритмами", legacy=("PIPELINE_CUSTOM_CHECKERS_DIR",)),

    # ----------------------------------- PATHS: файлы и каталоги проекта
    # Пути делятся на ДВЕ семьи, и это не формальность:
    #
    #   * ИЗМЕНЯЕМЫЕ ДАННЫЕ считаются от PROJECT_ROOT (= DATA_ROOT) — корня
    #     данных. У системной установки это профиль пользователя
    #     (%LOCALAPPDATA%\singbox-subs-finder, ~/.local/share/...), у старой,
    #     из клона репозитория, — сам клон. Обновление программы первый
    #     каталог не трогает: база и настройки переживают переустановку.
    #   * КОД и ШАБЛОНЫ считаются от ROOT (пакет singbox-subscribe). Они
    #     приезжают вместе с программой и меняются только вместе с ней.
    #
    # Поэтому база, подписки и логи лежат рядом с config.yaml (их видно и
    # можно править руками), а шаблоны проверок — внутри пакета.
    Field("paths", "urls_file", "path", PROJECT_ROOT / "config" / "subs" / "urls.json", "Список ссылок на подписки", legacy=("URLS_FILE",)),
    Field("paths", "merge_file", "path", PROJECT_ROOT / "source" / "merge.txt", "Файл пула проверки", legacy=("MERGE_FILE",)),
    Field("paths", "whitelist_file", "path", PROJECT_ROOT / "source" / "whitelist.txt", "Выгрузка живых серверов", legacy=("WHITELIST_FILE",)),
    Field("paths", "blacklist_file", "path", PROJECT_ROOT / "source" / "blacklist.txt", "Чёрный список", legacy=("BLACKLIST_FILE",)),
    Field("paths", "servers_db_file", "path", PROJECT_ROOT / "source" / "servers.db", "Центральная база серверов", legacy=("SERVERS_DB_FILE",)),
    Field("paths", "urltest_template", "path", ROOT / "config" / "urltest_template.json", "Шаблон конфига sing-box для проверки живости", legacy=("URLTEST_TEMPLATE",)),
    Field("paths", "countrytest_template", "path", ROOT / "config" / "countrytest_template.json", "Шаблон конфига sing-box для прочих проверок", legacy=("COUNTRYTEST_TEMPLATE",)),
    Field("paths", "config_template_dir", "path", PROJECT_ROOT / "config" / "templates", "Папка с шаблонами конфига для деплоя", legacy=("CONFIG_TEMPLATE_DIR",)),
    # Времянки (batch_*.json, refine_*.json, browser_*.json) живут в отдельном
    # каталоге .tmp_singbox/, а не рядом с бинарниками sing-box: в sing-box/
    # лежат сами sing-box.exe и libcronet.so, они под гитом, и сгенерированные
    # конфиги там выглядели как исходники. Весь .tmp_singbox/ в .gitignore.
    Field("paths", "sing_box_output_dir", "path", ROOT / ".tmp_singbox", "Куда sing-box пишет временные конфиги батчей", legacy=("SING_BOX_OUTPUT_DIR",)),

    # -------------------------------------------------- LOG: логирование
    # Логи — данные, а не код: они живут рядом с config.yaml, в каталоге
    # данных. Программу можно переустановить, журнал работы останется.
    Field("log", "dir_path", "path", PROJECT_ROOT / "logs", "Каталог логов", legacy=("LOG_DIR_PATH", "LOG_DIR")),
    Field("log", "level", "level", 20, "ПОРОГ: что попадает в файлы и консоль", values="DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF", legacy=("LOG_LEVEL",)),
    Field("log", "console_level", "level", 20, "Порог КОНСОЛИ (файлы можно подробнее, консоль тише)", values="DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF", legacy=("LOG_CONSOLE_LEVEL",)),
    Field("log", "file_name", "str", "app.log", "Главный файл (всё, что прошло порог)", legacy=("LOG_FILE_NAME",)),
    Field("log", "max_bytes", "int", 10 * 1024 * 1024, "Размер файла до ротации", unit="байт", legacy=("LOG_MAX_BYTES",)),
    Field("log", "backup_count", "int", 5, "Сколько копий хранит ротация", legacy=("LOG_BACKUP_COUNT",)),
    Field("log", "retention_days", "int", 14, "Файлы старше этого срока удаляются при старте; 0 = не удалять", unit="дней", legacy=("LOG_RETENTION_DAYS",)),
    Field("log", "per_level_files", "bool", True, "Отдельный файл на уровень (debug/info/warning/error)", legacy=("LOG_PER_LEVEL_FILES",)),
    Field("log", "format", "str", "text", "Формат записей", values="text, json", legacy=("LOG_FORMAT",)),
    Field("log", "throttle_limit", "int", 20, "Не больше N одинаковых сообщений за окно, дальше — одна сводка", legacy=("LOG_THROTTLE_LIMIT",)),
    Field("log", "throttle_window", "float", 60.0, "Окно защиты от «пулемёта»", unit="сек", legacy=("LOG_THROTTLE_WINDOW",)),
    Field("log", "capture_stdout", "bool", False, "Заворачивать print() и сторонние библиотеки в логгер", legacy=("LOG_CAPTURE_STDOUT",)),

    # -------------------------------------- WEB: API генератора конфигов
    Field("web", "host", "str", "0.0.0.0", "Адрес, на котором слушает API", legacy=("FLASK_HOST",)),
    Field("web", "port", "int", 8000, "Порт API", legacy=("FLASK_PORT",)),

    # ---------------- DEPLOY: публикация собранного конфига (локально и/или в GitHub)
    Field("deploy", "enabled", "bool", False, "Публиковать собранный конфиг после каждого цикла", legacy=("DEPLOY_ENABLED",)),
    Field("deploy", "local", "bool", False, "Локальный деплой: писать собранный конфиг в файл на этой машине (токены не нужны)", legacy=("DEPLOY_LOCAL",)),
    Field("deploy", "local_path", "path", PROJECT_ROOT / "output", "Папка локального деплоя (в мульти-режиме в неё кладутся файлы по имени шаблона)", legacy=("DEPLOY_LOCAL_PATH",)),
    # repo и ip_source намеренно ПУСТЫЕ: это данные конкретной установки.
    # Раньше здесь стоял чужой репозиторий и путь вида
    # /home/ray/ubuntu_server_ip/current_ip.txt — у нового пользователя они
    # выглядели как рабочие настройки и вели в никуда. Пустое значение видно
    # в preflight как «не задано» и требует осознанного заполнения.
    Field("deploy", "repo", "str", "", "Репозиторий owner/repo для GitHub-деплоя (пусто = GitHub-деплой не настроен)", legacy=("GH_DEPLOY_REPO",)),
    Field("deploy", "template", "str", "sbc-1.14.json", "Шаблон сборки из config/templates (одиночный режим)", legacy=("DEPLOY_TEMPLATE",)),
    Field("deploy", "templates", "list", [], "Мульти-деплой: имена шаблонов или all (все из config/templates)", legacy=("DEPLOY_TEMPLATES",)),
    Field("deploy", "path", "str", "config.json", "Путь файла внутри репозитория (в мульти-режиме — только каталог)", legacy=("DEPLOY_PATH",)),
    Field("deploy", "create_repo", "bool", False, "Создавать репозиторий автоматически, если его нет", legacy=("DEPLOY_CREATE_REPO",)),
    Field("deploy", "ip_source", "str", "", "Источник текущего IP для плейсхолдера: файл или http(s)-ссылка (нужен, только если шаблон содержит плейсхолдер)", legacy=("DEPLOY_IP_SOURCE",)),
    Field("deploy", "ip_placeholder", "str", "{{SERVER_IP}}", "Имя плейсхолдера в шаблоне, замещаемого актуальным IP", legacy=("DEPLOY_IP_PLACEHOLDER",)),

    # ------------------------ SERVICES: режим `python -m pipeline serve`
    Field("services", "discovery", "bool", True, "Служба поиска новых серверов", legacy=("SERVICE_DISCOVERY",)),
    Field("services", "collector", "bool", True, "Служба добора серверов из базы", legacy=("SERVICE_COLLECTOR",)),
    Field("services", "checker", "bool", True, "Служба проверки", legacy=("SERVICE_CHECKER",)),
    Field("services", "results", "bool", True, "Служба переноса вердиктов в таблицу серверов", legacy=("SERVICE_RESULTS",)),
    Field("services", "publisher", "bool", True, "Служба обновления списков", legacy=("SERVICE_PUBLISHER",)),
    Field("services", "discovery_interval", "float", 3600.0, "Как часто скачивать подписки", unit="сек", legacy=("SERVICE_DISCOVERY_INTERVAL",)),
    Field("services", "collector_interval", "float", 60.0, "Как часто добирать серверы из базы в очередь", unit="сек", legacy=("SERVICE_COLLECTOR_INTERVAL",)),
    Field("services", "results_interval", "float", 2.0, "Как часто переносить вердикты в базу", unit="сек", legacy=("SERVICE_RESULTS_INTERVAL",)),
    Field("services", "publisher_interval", "float", 1800.0, "Как часто обновлять whitelist/merge", unit="сек", legacy=("SERVICE_PUBLISHER_INTERVAL",)),
    Field("services", "publisher_deploy", "bool", False, "Публиковать (деплоить) списки при выгрузке", legacy=("SERVICE_PUBLISHER_DEPLOY",)),

    # ------------- SECRETS: только config.secret.yaml, в git не попадает
    Field("secrets", "gh_deploy_token", "str", "", "Деплой-токен GitHub (Contents: read+write). Только для пуша", secret=True, legacy=("GH_DEPLOY_TOKEN",)),
    Field("secrets", "gh_read_token", "str", "", "Read-only токен GitHub (Contents: read): вшивается в ссылки скачивания", secret=True, legacy=("GH_READ_TOKEN",)),
    Field("secrets", "api_keys", "table", {}, "Ключи сервисов для api_service; в целях подставляются как $env:ИМЯ", secret=True, legacy=("GEMINI_API_KEY", "OPENROUTER_API_KEY")),
)

#: Полный путь -> поле. По нему ищутся ключи YAML и перекрытия из кода.
BY_PATH: dict[str, Field] = {f.path: f for f in _SCHEMA}
#: Имя из старого .env -> поле.
BY_LEGACY: dict[str, Field] = {name: f for f in _SCHEMA for name in f.legacy}
#: Имя последнего уровня -> поля с таким именем (их может быть несколько).
BY_KEY: dict[str, list[Field]] = {}
for _f in _SCHEMA:
    BY_KEY.setdefault(_f.key, []).append(_f)
#: Короткий путь -> поле. По нему настройку находит код.
BY_SHORT: dict[str, Field] = {f.short: f for f in _SCHEMA}

#: Все группы в порядке появления в схеме.
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(f.group for f in _SCHEMA))


def fields_of(group: str) -> list[Field]:
    """Настройки одной группы в порядке объявления."""
    return [f for f in _SCHEMA if f.group == group]

# ============================================================================
# ЧТЕНИЕ СЛОЁВ
# ============================================================================

def read_env_file(path: Path) -> dict[str, str]:
    """Читает .env в словарь. Разбор намеренно простой."""
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        out[key] = value
    return out


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Читает YAML-конфиг. Нет файла — пустой словарь.

    Битый YAML — тоже пустой словарь и предупреждение: конфиг правят
    руками, и опечатка в отступе не должна ронять прогон без объяснения.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        warnings.warn(f"конфиг {path.name} не разобран: {exc}", RuntimeWarning, stacklevel=2)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        warnings.warn(
            f"конфиг {path.name}: ожидался словарь верхнего уровня, "
            f"получено {type(data).__name__}",
            RuntimeWarning, stacklevel=2,
        )
        return {}
    return data


#: Ключи, словарь которых — ЗНАЧЕНИЕ, а не ветка структуры. Без этого списка
#: secrets.api_keys развернулся бы в secrets.api_keys.GEMINI_API_KEY и
#: настройка потерялась бы.
_LEAF_TABLES = {"secrets.api_keys", "singbox.checkers.speed.tier_tags"}


def flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Разворачивает вложенный YAML в плоские пути.

    {"singbox": {"checkers": {"reachability": {"timeout": 6}}}} ->
    {"singbox.checkers.reachability.timeout": 6}.

    Список словарей и словарь значений (tier_tags, api_keys) остаются
    листьями: это значения, а не ветки структуры.
    """
    out: dict[str, Any] = {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, Mapping) and path not in _LEAF_TABLES:
                out.update(flatten(value, path))
            else:
                out[path] = value
    else:
        out[prefix] = node
    return out


def dotted(node: Any, prefix: str = "") -> dict[str, Any]:
    """Как flatten, но с ДВОЙНОЙ записью: и полный путь, и короткий.

    Настройка singbox.checkers.reachability.timeout доступна и как
    reachability.timeout — иначе код писал бы имя группы целиком там,
    где и без того ясно, о чём речь.
    """
    out = flatten(node, prefix)
    for path, value in list(out.items()):
        parts = path.split(".")
        for cut in range(1, len(parts)):
            out.setdefault(".".join(parts[cut:]), value)
    return out


def secret_values(source: Mapping[str, str]) -> dict[str, Any]:
    """Достаёт из .env ТОЛЬКО секреты.

    .env — не слой конфигурации, а хранилище секретов. Обычные настройки
    читаются из config.yaml и больше ниоткуда: раньше .env подмешивался в
    общий разбор, и одно устаревшее имя в нём молча перебивало конфиг —
    именно так max_ping_ms возвращался к 500 при записанных в файле 5000.

    Здесь остаются ровно те поля схемы, у которых secret=True, плюс ключи
    сервисов: GEMINI_API_KEY -> secrets.api_keys.GEMINI_API_KEY.
    """
    out: dict[str, Any] = {}
    for name, field in BY_LEGACY.items():
        if not field.secret or name not in source:
            continue
        value = source[name]
        if str(value).strip() == "":
            continue
        out.setdefault(field.path, value)
    keys = {
        name: source[name]
        for name in ("GEMINI_API_KEY", "OPENROUTER_API_KEY")
        if name in source and str(source[name]).strip()
    }
    if keys:
        out["secrets.api_keys"] = keys
    return out


# ============================================================================
# КОНФИГ
# ============================================================================

class ConfigError(ValueError):
    """Конфиг не собрался: файл уже есть, писать некуда, тому подобное."""


#: Группа -> {старое имя поля: значение из новой группы}. Заполняется в
#: config.settings: схема не обязана знать, какие имена были у кода раньше.
_FALLBACK: dict[str, dict[str, Any]] = {}


class Group(dict):
    """Настройки одной группы: и s.reachability.timeout, и s["reachability"]["timeout"].

    Возвращается методом Config.group(). Без этого класса обращение к группе
    точкой давало бы обычный словарь, и settings.log.level падало бы с
    AttributeError там, где settings["log"]["level"] работает: две формы
    доступа обязаны вести себя одинаково.

    ``fallback`` — поля, которые переехали в другую группу при разделении
    конфига по инструментам. Заполняется вызывающим кодом (config.settings),
    чтобы старые имена вроде pipeline.checkers продолжали работать.
    """

    def __init__(self, values: Mapping[str, Any], *,
                 fallback: Mapping[str, Any] | None = None) -> None:
        super().__init__(values)
        #: Поля, переехавшие в другую группу: «поле -> значение».
        self.fallback: dict[str, Any] = dict(fallback or {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError as exc:
            moved = self.fallback.get(name)
            if moved is not None:
                return moved
            raise AttributeError(name) from exc


class Config(dict):
    """Собранные настройки: доступ точкой, по полному и по короткому пути."""

    #: Откуда пришло каждое значение (для `config show` и разбора полётов).
    sources: dict[str, str]

    def __init__(self, values: Mapping[str, Any], sources: Mapping[str, str] | None = None):
        super().__init__(values)
        self.sources = dict(sources or {})

    # --------------------------------------------------------------- доступ
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self:
            return self[name]
        # Имя группы (singbox, log, reachability) отдаём словарём её настроек:
        # так работают и settings.log.level, и settings.reachability.timeout.
        field = BY_SHORT.get(name)
        if field is not None:
            return self.get(field.path, field.cast(field.default))
        if name in GROUPS:
            return self.group(name)
        # Группа по последнему уровню: reachability -> singbox.checkers.reachability.
        for group in GROUPS:
            if group.rsplit(".", 1)[-1] == name:
                return self.group(group)
        raise AttributeError(name)

    def value(self, path: str, default: Any = None) -> Any:
        """Значение по короткому или полному пути.

        Значения хранятся по ПОЛНОМУ пути (singbox.checkers.reachability
        .timeout), а код спрашивает коротким (reachability.timeout):
        перевод между ними делает схема, а не угадывание.
        """
        if path in self:
            return self[path]
        field = BY_PATH.get(path) or BY_SHORT.get(path)
        if field is not None:
            return self.get(field.path, field.cast(field.default))
        return default

    def field(self, path: str) -> Field | None:
        """Описание настройки (тип, комментарий, секретность)."""
        return BY_PATH.get(path) or BY_SHORT.get(path)

    def group(self, group: str) -> "Group":
        """Все настройки группы: доступ и точкой, и подпиской."""
        return Group(
            {f.key: self.value(f.short) for f in fields_of(group)},
            fallback=_FALLBACK.get(group, {}),
        )

    def group_tree(self, group: str) -> dict[str, Any]:
        """Группа вложенным словарём — так же, как она выглядит в YAML."""
        out: dict[str, Any] = {}
        for f in fields_of(group):
            out[f.key] = self.value(f.short)
        return out

    # --------------------------------------------------------------- вывод
    def as_dict(self) -> dict[str, Any]:
        """Все НЕсекретные значения по именам полей — для логов и отладки."""
        return {f.path: self.value(f.short) for f in _SCHEMA if not f.secret}

    def describe(self) -> dict[str, Any]:
        """Полная картина: значение, откуда взято, секрет ли.

        Секреты заменяются на *** либо на пометку «не задан»: объект
        настроек печатают целиком, и это не должно стоить утечки токена.
        """
        out: dict[str, Any] = {}
        for f in _SCHEMA:
            value = self.value(f.short)
            if f.secret:
                shown: Any = "***" if _has_value(value) else "(не задан)"
            else:
                shown = value
            out[f.path] = {
                "value": shown,
                "source": self.sources.get(f.path, "default"),
                "secret": f.secret,
            }
        return out

    def secrets(self) -> dict[str, Any]:
        """Только секреты — для записи в config.secret.yaml."""
        return {f.path: self.value(f.short) for f in _SCHEMA if f.secret}


def _has_value(value: Any) -> bool:
    """Есть ли у настройки непустое значение (для маскировки секретов)."""
    if value is None:
        return False
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) > 0
    return str(value).strip() != ""


#: Кэш собранного конфига: конфиг читается один раз на процесс.
_CONFIG: Config | None = None


def init_config(**overrides: Any) -> Config:
    """Собирает конфиг ОДИН раз и запоминает его на процесс.

    Вызывается точкой входа (main.py / pipeline.__main__ / тест). Всё
    остальное берёт настройки отсюда, а не импортирует константы.
    """
    global _CONFIG
    if _CONFIG is not None and not overrides:
        return _CONFIG
    _CONFIG = build_config(overrides=overrides or None)
    return _CONFIG


def get_config() -> Config:
    """Конфиг процесса; собирается при первом обращении.

    Ленивое создание здесь — единственное место автосоздания: настройка
    логирования и разбор аргументов командной строки обязаны работать ДО
    инициализации.
    """
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = build_config()
    return _CONFIG


def reset_config() -> None:
    """Сбрасывает кэш конфига (тесты и перезагрузка после правки файла)."""
    global _CONFIG
    _CONFIG = None


def build_config(
    *,
    overrides: Mapping[str, Any] | None = None,
    config_file: Path | None = None,
    env_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Собирает настройки из всех слоёв.

    overrides — значения от кода (аргументы CLI, тесты); они сильнее всего
    остального. Ключом может быть полный путь, короткий путь, имя поля,
    а для совместимости — старое имя из .env.
    """
    cfg_path = CONFIG_FILE if config_file is None else config_file
    env_path = ENV_FILE if env_file is None else env_file

    values: dict[str, Any] = {f.path: f.cast(f.default) for f in _SCHEMA}
    sources: dict[str, str] = {f.path: "default" for f in _SCHEMA}
    #: Значения, пришедшие из .env, — чтобы секреты можно было переприменить
    #: ПОСЛЕ config.yaml (см. ниже).
    from_env: dict[str, Any] = {}

    def apply(raw: Mapping[str, Any], origin: str, *, strict: bool,
              secrets_only: bool = False, is_config_file: bool = False) -> None:
        # Значение пишется ровно один раз — по ПОЛНОМУ пути: короткое имя
        # (reachability.timeout) лишь дубль, добавленный dotted().
        flat = flatten(raw)
        unknown: list[str] = []
        for path, value in flat.items():
            field = BY_PATH.get(path) or BY_LEGACY.get(path) or BY_SHORT.get(path)
            if field is None:
                unknown.append(path)
                continue
            if field.secret and value is None:
                continue
            if secrets_only and not field.secret:
                continue
            if not secrets_only and field.secret:
                # коммитится, и ключ уехал бы в git. Значение оттуда НЕ
                # применяем и говорим об этом громко.
                if is_config_file:
                    warnings.warn(
                        f"{origin}: секрет {field.path} в обычном конфиге — "
                        f"он не применён. Секреты держат в .env, файл "
                        f"которого в .gitignore.",
                        RuntimeWarning, stacklevel=3,
                    )
                continue
            values[field.path] = field.cast(value)
            sources[field.path] = origin
        if unknown and strict:
            warnings.warn(
                f"{origin}: неизвестных настроек {len(unknown)}: "
                + ", ".join(sorted(unknown)[:6])
                + ("…" if len(unknown) > 6 else ""),
                RuntimeWarning, stacklevel=3,
            )

    # --- слои, от слабого к сильному ------------------------------------
    # 1) config.yaml — ЕДИНСТВЕННЫЙ источник обычных настроек.
    if cfg_path.exists():
        apply(load_yaml_file(cfg_path), cfg_path.name, strict=True, is_config_file=True)

    # 2) .env — только секреты. Это не слой конфигурации: обычные настройки
    #    из него НЕ читаются, иначе устаревшее имя в файле молча перебивало
    #    бы config.yaml (так max_ping_ms возвращался к 500 при 5000 в конфиге).
    env_values = read_env_file(env_path) if env_path.exists() else {}
    if env is not None:
        env_values.update({str(k): v for k, v in env.items()})
    if env_values:
        apply(secret_values(env_values), "env-file", strict=False, secrets_only=True)

    if overrides:
        normalized: dict[str, Any] = {}
        for key, value in overrides.items():
            if value is None:
                continue
            field = BY_PATH.get(key) or BY_LEGACY.get(key) or BY_SHORT.get(key)
            if field is None and key in BY_KEY and len(BY_KEY[key]) == 1:
                field = BY_KEY[key][0]
            normalized[field.path if field else key] = value
        apply(normalized, "override", strict=False)

    return Config(values, sources)


#: Строки, которые в YAML надо цитировать всегда: иначе станут bool/null.
_YAML_RESERVED = {
    "true", "false", "yes", "no", "on", "off", "null", "~", "",
}


def _quote(text: str) -> str:
    """Строка в двойных кавычках с экранированием."""
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"')
    return '"' + escaped + '"'


def _scalar(value: Any) -> str:
    """Значение -> строка YAML. Цитируем там, где иначе смысл теряется."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    if text.lower() in _YAML_RESERVED:
        return _quote(text)
    # Одиночные {, }, [ и ] в YAML начинают поток или словарь, поэтому
    # {{SERVER_IP}} и подобное обязано быть в кавычках.
    if re.fullmatch(r"[A-Za-z0-9_./:$-]+", text) and not text.startswith(("-", ":")):
        return text
    return _quote(text)


def _wrap(text: str, width: int = 74) -> list[str]:
    """Комментарий с переносом по словам, с ведущим "# " на каждой строке.

    Пустая строка во входе даёт "#": так описание и подсказка «(в .env это
    ...)» разделяются, а не слипаются в одну фразу.
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("#")
            continue
        current = ""
        for word in paragraph.split():
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append("# " + current)
                current = word
        if current:
            lines.append("# " + current)
    return lines


def logging_name(level: Any) -> str:
    """Числовой уровень -> человекочитаемое имя (в конфиге видно глазом)."""
    number = as_int(level, 20)
    for name, value in LEVELS.items():
        if value == number and name not in {"WARN", "FATAL"}:
            return name
    return str(level)


def _default_yaml_value(field: Field) -> Any:
    """Значение по умолчанию в том виде, в каком оно пишется в конфиг."""
    value = field.cast(field.default)
    if field.kind == "level":
        return logging_name(value)
    # "binary" тоже путь: singbox.path разрешается до файла (resolve_binary),
    # и без этой ветки в конфиг попадал АБСОЛЮТНЫЙ путь текущей машины —
    # файл переставал работать у кого угодно ещё.
    if field.kind in ("path", "binary"):
        # В конфиге пишем путь ОТНОСИТЕЛЬНО репозитория: абсолютный путь
        # с чужой машины в чужом конфиге — источник тихих ошибок.
        try:
            return value.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return value.as_posix()
    return value


def _render_field(field: Field, indent: str, lines: list[str],
                  values: Mapping[str, Any] | None = None) -> None:
    """Настройка с комментарием: ключ и значение (скаляр, список, таблица).

    Значение берётся из ``values``, если оно там есть: так в файл попадает
    то, что действует сейчас, а не умолчание из схемы.
    """
    for comment in _wrap(field.comment()):
        lines.append(indent + comment)
    value = _default_yaml_value(field)
    if values is not None and field.path in values:
        value = values[field.path]
        if field.kind == "level":
            # Уровень в конфиге пишется ИМЕНЕМ (INFO), а не числом 20:
            # числом его потом никто не читает глазом, и при ручной правке
            # легко поставить «25».
            value = logging_name(value)
        elif field.kind in ("path", "binary"):
            # Живое значение пути абсолютное (оно пришло из Settings); в
            # коммитимый конфиг пишем относительный путь, иначе файл
            # бесполезен на другой машине. Ветка та же, что у умолчания:
            # иначе singbox.path уезжал в файл абсолютным путём этой машины.
            if value:
                try:
                    value = Path(value).relative_to(PROJECT_ROOT).as_posix()
                except (ValueError, TypeError):
                    value = Path(value).as_posix()
    if field.kind == "list" and value:
        lines.append(f"{indent}{field.key}:")
        for item in value:
            lines.append(f"{indent}  - {_scalar(item)}")
        return
    if field.kind == "table" and value:
        lines.append(f"{indent}{field.key}:")
        for key, item in dict(value).items():
            lines.append(f"{indent}  {key}: {_scalar(item)}")
        return
    if isinstance(value, list):
        lines.append(f"{indent}{field.key}: []")
        return
    if isinstance(value, Mapping):
        lines.append(f"{indent}{field.key}: {{}}")
        return
    lines.append(f"{indent}{field.key}: {_scalar(value)}")


def render_config(values: Mapping[str, Any] | None = None) -> str:
    """Собирает текст config.yaml: комментарии, группы и РАБОЧИЕ значения.

    Значения берутся из ``values`` (это эффективный конфиг: то, что реально
    действует с учётом .env), а чего там нет — из схемы. Файл получается
    живым: он не описывает, что «можно было бы задать», а показывает, что
    задано сейчас.

    СЕКРЕТОВ ЗДЕСЬ НЕТ. Ключи и токены живут в .env, который не коммитится;
    группа secrets в файл не попадает вовсе, а не пишется пустой. Так конфиг
    можно спокойно коммитить и показывать, не проверяя построчно, не уехал ли
    в него ключ.

    Группа — это путь в YAML: "singbox.checkers.reachability" пишется
    вложенными словарями, а настройки группы идут под ней.
    """
    lines: list[str] = []
    lines.append("# " + "=" * 76)
    lines.append("# singbox-subs-finder — конфигурация")
    lines.append("#")
    lines.append("# Здесь лежат ОБЫЧНЫЕ настройки: то, что действует сейчас.")
    lines.append("# Секретов нет — ключи и токены живут в .env, который в .gitignore.")
    lines.append("#")
    lines.append("# Приоритет слоёв, от сильного к слабому:")
    lines.append("#   аргументы CLI > системное окружение > config.yaml")
    lines.append("#   > значения по умолчанию из схемы.")
    lines.append("#")
    lines.append("# .env — НЕ слой настроек, а хранилище секретов: ключи и токены")
    lines.append("# читаются только оттуда (см. группу secrets в схеме).")
    lines.append("#")
    lines.append("# Группа — это инструмент: настройки одного чекера лежат в")
    lines.append("# singbox.checkers.<имя>, общее ядро проверки — в singbox.")
    lines.append("# Список включённых чекеров — singbox.enabled_checkers.")
    lines.append("# " + "=" * 76)

    # Группы пишутся ОДНИМ деревом. У singbox.checkers.url_probe,
    # singbox.checkers.reachability и прочих общий префикс: если объявлять
    # "singbox:" в каждой группе заново, в YAML получится дубль ключа, и
    # уцелеет только последняя ветка — ровно это и теряло все чекеры, кроме
    # одного. Поэтому общие уровни объявляются один раз, а заголовок группы
    # ставится там, где ветка начинается.
    emitted: list[str] = []
    for group in GROUPS:
        if group == "secrets":
            continue
        items = fields_of(group)
        if not items:
            continue
        parts = group.split(".")
        shared = 0
        while (shared < len(parts) and shared < len(emitted)
               and parts[shared] == emitted[shared]):
            shared += 1

        lines.append("")
        # Заголовок называет ГРУППУ, а вложенность видна по отступам ниже.
        # Полный путь ("singbox.checkers.url_probe") дублировал бы то, что и
        # так читается в YAML, и выходил за ширину строки; оставляем хвост
        # пути — имя инструмента.
        lines.append("# " + "-" * 76)
        lines.append("# " + parts[-1] + "  (" + ".".join(parts[:-1]) + ")"
                     if len(parts) > 1 else "# " + parts[-1])
        lines.append("# " + "-" * 76)
        for depth in range(shared, len(parts)):
            lines.append("  " * depth + f"{parts[depth]}:")
        indent = "  " * len(parts)
        for field in items:
            _render_field(field, indent, lines, values)
        emitted = parts
    lines.append("")
    return "\n".join(lines)


def write_config(path: Path, *, force: bool = False,
                 values: Mapping[str, Any] | None = None) -> Path:
    """Пишет config.yaml. Существующий не перезаписывается без force.

    force=True заменяет файл целиком: он собирается из ``values`` (или из
    умолчаний схемы) и теряет комментарии, которые там были. Для правки
    значений файл достаточно отредактировать руками.
    """
    if path.exists() and not force:
        raise ConfigError(
            f"{path.name} уже существует — правьте его или укажите --force, "
            f"чтобы перезаписать значениями по умолчанию"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(values), encoding="utf-8")
    return path


__all__ = [
    "BY_KEY", "BY_LEGACY", "BY_PATH", "BY_SHORT", "CONFIG_FILE",
    "Config", "ConfigError", "ENV_FILE", "Field", "GROUPS", "Group",
    "LEVELS", "PROJECT_ROOT", "ROOT", "as_bool", "as_float", "as_int",
    "as_list", "as_path", "as_str", "as_table", "build_config",
    "dotted", "fields_of", "flatten", "get_config", "init_config",
    "load_yaml_file", "logging_name", "parse_level", "read_env_file",
    "render_config", "reset_config", "resolve_binary", "write_config",
]
