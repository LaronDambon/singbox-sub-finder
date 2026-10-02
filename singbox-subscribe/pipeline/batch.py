"""Разбиение работы на батчи и учёт того, как батч закончился.

Планирование
------------
Полный батч — ``PIPELINE_BATCH_SIZE`` серверов (по умолчанию 100). Если в
очереди осталось меньше, запускать один недобранный батч смысла нет: процесс
sing-box всё равно один, а свободные параллельные слоты простаивают. Поэтому
остаток дробится на несколько меньших батчей — это и есть «внутренняя
фрагментация»:

    131 сервер, батч 100, 3 слота  ->  [100, 15, 16]
     30 серверов, батч 100, 3 слота ->  [10, 10, 10]

У каждого батча свои конфиг, свой порт, свой дедлайн и свой счётчик.

Итог батча
----------
``BatchOutcome`` различает ЧЕТЫРЕ разных исхода, которые раньше смешивались в
«не получилось проверить»:

    COMPLETED    — все серверы батча ответили;
    PARTIAL      — часть ответила, остальные не отозвались до дедлайна;
    NO_RESULTS   — sing-box отработал, но ни одного вердикта не напечатал;
    CONFIG_ERROR — не собрался конфиг или процесс упал сразу, результатов нет.

Последние два — не «серверы мертвы». Их серверы возвращаются в очередь.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class BatchOutcome(str, Enum):
    """Как закончился батч sing-box."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    NO_RESULTS = "no_results"
    CONFIG_ERROR = "config_error"
    SPAWN_ERROR = "spawn_error"

    @property
    def has_verdicts(self) -> bool:
        """Были ли хоть какие-то вердикты — их нужно записать в базу."""
        return self in (BatchOutcome.COMPLETED, BatchOutcome.PARTIAL)


def plan_batches(
    total: int, batch_size: int, slots: int, *, min_chunk: int = 8,
) -> list[int]:
    """Сколько серверов класть в каждый параллельный батч.

    Возвращает список размеров. Сумма равна ``total`` (если total помещается в
    ``slots` батчей), каждый кусок не больше ``batch_size``.
    """
    if total <= 0 or slots <= 0 or batch_size <= 0:
        return []
    min_chunk = max(1, min(min_chunk, batch_size))
    sizes: list[int] = []
    left = total
    while left > 0 and len(sizes) < slots:
        free = slots - len(sizes)
        if left >= batch_size:
            take = batch_size
        elif free == 1 or left < min_chunk * 2:
            # Дробить больше не на что: забираем всё одним последним батчем.
            take = left
        else:
            # Делим остаток поровну между свободными слотами.
            take = max(min_chunk, left // free)
        take = min(take, left)
        sizes.append(take)
        left -= take
    return sizes


@dataclass
class BatchReport:
    """Учёт одного батча: что запускали, что получили, чем закончилось."""

    index: int
    size: int
    outcome: BatchOutcome = BatchOutcome.NO_RESULTS
    tag_prefix: str = ""
    lines: list[str] = field(default_factory=list)
    #: тег sing-box -> исходная строка прокси
    tag_to_line: dict[str, str] = field(default_factory=dict)
    alive: int = 0
    dead: int = 0
    silent: int = 0
    lines_seen: int = 0
    dns_failures: int = 0
    dial_timeouts: int = 0
    fatal_lines: list[str] = field(default_factory=list)
    unparsable: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""
    attempts: int = 1
    #: строки, которые вернуть в очередь (sing-box о них не сказал ни слова)
    retry_lines: list[str] = field(default_factory=list)
    #: тег sing-box -> вердикт: точная картина «кто ответил, кто нет»
    verdicts: dict = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"батч {self.index} ({self.size} серв.): "
            f"ответили {self.alive + self.dead}/{self.size} "
            f"(живых {self.alive}, мёртвых {self.dead}), "
            f"не отозвались {self.silent}, {self.outcome.value}, {self.elapsed:.1f}s"
        )

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "size": self.size,
            "outcome": self.outcome.value,
            "alive": self.alive,
            "dead": self.dead,
            "silent": self.silent,
            "unparsable": len(self.unparsable),
            "retry": len(self.retry_lines),
            "dns_failures": self.dns_failures,
            "dial_timeouts": self.dial_timeouts,
            "lines_seen": self.lines_seen,
            "elapsed": round(self.elapsed, 2),
            "attempts": self.attempts,
            "error": self.error[:200],
        }


class BatchClock:
    """Личный дедлайн батча.

    Каждый батч живёт недолго: sing-box не умеет отдавать вердикт по серверу
    быстрее, чем сервер отвечает, поэтому затянувшийся батч лучше убить и
    вернуть молчавших серверов в очередь, чем держать процесс на минутах.

    Время складывается из постоянной части — на старт процесса, разбор
    конфига и DNS — и доли, пропорциональной размеру батча.
    """

    def __init__(self, size: int, *, base_timeout: float, batch_size: int,
                 min_timeout: float = 10.0) -> None:
        self.base = base_timeout
        # Пол не даёт убить батч раньше, чем sing-box вообще что-то сказал.
        ratio = (size / batch_size) if batch_size else 1.0
        self.timeout = min_timeout + base_timeout * max(0.0, ratio)
        self.started = time.monotonic()

    @property
    def remaining(self) -> float:
        return max(0.0, self.timeout - (time.monotonic() - self.started))

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started
