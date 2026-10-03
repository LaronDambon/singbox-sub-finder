"""Общий транспорт для проверок через sing-box.

Один процесс sing-box, на каждый прокси — свой локальный mixed-инбаунд,
и дальше с этим портом работает любая проверяющая функция. Так и
достижимость целевых сайтов, и замер скорости идут через один и тот же
транспорт, а не через две копии одной и той же логики.

Проверяющая функция передаётся снаружи:

    def probe(port: int, line: str) -> dict:
        ...  # вернуть {"ok": True, "latency_ms": 120}

Всё внутри синхронное и блокирующее (subprocess, сокеты, requests) —
вызывающий код запускает это в потоке через ctx.run_sync.
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
from typing import Callable, Iterable, Sequence

from config.settings import get_settings
from script.logger_utils import get_project_logger
from script.core import reserve_ports
from script.batch_singbox import (
    build_batch_config, parse_outbound, ports_ready, start_singbox,
)

LOGGER = get_project_logger("proxy_probe")

# Ожидание готовности inbound-портов при старте sing-box, сек.
STARTUP_WAIT_SECONDS = 10.0
# Максимум inbound-ов (и прокси) в одном процессе sing-box.
MAX_BATCH_INBOUNDS = 100

ProbeFn = Callable[[int, str], dict]


# --- Порты -------------------------------------------------------------------


# --- Конфиг ------------------------------------------------------------------
# --- Запуск ------------------------------------------------------------------
def stop_singbox(proc) -> None:
    """Останавливает процесс sing-box вместе со всеми его детьми.

    terminate() на Windows бьёт только сам процесс: дерево остаётся жить.
    Из-за этого накапливались осиротевшие sing-box.exe — найдено 10 штук,
    старейший работал 3.5 суток, суммарно ~625 МБ RAM, и каждый держал
    свой listen-порт, хотя конфиг с него давно удалён.

    На Windows дерево снимается taskkill /F /T, на остальных системах
    достаточно kill(). Как уже сделано в pipeline/singbox.py.
    """
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
            return
        except Exception:  # noqa: BLE001
            pass
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        else:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Не удалось остановить sing-box %s: %s", getattr(proc, "pid", "?"), exc)


# --- Разбор прокси-строки ----------------------------------------------------
# --- Батч --------------------------------------------------------------------
def run_probe_batch(
    items: list[tuple[str, int]],
    results: dict[str, dict],
    *,
    probe: ProbeFn,
    concurrency: int,
    template_path: str | Path,
    config_dir: Path,
    tag_prefix: str,
    depth: int = 0,
    max_depth: int = 8,
) -> None:
    """Один батч: один sing-box, свой порт на прокси, проба в потоках.

    Если процесс не поднялся, батч делится пополам — так находится одна
    кривая строка, не убивая весь батч.
    """
    if not items:
        return

    used_tags: set[str] = set()
    entries: list[dict] = []
    for line, idx in items:
        outbound, err = parse_outbound(line, idx, used_tags)
        if err:
            results[line] = {"error": err}
            continue
        entries.append({"index": idx, "port": 0, "outbound": outbound})

    if not entries:
        return

    for e, p in zip(entries, reserve_ports(len(entries))):
        e["port"] = p

    config = build_batch_config(entries, template_path)
    config_path = Path(config_dir) / f"{tag_prefix}_{items[0][1]}_{len(items)}.json"
    proc = start_singbox(config, config_path)

    started_ok = proc is not None and proc.poll() is None
    if started_ok:
        started_ok = ports_ready([e["port"] for e in entries], STARTUP_WAIT_SECONDS)

    if not started_ok:
        stop_singbox(proc)
        Path(config_path).unlink(missing_ok=True)
        if len(items) > 1 and depth < max_depth:
            mid = len(items) // 2
            LOGGER.warning(
                "Батч из %d прокси не стартовал, делю пополам (%d + %d)",
                len(items), mid, len(items) - mid,
            )
            half = dict(
                results=results, probe=probe, concurrency=concurrency,
                template_path=template_path, config_dir=config_dir,
                tag_prefix=tag_prefix, depth=depth + 1, max_depth=max_depth,
            )
            run_probe_batch(items[:mid], results, **half)
            run_probe_batch(items[mid:], results, **half)
        else:
            for line, _ in items:
                results.setdefault(line, {"error": "sing-box не смог обработать этот конфиг"})
        return

    try:
        port_by_index = {e["index"]: e["port"] for e in entries}
        pairs = [(line, port_by_index[idx]) for line, idx in items if idx in port_by_index]
        for line, _ in pairs:
            results.setdefault(line, {"error": None})

        workers = max(1, min(concurrency, len(pairs)))
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(probe, port, line): line
                for line, port in pairs
            }
            for fut in as_completed(futures):
                line = futures[fut]
                try:
                    payload = fut.result()
                except Exception as exc:  # noqa: BLE001
                    payload = {"error": f"{type(exc).__name__}: {exc}"}
                with lock:
                    results[line].update(payload)
    finally:
        stop_singbox(proc)
        Path(config_path).unlink(missing_ok=True)


def probe_lines(
    proxy_lines: Iterable[str],
    *,
    probe: ProbeFn,
    concurrency: int = 8,
    batch_size: int = MAX_BATCH_INBOUNDS,
    template_path: str | Path,
    config_dir: Path,
    tag_prefix: str = "batch",
    max_depth: int = 8,
) -> dict[str, dict]:
    """Прогоняет список прокси через транспорт и возвращает {line: результат}."""
    results: dict[str, dict] = {}
    items: list[tuple[str, int]] = []
    seen: set[str] = set()
    for i, raw in enumerate(proxy_lines):
        line = str(raw).strip()
        if line and line not in seen:
            seen.add(line)
            items.append((line, i))
    if not items:
        return results
    step = max(1, batch_size)
    for start in range(0, len(items), step):
        run_probe_batch(
            items[start:start + step],
            results,
            probe=probe,
            concurrency=concurrency,
            template_path=template_path,
            config_dir=config_dir,
            tag_prefix=tag_prefix,
            max_depth=max_depth,
        )
    return results
