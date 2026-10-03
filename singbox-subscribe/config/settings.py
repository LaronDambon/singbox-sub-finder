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
class Settings:
    """Все настройки проекта. Один экземпляр на процесс."""


    # ============================== значения из окружения ==============
    # Пути считаются от корня пакета; при необходимости переопределяются env.
    URLS_FILE: Path = None
    MERGE_FILE: Path = None
    WHITELIST_FILE: Path = None
    BLACKLIST_FILE: Path = None
    # Центральная база всех серверов и их stable (единственный источник правды).
    SERVERS_DB_FILE: Path = None
    URLTEST_TEMPLATE: Path = None
    COUNTRYTEST_TEMPLATE: Path = None
    REACHABILITY_TARGETS_FILE: Path = None
    CONFIG_TEMPLATE_DIR: Path = None
    SING_BOX_OUTPUT_DIR: Path = None
    SING_BOX_PATH: Path = None
    # Нижний порог stable: в цикл проверки импортируются серверы со stable >= порога.
    # Дойдя до порога - 1 (по умолчанию -1), сервер считается умершим и больше не
    PURGE_STABLE_BELOW: int = 0
    # Значение stable нового сервера ДО первого удачного пинга: столько неудачных
    # проверок у него есть, чтобы доказать жизнеспособность.
    NEW_SERVER_STABLE: int = 5
    # «Щит» от удаления: после удачного пинга stable выставляется не ниже этого
    # значения — столько неудачных проверок подряд сервер ещё проживёт.
    SHIELD_CYCLES: int = 96
    # URL для проверки доступности (лёгкий 204-ответ; можно заменить на «тяжёлый»).
    URLTEST_URL: str = "https://speed.cloudflare.com/__down?during=download&bytes=2048576"
    # Таймаут одной проверки, сек.
    URLTEST_TIMEOUT: float = 10.0
    # Сколько серверов проверять за один запуск sing-box.
    URLTEST_BATCH_SIZE: int = 100
    # Порт локального mixed-inbound sing-box.
    SING_BOX_PORT: int = 7891
    # Сколько источников подписок качать параллельно при сборке merge.txt.
    SUB_DOWNLOAD_CONCURRENCY: int = 6
    COUNTRY_CHECK_ENABLED: bool = True
    # Сколько прокси проверять параллельно (аналог countryConcurrency в Throne).
    COUNTRY_CHECK_CONCURRENCY: int = 8
    # Таймаут чтения одного geo-запроса (сек); всего пробуется до 3 эндпоинта.
    COUNTRY_CHECK_TIMEOUT: float = 6.0
    REACHABILITY_ENABLED: bool = True
    REACHABILITY_CONCURRENCY: int = 8
    REACHABILITY_TIMEOUT: float = 6.0
    # Максимальный пинг до цели (ms) для попадания в capabilities.
    # Это порог TTFB — времени до ЗАГОЛОВКОВ ответа, а не времени скачивания
    REACHABILITY_MAX_PING_MS: int = 500
    # Сколько байт тела дочитывать после заголовков. TTFB уже измерен, дальше тело
    # не нужно: без ограничения requests тянул страницу целиком (gemini = 856 КБ
    REACHABILITY_BODY_BYTES: int = 64 * 1024
    # И сколько секунд максимум читать это тело, чтобы медленный хост не висел.
    REACHABILITY_BODY_SECONDS: float = 2.0
    # Сколько целей одновременно опрашиваем через ОДИН прокси. Каждая проба — это
    # новое соединение с узлом (TCP+TLS), и если одновременно их 8, они встают в
    REACHABILITY_PER_PROXY_CONCURRENCY: int = 1
    # С какого stable начинать профилировать (ok-серверы ниже не трогаем).
    REACHABILITY_MIN_STABLE: int = 0
    # Тэг цели, которой помечается сервер, прошедший ВСЕ проверки категории.
    REACHABILITY_GLOBAL_TAG: str = "Global"
    # --- Метки «лучших» серверов -------------------------------------------------
    # Сколько лучших серверов на каждый профиль достижимости получают дополнительный
    BEST_TOP: int = 0
    # --- Замер скорости прокси ---------------------------------------------------
    # Отдельный чекер-дополнение. Меряет МБ/с на размеро-контролируемой выкачке
    SPEED_ENABLED: bool = True
    # Сайт для проверки доступа. Gemini режет дата-центровые и VPN-адреса,
    # поэтому это отдельная проверка, а не следствие доступности.
    SPEED_SITE_URL: str = "https://gemini.google.com/"
    # --- Источники замера: несколько сайтов сразу -------------------------------
    # Скорость меряется не по одному сайту, а по нескольким ОДНОВРЕМЕННО, и итог —
    SPEED_SOURCES: str = ""
    # Сколько байт тянуть с КАЖДОГО источника (они идут параллельно).
    SPEED_PROBE_BYTES: int = 2 * 1024 * 1024
    # Сколько байт отдавать на проверку вверх.
    SPEED_UPLOAD_BYTES: int = 1024 * 1024
    # Сколько источников должны ответить, чтобы замер вообще засчитали: один
    # ответивший сайт ничего не доказывает.
    SPEED_MIN_SOURCES: int = 2
    # Потолок ЧТЕНИЯ, а не размера: медленный сервер отдаёт своё и уходит.
    SPEED_READ_SECONDS: float = 4.0
    # Границы категорий: ниже min — slow, от good и выше — fast.
    SPEED_MIN_MBPS: float = 1.0
    SPEED_GOOD_MBPS: float = 5.0
    SPEED_CONCURRENCY: int = 8
    # Префикс категории: новая метка ЗАМЕНЯет старую speed-*, а не дописывается
    # к ней. Иначе за несколько прогонов на сервере накапливаются все категории
    SPEED_TAG_PREFIX: str = "speed-"
    FLASK_HOST: str = "0.0.0.0"
    FLASK_PORT: int = 8000
    DEPLOY_ENABLED: bool = False
    # Репозиторий owner/repo; токены — GH_DEPLOY_TOKEN (write) и GH_READ_TOKEN (read).
    GH_DEPLOY_REPO: str = "LaronDambon/sing-box-config"
    DEPLOY_TEMPLATE: str = "sbc-1.14.json"
    # Мульти-деплой: шаблоны через запятую/пробел или 'all'.
    DEPLOY_TEMPLATES: str = ""
    DEPLOY_PATH: str = "config.json"
    DEPLOY_CREATE_REPO: bool = False
    DEPLOY_IP_SOURCE: str = ""
    DEPLOY_IP_PLACEHOLDER: str = "{{SERVER_IP}}"
    # Единая точка настройки — pipeline.logging_setup. Всё, что ниже, читается
    # ОДИН раз при первом обращении к get_logger()/setup_logging().
    LOG_DIR_PATH: Path = None
    LOG_LEVEL: int = 0
    # Уровень консоли отдельно от файлов: LOG_CONSOLE_LEVEL=INFO -> файлы подробнее.
    LOG_CONSOLE_LEVEL: int = 0
    # Главный файл: всё, что прошло порог LOG_LEVEL.
    LOG_FILE_NAME: str = "app.log"
    # Ротация по размеру: файл до LOG_MAX_BYTES, затем LOG_BACKUP_COUNT копий.
    LOG_MAX_BYTES: int = 10 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 5
    # Очистка по возрасту: файлы старше LOG_RETENTION_DAYS дней удаляются при старте.
    # 0 = не удалять по возрасту (чистая ротация по размеру).
    LOG_RETENTION_DAYS: int = 14
    # Отдельный файл на каждый уровень (info.log/warning.log/error.log) с фильтром
    # ТОЧНО на уровень: запись INFO попадает только в info.log, а не копией в три
    LOG_PER_LEVEL_FILES: bool = True
    # Подробный формат с модулем в файле: text|json
    LOG_FORMAT: str = "text"
    # Защита от «пулемёта»: не более LOG_THROTTLE_LIMIT одинаковых сообщений
    # за LOG_THROTTLE_WINDOW секунд (далее — одна сводная строка).
    LOG_THROTTLE_LIMIT: int = 20
    LOG_THROTTLE_WINDOW: float = 60.0
    # Перенаправлять print()/сторонние библиотеки в логгер (1/0).
    LOG_CAPTURE_STDOUT: bool = False
    # Порядок и состав этапов задаются здесь; каждый этап реализуется отдельным
    # модулем (см. pipeline/checkers и pipeline/stages).
    PIPELINE_CHECKERS: str = "url_probe,country,reachability,speed"
    # Этап поиска новых серверов из ссылок (асинхронный, расширяет БД).
    PIPELINE_DISCOVERY: bool = True
    # Сколько источников качать одновременно на этапе поиска.
    PIPELINE_DISCOVERY_CONCURRENCY: int = 6
    # Сколько батчей проверки крутится одновременно (каждый = свой sing-box).
    PIPELINE_CHECK_WORKERS: int = 2
    # Размер батча проверки = сколько серверов уходит в один запуск sing-box.
    # Наследует URLTEST_BATCH_SIZE (см. from_env).
    PIPELINE_BATCH_SIZE: int = None
    # Таймаут одного БАТЧА проверки, сек. Батч из 100 серверов получает
    # PIPELINE_CHECK_TIMEOUT + PIPELINE_BATCH_STARTUP, осколок — долю от своего
    PIPELINE_CHECK_TIMEOUT: float = 20.0
    # Постоянная часть таймаута: запуск sing-box, разбор конфига, резолв DNS.
    PIPELINE_BATCH_STARTUP: float = 10.0
    # Таймаут ЧЕКЕРОВ-ДОПОЛНЕНИЙ (страна, достижимость целевых сайтов), сек.
    # Это отдельные часы: профиль достижимости гоняет каждый сервер через
    PIPELINE_ENRICH_TIMEOUT: float = 180.0
    # Сколько попыток повторить батч при сбое ЗАПУСКА sing-box (занятый порт).
    PIPELINE_MAX_ATTEMPTS: int = 3
    # Сколько раз сервер возвращается в очередь, если sing-box ни разу не сказал
    # про него ничего: таймаут батча, сбой конфига, обрыв. Такие серверы не
    PIPELINE_MAX_RETRIES: int = 3
    # Очередь проверки живёт в БД (check_queue) и переживает перезапуск.
    # PIPELINE_QUEUE_PERSIST=1 -> ставить pending-строки обратно в очередь при старте.
    PIPELINE_QUEUE_PERSIST: bool = True
    # --- Службы (режим python -m pipeline serve) -------------------------------
    # Каждая служба крутится своим циклом и общается с остальными через очередь
    SERVICE_DISCOVERY: bool = True
    SERVICE_COLLECTOR: bool = True
    SERVICE_CHECKER: bool = True
    SERVICE_RESULTS: bool = True
    SERVICE_PUBLISHER: bool = True
    SERVICE_DISCOVERY_INTERVAL: float = 3600.0
    # Сборщик батчей ходит в базу чаще: очередь должна пополняться, пока
    # подписки обновляются раз в час.
    SERVICE_COLLECTOR_INTERVAL: float = 60.0
    # Служба записи забирает вердикты: пауза меньше, чтобы свежие серверы
    # появлялись в базе сразу после подтверждения.
    SERVICE_RESULTS_INTERVAL: float = 2.0
    # Выгрузка свежих рабочих серверов раз в полчаса.
    SERVICE_PUBLISHER_INTERVAL: float = 1800.0
    # Деплой по расписанию выключен: выгрузка обновляет списки, публикация —
    # отдельное решение.
    SERVICE_PUBLISHER_DEPLOY: bool = False
    # Папка с ПОЛЬЗОВАТЕЛЬСКИМИ проверяющими алгоритмами (подхватываются автоматически).
    PIPELINE_CUSTOM_CHECKERS_DIR: Path = None
    # Экспортировать whitelist.txt/blacklist.txt после цикла.
    PIPELINE_EXPORT_LISTS: bool = True
    # Записывать merge.txt (пул проверки) после наполнения очереди.
    PIPELINE_WRITE_MERGE: bool = True

    # Тэги категорий скорости: fast/ok/slow/blocked/geo.
    SPEED_TIER_TAGS: dict = field(default_factory=dict)

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
        return cls(**val)

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
        """Все значения словарём — для логов и отладки."""
        return {f.name: getattr(self, f.name) for f in dataclass_fields(self)}

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


def setting(name: str, default: Any = None) -> Any:
    """Настройка по имени, как в .env: setting("PIPELINE_BATCH_SIZE").

    Основной способ достать настройку в коде, который не получает объект
    сверху. Имя настройки называется ровно один раз — здесь строкой, в
    .env строкой же. Отдельные константы больше не нужны и не должны
    появляться: это и есть та вторая связь, которая ломалась молча.
    """
    value = getattr(get_settings(), name, None)
    return default if value is None else value
