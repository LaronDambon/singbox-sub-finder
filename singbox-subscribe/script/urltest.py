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
from script.downloader import build_clean_tag, normalize_proxy_key
from script.logger_utils import get_project_logger, setup_project_logging
from script.server_store import (  # noqa: F401 — re-export parse_stable_from_line для совместимости
    ServerStore,
    compute_next_state,
    parse_stable_from_line,
)
from config.env import (
    URLTEST_BATCH_SIZE,
    URLTEST_URL,
    URLTEST_TIMEOUT,
    WHITELIST_FILE,
    BLACKLIST_FILE,
    SERVERS_DB_FILE,
    WHITELIST_EXPORT_MIN_STABLE,
    PURGE_STABLE_BELOW,
    COUNTRY_CHECK_ENABLED,
    COUNTRY_CHECK_CONCURRENCY,
    COUNTRY_CHECK_TIMEOUT,
    REACHABILITY_ENABLED,
    REACHABILITY_CONCURRENCY,
    REACHABILITY_TIMEOUT,
    REACHABILITY_MIN_STABLE,
    REACHABILITY_GLOBAL_TAG,
    DEPLOY_ENABLED,
    GH_DEPLOY_REPO,
    DEPLOY_TEMPLATE,
    DEPLOY_TEMPLATES,
    DEPLOY_PATH,
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


def build_ping_result_line(proxy_line: str, seq_counter: int, ping_ms: int | None, include_country: bool = False, country_tag: str | None = None, stable: int | None = None) -> str:
    cleaned = build_clean_tag(proxy_line, seq_counter=seq_counter, include_country=include_country, country_tag=country_tag)
    if ping_ms is not None:
        cleaned = f"{cleaned}-ping-{ping_ms}"
    if stable is not None:
        cleaned = f"{cleaned}-stable-{stable}"
    return cleaned


def _has_urltest_records(raw_lines: list[str]) -> bool:
    """True, если в выводе sing-box есть хоть один outbound/urltest-результат.

    Пустой результат (при bind-ошибке / аварийном старте) — инфраструктурный
    сбой, а не «все серверы мертвы»: списывать на него весь батч нельзя.
    """
    return any(
        "outbound/urltest" in line
        and ("available" in line or "unavailable" in line)
        for line in raw_lines
    )


def _run_singbox_batch(batch: list[str], urltest: str, *,
                       batch_no: int = 0, max_retries: int = 3) -> dict | None:
    """Запускает sing-box на батче с ретраями на инфраструктурные сбои.

    Причина: зависший/осиротевший sing-box может держать порт из
    fresh_inbound_port; следующий батч с тем же портом падает с
    "bind: address already in use" -> sing-box возвращается без urltest-строк ->
    весь батч списывается в unavailable. Каждый ретрай делает свежий порт
    (fresh_inbound_port выбирает новый), поэтому конфликт сам себя разрешает.

    Возвращает result либо None, если батч так и не удалось обработать
    (вызывающий должен пропустить его, НЕ помечая серверы dead).
    """
    result = None
    for attempt in range(1, max_retries + 1):
        try:
            result = generate_debug_configs_with_singbox(
                threads=4,
                urltest=urltest,
                ping_limit=500,
                merge_lines=batch,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Batch %d attempt %d/%d: sing-box generation failed: %s",
                batch_no, attempt, max_retries, exc,
            )
            result = None
        raw = (result.get("raw_output") or result.get("output", [])) if result else []
        if result is not None and _has_urltest_records(raw):
            return result
        # Инфраструктурный сбой (нет результатов). Даём ещё одну попытку на
        # свежем порту; если снова пусто после последней — отдаём результат,
        # чтобы вызывающий разобрался по выводу.
        LOGGER.warning(
            "Batch %d attempt %d/%d: sing-box вернул без urltest-результатов "
            "(вероятно занятый порт); retry на свежем порту.",
            batch_no, attempt, max_retries,
        )
    return result


def _evaluate_nodes(
    tag_to_line: dict[str, str],
    parsed: list[tuple[str, int | None]],
    state_map: dict[str, dict],
    serial_start: int,
    purge_below: int,
    known_countries: dict[str, str] | None = None,
) -> tuple[list[dict], set[str]]:
    """Чистая функция: решение по каждому узлу батча.

    Возвращает decisions — список словарей
      {tag, key, raw_line, ping_ms, is_ok, serial, prev_stable, new_stable,
       is_temp_banned}
    и country_check_lines — исходные строки ok-узлов, страну которых
    не удалось определить дёшево.

    Переход состояния (stable, fail_streak, temp_ban_until) считает
    compute_next_state() из server_store — та же стейт-машина, что и в БД:
      * успех — stable +1, серия неудач и бан сбрасываются;
      * TEMP_BAN_FAILS неудач подряд — временный чс (is_temp_banned=True):
        сервер пропадает из проверки и whitelist на TEMP_BAN_HOURS часов;
      * провал решающей проверки после бана — stable=FULL_BAN_STABLE.

    state_map: key -> {stable, fail_streak, temp_ban_until} из базы;
    обновляется по ходу (как раньше stable_map).
    Узлы с new_stable < purge_below помечаются is_excluded=True:
    их результат всё равно пишется в базу, а из пула проверки их
    исключит purge_dead() в конце цикла.
    """
    known_countries = known_countries or {}
    available_tags = {tag for tag, ping_ms in parsed if ping_ms is not None}
    tag_to_ping = dict(parsed)
    decisions: list[dict] = []
    country_check_lines: set[str] = set()
    serial = serial_start

    for tag, raw_line in tag_to_line.items():
        serial += 1
        is_ok = tag in available_tags
        ping_ms = tag_to_ping.get(tag)
        key = normalize_proxy_key(raw_line)
        prev = state_map.get(key) or {}
        prev_stable = int(prev.get("stable", 0) or 0)
        nxt = compute_next_state(
            prev_stable,
            int(prev.get("fail_streak", 0) or 0),
            prev.get("temp_ban_until"),
            ok=is_ok,
        )
        new_stable = nxt["stable"]
        state_map[key] = {
            "stable": new_stable,
            "fail_streak": nxt["fail_streak"],
            "temp_ban_until": nxt["temp_ban_until"],
        }

        # Страна известна, если эмодзи уже есть в имени строки или в базе.
        # В обоих случаях скоростной тест не нужен: эмодзи просто переносится
        # в пересобранную строку (это чинит потерю эмодзи после цикла,
        # где сервер не ответил и строка писалась без страны).
        name_country = tool.get_country_from_name(raw_line)
        db_country = known_countries.get(key)
        known_country = name_country or db_country

        decision = {
            "tag": tag,
            "key": key,
            "raw_line": raw_line,
            "ping_ms": ping_ms if is_ok else None,
            "is_ok": is_ok,
            "serial": serial,
            "prev_stable": prev_stable,
            "new_stable": new_stable,
            "is_temp_banned": bool(nxt["temp_ban_until"]),
            "is_excluded": new_stable < purge_below,
            "country_tag": known_country if is_ok else None,
            # Страна из имени считается один раз здесь и переиспользуется
            # в _finalize_rows — второй проход по 140+ регэкспам не нужен.
            "name_country": name_country,
        }
        if is_ok and not known_country and COUNTRY_CHECK_ENABLED:
            country_check_lines.add(raw_line)
        decisions.append(decision)
    return decisions, country_check_lines


def _finalize_rows(decisions: list[dict], speedtest_country: dict[str, str]) -> list[dict]:
    """Чистая функция: превращает решения в строки-записи для базы.

    row: {key, available, line, ping_ms, country, protocol} — формат record_results.
    """
    rows: list[dict] = []
    for d in decisions:
        # Свежий результат скоростного теста приоритетнее; иначе — уже
        # известная страна (из имени строки или из базы).
        country_tag = speedtest_country.get(d["raw_line"]) or d.get("country_tag")
        line_text = build_ping_result_line(
            d["raw_line"],
            seq_counter=d["serial"],
            ping_ms=d["ping_ms"],
            include_country=d["is_ok"],
            country_tag=country_tag,
            stable=d["new_stable"],
        )
        name_country = d.get("name_country")
        rows.append({
            "key": d["key"],
            "available": d["is_ok"],
            "line": line_text,
            "ping_ms": d["ping_ms"],
            "country": country_tag or name_country or "",
            "protocol": (tool.get_protocol(d["raw_line"]) or "").lower(),
            "capabilities": d.get("capabilities", ""),
        })
    return rows


def run_debug_ping_cycle(
    merge_path: str | Path,
    *,
    batch_size: int = URLTEST_BATCH_SIZE,
    urltest: str = URLTEST_URL,
    timeout: float = URLTEST_TIMEOUT,
    whitelist_path: str | Path | None = WHITELIST_FILE,
    blacklist_path: str | Path | None = BLACKLIST_FILE,
    db_path: str | Path | None = SERVERS_DB_FILE,
    export_lists: bool = True,
) -> dict:
    """Проверяет серверы из merge.txt через sing-box urltest и пишет результаты
    в центральную базу (ServerStore).

    stable: +1 за успешный пинг, -1 за неудачу; TEMP_BAN_FAILS неудач подряд
    отправляют сервер во временный чс (TEMP_BAN_HOURS часов вне проверки и
    whitelist), провал решающей проверки после бана — полноценный чс
    (stable=FULL_BAN_STABLE). По завершении цикла:
      * серверы со stable < PURGE_STABLE_BELOW (только полноценный чс)
        исключаются из проверочного пула;
      * whitelist.txt экспортируется из базы по фильтру stable > порога
        (серверы в действующем временном чсе не экспортируются);
      * blacklist.txt — проекция полноценного чса для наблюдения.
    """
    merge_file = Path(merge_path).resolve()
    lines = load_merge_lines(merge_file)

    store = ServerStore(db_path)
    state_map = store.load_state_map()
    known_countries = store.load_country_map()

    total = len(lines)
    processed = 0
    checked = 0
    available_count = 0
    failed_count = 0
    cycle_started = time.monotonic()

    for start in range(0, total, batch_size):
        batch = lines[start:start + batch_size]
        processed += len(batch)
        batch_end = min(start + len(batch), total)
        LOGGER.info("Checking batch %d-%d/%d", start + 1, batch_end, total)

        batch_started = time.monotonic()
        # Запуск sing-box с ретраями на инфраструктурные сбои (занятый порт
        # от зависшего экземпляра -> bind error -> ноль результатов). Ретраим
        # тем же батчем на СВЕЖЕМ порту (fresh_inbound_port выбирает новый),
        # вместо того чтобы списать весь батч в unavailable и уронить stable.
        result = _run_singbox_batch(batch, urltest, batch_no=start + 1)
        if result is None:
            continue
        batch_elapsed = time.monotonic() - batch_started
        LOGGER.info(
            "Batch %d-%d sing-box finished in %.2fs, captured %d output lines",
            start + 1, batch_end, batch_elapsed, len(result.get("output", [])),
        )

        output_lines = result.get("raw_output") or result.get("output", [])
        parsed = parse_ping_from_output(output_lines)
        parsed_nodes = result.get("parsed_nodes", [])
        parsed_node_lines = result.get("parsed_node_lines", [])
        tag_to_line = {
            node.get("tag"): line
            for node, line in zip(parsed_nodes, parsed_node_lines)
            if isinstance(node, dict) and node.get("tag")
        }
        missing_tags = [tag for tag, _ in parsed if tag not in tag_to_line]
        if missing_tags:
            LOGGER.warning(
                "Batch %d-%d parsed %d tags not found in original batch: %s",
                start + 1, batch_end, len(missing_tags), missing_tags[:10],
            )

        # Фаза 1: решение по узлам (stable +/-, временный чс) — чистая функция
        # над картой состояния базы.
        decisions, country_check_lines = _evaluate_nodes(
            tag_to_line, parsed, state_map,
            serial_start=start, purge_below=PURGE_STABLE_BELOW,
            known_countries=known_countries,
        )
        resolved_from_cache = sum(
            1 for d in decisions
            if d["is_ok"] and d.get("country_tag") and d["raw_line"] not in country_check_lines
        )

        # Фаза 2: страна через скоростной тест для ok-серверов без страны в имени.
        speedtest_country: dict[str, str] = {}
        if country_check_lines:
            from script.country_check import batch_country_check, country_to_emoji
            LOGGER.info(
                "Batch %d-%d: скоростной тест страны нужен для %d серверов "
                "(пропущено благодаря имени/базе: %d, concurrency=%d)",
                start + 1, batch_end, len(country_check_lines),
                resolved_from_cache, COUNTRY_CHECK_CONCURRENCY,
            )
            cc_results = batch_country_check(
                sorted(country_check_lines),
                concurrency=COUNTRY_CHECK_CONCURRENCY,
            )
            for line, res in cc_results.items():
                emoji = res.get("emoji") or country_to_emoji(res.get("country"))
                if emoji:
                    speedtest_country[line] = emoji
                    LOGGER.info(
                        "Скоростной тест: страна %s (%sms) для %s",
                        res.get("country"), res.get("latency_ms"), line[:80],
                    )

        # Фаза 2.5: профиль достижимости до целевых сайтов (reachability).
        # Для ok-серверов батча со stable >= порога проверяем, до каких целей
        # прокси реально дозванивается. Профиль пишется в БД (capabilities),
        # тэги [name] / [Global] добавляются только при экспорте в whitelist.
        capabilities_map: dict[str, str] = {}
        if REACHABILITY_ENABLED:
            from script.reachability_check import (
                load_targets,
                batch_reachability_check,
                profile_to_tags,
                profile_is_global,
            )
            reach_targets = load_targets()
            # Пул для профилирования: ok-серверы батча с достаточным stable.
            reach_lines = sorted({
                d["raw_line"] for d in decisions
                if d["is_ok"] and d["new_stable"] >= REACHABILITY_MIN_STABLE
            })
            if reach_lines and reach_targets:
                LOGGER.info(
                    "Batch %d-%d: reachability-профиль для %d ok-серверов "
                    "(целей: %d, concurrency=%d)",
                    start + 1, batch_end, len(reach_lines),
                    len(reach_targets), REACHABILITY_CONCURRENCY,
                )
                rr = batch_reachability_check(
                    reach_lines,
                    concurrency=REACHABILITY_CONCURRENCY,
                    targets=reach_targets,
                )
                for line, res in rr.items():
                    targets_res = res.get("targets")
                    if not targets_res:
                        continue
                    if profile_is_global(reach_targets, targets_res):
                        # Прошёл ВСЕ проверки -> в БД ровно глобальный тэг,
                        # на экспорте превратится в [Global].
                        capabilities_map[line] = REACHABILITY_GLOBAL_TAG
                        LOGGER.info("Reachability: %s -> [%s] (все цели)", line[:60], REACHABILITY_GLOBAL_TAG)
                    else:
                        reached = profile_to_tags(reach_targets, targets_res)
                        if reached:
                            capabilities_map[line] = ",".join(reached)
                            LOGGER.info("Reachability: %s -> [%s]", line[:60], "][".join(reached))

        # Фаза 3: строки-записи и запись результатов в базу одним commit'ом.
        # Профиль достижимости прошиваем в записи для capabilities.
        for d in decisions:
            d["capabilities"] = capabilities_map.get(d["raw_line"], "")
        rows = _finalize_rows(decisions, speedtest_country)
        store.record_results(rows)

        batch_ok = sum(1 for r in rows if r["available"])
        batch_fail = len(rows) - batch_ok
        batch_banned = sum(1 for d in decisions if d.get("is_temp_banned"))
        checked += len(rows)
        available_count += batch_ok
        failed_count += batch_fail
        LOGGER.info(
            "Batch %d-%d result: %d available, %d failed, %d записано в базу, "
            "temp-ban: %d (sing-box %.2fs + country/write %.2fs)",
            start + 1, batch_end, batch_ok, batch_fail, len(rows), batch_banned,
            batch_elapsed, (time.monotonic() - batch_started) - batch_elapsed,
        )

    # --- Пост-обработка: исключение мёртвых из пула и экспорт списков из базы ---
    purged = store.purge_dead(PURGE_STABLE_BELOW)
    stats = store.stats()

    whitelist_output = Path(whitelist_path) if whitelist_path else merge_file.with_suffix(".whitelist.txt")
    blacklist_output = Path(blacklist_path) if blacklist_path else merge_file.with_suffix(".blacklist.txt")
    exported_wl = exported_bl = 0
    if export_lists:
        exported_wl = store.export_tagged_to_file(
            whitelist_output,
            min_stable=WHITELIST_EXPORT_MIN_STABLE,
            global_tag=REACHABILITY_GLOBAL_TAG,
        )
        exported_bl = store.export_to_file(blacklist_output, max_stable=PURGE_STABLE_BELOW)

    total_elapsed = time.monotonic() - cycle_started
    LOGGER.info(
        "Ping cycle finished in %.2fs: processed=%d checked=%d available=%d failed=%d | "
        "db: total=%d active=%d excluded=%d proven=%d temp_banned=%d | "
        "excluded_now=%d wl_export=%d bl_export=%d",
        total_elapsed, processed, checked, available_count, failed_count,
        stats["total"], stats["active"], stats["excluded"],
        stats["zones"]["proven"], stats["zones"]["temp_banned"],
        purged, exported_wl, exported_bl,
    )

    # --- Авто-деплой собранного конфига в GitHub (если включён) ---
    deploy_result = None
    if DEPLOY_ENABLED:
        deploy_result = _auto_deploy()

    return {
        "merge_path": str(merge_file),
        "batch_size": batch_size,
        "processed": processed,
        "checked": checked,
        "available": available_count,
        "failed": failed_count,
        "excluded_now": purged,
        "whitelist_exported": exported_wl,
        "blacklist_exported": exported_bl,
        "whitelist_path": str(whitelist_output),
        "blacklist_path": str(blacklist_output),
        "db": stats,
        "deploy": deploy_result,
    }


def _auto_deploy():
    """Авто-деплой собранного конфига в GitHub после цикла проверки.

    Вызывается в конце run_debug_ping_cycle, когда whitelist уже обновлён.
    Никогда не бросает исключений наружу — при любой ошибке логирует и
    возвращает None, чтобы сбой деплоя не ломал основной цикл проверки.

    Мульти-режим: если задан DEPLOY_TEMPLATES (имена через запятую/пробел или
    'all' — все шаблоны из config/templates), деплоится КАЖДЫЙ шаблон, а
    итоговый файл называется именем шаблона. Иначе — один шаблон DEPLOY_TEMPLATE,
    как раньше.
    """
    try:
        from deploy_config import deploy, deploy_multi

        if DEPLOY_TEMPLATES:
            tpl_list = [t for t in DEPLOY_TEMPLATES.replace(",", " ").split() if t]
            LOGGER.info(
                "Авто-деплой (мульти) в GitHub: repo=%s templates=%s path=%s",
                GH_DEPLOY_REPO, tpl_list, DEPLOY_PATH,
            )
            res = deploy_multi(
                repo=GH_DEPLOY_REPO,
                templates=tpl_list,
                path=DEPLOY_PATH,
                silent=True,
            )
            if res.get("ok"):
                LOGGER.info(
                    "Авто-деплой выполнен: %s/%s шаблонов (ветка %s)",
                    res.get("deployed"), len(res.get("results") or []), res.get("branch"),
                )
                for r in res.get("results") or []:
                    LOGGER.info("  %s -> %s (commit %s)", r.get("template"), r.get("url"), r.get("commit_sha"))
            else:
                LOGGER.warning("Авто-деплой не выполнен: %s", res.get("error"))
            if any(r.get("url_uses_write_token") for r in res.get("results") or []):
                LOGGER.warning(
                    "GH_READ_TOKEN не задан: ссылки на скачивание содержат ДЕПЛОЙ-токен "
                    "(write). Задайте GH_READ_TOKEN и не раздавайте эти ссылки наружу."
                )
            return res

        LOGGER.info(
            "Авто-деплой конфига в GitHub: repo=%s template=%s path=%s",
            GH_DEPLOY_REPO, DEPLOY_TEMPLATE, DEPLOY_PATH,
        )
        res = deploy(
            repo=GH_DEPLOY_REPO,
            template=DEPLOY_TEMPLATE,
            path=DEPLOY_PATH,
            silent=True,
        )
        if res.get("ok"):
            LOGGER.info("Авто-деплой выполнен: %s (commit %s)", res.get("url"), res.get("commit_sha"))
            if res.get("url_uses_write_token"):
                LOGGER.warning(
                    "GH_READ_TOKEN не задан: ссылка на скачивание содержит ДЕПЛОЙ-токен "
                    "(write). Задайте GH_READ_TOKEN и не раздавайте эту ссылку наружу."
                )
        else:
            LOGGER.warning("Авто-деплой не выполнен: %s", res.get("error"))
        return res
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Авто-деплой упал: %s", exc)
        return None