"""Проверка живости серверов через urltest sing-box.

Это штатный «решающий» чекер: именно он отвечает на вопрос, жив сервер или
мёртв. Логика проверки вынесена в общие модули и не дублируется здесь:

  * ``pipeline.batch``      — как режем список на параллельные батчи;
  * ``pipeline.singbox``    — запуск батча и чтение его вывода на лету;
  * ``pipeline.collector``  — разбор строк вывода и три состояния сервера;
  * ``pipeline.dispatcher`` — сборка батчей, деление сбойных пополам.

Здесь только тонкая обёртка: получить строки, прогнать и разложить отчёт
обратно в ``CheckResult``.

Три состояния, а не два «жив/мёртв»
------------------------------------
    ALIVE      — sing-box ответил ``available``, есть пинг;
    DEAD       — sing-box ответил ``unavailable``;
    NO_RESULT  — sing-box не сказал про сервер ни слова.

Третье состояние возвращается как ``ok=None``: сервер не признаётся мёртвым
и уходит обратно в очередь. Раньше молчание sing-box и настоящая смерть
сервера выглядели одинаково — из-за этого сбой одного батча выглядел как
массовая гибель серверов.
"""

from __future__ import annotations

from pathlib import Path

from config.env import (
    PIPELINE_BATCH_SIZE,
    PIPELINE_CHECK_TIMEOUT,
    PIPELINE_MAX_ATTEMPTS,
    URLTEST_URL,
)
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.collector import Verdict, summarize
from pipeline.dispatcher import BatchDispatcher, make_dispatcher
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.url_probe")


def parse_ping_results(lines) -> dict:
    """Разбирает готовый вывод sing-box в {tag: ping_ms|None}.

    None означает, что тег отмечен как unavailable: сервер не ответил.
    Оставлено для совместимости и для разбора инцидентов — сам pipeline
    разбирает вывод построчно через ``pipeline.collector``.
    """
    collector = summarize(list(lines))
    return {
        tag: (result.ping_ms if result.verdict is Verdict.ALIVE else None)
        for tag, result in collector.results.items()
    }


def has_urltest_records(lines) -> bool:
    """Есть ли в выводе хоть один настоящий результат проверки."""
    return summarize(list(lines)).result_lines > 0


class UrlProbeChecker(Checker):
    """Проверка живости сервера через urltest-батчи sing-box."""

    name = "url_probe"
    description = "urltest sing-box: доступность и задержка (решает, жив ли сервер)"
    decides_availability = True
    #: Этап проверки отдаёт этому чекеру батчи целиком: служба 1 дробит список
    #: на параллельные батчи, служба 2 разбирает вывод каждого на лету.
    #: Свои проверяющие алгоритмы это не включают — они идут обычным путём.
    uses_dispatcher = True

    def __init__(self) -> None:
        self.batch_size = PIPELINE_BATCH_SIZE
        self.timeout = PIPELINE_CHECK_TIMEOUT
        self.max_attempts = PIPELINE_MAX_ATTEMPTS
        self.urltest = URLTEST_URL
        self.slots = 1
        self._dispatcher: BatchDispatcher | None = None

    async def setup(self, ctx) -> None:
        import os

        settings = ctx.settings
        self.batch_size = int(settings.get("batch_size", self.batch_size))
        self.timeout = float(settings.get("timeout", self.timeout))
        self.max_attempts = int(settings.get("max_attempts", self.max_attempts))
        self.urltest = ctx.settings.get("url", URLTEST_URL) or URLTEST_URL
        # Один батч на вызов check(): параллелизмом занимается диспетчер,
        # когда он сам набирает серверы из базы.
        self.slots = 1

        self._dispatcher = make_dispatcher(
            db=ctx.db,
            batch_size=self.batch_size,
            slots=self.slots,
            timeout=self.timeout,
            urltest=self.urltest,
            max_attempts=self.max_attempts,
        )
        if not Path(self._dispatcher.singbox_path).exists():
            LOGGER.warning(
                "Бинарник sing-box не найден по пути %s — проверка вернёт «не проверено».",
                self._dispatcher.singbox_path,
            )
        if (os.getenv("SING_BOX_PORT") or "").strip():
            LOGGER.info(
                "SING_BOX_PORT=%s задан, но каждый батч берёт СВОЙ свободный порт.",
                os.getenv("SING_BOX_PORT"),
            )

    async def check(self, ctx: CheckContext) -> CheckResult:
        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult()

        dispatcher = self._dispatcher or make_dispatcher(
            batch_size=self.batch_size, slots=self.slots, timeout=self.timeout,
            urltest=self.urltest, max_attempts=self.max_attempts,
        )
        reports = await dispatcher.run_lines(lines)

        outcomes: dict[str, CheckOutcome] = {}
        unparsable: list[str] = []
        retry: list[str] = []
        for report in reports:
            unparsable.extend(report.unparsable)
            retry.extend(report.retry_lines)
            for tag, verdict in report.verdicts.items():
                raw = report.tag_to_line.get(tag)
                if not raw:
                    continue
                outcomes[raw] = _to_outcome(verdict)

        # Серверы без вердикта: sing-box о них не сказал. Это не «мёртвые».
        for line in lines:
            if line in outcomes:
                continue
            detail = "sing-box не успел проверить" if line in retry else "нет результата от sing-box"
            outcomes[line] = CheckOutcome(ok=None, detail=detail)

        alive = sum(1 for o in outcomes.values() if o.ok is True)
        return CheckResult(
            outcomes=outcomes,
            summary=f"{alive}/{len(outcomes)} ответили",
            unparsable=unparsable,
        )


def _to_outcome(verdict) -> CheckOutcome:
    """Переводит вердикт службы 2 в результат чекера."""
    if verdict.verdict is Verdict.ALIVE:
        return CheckOutcome(
            ok=True, ping_ms=verdict.ping_ms, data={"tag": verdict.tag},
        )
    if verdict.verdict is Verdict.DEAD:
        return CheckOutcome(
            ok=False, ping_ms=None,
            detail=verdict.reason or "не ответил",
            data={"tag": verdict.tag},
        )
    return CheckOutcome(ok=None, detail="sing-box не дал результата", data={"tag": verdict.tag})
