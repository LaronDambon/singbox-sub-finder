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
from typing import Iterable, Sequence

from pipeline.logging_setup import get_logger
from pipeline.checkers.base import Checker, discover_checker_files

LOGGER = get_logger("checkers.registry")

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
DEFAULT_CUSTOM_DIR = Path(__file__).resolve().parent / "custom"

#: Классы-наследники Checker, которые не регистрируются как отдельные алгоритмы.
_SKIP_NAMES = {"Checker", "FunctionChecker"}


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
    from config.settings import get_settings

    folders = [BUILTIN_DIR, Path(custom_dir or get_settings().paths.custom_checkers_dir)]
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


def _parse_selection(selection: str | Sequence[str] | None) -> list[str] | None:
    """Разбирает список чекеров. None — значит «все найденные».

    Принимает и список (так его пишет config.yaml: checkers: [url_probe,
    reachability]), и строку через запятую (старая форма из .env, а также
    аргумент --checkers в командной строке).
    """
    if selection is None:
        return None
    if isinstance(selection, (list, tuple, set)):
        items = [str(item).strip().lower() for item in selection if str(item).strip()]
    else:
        raw = str(selection).strip().lower()
        if not raw:
            return None
        items = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    if not items:
        return None
    if len(items) == 1 and items[0] in {"all", "*"}:
        return None
    if len(items) == 1 and items[0] in {"none", "-", "off"}:
        return []
    return items


#: Порядок внутри дополнений. Чем больше число, тем позже чекер.
#: Проверки сервиса (api_service и browser_service) обязаны идти ПОСЛЕ
#: reachability: они уточняют метку [gemini], которую reachability только что
#: поставила, и без неё им нечего проверять.
#: Порядок по замыслу:
#:   country      — страна (дёшево, ни от чего не зависит);
#:   reachability — грубая метка [gemini] по TTFB;
#:   service_access — дешёвый HTTP-отбор: снимает бан там, где сервис явно
#:                  закрыт, и тем самым УМЕНЬШАЕТ число кандидатов;
#:   api_service  — проверка сервиса по API: ДЕШЁВАЯ и потому раньше браузера.
#:                  Делает ту же работу, что browser_service, другим
#:                  транспортом: обычный HTTP-запрос с ключом вместо сессии
#:                  браузера. Включается по умолчанию, браузер — нет;
#:   browser_service — браузерная проверка по тому, что осталось (дорогая:
#:                  3-20 с на сервер, заходы по одному);
#:   speed        — замер скорости (дорогой по трафику, идёт последним).
#: Оба сервисных чекера делят ОДНУ таблицу вердиктов (service_bans) и один
#: список сервисов, поэтому их порядок между собой роли не играет: кто
#: отработал, тот и поставил метку, а второй увидит свежий вердикт в кэше.
_ENRICH_ORDER = {
    "country": 10,
    "reachability": 20,
    "service_access": 30,
    "api_service": 35,
    "browser_service": 40,
    "speed": 50,
}

def sort_checkers(checkers: Iterable[Checker]) -> list[Checker]:
    """Решающие чекеры — первыми, дополнения — после них, в заданном порядке.

    Одного деления «решающий/дополнение» мало: внутри дополнений порядок
    важен. reachability ставит метку, browser_service её уточняет, а
    service_access и speed идут уже по уточнённым данным.
    """
    def key(c: Checker):
        if c.decides_availability:
            return (0, 0)
        return (1, _ENRICH_ORDER.get(getattr(c, "name", ""), 15))

    return sorted(checkers, key=key)


def build(selection: str | None = None, *, custom_dir: str | Path | None = None) -> list[Checker]:
    """Собирает список чекеров к запуску в нужном порядке.

    ```python
    checkers = build()                                  # по PIPELINE_CHECKERS
    checkers = build("url_probe,country")               # явно
    checkers = build(custom_dir="~/my_checkers")        # + своя папка
    ```
    """
    # Явный аргумент важнее настройки. Раньше при selection=None брались ВСЕ
    # найденные чекеры — то есть PIPELINE_CHECKERS не действовал вообще, и
    # отключить чекер через .env было нельзя (в логе это выглядело как
    # «Чекеры: url_probe, country, service_access, speed, reachability,
    # browser_service» при заданном списке из трёх).
    if selection is None:
        from config.settings import get_settings

        selection = get_settings().pipeline.checkers

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