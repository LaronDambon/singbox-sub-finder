"""Замер реальной скорости прокси и проверка доступа к целевому сайту.

Зачем это нужно рядом с проверкой доступности: ping бывает отличным, а
сервер при этом еле тянет данные. И обратная история — до Gemini пинг есть,
а сам сайт сервер не открывает (Google режет дата-центровые и VPN-адреса).

Что делает проба для одного прокси (локальный порт уже поднят sing-box):

  1. САЙТ. GET целевого сайта с браузерным User-Agent. Дёшево: у Gemini
     около 800 КБ. Смотрим status и признаки блокировки (403/429, редирект
     на вход, капча). Если сайт режет — сразу stop, дальше не идём.
  2. СКОРОСТЬ. Скачиваем размеро-контролируемый кусок данных и считаем
     МБ/с. Ограничиваем не размер, а ВРЕМЯ чтения: медленный сервер
     отдаёт своё число байт и отваливается по таймауту, не выкачивая
     мегабайты впустую. Это и делает проверку экономной.

Один процесс sing-box на весь батч, транспорт общий с проверкой
достижимости целей (script/proxy_probe.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import requests

from config.settings import setting
from script.logger_utils import get_project_logger
from script.proxy_probe import probe_lines
from script.speed_sources import describe as speed_describe
from script.speed_sources import measure as speed_measure
from script.speed_sources import resolve_sources

LOGGER = get_project_logger("speed_check")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Статусы, которыми сервер прямо говорит «тебе сюда нельзя».
BLOCKED_STATUS = frozenset({401, 402, 403, 407, 429, 451, 503})
# Признаки блокировки в теле ответа или в адресе редиректа.
BLOCKED_MARKERS = (
    "unusual traffic",
    "recaptcha",
    "/sorry/index",
    "accounts.google.com",
    "consent.google.com",
    "access denied",
    "are you a robot",
)
# ГЕО-блокировка: сервер жив и быстрый, но Google не работает в этой стране.
# Это не проблема пропускной способности, поэтому у неё своя метка.
GEO_BLOCKED_MARKERS = (
    "not available in your country",
    "isn't available in your country",
    "not available in your region",
    "isn’t available in your region",
    "unavailable in your country",
    "not supported in your country",
    "not available in your location",
    "country is not supported",
    "answer/13575153",
)


@dataclass(frozen=True)
class SpeedConfig:
    """Настройки замера. Все значения приходят из конфигурации."""

    site_url: str = "https://gemini.google.com/"

    # Источники замера. Пусто = все по умолчанию (Cloudflare, OVH, Google,
    # Microsoft, GitHub). Замер идёт по ним ОДНОВРЕМЕННО, итог — среднее.
    sources: tuple[str, ...] | str = ()

    # Объём на ОДИН источник. Пять источников по 2 МБ — это 10 МБ на сервер,
    # поэтому берём мало: замер ограничен по времени, и быстрый канал успеет
    # набрать своё за первую секунду.
    probe_bytes: int = 2 * 1024 * 1024
    upload_bytes: int = 1024 * 1024
    # Сколько источников должно ответить, чтобы замер вообще засчитали.
    min_sources: int = 2
    # Сайт — это дешёвый СИГНАЛ «открывается ли», а не источник байтов:
    # хватает первых килобайт и короткого таймаута. Отдельные часы от замера
    # скорости, иначе недоступный Gemini съедал бы время всей проверки.
    site_max_bytes: int = 256 * 1024
    site_timeout: float = 3.0
    read_seconds: float = 4.0
    connect_timeout: float = 6.0
    min_mbps: float = 1.0
    good_mbps: float = 5.0
    concurrency: int = 8
    batch_size: int = 40

def detect_geo_blocked(head_text: str, final_url: str = "") -> bool:
    """Отказал ли сайт ПО СТРАНЕ, а не по капче или коду ответа.

    Отдельная чистая функция ради тестов без сети. Такая страница обычно
    отдаёт 200 и читается как обычная, поэтому по коду ответа её не видно:
    признак — только текст «не работает в вашей стране» или переход на
    справку Gemini про доступность по странам.
    """
    haystack = f"{head_text}\n{final_url}".lower()
    return any(marker in haystack for marker in GEO_BLOCKED_MARKERS)


def detect_blocked(status: int | None, head_text: str, final_url: str = "") -> bool:
    """Решил ли сервер нас порезать.

    Отдельная чистая функция, чтобы правило можно было проверить без сети:
    Google закрывает дата-центровые и VPN-адреса либо кодом 403/429, либо
    редиректом на вход, либо капчей в теле ответа.
    """
    if status is None:
        return False
    if status in BLOCKED_STATUS:
        return True
    haystack = f"{head_text}\n{final_url}".lower()
    return any(marker in haystack for marker in BLOCKED_MARKERS)


def _short(exc: Exception) -> str:
    """Короткая причина без громоздкого трейсбека requests."""
    text = str(exc).strip().split(" (")[0]
    return f"{type(exc).__name__}: {text[:120]}"


def tier_of(
    mbps: float | None, cfg: SpeedConfig, blocked: bool, geo: bool = False
) -> str:
    """Итоговая категория сервера.

    Гео-блок важнее скорости: сервер может быть быстрым насколько угодно,
    но в Gemini из этой страны не пустят.
    """
    if geo:
        return "geo"
    if blocked:
        return "blocked"
    if mbps is None:
        return "unknown"
    if mbps >= cfg.good_mbps:
        return "fast"
    if mbps >= cfg.min_mbps:
        return "ok"
    return "slow"


def _drain(resp, *, max_bytes: int, max_seconds: float) -> tuple[int, float]:
    """Читает поток не дольше max_seconds и не больше max_bytes.

    Ограничение по времени — главный трюк экономии: медленный сервер
    отдаёт столько, сколько успевает, и уходит, не забирая лишнего.
    """
    started = time.monotonic()
    total = 0
    for chunk in resp.iter_content(65536):
        if not chunk:
            continue
        total += len(chunk)
        if total >= max_bytes or (time.monotonic() - started) >= max_seconds:
            break
    return total, time.monotonic() - started


def _drain_once(resp, *, max_bytes: int, max_seconds: float) -> tuple[bytes, int, float]:
    """Один проход по телу ответа.

    Возвращает (начало тела для разбора, всего байт, секунды). Раньше тело
    читалось дважды — сначала немного на проверку признаков блокировки, потом
    ещё раз на скорость, — и второй проход уже не получал ничего.
    """
    started = time.monotonic()
    head = b""
    total = 0
    for chunk in resp.iter_content(65536):
        if not chunk:
            continue
        if len(head) < 4096:
            head += chunk[: 4096 - len(head)]
        total += len(chunk)
        if total >= max_bytes or (time.monotonic() - started) >= max_seconds:
            break
    return head, total, time.monotonic() - started


def probe_server(port: int, cfg: SpeedConfig) -> dict:
    """Меряет один прокси через локальный порт. Возвращает плоский dict."""
    proxy = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    result: dict = {
        "error": None,
        "site_error": None,
        "speed_error": None,
        "blocked": False,
        "geo": False,
        "site_status": None,
        "site_mbps": None,
        "mbps": None,
        # Раздельно вниз и вверх: у прокси они могут отличаться вдвое,
        # а раньше замерялось только скачивание.
        "down_mbps": None,
        "up_mbps": None,
        "sources_ok": 0,
        "sources_total": 0,
        "sources_detail": None,
        "bytes": 0,
        "tier": "unknown",
    }
    session = requests.Session()
    session.trust_env = False  # игнорировать системные HTTP(S)_PROXY
    try:
        # --- 1. Целевой сайт: открывается ли вообще -----------------------
        site_ok = False
        try:
            started = time.monotonic()
            resp = session.get(
                cfg.site_url,
                proxies=proxy,
                headers={"User-Agent": BROWSER_UA,
                         "Accept": "text/html,application/xhtml+xml,*/*"},
                timeout=(cfg.connect_timeout, cfg.site_timeout),
                stream=True,
                allow_redirects=True,
            )
            result["site_status"] = resp.status_code
            head, body_bytes, body_seconds = _drain_once(
                resp, max_bytes=cfg.site_max_bytes, max_seconds=cfg.site_timeout
            )
            text = head.decode("utf-8", "ignore").lower()
            result["blocked"] = detect_blocked(
                resp.status_code, text, resp.url or ""
            )
            # «Не работает в вашей стране» — это не капча и не 403:
            # сервер жив, но в Gemini из этой страны не пустят.
            result["geo"] = detect_geo_blocked(text, resp.url or "")
            if body_bytes and body_seconds > 0:
                result["site_mbps"] = round(body_bytes / 1e6 / body_seconds, 2)
            site_ok = resp.status_code < 400 and not result["blocked"]
            resp.close()
            del started
        except Exception as exc:  # noqa: BLE001
            result["site_error"] = _short(exc)

        # --- 2. Скорость: несколько сайтов одновременно -------------------
        # Замер идёт ВСЕГДА, даже если сайт не открылся: недоступный Gemini
        # ничего не говорит о пропускной способности сервера. Пропускаем
        # только при явной блокировке — там прокси уже известно, что он режет.
        #
        # Все источники стартуют разом в своих потоках, поэтому замер идёт
        # столько же, сколько самый медленный из них, а не сумма всех.
        if not result["blocked"]:
            try:
                sample = speed_measure(
                    f"http://127.0.0.1:{port}",
                    sources=resolve_sources(cfg.sources),
                    seconds=cfg.read_seconds,
                    bytes_per_source=cfg.probe_bytes,
                    upload_bytes=cfg.upload_bytes,
                    connect_timeout=cfg.connect_timeout,
                    min_sources=cfg.min_sources,
                )
                result["down_mbps"] = (
                    round(sample.down_mbps, 2) if sample.down_mbps else None
                )
                result["up_mbps"] = (
                    round(sample.up_mbps, 2) if sample.up_mbps else None
                )
                result["bytes"] = sum(r.bytes for r in sample.sources)
                result["sources_ok"] = sample.ok_sources
                result["sources_total"] = len(sample.sources)
                result["sources_detail"] = speed_describe(sample)
                if sample.total_mbps:
                    result["mbps"] = round(sample.total_mbps, 2)
                else:
                    result["speed_error"] = (
                        f"ответило {sample.ok_sources} из "
                        f"{len(sample.sources)} источников, "
                        f"минимум {cfg.min_sources}"
                    )
            except Exception as exc:  # noqa: BLE001
                result["speed_error"] = _short(exc)

        # Ошибки держим РАЗДЕЛЬНО: «не открылся Gemini» и «не идёт
        # загрузка» — разные вещи, и раньше вторая молча терялась за первой.
        notes = [n for n in (result["site_error"], result["speed_error"]) if n]
        if not site_ok and not result["blocked"] and not result["site_status"]:
            notes.insert(0, "сайт не открылся")
        result["error"] = "; ".join(notes) or None
        result["tier"] = tier_of(
            result["mbps"], cfg, bool(result["blocked"]), bool(result["geo"])
        )
        return result
    finally:
        session.close()


def batch_speed_check(
    proxy_lines: list[str], *, cfg: SpeedConfig | None = None
) -> dict[str, dict]:
    """Замеряет скорость списка прокси. Возвращает {line: результат}."""
    cfg = cfg or SpeedConfig()
    if not proxy_lines:
        return {}
    LOGGER.info(
        "Speed: %d серверов, цель %s, потоков %d, чтение до %.1f с",
        len(proxy_lines), cfg.site_url, cfg.concurrency, cfg.read_seconds,
    )
    results = probe_lines(
        proxy_lines,
        probe=lambda port, line: probe_server(port, cfg),
        concurrency=cfg.concurrency,
        batch_size=cfg.batch_size,
        template_path=Path(str(setting("COUNTRYTEST_TEMPLATE"))),
        config_dir=Path(str(setting("SING_BOX_OUTPUT_DIR"))),
        tag_prefix="speed",
    )
    tiers: dict[str, int] = {}
    for payload in results.values():
        tiers[payload.get("tier", "unknown")] = tiers.get(payload.get("tier", "unknown"), 0) + 1
    if tiers:
        LOGGER.info("Speed: %s", ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    return results
