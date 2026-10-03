"""Чекер страны: определяет страну сервера (эмодзи-флаг).

Чекер-дополнение: он НЕ решает, жив ли сервер (``decides_availability=False``),
а только обогащает результат полем ``country``, которое уходит в базу.

Страна берётся дешевле всего: сначала из имени сервера (эмодзи уже есть) или из
базы (колонка country). Дорогую сетевую проверку через sing-box запускаем только
для серверов, у которых страна ещё неизвестна.

Реализация сетевой части — ``script/country_check.py`` (один процесс sing-box
на батч, порт на прокси, цепочка geo-эндпоинтов).
"""

from __future__ import annotations

from config.env import COUNTRY_CHECK_CONCURRENCY, COUNTRY_CHECK_ENABLED
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.country")


class CountryChecker(Checker):
    """Определяет страну сервера и кладёт её в базу."""

    name = "country"
    description = "Определение страны (эмодзи) для серверов без известной страны"
    decides_availability = False

    def __init__(self) -> None:
        self.enabled = bool(COUNTRY_CHECK_ENABLED)
        self.concurrency = int(COUNTRY_CHECK_CONCURRENCY)
        self._known: dict[str, str] = {}

    async def setup(self, ctx) -> None:
        self.enabled = bool(ctx.value("COUNTRY_CHECK_ENABLED"))
        self.concurrency = max(1, int(
            ctx.value("COUNTRY_CHECK_CONCURRENCY")
        ))
        # Карта «ключ -> страна» из базы: позволяет не проверять заново то,
        # что уже определено в прошлых циклах.
        self._known = await ctx.db.load_country_map() if ctx.db is not None else {}

    def wants(self, ctx: CheckContext, line: str) -> bool:
        if not self.enabled:
            return False
        # Страна уже видна прямо в имени сервера — сеть не нужна.
        from utils import tool

        if tool.get_country_from_name(line):
            return False
        return True

    async def check(self, ctx: CheckContext) -> CheckResult:
        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="все страны уже известны")

        from script.country_check import batch_country_check, country_to_emoji

        LOGGER.info(
            "Страна: проверяю %d серверов (concurrency=%d)", len(lines), self.concurrency,
        )
        # Под замком запуска sing-box: country_check меняет cwd процесса и
        # пишет общий временный конфиг — параллельный запуск небезопасен.
        results = await run_exclusive(
            batch_country_check, sorted(lines), concurrency=self.concurrency,
        )

        outcomes: dict[str, CheckOutcome] = {}
        found = 0
        for line in lines:
            result = results.get(line) or {}
            emoji = result.get("emoji") or country_to_emoji(result.get("country"))
            if emoji:
                found += 1
                LOGGER.info(
                    "Страна %s (%sms): %s",
                    result.get("country"), result.get("latency_ms"), line[:80],
                )
            outcomes[line] = CheckOutcome(
                ok=None,
                country=emoji or "",
                detail=result.get("error"),
                data={"country_code": result.get("country")} if result else {},
            )
        return CheckResult(outcomes=outcomes, summary=f"{found}/{len(lines)} определили")
