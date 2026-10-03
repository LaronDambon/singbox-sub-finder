"""Запуск батча sing-box: сборка конфига, порты, старт, ожидание.

Раньше эти пять функций лежали копией в script/reachability_check.py
и script/country_check.py — тела совпадали, различались только докстринги
и местный алиас импорта time. Копии разъезжались: правка в одной не
доезжала до другой. Здесь одна реализация, оба чекера её импортируют.

Совпадение тел проверено сравнением деревьев AST без докстрингов —
глазами такое расхождение не видно.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

from config.settings import get_settings
from script.core import reserve_ports
from script.logger_utils import get_project_logger

LOGGER = get_project_logger("batch_singbox")


def normalize_key(raw_line: str) -> str:
    """Стабильный ключ прокси без учёта имени/тэга (для кэша)."""
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(raw_line)


def parse_outbound(proxy_line: str, index: int, used_tags: set[str]):
    """Парсит строку прокси в outbound-dict sing-box (через парсеры core).

    Возвращает (outbound|None, error|None). WireGuard выносится отдельно:
    новые версии sing-box требуют его в "endpoints".
    """
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


def build_batch_config(entries: list[dict], template_path: Path) -> dict:
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


def start_singbox(config: dict, config_path: Path):
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
        return subprocess.Popen([str(get_settings().paths.sing_box_path), "run", "-c", str(config_path)], **kwargs)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Не удалось запустить sing-box: %s", exc)
        return None


def ports_ready(ports: list[int], deadline_seconds: float) -> bool:
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
