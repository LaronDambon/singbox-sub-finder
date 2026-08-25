import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
#URLTEST_URL = "https://cp.cloudflare.com/gen_204"
URLTEST_URL = "https://speed.cloudflare.com/__down?bytes=500000"
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

FLASK_HOST="0.0.0.0"
FLASK_PORT=8000