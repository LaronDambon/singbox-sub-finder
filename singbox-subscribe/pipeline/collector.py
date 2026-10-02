"""Служба 2: разбор вывода sing-box на лету.

sing-box не отдаёт результат пачкой в конце — он печатает строку за строкой
в процессе проверки:

    DEBUG[0000] outbound/urltest[proxy]: outbound 000012 available (127ms)
    DEBUG[0005] outbound/urltest[proxy]: outbound 000013 unavailable: i/o timeout
    DEBUG[0005] dns: lookup domain example.com

Класс BatchCollector принимает эти строки по одной, как только они пришли, и
ведёт счёт по трём состояниям сервера:

    ALIVE       — sing-box ответил ``available``, есть пинг;
    DEAD        — sing-box ответил ``unavailable`` (сервер не прошёл проверку);
    NO_RESULT   — сервер вообще не упомянут в выводе.

Разделение последнего состояния принципиально. Если по таймауту или из-за
ошибки сборки конфига сервер не попал в вывод, он НЕ мёртвый — он
непроверенный. Такой сервер возвращается в очередь, а не выписывается из базы.
Раньше эти случаи не различались, и сломанный батч выглядел как «всё мертво».

Модуль ничего не знает про subprocess: сюда подаются уже прочитанные строки.
Так его можно тестировать без sing-box вообще.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from pipeline.logging_setup import get_logger

LOGGER = get_logger("collector")


class Verdict(str, Enum):
    """Итог проверки одного сервера."""

    #: sing-box ответил, сервер доступен, есть пинг.
    ALIVE = "alive"
    #: sing-box ответил ``unavailable`` — сервер не отвечает.
    DEAD = "dead"
    #: sing-box не упомянул сервер: таймаут батча или сбой конфигурации.
    NO_RESULT = "no_result"


#: Основная строка результата urltest: «...outbound <тег> available (127ms)».
#:
#: Тег sing-box печатает НЕОДНИМ словом: он может содержать пробел и эмодзи,
#: например «🇨🇦 [openproxylist.com] ss-CA#15 unavailable». Поэтому тег ловится
#: лениво до первого «available/unavailable», а не через \S+.
RESULT_RE = re.compile(
    r"outbound/urltest\[[^\]]+\]:\s*outbound\s+(?P<tag>.+?)\s+"
    r"(?P<state>available|unavailable)\b(?:\s*\((?P<ping>\d+)\s*ms\))?",
    re.IGNORECASE,
)

#: Строка urltest без вердикта (есть только тег) — сервер упомянут, но ответа нет.
RESULT_NO_VERDICT_RE = re.compile(
    r"outbound/urltest\[[^\]]+\]:\s*outbound\s+(?P<tag>.+?)\s*$"
)

#: Причины, по которым конкретный outbound не смог подключиться.
REASON_RE = re.compile(
    r"(?P<reason>dial tcp|i/o timeout|context deadline exceeded|"
    r"connection reset|connection refused|handshake|lookup|"
    r"use of closed network connection|network is unreachable)",
)

#: Строка, где sing-box сам отчитался об ошибке (не о конкретном сервере).
FATAL_RE = re.compile(r"(FATAL|ERROR)\[")


@dataclass
class ServerResult:
    """Вердикт по одному серверу."""

    tag: str
    verdict: Verdict
    ping_ms: int | None = None
    reason: str = ""

    @property
    def answered(self) -> bool:
        """Ответил ли sing-box по этому серверу хоть как-то."""
        return self.verdict is not Verdict.NO_RESULT


@dataclass
class BatchCollector:
    """Живой сборщик результатов по одному батчу sing-box.

    Экземпляр на каждый батч: у каждого свой вывод, свой счёт и свой дедлайн.
    Общими остаются только класс и агрегированная статистика прогона.
    """

    #: Сколько серверов в батче — столько результатов ждём в лучшем случае.
    expected: int = 0
    #: Человеческое имя батча для логов.
    name: str = ""

    results: dict[str, ServerResult] = field(default_factory=dict)
    lines_seen: int = 0
    result_lines: int = 0
    dns_lookups: int = 0
    dns_failures: int = 0
    dial_timeouts: int = 0
    fatal_lines: list[str] = field(default_factory=list)
    _buffer: str = ""

    # -------------------------------------------------------------- приём строк
    def feed(self, line: str) -> bool:
        """Принимает одну строку вывода sing-box.

        Возвращает True, если строка дала вердикт по серверу. Регистрирует
        строки, по которым сервер упомянут, но ответа нет — они важны для
        отчёта и для ответа на вопрос «кто конкретно не отвечает».
        """
        line = line.strip()
        if not line:
            return False
        self.lines_seen += 1

        if FATAL_RE.search(line):
            self.fatal_lines.append(line)
            LOGGER.debug("[%s] sing-box: %s", self.name, line)
            return False

        if "dns: lookup" in line:
            self.dns_lookups += 1

        match = RESULT_RE.search(line)
        if match:
            self._record(
                tag=match.group("tag").strip(),
                state=match.group("state").lower(),
                ping=match.group("ping"),
                line=line,
            )
            return True

        if RESULT_NO_VERDICT_RE.search(line):
            # Упомянут, но без available/unavailable — вердикта нет.
            self._count_reason(line)
            return False

        self._count_reason(line)
        return False

    def feed_chunk(self, chunk: str) -> bool:
        """Принимает кусок вывода, склеивая строки, разорванные посреди чанка.

        Нужно, когда читающий поток отдаёт данные блоками, а не построчно.
        """
        data = self._buffer + chunk
        parts = data.split("\n")
        self._buffer = parts.pop()
        got = False
        for part in parts:
            got = self.feed(part) or got
        return got

    def flush(self) -> bool:
        """Обрабатывает хвост буфера без завершающего перевода строки."""
        if not self._buffer:
            return False
        tail, self._buffer = self._buffer, ""
        return self.feed(tail)

    # -------------------------------------------------------------- учёт вердиктов
    def _record(self, *, tag: str, state: str, ping: str | None, line: str) -> None:
        self.result_lines += 1
        if state == "available":
            self.results[tag] = ServerResult(
                tag=tag, verdict=Verdict.ALIVE,
                ping_ms=int(ping) if ping else None,
            )
        else:
            reason = _reason_of(line)
            self.results[tag] = ServerResult(tag=tag, verdict=Verdict.DEAD, reason=reason)
            self._count_reason(line)

    def _count_reason(self, line: str) -> None:
        low = line.lower()
        reason = _reason_of(line)
        if reason in {"lookup", "dns"}:
            self.dns_failures += 1
        elif reason in {"dial tcp", "i/o timeout", "context deadline exceeded"}:
            self.dial_timeouts += 1

    # ------------------------------------------------------------------ итоги
    @property
    def complete(self) -> bool:
        """Все ожидаемые серверы отчитались — батч можно закрывать досрочно."""
        return self.expected > 0 and len(self.results) >= self.expected

    def verdict(self, tag: str) -> ServerResult:
        """Вердикт по тегу; для неизвестного тега — NO_RESULT."""
        return self.results.get(tag) or ServerResult(tag=tag, verdict=Verdict.NO_RESULT)

    def missing_tags(self) -> list[str]:
        """Теги, о которых sing-box не сказал ни слова."""
        return [tag for tag in self.results if self.results[tag].verdict is Verdict.NO_RESULT]

    def counts(self) -> dict[str, int]:
        alive = sum(1 for r in self.results.values() if r.verdict is Verdict.ALIVE)
        dead = sum(1 for r in self.results.values() if r.verdict is Verdict.DEAD)
        return {
            "alive": alive,
            "dead": dead,
            "answered": alive + dead,
            "silent": max(0, self.expected - alive - dead),
            "lines": self.lines_seen,
        }

    def summary(self) -> str:
        c = self.counts()
        return (
            f"{self.name or 'батч'}: ответили {c['answered']}/{self.expected} "
            f"(живых {c['alive']}, мёртвых {c['dead']}), "
            f"не отозвались {c['silent']}, строк вывода {c['lines']}"
        )


def _reason_of(line: str) -> str:
    """Короткая причина неудачи из строки sing-box."""
    low = line.lower()
    if "lookup" in low:
        return "dns"
    match = REASON_RE.search(low)
    return match.group("reason") if match else ""


def summarize(lines: list[str], *, expected: int = 0, name: str = "") -> BatchCollector:
    """Разбирает готовый список строк (удобно для тестов и разбора инцидентов)."""
    collector = BatchCollector(expected=expected, name=name)
    for line in lines:
        collector.feed(line)
    collector.flush()
    return collector
