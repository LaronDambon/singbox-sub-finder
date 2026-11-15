# -*- coding: utf-8 -*-
"""Справочник автономных систем, который расширяется сам.

Задача: по номеру AS сказать, датацентр это (dc), домашний провайдер (res)
или мобильный оператор (mobile) — чтобы отбирать «чистые» IP для Google и
Claude.

Готовых бесплатных справочников с таким признаком нет:
  * ipapi.is отдаёт is_datacenter только в платном тарифе;
  * ipinfo.io company.type (hosting/isp) — платно;
  * PeeringDB info_type классифицирует по роли в пиринге, а не по типу
    бизнеса: у MegaFon (домашний ISP) и у Vultr (хостинг) там одинаковый
    NSP. Единственное осмысленное значение — Cable/DSL/ISP, это правда
    абонентская сеть.

Поэтому основной механизм — НАБЛЮДЕНИЯ. Каждая проверка ip_info приносит
от ip-api.com флаги hosting/mobile для конкретного IP; они копятся в
таблице asn_info голосами по номеру AS. Набралось достаточно — провайдер
получает устойчивый kind, и дальше все его серверы классифицируются без
единого запроса.

PeeringDB используется как вспомогательный источник: имя провайдера, когда
ip-api его не отдал, и слабый признак res для Cable/DSL/ISP.
"""

from __future__ import annotations

import threading

import requests

from config.settings import get_settings
from script.logger_utils import get_project_logger

LOGGER = get_project_logger("asn_registry")

PEERINGDB_URL = "https://www.peeringdb.com/api/net?asn={asn}"
PEERINGDB_TIMEOUT = 8.0

#: info_type PeeringDB, который действительно означает абонентскую сеть.
_RES_INFO_TYPES = {"Cable/DSL/ISP"}

#: Сколько незнакомых ASN опрашивать за прогон. PeeringDB ограничивает
#: частоту, а прогон по 200 тысяч серверов легко даёт сотни новых ASN.
REMOTE_LOOKUP_LIMIT = 200

_lock = threading.Lock()
_cache: dict[str, dict] | None = None
_remote_tried: set[str] = set()
_store = None


def _get_store():
    global _store
    if _store is None:
        from script.server_store import ServerStore

        _store = ServerStore(get_settings().paths.servers_db_file)
    return _store


def _ensure_loaded() -> dict[str, dict]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                try:
                    _cache = _get_store().asn_known()
                except Exception:  # noqa: BLE001 — без справочника работаем по встроенному списку
                    _cache = {}
    return _cache


def kind_of(asn: str) -> str:
    """Готовый вердикт по ASN из справочника ("" если ещё не знаем)."""
    key = str(asn or "").strip()
    if not key:
        return ""
    return str((_ensure_loaded().get(key) or {}).get("kind") or "")


def name_of(asn: str) -> str:
    key = str(asn or "").strip()
    if not key:
        return ""
    return str((_ensure_loaded().get(key) or {}).get("name") or "")


def _builtin_hosting(asn: str) -> bool:
    """Номер AS во встроенном списке хостингов (script/ip_info_check)."""
    try:
        from script.ip_info_check import hosting_asns

        return asn in hosting_asns()
    except Exception:  # noqa: BLE001 — без списка решает справочник
        return False


def _lookup_peeringdb(asn: str) -> dict:
    """Имя провайдера и слабый признак типа из PeeringDB.

    Возвращает {"name": str, "kind": str}; kind заполняется только для
    Cable/DSL/ISP — остальные info_type про тип бизнеса ничего не говорят.
    """
    out = {"name": "", "kind": ""}
    try:
        resp = requests.get(
            PEERINGDB_URL.format(asn=asn),
            timeout=PEERINGDB_TIMEOUT,
            headers={"User-Agent": "singbox-subs-finder/1.0"},
        )
        if resp.status_code != 200:
            return out
        data = resp.json().get("data") or []
        if not data:
            return out
        net = data[0]
        out["name"] = str(net.get("name") or net.get("aka") or "").strip()
        info_types = net.get("info_types") or [net.get("info_type") or ""]
        if any(str(t).strip() in _RES_INFO_TYPES for t in info_types):
            out["kind"] = "res"
    except Exception as exc:  # noqa: BLE001 — сеть не должна ронять проверку
        LOGGER.debug("PeeringDB: AS%s не опрошен: %s", asn, exc)
    return out


def resolve(asn: str, *, hosting: bool = False, mobile: bool = False,
            known: bool = False, allow_remote: bool = True) -> str:
    """Итоговый kind для конкретного IP: dc | res | mobile | "".

    Порядок:
      1. hosting/proxy от ip-api.com — самый точный сигнал по этому IP;
      2. устойчивый kind ASN из справочника (выученный или встроенный);
      3. если ASN незнаком — один запрос в PeeringDB и запись результата;
      4. если источник умеет отвечать про hosting и сказал «нет» — res.
    """
    key = str(asn or "").strip()
    if hosting:
        return "dc"
    if key:
        learned = kind_of(key)
        if learned:
            return learned
        if _builtin_hosting(key):
            return "dc"
        # Повторно не спрашиваем: если запись про ASN уже есть в справочнике
        # (даже с пустым kind), значит его уже опрашивали в прошлых прогонах.
        if allow_remote and key.isdigit() and key not in _remote_tried:
            with _lock:
                first = key not in _remote_tried
                _remote_tried.add(key)
                too_many = len(_remote_tried) > REMOTE_LOOKUP_LIMIT
            if key in _ensure_loaded():
                first = False
            if too_many:
                first = False
            if first:
                remote = _lookup_peeringdb(key)
                if remote.get("name") or remote.get("kind"):
                    try:
                        _get_store().asn_set(
                            key, name=remote.get("name", ""),
                            kind=remote.get("kind", ""), source="peeringdb",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    with _lock:
                        _ensure_loaded()[key] = {
                            "name": remote.get("name", ""),
                            "kind": remote.get("kind", ""),
                            "source": "peeringdb",
                        }
                if remote.get("kind"):
                    return remote["kind"]
    if mobile:
        return "mobile"
    if known:
        return "res"
    return ""


def observe(infos) -> int:
    """Записывает наблюдения по ASN и обновляет кэш.

    infos — список словарей результата ip_info (asn, isp/asn_name,
    hosting, mobile, hosting_known).
    """
    rows = []
    for info in infos or []:
        asn = str(info.get("asn") or "").strip()
        if not asn.isdigit():
            continue
        rows.append({
            "asn": asn,
            "name": str(info.get("isp") or info.get("asn_name") or "").strip(),
            "hosting": bool(info.get("hosting") or info.get("proxy")),
            "mobile": bool(info.get("mobile")),
            "known": bool(info.get("hosting_known")),
        })
    if not rows:
        return 0
    try:
        cfg = get_settings().ip_info
        changed = _get_store().asn_observe(
            rows,
            min_votes=int(getattr(cfg, "learn_min_votes", 3) or 3),
            ratio=float(getattr(cfg, "learn_ratio", 0.8) or 0.8),
        )
    except Exception as exc:  # noqa: BLE001 — обучение не должно ронять проверку
        LOGGER.debug("ASN: наблюдения не записаны: %s", exc)
        return 0
    if changed:
        with _lock:
            global _cache
            _cache = None
        LOGGER.info("ASN: вердикт обновлён у %d провайдеров", changed)
    return changed
