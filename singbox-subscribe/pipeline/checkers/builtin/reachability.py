"""Чекер достижимости: до каких целевых сайтов дозванивается прокси.

Чекер-дополнение (``decides_availability=False``). Для каждого живого сервера
один процесс sing-box проверяет список целей из
``config/reachability_targets.json`` и профиль складывается в колонку
``capabilities`` базы. При экспорте whitelist профиль превращается в тэги
[name] / [Global].

Проверяются только живые серверы со stable не ниже REACHABILITY_MIN_STABLE.
"""

from __future__ import annotations

from config.settings import get_settings
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
        self.enabled = bool(get_settings().reach.enabled)
        self.min_stable = int(get_settings().reach.min_stable)
        self.global_tag = get_settings().reach.global_tag
        # Порог задержки до цели (TTFB, мс). None = у каждой цели свой
        # max_ping_ms, а где его нет — значение из настроек.
        self.max_ping_ms: int | None = None
        self._targets: list[dict] = []
        self._alive: set[str] = set()

    async def setup(self, ctx) -> None:
        self.enabled = bool(get_settings().reach.enabled)
        self.min_stable = int(
            get_settings().reach.min_stable
        )
        self.global_tag = get_settings().reach.global_tag
        raw_ping = get_settings().reach.max_ping_ms
        # Нужно дважды ниже — в запасном значении и в тексте сообщения, —
        # поэтому читаем один раз здесь, локально.
        default_ping = get_settings().reach.max_ping_ms
        if raw_ping is None:
            self.max_ping_ms = None
        else:
            try:
                self.max_ping_ms = max(1, int(raw_ping))
            except (TypeError, ValueError):
                LOGGER.warning("Некорректный порог пинга %r, беру %s", raw_ping, default_ping)
                self.max_ping_ms = int(default_ping) if default_ping else None
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
                "Reachability: %d целей, профиль для stable >= %d, порог TTFB: %s",
                len(self._targets), self.min_stable,
                f"{self.max_ping_ms} мс (всем целям)" if self.max_ping_ms
                else f"{default_ping} мс по умолчанию, у целей свои",
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
            max_ping_ms=self.max_ping_ms,
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
