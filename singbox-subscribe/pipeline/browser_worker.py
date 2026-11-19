"""Браузерная проверка ОТДЕЛЬНЫМ процессом, параллельно остальным проверкам.

Зачем отдельно. Раньше browser_service был чекером-дополнением и работал
внутри _enrich каждого батча: пока браузер возится с одним сервером
(20-60 с), батч стоит и ждёт.

Почему отдельный ПРОЦЕСС, а не задача в том же. Все запуски sing-box в
проекте сериализованы общим замком (pipeline/checkers/builtin/_runner.py):
они меняют состояние процесса — os.chdir, общие временные конфиги. Пока
браузер держит этот замок (а он держит его на всю порцию серверов),
основная проверка не может запустить ни одного батча. Отдельный процесс
живёт со своим замком, своим cwd и своим диапазоном портов.

Общая у них только база (WAL это позволяет) и файл лога.

Цена: сервер, до которого браузер не дошёл в этом цикле, останется без
метки [gemini-chat] до следующего раза. Конфиг при этом соберётся по метке
[gemini-api], которую ставит обычная проверка.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from pipeline.checkers.base import CheckContext
from pipeline.logging_setup import get_logger

LOGGER = get_logger("browser_worker")

#: Сколько серверов брать за один заход.
DEFAULT_CHUNK = 5
#: Пауза между заходами, сек. Между серверами пауза своя (browser_service.pause).
DEFAULT_INTERVAL = 20.0

#: Свой диапазон портов для дочернего процесса: не пересекается с основным
#: (40000-49000), поэтому гонка за порт между процессами исключена.
CHILD_PORT_START = "50000"
CHILD_PORT_END = "59000"

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _candidate_tags(checker) -> list[str]:
    """Метки, по которым браузер выбирает серверы (пусто = все живые)."""
    # candidate_tags задаёт пользователь в config.yaml. Пустой кортеж здесь
    # означает "all" — смотреть все живые серверы, а не только с меткой API.
    custom = getattr(checker, "candidate_tags", None)
    if custom is not None:
        return list(custom)
    tags = list(getattr(checker, "accept_tags", ()) or ())
    tag = getattr(checker, "tag", "")
    if tag and tag not in tags:
        tags.append(tag)
    return tags


async def check_once(ctx, checker, chunk: int) -> int:
    """Один заход: взять порцию кандидатов, проверить, записать метки."""
    from pipeline.stages.check import _key_of

    service = str(getattr(checker, "service", "") or "")
    # Берём с запасом: часть кандидатов отсеется по свежему вердикту в кэше
    # (проверять только что проверенное незачем), и нам всё равно нужно
    # набрать ровно chunk.
    candidates = await ctx.db.lines_with_tags(
        _candidate_tags(checker), limit=chunk * 6,
        # Исключаем конфиги с действующим отказом: вердикт живёт тегом
        # gemini-chat с passed=0, отдельной таблицы банов больше нет.
        exclude_banned_service=getattr(checker, "tag", None) or None,
    )
    if not candidates:
        return 0

    cache = getattr(checker, "_cache", None)
    lines = []
    seen_endpoints = set()
    for line in candidates:
        # Один сервер может лежать в базе десятком строк: они отличаются
        # только параметрами после '?', а выход у них один и тот же
        # (vless://uuid@188.114.98.112:80?...). Проверять такой сервер
        # несколько раз бессмысленно — вердикт будет тот же.
        endpoint = line.split("?", 1)[0]
        if endpoint in seen_endpoints:
            continue
        if cache is not None and cache.get(line) is not None:
            continue          # свежий вердикт — не перепроверяем
        seen_endpoints.add(endpoint)
        lines.append(line)
        if len(lines) >= chunk:
            break
    if not lines:
        LOGGER.info(
            "Браузер (отдельный процесс): все %d кандидатов с меткой имеют "
            "свежий вердикт — проверять нечего", len(candidates),
        )
        return 0
    # Карта меток обязательна: чекер отбирает кандидатов по ней
    # (CheckContext.capabilities_of). Без неё все строки выглядят «без метки»
    # и отсеиваются, хотя lines_with_tags их только что выбрал по метке.
    caps = await ctx.db.capabilities_map(lines)
    check_ctx = CheckContext(
        lines, settings=ctx.settings, db=ctx.db, logger=ctx.logger,
        capabilities_by_line=caps, pass_mark="",
    )
    result = await checker.check(check_ctx)
    rows = []
    for line, outcome in (result.outcomes or {}).items():
        if outcome is None:
            continue
        caps = outcome.capabilities or ""
        # Пустой профиль не пишем: запись с capabilities="" и без метки
        # прохода ничего не меняет, а UPDATE на каждый забаненный сервер
        # ни к чему.
        if not caps:
            continue
        rows.append({"key": _key_of(line), "country": "", "capabilities": caps})
    if rows:
        await ctx.db.record_enrichment(rows)
    LOGGER.info(
        "Браузер (отдельный процесс): %s | меток проставлено %d из %d",
        result.summary, len(rows), len(lines),
    )
    return len(rows)


class BrowserWorker:
    """Запускает браузерную проверку дочерним процессом по расписанию."""

    def __init__(self, ctx, checker, *, chunk: int = DEFAULT_CHUNK,
                 interval: float = DEFAULT_INTERVAL) -> None:
        self.ctx = ctx
        self.checker = checker
        self.chunk = max(1, int(chunk))
        self.interval = max(5.0, float(interval))
        self.rounds = 0

    async def run_once(self) -> int:
        """Один дочерний процесс: своя порция, свой sing-box, свой замок."""
        env = dict(os.environ)
        env["SINGBOX_PORT_RANGE_START"] = CHILD_PORT_START
        env["SINGBOX_PORT_RANGE_END"] = CHILD_PORT_END
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pipeline.browser_worker", "--once",
                "--chunk", str(self.chunk),
                cwd=str(PACKAGE_ROOT), env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001 — не смогли запустить, попробуем позже
            LOGGER.exception("Браузерный цикл: дочерний процесс не запустился")
            return 0
        code = await proc.wait()
        self.rounds += 1
        if code:
            LOGGER.warning("Браузерный цикл: дочерний процесс завершился с кодом %s", code)
        return code or 0

    async def run(self) -> None:
        LOGGER.info(
            "Браузерная проверка вынесена в отдельный процесс: порция %d, пауза %.0f с",
            self.chunk, self.interval,
        )
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Браузерный цикл: заход не удался")
            await asyncio.sleep(self.interval)


def main(argv=None) -> int:
    """CLI: python -m pipeline.browser_worker --once [--chunk N]."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m pipeline.browser_worker")
    parser.add_argument("--once", action="store_true", help="один заход и выход")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    args = parser.parse_args(argv)

    from config.settings import init_settings
    from pipeline.checkers.registry import build as build_checkers
    from pipeline.context import PipelineContext, default_settings
    from pipeline.database import Database
    from pipeline.logging_setup import setup_logging

    init_settings()
    setup_logging()

    checkers = build_checkers("browser_service")
    checker = next((c for c in checkers if getattr(c, "name", "") == "browser_service"), None)
    if checker is None:
        LOGGER.error("Чекер browser_service не найден")
        return 1

    db = Database()
    ctx = PipelineContext(settings=default_settings(), db=db,
                          checkers=[checker], logger=LOGGER)

    async def _main() -> int:
        try:
            await checker.setup(ctx)
            if not getattr(checker, "enabled", True):
                LOGGER.info("Браузерная проверка выключена — выходим")
                return 0
            await check_once(ctx, checker, args.chunk)
            return 0
        finally:
            await db.close()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
