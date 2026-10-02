"""Реестр проверяющих алгоритмов.

Чекеры лежат обычными .py-файлами в двух папках:

  * ``pipeline/checkers/builtin/`` — штатные алгоритмы репозитория;
  * ``pipeline/checkers/custom/``  — ваши собственные (папка настраивается
    переменной PIPELINE_CUSTOM_CHECKERS_DIR).

Каждый файл импортируется, из него берутся все наследники ``Checker`` и
создаются по одному экземпляру. Добавили файл — алгоритм уже доступен,
править ядро не нужно.

Какие из них реально запускать, задаёт переменная окружения:

    PIPELINE_CHECKERS=url_probe,country,reachability
    PIPELINE_CHECKERS=none          # ничего не включать
    PIPELINE_CHECKERS=all           # всё, что найдено в папках

Порядок: сначала идут чекеры с ``decides_availability=True`` (решают, жив ли
сервер), потом дополнения (страна, достижимость). Итоговый «жив/мёртв» берётся
у первого решающего чекера.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys as _sys
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from pipeline.logging_setup import get_logger
from pipeline.checkers.base import Checker, discover_checker_files

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.context import PipelineContext

LOGGER = get_logger("checkers.registry")

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
DEFAULT_CUSTOM_DIR = Path(__file__).resolve().parent / "custom"

#: Классы-наследники Checker, которые не регистрируются как отдельные алгоритмы.
_SKIP_NAMES = {"Checker", "FunctionChecker"}

_instances: dict[str, Checker] = {}


def _import_module_from_path(path: Path):
    """Импортирует .py по пути как отдельный модуль с уникальным именем."""
    module_name = f"pipeline.checkers._loaded.{path.stem}"
    if module_name in _sys.modules:
        return _sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить модуль чекера: {path}")
    module = importlib.util.module_from_spec(spec)
    _sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _sys.modules.pop(module_name, None)
        raise
    return module


def _iter_checker_classes(module, source: Path):
    for obj in list(vars(module).values()):
        if not inspect.isclass(obj) or obj.__name__ in _SKIP_NAMES:
            continue
        if not issubclass(obj, Checker):
            continue
        # Класс должен быть объявлен именно в этом модуле, а не импортирован
        # из соседнего — иначе один чекер зарегистрировался бы несколько раз.
        if inspect.getmodule(obj) is not module:
            continue
        if inspect.isabstract(obj):
            continue
        yield obj, source


def discover(custom_dir: str | Path | None = None) -> list[Checker]:
    """Находит все чекеры в папках builtin/ и custom/, создаёт экземпляры."""
    from config.env import PIPELINE_CUSTOM_CHECKERS_DIR

    folders = [BUILTIN_DIR, Path(custom_dir or PIPELINE_CUSTOM_CHECKERS_DIR)]
    found: dict[str, Checker] = {}

    for path in discover_checker_files(*folders):
        try:
            module = _import_module_from_path(path)
        except Exception as exc:  # noqa: BLE001 — битый чекер не должен ронять прогон
            LOGGER.error("Чекер %s не импортирован: %s", path.name, exc)
            continue
        for cls, source in _iter_checker_classes(module, path):
            instance_name = getattr(cls, "name", cls.__name__)
            try:
                instance = cls()
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Чекер %s не создан: %s", instance_name, exc)
                continue
            key = instance.name or instance_name
            if key in found:
                LOGGER.warning(
                    "Дубликат имени чекера '%s': %s заменяет более ранний", key, source.name,
                )
            found[key] = instance
    return list(found.values())


def _parse_selection(selection: str | None) -> list[str] | None:
    """Разбирает PIPELINE_CHECKERS. None — значит «все найденные»."""
    raw = (selection or "").strip().lower()
    if not raw or raw in {"all", "*"}:
        return None
    if raw in {"none", "-", "off"}:
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def sort_checkers(checkers: Iterable[Checker]) -> list[Checker]:
    """Решающие чекеры — первыми, дополнения — после них."""
    return sorted(checkers, key=lambda c: 0 if c.decides_availability else 1)


def build(selection: str | None = None, *, custom_dir: str | Path | None = None) -> list[Checker]:
    """Собирает список чекеров к запуску в нужном порядке.

    ```python
    checkers = build()                                  # по PIPELINE_CHECKERS
    checkers = build("url_probe,country")               # явно
    checkers = build(custom_dir="~/my_checkers")        # + своя папка
    ```
    """
    global _instances

    wanted = _parse_selection(selection)
    available = discover(custom_dir)

    if wanted is None:
        chosen = available
    else:
        by_name = {c.name: c for c in available}
        chosen = []
        for name in wanted:
            checker = by_name.get(name)
            if checker is None:
                LOGGER.warning(
                    "Чекер '%s' не найден. Доступны: %s",
                    name, ", ".join(sorted(by_name)) or "(нет)",
                )
                continue
            chosen.append(checker)

    if not chosen:
        # Без решающего чекера результат проверки бессмыслен: нечем определить
        # «жив/мёртв». Подставляем штатный url_probe.
        fallback = next((c for c in available if c.name == "url_probe"), None)
        if fallback is not None and wanted != []:
            LOGGER.info("Ни один чекер не выбран — используется url_probe по умолчанию")
            chosen = [fallback]

    _instances = {c.name: c for c in chosen}
    return sort_checkers(chosen)


def describe(checkers: Iterable[Checker] | None = None) -> list[dict]:
    """Список чекеров для CLI (```python -m pipeline checkers```)."""
    items = list(checkers) if checkers is not None else discover()
    return [
        {
            "name": c.name,
            "description": c.description or "",
            "decides_availability": c.decides_availability,
            "read_only": c.read_only,
            "run_on_dead": c.run_on_dead,
        }
        for c in sort_checkers(items)
    ]


async def setup_all(checkers: Iterable[Checker], ctx: "PipelineContext") -> None:
    for checker in checkers:
        try:
            await checker.setup(ctx)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Подготовка чекера %s не удалась: %s", checker.name, exc)


async def teardown_all(checkers: Iterable[Checker]) -> None:
    for checker in checkers:
        try:
            await checker.teardown()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Завершение чекера %s не удалось: %s", checker.name, exc)
