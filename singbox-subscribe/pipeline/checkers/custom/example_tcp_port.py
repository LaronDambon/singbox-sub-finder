"""ПРИМЕР своего чекера — его можно скопировать как заготовку.

Проверяет сервер обычным HTTP-запросом напрямую (без sing-box): если TCP
соединение с хостом сервера устанавливается за отведённое время, сервер считается
живым. Это НЕ проверка проксирования, только живость хоста — зато работает
без бинарника sing-box и показывает, как писать свои алгоритмы.

Включение: PIPELINE_CHECKERS=tcp_port,url_probe
( tcp_port будет первым решающим, url_probe даст задержку).

Запуск: python -m pipeline checkers
"""

from __future__ import annotations

import asyncio
import socket
import time

from pipeline.checkers import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.tcp_port")

CONNECT_TIMEOUT = 3.0


def _host_port(line: str) -> tuple[str, int] | None:
    """Достаёт хост и порт из строки прокси (после @ у ss/vmess и т.п.)."""
    from utils import tool

    host = tool.extract_proxy_host(line)
    if not host:
        return None
    port = 443
    tail = line.split("@", 1)[-1].split("/")[0].split("?")[0].split("#")[0]
    if ":" in tail:
        candidate = tail.rsplit(":", 1)[-1]
        if candidate.isdigit():
            port = int(candidate)
    return host, port


def probe_tcp(line: str, timeout: float = CONNECT_TIMEOUT) -> tuple[bool, int | None]:
    """Одна синхронная проба. Возвращает (жив, задержка_мс)."""
    target = _host_port(line)
    if not target:
        return False, None
    host, port = target
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, round((time.monotonic() - started) * 1000)
    except OSError:
        return False, None


class TcpPortChecker(Checker):
    """Живость сервера по TCP-соединению до его хоста."""

    name = "tcp_port"
    description = "Пример своего чекера: TCP-доступность хоста сервера"
    decides_availability = True

    async def check(self, ctx: CheckContext) -> CheckResult:
        lines = self.filter_lines(ctx)
        outcomes: dict[str, CheckOutcome] = {}

        # Проверки идут параллельно: ctx.run_sync уводит блокирующий сокет
        # в поток и не тормозит остальные батчи.
        async def one(line: str) -> CheckOutcome:
            ok, ping = await ctx.run_sync(probe_tcp, line)
            return CheckOutcome(ok=ok, ping_ms=ping)

        results = await asyncio.gather(*(one(line) for line in lines))
        outcomes.update(dict(zip(lines, results)))
        return CheckResult(
            outcomes=outcomes,
            summary=f"{sum(1 for o in outcomes.values() if o.ok)}/{len(outcomes)} доступны",
        )
