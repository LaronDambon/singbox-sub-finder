# -*- coding: utf-8 -*-
"""Глубокие данные о сервере: ISP, город, регион, признак датацентра.

Зачем: Google и Claude не любят подключения с серверных хостингов. Чтобы
отличить «домашний» IP от датацентра, нужны ISP и признак hosting — их
отдаёт ip-api.com. 2ip.io для этого не годится: страница рендерится
JavaScript'ом и по обычному HTTP приходит пустая оболочка (503 + loader).

Реализация повторяет country_check: ОДИН процесс sing-box на батч, по
mixed-inbound на каждый прокси, правило inbound_i -> outbound_i. Потоки
Python стучатся каждый в свой локальный порт.

Цепочка источников (первый ответивший побеждает):
  1. ip-api.com   — ISP, org, AS, город, регион, hosting/proxy/mobile;
  2. ipinfo.io    — org (AS + имя провайдера), город, регион;
  3. api.ip.sb    — isp, organization, город, регион.

Признак датацентра считается так: hosting=True от ip-api.com, либо ISP/org,
похожий на хостинг (OVH, Hetzner, DigitalOcean, ...). Второе — страховка на
случай, когда источник hosting не отдал.

CLI:
  python -m script.ip_info_check '<proxy_line>'
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent

from config.settings import get_settings

from script.logger_utils import get_project_logger
from script.core import release_ports, reserve_ports
from script.batch_singbox import (
    build_batch_config, normalize_key, parse_outbound,
    ports_ready, start_singbox, stop_singbox,
)

LOGGER = get_project_logger("ip_info_check")

PROBE_CONNECT_TIMEOUT = 4.0
STARTUP_WAIT_SECONDS = 10.0
MAX_BATCH_INBOUNDS = 100

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Поля ip-api.com. Без списка полей бесплатный тариф отдаёт всё, но лишний
#: трафик через прокси нам не нужен.
_IPAPI_FIELDS = (
    "status,message,query,country,countryCode,regionName,city,"
    "isp,org,as,hosting,proxy,mobile"
)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _clean(value) -> str:
    return str(value or "").strip()


def _parse_ipapi(resp: requests.Response) -> dict | None:
    data = resp.json()
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    asn, asn_name = normalize_asn(data.get("as"))
    return {
        "ip": _clean(data.get("query")),
        "isp": _clean(data.get("isp")),
        "org": _clean(data.get("org")),
        "asn": asn,
        "asn_name": asn_name,
        "city": _clean(data.get("city")),
        "region": _clean(data.get("regionName")),
        "country_code": _clean(data.get("countryCode")).upper(),
        "hosting": _as_bool(data.get("hosting")),
        "proxy": _as_bool(data.get("proxy")),
        "mobile": _as_bool(data.get("mobile")),
        # Только этот источник умеет отвечать про hosting. По остальным
        # "hosting=False" означает "не знаю", а не "домашний".
        "hosting_known": True,
    }


def _parse_ipinfo(resp: requests.Response) -> dict | None:
    data = resp.json()
    if not isinstance(data, dict) or data.get("bogon"):
        return None
    org = _clean(data.get("org"))
    # org у ipinfo выглядит как "AS31133 PJSC MegaFon".
    asn, asn_name = normalize_asn(org)
    return {
        "ip": _clean(data.get("ip")),
        "isp": asn_name or org,
        "org": org,
        "asn": asn,
        "asn_name": asn_name,
        "city": _clean(data.get("city")),
        "region": _clean(data.get("region")),
        "country_code": _clean(data.get("country")).upper(),
        "hosting": False,
        "proxy": False,
        "mobile": False,
        "hosting_known": False,
    }


def _parse_ip_sb(resp: requests.Response) -> dict | None:
    data = resp.json()
    if not isinstance(data, dict):
        return None
    asn, asn_name = normalize_asn(data.get("asn"))
    return {
        "ip": _clean(data.get("ip")),
        "isp": _clean(data.get("isp")),
        "org": _clean(data.get("organization")),
        "asn": asn,
        "asn_name": asn_name or _clean(data.get("organization")),
        "city": _clean(data.get("city")),
        "region": _clean(data.get("region")),
        "country_code": _clean(data.get("country_code")).upper(),
        "hosting": False,
        "proxy": False,
        "mobile": False,
        "hosting_known": False,
    }


IP_INFO_PROBES = [
    ("ip-api.com", f"http://ip-api.com/json/?fields={_IPAPI_FIELDS}", _parse_ipapi),
    ("ipinfo.io", "https://ipinfo.io/json", _parse_ipinfo),
    ("ip.sb", "https://api.ip.sb/geoip", _parse_ip_sb),
]


# --- ASN ---------------------------------------------------------------------
_ASN_RE = re.compile(r"\bAS(\d{1,10})\b", re.IGNORECASE)


def normalize_asn(value) -> tuple[str, str]:
    """'AS56630 Melbikomas UAB' -> ('56630', 'Melbikomas UAB').

    Источники отдают по-разному: ip-api — номер и имя одной строкой,
    ipinfo — то же в поле org, ip.sb — обычно только номер. Приводим всё
    к паре (номер, имя); номер идёт в тег, имя — в тег ISP.
    """
    text = _clean(value)
    if not text:
        return "", ""
    match = _ASN_RE.search(text)
    if not match:
        # Бывает, что пришёл голый номер без префикса AS.
        digits = re.fullmatch(r"(\d{1,10})", text)
        return (digits.group(1), "") if digits else ("", text)
    name = _ASN_RE.sub("", text).strip(" -–—,;")
    return match.group(1), name


# --- Признак датацентра ------------------------------------------------------
#: Номера автономных систем крупных хостингов. В отличие от названий, номер
#: не меняется от переименования компании: Vultr — это AS20473 и в 2019, и
#: сейчас, хотя юридически он "The Constant Company".
_HOSTING_ASN = frozenset({
    "16276",   # OVH
    "24940",   # Hetzner
    "14061",   # DigitalOcean
    "20473",   # Vultr / The Constant Company / Choopa
    "63949",   # Akamai / Linode
    "16509",   # Amazon AWS
    "14618",   # Amazon
    "396982",  # Google Cloud
    "15169",   # Google
    "8075",    # Microsoft
    "8068",    # Microsoft
    "31898",   # Oracle Cloud
    "51167",   # Contabo
    "12876",   # Scaleway / Online SAS
    "202053",  # UpCloud
    "60781",   # LeaseWeb NL
    "28753",   # LeaseWeb DE
    "9009",    # M247
    "199524",  # Gcore
    "13335",   # Cloudflare
    "20940",   # Akamai
    "49505",   # Selectel
    "200350",  # Yandex Cloud
    "47764",   # VK Cloud (Mail.ru)
    "9123",    # Timeweb
    "197695",  # Reg.ru
    "29182",   # ISPsystem / FirstVDS
    "210644",  # Aeza
    "62240",   # Clouvider
    "29802",   # Hivelocity
    "36352",   # ColoCrossing
    "46844",   # Sharktech
    "53667",   # FranTech / BuyVM
    "40676",   # Psychz
    "25820",   # IT7 Networks
    "26496",   # GoDaddy
    "3223",    # Voxility
    "60068",   # Datacamp / CDN77
    "212238",  # Datacamp
    "9002",    # RETN
    "197540",  # netcup
    "213230",  # Hetzner Cloud
    "44477",   # Stark Industries (RU hosting)
    "34872",   # Servers.com
    "51396",   # Pfcloud
})

_hosting_asn_cache: frozenset[str] | None = None


def hosting_asns() -> frozenset[str]:
    """Встроенный список хостинговых ASN + добавка из настроек."""
    global _hosting_asn_cache
    if _hosting_asn_cache is not None:
        return _hosting_asn_cache
    extra: set[str] = set()
    try:
        raw = str(get_settings().ip_info.extra_hosting_asns or "")
        for chunk in re.split(r"[,;\s]+", raw):
            digits = chunk.strip().lstrip("ASas")
            if digits.isdigit():
                extra.add(digits)
    except Exception:  # noqa: BLE001 — без добавки список всё равно рабочий
        pass
    _hosting_asn_cache = frozenset(set(_HOSTING_ASN) | extra)
    return _hosting_asn_cache


def _remote_enabled() -> bool:
    """Разрешено ли спрашивать внешний справочник про незнакомый ASN."""
    try:
        return bool(get_settings().ip_info.peeringdb)
    except Exception:  # noqa: BLE001
        return True


def hosting_kind(info: dict) -> str:
    """'dc' | 'res' | 'mobile' | '' — тип провайдера.

    Порядок сигналов:
      1. hosting/proxy от ip-api.com — их классификация конкретного IP;
      2. номер AS во встроенном списке хостинговых;
      3. справочник asn_info — он расширяется сам (script/asn_registry);
      4. только если источник УМЕЕТ отвечать про hosting и сказал "нет" —
         'res'. ipinfo.io и ip.sb про hosting не знают вовсе, поэтому по ним
         'res' не ставится: неизвестное не должно выглядеть проверенным.
    """
    if info.get("hosting") or info.get("proxy"):
        return "dc"
    from script.asn_registry import resolve

    return resolve(
        str(info.get("asn") or ""),
        hosting=False,
        mobile=bool(info.get("mobile")),
        known=bool(info.get("hosting_known")),
        allow_remote=_remote_enabled(),
    )


# --- Теги --------------------------------------------------------------------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_SLUG_DROP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Строка -> безопасный кусок тега: 'МегаФон' -> 'megafon'."""
    text = _clean(value).lower()
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    return _SLUG_DROP.sub("-", text).strip("-")[:40]


def build_tags(info: dict) -> list[str]:
    """Теги сервера по данным о провайдере.

    isp-<slug>    провайдер: isp-megafon, isp-ovh-sas
    city-<slug>   город: city-novosibirsk
    region-<slug> регион/область, если отличается от города
    as-<номер>    автономная система: as-56630, as-20473
    dc            датацентр (плохо для Google/Claude)
    res           не датацентр — ставится только если источник это знает
    mobile        мобильный оператор
    """
    tags: list[str] = []
    isp = slugify(info.get("isp") or info.get("asn_name") or info.get("org"))
    if isp:
        tags.append(f"isp-{isp}")
    city = slugify(info.get("city"))
    if city:
        tags.append(f"city-{city}")
    region = slugify(info.get("region"))
    if region and region != city:
        tags.append(f"region-{region}")
    asn = str(info.get("asn") or "").strip()
    if asn.isdigit():
        tags.append(f"as-{asn}")
    kind = hosting_kind(info)
    if kind:
        tags.append(kind)
    if info.get("mobile"):
        tags.append("mobile")
    return tags


# --- Кэш запуска -------------------------------------------------------------
_ipinfo_cache: dict[str, dict | None] = {}


def _probe_port(port: int) -> dict:
    """Опрашивает источники через локальный порт-прокси одного outbound'а."""
    result = {
        "ip": "", "isp": "", "org": "", "asn": "", "asn_name": "", "hosting_known": False, "city": "", "region": "",
        "country_code": "", "hosting": False, "proxy": False, "mobile": False,
        "source": "", "latency_ms": None, "tags": [], "error": None,
    }
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    session = requests.Session()
    session.trust_env = False
    last_error = "неизвестная ошибка"
    read_timeout = float(get_settings().ip_info.timeout or 10.0)

    for name, url, parser in IP_INFO_PROBES:
        start = time.monotonic()
        try:
            resp = session.get(
                url,
                proxies=proxies,
                timeout=(PROBE_CONNECT_TIMEOUT, read_timeout),
                headers={"User-Agent": BROWSER_UA},
            )
            if resp.status_code != 200:
                last_error = f"{name}: HTTP {resp.status_code}"
                continue
            info = parser(resp)
            if not info or not (info.get("isp") or info.get("org")):
                last_error = f"{name}: нет данных о провайдере"
                continue
            info["source"] = name
            info["latency_ms"] = round((time.monotonic() - start) * 1000.0, 1)
            info["tags"] = build_tags(info)
            info["error"] = None
            return info
        except Exception as exc:  # noqa: BLE001
            last_error = f"{name}: {exc}"
    result["error"] = last_error
    return result


def _run_batch(items, results, concurrency, depth=0):
    if not items:
        return

    used_tags: set[str] = set()
    entries: list[dict] = []
    for line, idx in items:
        outbound, err = parse_outbound(line, idx, used_tags)
        if err:
            results[line] = {
                "ip": "", "isp": "", "org": "", "asn": "", "asn_name": "", "hosting_known": False, "city": "", "region": "",
                "country_code": "", "hosting": False, "proxy": False, "mobile": False,
                "source": "", "latency_ms": None, "tags": [], "error": err,
            }
            continue
        entries.append({"index": idx, "port": 0, "outbound": outbound})

    active = list(entries)
    if not active:
        return

    ports = reserve_ports(len(active))
    for e, port in zip(active, ports):
        e["port"] = port

    config = build_batch_config(active, ROOT / str(get_settings().paths.countrytest_template))
    config_path = PROJECT_ROOT / "source" / "tests" / f"ipinfo_batch_{items[0][1]}_{len(items)}.json"
    proc = start_singbox(config, config_path)

    started_ok = False
    if proc is not None and proc.poll() is None:
        started_ok = ports_ready([e["port"] for e in active], STARTUP_WAIT_SECONDS)

    if not started_ok:
        stop_singbox(proc)
        release_ports(ports)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass
        if len(items) > 1 and depth < 8:
            mid = len(items) // 2
            LOGGER.warning(
                "Батч из %d прокси не стартовал, делю пополам (%d + %d)",
                len(items), mid, len(items) - mid,
            )
            _run_batch(items[:mid], results, concurrency, depth + 1)
            _run_batch(items[mid:], results, concurrency, depth + 1)
        else:
            for line, _ in items:
                if line not in results:
                    results[line] = {
                        "ip": "", "isp": "", "org": "", "asn": "", "asn_name": "", "hosting_known": False, "city": "",
                        "region": "", "country_code": "", "hosting": False,
                        "proxy": False, "mobile": False, "source": "",
                        "latency_ms": None, "tags": [],
                        "error": "sing-box не смог обработать этот конфиг",
                    }
        return

    try:
        port_by_index = {e["index"]: e["port"] for e in active}
        pairs = [(line, port_by_index[idx]) for line, idx in items if idx in port_by_index]
        workers = max(1, min(concurrency, len(pairs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_probe_port, port): line for line, port in pairs}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()

        # Наблюдения по ASN: провайдер, у которого ещё нет вердикта, копит
        # голоса. Набрал достаточно — дальше классифицируется без запросов.
        try:
            from script.asn_registry import observe

            observe([results[line] for line, _ in pairs if results.get(line)])
        except Exception:  # noqa: BLE001 — обучение не должно ронять батч
            LOGGER.debug("ASN: наблюдения батча не записаны", exc_info=True)
    finally:
        stop_singbox(proc)
        release_ports(ports)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass


def batch_ip_info(proxy_lines, *, concurrency=None, batch_size=MAX_BATCH_INBOUNDS):
    """Собирает ISP/город/признак датацентра для списка прокси.

    Возвращает {proxy_line: {ip, isp, org, asn, city, region, country_code,
    hosting, proxy, mobile, source, latency_ms, tags, error}}.
    """
    if concurrency is None:
        concurrency = get_settings().ip_info.concurrency
    concurrency = max(1, int(concurrency))

    results: dict[str, dict] = {}
    pending: list[str] = []
    for raw in proxy_lines:
        line = str(raw).strip()
        if not line:
            continue
        key = normalize_key(line)
        if key in _ipinfo_cache:
            cached = _ipinfo_cache[key]
            if cached is not None:
                results[line] = dict(cached)
            continue
        pending.append(line)

    if not pending:
        return results

    indexed = [(line, i) for i, line in enumerate(pending)]
    total_batches = (len(indexed) + batch_size - 1) // batch_size
    for bi, start in enumerate(range(0, len(indexed), batch_size)):
        chunk = indexed[start:start + batch_size]
        LOGGER.info(
            "IP-info batch %d/%d: %d прокси в одном процессе sing-box (concurrency=%d)",
            bi + 1, total_batches, len(chunk), concurrency,
        )
        batch_started = time.monotonic()
        _run_batch(chunk, results, concurrency)
        LOGGER.info(
            "IP-info batch %d/%d завершён за %.2fs",
            bi + 1, total_batches, time.monotonic() - batch_started,
        )

    for line in pending:
        res = results.get(line)
        if res and res.get("tags"):
            _ipinfo_cache[normalize_key(line)] = dict(res)

    return results


def check_ip_info(proxy_line: str) -> dict:
    """Совместимость: данные одного прокси (обёртка над батчем из 1)."""
    line = str(proxy_line).strip()
    res = batch_ip_info([line]).get(line)
    return res or {
        "ip": "", "isp": "", "org": "", "asn": "", "asn_name": "", "hosting_known": False, "city": "", "region": "",
        "country_code": "", "hosting": False, "proxy": False, "mobile": False,
        "source": "", "latency_ms": None, "tags": [], "error": "нет результата",
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Использование: python -m script.ip_info_check '<proxy_line>'")
        sys.exit(1)
    print(json.dumps(check_ip_info(sys.argv[1]), ensure_ascii=False, indent=2))
