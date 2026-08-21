"""Проверка страны через скоростной тест (аналог Throne).

Механизм (как в Throne/core/server/test_utils/speedtest_utils.go + common.go):
  1. Для каждого прокси строится sing-box конфиг с ОДНИМ outbound (сам прокси)
     и локальным mixed-inbound на свободном порту.
  2. sing-box запускается, и весь трафик через этот локальный inbound идёт
     через проверяемый прокси (аналог dialer'а в Go).
  3. Через прокси запрашивается список серверов speedtest.net
     (https://www.speedtest.net/api/js/servers) и каждый сервер "пингуется"
     HTTP-запросом к <url_dir>/latency.txt (как speedtest-go HTTPPing).
  4. Выбирается сервер с наименьшей latency — его страна считается страной
     исходящего соединения прокси (res.ServerCountry в Throne).

Это косвенный метод геолокации: страна определяется не по IP, а по тому,
к какому ближайшему/самому быстрому серверу speedtest можно достучаться
через данный прокси.
"""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]

from config.settings import SING_BOX_PATH, URLTEST_TEMPLATE
from script.logger_utils import get_project_logger

LOGGER = get_project_logger("country_check")

# Таймаут получения списка серверов (аналог FetchServersTimeout = 8s в Throne).
FETCH_SERVERS_TIMEOUT = 8.0
# Таймаут одного HTTP-пинга до сервера speedtest.
PING_TIMEOUT = 4.0
# Сколько серверов пинговать (берём первые N по расстоянию, как в speedtest-go).
MAX_PING_SERVERS = 20
# Таймаут работы sing-box для одного прокси.
SINGBOX_TIMEOUT = 25.0

# Кэш: страна по "нормализованному ключу" прокси, чтобы не перепроверять
# одинаковые серверы в рамках одного запуска.
_country_cache: dict[str, str | None] = {}


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _normalize_key(raw_line: str) -> str:
    """Стабильный ключ прокси без учёта имени/тэга (для кэша)."""
    line = raw_line.strip()
    if "#" in line:
        line = line.split("#", 1)[0].strip()
    return line.lower()


def _build_single_config(proxy_line: str, inbound_port: int) -> dict | None:
    """Строит sing-box конфиг с одним outbound (прокси) и mixed-inbound.

    Переиспользует парсеры и сборку конфига из script.core.
    Возвращает dict конфига или None, если прокси не удалось распарсить.
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
            return None
        node = nodes[0]
        if not isinstance(node, dict):
            return None

        template_path = ROOT / URLTEST_TEMPLATE
        template_data = core.load_template(str(template_path))
        config = core.build_singbox_config_from_nodes(template_data, [node])

        # Единый inbound на заданном порту.
        for inbound in config.get("inbounds", []):
            if inbound.get("type") == "mixed" and inbound.get("listen") == "127.0.0.1":
                inbound["listen_port"] = inbound_port
                break
        return config
    finally:
        os.chdir(old_cwd)
        core.providers = previous_providers


def _run_singbox(config: dict, inbound_port: int, timeout: float = SINGBOX_TIMEOUT) -> bool:
    """Запускает sing-box с данным конфигом и держит его, пока жив.

    Возвращает True, если процесс успешно стартовал (порт слушается).
    """
    executable = SING_BOX_PATH
    if not os.path.exists(executable):
        raise FileNotFoundError(f"Не найден sing-box по пути: {executable}")

    config_path = ROOT / "source" / "tests" / f"country_{inbound_port}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "text": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen([executable, "run", "-c", str(config_path)], **kwargs)

    # Ждём, пока inbound-порт начнёт слушаться.
    import socket
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", inbound_port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    return False


def _fetch_servers(proxy_url: str, timeout: float = FETCH_SERVERS_TIMEOUT) -> list[dict]:
    """Запрашивает список серверов speedtest.net через прокси.

    Аналог getSpeedtestServer -> FetchServerListContext в Throne.
    """
    proxies = {"http": proxy_url, "https": proxy_url}
    resp = requests.get(
        "https://www.speedtest.net/api/js/servers",
        proxies=proxies,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("url")]


def _ping_server(proxy_url: str, server: dict, timeout: float = PING_TIMEOUT) -> float | None:
    """HTTP-пинг сервера speedtest через прокси (аналог HTTPPing в speedtest-go).

    Пингуем <url_dir>/latency.txt и возвращаем latency в мс.
    """
    url = server.get("url", "")
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        ping_url = base + "/latency.txt"
    except Exception:
        return None

    proxies = {"http": proxy_url, "https": proxy_url}
    start = time.monotonic()
    try:
        resp = requests.get(
            ping_url,
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        return (time.monotonic() - start) * 1000.0
    except Exception:
        return None


def check_country(proxy_line: str, *, timeout: float = SINGBOX_TIMEOUT) -> dict:
    """Проверяет страну прокси через скоростной тест (аналог countryTest в Throne).

    Returns:
        dict с ключами: 'country' (название страны), 'country_code' (2-буквенный код),
        'server_name', 'latency_ms', 'error'.
    """
    result = {
        "country": None,
        "country_code": None,
        "server_name": None,
        "latency_ms": None,
        "error": None,
    }

    key = _normalize_key(proxy_line)
    if key in _country_cache:
        cached = _country_cache[key]
        if cached:
            result["country"] = cached
        return result

    inbound_port = _find_free_port()
    proxy_url = f"http://127.0.0.1:{inbound_port}"

    config = _build_single_config(proxy_line, inbound_port)
    if config is None:
        result["error"] = "не удалось распарсить прокси"
        _country_cache[key] = None
        return result

    proc = None
    try:
        executable = SING_BOX_PATH
        config_path = ROOT / "source" / "tests" / f"country_{inbound_port}.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)

        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen([executable, "run", "-c", str(config_path)], **kwargs)

        # Ждём готовности inbound-порта.
        import socket
        ready = False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                result["error"] = "sing-box завершился при старте"
                _country_cache[key] = None
                return result
            try:
                with socket.create_connection(("127.0.0.1", inbound_port), timeout=0.5):
                    ready = True
                    break
            except OSError:
                time.sleep(0.2)
        if not ready:
            result["error"] = "таймаут ожидания inbound-порта"
            _country_cache[key] = None
            return result

        # 1) Получаем список серверов через прокси.
        servers = _fetch_servers(proxy_url)
        if not servers:
            result["error"] = "не удалось получить список серверов speedtest"
            _country_cache[key] = None
            return result

        # 2) Пингуем серверы через прокси, выбираем лучший (мин. latency).
        best_server = None
        best_latency = None
        for server in servers[:MAX_PING_SERVERS]:
            latency = _ping_server(proxy_url, server)
            if latency is None:
                continue
            if best_latency is None or latency < best_latency:
                best_latency = latency
                best_server = server

        if best_server is None:
            result["error"] = "ни один сервер speedtest не ответил через прокси"
            _country_cache[key] = None
            return result

        result["country"] = best_server.get("country")
        result["server_name"] = best_server.get("name")
        result["latency_ms"] = round(best_latency, 1) if best_latency is not None else None
        _country_cache[key] = result["country"]
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        _country_cache[key] = None
        return result
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def country_to_emoji(country_name: str | None) -> str | None:
    """Преобразует название страны в эмодзи-флаг через regex_patterns из tool.

    Возвращает эмодзи страны или None, если не удалось сопоставить.
    """
    if not country_name:
        return None
    from utils import tool
    for country_code, pattern in tool.regex_patterns.items():
        if pattern.search(country_name):
            return country_code
    return None


def batch_country_check(proxy_lines: list[str], *, concurrency: int = 4) -> dict[str, dict]:
    """Проверяет страну для списка прокси параллельно.

    Returns:
        dict {proxy_line: result_dict}
    """
    results: dict[str, dict] = {}
    lock = threading.Lock()
    index = 0
    index_lock = threading.Lock()

    def worker():
        nonlocal index
        while True:
            with index_lock:
                if index >= len(proxy_lines):
                    return
                i = index
                index += 1
            line = proxy_lines[i]
            res = check_country(line)
            with lock:
                results[line] = res

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Использование: python -m script.country_check '<proxy_line>'")
        sys.exit(1)
    line = sys.argv[1]
    res = check_country(line)
    print(json.dumps(res, ensure_ascii=False, indent=2))
