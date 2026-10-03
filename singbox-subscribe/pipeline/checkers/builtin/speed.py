"""Чекер скорости: меряет реальную пропускную способность и доступ к сайту.

Чекер-дополнение (decides_availability=False): он не решает, жив ли сервер,
а добавляет измеренную скорость и категорию. Запускается только по тем
серверам, которые уже признаны живыми, — замер скорости дорогой, и тратить
его на заведомо мёртвые серверы смысла нет.

Что именно проверяется (оба шага через один локальный порт sing-box):

  * сайт (по умолчанию gemini.google.com) — открывается ли и не режет ли
    сервер. Gemini часто отвечает на ping и при этом не отдаёт страницу
    с дата-центрового адреса, поэтому проверка отдельная от доступности;
  * скорость — МБ/с на размеро-контролируемой выкачке, с ограничением по
    времени чтения, а не по размеру.

Результат пишется в колонку capabilities как тэг скорости (на экспорте
превращается в [speed-fast] и т.п.) и в data для отчёта.
"""

from __future__ import annotations

from config.env import (
    SPEED_CONCURRENCY,
    SPEED_ENABLED,
    SPEED_GOOD_MBPS,
    SPEED_MIN_MBPS,
    SPEED_PROBE_BYTES,
    SPEED_PROBE_URL,
    SPEED_READ_SECONDS,
    SPEED_SITE_URL,
    SPEED_TAG_PREFIX,
    SPEED_TIER_TAGS,
)
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.speed")


class SpeedChecker(Checker):
    """Замер скорости и проверка доступа к целевому сайту."""

    name = "speed"
    description = "Скорость скачивания (МБ/с) + доступ к целевому сайту (Gemini)"
    decides_availability = False

    def __init__(self) -> None:
        self.enabled = bool(SPEED_ENABLED)
        self.cfg = None

    def _make_cfg(self, ctx):
        from script.speed_check import SpeedConfig

        s = ctx.settings
        return SpeedConfig(
            site_url=str(s.get("site_url", SPEED_SITE_URL) or SPEED_SITE_URL),
            probe_url=str(s.get("probe_url", SPEED_PROBE_URL) or SPEED_PROBE_URL),
            probe_bytes=int(s.get("probe_bytes", SPEED_PROBE_BYTES)),
            read_seconds=float(s.get("read_seconds", SPEED_READ_SECONDS)),
            min_mbps=float(s.get("min_mbps", SPEED_MIN_MBPS)),
            good_mbps=float(s.get("good_mbps", SPEED_GOOD_MBPS)),
            concurrency=int(s.get("concurrency", SPEED_CONCURRENCY)),
            batch_size=int(s.get("batch_size", 40)),
        )

    async def setup(self, ctx) -> None:
        self.enabled = bool(ctx.settings.get("speed_enabled", SPEED_ENABLED))
        if self.enabled:
            self.cfg = self._make_cfg(ctx)
            LOGGER.info(
                "Speed: сайт %s, загрузка %s, slow < %.1f МБ/с, fast >= %.1f МБ/с",
                self.cfg.site_url, self.cfg.download_url,
                self.cfg.min_mbps, self.cfg.good_mbps,
            )

    async def check(self, ctx: CheckContext) -> CheckResult:
        if not self.enabled:
            return CheckResult()
        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="нет живых серверов для замера")

        cfg = self.cfg or self._make_cfg(ctx)
        from script.speed_check import batch_speed_check

        # Под замком запуска sing-box: транспорт пишет общий временный конфиг.
        results = await run_exclusive(batch_speed_check, sorted(lines), cfg=cfg)

        tiers = dict(SPEED_TIER_TAGS)
        outcomes: dict[str, CheckOutcome] = {}
        counts: dict[str, int] = {}
        for line in lines:
            payload = results.get(line) or {}
            tier = payload.get("tier") or "unknown"
            counts[tier] = counts.get(tier, 0) + 1
            mbps = payload.get("mbps")
            detail_parts = []
            if payload.get("geo"):
                detail_parts.append("сайт не работает в этой стране")
            elif payload.get("site_status"):
                status = payload["site_status"]
                detail_parts.append(f"сайт {status}{' ЗАБЛОКИРОВАН' if payload.get('blocked') else ''}")
            if mbps:
                detail_parts.append(f"{mbps} МБ/с")
            outcomes[line] = CheckOutcome(
                ok=None,
                capabilities=tiers.get(tier, ""),
                # Категория одна: новая метка заменяет старую, а не дописывается.
                capabilities_replace=(SPEED_TAG_PREFIX,),
                detail=payload.get("error") or "; ".join(detail_parts) or None,
                data={
                    "speed_mbps": mbps,
                    "speed_down": payload.get("down_mbps"),
                    "speed_up": payload.get("up_mbps"),
                    "speed_sources": payload.get("sources_ok"),
                    "speed_tier": tier,
                    "site_status": payload.get("site_status"),
                    "site_mbps": payload.get("site_mbps"),
                    "site_blocked": bool(payload.get("blocked")),
                    "site_geo_blocked": bool(payload.get("geo")),
                },
            )
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return CheckResult(outcomes=outcomes, summary=summary)
