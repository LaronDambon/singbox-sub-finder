import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Прямые пакеты доступны через package imports, sys.path правки не нужны.

from config.env import URLS_FILE, MERGE_FILE, LOG_FILE, URLTEST_URL, URLTEST_TEMPLATE, SING_BOX_OUTPUT_DIR
from script.logger_utils import capture_stdout_stderr_to_logger, get_project_logger, setup_project_logging
import importlib

setup_project_logging(console_level=logging.INFO)
LOGGER = get_project_logger("main")

# import local script package modules (ROOT is first on sys.path)
_core = importlib.import_module("script.core")
_downloader = importlib.import_module("script.downloader")
_urltest = importlib.import_module("script.urltest")

# exported helpers
generate_debug_configs_with_singbox = _core.generate_debug_configs_with_singbox
build_merge_from_urls = _downloader.build_merge_from_urls
run_debug_ping_cycle = _urltest.run_debug_ping_cycle


def run_debug_generation(merge_lines, *, template=URLTEST_TEMPLATE, output_dir=SING_BOX_OUTPUT_DIR, singbox_path=None):
    """Оркеструет генерацию merged-config из переданных строк."""
    return generate_debug_configs_with_singbox(
        threads=1,
        urltest=URLTEST_URL,
        ping_limit=500,
        template=template,
        output_dir=output_dir,
        singbox_path=singbox_path,
        merge_lines=merge_lines,
    )


def orchestrate_default_run():
    """Default no-argument run:
    1) Скачать источники и зарегистрировать серверы в центральной базе (source/servers.db)
    2) Собрать source/merge.txt из пула проверки базы (excluded-серверы не входят)
    3) Прогнать urltest-циклы: stable +/- пишется в базу
    4) Экспорт списков из базы: whitelist.txt = stable > порога;
       серверы со stable < PURGE_STABLE_BELOW исключаются из проверочного списка
    """
    urls_file = URLS_FILE
    output_merge = MERGE_FILE
    log_file = LOG_FILE

    LOGGER.info("[main] Building merge from URLs")
    build_res = build_merge_from_urls(urls_file, output_merge, log_file)
    LOGGER.info("[main] Merged %d configs -> %s", build_res.get("merged_count", 0), build_res.get("output_path"))

    # parse + sing-box generation is handled inside urltest runner; run ping cycle
    LOGGER.info("[main] Running urltest ping cycle")
    ping_res = run_debug_ping_cycle(output_merge)
    LOGGER.info(json.dumps(ping_res, indent=2, ensure_ascii=False))
    return {"merge": build_res, "urltest": ping_res}


if __name__ == "__main__":
    with capture_stdout_stderr_to_logger(LOGGER):
        orchestrate_default_run()