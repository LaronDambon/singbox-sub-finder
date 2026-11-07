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
from concurrent.futures import ThreadPoolExecutor
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


#: Ловим ли вывод sing-box в файл. Включается переменной окружения
#: SINGBOX_CAPTURE_LOG: без неё поведение прежнее (DEVNULL).
#: Зачем: раньше причина «батч не стартовал» была НЕВИДИМА — stderr уходил в
#: пустоту, и оставалось только гадать. С включённым логом видно настоящую
#: ошибку (занятый порт, битая строка, отказ парсера конфига).
def _capture_enabled() -> bool:
    return bool((os.environ.get("SINGBOX_CAPTURE_LOG") or "").strip())


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
    if _capture_enabled():
        # Держим вывод рядом с конфигом: он удаляется вместе с ним.
        log_path = config_path.with_suffix(".out.log")
        try:
            kwargs["stdout"] = open(log_path, "w", encoding="utf-8", errors="replace")
            kwargs["stderr"] = subprocess.STDOUT
        except OSError:
            pass
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen(
            [str(get_settings().paths.sing_box_path), "run", "-c", str(config_path)],
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Не удалось запустить sing-box: %s", exc)
        return None


def last_output(config_path: Path, *, max_chars: int = 600) -> str:
    """Последние строки вывода sing-box для этого конфига (если ловим)."""
    log_path = Path(config_path).with_suffix(".out.log")
    if not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [l for l in text.splitlines() if l.strip()]
    return " | ".join(lines[-6:])[:max_chars]


def stop_singbox(proc) -> None:
    """Останавливает sing-box ВМЕСТЕ С ДЕТЬМИ.

    На Windows terminate() убивает только сам процесс: дочерние остаются
    жить и держат listen-порты. Накопление таких сирот — прямая причина
    «батч не стартовал»: следующий запуск не может забиндить занятый порт.
    Замером найдено 10 осиротевших sing-box.exe, старейший работал 3.5 суток.

    Поэтому на Windows дерево снимается taskkill /F /T, и делается это
    ПЕРВЫМ, а не как запасной вариант после terminate(): успешный terminate()
    раньше приводил к раннему return, и до taskkill дело не доходило.
    """
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
            return
        except Exception:  # noqa: BLE001 — не умер, добиваем
            pass
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Не удалось остановить sing-box %s: %s",
                       getattr(proc, "pid", "?"), exc)


def _port_open(port: int, timeout: float = 0.35) -> bool:
    """True, если на 127.0.0.1:port кто-то принимает соединение."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ports_ready(ports: list[int], deadline_seconds: float,
                *, poll_interval: float = 0.15) -> bool:
    """Ждёт, пока ВСЕ порты начнут принимать соединения, в пределах дедлайна.

    Проверка идёт ПАРАЛЛЕЛЬНО. Раньше порты обходились по очереди, по 0.35 с
    таймаута на каждый, и на каждой итерации цикла список проверялся заново
    целиком. При 100 inbound'ах один проход занимал до 35 с — то есть дедлайн
    (10 с) истекал, едва начавшись, и батч объявлялся «не стартовавшим», хотя
    sing-box в этот момент спокойно поднимался. Именно поэтому делились даже
    батчи из 6, 3 и 2 прокси: размер тут ни при чём.

    Важно: на Windows connect к ЗАКРЫТОМУ порту на localhost не отдаёт RST
    мгновенно, а висит весь свой таймаут (замерено: ровно 0.35 с на порт).
    Поэтому обход идёт параллельно — иначе стоимость прохода равна
    «число портов x таймаут», и на 100 портах это 35 с.
    """
    remaining = set(ports)
    if not remaining:
        return True
    deadline = time.monotonic() + deadline_seconds
    while remaining:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        # Опрос ПАРАЛЛЕЛЬНЫЙ: на Windows connect к закрытому порту на
        # localhost не отдаёт RST мгновенно, а висит весь свой таймаут
        # (замерено: 0.35 с ровно). Последовательный обход 100 портов стоил
        # 35 с на один проход — дедлайн истекал, не начавшись.
        # Таймаут одного connect не длиннее остатка дедлайна, иначе проход
        # переживёт дедлайн и вернёт «не готово» уже после его истечения.
        pending = list(remaining)
        probe_timeout = max(0.01, min(0.35, left))
        with ThreadPoolExecutor(max_workers=min(32, len(pending))) as pool:
            for p, open_ in zip(
                pending, pool.map(lambda q: _port_open(q, probe_timeout), pending)
            ):
                if open_:
                    remaining.discard(p)
        if not remaining:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    return True