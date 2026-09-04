import argparse
import base64
import email.utils
import hashlib
import html
import json
import logging
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from script.logger_utils import get_project_logger
from utils import tool

PROTOCOL_PREFIXES = (
    "vmess://", "vless://", "trojan://", "ss://", "ssr://",
    "tuic://", "hysteria://", "hysteria2://", "hy2://",
    "socks5://", "socks4://", "wireguard://", "ssh://",
    "snell://", "brook://", "juicity://",
)

INSECURE_PATTERN = re.compile(
    r'(?:[?&;]|3%[Bb])(allowinsecure|allow_insecure|insecure)=(?:1|true|yes)(?:[&;#]|$|(?=\s|$))',
    re.IGNORECASE,
)

# Значения finger-print, при которых sing-box отвергает весь батч конфигов.
# В Xray/v2rayN 'unsafe' отключает проверку отпечатка, но в sing-box uTLS
# принимает только конкретные имена ('chrome', 'firefox', ...). 'unsafe' не
# валиден -> sing-box падает с ошибкой и выбрасывает ВСЕ конфиги в батче,
# включая рабочие. Такие серверы вырезаем на этапе скачивания/объединения.
FP_UNSAFE_PATTERN = re.compile(
    r'(?:^|[?&;])(?:fp|fingerprint)=(?:unsafe|none|disabled)(?:[&;#]|[?#]|$)',
    re.IGNORECASE,
)


def has_unsafe_fingerprint(line: str) -> bool:
    """True, если конфиг задаёт TLS-отпечаток 'unsafe'/'none'/'disabled'.

    Проверяет query-параметры (fp, fingerprint) в URI, включая URL-кодировку,
    и поле 'fp'/'client-fingerprint' внутри base64-конфига vmess://.
    """
    decoded = urllib.parse.unquote(html.unescape(line))

    # 1) Query-параметр fp / fingerprint с unsafe-значением.
    if FP_UNSAFE_PATTERN.search(decoded):
        return True

    # 2) vmess:// — fingerprint лежит внутри base64-закодированного JSON.
    if decoded.lower().startswith("vmess://"):
        try:
            encoded = decoded[8:].split("#", 1)[0].split("?", 1)[0]
            rem = len(encoded) % 4
            if rem:
                encoded += "=" * (4 - rem)
            payload = base64.b64decode(encoded, validate=False).decode("utf-8", errors="ignore")
            obj = json.loads(payload or "{}")
            if isinstance(obj, dict):
                for key in ("fp", "fingerprint", "client-fingerprint", "client_fingerprint"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip().lower() in {"unsafe", "none", "disabled"}:
                        return True
        except Exception:
            pass

    return False


def fetch_text(url: str, timeout: int = 15, retries: int = 3) -> str:
    """Скачивает текст по URL с небольшими повторами."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                continue
    raise RuntimeError(f"Не удалось скачать {url}: {last_error}")


def parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except Exception:
        return None


def metadata_path_for(local_path: Path) -> Path:
    return local_path.with_name(f"{local_path.name}.meta.json")


def read_cache_metadata(local_path: Path) -> dict:
    meta_file = metadata_path_for(local_path)
    if not meta_file.exists():
        return {}
    try:
        with meta_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def write_cache_metadata(local_path: Path, etag: str | None, last_modified: str | None) -> None:
    meta_file = metadata_path_for(local_path)
    payload = {
        "etag": etag,
        "last_modified": last_modified,
    }
    with meta_file.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def should_download(url: str, local_path: Path) -> bool:
    """Возвращает True, если удалённый файл новее локального кеша или он отсутствует."""
    if not local_path.exists():
        return True

    try:
        request = urllib.request.Request(
            url,
            method="HEAD",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
            remote_last_modified = parse_http_date(response.headers.get("Last-Modified"))
            remote_etag = response.headers.get("ETag")
            local_mtime = local_path.stat().st_mtime

            cached_meta = read_cache_metadata(local_path)
            cached_etag = cached_meta.get("etag")
            cached_last_modified = parse_http_date(cached_meta.get("last_modified"))

            if remote_etag and cached_etag == remote_etag:
                return False

            if remote_last_modified is not None:
                if cached_last_modified is not None and remote_last_modified <= cached_last_modified:
                    return False
                if remote_last_modified <= local_mtime:
                    return False

            return True
    except Exception:
        return True


def try_decode_base64(data: str) -> str:
    """Если это base64-список конфигов, раскодирует его."""
    if "://" in data:
        return data

    text = "".join(data.split())
    try:
        pad = len(text) % 4
        if pad:
            text += "=" * (4 - pad)
        decoded = base64.b64decode(text, validate=False)
        decoded_text = decoded.decode("utf-8", errors="ignore")
        if any(prefix in decoded_text.lower() for prefix in PROTOCOL_PREFIXES):
            return decoded_text
    except Exception:
        pass
    return data


def build_clean_tag(raw_line: str, seq_counter: int | None = None, include_country: bool = False, country_tag: str | None = None) -> str:
    line = raw_line.strip()
    if not line:
        return line

    clean_url = line.split("#", 1)[0].strip()
    protocol = tool.get_protocol(clean_url) or "proxy"
    protocol = protocol.lower()

    # Страна определяется по имени/тэгу из исходной строки (быстро, без сети),
    # а не по IP через внешний API. Если передан явный country_tag (например,
    # определённый через скоростной тест), используем его.
    if include_country:
        country_tag = country_tag or tool.get_country_from_name(line)

    if seq_counter is not None:
        code = f"{seq_counter:06d}"
    else:
        key = normalize_proxy_key(clean_url)
        if not key:
            key = clean_url.lower()
        code = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]

    if country_tag:
        new_tag = f"{country_tag} {protocol} {code}"
    else:
        new_tag = f"{protocol} {code}"

    return f"{clean_url}#{new_tag}"


def filter_insecure_configs(data: str) -> str:
    """Удаляет строки с insecure параметры и оставляет корректные конфиги."""
    data = try_decode_base64(data)

    pattern = "|".join(prefix.replace("://", "") for prefix in PROTOCOL_PREFIXES)
    data = re.sub(rf"({pattern})://", r"\n\1://", data, flags=re.IGNORECASE)

    result: list[str] = []
    for line in data.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if not clean.lower().startswith(PROTOCOL_PREFIXES):
            continue
        decoded = urllib.parse.unquote(html.unescape(clean))
        if INSECURE_PATTERN.search(decoded) or has_unsafe_fingerprint(decoded):
            continue
        result.append(clean)
    return "\n".join(result)


def load_urls(path: Path) -> list[str]:
    """Читает JSON список URL из файла: dict {"1": "..."} или list ["..."]"""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict):
        return [data[str(key)] for key in sorted(data, key=lambda x: int(x))]
    if isinstance(data, list):
        return [str(item) for item in data]
    raise ValueError(f"Неверный формат JSON в {path}")


def save_partial_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def normalize_proxy_key(raw: str) -> str:
    """Возвращает стабильный ключ для сравнения прокси без учёта имени/тэга."""
    value = raw.strip()
    if not value:
        return ""

    lower = value.lower()
    if lower.startswith("vmess://"):
        try:
            encoded = value[8:]
            rem = len(encoded) % 4
            if rem:
                encoded += "=" * (4 - rem)
            payload = base64.b64decode(encoded, validate=False)
            obj = json.loads(payload.decode("utf-8", errors="ignore") or "{}")
            if isinstance(obj, dict):
                cleaned = {k: v for k, v in obj.items() if k not in {"ps", "name", "remark", "label", "fp"}}
                return "vmess:" + json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            pass
        return value.split("#", 1)[0].lower()

    if any(lower.startswith(prefix) for prefix in PROTOCOL_PREFIXES):
        try:
            parsed = urllib.parse.urlsplit(value)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            filtered = []
            for key, val in query:
                if key.lower() in {"remarks", "remark", "name", "label", "tag", "n", "fp"}:
                    continue
                filtered.append((key, val))
            cleaned_query = urllib.parse.urlencode(filtered)
            normalized = urllib.parse.urlunsplit(parsed._replace(query=cleaned_query, fragment=""))
            return normalized.lower()
        except ValueError:
            # Для нестандартных строк с IPv6/другими неочевидными формами URL
            # достаточно безопасно нормализовать строку вручную.
            without_fragment = value.split("#", 1)[0]
            return re.sub(r"(?i)([?&;](?:remarks|remark|name|label|tag|n|fp)=)[^&;#]*", "", without_fragment).lower()

    return value.lower()


def write_merge_from_pool(store, output_path: Path) -> int:
    """Пишет merge.txt из пула проверки центральной базы.

    Пул = все серверы с excluded=0 (stable < PURGE_STABLE_BELOW уже исключены
    из базы проверки). Лучшие серверы (высокий stable) идут первыми.
    """
    pool = store.check_pool()
    # Дополнительно вырезаем серверы с insecure-fingerprint (fp=unsafe и т.п.),
    # которые могли попасть в базу из старых кешей/до этого фикса.
    merged = [
        row["line"] for row in pool
        if row["line"] and not has_unsafe_fingerprint(row["line"])
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(merged), encoding="utf-8")
    return len(merged)


def _download_source(idx: int, url: str, download_dir: Path) -> dict:
    """Скачивает один источник (или пропускает по кэшу). Потокобезопасно.

    Возвращает {idx, path, ok, cached, count, error} — логирование в главном потоке.
    """
    file_path = download_dir / f"{idx}.txt"
    if not should_download(url, file_path):
        return {"idx": idx, "path": file_path, "ok": True, "cached": True,
                "count": None, "error": None}
    try:
        raw = fetch_text(url)
        filtered = filter_insecure_configs(raw)
        save_partial_file(file_path, filtered)
        try:
            request = urllib.request.Request(
                url,
                method="HEAD",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
                write_cache_metadata(file_path, response.headers.get("ETag"), response.headers.get("Last-Modified"))
        except Exception:
            pass
        return {"idx": idx, "path": file_path, "ok": True, "cached": False,
                "count": len(filtered.splitlines()), "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"idx": idx, "path": file_path, "ok": False, "cached": False,
                "count": None, "error": exc}


def _download_sources_parallel(urls: list[str], download_dir: Path,
                               *, logger) -> list[Path]:
    """Параллельно скачивает все источники; возвращает список существующих файлов."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from config.settings import SUB_DOWNLOAD_CONCURRENCY

    results: list[dict] = []
    workers = max(1, min(int(SUB_DOWNLOAD_CONCURRENCY), len(urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_source, idx, url, download_dir): idx
            for idx, url in enumerate(urls, start=1)
        }
        for fut in as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r["idx"])

    partial_files: list[Path] = []
    total = len(urls)
    for r in results:
        idx, url_idx = r["idx"], r["idx"]
        url = urls[url_idx - 1]
        if r["cached"]:
            logger.info("[%d/%d] Пропуск: %s (локальный файл актуален)", idx, total, url)
            partial_files.append(r["path"])
        elif r["ok"]:
            logger.info("[%d/%d] Успешно: %s -> %d конфигов", idx, total, url, r["count"])
            partial_files.append(r["path"])
        else:
            logger.error("[%d/%d] Ошибка загрузки %s: %s", idx, total, url, r["error"])
    return partial_files


def build_merge_from_urls(
    urls_file: Path | str,
    output_path: Path | str,
    log_file: Path | str | None = None,
    *,
    logger: logging.Logger | None = None,
) -> dict[str, object]:
    """Скачивает подписки, регистрирует серверы в центральной базе (servers.db)
    и собирает merge.txt из пула проверки базы. Списки экспортируются из базы
    по фильтру stable (whitelist.txt = stable > порога)."""
    urls_file_path = Path(urls_file).resolve()
    output_path_path = Path(output_path).resolve()
    log_file_path = Path(log_file).resolve() if log_file else None

    if logger is None:
        # Единый логгер проекта; файловые обработчики уже настроены в setup_project_logging.
        logger = get_project_logger("merge_configs")

    logger.info("Старт сборки merge.txt")
    logger.info("URLs file: %s", urls_file_path)
    logger.info("Output file: %s", output_path_path)
    if log_file_path:
        logger.info("Log file: %s", log_file_path)

    download_dir = output_path_path.parent / "downloaded"
    download_dir.mkdir(parents=True, exist_ok=True)

    try:
        urls = load_urls(urls_file_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось прочитать список URL из %s: %s", urls_file_path, exc)
        raise

    logger.info("Загружено %d URL-источников (параллельная загрузка)", len(urls))
    partial_files: list[Path] = _download_sources_parallel(urls, download_dir, logger=logger)

    if not partial_files:
        logger.warning("Не найдено ни одного валидного промежуточного файла для объединения")
        return {
            "output_path": str(output_path_path),
            "merged_count": 0,
            "partial_files": [str(path) for path in partial_files],
            "logger": logger,
        }

    # --- Центральная база: регистрируем всё скачанное и собираем пул проверки ---
    from config.settings import (
        SERVERS_DB_FILE,
        WHITELIST_FILE,
        WHITELIST_EXPORT_MIN_STABLE,
    )
    from script.server_store import ServerStore

    store = ServerStore(SERVERS_DB_FILE)

    source_lines: list[str] = []
    seen_keys: set[str] = set()
    for file_path in sorted(partial_files):
        if not file_path.exists():
            continue
        for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            clean = line.strip()
            if not clean:
                continue
            # Пропускаем серверы с insecure-fingerprint (fp=unsafe и т.п.) —
            # они ломают батч проверки, потому что sing-box их не принимает.
            if has_unsafe_fingerprint(clean):
                continue
            key = normalize_proxy_key(clean)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            source_lines.append(clean)

    upsert_res = store.upsert_lines(source_lines)
    logger.info(
        "База серверов: добавлено %d новых, обновлено %d известных (источников строк: %d)",
        upsert_res["added"], upsert_res["updated"], len(source_lines),
    )

    logger.info("Сборка %s из пула проверки базы", output_path_path)
    merged_count = write_merge_from_pool(store, output_path_path)

    # Экспорт подтверждённых серверов (stable > порога) в whitelist.txt —
    # файловая проекция базы для внешних потребителей.
    exported = store.export_to_file(WHITELIST_FILE, min_stable=WHITELIST_EXPORT_MIN_STABLE)
    logger.info(
        "Экспорт whitelist.txt из базы: %d серверов со stable > %d",
        exported, WHITELIST_EXPORT_MIN_STABLE,
    )
    db_stats = store.stats()
    logger.info(
        "Итог: пул проверки %d конфигов (база: %d всего, %d исключено) в %s",
        merged_count, db_stats["total"], db_stats["excluded"], output_path_path,
    )
    logger.info("Завершено успешно")
    return {
        "output_path": str(output_path_path),
        "merged_count": merged_count,
        "db": db_stats,
        "upsert": upsert_res,
        "whitelist_exported": exported,
        "partial_files": [str(path) for path in partial_files],
        "logger": logger,
    }