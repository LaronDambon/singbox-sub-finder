"""Определение страны сервера через локальный sing-box (по мотивам Throne).

Исследование Throne (throneproj/Throne, core/server/test_utils/{common,speedtest_utils}.go):
  * страна для списка конфигов определяется методом countryTest: это ТОЛЬКО
    getSpeedtestServer — получить список серверов speedtest через прокси и
    выбрать ближайший по HTTP-латентности; download/upload не выполняются;
  * ключ к скорости: используется ОДИН уже запущенный sing-box со всеми
    outbound'ами, и HTTP-client каждого теста дозванивается напрямую через
    объект outbound (DialContext), без процесса на конфиг и без локального
    HTTP-proxy хопа;
  * параллелизм — горутины с семафором (countryConcurrency, по умолчанию 5).

Почему прежняя версия здесь была медленной и давала ~60% успеха:
  * отдельный процесс sing-box на КАЖДЫЙ прокси (старт до 8с + teardown);
  * traffic шёл через mixed-inbound как через HTTP-прокси (лишний слой);
  * список серверов тянулся с www.speedtest.net, который агрессивно
    блокирует датацентровые IP (403) — источник большинства отказов.

Реализация здесь повторяет семантику Throne средствами Python:
  * ОДИН процесс sing-box на батч: N mixed-inbound'ов (127.0.0.1:порт_i),
    N outbound'ов и правило маршрутизации inbound_i -> outbound_i,
    т.е. каждый локальный порт жёстко привязан к своему прокси;
  * потоки Python одновременно стучатся в свои порты — эквивалент
    параллельных DialContext из Throne;
  * вместо speedtest.net страна определяется одним лёгким запросом через
    цепочку geo-эндпоинтов: Cloudflare cdn-cgi/trace -> api.ip.sb/geoip ->
    ipinfo.io/json. Это крошечные ответы на anycast-инфраструктуре,
    почти не дающие отказов; ISO-код сразу превращается в эмодзи флага.

CLI:
  python -m script.country_check '<proxy_line>'
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]

from config.settings import (
    SING_BOX_PATH,
    COUNTRYTEST_TEMPLATE,
    COUNTRY_CHECK_TIMEOUT,
)

from script.logger_utils import get_project_logger

LOGGER = get_project_logger("country_check")

# --- Параметры probing -------------------------------------------------------
# Таймауты одного geo-запроса: (connect, read), сек.
PROBE_CONNECT_TIMEOUT = 4.0
PROBE_READ_TIMEOUT = float(COUNTRY_CHECK_TIMEOUT) if COUNTRY_CHECK_TIMEOUT else 6.0
# Ожидание готовности inbound-портов при старте sing-box, сек.
STARTUP_WAIT_SECONDS = 10.0
# Максимум inbound'ов (и прокси) в одном процессе sing-box.
MAX_BATCH_INBOUNDS = 100

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# --- Geo-эндпоинты -----------------------------------------------------------
# Каждый возвращает ISO-код страны (2 буквы) или None. Пробуются по порядку.
def _parse_cloudflare_trace(resp: requests.Response) -> str | None:
    # Текст вида "fl=...\nloc=DE\n..."
    m = re.search(r"^loc=([A-Za-z]{2})\s*$", resp.text, re.MULTILINE)
    return m.group(1).upper() if m else None


def _parse_ip_sb(resp: requests.Response) -> str | None:
    data = resp.json()
    code = data.get("country_code") if isinstance(data, dict) else None
    return str(code).upper() if isinstance(code, str) and len(code) == 2 else None


def _parse_ipinfo(resp: requests.Response) -> str | None:
    data = resp.json()
    code = data.get("country") if isinstance(data, dict) else None
    return str(code).upper() if isinstance(code, str) and len(code) == 2 else None


GEO_PROBES: list[tuple[str, str, object]] = [
    ("cloudflare-trace", "https://www.cloudflare.com/cdn-cgi/trace", _parse_cloudflare_trace),
    ("ip.sb", "https://api.ip.sb/geoip", _parse_ip_sb),
    ("ipinfo.io", "https://ipinfo.io/json", _parse_ipinfo),
]

_ISO_RE = re.compile(r"^[A-Za-z]{2}$")


def iso_to_emoji(code: str | None) -> str | None:
    """ISO-3166 alpha-2 -> эмодзи флага (regional indicator symbols)."""
    if not code or not _ISO_RE.match(code):
        return None
    base = ord("A")
    return "".join(chr(0x1F1E6 + ord(ch) - base) for ch in code.upper())


def country_to_emoji(country_name: str | None) -> str | None:
    """Эмодзи флага по ISO-коду ИЛИ текстовому названию страны.

    Совместимость со старым API: новые geo-пробы возвращают ISO-код
    (срабатывает быстрый путь), а для текстовых названий, как и раньше,
    ищем совпадение в regex_patterns из utils.tool.
    """
    if not country_name:
        return None
    stripped = country_name.strip()
    if _ISO_RE.match(stripped):
        return iso_to_emoji(stripped)
    try:
        from utils import tool

        for country_code, pattern in tool.regex_patterns.items():
            if pattern.search(country_name):
                return country_code
    except Exception:  # noqa: BLE001
        pass
    return None


# --- Кэш в рамках одного запуска --------------------------------------------
_country_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


def _normalize_key(raw_line: str) -> str:
    """Стабильный ключ прокси без учёта имени/тэга (для кэша)."""
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(raw_line)


# --- Разбор прокси и сборка батч-конфига -------------------------------------
def _parse_outbound(proxy_line: str, index: int, used_tags: set[str]):
    """Парсит строку прокси в outbound-dict sing-box (через парсеры core).

    Возвращает (outbound|None, error|None). WireGuard выносится отдельно:
    новые версии sing-box требуют его в "endpoints".
    """
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
    """Собирает конфиг: N inbound->N outbound c правилами маршрутизации.

    entries: [{index, port, outbound}]
    """
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
    """Подбирает count свободных TCP-портов на 127.0.0.1."""
    ports: list[int] = []
    # Берём базовый свободный порт и проверяем соседей связыванием.
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
    """Запускает sing-box с батч-конфигом. Возвращает процесс или None."""
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
    import time as _time

    deadline = _time.monotonic() + deadline_seconds
    remaining = set(ports)
    while _time.monotonic() < deadline and remaining:
        for p in list(remaining):
            try:
                with socket.create_connection(("127.0.0.1", p), timeout=0.35):
                    remaining.discard(p)
            except OSError:
                pass
        if remaining:
            time.sleep(0.15)
    return not remaining


# --- Geo-probe через конкретный локальный порт -------------------------------
def _probe_port(port: int) -> dict:
    """Определяет страну выхода через локальный порт-прокси одного outbound'а."""
    result = {"country": None, "country_code": None, "emoji": None,
              "server_name": None, "latency_ms": None, "error": None}
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    session = requests.Session()
    session.trust_env = False  # игнорировать системные HTTP(S)_PROXY
    last_error = "неизвестная ошибка"
    for name, url, parser in GEO_PROBES:
        start = time.monotonic()
        try:
            resp = session.get(
                url,
                proxies=proxies,
                timeout=(PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT),
                headers={"User-Agent": BROWSER_UA},
            )
            if resp.status_code != 200:
                last_error = f"{name}: HTTP {resp.status_code}"
                continue
            code = parser(resp)
            if not code:
                last_error = f"{name}: код страны не найден в ответе"
                continue
            elapsed = round((time.monotonic() - start) * 1000.0, 1)
            result.update({
                "country": code,
                "country_code": code,
                "emoji": iso_to_emoji(code),
                "server_name": name,
                "latency_ms": elapsed,
            })
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = f"{name}: {exc}"
    result["error"] = last_error
    return result


# --- Батч-исполнение ----------------------------------------------------------
def _run_batch(items: list[tuple[str, int]], results: dict[str, dict],
               concurrency: int, depth: int = 0) -> None:
    """items: [(line, index)] — проверяет батч одним процессом sing-box.

    При инфраструктурном сбое старта делит батч пополам (рекурсия), чтобы
    один «ядовитый» конфиг не валил остальные.
    """
    if not items:
        return

    used_tags: set[str] = set()
    entries: list[dict] = []
    for line, idx in items:
        outbound, err = _parse_outbound(line, idx, used_tags)
        if err:
            results[line] = {"country": None, "country_code": None, "emoji": None,
                             "server_name": None, "latency_ms": None, "error": err}
            continue
        entries.append({"index": idx, "port": 0, "outbound": outbound})

    active = [e for e in entries]
    if not active:
        return

    ports = _reserve_ports(len(active))
    for e, p in zip(active, ports):
        e["port"] = p

    config = _build_batch_config(active, ROOT / COUNTRYTEST_TEMPLATE)
    config_path = ROOT / "source" / "tests" / f"country_batch_{items[0][1]}_{len(items)}.json"
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
                "Батч из %d прокси не стартовал, делю пополам (%d + %d)",
                len(items), mid, len(items) - mid,
            )
            _run_batch(items[:mid], results, concurrency, depth + 1)
            _run_batch(items[mid:], results, concurrency, depth + 1)
        else:
            for line, _ in items:
                if line not in results:
                    results[line] = {"country": None, "country_code": None, "emoji": None,
                                     "server_name": None, "latency_ms": None,
                                     "error": "sing-box не смог обработать этот конфиг"}
        return

    try:
        # Соответствие line<->port по индексу из items.
        port_by_index = {e["index"]: e["port"] for e in active}
        pairs = [(line, port_by_index[idx]) for line, idx in items if idx in port_by_index]

        workers = max(1, min(concurrency, len(pairs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_probe_port, p): line for line, p in pairs}
            for fut in as_completed(futures):
                line = futures[fut]
                results[line] = fut.result()
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


def batch_country_check(proxy_lines: list[str], *, concurrency: int | None = None,
                        batch_size: int = MAX_BATCH_INBOUNDS) -> dict[str, dict]:
    """Определяет страну для списка прокси.

    Возвращает dict {proxy_line: результат-словарь} с ключами:
    country, country_code, emoji, server_name, latency_ms, error.
    """
    from config.settings import COUNTRY_CHECK_CONCURRENCY

    if concurrency is None:
        concurrency = COUNTRY_CHECK_CONCURRENCY
    concurrency = max(1, int(concurrency))

    results: dict[str, dict] = {}
    pending: list[str] = []
    seen_keys: set[str] = set()

    with _cache_lock:
        for raw in proxy_lines:
            line = str(raw).strip()
            if not line:
                continue
            key = _normalize_key(line)
            if key in _country_cache:
                cached = _country_cache[key]
                if cached is not None:
                    results[line] = dict(cached)
                else:
                    results[line] = {"country": None, "country_code": None, "emoji": None,
                                     "server_name": None, "latency_ms": None,
                                     "error": "нет результата (кэш запуска)"}
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
            "Country batch %d/%d: %d прокси в одном процессе sing-box (concurrency=%d)",
            bi + 1, total_batches, len(chunk), concurrency,
        )
        batch_started = time.monotonic()
        _run_batch(chunk, results, concurrency)
        LOGGER.info("Country batch %d/%d завершён за %.2fs", bi + 1, total_batches, time.monotonic() - batch_started)

    # Наполняем кэш успешными результатами.
    with _cache_lock:
        for line in pending:
            res = results.get(line)
            key = _normalize_key(line)
            if res and res.get("country"):
                _country_cache[key] = dict(res)

    return results


def check_country(proxy_line: str) -> dict:
    """Совместимость: страна одного прокси (обёртка над батчем из 1)."""
    res_map = batch_country_check([proxy_line])
    return res_map.get(proxy_line.strip()) or {
        "country": None, "country_code": None, "emoji": None,
        "server_name": None, "latency_ms": None, "error": "нет результата",
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Использование: python -m script.country_check '<proxy_line>'")
        sys.exit(1)
    line = sys.argv[1]
    res = check_country(line)
    print(json.dumps(res, ensure_ascii=False, indent=2))