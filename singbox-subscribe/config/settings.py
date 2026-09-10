import os
from pathlib import Path

from dotenv import load_dotenv

# Поднимаем локальные секреты/настройки из .env (файл в .gitignore) до чтения
# любых os.getenv ниже. Файл лежит в корне репозитория (на уровень выше папки
# пакета singbox-subscribe). override=False: уже заданные в окружении переменные
# (setx/системно) имеют приоритет.
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT.parent / ".env", override=False)
URLS_FILE = ROOT / "config" / "subs" / "urls.json"
MERGE_FILE = ROOT / "source" / "merge.txt"
WHITELIST_FILE = ROOT / "source" / "whitelist.txt"
BLACKLIST_FILE = ROOT / "source" / "blacklist.txt"
# Центральная база всех серверов и их stable (единственный источник правды).
SERVERS_DB_FILE = ROOT / "source" / "servers.db"
# Экспорт списков (whitelist.txt, /api/whitelist): берём серверы со stable > значения.
WHITELIST_EXPORT_MIN_STABLE = int(os.getenv("WHITELIST_EXPORT_MIN_STABLE", "1"))
# Удаление из проверочного списка: серверы со stable < значения исключаются из пула проверки.
PURGE_STABLE_BELOW = int(os.getenv("PURGE_STABLE_BELOW", "0"))
# Верхняя граница stable ("накопленный авторитет"). Без потолка давно работающий
# сервер после смерти слишком долго держался бы в списках: с капом он выпадает
# не дольше чем за STABLE_MAX + |PURGE_STABLE_BELOW| провальных циклов.
STABLE_MAX = int(os.getenv("STABLE_MAX", "5"))
# Сколько источников подписок качать параллельно при сборке merge.txt.
SUB_DOWNLOAD_CONCURRENCY = int(os.getenv("SUB_DOWNLOAD_CONCURRENCY", "6"))
LOG_FILE = ROOT / "logs" / "merge.log"
LOG_DIR = ROOT / "logs"
URLTEST_TEMPLATE = ROOT / "config" / "urltest_template.json"
COUNTRYTEST_TEMPLATE = ROOT / "config" / "countrytest_template.json"
CONFIG_TEMPLATE_DIR = ROOT / "config_template"
SING_BOX_PATH = ROOT / "sing-box" / ("sing-box.exe" if os.name == "nt" else "sing-box")
SING_BOX_OUTPUT_DIR = ROOT / "sing-box"
SING_BOX_PORT = int(os.getenv("SING_BOX_PORT", "7891"))
URLTEST_URL = "https://cp.cloudflare.com/gen_204"
#URLTEST_URL = "https://speed.cloudflare.com/__down?bytes=5000000"
TIMEOUT = 10.0
BATCH_SIZE = 100

# Проверка страны через скоростной тест (аналог Throne).
# Включает определение страны для whitelist-серверов через speedtest.net,
# а не только по имени/тэгу.
COUNTRY_CHECK_ENABLED = os.getenv("COUNTRY_CHECK_ENABLED", "1") == "1"
# Сколько прокси проверять параллельно (аналог countryConcurrency в Throne,
# где по умолчанию 5). Все они ходят через один процесс sing-box.
COUNTRY_CHECK_CONCURRENCY = int(os.getenv("COUNTRY_CHECK_CONCURRENCY", "8"))
# Таймаут чтения одного geo-запроса (сек); всего пробуется до 3 эндпоинта.
COUNTRY_CHECK_TIMEOUT = float(os.getenv("COUNTRY_CHECK_TIMEOUT", "6"))

# --- Профилирование достижимости до целевых сайтов (reachability check) ---
# По аналогии с countrytest: для ok-серверов (после ping-цикла) проверяется,
# до каких целевых сайтов прокси реально дозванивается и с каким пингом.
# Профиль хранится в БД (колонка capabilities); тэги [name] добавляются
# только при экспорте в whitelist для генерации конфига.
# Файл с целевыми сайтами (url/tag/пороги) — config/reachability_targets.json
REACHABILITY_TARGETS_FILE = ROOT / "config" / "reachability_targets.json"
# Включает профилирование достижимости после ping-цикла.
REACHABILITY_ENABLED = os.getenv("REACHABILITY_ENABLED", "1") == "1"
# Сколько прокси обрабатывать параллельно в одном процессе sing-box.
REACHABILITY_CONCURRENCY = int(os.getenv("REACHABILITY_CONCURRENCY", "8"))
# Таймаут (сек) на один целевой запрос внутри reachability-пробы.
REACHABILITY_TIMEOUT = float(os.getenv("REACHABILITY_TIMEOUT", "6"))
# Максимальный пинг до цели (ms) для попадания в capabilities.
REACHABILITY_MAX_PING_MS = int(os.getenv("REACHABILITY_MAX_PING_MS", "500"))
# С какого stable начинать профилировать (ок-серверы ниже не трогаем).
REACHABILITY_MIN_STABLE = int(os.getenv("REACHABILITY_MIN_STABLE", "0"))
# Тэг цели, которой помечается сервер, прошедший ВСЕ проверки этой категории.
REACHABILITY_GLOBAL_TAG = os.getenv("REACHABILITY_GLOBAL_TAG", "Global")


FLASK_HOST="0.0.0.0"
FLASK_PORT=8000

# --- Авто-деплой собранного конфига в GitHub (private) ---
# Включить деплой после каждого цикла проверки: DEPLOY_ENABLED=1
DEPLOY_ENABLED = os.getenv("DEPLOY_ENABLED", "0") == "1"
# Репозиторий owner/repo; токен — GH_DEPLOY_TOKEN (или GH_TOKEN)
GH_DEPLOY_REPO = os.getenv("GH_DEPLOY_REPO", "LaronDambon/sing-box-config")
# Какой шаблон из config/templates использовать для сборки деплоя
DEPLOY_TEMPLATE = os.getenv("DEPLOY_TEMPLATE", "sbc-1.14.json")
# Мульти-деплой: несколько шаблонов через запятую/пробел или 'all' (все из config/templates).
# Если задан — авто-деплой деплоит КАЖДЫЙ шаблон, итоговый файл называется именем шаблона.
DEPLOY_TEMPLATES = os.getenv("DEPLOY_TEMPLATES", "")
# Путь файла внутри репозитория (в мульти-режиме используется только каталог из него)
DEPLOY_PATH = os.getenv("DEPLOY_PATH", "config.json")
# Создавать репозиторий автоматически, если его нет (0/1)
DEPLOY_CREATE_REPO = os.getenv("DEPLOY_CREATE_REPO", "0") == "1"
# Источник текущего IP сервера для плейсхолдера в шаблоне деплоя:
# путь к локальному файлу (первая непустая строка) либо http(s)-ссылка
DEPLOY_IP_SOURCE = os.getenv("DEPLOY_IP_SOURCE", "")
# Имя плейсхолдера в шаблоне, замещаемого актуальным IP (по умолчанию {{SERVER_IP}})
DEPLOY_IP_PLACEHOLDER = os.getenv("DEPLOY_IP_PLACEHOLDER", "{{SERVER_IP}}")