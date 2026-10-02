"""Чекер достижимости: до каких целевых сайтов дозванивается прокси.

Чекер-дополнение (``decides_availability=False``). Для каждого живого сервера
один процесс sing-box проверяет список целей из
``config/reachability_targets.json`` и профиль складывается в колонку
``capabilities`` базы. При экспорте whitelist профиль превращается в тэги
[name] / [Global].

Проверяются только живые серверы со stable не ниже REACHABILITY_MIN_STABLE.
"""

from __future__ import annotations

from config.env import (
    REACHABILITY_ENABLED,
    REACHABILITY_GLOBAL_TAG,
    REACHABILITY_MIN_STABLE,
)
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.reachability")


class ReachabilityChecker(Checker):
    """Профилирование прокси по списку целевых сайтов."""

    name = "reachability"
    description = "Профиль достижимости целевых сайтов -> тэги [name] / [Global]"
    decides_availability = False

    def __init__(self) -> None:
        self.enabled = bool(REACHABILITY_ENABLED)
        self.min_stable = int(REACHABILITY_MIN_STABLE)
        self.global_tag = REACHABILITY_GLOBAL_TAG
        self._targets: list[dict] = []
        self._alive: set[str] = set()

    async def setup(self, ctx) -> None:
        self.enabled = bool(ctx.settings.get("reachability_enabled", REACHABILITY_ENABLED))
        self.min_stable = int(
            ctx.settings.get("reachability_min_stable", REACHABILITY_MIN_STABLE)
        )
        self.global_tag = ctx.settings.get("global_tag", REACHABILITY_GLOBAL_TAG)
        if not self.enabled:
            return
        from script.reachability_check import load_targets

        try:
            self._targets = await ctx.run_sync(load_targets)
        except Exception as exc:  # noqa: BLE001 — нет файла целей = фича выключена
            LOGGER.warning("Список целей не загружен, reachability выключен: %s", exc)
            self._targets = []
        if self._targets:
            LOGGER.info(
                "Reachability: %d целей, профиль для stable >= %d",
                len(self._targets), self.min_stable,
            )

    async def check(self, ctx: CheckContext) -> CheckResult:
        if not self.enabled or not self._targets:
            return CheckResult()

        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="нет живых серверов для профиля")

        from script.reachability_check import (
            batch_reachability_check,
            profile_is_global,
            profile_to_tags,
        )

        LOGGER.info("Reachability: профилирую %d живых серверов", len(lines))
        # Под замком запуска sing-box: reachability меняет cwd процесса и
        # пишет общий временный конфиг — параллельный запуск небезопасен.
        results = await run_exclusive(
            batch_reachability_check, sorted(lines), targets=self._targets,
        )

        outcomes: dict[str, CheckOutcome] = {}
        tagged = 0
        for line in lines:
            result = results.get(line) or {}
            targets_res = result.get("targets") or {}
            capabilities = ""
            if targets_res:
                if profile_is_global(self._targets, targets_res):
                    capabilities = self.global_tag
                else:
                    capabilities = ",".join(profile_to_tags(self._targets, targets_res))
                if capabilities:
                    tagged += 1
                    LOGGER.info("Reachability [%s]: %s", capabilities, line[:70])
            outcomes[line] = CheckOutcome(
                ok=None,
                capabilities=capabilities,
                detail=result.get("error"),
            )
        return CheckResult(outcomes=outcomes, summary=f"{tagged}/{len(lines)} с тэгами")
