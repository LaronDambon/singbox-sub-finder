# -*- coding: utf-8 -*-
"""Развёртывание блоков alldeploy на метки сервисов.

Шаблон (config/templates/*.json) остаётся обычным конфигом sing-box.
Отличие одно: в outbounds лежит СЛУЖЕБНЫЙ КЛЮЧ "alldeploy" — не outbound,
а контейнер блоков:

    {
      "tag": "alldeploy",
      "type": "block",
      "tags": ["all"],
      "blocks": [ ...блоки... ]
    }

Поле "tags" решает, на какие метки блоки размножаются:
  * ["all"]                     — на ВСЕ метки из reachability_targets.json
                                  плюс браузерная метка из настроек;
  * ["openrouter", "gemini-chat"] — только на перечисленные метки,
                                  остальные из файла целей игнорируются.

Внутри блоков работают три подстановки, и порядок важен — сначала
длинные, иначе "alldeploy" съест начало "alldeploy-reserved":
  * "alldeploy-reserved" -> "<метка>-reserved"
  * "alldeploy-best"     -> "<метка>-best"
  * "alldeploy"          -> "<метка>"

Подстановка идёт ДО фильтрации, и фильтр ищет уже по подставленному
имени. Поэтому группа сервиса собирается из строк с меткой "<метка>-best",
а не из всего, что попало под подстроку "<метка>".

Поле "url" внутри блока — особая статья: там "alldeploy" заменяется
ссылкой цели из reachability_targets.json. Блок api не используется
никогда: это служебный адрес проверки, а в конфиг идёт обычная страница.

Служебный контейнер в собранный конфиг не попадает.
Правило маршрута, ведущее в отсутствующий outbound, перенаправляется
в proxy, а не удаляется.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

__all__ = [
    "TargetError",
    "load_targets",
    "service_tags",
    "has_alldeploy",
    "expand_alldeploy",
]

PLACEHOLDER = "alldeploy"
PLACEHOLDER_RESERVED = "alldeploy-reserved"
PLACEHOLDER_RESERVE = "alldeploy-reserve"
PLACEHOLDER_BEST = "alldeploy-best"

#: Имя служебного ключа в outbounds.
KEY = "alldeploy"

#: Значение поля tags, означающее "все метки файла целей".
ALL = "all"

#: Порядок подстановок: длинные плейсхолдеры первыми, иначе "alldeploy"
#: съест начало "alldeploy-reserve" и "alldeploy-best".
#:
#: Суффиксы РАЗНЫЕ, и это не опечатка:
#:   * "<метка>-best"     — метка в базе и имя группы;
#:   * "<метка>-reserve"  — метка в базе (best_tags.reserve_suffix);
#:   * "<метка>-reserved" — имя ГРУППЫ резерва.
#: Группа называется иначе намеренно: фильтр резерва ищет метку базы
#: "<метка>-reserve", и если бы группа звалась так же, поиск шёл бы
#: подстрокой и в резерв попадало бы лишнее. Проверяется тестом
#: test_service_template.
_SUBSTITUTIONS = (
    (PLACEHOLDER_RESERVED, "-reserved"),
    (PLACEHOLDER_RESERVE, "-reserve"),
    (PLACEHOLDER_BEST, "-best"),
    (PLACEHOLDER, ""),
)


class TargetError(ValueError):
    """Цели описаны так, что развернуть их в шаблоне нельзя."""


# --------------------------------------------------------------------------
# Цели
# --------------------------------------------------------------------------

def load_targets(path: str | Path) -> list[dict]:
    """Читает цели из reachability_targets.json.

    Возвращает список словарей name/tag/url. Блок api не используется
    никогда: это служебный адрес проверки, а в конфиг идёт обычная
    страница из поля url.
    """
    p = Path(path)
    if not p.exists():
        raise TargetError(f"файл целей не найден: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TargetError(f"{p.name}: битый JSON — {exc}") from exc

    targets = data.get("targets") or []
    if not isinstance(targets, list):
        raise TargetError("'targets' должен быть списком")

    # Для группы Global в merge указан список целей: берём ПЕРВУЮ ссылку.
    merge_urls: dict[str, str] = {}
    by_tag: dict[str, str] = {}
    for item in targets:
        if isinstance(item, dict):
            tag = item.get("tag") or item.get("name")
            url = item.get("url")
            if tag and url:
                by_tag[tag] = url
    for block in data.get("merge") or []:
        if not isinstance(block, dict):
            continue
        mtag = block.get("tag")
        if not mtag or not block.get("list"):
            continue
        first = block["list"][0]
        if first in by_tag:
            merge_urls[mtag] = by_tag[first]

    out: list[dict] = []
    for item in targets:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        tag = item.get("tag") or item.get("name")
        if not url or not tag:
            continue
        out.append({"name": item.get("name") or tag, "tag": tag, "url": url})
    for mtag, murl in merge_urls.items():
        if not any(t["tag"] == mtag for t in out):
            out.append({"name": mtag, "tag": mtag, "url": murl})
    return out


def service_tags(targets: list[dict], extra: list[str] | None = None) -> list[str]:
    """Все метки, которые вообще есть в данных."""
    seen: list[str] = []
    for t in targets:
        tag = t.get("tag")
        if tag and tag not in seen:
            seen.append(tag)
    for tag in extra or []:
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _url_for(tag: str, targets: list[dict]) -> str | None:
    """Ссылка проверки для метки.

    Точное совпадение по tag, затем по name. Метка браузерной проверки
    ("gemini-chat") своей цели в файле не имеет — она опирается на цель
    того же сервиса, у которого tag начинается с её имени ("gemini-api").
    """
    for t in targets:
        if t.get("tag") == tag:
            return t.get("url")
    for t in targets:
        if t.get("name") == tag:
            return t.get("url")
    # "gemini-chat" -> цель "gemini" / "gemini-api"
    base = tag.split("-")[0]
    for t in targets:
        cand = str(t.get("tag") or "")
        if cand == base or cand.startswith(base + "-"):
            return t.get("url")
    return None


# --------------------------------------------------------------------------
# Поиск служебного ключа
# --------------------------------------------------------------------------

def _is_key(ob: Any) -> bool:
    """Служебный контейнер, а не настоящий outbound."""
    return (isinstance(ob, dict)
            and ob.get("type") == "block"
            and isinstance(ob.get("blocks"), list))


def has_alldeploy(template: dict) -> bool:
    """Есть ли в шаблоне служебный ключ alldeploy."""
    for ob in template.get("outbounds") or []:
        if isinstance(ob, dict) and ob.get("tag") == KEY and _is_key(ob):
            return True
    return False


# --------------------------------------------------------------------------
# Подстановка
# --------------------------------------------------------------------------

def _substitute(value: str, tag: str) -> str:
    """Заменяет плейсхолдеры на метку сервиса.

    Метка подставляется как есть, со своим регистром из
    reachability_targets.json: "gemini-api" остаётся "gemini-api".
    """
    text = value
    for needle, suffix in _SUBSTITUTIONS:
        text = text.replace(needle, tag + suffix)
    return text


def _substitute_deep(value: Any, tag: str) -> Any:
    if isinstance(value, str):
        return _substitute(value, tag)
    if isinstance(value, list):
        return [_substitute_deep(v, tag) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_deep(v, tag) for k, v in value.items()}
    return value


def _expand_block(block: dict, tag: str, url: str) -> dict:
    """Разворачивает один блок контейнера в блок конкретного сервиса.

    Подстановка идёт по всему блоку целиком, включая filter: имя меняется
    ПЕРВЫМ, и фильтр ищет уже по подставленному имени. Именно поэтому в
    группу попадают строки с меткой "<метка>-best", а не всё, что попалось
    под подстроку "<метка>".

    Поле url — исключение: плейсхолдер в нём означает ссылку цели, а не
    метку сервиса.
    """
    out = _substitute_deep(block, tag)
    raw_url = block.get("url")
    if isinstance(raw_url, str) and PLACEHOLDER in raw_url:
        out["url"] = _substitute(raw_url, tag).replace(tag, url)
    return out


def _outbound_tags(rule: dict) -> list[str]:
    out = rule.get("outbound")
    if isinstance(out, str):
        return [out]
    if isinstance(out, list):
        return [o for o in out if isinstance(o, str)]
    return []


def _set_outbound(rule: dict, value: str) -> None:
    """Меняет outbound правила, сохраняя тип (строка/список)."""
    if isinstance(rule.get("outbound"), list):
        rule["outbound"] = [value]
    else:
        rule["outbound"] = value


# --------------------------------------------------------------------------
# Развёртывание
# --------------------------------------------------------------------------

def expand_alldeploy(template: dict, targets: list[dict],
                     extra_tags: list[str] | None = None,
                     fallback: str = "proxy") -> tuple[dict, dict]:
    """Разворачивает служебный ключ alldeploy на метки сервисов.

    Возвращает (config, report).
    report:
      "expanded" — метки, на которые блоки развёрнуты;
      "repointed" — правила маршрута, перенаправленные в fallback.
    """
    config = deepcopy(template)
    outbounds = config.get("outbounds") or []

    if not has_alldeploy(config):
        return config, {"expanded": [], "repointed": []}

    available = service_tags(targets, extra_tags)
    known_urls = {t.get("tag"): t.get("url") for t in targets}

    new_outbounds: list[Any] = []
    expanded: list[str] = []
    missing: list[str] = []

    for ob in outbounds:
        if not _is_key(ob):
            new_outbounds.append(ob)
            continue

        wanted = ob.get("tags")
        if not isinstance(wanted, list) or not wanted:
            wanted = [ALL]
        if ALL in wanted:
            tags = list(available)
        else:
            # Явный список: только эти метки и только в этом порядке.
            tags = [str(t) for t in wanted if str(t)]
            for tag in tags:
                if tag not in available:
                    missing.append(tag)

        for tag in tags:
            url = _url_for(tag, targets)
            if not url:
                # Метка без своей цели не разворачивается: url группы
                # остался бы плейсхолдером, и sing-box не поднялся бы.
                missing.append(tag)
                continue
            for block in ob.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                new_outbounds.append(_expand_block(block, tag, url))
            expanded.append(tag)

    config["outbounds"] = new_outbounds
    known = {str(o.get("tag")) for o in new_outbounds
             if isinstance(o, dict) and o.get("tag")}

    # Правило ведёт в ГРУППУ, которой нет среди собранных, — направляем его
    # в fallback. Так же поступаем с существующей, но ПУСТОЙ группой: она
    # собралась, потому что метку сервиса не набрал ни один сервер, и
    # оставлять правило в ней значит отдать трафик в никуда.
    #
    # Трогаем ТОЛЬКО группы: direct, block и сами узлы-прокси служебные.
    # Правило ip_is_private -> direct обязано остаться в direct, иначе
    # локальный трафик уходит в туннель.
    #
    # Группу при этом НЕ удаляем: клиент видит её в конфиге и понимает,
    # какой сервис не набрал серверов.
    groups = {str(o.get("tag")): o for o in new_outbounds
              if isinstance(o, dict) and o.get("type") in ("urltest", "selector")}

    repointed: list[dict] = []
    route = config.get("route") or {}
    for rule in route.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        for out in _outbound_tags(rule):
            if out not in groups:
                continue
            if groups[out].get("outbounds"):
                continue
            if fallback in groups and fallback != out:
                _set_outbound(rule, fallback)
                repointed.append({"from": out, "to": fallback})
                break

    final = route.get("final")
    if isinstance(final, str) and final not in known and fallback in known:
        route["final"] = fallback
        repointed.append({"from": final, "to": fallback})
    config["route"] = route

    return config, {
        "expanded": sorted(set(expanded)),
        "missing": sorted(set(missing)),
        "repointed": repointed,
    }
