# -*- coding: utf-8 -*-
"""Чекер глубины IP: ISP, город, регион и признак датацентра.

Зачем: Google и Claude не любят подключения с серверных хостингов. Одной
страны для отбора мало — нужен провайдер и понимание, домашний это IP или
датацентр.

Чекер-дополнение (decides_availability = False): он не решает, жив ли
сервер, а вешает на него ТЕГИ, которые потом работают как фильтр в SQL:

    isp-megafon        провайдер (слаг от ISP/org)
    city-novosibirsk   город
    region-...         регион/область (если отличается от города)
    as-56630           номер автономной системы (провайдер целиком)
    dc                 датацентр — плохо для Google/Claude
    res                не датацентр; ставится ТОЛЬКО если источник знает
                       про hosting и сказал "нет" (ipinfo/ip.sb не знают)
    mobile             мобильный оператор

Теги пишутся в server_tags с kind="geo": они не участвуют в отборе
сервисов (там kind="service"), но по ним можно фильтровать запросом.

Реализация сетевой части — script/ip_info_check.py (один процесс
sing-box на батч, порт на прокси, цепочка ip-api.com -> ipinfo.io ->
api.ip.sb).
"""

from __future__ import annotations

from config.settings import get_settings
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.ip_info")


class IpInfoChecker(Checker):
    """Определяет провайдера, город и признак датацентра для сервера."""

    name = "ip_info"
    description = "ISP, город и признак датацентра (dc/res) для отбора чистых IP"
    decides_availability = False

    def __init__(self) -> None:
        self.enabled = bool(get_settings().ip_info.enabled)
        self.concurrency = int(get_settings().ip_info.concurrency)
        #: Ключи серверов, у которых теги ISP уже есть: повторно не ходим.
        self._known: set[str] = set()

    async def setup(self, ctx) -> None:
        self.enabled = bool(get_settings().ip_info.enabled)
        self.concurrency = max(1, int(get_settings().ip_info.concurrency))
        self._known = self._load_known()

    @staticmethod
    def _load_known() -> set[str]:
        """Ключи серверов, у которых уже проставлены geo-теги."""
        try:
            from script.server_store import ServerStore

            store = ServerStore(get_settings().paths.servers_db_file)
            with store._connect() as conn:  # noqa: SLF001 — свой же класс хранилища
                rows = conn.execute(
                    "SELECT DISTINCT key FROM server_tags WHERE kind = 'geo'"
                ).fetchall()
            return {str(r["key"]) for r in rows}
        except Exception:  # noqa: BLE001 — без карты просто проверим всех
            return set()

    def wants(self, ctx: CheckContext, line: str) -> bool:
        if not self.enabled:
            return False
        if not self._known:
            return True
        from script.downloader import normalize_proxy_key

        try:
            return normalize_proxy_key(line) not in self._known
        except Exception:  # noqa: BLE001
            return True

    async def check(self, ctx: CheckContext) -> CheckResult:
        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="ISP уже известен у всех")

        from script.downloader import normalize_proxy_key
        from script.ip_info_check import batch_ip_info

        LOGGER.info(
            "IP-info: проверяю %d серверов (concurrency=%d)", len(lines), self.concurrency,
        )
        # Под замком запуска sing-box: модуль пишет общий временный конфиг и
        # меняет cwd процесса — параллельный запуск небезопасен.
        results = await run_exclusive(
            batch_ip_info, sorted(lines), concurrency=self.concurrency,
        )

        outcomes: dict[str, CheckOutcome] = {}
        found = 0
        hosting = 0
        for line in lines:
            info = results.get(line) or {}
            tags = list(info.get("tags") or [])
            if tags:
                found += 1
                if "dc" in tags:
                    hosting += 1
                LOGGER.info(
                    "IP-info %s: %s / %s -> %s",
                    info.get("ip") or "?", info.get("isp") or "?",
                    info.get("city") or "?", ",".join(tags),
                )
                try:
                    self._known.add(normalize_proxy_key(line))
                except Exception:  # noqa: BLE001
                    pass
            outcomes[line] = CheckOutcome(
                ok=None,
                # Теги уходят в server_tags с kind="geo" — это НЕ профиль
                # сервисов, поэтому capabilities не трогаем.
                extra_tags=",".join(tags),
                detail=info.get("error"),
                data={
                    "ip": info.get("ip") or "",
                    "isp": info.get("isp") or "",
                    "asn": info.get("asn") or "",
                    "city": info.get("city") or "",
                    "region": info.get("region") or "",
                    "hosting": bool(info.get("hosting")),
                },
            )
        return CheckResult(
            outcomes=outcomes,
            summary=f"{found}/{len(lines)} определили, датацентров {hosting}",
        )
