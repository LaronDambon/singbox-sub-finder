"""Конфигурация проекта: единственный источник значений — окружение.

Модуль заменяет удалённый config/settings.py. Все настройки читаются из
переменных окружения; локальные значения задаются в файле ``.env`` в корне
репозитория (в .gitignore, не коммитится), шаблон со всеми переменными и
комментариями — ``.env.example``.

Приоритет значений: системное окружение (setx/export) > .env > значение
по умолчанию из этого модуля. Пути — производные от корня проекта:
это структура репозитория, а не настройка (отдельные пути помечены
как переопределяемые переменными).

Модуль безопасен к импорту даже без установленного python-dotenv:
в этом случае просто не подхватывается .env, а os.getenv читает
системное окружение и возвращает значения по умолчанию.
"""

from __future__ import annotations

import os
from pathlib import Path

# Корень пакета singbox-subscribe и корень репозитория (рядом с .env).
ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ROOT.parent

try:
    from dotenv import load_dotenv

    # override=False: уже заданные в окружении переменные (setx/системно)
    # имеют приоритет над значениями из .env.
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001 — dotenv необязателен, работаем и без него
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Человекочитаемые имена уровней -> числовые (logging.DEBUG и т.п.).
_LEVELS = {
    "CRITICAL": 50, "FATAL": 50, "ERROR": 40, "WARN": 30, "WARNING": 30,
    "INFO": 20, "DEBUG": 10, "NOTSET": 0, "OFF": 100,
}


def _level(name: str, default: str) -> int:
    """Читает уровень логирования из окружения (DEBUG/INFO/WARNING/ERROR/...).

    Принимается и числовое значение (например LOG_LEVEL=25). Неизвестное имя
    не роняет процесс — берётся значение по умолчанию.
    """
    raw = str(os.getenv(name, "")).strip().upper()
    if not raw:
        raw = str(default).upper()
    if raw.isdigit():
        return int(raw)
    return _LEVELS.get(raw, _LEVELS.get(str(default).upper(), _LEVELS["INFO"]))


# --------------------------------------------------------------------- пути
# Пути считаются от корня пакета; при необходимости переопределяются env.
URLS_FILE = ROOT / "config" / "subs" / "urls.json"
MERGE_FILE = ROOT / "source" / "merge.txt"
WHITELIST_FILE = ROOT / "source" / "whitelist.txt"
BLACKLIST_FILE = ROOT / "source" / "blacklist.txt"
# Центральная база всех серверов и их stable (единственный источник правды).
SERVERS_DB_FILE = ROOT / "source" / "servers.db"
LOG_DIR = ROOT / "logs"
LOG_FILE = ROOT / "logs" / "merge.log"
URLTEST_TEMPLATE = ROOT / "config" / "urltest_template.json"
COUNTRYTEST_TEMPLATE = ROOT / "config" / "countrytest_template.json"
REACHABILITY_TARGETS_FILE = ROOT / "config" / "reachability_targets.json"
CONFIG_TEMPLATE_DIR = Path(os.getenv("CONFIG_TEMPLATE_DIR", str(ROOT / "config_template")))
SING_BOX_OUTPUT_DIR = Path(os.getenv("SING_BOX_OUTPUT_DIR", str(ROOT / "sing-box")))
# Бинарник sing-box: по умолчанию автоопределение по ОС, можно переопределить.
_DEFAULT_SING_BOX = ROOT / "sing-box" / ("sing-box.exe" if os.name == "nt" else "sing-box")
SING_BOX_PATH = Path(os.getenv("SING_BOX_PATH", str(_DEFAULT_SING_BOX)))

# ------------------------------------------------ stable / «щит» от удаления ---
# Нижний порог stable: в цикл проверки импортируются серверы со stable >= порога.
# Дойдя до порога - 1 (по умолчанию -1), сервер считается умершим и больше не
# импортируется из базы. Значение порога — 0.
PURGE_STABLE_BELOW = _int("PURGE_STABLE_BELOW", 0)
# Значение stable нового сервера ДО первого удачного пинга: столько неудачных
# проверок у него есть, чтобы доказать жизнеспособность.
NEW_SERVER_STABLE = _int("NEW_SERVER_STABLE", 5)
# «Щит» от удаления: после удачного пинга stable выставляется не ниже этого
# значения — столько неудачных проверок подряд сервер ещё проживёт.
SHIELD_CYCLES = _int("SHIELD_CYCLES", 96)

# --------------------------------------------------------- проверка urltest
# URL для проверки доступности (лёгкий 204-ответ; можно заменить на «тяжёлый»).
URLTEST_URL = os.getenv("URLTEST_URL", "https://speed.cloudflare.com/__down?during=download&bytes=2048576")
# Таймаут одной проверки, сек.
URLTEST_TIMEOUT = _float("URLTEST_TIMEOUT", 10.0)
# Сколько серверов проверять за один запуск sing-box.
URLTEST_BATCH_SIZE = _int("URLTEST_BATCH_SIZE", 100)
# Порт локального mixed-inbound sing-box.
SING_BOX_PORT = _int("SING_BOX_PORT", 7891)
# Сколько источников подписок качать параллельно при сборке merge.txt.
SUB_DOWNLOAD_CONCURRENCY = _int("SUB_DOWNLOAD_CONCURRENCY", 6)

# ------------------------------------------- определение страны сервера ----
COUNTRY_CHECK_ENABLED = _bool("COUNTRY_CHECK_ENABLED", True)
# Сколько прокси проверять параллельно (аналог countryConcurrency в Throne).
COUNTRY_CHECK_CONCURRENCY = _int("COUNTRY_CHECK_CONCURRENCY", 8)
# Таймаут чтения одного geo-запроса (сек); всего пробуется до 3 эндпоинта.
COUNTRY_CHECK_TIMEOUT = _float("COUNTRY_CHECK_TIMEOUT", 6.0)

# --------------------- профилирование достижимости (reachability check) ---
REACHABILITY_ENABLED = _bool("REACHABILITY_ENABLED", True)
REACHABILITY_CONCURRENCY = _int("REACHABILITY_CONCURRENCY", 8)
REACHABILITY_TIMEOUT = _float("REACHABILITY_TIMEOUT", 6.0)
# Максимальный пинг до цели (ms) для попадания в capabilities.
REACHABILITY_MAX_PING_MS = _int("REACHABILITY_MAX_PING_MS", 500)
# С какого stable начинать профилировать (ok-серверы ниже не трогаем).
REACHABILITY_MIN_STABLE = _int("REACHABILITY_MIN_STABLE", 0)
# Тэг цели, которой помечается сервер, прошедший ВСЕ проверки категории.
REACHABILITY_GLOBAL_TAG = os.getenv("REACHABILITY_GLOBAL_TAG", "Global")

# ------------------------------------------------------------------ Flask --
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = _int("FLASK_PORT", 8000)

# --------------------------------------- деплой собранного конфига в GitHub
DEPLOY_ENABLED = _bool("DEPLOY_ENABLED", False)
# Репозиторий owner/repo; токены — GH_DEPLOY_TOKEN (write) и GH_READ_TOKEN (read).
GH_DEPLOY_REPO = os.getenv("GH_DEPLOY_REPO", "LaronDambon/sing-box-config")
DEPLOY_TEMPLATE = os.getenv("DEPLOY_TEMPLATE", "sbc-1.14.json")
# Мульти-деплой: шаблоны через запятую/пробел или 'all'.
DEPLOY_TEMPLATES = os.getenv("DEPLOY_TEMPLATES", "")
DEPLOY_PATH = os.getenv("DEPLOY_PATH", "config.json")
DEPLOY_CREATE_REPO = _bool("DEPLOY_CREATE_REPO", False)
DEPLOY_IP_SOURCE = os.getenv("DEPLOY_IP_SOURCE", "")
DEPLOY_IP_PLACEHOLDER = os.getenv("DEPLOY_IP_PLACEHOLDER", "{{SERVER_IP}}")

# ================================================================== ЛОГИРОВАНИЕ
# Единая точка настройки — pipeline.logging_setup. Всё, что ниже, читается
# ОДИН раз при первом обращении к get_logger()/setup_logging().
#
# ГЛАВНОЕ ПРАВИЛО: LOG_LEVEL — это ПОРОГ. Запись ниже порога не попадает ни в
# один файл и ни в консоль.
#     LOG_LEVEL=INFO  -> пишутся только info/warning/error/critical
#     LOG_LEVEL=WARNING-> пишутся только warning/error/critical
#     LOG_LEVEL=ERROR -> пишутся только error/critical
#     LOG_LEVEL=DEBUG -> всё, включая отладочные сообщения
LOG_DIR_PATH = Path(os.getenv("LOG_DIR", str(ROOT / "logs")))
LOG_LEVEL = _level("LOG_LEVEL", "INFO")
# Уровень консоли отдельно от файлов: LOG_CONSOLE_LEVEL=INFO -> файлы подробнее.
LOG_CONSOLE_LEVEL = _level("LOG_CONSOLE_LEVEL", str(os.getenv("LOG_LEVEL", "INFO")))
# Главный файл: всё, что прошло порог LOG_LEVEL.
LOG_FILE_NAME = os.getenv("LOG_FILE_NAME", "app.log")
# Ротация по размеру: файл до LOG_MAX_BYTES, затем LOG_BACKUP_COUNT копий.
LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 10 * 1024 * 1024)
LOG_BACKUP_COUNT = _int("LOG_BACKUP_COUNT", 5)
# Очистка по возрасту: файлы старше LOG_RETENTION_DAYS дней удаляются при старте.
# 0 = не удалять по возрасту (чистая ротация по размеру).
LOG_RETENTION_DAYS = _int("LOG_RETENTION_DAYS", 14)
# Отдельный файл на каждый уровень (info.log/warning.log/error.log) с фильтром
# ТОЧНО на уровень: запись INFO попадает только в info.log, а не копией в три
# файла сразу. 1 = включено, 0 = только главный LOG_FILE_NAME.
LOG_PER_LEVEL_FILES = _bool("LOG_PER_LEVEL_FILES", True)
# Подробный формат с модулем в файле: text|json
LOG_FORMAT = os.getenv("LOG_FORMAT", "text").strip().lower()
# Защита от «пулемёта»: не более LOG_THROTTLE_LIMIT одинаковых сообщений
# за LOG_THROTTLE_WINDOW секунд (далее — одна сводная строка).
LOG_THROTTLE_LIMIT = _int("LOG_THROTTLE_LIMIT", 20)
LOG_THROTTLE_WINDOW = _float("LOG_THROTTLE_WINDOW", 60.0)
# Перенаправлять print()/сторонние библиотеки в логгер (1/0).
LOG_CAPTURE_STDOUT = _bool("LOG_CAPTURE_STDOUT", False)

# ==================================================================== PIPELINE
# Порядок и состав этапов задаются здесь; каждый этап реализуется отдельным
# модулем (см. pipeline/checkers и pipeline/stages).
# Проверяющие алгоритмы через запятую: url_probe,country,reachability.
# «none» или пусто -> только url_probe.
PIPELINE_CHECKERS = os.getenv(
    "PIPELINE_CHECKERS", "url_probe,country,reachability"
)
# Этап поиска новых серверов из ссылок (асинхронный, расширяет БД).
PIPELINE_DISCOVERY = _bool("PIPELINE_DISCOVERY", True)
# Сколько источников качать одновременно на этапе поиска.
PIPELINE_DISCOVERY_CONCURRENCY = _int("PIPELINE_DISCOVERY_CONCURRENCY", 6)
# Сколько батчей проверки крутится одновременно (каждый = свой sing-box).
PIPELINE_CHECK_WORKERS = _int("PIPELINE_CHECK_WORKERS", 2)
# Размер батча проверки = сколько серверов уходит в один запуск sing-box.
PIPELINE_BATCH_SIZE = _int("PIPELINE_BATCH_SIZE", URLTEST_BATCH_SIZE)
# Таймаут одного БАТЧА проверки, сек. Батч из 100 серверов получает
# PIPELINE_CHECK_TIMEOUT + PIPELINE_BATCH_STARTUP, осколок — долю от своего
# размера. По истечении sing-box убивается, а серверы, про которых он не
# успел сказать ни слова, НЕ считаются мёртвыми: они возвращаются в очередь
# и проверяются в следующем круге (PIPELINE_MAX_RETRIES).
PIPELINE_CHECK_TIMEOUT = _float("PIPELINE_CHECK_TIMEOUT", 20.0)
# Постоянная часть таймаута: запуск sing-box, разбор конфига, резолв DNS.
PIPELINE_BATCH_STARTUP = _float("PIPELINE_BATCH_STARTUP", 10.0)
# Таймаут ЧЕКЕРОВ-ДОПОЛНЕНИЙ (страна, достижимость целевых сайтов), сек.
# Это отдельные часы: профиль достижимости гоняет каждый сервер через
# несколько сайтов и спокойно занимает минуту даже на трёх серверах.
PIPELINE_ENRICH_TIMEOUT = _float("PIPELINE_ENRICH_TIMEOUT", 180.0)
# Сколько попыток повторить батч при сбое ЗАПУСКА sing-box (занятый порт).
PIPELINE_MAX_ATTEMPTS = _int("PIPELINE_MAX_ATTEMPTS", 3)
# Сколько раз сервер возвращается в очередь, если sing-box ни разу не сказал
# про него ничего: таймаут батча, сбой конфига, обрыв. Такие серверы не
# считаются мёртвыми — они просто проверяются заново. После этого срока
# снимаются с проверки на текущий прогон.
PIPELINE_MAX_RETRIES = _int("PIPELINE_MAX_RETRIES", 3)
# Очередь проверки живёт в БД (check_queue) и переживает перезапуск.
# PIPELINE_QUEUE_PERSIST=1 -> ставить pending-строки обратно в очередь при старте.
PIPELINE_QUEUE_PERSIST = _bool("PIPELINE_QUEUE_PERSIST", True)
# Папка с ПОЛЬЗОВАТЕЛЬСКИМИ проверяющими алгоритмами (подхватываются автоматически).
PIPELINE_CUSTOM_CHECKERS_DIR = Path(
    os.getenv("PIPELINE_CUSTOM_CHECKERS_DIR", str(ROOT / "pipeline" / "checkers" / "custom"))
)
# Экспортировать whitelist.txt/blacklist.txt после цикла.
PIPELINE_EXPORT_LISTS = _bool("PIPELINE_EXPORT_LISTS", True)
# Записывать merge.txt (пул проверки) после наполнения очереди.
PIPELINE_WRITE_MERGE = _bool("PIPELINE_WRITE_MERGE", True)
