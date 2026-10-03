"""Профилирование достижимости прокси до целевых сайтов (reachability check).

По аналогии с country_check: один процесс sing-box на батч прокси (N mixed-inbound
на 127.0.0.1, каждый жёстко привязан к своему outbound), и параллельные HTTP-запросы
через эти локальные порты. Но вместо geo-проб (определение страны) каждый порт
опрашивается по СПИСКУ ЦЕЛЕВЫХ сайтов из config/reachability_targets.json.

Цель: узнать, до каких целевых сайтов (openrouter, gemini, youtube, telegram, ...)
прокси реально дозванивается и с какой латентностью. Профиль записывается в БД
(колонка capabilities); тэги [name] в итоговую строку добавляются только при
экспорте в whitelist для генерации итогового конфига.

Сервер, прошедший проверку до ВСЕХ целевых сайтов, помечается тэгом [Global] —
такие серверы попадают в общий outbound 'proxy' через фильтры config.

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


# --- Кэш в рамках одного запуска --------------------------------------------
_profile_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


def _normalize_key(raw_line: str) -> str:
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(raw_line)


# --- Разбор прокси и сборка батч-конфига (как в country_check) --------------
def _parse_outbound(proxy_line: str, index: int, used_tags: set[str]):
    from script import core

    previous_providers = core.providers
    try:
        core.init_parsers()
        core.providers = {"exclude_protocol": "", "subscribes": []}

        nodes = core.get_nodes(proxy_line)
        if not nodes:
            return None, "не удалось распарсить прокси"
        node = nodes[0]
        if not isinstance(node, dict):
            return None, "не удалось распарсить прокси"

        tag = str(node.get("tag") or "").strip() or f"c{index}"
        base_tag = tag
        n = 0
        while tag in used_tags:
            n += 1
            tag = f"{base_tag}~{n}"
        used_tags.add(tag)
        node["tag"] = tag
        return node, None
    except Exception as exc:  # noqa: BLE001
        return None, f"ошибка разбора: {exc}"
    finally:
        core.providers = previous_providers


def _build_batch_config(entries: list[dict], template_path: Path) -> dict:
    base = json.loads(Path(template_path).read_text(encoding="utf-8"))

    inbounds: list[dict] = []
    outbounds: list[dict] = []
    endpoints: list[dict] = []
    rules: list[dict] = []

    for e in entries:
        inbound_tag = f"cin-{e['index']}"
        inbounds.append({
            "tag": inbound_tag,
            "type": "mixed",
            "listen": "127.0.0.1",
            "listen_port": e["port"],
        })
        ob = e["outbound"]
        target_tag = ob["tag"]
        if ob.get("type") == "wireguard":
            endpoints.append(ob)
        else:
            outbounds.append(ob)
        rules.append({"inbound": [inbound_tag], "outbound": target_tag})

    outbounds.append({"tag": "direct", "type": "direct"})
    rules.append({"protocol": "dns", "outbound": "direct"})

    route = {
        "default_domain_resolver": (base.get("route") or {}).get(
            "default_domain_resolver", {"server": "dns-google-v4"}
        ),
        "auto_detect_interface": True,
        "final": "direct",
        "rules": rules,
    }

    config: dict = {
        "log": {"level": "error", "timestamp": False},
        "dns": base.get("dns"),
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": route,
    }
    if endpoints:
        config["endpoints"] = endpoints
    return config




def _start_singbox(config: dict, config_path: Path):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False)
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen([str(get_settings().paths.sing_box_path), "run", "-c", str(config_path)], **kwargs)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Не удалось запустить sing-box: %s", exc)
        return None


def _ports_ready(ports: list[int], deadline_seconds: float) -> bool:
    deadline = time.monotonic() + deadline_seconds
    remaining = set(ports)
    while time.monotonic() < deadline and remaining:
        for p in list(remaining):
            try:
                with socket.create_connection(("127.0.0.1", p), timeout=0.35):
                    remaining.discard(p)
            except OSError:
                pass
        if remaining:
            time.sleep(0.15)
    return not remaining

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
        outbound, err = _parse_outbound(line, idx, used_tags)
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

    config = _build_batch_config(active, ROOT / str(get_settings().paths.countrytest_template))
    config_path = ROOT / "source" / "tests" / f"reach_batch_{items[0][1]}_{len(items)}.json"
    proc = _start_singbox(config, config_path)

    started_ok = False
    if proc is not None and proc.poll() is None:
        started_ok = _ports_ready([e["port"] for e in active], STARTUP_WAIT_SECONDS)

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
            key = _normalize_key(line)
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
            key = _normalize_key(line)
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


def profile_to_tags(targets: list[dict], reach_result: dict[str, dict]) -> list[str]:
    """Превращает результат пробы в упорядоченный список тэгов [name].

    Тэг добавляется для каждой цели, где ok=True. Порядок — как в списке целей.
    """
    reached = [t["tag"] for t in targets
               if (reach_result.get(t["name"]) or {}).get("ok")]
    return reached


def profile_is_global(targets: list[dict], reach_result: dict[str, dict]) -> bool:
    """True, если прокси достиг ВСЕХ целей списка (кандидат на [Global])."""
    if not targets:
        return False
    return all((reach_result.get(t["name"]) or {}).get("ok") for t in targets)


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
            reached = profile_to_tags(all_targets, res["targets"])
            print("Тэги: " + ", ".join(f"[{t}]" for t in reached) or "(нет)")
            for name, entry in res["targets"].items():
                print(
                    f"  {name}: ttfb={entry.get('ttfb_ms')}мс "
                    f"(порог {entry.get('max_ping_ms')}мс, статус {entry.get('status')}, "
                    f"тело {entry.get('body_bytes')}Б)"
                    + (f" ошибка: {entry['error']}" if entry.get("error") else "")
                )
            if profile_is_global(all_targets, res["targets"]):
                print("=> [Global] (достиг всех целей)")