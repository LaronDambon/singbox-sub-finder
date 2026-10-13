"""Профилирование достижимости прокси до целевых сайтов (reachability check).

По аналогии с country_check: один процесс sing-box на батч прокси (N mixed-inbound
на 127.0.0.1, каждый жёстко привязан к своему outbound), и параллельные HTTP-запросы
через эти локальные порты. Но вместо geo-проб (определение страны) каждый порт
опрашивается по СПИСКУ ЦЕЛЕВЫХ сайтов из config/reachability_targets.json.

Цель: узнать, до каких целевых сайтов (openrouter, gemini, youtube, telegram, ...)
прокси реально дозванивается и с какой латентностью. Профиль записывается в БД
(колонка capabilities); тэги [name] в итоговую строку добавляются только при
экспорте в whitelist для генерации итогового конфига.

Особые метки собираются блоком "merge" того же файла: группа — это список
тэгов целей и порог min_matches. Сервер, прошедший порог, помечается в БД
одним общим тегом вместо тегов группы. Пример: [Global] — любые 3 из 3 целей
gstatic / yandex-portal / cloudflare (generate_204). Раньше [Global] требовал
пройти ВСЕ цели профиля разом, и добавление девятой цели отнимало метку у
всех разом.

Задержка здесь — это TTFB: время до ЗАГОЛОВКОВ ответа (stream=True), а не
время выкачивания страницы. Тело обрывается через REACHABILITY_BODY_BYTES.
Порог — REACHABILITY_MAX_PING_MS (или max_ping_ms цели), и сравнивается
именно с TTFB: полное время загрузки страницы с порогом пинга не сравнимо
(openai отвечает 404 за 3.4 с на живом прокси — это нормальный TTFB).

CLI:
  python -m script.reachability_check '<proxy_line>'
  python -m script.reachability_check --targets openrouter,gemini '<proxy_line>'
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]

from config.settings import get_settings

from script.logger_utils import get_project_logger
from script.core import reserve_ports
from script.batch_singbox import (
    build_batch_config, normalize_key, parse_outbound,
    ports_ready, start_singbox,
)

LOGGER = get_project_logger("reachability_check")

# --- Параметры probing -------------------------------------------------------
# Таймаут CONNECT одного целевого запроса, сек. Остальные параметры — из
# настроек, и читаются они в точке использования: REACHABILITY_TIMEOUT в
# _probe_target, REACHABILITY_BODY_* там же, REACHABILITY_MAX_PING_MS в
# load_targets/_probe_target/_log_probe_stats, а параллелизм внутри одного
# прокси (REACHABILITY_PER_PROXY_CONCURRENCY) — в _run_batch.
PROBE_CONNECT_TIMEOUT = 4.0
# Ожидание готовности inbound-портов при старте sing-box, сек.
STARTUP_WAIT_SECONDS = 10.0
# Максимум inbound'ов (и прокси) в одном процессе sing-box.
MAX_BATCH_INBOUNDS = 100

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# --- Загрузка списка целевых сайтов ------------------------------------------
def load_targets(path: str | Path | None = None) -> list[dict]:
    """Читает config/reachability_targets.json и возвращает список целевых сайтов.

    Каждая цель: {name, tag, url, max_ping_ms, timeout_ms, expected_statuses, headers}
    Пороги по умолчанию подставляются, если не заданы.
    """
    path = Path(path) if path else Path(str(get_settings().paths.reachability_targets_file))
    if not path.exists():
        raise FileNotFoundError(f"Файл целевых сайтов не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(targets, list):
        raise ValueError(f"Неверный формат {path}: ожидается список 'targets'")
    out = []
    # Порог TTFB по умолчанию, если цель не задала max_ping_ms.
    default_ping = int(get_settings().reach.max_ping_ms or 500)
    for t in targets:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("tag") or "").strip()
        url = str(t.get("url") or "").strip()
        if not name or not url:
            continue
        out.append({
            "name": name,
            "tag": str(t.get("tag") or name).strip(),
            "url": url,
            "max_ping_ms": int(t.get("max_ping_ms") or default_ping),
            "timeout_ms": int(t.get("timeout_ms") or 6000),
            "expected_statuses": list(t.get("expected_statuses") or [200, 204, 301, 302]),
            "headers": dict(t.get("headers") or {"User-Agent": BROWSER_UA}),
        })
    return out


# --- Группы слияния тегов (блок merge) ---------------------------------------
def _merge_rule(rule: dict) -> dict | None:
    """Одна запись блока merge: {tag, tags, min_matches, ...} или None, если пуста.

    min_matches по умолчанию — все перечисленные теги (то есть прежнее
    «прошёл все цели группы»); число меньше длины списка задаёт «любые N из».
    remove_tags по умолчанию True: смысл записи в том, чтобы вместо N тегов
    группы в профиле остался ОДИН общий.
    """
    tag = str(rule.get("tag") or "").strip()
    tags = list(dict.fromkeys(
        str(t).strip() for t in (rule.get("tags") or []) if str(t).strip()
    ))
    if not tag or not tags:
        return None
    raw_min = rule.get("min_matches")
    try:
        min_matches = int(raw_min) if raw_min is not None else len(tags)
    except (TypeError, ValueError):
        min_matches = len(tags)
    # Порог вне диапазона 1..len(tags) сделал бы группу либо вечной, либо
    # недостижимой: «3 из 2» не наберёт никто, «0 из 3» — любой сервер.
    min_matches = max(1, min(min_matches, len(tags)))
    return {
        "tag": tag,
        "tags": tags,
        "min_matches": min_matches,
        "remove_tags": bool(rule.get("remove_tags", True)),
    }


def load_merge_rules(path: str | Path | None = None) -> list[dict]:
    """Группы слияния тегов: блок merge из файла целей плюс окружение.

    Запись группы: сервер, прошедший min_matches целей из tags, получает ОДИН
    общий тег tag вместо тегов этих целей. Пример: три generate_204 (gstatic,
    yandex-portal, cloudflare) -> [Global].

    Источника два, и они складываются: правила из REACHABILITY_MERGE_TAGS идут
    первыми, группы из файла добавляются к ним. Тег, названный в обоих местах,
    берётся из окружения — так рабочую группу можно переопределить, не трогая
    файл.

    Ошибки здесь не поднимаются: недоступный или битый файл — это отсутствие
    групп, а не падение прогона. Профиль целей грузится отдельно и о своей
    беде сообщает сам.
    """
    rules = default_merge_rules()
    try:
        cfg_path = Path(path) if path else Path(
            str(get_settings().paths.reachability_targets_file)
        )
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = None
    raw = data.get("merge") if isinstance(data, dict) else None
    if isinstance(raw, list):
        for rule in raw:
            if not isinstance(rule, dict):
                continue
            parsed = _merge_rule(rule)
            # Тег, названный в окружении, из файла не дублируется: правило
            # окружения идёт первым и при совпадении тега выигрывает оно.
            if parsed and not any(parsed["tag"] == r["tag"] for r in rules):
                rules.append(parsed)
    if not rules:
        return []
    warn_about_missing_merge_targets(rules)
    return rules


def default_merge_rules() -> list[dict]:
    """Группы слияния из REACHABILITY_MERGE_TAGS, если она задана.

    Формат строки: "Global=gstatic,yandex-portal,cloudflare:3;Fast=a,b:2".
    Порог после «:» необязателен — без него нужны все теги группы.
    Настройка НЕ отменяет блок merge из файла: она добавляется к нему и
    идёт первой (при совпадении тега выигрывает она).
    """
    raw = str(get_settings().reach.merge_tags or "").strip()
    if not raw:
        return []
    rules: list[dict] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        tag, _, rest = chunk.partition("=")
        body, _, threshold = rest.partition(":")
        parsed = _merge_rule({
            "tag": tag.strip(),
            "tags": [s.strip() for s in body.split(",") if s.strip()],
            "min_matches": threshold.strip() or None,
        })
        if parsed:
            rules.append(parsed)
    return rules


def warn_about_missing_merge_targets(rules: list[dict],
                                     targets: list[dict] | None = None) -> list[str]:
    """Логирует группы merge, ссылающиеся на цели, которых в профиле нет.

    Опечатка в имени тега не ломает прогон, но делает группу недостижимой:
    min_matches из трёх тегов не наберётся, если один из них не проверяется.
    Возвращает имена отсутствующих тегов — ради этого списка функция и
    вызывается повторно из чекера, когда цели уже загружены.
    """
    if not rules:
        return []
    if targets is None:
        try:
            targets = load_targets()
        except Exception:  # noqa: BLE001 — предупреждение не стоит падения
            return []
    known = {
        str(t.get("tag") or t.get("name") or "").strip().lower() for t in targets
    }
    missing = sorted({
        tag for rule in rules for tag in rule["tags"] if tag.lower() not in known
    })
    if missing:
        LOGGER.warning(
            "Merge: теги %s перечислены в группах, но таких целей нет в профиле — "
            "группа останется недостижимой", ", ".join(missing),
        )
    return missing


# --- Кэш в рамках одного запуска --------------------------------------------
_profile_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


# --- Разбор прокси и сборка батч-конфига (как в country_check) --------------
# --- Reachability-проба конкретного порта по всем целям ----------------------
def _drain_body(resp, *, max_bytes: int, max_seconds: float) -> int:
    """Дочитывает первые max_bytes тела и обрывает его. Возвращает число байт.

    Тот же приём, что _drain в script/speed_check.py: тело читается порциями
    и закрывается на первом же превышении, поэтому страница не уезжает целиком
    (gemini.google.com один занимал 856 КБ на каждый прокси). Ошибки чтения
    здесь не важны — TTFB уже измерен по заголовкам.
    """
    total = 0
    started = time.monotonic()
    try:
        for chunk in resp.iter_content(16384):
            if not chunk:
                continue
            total += len(chunk)
            if total >= max_bytes or (time.monotonic() - started) >= max_seconds:
                break
    except Exception:  # noqa: BLE001 — тело нам не нужно, важны только заголовки
        pass
    return total


def _probe_target(port: int, target: dict, *, max_ping_ms: int | None = None) -> dict:
    """Одна проба «прокси -> целевой сайт». Меряет TTFB, а не время загрузки.

    stream=True отдаёт управление, как только пришли заголовки ответа, — это и
    есть задержка до целевого сайта. Дальше тело добирается порциями и
    сразу обрывается. Сравнивается с порогом именно TTFB: время выкачивания
    страницы к пингу отношения не имеет.
    """
    connect_to = PROBE_CONNECT_TIMEOUT
    # Таймаут READ одного целевого запроса, сек (REACHABILITY_TIMEOUT).
    read_to = float(get_settings().reach.timeout or 6.0)
    tm = target.get("timeout_ms")
    if tm:
        read_to = max(0.5, tm / 1000.0)
    # Тело ответа нужно только чтобы убедиться, что соединение живое: читаем
    # первые ~64 КБ и обрываем (тот же приём, что _drain в script/speed_check.py).
    body_max_bytes = int(get_settings().reach.body_bytes or 65536)
    body_max_seconds = float(get_settings().reach.body_seconds or 2.0)
    # Порог TTFB по умолчанию, если не заданы ни явный порог, ни порог цели.
    default_ping = int(get_settings().reach.max_ping_ms or 500)
    # Явный max_ping_ms (настройка конвейера) важнее порога цели; если его
    # нет — берём max_ping_ms цели, иначе REACHABILITY_MAX_PING_MS.
    threshold = int(max_ping_ms or target.get("max_ping_ms") or default_ping)

    start = time.monotonic()
    entry = {
        "ok": False,
        "status": None,
        "latency_ms": None,   # синоним ttfb_ms, оставлен для совместимости
        "ttfb_ms": None,
        "body_bytes": 0,
        "max_ping_ms": threshold,
        "error": None,
    }
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    session = requests.Session()
    session.trust_env = False  # игнорировать системные HTTP(S)_PROXY
    resp = None
    try:
        resp = session.get(
            target["url"],
            proxies=proxies,
            timeout=(connect_to, read_to),
            headers=target.get("headers") or {"User-Agent": BROWSER_UA},
            allow_redirects=True,
            stream=True,
        )
        # Заголовки получены — это и есть TTFB (включая редиректы).
        ttfb_ms = round((time.monotonic() - start) * 1000.0, 1)
        entry["status"] = resp.status_code
        entry["ttfb_ms"] = ttfb_ms
        entry["latency_ms"] = ttfb_ms
        entry["body_bytes"] = _drain_body(
            resp,
            max_bytes=body_max_bytes,
            max_seconds=min(body_max_seconds, read_to),
        )
        # "дозвонился" = получил любой HTTP-статус (в т.ч. 403/429/404 — это
        # отказ ПОСЛЕ прохода через прокси, сам прокси жив) И уложился в порог.
        entry["ok"] = ttfb_ms <= threshold
    except Exception as exc:  # noqa: BLE001
        entry["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        session.close()
    return entry


def _probe_port(port: int, targets: list[dict], *,
                max_ping_ms: int | None = None) -> dict:
    """Проверяет доступность целевых сайтов через локальный порт-прокси.

    Возвращает {target_name: {"ok", "status", "ttfb_ms"/"latency_ms",
    "body_bytes", "error"}}. Сервер считается достижившим цель, если пришли
    заголовки ответа (нет connect/read-сбоя) И TTFB <= порога.
    """
    return {t["name"]: _probe_target(port, t, max_ping_ms=max_ping_ms) for t in targets}


def _probe_single(port: int, target: dict, *,
                  max_ping_ms: int | None = None,
                  gate: threading.Semaphore | None = None) -> tuple[str, dict]:
    """Одна проба: один прокси — одна цель.

    Раньше все цели обходились последовательно внутри одного прокси, и батч
    из двух серверов с восемью целями растягивался на 80+ секунд: каждая цель
    ждала своего connect+read таймаута по очереди. Теперь цели независимы и
    уходят в общий пул потоков.

    gate — семафор на конкретный прокси: не даёт нескольким целям одновременно
    открывать соединения через один узел, иначе в замер TTFB попадает очередь
    внутри sing-box (см. REACHABILITY_PER_PROXY_CONCURRENCY).
    """
    if gate is None:
        return target["name"], _probe_target(port, target, max_ping_ms=max_ping_ms)
    with gate:
        return target["name"], _probe_target(port, target, max_ping_ms=max_ping_ms)


# --- Батч-исполнение ---------------------------------------------------------
def _run_batch(items: list[tuple[str, int]], results: dict[str, dict],
               targets: list[dict], concurrency: int, depth: int = 0,
               max_ping_ms: int | None = None) -> None:
    """items: [(line, index)] — проверяет батч одним процессом sing-box.

    При инфраструктурном сбое старта делит батч пополам (рекурсия).
    """
    if not items:
        return

    used_tags: set[str] = set()
    entries: list[dict] = []
    for line, idx in items:
        outbound, err = parse_outbound(line, idx, used_tags)
        if err:
            results[line] = {"error": err, "targets": {}}
            continue
        entries.append({"index": idx, "port": 0, "outbound": outbound})

    active = [e for e in entries]
    if not active:
        return

    ports = reserve_ports(len(active))
    for e, p in zip(active, ports):
        e["port"] = p

    config = build_batch_config(active, ROOT / str(get_settings().paths.countrytest_template))
    config_path = ROOT / "source" / "tests" / f"reach_batch_{items[0][1]}_{len(items)}.json"
    proc = start_singbox(config, config_path)

    started_ok = False
    if proc is not None and proc.poll() is None:
        started_ok = ports_ready([e["port"] for e in active], STARTUP_WAIT_SECONDS)

    if not started_ok:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                proc.kill()
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass
        if len(items) > 1 and depth < 8:
            mid = len(items) // 2
            LOGGER.warning(
                "Reach батч из %d прокси не стартовал, делю пополам (%d + %d)",
                len(items), mid, len(items) - mid,
            )
            _run_batch(items[:mid], results, targets, concurrency, depth + 1, max_ping_ms)
            _run_batch(items[mid:], results, targets, concurrency, depth + 1, max_ping_ms)
        else:
            for line, _ in items:
                if line not in results:
                    results[line] = {"error": "sing-box не смог обработать этот конфиг", "targets": {}}
        return

    try:
        port_by_index = {e["index"]: e["port"] for e in active}
        pairs = [(line, port_by_index[idx]) for line, idx in items if idx in port_by_index]

        # Параллелим сразу по двум осям: прокси И цели. Иначе восемь целей
        # шли последовательно и батч из двух серверов занимал больше минуты.
        # Но через ОДИН прокси цели ждут друг друга (gate): иначе все восемь
        # соединений встают в очередь внутри sing-box и TTFB выходит в разы
        # больше настоящей задержки до сайта.
        # Сколько целей одновременно опрашиваем через один и тот же прокси
        # (REACHABILITY_PER_PROXY_CONCURRENCY). Каждая проба открывает НОВОЕ
        # соединение с узлом (TCP+TLS внутри sing-box), поэтому при
        # одновременных 8 пробах они стоят в очереди друг за другом и в
        # измеренный TTFB попадает время ожидания в этой очереди: замер в 3-6
        # раз больше настоящей задержки до сайта, и ни один живой сервер не
        # проходит порог. По умолчанию — одна цель за раз на прокси, а
        # параллелизм остаётся между прокси.
        per_proxy = max(1, int(get_settings().reach.per_proxy_concurrency or 1))
        gates = {e["port"]: threading.Semaphore(per_proxy) for e in active}
        jobs = [(line, port, target) for line, port in pairs for target in targets]
        # Потоков нужно ровно столько, сколько одновременных соединений держит
        # sing-box: по одному на прокси (или столько, сколько разрешает
        # REACHABILITY_PER_PROXY_CONCURRENCY). Больше не нужно: остальное всё
        # равно упрётся в gate своего прокси.
        workers = max(1, min(
            len(jobs), max(concurrency, len(pairs) * per_proxy)
        ))
        lock = threading.Lock()
        for line, _ in pairs:
            results.setdefault(line, {"error": None, "targets": {}})
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _probe_single, port, target,
                    max_ping_ms=max_ping_ms, gate=gates.get(port),
                ): (line, target["name"])
                for line, port, target in jobs
            }
            for fut in as_completed(futures):
                line, _expected = futures[fut]
                name, entry = fut.result()
                with lock:
                    results[line]["targets"][name] = entry
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            proc.kill()
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass


def batch_reachability_check(proxy_lines: list[str], *,
                             concurrency: int | None = None,
                             batch_size: int = MAX_BATCH_INBOUNDS,
                             targets: list[dict] | None = None,
                             target_names: list[str] | None = None,
                             max_ping_ms: int | None = None) -> dict[str, dict]:
    """Проверяет доступность до целевых сайтов для списка прокси.

    targets — список целей; если None (или пусто), берётся список из
    config/reachability_targets.json. target_names — фильтр по именам.
    max_ping_ms — порог TTFB в мс: None = у каждой цели свой max_ping_ms,
    а где его нет — REACHABILITY_MAX_PING_MS из config/settings.py; число = жёсткое
    переопределение порога для всех целей.

    Возвращает {proxy_line: {"error": str|None, "targets": {name: {ok, status,
    ttfb_ms/latency_ms, body_bytes, error}}}}.
    """
    if concurrency is None:
        concurrency = get_settings().reach.concurrency
    concurrency = max(1, int(concurrency))

    all_targets = targets if targets is not None else load_targets()
    if target_names:
        wanted = set(target_names)
        all_targets = [t for t in all_targets if t["name"] in wanted]

    results: dict[str, dict] = {}
    pending: list[str] = []
    seen_keys: set[str] = set()

    with _cache_lock:
        for raw in proxy_lines:
            line = str(raw).strip()
            if not line:
                continue
            key = normalize_key(line)
            if key in _profile_cache:
                cached = _profile_cache[key]
                results[line] = dict(cached) if cached else {"error": "кэш запуска", "targets": {}}
                continue
            seen_keys.add(key)
            pending.append(line)

    if not pending:
        return results

    indexed = [(line, i) for i, line in enumerate(pending)]
    total_batches = (len(indexed) + batch_size - 1) // batch_size
    for bi, start in enumerate(range(0, len(indexed), batch_size)):
        chunk = indexed[start:start + batch_size]
        LOGGER.info(
            "Reach batch %d/%d: %d прокси, %d целей (concurrency=%d)",
            bi + 1, total_batches, len(chunk), len(all_targets), concurrency,
        )
        batch_started = time.monotonic()
        _run_batch(chunk, results, all_targets, concurrency, 0, max_ping_ms)
        LOGGER.info(
            "Reach batch %d/%d завершён за %.2fs", bi + 1, total_batches,
            time.monotonic() - batch_started,
        )
    _log_probe_stats(results, all_targets, max_ping_ms)

    with _cache_lock:
        for line in pending:
            res = results.get(line)
            key = normalize_key(line)
            if res and res.get("targets"):
                _profile_cache[key] = dict(res)

    return results


def _log_probe_stats(results: dict[str, dict], targets: list[dict],
                     max_ping_ms: int | None = None) -> None:
    """Итог по пробам: сколько целей пройдено по TTFB и сколько байт скачано.

    Тело обрывается через REACHABILITY_BODY_BYTES, поэтому трафик на сервер
    урезан в разы по сравнению с полной выкачкой страниц.
    """
    ok_by_target = {t["name"]: 0 for t in targets}
    body_bytes = 0
    for res in results.values():
        for name, entry in (res.get("targets") or {}).items():
            if name not in ok_by_target:
                continue
            if entry.get("ok"):
                ok_by_target[name] += 1
            body_bytes += int(entry.get("body_bytes") or 0)
    default_ping = (
        max_ping_ms if max_ping_ms is not None
        else int(get_settings().reach.max_ping_ms or 500)
    )
    detail = ", ".join(
        f"{name}={ok_by_target[name]}/{len(results)} (порог "
        f"{next((t.get('max_ping_ms') for t in targets if t['name'] == name), default_ping)}мс)"
        for name in ok_by_target
    )
    LOGGER.info(
        "Reach итог: %d/%d серверов с тэгами; тела прочитано %.1f КБ "
        "(порог по умолчанию %dмс) | %s",
        sum(1 for r_ in results.values()
            if r_.get("targets") and any(e.get("ok") for e in r_["targets"].values())),
        len(results),
        body_bytes / 1024.0,
        default_ping,
        detail,
    )


def check_reachability(proxy_line: str, *, targets: list[dict] | None = None,
                       target_names: list[str] | None = None,
                       max_ping_ms: int | None = None) -> dict:
    """Совместимость: профиль одного прокси (обёртка над батчем из 1)."""
    res_map = batch_reachability_check(
        [proxy_line], targets=targets, target_names=target_names,
        max_ping_ms=max_ping_ms,
    )
    return res_map.get(proxy_line.strip()) or {"error": "нет результата", "targets": {}}


def _reached_tags(targets: list[dict], reach_result: dict[str, dict]) -> list[str]:
    """Теги целей, которые прокси прошёл. Порядок — как в списке целей.

    Тег, совпавший с уже добавленным, пропускается: два имени цели могут
    указывать на один URL и один тег, и профиль тогда молча терял бы часть
    совпадений группы merge (порог «3 из 3» не набрался бы у сервера,
    прошедшего все три URL, — просто потому что целей с таким тегом две).
    """
    out: list[str] = []
    for t in targets:
        if not (reach_result.get(t["name"]) or {}).get("ok"):
            continue
        tag = str(t.get("tag") or t["name"])
        if tag not in out:
            out.append(tag)
    return out


def profile_merge_tags(targets: list[dict], reach_result: dict[str, dict],
                       merge_rules: list[dict] | None = None) -> list[str]:
    """Общие теги групп, набранных прокси (например ["Global"]).

    Порядок — как в списке групп; первое правило, забравшее тег, выигрывает.
    """
    if merge_rules is None:
        merge_rules = load_merge_rules()
    reached = _reached_tags(targets, reach_result)
    lowered = {tag.lower() for tag in reached}
    matched: list[str] = []
    claimed: set[str] = set()
    for rule in merge_rules or []:
        tag = rule["tag"]
        if tag in claimed:
            continue
        # Цель группы засчитывается только если её тег вообще проверялся:
        # иначе опечатка в merge давала бы совпадение «всех» тегов на пустом месте.
        hits = [t for t in rule["tags"] if t.lower() in lowered]
        if len(hits) >= rule["min_matches"]:
            matched.append(tag)
            claimed.add(tag)
    return matched


def profile_to_tags(targets: list[dict], reach_result: dict[str, dict],
                    merge_rules: list[dict] | None = None) -> list[str]:
    """Превращает результат пробы в упорядоченный список тэгов [name].

    Тэг добавляется для каждой цели, где ok=True. Порядок — как в списке целей.

    Группы merge сворачивают свои теги в один общий: сервер, прошедший три
    generate_204, получает один тэг Global вместо трёх тегов этих целей
    (у группы remove_tags=True). Теги остальных целей остаются на месте.
    """
    if merge_rules is None:
        merge_rules = load_merge_rules()
    reached = _reached_tags(targets, reach_result)
    merged: list[str] = []
    hidden: set[str] = set()
    # Регистр имени группы и её тегов: сервер, прошедший цель yandex-portal,
    # не должен получать сворачивание группы ["yandex-portal"].
    lowered = {tag.lower() for tag in reached}
    for rule in merge_rules or []:
        tag = rule["tag"]
        hits = [t for t in rule["tags"] if t.lower() in lowered]
        if len(hits) < rule["min_matches"]:
            continue
        # Тег группы может быть и именем самой цели: тогда сворачивать нечего.
        if rule["remove_tags"]:
            hidden.update(t.lower() for t in rule["tags"] if t != tag)
        if tag not in merged:
            merged.append(tag)
    out = merged + [
        tag for tag in reached
        if tag.lower() not in hidden and tag not in merged
    ]
    return list(dict.fromkeys(out))


def profile_is_global(targets: list[dict], reach_result: dict[str, dict],
                      merge_rules: list[dict] | None = None,
                      global_tag: str | None = None) -> bool:
    """True, если прокси набрал группу с тегом Global (кандидат на [Global]).

    Раньше здесь было «достиг ВСЕХ целей списка»: порог задавала не цель, а
    весь профиль, и стоило добавить девятую цель — метка [Global] пропадала
    у всех. Теперь порог берётся из группы merge (три generate_204), а
    остальные цели на неё не влияют.
    """
    if global_tag is None:
        global_tag = str(getattr(get_settings().reach, "global_tag", "") or "Global")
    return global_tag in profile_merge_tags(targets, reach_result, merge_rules)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Reachability profile for proxy lines")
    parser.add_argument("lines", nargs="*", help="proxy lines (or read stdin)")
    parser.add_argument("--targets", default=None, help="comma-separated target names to probe")
    args = parser.parse_args()

    if not args.lines:
        data = sys.stdin.read()
        args.lines = [l for l in data.splitlines() if l.strip()]

    if len(sys.argv) < 2 and not args.lines:
        print("Использование: python -m script.reachability_check '<proxy_line>'")
        sys.exit(1)

    name_filter = [s.strip() for s in (args.targets or "").split(",") if s.strip()] or None
    for line in args.lines:
        res = check_reachability(line, target_names=name_filter)
        print("===== " + (line[:80]) + " =====")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("targets") and not res.get("error"):
            all_targets = load_targets()
            merge_rules = load_merge_rules()
            reached = profile_to_tags(all_targets, res["targets"], merge_rules)
            print("Тэги: " + (", ".join(f"[{t}]" for t in reached) or "(нет)"))
            for name, entry in res["targets"].items():
                print(
                    f"  {name}: ttfb={entry.get('ttfb_ms')}мс "
                    f"(порог {entry.get('max_ping_ms')}мс, статус {entry.get('status')}, "
                    f"тело {entry.get('body_bytes')}Б)"
                    + (f" ошибка: {entry['error']}" if entry.get("error") else "")
                )
            for rule in merge_rules:
                hits = [t for t in rule["tags"]
                        if (res["targets"].get(t) or {}).get("ok")]
                mark = "=>" if len(hits) >= rule["min_matches"] else "  "
                print(
                    f"{mark} [{rule['tag']}]: {len(hits)}/{rule['min_matches']} "
                    f"из {list(rule['tags'])}"
                )