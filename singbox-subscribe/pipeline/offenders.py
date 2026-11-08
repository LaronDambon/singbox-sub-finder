"""Кто именно сломал конфиг батча.

sing-box на старте валидирует конфиг целиком и печатает номер сломанного
outbound'а:

    FATAL[0000] decode config at ...: outbounds[33].private_key: json: unknown field
    FATAL[0000] create service: initialize outbound[41]: invalid public_key
    FATAL[0000] create service: initialize endpoint[3]: invalid public_key

Раньше это лечилось догадками: писались локальные проверки «похоже ли
значение на ключ», и каждый новый способ сломать конфиг пробивал очередную
дырку (сначала pbk=enabled, потом private_key в outbounds). Здесь по-другому:
сначала запускаем sing-box и получаем ФАКТИЧЕСКУЮ ошибку с номером, потом
вырезаем именно этот outbound и запускаем снова. Не «угадали причину», а
«убрали то, на что указал sing-box».

Модуль намеренно не знает про батчи и базы: только разбор номера из строки
и вырезание элемента по номеру. Всё остальное — в pipeline.singbox.
"""

from __future__ import annotations

import re

#: Номер сломанного элемента в тексте FATAL.
#:
#: Два разных формата, и оба встречаются в одном и том же логе:
#:
#:   * декодер конфига:  outbounds[33].private_key  ← ОТНОСИТЕЛЬНЫЙ индекс
#:     внутри массива outbounds (считая selector/urltest/direct, которые
#:     сборщик кладёт первыми), плюс имя поля;
#:   * старт сервиса:    initialize outbound[41]    ← АБСОЛЮТНЫЙ индекс того
#:     же массива (в отчёте sing-box нумерует outbounds вместе с группами).
#:
#: Оба формата указывают в ОДИН массив, поэтому работают с ним одинаково.
#: Секцию (outbounds/endpoints) берём из текста: у endpoints свой массив.
_OFFENDER_RES = (
    re.compile(r"outbounds\[(?P<idx>\d+)\]", re.IGNORECASE),
    re.compile(r"initialize\s+outbound\[(?P<idx>\d+)\]", re.IGNORECASE),
    re.compile(r"endpoints\[(?P<idx>\d+)\]", re.IGNORECASE),
    re.compile(r"initialize\s+endpoint\[(?P<idx>\d+)\]", re.IGNORECASE),
)

_ENDPOINT_RE = re.compile(r"endpoints?\[", re.IGNORECASE)


def offender_index(text: str) -> tuple[str, int] | None:
    """Возвращает ("outbounds"|"endpoints", индекс) сломанного элемента.

    None — если в тексте нет указания на конкретный элемент: тогда причину
    по номеру не определить и лечить батч вырезанием нельзя.
    """
    if not text:
        return None
    section = "endpoints" if _ENDPOINT_RE.search(text) else "outbounds"
    for pattern in _OFFENDER_RES:
        match = pattern.search(text)
        if match:
            try:
                idx = int(match.group("idx"))
            except (TypeError, ValueError):
                continue
            if idx >= 0:
                return section, idx
    return None


def _tag_of(item) -> str:
    if isinstance(item, dict):
        return str(item.get("tag") or "")
    return ""


def _rebuild_without(items: list, idx: int) -> tuple[list, str] | None:
    """Убирает элемент idx и все ссылки на вырезанный тег.

    Удалять недостаточно: selector/urltest держат список outbounds по тегам,
    и висячая ссылка — это уже другая ошибка запуска («outbound not found»),
    то есть батч упал бы снова, но по другой причине, и мы бы чинили не то.

    Возвращает (новый список, тег) или None, если номер вне диапазона.
    """
    if idx < 0 or idx >= len(items):
        return None
    victim = items[idx]
    tag = _tag_of(victim)
    kept = [item for pos, item in enumerate(items) if pos != idx]
    if tag:
        for item in kept:
            if not isinstance(item, dict):
                continue
            children = item.get("outbounds")
            if isinstance(children, list) and tag in children:
                item["outbounds"] = [child for child in children if child != tag]
    return kept, tag


def prune_config(config: dict, section: str, idx: int) -> str | None:
    """Вырезает сломанный элемент прямо в конфиге. Возвращает его тег.

    None — если вырезать нечего (номер вне диапазона или ссылок на массив).
    Конфиг правится на месте: он всё равно уже никому не нужен, кроме
    этого запуска.
    """
    if not isinstance(config, dict):
        return None
    items = config.get(section)
    if not isinstance(items, list):
        return None
    rebuilt = _rebuild_without(items, idx)
    if rebuilt is None:
        return None
    kept, tag = rebuilt
    config[section] = kept
    return tag


def drop_group_children(config: dict, tags: set[str]) -> int:
    """Убирает теги из списков selector/urltest. Возвращает число правок.

    Нужно после того, как строки-виновники выброшены из батча ДО сборки:
    без этого группа ссылалась бы на несуществующий outbound и sing-box
    упал бы на «outbound not found».
    """
    if not tags:
        return 0
    fixed = 0
    for section in ("outbounds", "endpoints"):
        items = config.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            children = item.get("outbounds")
            if isinstance(children, list):
                pruned = [child for child in children if child not in tags]
                if len(pruned) != len(children):
                    item["outbounds"] = pruned
                    fixed += 1
    return fixed
