"""Конфигурация проекта: ЕДИНСТВЕННОЕ место, где живут значения настроек.

Зачем этот модуль. Раньше значения лежали в config/env.py как 92 отдельные
константы, каждая со своим os.getenv на этапе ИМПОРТА. Это давало три
проблемы, и все три уже стоили времени:

  1. Значение фиксировалось при первом импорте. Тестам приходилось ставить
     переменные окружения ДО импорта проекта и делать каталоги уникальными
     по PID, иначе второй процесс в той же сессии получал ту же папку.
  2. Одно и то же значение оказывалось связано в трёх местах: константа
     здесь, имя в списке "from config.env import ..." и поле чужого
     конструктора. Через третью связь чекер скорости и падал: он собирал
     SpeedConfig(probe_url=...) по старой подписи.
  3. load_dotenv менял само окружение процесса, то есть подделывал
     глобальное состояние: тесты и код видели одни и те же переменные,
     а отменить подмену было нельзя.

Как устроено теперь. Значения живут в одном дата-классе Settings. Каждое
поле — одно место, где определены и имя, и тип, и значение по умолчанию.
Settings.from_env читает .env и системное окружение и собирает из них
объект — ОДИН РАЗ, явно, в точке старта. Дальше объект передаётся вниз (в
конструкторы и в ctx.settings), а не импортируется как магия на уровне
модуля.

Приоритет: явные переопределения (аргумент overrides) > системное окружение
> .env > значение по умолчанию из поля.

Окружение не мутируется: .env читается в собственный словарь, поэтому
подмена переменных в одном тесте не утекает в другой.

Пути — производные от корня проекта: это структура репозитория, а не
настройка (отдельные пути помечены как переопределяемые переменными).

СЕКРЕТЫ СЮДА НЕ ПОПАДАЮТ намеренно. Токены деплоя (GH_DEPLOY_TOKEN с
правом записи и GH_READ_TOKEN с правом чтения) читаются напрямую через
os.getenv в точке использования. Причина не в беспорядке, а в том, что
Settings.as_dict() предназначен для логов и отладки: положил бы токен в
поле — он уехал бы в лог и в отчёт. Держать секреты вне объекта, который
печатают целиком, безопаснее, чем надеяться, что никто не напечатает.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any, Mapping

# Корень пакета singbox-subscribe и корень репозитория (рядом с .env).
ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parent

# Человекочитаемые имена уровней -> числовые (logging.DEBUG и т.п.).
_LEVELS = {
    "CRITICAL": 50, "FATAL": 50, "ERROR": 40, "WARN": 30, "WARNING": 30,
    "INFO": 20, "DEBUG": 10, "NOTSET": 0, "OFF": 100,
}


def parse_level(value, default: str = "INFO") -> int:
    """Строка "DEBUG" / "info" / число 25 -> числовой уровень logging.

    Неизвестное имя не роняет процесс — берётся значение по умолчанию:
    опечатка в .env не должна мешать запуску службы.
    """
    raw = str(value if value is not None else "").strip().upper()
    if not raw:
        raw = str(default).upper()
    if raw.isdigit():
        return int(raw)
    return _LEVELS.get(raw, _LEVELS.get(str(default).upper(), _LEVELS["INFO"]))


class EnvSource:
    """Значения переменных: системное окружение поверх .env.

    Собственный словарь вместо load_dotenv: переменные процесса не
    подменяются, поэтому настройки одного теста не утекают в другой.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    @classmethod
    def read(cls, env_file: Path | None = None) -> "EnvSource":
        values: dict[str, str] = {}
        if env_file is None:
            env_file = PROJECT_ROOT / ".env"
        values.update(read_env_file(env_file))
        # Системное окружение приоритетнее файла (setx/export на Windows).
        values.update(os.environ)
        return cls(values)

    def get(self, name: str) -> str | None:
        raw = self._values.get(name)
        return None if raw is None else str(raw)

    def __contains__(self, name: str) -> bool:
        return name in self._values


#: Значения, которыми может быть обёрнуто в .env.
QUOTES = '"', "'"


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTES:
            value = value[1:-1]
        out[key] = value
    return out


def _as_int(raw, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_float(raw, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(raw, default: bool) -> bool:
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


@dataclass(frozen=True)
class PathsSettings:
    """Настройки: пути проекта."""

    urls_file: Path
    merge_file: Path
    whitelist_file: Path
    blacklist_file: Path
    servers_db_file: Path
    urltest_template: Path
    countrytest_template: Path
    reachability_targets_file: Path
    config_template_dir: Path
    sing_box_output_dir: Path
    sing_box_path: Path
    custom_checkers_dir: Path


@dataclass(frozen=True)
class CoreSettings:
    """Настройки: core."""

    purge_stable_below: int
    new_server_stable: int
    shield_cycles: int
    best_top: int
    sing_box_port: int


@dataclass(frozen=True)
class UrltestSettings:
    """Настройки: urltest."""

    url: str
    timeout: float
    batch_size: int


@dataclass(frozen=True)
class SubSettings:
    """Настройки: sub."""

    concurrency: int


@dataclass(frozen=True)
class CountrySettings:
    """Настройки: country."""

    enabled: bool
    concurrency: int
    timeout: float


@dataclass(frozen=True)
class ReachSettings:
    """Настройки: reach."""

    enabled: bool
    concurrency: int
    timeout: float
    max_ping_ms: int
    body_bytes: int
    body_seconds: float
    per_proxy_concurrency: int
    min_stable: int
    global_tag: str


@dataclass(frozen=True)
class SpeedSettings:
    """Настройки: speed."""

    enabled: bool
    site_url: str
    sources: str
    probe_bytes: int
    upload_bytes: int
    min_sources: int
    read_seconds: float
    min_mbps: float
    good_mbps: float
    concurrency: int
    tag_prefix: str
    tier_tags: dict


@dataclass(frozen=True)
class WebSettings:
    """Настройки: web."""

    host: str
    port: int


@dataclass(frozen=True)
class DeploySettings:
    """Настройки: deploy."""

    enabled: bool
    repo: str
    template: str
    templates: str
    path: str
    create_repo: bool
    ip_source: str
    ip_placeholder: str


@dataclass(frozen=True)
class LogSettings:
    """Настройки: log."""

    dir_path: Path
    level: int
    console_level: int
    file_name: str
    max_bytes: int
    backup_count: int
    retention_days: int
    per_level_files: bool
    format: str
    throttle_limit: int
    throttle_window: float
    capture_stdout: bool


@dataclass(frozen=True)
class PipelineSettings:
    """Настройки: pipeline."""

    checkers: str
    discovery: bool
    discovery_concurrency: int
    check_workers: int
    batch_size: int
    check_timeout: float
    batch_startup: float
    enrich_timeout: float
    max_attempts: int
    max_retries: int
    queue_persist: bool
    export_lists: bool
    write_merge: bool


@dataclass(frozen=True)
class ServicesSettings:
    """Настройки: services."""

    discovery: bool
    collector: bool
    checker: bool
    results: bool
    publisher: bool
    discovery_interval: float
    collector_interval: float
    results_interval: float
    publisher_interval: float
    publisher_deploy: bool


#: Секция -> (префикс имён в .env, поля). По этой таблице плоский словарь
#: сворачивается в секции, а имя настройки ищется обратно по имени поля.
_LAYOUT: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("paths", "", (
        ("URLS_FILE", "urls_file"), ("MERGE_FILE", "merge_file"),
        ("WHITELIST_FILE", "whitelist_file"), ("BLACKLIST_FILE", "blacklist_file"),
        ("SERVERS_DB_FILE", "servers_db_file"),
        ("URLTEST_TEMPLATE", "urltest_template"),
        ("COUNTRYTEST_TEMPLATE", "countrytest_template"),
        ("REACHABILITY_TARGETS_FILE", "reachability_targets_file"),
        ("CONFIG_TEMPLATE_DIR", "config_template_dir"),
        ("SING_BOX_OUTPUT_DIR", "sing_box_output_dir"),
        ("SING_BOX_PATH", "sing_box_path"),
        ("PIPELINE_CUSTOM_CHECKERS_DIR", "custom_checkers_dir"),
    )),
    ("core", "", (
        ("PURGE_STABLE_BELOW", "purge_stable_below"),
        ("NEW_SERVER_STABLE", "new_server_stable"),
        ("SHIELD_CYCLES", "shield_cycles"),
        ("BEST_TOP", "best_top"), ("SING_BOX_PORT", "sing_box_port"),
    )),
    ("urltest", "URLTEST_", (
        ("URLTEST_URL", "url"), ("URLTEST_TIMEOUT", "timeout"),
        ("URLTEST_BATCH_SIZE", "batch_size"),
    )),
    ("sub", "SUB_DOWNLOAD_", (("SUB_DOWNLOAD_CONCURRENCY", "concurrency"),)),
    ("country", "COUNTRY_CHECK_", (
        ("COUNTRY_CHECK_ENABLED", "enabled"),
        ("COUNTRY_CHECK_CONCURRENCY", "concurrency"),
        ("COUNTRY_CHECK_TIMEOUT", "timeout"),
    )),
    ("reach", "REACHABILITY_", (
        ("REACHABILITY_ENABLED", "enabled"),
        ("REACHABILITY_CONCURRENCY", "concurrency"),
        ("REACHABILITY_TIMEOUT", "timeout"),
        ("REACHABILITY_MAX_PING_MS", "max_ping_ms"),
        ("REACHABILITY_BODY_BYTES", "body_bytes"),
        ("REACHABILITY_BODY_SECONDS", "body_seconds"),
        ("REACHABILITY_PER_PROXY_CONCURRENCY", "per_proxy_concurrency"),
        ("REACHABILITY_MIN_STABLE", "min_stable"),
        ("REACHABILITY_GLOBAL_TAG", "global_tag"),
    )),
    ("speed", "SPEED_", (
        ("SPEED_ENABLED", "enabled"), ("SPEED_SITE_URL", "site_url"),
        ("SPEED_SOURCES", "sources"), ("SPEED_PROBE_BYTES", "probe_bytes"),
        ("SPEED_UPLOAD_BYTES", "upload_bytes"),
        ("SPEED_MIN_SOURCES", "min_sources"),
        ("SPEED_READ_SECONDS", "read_seconds"),
        ("SPEED_MIN_MBPS", "min_mbps"), ("SPEED_GOOD_MBPS", "good_mbps"),
        ("SPEED_CONCURRENCY", "concurrency"),
        ("SPEED_TAG_PREFIX", "tag_prefix"), ("SPEED_TIER_TAGS", "tier_tags"),
    )),
    ("web", "FLASK_", (("FLASK_HOST", "host"), ("FLASK_PORT", "port"))),
    ("deploy", "DEPLOY_", (
        ("DEPLOY_ENABLED", "enabled"), ("DEPLOY_TEMPLATE", "template"),
        ("GH_DEPLOY_REPO", "repo"),
        ("DEPLOY_TEMPLATES", "templates"), ("DEPLOY_PATH", "path"),
        ("DEPLOY_CREATE_REPO", "create_repo"),
        ("DEPLOY_IP_SOURCE", "ip_source"),
        ("DEPLOY_IP_PLACEHOLDER", "ip_placeholder"),
    )),
    ("log", "LOG_", (
        ("LOG_DIR_PATH", "dir_path"), ("LOG_LEVEL", "level"),
        ("LOG_CONSOLE_LEVEL", "console_level"), ("LOG_FILE_NAME", "file_name"),
        ("LOG_MAX_BYTES", "max_bytes"), ("LOG_BACKUP_COUNT", "backup_count"),
        ("LOG_RETENTION_DAYS", "retention_days"),
        ("LOG_PER_LEVEL_FILES", "per_level_files"), ("LOG_FORMAT", "format"),
        ("LOG_THROTTLE_LIMIT", "throttle_limit"),
        ("LOG_THROTTLE_WINDOW", "throttle_window"),
        ("LOG_CAPTURE_STDOUT", "capture_stdout"),
    )),
    ("pipeline", "PIPELINE_", (
        ("PIPELINE_CHECKERS", "checkers"), ("PIPELINE_DISCOVERY", "discovery"),
        ("PIPELINE_DISCOVERY_CONCURRENCY", "discovery_concurrency"),
        ("PIPELINE_CHECK_WORKERS", "check_workers"),
        ("PIPELINE_BATCH_SIZE", "batch_size"),
        ("PIPELINE_CHECK_TIMEOUT", "check_timeout"),
        ("PIPELINE_BATCH_STARTUP", "batch_startup"),
        ("PIPELINE_ENRICH_TIMEOUT", "enrich_timeout"),
        ("PIPELINE_MAX_ATTEMPTS", "max_attempts"),
        ("PIPELINE_MAX_RETRIES", "max_retries"),
        ("PIPELINE_QUEUE_PERSIST", "queue_persist"),
        ("PIPELINE_EXPORT_LISTS", "export_lists"),
        ("PIPELINE_WRITE_MERGE", "write_merge"),
    )),
    ("services", "SERVICE_", (
        ("SERVICE_DISCOVERY", "discovery"), ("SERVICE_COLLECTOR", "collector"),
        ("SERVICE_CHECKER", "checker"), ("SERVICE_RESULTS", "results"),
        ("SERVICE_PUBLISHER", "publisher"),
        ("SERVICE_DISCOVERY_INTERVAL", "discovery_interval"),
        ("SERVICE_COLLECTOR_INTERVAL", "collector_interval"),
        ("SERVICE_RESULTS_INTERVAL", "results_interval"),
        ("SERVICE_PUBLISHER_INTERVAL", "publisher_interval"),
        ("SERVICE_PUBLISHER_DEPLOY", "publisher_deploy"),
    )),
)


#: Имя секции -> её класс. Строится один раз, чтобы _fold не искал по имени.
_SECTION_CLASSES = {
    "paths": PathsSettings, "core": CoreSettings, "urltest": UrltestSettings,
    "sub": SubSettings, "country": CountrySettings, "reach": ReachSettings,
    "speed": SpeedSettings, "web": WebSettings, "deploy": DeploySettings,
    "log": LogSettings, "pipeline": PipelineSettings, "services": ServicesSettings,
}

#: Имя настройки в .env -> (секция, поле). Строится из _LAYOUT.
_BY_ENV_NAME: dict[str, tuple[str, str]] = {
    env_name: (section, field)
    for section, _prefix, fields in _LAYOUT
    for env_name, field in fields
}

@dataclass(frozen=True)
class Settings:
    """Все настройки проекта. Один экземпляр на процесс.

    Значения разложены по тематическим секциям: settings.speed.enabled.
    Так работает автодополнение IDE и находится любое использование, чего
    не даёт чтение по строке. Имена в .env при этом прежние — SPEED_ENABLED
    так и остаётся SPEED_ENABLED, менять их не нужно.
    """


    # Секции. Каждая — отдельный дата-класс выше; здесь только ссылки.
    paths: PathsSettings
    core: CoreSettings
    urltest: UrltestSettings
    sub: SubSettings
    country: CountrySettings
    reach: ReachSettings
    speed: SpeedSettings
    web: WebSettings
    deploy: DeploySettings
    log: LogSettings
    pipeline: PipelineSettings
    services: ServicesSettings

    # ============================== сборка значений ====================
    @classmethod
    def from_env(cls, *, overrides: Mapping[str, Any] | None = None,
                 env_file: Path | None = None) -> Settings:
        """Собирает настройки из .env и системного окружения.

        overrides — значения, заданные кодом (тесты, аргументы CLI).
        Они сильнее всего: тест может задать настройку, не трогая
        переменные окружения всего процесса.
        """
        source = EnvSource.read(env_file)
        over = dict(overrides or {})
        val: dict[str, Any] = {}

        def raw(name: str):
            if name in over:
                return str(over[name])
            return source.get(name)

        def get(name: str, default, cast=None):
            if name in over:
                # Перекрытие тоже приводится к типу. Иначе мусор из теста
                # или аргумента CLI уехал бы в настройки строкой и сломал
                # бы сравнение уже в потоке проверки, а не здесь.
                value = over[name]
                if cast is None or value is None:
                    return value
                coerced = cast(value, None)
                if coerced is not None:
                    return coerced
                # Перекрытие оказалось мусором: оно не должно затирать
                # рабочее значение. Логика разрешения продолжается ниже, как
                # если бы перекрытия не было вовсе.
                warnings.warn(
                    "настройка %s: не удалось привести %r к нужному типу, "
                    "значение из окружения" % (name, value),
                    RuntimeWarning,
                    stacklevel=3,
                )
            text = source.get(name)
            if text is None or str(text).strip() == "":
                return default
            if cast is None:
                return text
            return cast(text, default)

        val["URLS_FILE"] = None  # заполняется в _fill_paths
        val["MERGE_FILE"] = None  # заполняется в _fill_paths
        val["WHITELIST_FILE"] = None  # заполняется в _fill_paths
        val["BLACKLIST_FILE"] = None  # заполняется в _fill_paths
        val["SERVERS_DB_FILE"] = None  # заполняется в _fill_paths
        val["URLTEST_TEMPLATE"] = None  # заполняется в _fill_paths
        val["COUNTRYTEST_TEMPLATE"] = None  # заполняется в _fill_paths
        val["REACHABILITY_TARGETS_FILE"] = None  # заполняется в _fill_paths
        val["CONFIG_TEMPLATE_DIR"] = None  # заполняется в _fill_paths
        val["SING_BOX_OUTPUT_DIR"] = None  # заполняется в _fill_paths
        val["SING_BOX_PATH"] = None  # заполняется в _fill_paths
        val["PURGE_STABLE_BELOW"] = get('PURGE_STABLE_BELOW', 0, _as_int)
        val["NEW_SERVER_STABLE"] = get('NEW_SERVER_STABLE', 5, _as_int)
        val["SHIELD_CYCLES"] = get('SHIELD_CYCLES', 96, _as_int)
        val["URLTEST_URL"] = get('URLTEST_URL', "https://speed.cloudflare.com/__down?during=download&bytes=2048576")
        val["URLTEST_TIMEOUT"] = get('URLTEST_TIMEOUT', 10.0, _as_float)
        val["URLTEST_BATCH_SIZE"] = get('URLTEST_BATCH_SIZE', 100, _as_int)
        val["SING_BOX_PORT"] = get('SING_BOX_PORT', 7891, _as_int)
        val["SUB_DOWNLOAD_CONCURRENCY"] = get('SUB_DOWNLOAD_CONCURRENCY', 6, _as_int)
        val["COUNTRY_CHECK_ENABLED"] = get('COUNTRY_CHECK_ENABLED', True, _as_bool)
        val["COUNTRY_CHECK_CONCURRENCY"] = get('COUNTRY_CHECK_CONCURRENCY', 8, _as_int)
        val["COUNTRY_CHECK_TIMEOUT"] = get('COUNTRY_CHECK_TIMEOUT', 6.0, _as_float)
        val["REACHABILITY_ENABLED"] = get('REACHABILITY_ENABLED', True, _as_bool)
        val["REACHABILITY_CONCURRENCY"] = get('REACHABILITY_CONCURRENCY', 8, _as_int)
        val["REACHABILITY_TIMEOUT"] = get('REACHABILITY_TIMEOUT', 6.0, _as_float)
        val["REACHABILITY_MAX_PING_MS"] = get('REACHABILITY_MAX_PING_MS', 500, _as_int)
        val["REACHABILITY_BODY_BYTES"] = get('REACHABILITY_BODY_BYTES', 64 * 1024, _as_int)
        val["REACHABILITY_BODY_SECONDS"] = get('REACHABILITY_BODY_SECONDS', 2.0, _as_float)
        val["REACHABILITY_PER_PROXY_CONCURRENCY"] = get('REACHABILITY_PER_PROXY_CONCURRENCY', 1, _as_int)
        val["REACHABILITY_MIN_STABLE"] = get('REACHABILITY_MIN_STABLE', 0, _as_int)
        val["REACHABILITY_GLOBAL_TAG"] = get('REACHABILITY_GLOBAL_TAG', "Global")
        val["BEST_TOP"] = get('BEST_TOP', 0, _as_int)
        val["SPEED_ENABLED"] = get('SPEED_ENABLED', True, _as_bool)
        val["SPEED_SITE_URL"] = get('SPEED_SITE_URL', "https://gemini.google.com/")
        val["SPEED_SOURCES"] = get('SPEED_SOURCES', "")
        val["SPEED_PROBE_BYTES"] = get('SPEED_PROBE_BYTES', 2 * 1024 * 1024, _as_int)
        val["SPEED_UPLOAD_BYTES"] = get('SPEED_UPLOAD_BYTES', 1024 * 1024, _as_int)
        val["SPEED_MIN_SOURCES"] = get('SPEED_MIN_SOURCES', 2, _as_int)
        val["SPEED_READ_SECONDS"] = get('SPEED_READ_SECONDS', 4.0, _as_float)
        val["SPEED_MIN_MBPS"] = get('SPEED_MIN_MBPS', 1.0, _as_float)
        val["SPEED_GOOD_MBPS"] = get('SPEED_GOOD_MBPS', 5.0, _as_float)
        val["SPEED_CONCURRENCY"] = get('SPEED_CONCURRENCY', 8, _as_int)
        val["SPEED_TAG_PREFIX"] = get('SPEED_TAG_PREFIX', "speed-")
        val["FLASK_HOST"] = get('FLASK_HOST', "0.0.0.0")
        val["FLASK_PORT"] = get('FLASK_PORT', 8000, _as_int)
        val["DEPLOY_ENABLED"] = get('DEPLOY_ENABLED', False, _as_bool)
        val["GH_DEPLOY_REPO"] = get('GH_DEPLOY_REPO', "LaronDambon/sing-box-config")
        val["DEPLOY_TEMPLATE"] = get('DEPLOY_TEMPLATE', "sbc-1.14.json")
        val["DEPLOY_TEMPLATES"] = get('DEPLOY_TEMPLATES', "")
        val["DEPLOY_PATH"] = get('DEPLOY_PATH', "config.json")
        val["DEPLOY_CREATE_REPO"] = get('DEPLOY_CREATE_REPO', False, _as_bool)
        val["DEPLOY_IP_SOURCE"] = get('DEPLOY_IP_SOURCE', "")
        val["DEPLOY_IP_PLACEHOLDER"] = get('DEPLOY_IP_PLACEHOLDER', "{{SERVER_IP}}")
        val["LOG_DIR_PATH"] = None  # заполняется в _fill_paths
        val["LOG_LEVEL"] = parse_level(raw("LOG_LEVEL") or "INFO")
        val["LOG_CONSOLE_LEVEL"] = parse_level(raw("LOG_CONSOLE_LEVEL")
                                or raw("LOG_LEVEL") or "INFO")
        val["LOG_FILE_NAME"] = get('LOG_FILE_NAME', "app.log")
        val["LOG_MAX_BYTES"] = get('LOG_MAX_BYTES', 10 * 1024 * 1024, _as_int)
        val["LOG_BACKUP_COUNT"] = get('LOG_BACKUP_COUNT', 5, _as_int)
        val["LOG_RETENTION_DAYS"] = get('LOG_RETENTION_DAYS', 14, _as_int)
        val["LOG_PER_LEVEL_FILES"] = get('LOG_PER_LEVEL_FILES', True, _as_bool)
        val["LOG_FORMAT"] = get('LOG_FORMAT', "text").strip().lower()
        val["LOG_THROTTLE_LIMIT"] = get('LOG_THROTTLE_LIMIT', 20, _as_int)
        val["LOG_THROTTLE_WINDOW"] = get('LOG_THROTTLE_WINDOW', 60.0, _as_float)
        val["LOG_CAPTURE_STDOUT"] = get('LOG_CAPTURE_STDOUT', False, _as_bool)
        val["PIPELINE_CHECKERS"] = get('PIPELINE_CHECKERS', "url_probe,country,reachability,speed")
        val["PIPELINE_DISCOVERY"] = get('PIPELINE_DISCOVERY', True, _as_bool)
        val["PIPELINE_DISCOVERY_CONCURRENCY"] = get('PIPELINE_DISCOVERY_CONCURRENCY', 6, _as_int)
        val["PIPELINE_CHECK_WORKERS"] = get('PIPELINE_CHECK_WORKERS', 2, _as_int)
        # Размер батча по умолчанию равен размеру батча urltest:
        # отдельного смысла в двух разных числах нет.
        val["PIPELINE_BATCH_SIZE"] = get("PIPELINE_BATCH_SIZE", val["URLTEST_BATCH_SIZE"], _as_int)
        val["PIPELINE_CHECK_TIMEOUT"] = get('PIPELINE_CHECK_TIMEOUT', 20.0, _as_float)
        val["PIPELINE_BATCH_STARTUP"] = get('PIPELINE_BATCH_STARTUP', 10.0, _as_float)
        val["PIPELINE_ENRICH_TIMEOUT"] = get('PIPELINE_ENRICH_TIMEOUT', 180.0, _as_float)
        val["PIPELINE_MAX_ATTEMPTS"] = get('PIPELINE_MAX_ATTEMPTS', 3, _as_int)
        val["PIPELINE_MAX_RETRIES"] = get('PIPELINE_MAX_RETRIES', 3, _as_int)
        val["PIPELINE_QUEUE_PERSIST"] = get('PIPELINE_QUEUE_PERSIST', True, _as_bool)
        val["SERVICE_DISCOVERY"] = get('SERVICE_DISCOVERY', True, _as_bool)
        val["SERVICE_COLLECTOR"] = get('SERVICE_COLLECTOR', True, _as_bool)
        val["SERVICE_CHECKER"] = get('SERVICE_CHECKER', True, _as_bool)
        val["SERVICE_RESULTS"] = get('SERVICE_RESULTS', True, _as_bool)
        val["SERVICE_PUBLISHER"] = get('SERVICE_PUBLISHER', True, _as_bool)
        val["SERVICE_DISCOVERY_INTERVAL"] = get('SERVICE_DISCOVERY_INTERVAL', 3600.0, _as_float)
        val["SERVICE_COLLECTOR_INTERVAL"] = get('SERVICE_COLLECTOR_INTERVAL', 60.0, _as_float)
        val["SERVICE_RESULTS_INTERVAL"] = get('SERVICE_RESULTS_INTERVAL', 2.0, _as_float)
        val["SERVICE_PUBLISHER_INTERVAL"] = get('SERVICE_PUBLISHER_INTERVAL', 1800.0, _as_float)
        val["SERVICE_PUBLISHER_DEPLOY"] = get('SERVICE_PUBLISHER_DEPLOY', False, _as_bool)
        val["PIPELINE_CUSTOM_CHECKERS_DIR"] = None  # заполняется в _fill_paths
        val["PIPELINE_EXPORT_LISTS"] = get('PIPELINE_EXPORT_LISTS', True, _as_bool)
        val["PIPELINE_WRITE_MERGE"] = get('PIPELINE_WRITE_MERGE', True, _as_bool)

        val["SPEED_TIER_TAGS"] = {
            "fast": get("SPEED_TAG_FAST", "speed-fast"),
            "ok": get("SPEED_TAG_OK", "speed-ok"),
            "slow": get("SPEED_TAG_SLOW", "speed-slow"),
            "blocked": get("SPEED_TAG_BLOCKED", "speed-blocked"),
            # Сайт не пускает по СТРАНЕ: сервер жив и быстрый, но в Gemini не войти.
            "geo": get("SPEED_TAG_GEO", "speed-geo"),
        }

        cls._fill_paths(val, source, over)
        return cls(**cls._fold(val))

    @staticmethod
    def _fold(val: dict[str, Any]) -> dict[str, Any]:
        """Сворачивает плоский словарь в секции.

        Имя настройки в .env и имя поля в секции связаны таблицей _LAYOUT,
        а не угадываются по префиксу. Исключения из правила перечислены там
        же, а не спрятаны в коде.
        """
        out: dict[str, Any] = {}
        for section, _prefix, fields in _LAYOUT:
            out[section] = _SECTION_CLASSES[section](
                **{field: val[env_name] for env_name, field in fields},
            )
        return out

    @staticmethod
    def _fill_paths(val, source, over) -> None:
        """Пути: сначала переменная окружения, иначе путь от корня.
        Корневой путь — это структура репозитория, а не настройка,
        поэтому в .env его задать бессмысленно.
        """

        def path(env_name: str, fallback: Path) -> Path:
            if env_name in over:
                return Path(over[env_name])
            text = source.get(env_name)
            if text and text.strip():
                return Path(text.strip())
            return fallback

        # Пути, выводимые из корня проекта.
        val["URLS_FILE"] = ROOT / "config" / "subs" / "urls.json"
        val["MERGE_FILE"] = ROOT / "source" / "merge.txt"
        val["WHITELIST_FILE"] = ROOT / "source" / "whitelist.txt"
        val["BLACKLIST_FILE"] = ROOT / "source" / "blacklist.txt"
        val["SERVERS_DB_FILE"] = ROOT / "source" / "servers.db"
        val["URLTEST_TEMPLATE"] = ROOT / "config" / "urltest_template.json"
        val["COUNTRYTEST_TEMPLATE"] = ROOT / "config" / "countrytest_template.json"
        val["REACHABILITY_TARGETS_FILE"] = ROOT / "config" / "reachability_targets.json"

        # Переопределяемые пути.
        val["CONFIG_TEMPLATE_DIR"] = path("CONFIG_TEMPLATE_DIR", ROOT / "config_template")
        val["SING_BOX_OUTPUT_DIR"] = path("SING_BOX_OUTPUT_DIR", ROOT / "sing-box")
        val["SING_BOX_PATH"] = path("SING_BOX_PATH", ROOT / "sing-box" / _SING_BOX_BIN)
        val["LOG_DIR_PATH"] = path("LOG_DIR", ROOT / "logs")
        val["PIPELINE_CUSTOM_CHECKERS_DIR"] = path(
            "PIPELINE_CUSTOM_CHECKERS_DIR", ROOT / "pipeline" / "checkers" / "custom",
        )

    def as_dict(self) -> dict[str, Any]:
        """Все значения словарём — для логов и отладки.

        Раскрывает секции, чтобы в лог попало всё, а не пустые объекты.
        """
        out: dict[str, Any] = {}
        for section, _prefix, fields in _LAYOUT:
            nested = getattr(self, section)
            for env_name, field in fields:
                # Ключ — имя из .env, а НЕ имя поля: полей с одинаковым
                # именем несколько (timeout есть в country/reach/urltest,
                # enabled — в speed/reach/country/deploy), и по именам полей
                # словарь молча терял бы 10 настроек. Имя из .env уникально.
                out[env_name] = getattr(nested, field)
        return out

_SING_BOX_BIN = "sing-box.exe" if os.name == "nt" else "sing-box"

#: Настройки текущего процесса. main.py вызывает init_settings() при старте.
_SETTINGS: Settings | None = None


def init_settings(**overrides: Any) -> Settings:
    """Собирает настройки ОДИН раз и запоминает их на процесс.

    Вызывается точкой входа (main.py / pipeline.__main__ / тест). Всё
    остальное берёт настройки отсюда, а не импортирует константы.
    """
    global _SETTINGS
    if _SETTINGS is not None and not overrides:
        # Настройки уже собраны — пересобирать нельзя. Иначе второй вызов
        # без перекрытий тихо отменил бы перекрытия первого: main.py зовёт
        # init_settings(**env_overrides(args)), а точки входа служб зовут
        # init_settings() просто чтобы настройки существовали, и стоило
        # случиться одному вызову после другого, аргументы командной строки
        # пропали бы молча.
        return _SETTINGS
    _SETTINGS = Settings.from_env(overrides=overrides or None)
    return _SETTINGS


def get_settings() -> Settings:
    """Настройки процесса.

    Если init_settings() ещё не вызывали, настройки собираются здесь же.
    Это нужно не для удобства, а для двух мест, которые обязаны работать
    ДО инициализации: настройка логирования (её зовут первыми, чтобы видеть
    всё остальное) и разбор аргументов командной строки.

    Важно: ленивое создание здесь — единственное место автосоздания во всём
    проекте. Раньше таких мест было 92, по одному на каждую константу.
    """
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings.from_env()
    return _SETTINGS
