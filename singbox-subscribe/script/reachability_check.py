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

from config.settings import (
    SING_BOX_PATH,
    COUNTRYTEST_TEMPLATE,
    REACHABILITY_TARGETS_FILE,
    REACHABILITY_TIMEOUT,
)

from script.logger_utils import get_project_logger

LOGGER = get_project_logger("reachability_check")

# --- Параметры probing -------------------------------------------------------
# Таймаут одного целевого запроса: (connect, read), сек.
PROBE_CONNECT_TIMEOUT = 4.0
PROBE_READ_TIMEOUT = float(REACHABILITY_TIMEOUT) if REACHABILITY_TIMEOUT else 6.0
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
    path = Path(path) if path else Path(REACHABILITY_TARGETS_FILE)
    if not path.exists():
        raise FileNotFoundError(f"Файл целевых сайтов не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(targets, list):
        raise ValueError(f"Неверный формат {path}: ожидается список 'targets'")
    out = []
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
            "max_ping_ms": int(t.get("max_ping_ms") or 500),
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

    old_cwd = os.getcwd()
    previous_providers = core.providers
    try:
        os.chdir(ROOT)
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
        os.chdir(old_cwd)
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


def _reserve_ports(count: int) -> list[int]:
    ports: list[int] = []
    base_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    base_sock.bind(("127.0.0.1", 0))
    candidate = base_sock.getsockname()[1]
    base_sock.close()
    used: set[int] = set()
    while len(ports) < count:
        if candidate not in used:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", candidate))
                ports.append(candidate)
                used.add(candidate)
            except OSError:
                pass
            finally:
                s.close()
        candidate += 1
    return ports


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
        return subprocess.Popen([str(SING_BOX_PATH), "run", "-c", str(config_path)], **kwargs)
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
def _probe_port(port: int, targets: list[dict]) -> dict:
    """Проверяет доступность целевых сайтов через локальный порт-прокси.

    Возвращает {target_name: {"ok": bool, "status": int|None, "latency_ms": int|None, "error": str|None}}
    Сервер считается достижившим цель, если запрос завершился (нет connect/read-сбоя)
    И пинг <= max_ping_ms.
    """
    result: dict[str, dict] = {}
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    session = requests.Session()
    session.trust_env = False  # игнорировать системные HTTP(S)_PROXY

    for t in targets:
        name = t["name"]
        connect_to, read_to = PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT
        tm = t.get("timeout_ms")
        if tm:
            read_to = max(0.5, tm / 1000.0)
        start = time.monotonic()
        entry = {"ok": False, "status": None, "latency_ms": None, "error": None}
        try:
            resp = session.get(
                t["url"],
                proxies=proxies,
                timeout=(connect_to, read_to),
                headers=t.get("headers") or {"User-Agent": BROWSER_UA},
                allow_redirects=True,
            )
            elapsed = round((time.monotonic() - start) * 1000.0, 1)
            entry["status"] = resp.status_code
            entry["latency_ms"] = elapsed
            # "дозвонился" = завершил прокси-запрос с любым HTTP-статусом
            # (в т.ч. 403/429 — это отказ ПОСЛЕ прохода через прокси, прокси жив).
            # Считаем ok, если пинг уложился в порог.
            entry["ok"] = elapsed <= t.get("max_ping_ms", 500)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        result[name] = entry
    return result


# --- Батч-исполнение ---------------------------------------------------------
def _run_batch(items: list[tuple[str, int]], results: dict[str, dict],
               targets: list[dict], concurrency: int, depth: int = 0) -> None:
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

    ports = _reserve_ports(len(active))
    for e, p in zip(active, ports):
        e["port"] = p

    config = _build_batch_config(active, ROOT / COUNTRYTEST_TEMPLATE)
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
            _run_batch(items[:mid], results, targets, concurrency, depth + 1)
            _run_batch(items[mid:], results, targets, concurrency, depth + 1)
        else:
            for line, _ in items:
                if line not in results:
                    results[line] = {"error": "sing-box не смог обработать этот конфиг", "targets": {}}
        return

    try:
        port_by_index = {e["index"]: e["port"] for e in active}
        pairs = [(line, port_by_index[idx]) for line, idx in items if idx in port_by_index]

        workers = max(1, min(concurrency, len(pairs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_probe_port, p, targets): line for line, p in pairs}
            for fut in as_completed(futures):
                line = futures[fut]
                targets_res = fut.result()
                results[line] = {"error": None, "targets": targets_res}
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
                             target_names: list[str] | None = None) -> dict[str, dict]:
    """Проверяет доступность до целевых сайтов для списка прокси.

    targets — список целей; если None (или пусто), берётся список из
    config/reachability_targets.json. target_names — фильтр по именам.

    Возвращает {proxy_line: {"error": str|None, "targets": {name: {ok,status,latency_ms,error}}}}.
    """
    from config.settings import REACHABILITY_CONCURRENCY

    if concurrency is None:
        concurrency = REACHABILITY_CONCURRENCY
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
        _run_batch(chunk, results, all_targets, concurrency)
        LOGGER.info(
            "Reach batch %d/%d завершён за %.2fs", bi + 1, total_batches,
            time.monotonic() - batch_started,
        )

    with _cache_lock:
        for line in pending:
            res = results.get(line)
            key = _normalize_key(line)
            if res and res.get("targets"):
                _profile_cache[key] = dict(res)

    return results


def check_reachability(proxy_line: str, *, targets: list[dict] | None = None,
                       target_names: list[str] | None = None) -> dict:
    """Совместимость: профиль одного прокси (обёртка над батчем из 1)."""
    res_map = batch_reachability_check(
        [proxy_line], targets=targets, target_names=target_names,
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
            reached = profile_to_tags(load_targets(), res["targets"])
            print("Тэги: " + ", ".join(f"[{t}]" for t in reached) or "(нет)")
            if profile_is_global(load_targets(), res["targets"]):
                print("=> [Global] (достиг всех целей)")