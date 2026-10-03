"""Параллельный замер скорости сервера по нескольким сайтам сразу.

Зачем несколько источников. Один замер почти ничего не значит: скорость до
одного CDN зависит от того, насколько близко сервер оказался именно к нему,
а не от пропускной способности самого сервера. Поэтому источники подобраны
в разных сетях (Cloudflare, OVH, Google, Microsoft, GitHub), и итог — среднее
по тем, кто ответил. Это устойчивее и к случайно быстрому, и к случайно
медленному каналу.

Скачивание и отдача меряются ОДНОВРЕМЕННО, каждая пара — в своём потоке:
пропускная способность канала одна и та же в обе стороны, но мерить их
по очереди значит удваивать время проверки без выигрыша.

Ограничение по времени, а не по размеру: медленный сервер отдаёт столько,
сколько успевает за отведённые секунды, и уходит, не забирая лишнего.

Здесь нет ничего от sing-box: модуль умеет только мерить через готовый
адрес прокси, поэтому проверяется обычными тестами без сети и без
поднятого прокси-сервера.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CHUNK = 65536



@dataclass(frozen=True)
class SpeedSource:
    """Один сайт-источник замера.

    bytes_query — источник сам умеет отдать ровно нужный объём по параметру
    в адресе (так умеет Cloudflare). У остальных объём ограничивается на
    стороне клиента, потому что файлы там фиксированного размера.
    """

    key: str
    label: str
    download_url: str | None = None
    upload_url: str | None = None
    bytes_query: bool = False


#: Отобраны замером: отвечают, отдают мегабайты и не рвут соединение.
#: Hetzner и российские CDN отсюда вычеркнуты — Hetzner закрывает соединение,
#: yandex.ru и mail.ru отдают только HTML на десятки килобайт, которых для
#: замера скорости не хватает. Русский сегмент тут бесполезен.
DEFAULT_SOURCES: tuple[SpeedSource, ...] = (
    SpeedSource("cloudflare", "Cloudflare",
                "https://speed.cloudflare.com/__down",
                "https://speed.cloudflare.com/__up", bytes_query=True),
    SpeedSource("ovh", "OVH", "https://proof.ovh.net/files/10Mb.dat"),
    SpeedSource("google", "Google",
                "https://dl.google.com/linux/direct/"
                "google-chrome-stable_current_amd64.deb"),
    SpeedSource("microsoft", "Microsoft",
                "https://aka.ms/vs/17/release/vc_redist.x64.exe"),
    SpeedSource("github", "GitHub",
                "https://github.com/git-lfs/git-lfs/releases/download/"
                "v3.5.1/git-lfs-windows-amd64-v3.5.1.zip"),
)

BY_KEY: dict[str, SpeedSource] = {s.key: s for s in DEFAULT_SOURCES}


def resolve_sources(keys) -> tuple[SpeedSource, ...]:
    """Собирает источники по ключам. Пустой список — все по умолчанию.

    Неизвестный ключ — понятная ошибка, а не молчаливый пропуск: иначе опечатка
    в конфиге тихо оставила бы сервер с одним источником вместо пяти.
    """
    if not keys:
        return DEFAULT_SOURCES
    if isinstance(keys, str):
        keys = [part.strip() for part in keys.split(",") if part.strip()]
    picked: list[SpeedSource] = []
    unknown: list[str] = []
    for key in keys:
        source = BY_KEY.get(str(key).strip().lower())
        if source is None:
            unknown.append(str(key))
        elif source not in picked:
            picked.append(source)
    if unknown:
        raise ValueError(
            "Неизвестные источники замера скорости: "
            + ", ".join(sorted(unknown))
            + ". Доступны: " + ", ".join(sorted(BY_KEY))
        )
    if not picked:
        raise ValueError("Не выбран ни один источник замера скорости")
    return tuple(picked)


def download_url(source: SpeedSource, nbytes: int) -> str:
    """Адрес с явным объёмом для источников, которые объём понимают."""
    if not source.download_url:
        return ""
    if not source.bytes_query:
        return source.download_url
    sep = "&" if "?" in source.download_url else "?"
    return f"{source.download_url}{sep}bytes={int(nbytes)}"



@dataclass(frozen=True)
class SourceResult:
    """Что получилось от одного источника."""

    key: str
    direction: str          # "down" | "up"
    ok: bool
    bytes: int = 0
    seconds: float = 0.0
    mbps: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class SpeedSample:
    """Итог замера: среднее по ответившим источникам плюс детали."""

    down_mbps: float | None = None
    up_mbps: float | None = None
    total_mbps: float | None = None
    min_sources: int = 1
    sources: tuple[SourceResult, ...] = field(default_factory=tuple)

    @property
    def ok_sources(self) -> int:
        return sum(1 for s in self.sources if s.ok)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # не подхватывать системный HTTP(S)_PROXY
    return s


def _measure_download(source: SpeedSource, proxy: dict, *, nbytes: int,
                     seconds: float, connect_timeout: float) -> SourceResult:
    session = _session()
    started = time.monotonic()
    try:
        resp = session.get(
            download_url(source, nbytes),
            proxies=proxy,
            headers={"User-Agent": BROWSER_UA},
            timeout=(connect_timeout, connect_timeout),
            stream=True,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            resp.close()
            return SourceResult(source.key, "down", False,
                                error=f"HTTP {resp.status_code}")
        total = 0
        for chunk in resp.iter_content(CHUNK):
            if not chunk:
                continue
            total += len(chunk)
            if total >= nbytes or (time.monotonic() - started) >= seconds:
                break
        resp.close()
        elapsed = time.monotonic() - started
        if total <= 0 or elapsed <= 0:
            return SourceResult(source.key, "down", False, total, elapsed,
                                error="пустой ответ")
        return SourceResult(source.key, "down", True, total, elapsed,
                            mbps=total / 1e6 / elapsed)
    except Exception as exc:  # noqa: BLE001 — источник не должен ронять замер
        return SourceResult(source.key, "down", False,
                            error=f"{type(exc).__name__}: {str(exc)[:80]}")
    finally:
        session.close()



def _measure_upload(source: SpeedSource, proxy: dict, *, nbytes: int,
                    seconds: float, connect_timeout: float) -> SourceResult:
    """Отдача данных. Тело уходит генератором — это chunked, он держит поток.

    Останавливаемся по времени, а не по размеру: иначе быстрый канал зальёт
    лишние мегабайты, пока медленный ещё только начал.
    """
    session = _session()
    deadline = time.monotonic() + seconds
    sent = 0

    def _body():
        nonlocal sent
        blob = b"\0" * CHUNK
        while sent < nbytes and time.monotonic() < deadline:
            sent += len(blob)
            yield blob

    started = time.monotonic()
    try:
        resp = session.post(
            source.upload_url,
            data=_body(),
            proxies=proxy,
            headers={"User-Agent": BROWSER_UA,
                     "Content-Type": "application/octet-stream"},
            timeout=(connect_timeout,
                     max(connect_timeout, seconds + connect_timeout)),
        )
        resp.close()
        elapsed = time.monotonic() - started
        if resp.status_code >= 400 or sent <= 0 or elapsed <= 0:
            return SourceResult(source.key, "up", False, sent, elapsed,
                                error=f"HTTP {resp.status_code}")
        return SourceResult(source.key, "up", True, sent, elapsed,
                            mbps=sent / 1e6 / elapsed)
    except Exception as exc:  # noqa: BLE001
        return SourceResult(source.key, "up", False,
                            error=f"{type(exc).__name__}: {str(exc)[:80]}")
    finally:
        session.close()


def average(results, direction: str) -> tuple[float | None, int]:
    """Среднее по ответившим источникам нужного направления и их число.

    Отдельная чистая функция: правило усреднения проверяется без сети.
    Источник, который не ответил, в среднее не попадает — иначе один битый
    сайт ронял бы замер всего сервера.
    """
    values = [r.mbps for r in results
              if r.ok and r.direction == direction and r.mbps]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)

def measure(proxy_url: str, sources, *, seconds: float = 4.0,
            bytes_per_source: int = 2 * 1024 * 1024,
            upload_bytes: int = 1024 * 1024,
            connect_timeout: float = 8.0,
            min_sources: int = 2) -> SpeedSample:
    """Меряет скорость через прокси по указанному адресу, все источники сразу.

    Все источники стартуют одновременно в отдельных потоках: замер занимает
    столько же времени, сколько самый медленный источник, а не сумму всех.
    Итог — среднее по тем, кто ответил, отдельно для скачивания и отдачи.

    Если ответило меньше min_sources, замер не засчитывается (total_mbps
    остаётся None): одна удачная CDN не доказывает скорость сервера.
    """
    sources = tuple(sources)
    proxy = {"http": proxy_url, "https": proxy_url}

    tasks: list[tuple[SpeedSource, str]] = []
    for src in sources:
        if src.download_url:
            tasks.append((src, "down"))
        if src.upload_url:
            tasks.append((src, "up"))

    if not tasks:
        raise ValueError("Ни у одного источника нет адреса для замера")

    def _run(item):
        src, direction = item
        if direction == "down":
            return _measure_download(src, proxy, nbytes=bytes_per_source,
                                     seconds=seconds,
                                     connect_timeout=connect_timeout)
        return _measure_upload(src, proxy, nbytes=upload_bytes,
                               seconds=seconds, connect_timeout=connect_timeout)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(tasks),
                            thread_name_prefix="speed") as pool:
        results = list(pool.map(_run, tasks))
    elapsed = time.monotonic() - started

    down_avg, down_n = average(results, "down")
    up_avg, up_n = average(results, "up")

    # Итог сервера — среднее из уцелевших направлений. Односторонний замер
    # (например, отдача отвалилась у всех) тоже полезен, поэтому при одном
    # направлении итог равен именно ему.
    parts = [v for v in (down_avg, up_avg) if v]
    total = sum(parts) / len(parts) if parts else None

    if (down_n + up_n) < min_sources:
        total = None

    return SpeedSample(
        down_mbps=down_avg,
        up_mbps=up_avg,
        total_mbps=total,
        min_sources=min_sources,
        sources=tuple(results),
    )


def describe(sample: SpeedSample) -> str:
    """Человекочитаемая сводка замера — попадает в лог и в разбор сбоя."""
    parts: list[str] = []
    for r in sample.sources:
        if r.ok:
            parts.append(f"{r.direction}:{r.key}={r.mbps:.2f}")
        else:
            parts.append(f"{r.direction}:{r.key}=ошибка")
    head = " | ".join(parts)
    if sample.total_mbps is None:
        return f"итог не засчитан ({sample.ok_sources} ответило): {head}"
    return (
        f"итог {sample.total_mbps:.2f} МБ/с "
        f"(вниз {sample.down_mbps}, вверх {sample.up_mbps}) из {head}"
    )
