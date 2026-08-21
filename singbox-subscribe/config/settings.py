import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URLS_FILE = ROOT / "config" / "subs" / "urls.json"
MERGE_FILE = ROOT / "source" / "merge.txt"
WHITELIST_FILE = ROOT / "source" / "whitelist.txt"
BLACKLIST_FILE = ROOT / "source" / "blacklist.txt"
LOG_FILE = ROOT / "logs" / "merge.log"
LOG_DIR = ROOT / "logs"
URLTEST_TEMPLATE = ROOT / "config" / "urltest_template.json"
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
# Сколько прокси проверять параллельно (аналог countryConcurrency в Throne).
COUNTRY_CHECK_CONCURRENCY = int(os.getenv("COUNTRY_CHECK_CONCURRENCY", "4"))
# Таймаут проверки одного прокси (сек).
COUNTRY_CHECK_TIMEOUT = float(os.getenv("COUNTRY_CHECK_TIMEOUT", "25"))

FLASK_HOST="0.0.0.0"
FLASK_PORT=8000