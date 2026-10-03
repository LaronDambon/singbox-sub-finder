"""СОВМЕСТИМОСТЬ: тонкая обёртка над config/settings.py.

Этот модуль НЕ источник истины. Значения живут в объекте Settings
(config/settings.py); здесь они только перевыкладываются под старыми
именами, чтобы не пришлось переписывать все импорты разом.

Зачем он ещё жив:
  * 20+ модулей делают from config.env import PIPELINE_CHECK_WORKERS.
    Переводить их все сразу — большой рефакторинг без выгоды в поведении.
  * Он даёт ПОНЯТНУЮ ошибку вместо AttributeError, если кто-то полез
    за настройкой, которой больше нет.

Чего здесь нельзя: задавать значения. Их собирает только Settings.from_env.

Как пользоваться в новом коде:

    from config.settings import get_settings
    cfg = get_settings()

или, лучше, получать готовый объект сверху (ctx.settings), а не
доставать глобальный.
"""

from __future__ import annotations

import warnings
from typing import Any

from config.settings import (  # noqa: F401  реэкспорт для совместимости
    PROJECT_ROOT,
    ROOT,
    EnvSource,
    Settings,
    get_settings,
    init_settings,
    parse_level,
    read_env_file,
)


def _ensure() -> Settings:
    """Настройки процесса; создаются при первом обращении.

    Раньше значения просто появлялись при импорте, и код, который
    импортировал env.py ДО main.py, получал готовые константы. Здесь то же
    поведение сохраняется, но через явный вызов init_settings().
    """
    from config import settings as _mod

    if _mod._SETTINGS is None:
        _mod.init_settings()
    return _mod.get_settings()


#: Старое имя -> поле объекта Settings.
_ALIASES = {
    "URLS_FILE": "URLS_FILE",
    "MERGE_FILE": "MERGE_FILE",
    "WHITELIST_FILE": "WHITELIST_FILE",
    "BLACKLIST_FILE": "BLACKLIST_FILE",
    "SERVERS_DB_FILE": "SERVERS_DB_FILE",
    "URLTEST_TEMPLATE": "URLTEST_TEMPLATE",
    "COUNTRYTEST_TEMPLATE": "COUNTRYTEST_TEMPLATE",
    "REACHABILITY_TARGETS_FILE": "REACHABILITY_TARGETS_FILE",
    "CONFIG_TEMPLATE_DIR": "CONFIG_TEMPLATE_DIR",
    "SING_BOX_OUTPUT_DIR": "SING_BOX_OUTPUT_DIR",
    "SING_BOX_PATH": "SING_BOX_PATH",
    "PURGE_STABLE_BELOW": "PURGE_STABLE_BELOW",
    "NEW_SERVER_STABLE": "NEW_SERVER_STABLE",
    "SHIELD_CYCLES": "SHIELD_CYCLES",
    "URLTEST_URL": "URLTEST_URL",
    "URLTEST_TIMEOUT": "URLTEST_TIMEOUT",
    "URLTEST_BATCH_SIZE": "URLTEST_BATCH_SIZE",
    "SING_BOX_PORT": "SING_BOX_PORT",
    "SUB_DOWNLOAD_CONCURRENCY": "SUB_DOWNLOAD_CONCURRENCY",
    "COUNTRY_CHECK_ENABLED": "COUNTRY_CHECK_ENABLED",
    "COUNTRY_CHECK_CONCURRENCY": "COUNTRY_CHECK_CONCURRENCY",
    "COUNTRY_CHECK_TIMEOUT": "COUNTRY_CHECK_TIMEOUT",
    "REACHABILITY_ENABLED": "REACHABILITY_ENABLED",
    "REACHABILITY_CONCURRENCY": "REACHABILITY_CONCURRENCY",
    "REACHABILITY_TIMEOUT": "REACHABILITY_TIMEOUT",
    "REACHABILITY_MAX_PING_MS": "REACHABILITY_MAX_PING_MS",
    "REACHABILITY_BODY_BYTES": "REACHABILITY_BODY_BYTES",
    "REACHABILITY_BODY_SECONDS": "REACHABILITY_BODY_SECONDS",
    "REACHABILITY_PER_PROXY_CONCURRENCY": "REACHABILITY_PER_PROXY_CONCURRENCY",
    "REACHABILITY_MIN_STABLE": "REACHABILITY_MIN_STABLE",
    "REACHABILITY_GLOBAL_TAG": "REACHABILITY_GLOBAL_TAG",
    "BEST_TOP": "BEST_TOP",
    "SPEED_ENABLED": "SPEED_ENABLED",
    "SPEED_SITE_URL": "SPEED_SITE_URL",
    "SPEED_SOURCES": "SPEED_SOURCES",
    "SPEED_PROBE_BYTES": "SPEED_PROBE_BYTES",
    "SPEED_UPLOAD_BYTES": "SPEED_UPLOAD_BYTES",
    "SPEED_MIN_SOURCES": "SPEED_MIN_SOURCES",
    "SPEED_READ_SECONDS": "SPEED_READ_SECONDS",
    "SPEED_MIN_MBPS": "SPEED_MIN_MBPS",
    "SPEED_GOOD_MBPS": "SPEED_GOOD_MBPS",
    "SPEED_CONCURRENCY": "SPEED_CONCURRENCY",
    "SPEED_TAG_PREFIX": "SPEED_TAG_PREFIX",
    "FLASK_HOST": "FLASK_HOST",
    "FLASK_PORT": "FLASK_PORT",
    "DEPLOY_ENABLED": "DEPLOY_ENABLED",
    "GH_DEPLOY_REPO": "GH_DEPLOY_REPO",
    "DEPLOY_TEMPLATE": "DEPLOY_TEMPLATE",
    "DEPLOY_TEMPLATES": "DEPLOY_TEMPLATES",
    "DEPLOY_PATH": "DEPLOY_PATH",
    "DEPLOY_CREATE_REPO": "DEPLOY_CREATE_REPO",
    "DEPLOY_IP_SOURCE": "DEPLOY_IP_SOURCE",
    "DEPLOY_IP_PLACEHOLDER": "DEPLOY_IP_PLACEHOLDER",
    "LOG_DIR_PATH": "LOG_DIR_PATH",
    "LOG_LEVEL": "LOG_LEVEL",
    "LOG_CONSOLE_LEVEL": "LOG_CONSOLE_LEVEL",
    "LOG_FILE_NAME": "LOG_FILE_NAME",
    "LOG_MAX_BYTES": "LOG_MAX_BYTES",
    "LOG_BACKUP_COUNT": "LOG_BACKUP_COUNT",
    "LOG_RETENTION_DAYS": "LOG_RETENTION_DAYS",
    "LOG_PER_LEVEL_FILES": "LOG_PER_LEVEL_FILES",
    "LOG_FORMAT": "LOG_FORMAT",
    "LOG_THROTTLE_LIMIT": "LOG_THROTTLE_LIMIT",
    "LOG_THROTTLE_WINDOW": "LOG_THROTTLE_WINDOW",
    "LOG_CAPTURE_STDOUT": "LOG_CAPTURE_STDOUT",
    "PIPELINE_CHECKERS": "PIPELINE_CHECKERS",
    "PIPELINE_DISCOVERY": "PIPELINE_DISCOVERY",
    "PIPELINE_DISCOVERY_CONCURRENCY": "PIPELINE_DISCOVERY_CONCURRENCY",
    "PIPELINE_CHECK_WORKERS": "PIPELINE_CHECK_WORKERS",
    "PIPELINE_BATCH_SIZE": "PIPELINE_BATCH_SIZE",
    "PIPELINE_CHECK_TIMEOUT": "PIPELINE_CHECK_TIMEOUT",
    "PIPELINE_BATCH_STARTUP": "PIPELINE_BATCH_STARTUP",
    "PIPELINE_ENRICH_TIMEOUT": "PIPELINE_ENRICH_TIMEOUT",
    "PIPELINE_MAX_ATTEMPTS": "PIPELINE_MAX_ATTEMPTS",
    "PIPELINE_MAX_RETRIES": "PIPELINE_MAX_RETRIES",
    "PIPELINE_QUEUE_PERSIST": "PIPELINE_QUEUE_PERSIST",
    "SERVICE_DISCOVERY": "SERVICE_DISCOVERY",
    "SERVICE_COLLECTOR": "SERVICE_COLLECTOR",
    "SERVICE_CHECKER": "SERVICE_CHECKER",
    "SERVICE_RESULTS": "SERVICE_RESULTS",
    "SERVICE_PUBLISHER": "SERVICE_PUBLISHER",
    "SERVICE_DISCOVERY_INTERVAL": "SERVICE_DISCOVERY_INTERVAL",
    "SERVICE_COLLECTOR_INTERVAL": "SERVICE_COLLECTOR_INTERVAL",
    "SERVICE_RESULTS_INTERVAL": "SERVICE_RESULTS_INTERVAL",
    "SERVICE_PUBLISHER_INTERVAL": "SERVICE_PUBLISHER_INTERVAL",
    "SERVICE_PUBLISHER_DEPLOY": "SERVICE_PUBLISHER_DEPLOY",
    "PIPELINE_CUSTOM_CHECKERS_DIR": "PIPELINE_CUSTOM_CHECKERS_DIR",
    "PIPELINE_EXPORT_LISTS": "PIPELINE_EXPORT_LISTS",
    "PIPELINE_WRITE_MERGE": "PIPELINE_WRITE_MERGE",
}


def __getattr__(name: str) -> Any:
    """Отдаёт настройку по старому имени и предупреждает об устаревании.

    Сделано через __getattr__, а не присваиваниями на уровне модуля:
    тогда имя появляется только когда действительно понадобилось, и
    поиск по config.env показывает реальных потребителей, а не список
    из 90 строк, который ни о чём не говорит.
    """
    field = _ALIASES.get(name)
    if field is None:
        raise AttributeError(
            "config.env больше не содержит настройку %r. " % name
            + "Доступные: " + ", ".join(sorted(_ALIASES))
        )
    warnings.warn(
        "config.env устарел: бери %r из config.settings" % name,
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(_ensure(), field)


__all__ = list(_ALIASES)
