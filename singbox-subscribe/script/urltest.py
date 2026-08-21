import json
import re
import sys
import time
import warnings
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]

import requests
warnings.filterwarnings("ignore", category=Warning, module=r"requests")

from script.core import generate_debug_configs_with_singbox
from script.downloader import build_clean_tag, dedupe_blacklist_file, prune_whitelist_by_blacklist
from script.logger_utils import get_project_logger, setup_project_logging
from config.settings import (
    BATCH_SIZE,
    URLTEST_URL,
    TIMEOUT,
    WHITELIST_FILE,
    BLACKLIST_FILE,
    COUNTRY_CHECK_ENABLED,
    COUNTRY_CHECK_CONCURRENCY,
    COUNTRY_CHECK_TIMEOUT,
)

from utils import tool

setup_project_logging(console_level=20)
LOGGER = get_project_logger("debug_ping_runner")


def load_merge_lines(merge_path: str | Path) -> List[str]:
    path = Path(merge_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"merge file not found: {path}")
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def parse_ping_from_output(output_lines: List[str]) -> List[Tuple[str, int | None]]:
    results: List[Tuple[str, int | None]] = []
    for line in output_lines:
        if "outbound/urltest" not in line or ("available" not in line and "unavailable" not in line):
            continue
        match = re.search(r"outbound/urltest\[[^\]]+\]: outbound\s+(.+?)\s+(available|unavailable)", line)
        if not match:
            continue
        tag = match.group(1).strip()
        status = match.group(2).lower()
        ping_value = None
        ping_match = re.search(r"(\d+)ms", line)
        if ping_match:
            ping_value = int(ping_match.group(1))
        results.append((tag, ping_value if status == "available" else None))
    return results


def append_line(path: str | Path, line: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if fh.tell() > 0:
            fh.write("\n")
        fh.write(line)


def build_ping_result_line(proxy_line: str, seq_counter: int, ping_ms: int | None, include_country: bool = False, country_tag: str | None = None) -> str:
    cleaned = build_clean_tag(proxy_line, seq_counter=seq_counter, include_country=include_country, country_tag=country_tag)
    if ping_ms is None:
        return cleaned
    return f"{cleaned}-ping-{ping_ms}"


def ensure_blacklist(path: Path, line: str) -> None:
    """Просто дописывает строку в blacklist.txt.

    Дедупликация выполняется один раз в конце цикла (dedupe_blacklist_file),
    поэтому здесь НЕ читаем весь файл на каждую строку — это давало O(n²)
    и превращало запись батча в десятки секунд.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if fh.tell() > 0:
            fh.write("\n")
        fh.write(line)


def run_debug_ping_cycle(
    merge_path: str | Path,
    *,
    batch_size: int = BATCH_SIZE,
    urltest: str = URLTEST_URL,
    timeout: float = TIMEOUT,
    whitelist_path: str | Path | None = WHITELIST_FILE,
    blacklist_path: str | Path | None = BLACKLIST_FILE,
) -> dict:
    merge_file = Path(merge_path).resolve()
    lines = load_merge_lines(merge_file)

    whitelist_output = Path(whitelist_path) if whitelist_path else merge_file.with_suffix(".whitelist.txt")
    blacklist_output = Path(blacklist_path) if blacklist_path else merge_file.with_suffix(".blacklist.txt")

    # Whitelist НЕ удаляем: он накапливается между запусками, чтобы ранее найденные
    # рабочие серверы не терялись, даже если их убрали из исходных списков.
    # Серверы, не прошедшие повторную проверку, убираются в конце цикла по blacklist.
    if whitelist_output.exists():
        LOGGER.info("Whitelist сохранён и будет дополнен: %s", whitelist_output)

    total = len(lines)
    processed = 0
    written = 0
    whitelist_count = 0
    blacklist_count = 0
    cycle_started = time.monotonic()

    for start in range(0, total, batch_size):
        batch = lines[start:start + batch_size]
        processed += len(batch)
        batch_end = min(start + len(batch), total)
        LOGGER.info("Checking batch %d-%d/%d", start + 1, batch_end, total)

        batch_started = time.monotonic()
        try:
            result = generate_debug_configs_with_singbox(
                threads=4,
                urltest=urltest,
                ping_limit=500,
                merge_lines=batch,
            )
        except Exception as exc:
            LOGGER.exception(
                "Batch %d-%d failed during sing-box generation; skipping this batch.",
                start + 1,
                batch_end,
            )
            continue
        batch_elapsed = time.monotonic() - batch_started

        LOGGER.info(
            "Batch %d-%d sing-box finished in %.2fs, captured %d output lines",
            start + 1,
            batch_end,
            batch_elapsed,
            len(result.get("output", [])),
        )

        output_lines = result.get("raw_output") or result.get("output", [])
        parsed = parse_ping_from_output(output_lines)
        parsed_nodes = result.get("parsed_nodes", [])
        parsed_node_lines = result.get("parsed_node_lines", [])
        tag_to_line = {node.get("tag"): line for node, line in zip(parsed_nodes, parsed_node_lines) if isinstance(node, dict) and node.get("tag")}

        available_tags = {tag for tag, ping_ms in parsed if ping_ms is not None}
        batch_whitelist = 0
        batch_blacklist = 0
        output_serial = start

        # Фаза 1: подготовка строк для записи.
        # Страна определяется по имени/тэгу из исходной строки (быстро, без сети).
        # Если по имени определить не удалось — для whitelist-серверов в последних
        # батчах делаем проверку страны через скоростной тест (аналог Throne).
        # По требованию: страна добавляется ТОЛЬКО для whitelist-серверов (прошедших
        # проверку) и ТОЛЬКО в последних 100 батчах. Если страну не удалось
        # определить — логируем и забываем (не блокируем запись).
        prepared: list[tuple[str, str]] = []  # (target_file, line)
        tag_to_ping = dict(parsed)
        country_started = time.monotonic()

        # Последние 100 батчей (по 100 строк) — только для них определяем страну.
        last_batches = 100
        is_last_batches = (total - start) <= last_batches * batch_size

        # Собираем whitelist-строки, для которых нужно определить страну через
        # скоростной тест (не удалось по имени).
        country_check_lines: list[str] = []
        # tag -> (original_line, ping_ms, is_whitelist, include_country, seq_counter)
        tag_meta: dict[str, tuple] = {}

        for tag, original_line in tag_to_line.items():
            output_serial += 1
            ping_ms = tag_to_ping.get(tag) if tag in available_tags else None
            is_whitelist = tag in available_tags
            # Страна только для whitelist-серверов в последних 100 батчах.
            include_country = is_whitelist and is_last_batches
            tag_meta[tag] = (original_line, ping_ms, is_whitelist, include_country, output_serial)
            if include_country and not tool.get_country_from_name(original_line):
                # По имени не определили — попробуем через скоростной тест.
                if COUNTRY_CHECK_ENABLED:
                    country_check_lines.append(original_line)
                else:
                    LOGGER.debug(
                        "Не удалось определить страну по имени для whitelist-сервера: %s",
                        original_line[:120],
                    )
            line_text = build_ping_result_line(
                original_line,
                seq_counter=output_serial,
                ping_ms=ping_ms,
                include_country=include_country,
            )
            if is_whitelist:
                prepared.append((str(whitelist_output), line_text))
            else:
                prepared.append((str(blacklist_output), line_text))

        # Проверка страны через скоростной тест для строк без имени-страны.
        speedtest_country: dict[str, str] = {}  # original_line -> emoji
        if country_check_lines:
            from script.country_check import batch_country_check, country_to_emoji
            LOGGER.info(
                "Batch %d-%d: определяю страну через скоростной тест для %d whitelist-серверов (concurrency=%d)",
                start + 1,
                batch_end,
                len(country_check_lines),
                COUNTRY_CHECK_CONCURRENCY,
            )
            cc_results = batch_country_check(
                country_check_lines,
                concurrency=COUNTRY_CHECK_CONCURRENCY,
            )
            for line, res in cc_results.items():
                emoji = country_to_emoji(res.get("country"))
                if emoji:
                    speedtest_country[line] = emoji
                    LOGGER.info(
                        "Скоростной тест: страна %s (сервер %s, %sms) для %s",
                        res.get("country"),
                        res.get("server_name"),
                        res.get("latency_ms"),
                        line[:80],
                    )
                else:
                    LOGGER.debug(
                        "Скоростной тест не дал страну для %s: %s",
                        line[:80],
                        res.get("error") or res.get("country"),
                    )

        # Пересобираем строки с учётом страны, определённой через скоростной тест.
        if speedtest_country:
            rebuilt: list[tuple[str, str]] = []
            for tag, (original_line, ping_ms, is_whitelist, include_country, seq) in tag_meta.items():
                country_tag = speedtest_country.get(original_line)
                line_text = build_ping_result_line(
                    original_line,
                    seq_counter=seq,
                    ping_ms=ping_ms,
                    include_country=include_country,
                    country_tag=country_tag,
                )
                if is_whitelist:
                    rebuilt.append((str(whitelist_output), line_text))
                else:
                    rebuilt.append((str(blacklist_output), line_text))
            prepared = rebuilt

        country_elapsed = time.monotonic() - country_started
        LOGGER.info(
            "Batch %d-%d: tag build done for %d nodes in %.2fs (country for whitelist in last %d batches: %s, speedtest: %d)",
            start + 1,
            batch_end,
            len(prepared),
            country_elapsed,
            last_batches,
            is_last_batches,
            len(speedtest_country),
        )

        # Фаза 2: запись строк в whitelist/blacklist.
        write_started = time.monotonic()
        for target_file, line_text in prepared:
            if target_file == str(whitelist_output):
                append_line(whitelist_output, line_text)
                whitelist_count += 1
                batch_whitelist += 1
            else:
                ensure_blacklist(blacklist_output, line_text)
                blacklist_count += 1
                batch_blacklist += 1
            written += 1
        write_elapsed = time.monotonic() - write_started
        LOGGER.info(
            "Batch %d-%d: wrote %d lines to whitelist/blacklist in %.2fs",
            start + 1,
            batch_end,
            len(prepared),
            write_elapsed,
        )

        missing_tags = [tag for tag, _ in parsed if tag not in tag_to_line]
        if missing_tags:
            LOGGER.warning(
                "Batch %d-%d parsed %d tags not found in original batch: %s",
                start + 1,
                min(start + len(batch), total),
                len(missing_tags),
                missing_tags,
            )

        LOGGER.info(
            "Batch %d-%d result: %d available, %d blacklist (sing-box %.2fs + country/write %.2fs)",
            start + 1,
            min(start + len(batch), total),
            batch_whitelist,
            batch_blacklist,
            batch_elapsed,
            (time.monotonic() - batch_started) - batch_elapsed,
        )

    total_elapsed = time.monotonic() - cycle_started
    LOGGER.info(
        "Ping cycle finished: processed=%d written=%d whitelist=%d blacklist=%d in %.2fs",
        processed,
        written,
        whitelist_count,
        blacklist_count,
        total_elapsed,
    )
    dedupe_blacklist_file(Path(blacklist_output))
    # Убираем из whitelist серверы, которые не прошли повторную проверку в этом цикле.
    pruned = prune_whitelist_by_blacklist(Path(whitelist_output), Path(blacklist_output))
    if pruned:
        LOGGER.info("Из whitelist удалено %d серверов, не прошедших повторную проверку", pruned)
    return {
        "merge_path": str(merge_file),
        "batch_size": batch_size,
        "processed": processed,
        "written": written,
        "whitelist_count": whitelist_count,
        "blacklist_count": blacklist_count,
        "whitelist_path": str(whitelist_output),
        "blacklist_path": str(blacklist_output),
    }
