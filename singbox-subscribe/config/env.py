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

# ------------------------------------------------- stable / временный чс ---
# Экспорт списков (whitelist.txt, /api/whitelist): берём серверы со stable
# СТРОГО БОЛЬШЕ этого порога.
WHITELIST_EXPORT_MIN_STABLE = _int("WHITELIST_EXPORT_MIN_STABLE", 1)
# Полноценный чс: из проверочного пула исключаются серверы со stable СТРОГО
# МЕНЬШЕ порога. При значении -1 в ротации остаётся и stable=-1 («подозрительный»),
# а в полноценный чс уходит только stable=-2.
PURGE_STABLE_BELOW = _int("PURGE_STABLE_BELOW", -1)
# Верхняя граница stable («накопленный авторитет»). Без потолка давно работающий
# сервер после смерти слишком долго держался бы в списках.
STABLE_MAX = _int("STABLE_MAX", 5)
# Временный чс: сколько ПОДРЯД неудачных проверок отправляют сервер в бан.
TEMP_BAN_FAILS = _int("TEMP_BAN_FAILS", 2)
# Длительность временного чса в часах: следующая проверка не раньше, чем через N часов.
TEMP_BAN_HOURS = _float("TEMP_BAN_HOURS", 12.0)
# Значение stable полноценного чс: провал решающей проверки после бана.
FULL_BAN_STABLE = _int("FULL_BAN_STABLE", -2)

# --------------------------------------------------------- проверка urltest
# URL для проверки доступности (лёгкий 204-ответ; можно заменить на «тяжёлый»).
URLTEST_URL = os.getenv("URLTEST_URL", "https://cp.cloudflare.com/gen_204")
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
