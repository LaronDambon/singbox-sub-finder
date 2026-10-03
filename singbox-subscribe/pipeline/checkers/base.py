"""Базовые типы для проверяющих алгоритмов.

Смысл этого файла — чтобы свой алгоритм проверки серверов писался на
«несложном уровне python». Ниже — весь контракт, который нужно знать.

Минимальный чекер выглядит так:

    from pipeline.checkers import Checker, CheckOutcome, CheckResult, get_logger

    class MyChecker(Checker):
        name = "my_checker"          # как включать в PIPELINE_CHECKERS
        description = "Моя проверка"
        decides_availability = True  # этот чекер решает, жив ли сервер

        async def check(self, ctx):
            log = get_logger(__name__)
            outcomes = {}
            for line in ctx.lines:
                alive, ping = await ctx.run_sync(my_probe, line)
                outcomes[line] = CheckOutcome(ok=alive, ping_ms=ping)
            return CheckResult(outcomes)

Файл с этим классом достаточно положить в
``pipeline/checkers/custom/`` — он подхватится автоматически (папка
настраивается переменной PIPELINE_CUSTOM_CHECKERS_DIR).

Ключевая идея разделения:
  * ``decides_availability = True`` — чекер решает, ЖИВ ли сервер (пинг/доступность).
    Итог: ok=True/False попадает в базу как available.
  * ``decides_availability = False`` — чекер только ДОПОЛНЯЕТ результат
    (страна, тэги достижимости). Поле ``ok`` в его CheckOutcome игнорируется,
    зато ``country``/``capabilities`` переносятся в базу.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - только для подсказок типов
    from pipeline.context import PipelineContext


@dataclass(slots=True)
class CheckOutcome:
    """Результат проверки одной строки прокси.

    ``ok=None`` означает «чекер не решает, жив ли сервер» — так работают
    чекеры-дополнения (страна, достижимость целей).
    """

    ok: bool | None = None
    ping_ms: int | None = None
    country: str | None = None
    capabilities: str | None = None
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    #: Префиксы тэгов, которые этот чекер ЗАМЕНЯЕТ, а не дополняет.
    #: Категория — это «одно из многих»: у сервера может быть ровно одна
    #: speed-метка, а не все категории, найденные за все прошлые прогоны.
    #: Пустой набор — обычное дополнение (тэги копятся).
    capabilities_replace: tuple[str, ...] = ()


@dataclass(slots=True)
class CheckResult:
    """Результат чекера для всего батча.

    ``outcomes`` сопоставляет исходную строку прокси с её результатом.
    Строки, которые чекер пропустил, просто отсутствуют в словаре.
    """

    outcomes: dict[str, CheckOutcome] = field(default_factory=dict)
    # Человекочитаемая сводка для лога и отчёта о прогоне.
    summary: str = ""
    # Строки, которые чекер не смог разобрать (их стоит убрать из ротации).
    unparsable: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return list(self.outcomes)

    def merge(self, other: "CheckResult") -> "CheckResult":
        """Сливает результат другого чекера в этот (для обогащения)."""
        for line, outcome in other.outcomes.items():
            current = self.outcomes.get(line)
            if current is None:
                # Чекер-дополнение пришёл раньше решающего: сохраняем как есть.
                self.outcomes[line] = outcome
                continue
            if outcome.ok is not None and current.ok is None:
                current.ok = outcome.ok
            if outcome.ping_ms is not None and current.ping_ms is None:
                current.ping_ms = outcome.ping_ms
            if outcome.country:
                current.country = outcome.country
            if outcome.capabilities:
                current.capabilities = outcome.capabilities
            if outcome.detail:
                current.detail = outcome.detail
            if outcome.data:
                current.data.update(outcome.data)
        self.unparsable.extend(other.unparsable)
        return self


class CheckContext:
    """Всё, что чекеру нужно для работы.

    Передаётся в ``check()`` движком. Чекер не обязан знать про базу, про
    очереди и про этапы — только про свои строки.
    """

    def __init__(
        self,
        lines: Sequence[str],
        *,
        index: int = 0,
        total_batches: int = 0,
        settings: dict[str, Any] | None = None,
        db: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self.lines = list(lines)
        self.index = index
        self.total_batches = total_batches
        self.settings = dict(settings or {})
        self.db = db
        self.logger = logger or get_logger("checker")

    def value(self, name: str, default: Any = None) -> Any:
        """Настройка по её НАСТОЯЩЕМУ имени, как в .env.

        Сначала смотрим в настройки прогона (их задаёт main.py аргументами
        командной строки), затем в общий объект Settings.

        Зачем это, а не ctx.settings.get("speed_enabled", SPEED_ENABLED):
        такая запись повторяла имя настройки дважды — в .env как
        SPEED_ENABLED и в чекере как speed_enabled. Стоит одному из них
        измениться, и перекрытие молча перестаёт действовать: чекер тихо
        берёт значение по умолчанию. Здесь имя называется ровно один раз.
        """
        if name in self.settings:
            return self.settings[name]
        # Через setting(), а не getattr(get_settings(), name): значения
        # лежат в секциях, и getattr по имени из .env возвращал None
        # МОЛЧА — чекер уходил в int(None) вместо падения на опечатке.
        from config.settings import setting

        return setting(name, default)

    @staticmethod
    async def run_sync(func, *args, **kwargs):
        """Запускает блокирующую функцию в потоке, не блокируя event loop.

        Нужно для urllib/requests/subprocess — почти весь существующий код
        проверки синхронный.
        """
        loop = asyncio.get_running_loop()
        if kwargs:
            from functools import partial

            func = partial(func, **kwargs)
        return await loop.run_in_executor(None, func, *args)

    def __repr__(self) -> str:  # pragma: no cover - удобство отладки
        return (
            f"CheckContext(lines={len(self.lines)}, batch={self.index}"
            f"/{self.total_batches})"
        )


class Checker(ABC):
    """Базовый класс проверяющего алгоритма.

    Обязателен только метод ``check``; остальное имеет разумные умолчания.
    """

    #: Имя чекера — как его включать в PIPELINE_CHECKERS.
    name: str = "checker"
    #: Что делает — попадает в ``pipeline checkers`` (CLI).
    description: str = ""
    #: True — чекер решает, жив ли сервер (его ``ok`` идёт в базу как available).
    #: False — только дополняет результат (страна, тэги целей).
    decides_availability: bool = False
    #: True — чекер можно запускать, даже если основной уже отметил сервер мёртвым.
    run_on_dead: bool = False
    #: True — результат чекера не влияет на stable/available.
    read_only: bool = False

    @abstractmethod
    async def check(self, ctx: CheckContext) -> CheckResult:
        """Проверить батч строк. Возвращает результат по каждой строке."""

    async def setup(self, ctx: "PipelineContext") -> None:
        """Одноразовая подготовка (проверить бинарник, загрузить цели и т.п.)."""

    async def teardown(self) -> None:
        """Освобождение ресурсов в конце прогона."""

    def wants(self, ctx: CheckContext, line: str) -> bool:
        """Фильтр строк перед проверкой (по умолчанию — все строки батча)."""
        return True

    def filter_lines(self, ctx: CheckContext) -> list[str]:
        return [line for line in ctx.lines if self.wants(ctx, line)]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Checker {self.name}>"


class FunctionChecker(Checker):
    """Чекер из одной функции — для совсем простых случаев.

    ```python
    FunctionChecker(
        name="ping",
        fn=lambda line: (True, 42),        # (жив, пинг_мс)
        decides_availability=True,
    )
    ```

    ``fn`` может быть обычной функцией или async-функцией. Она вызывается для
    каждой строки; результат — ``CheckOutcome` или кортеж ``(ok, ping_ms)``.
    """

    def __init__(self, name: str, fn, *, description: str = "",
                 decides_availability: bool = False, concurrency: int = 16,
                 run_on_dead: bool = False):
        self.name = name
        self.fn = fn
        self.description = description
        self.decides_availability = decides_availability
        self.run_on_dead = run_on_dead
        self.concurrency = max(1, int(concurrency))

    async def check(self, ctx: CheckContext) -> CheckResult:
        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(line: str) -> CheckOutcome:
            async with semaphore:
                try:
                    value = self.fn(line)
                    if asyncio.iscoroutine(value):
                        value = await value
                except Exception as exc:  # noqa: BLE001 — чекер не должен ронять прогон
                    return CheckOutcome(ok=False if self.decides_availability else None,
                                        detail=f"{type(exc).__name__}: {exc}")
                if isinstance(value, CheckOutcome):
                    return value
                if isinstance(value, tuple):
                    ok = value[0] if len(value) > 0 else None
                    ping = value[1] if len(value) > 1 else None
                    return CheckOutcome(ok=ok, ping_ms=ping)
                if isinstance(value, bool):
                    return CheckOutcome(ok=value)
                return CheckOutcome(ok=None, data={"result": value})

        results = await asyncio.gather(*(one(line) for line in lines))
        return CheckResult(outcomes=dict(zip(lines, results)))


def discover_checker_files(*directories: Path | str) -> list[Path]:
    """Собирает список .py-файлов с чекерами из указанных папок."""
    found: list[Path] = []
    for directory in directories:
        base = Path(directory)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name.startswith("_"):
                continue
            if "__pycache__" in path.parts:
                continue
            found.append(path)
    return found


def available_lines(ctx: CheckContext, outcomes: dict[str, CheckOutcome]) -> list[str]:
    """Утилита для чекеров-дополнений: строки, признанные живыми."""
    return [line for line in ctx.lines if (outcomes.get(line) or CheckOutcome()).ok is not False]
