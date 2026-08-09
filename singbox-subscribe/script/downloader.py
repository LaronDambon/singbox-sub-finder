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


def build_clean_tag(raw_line: str, seq_counter: int | None = None, include_country: bool = False) -> str:
    line = raw_line.strip()
    if not line:
        return line

    clean_url = line.split("#", 1)[0].strip()
    protocol = tool.get_protocol(clean_url) or "proxy"
    protocol = protocol.lower()

    country_tag = tool.get_proxy_country_emoji(clean_url) if include_country else None

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
        if not INSECURE_PATTERN.search(decoded):
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


def setup_logging(log_file: Path) -> logging.Logger:
    """Настраивает консольный и файловый логгер."""
    logger = logging.getLogger("merge_configs")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


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
                cleaned = {k: v for k, v in obj.items() if k not in {"ps", "name", "remark", "label"}}
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
                if key.lower() in {"remarks", "remark", "name", "label", "tag", "n"}:
                    continue
                filtered.append((key, val))
            cleaned_query = urllib.parse.urlencode(filtered)
            normalized = urllib.parse.urlunsplit(parsed._replace(query=cleaned_query, fragment=""))
            return normalized.lower()
        except ValueError:
            # Для нестандартных строк с IPv6/другими неочевидными формами URL
            # достаточно безопасно нормализовать строку вручную.
            without_fragment = value.split("#", 1)[0]
            return re.sub(r"(?i)([?&;](?:remarks|remark|name|label|tag|n)=)[^&;#]*", "", without_fragment).lower()

    return value.lower()


def load_blacklist_keys(blacklist_path: Path) -> set[str]:
    blacklist_keys: set[str] = set()
    if not blacklist_path.exists():
        return blacklist_keys
    for line in blacklist_path.read_text(encoding="utf-8", errors="replace").splitlines():
        clean = line.strip()
        if not clean:
            continue
        key = normalize_proxy_key(clean)
        if key:
            blacklist_keys.add(key)
    return blacklist_keys


def dedupe_blacklist_file(blacklist_path: Path) -> None:
    """Убирает дубликаты серверов из blacklist.txt без учёта тэга."""
    if not blacklist_path.exists():
        return

    original_lines = blacklist_path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen_keys: set[str] = set()
    unique_lines: list[str] = []

    for line in original_lines:
        clean = line.strip()
        if not clean:
            continue
        key = normalize_proxy_key(clean)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        unique_lines.append(clean)

    if unique_lines != [line.strip() for line in original_lines if line.strip()]:
        blacklist_path.write_text("\n".join(unique_lines), encoding="utf-8")


def dedupe_whitelist_file(whitelist_path: Path) -> None:
    """Убирает дубликаты серверов из whitelist.txt без учёта тэга."""
    if not whitelist_path.exists():
        return

    original_lines = whitelist_path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen_keys: set[str] = set()
    unique_lines: list[str] = []

    for line in original_lines:
        clean = line.strip()
        if not clean:
            continue
        key = normalize_proxy_key(clean)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        unique_lines.append(clean)

    if unique_lines != [line.strip() for line in original_lines if line.strip()]:
        whitelist_path.write_text("\n".join(unique_lines), encoding="utf-8")


def merge_files(
    file_paths: list[Path],
    output_path: Path,
    blacklist_path: Path | None = None,
    extra_lines: list[str] | None = None,
) -> int:
    """Объединяет все файлы в итоговый merge.txt и удаляет дубликаты только на финальном этапе."""
    blacklist_keys = load_blacklist_keys(blacklist_path) if blacklist_path else set()
    seen: set[str] = set()
    merged: list[str] = []
    seq_counter = 1

    if extra_lines:
        for line in extra_lines:
            clean = str(line).strip()
            if not clean:
                continue
            key = normalize_proxy_key(clean)
            if not key or key in seen or key in blacklist_keys:
                continue
            seen.add(key)
            merged.append(build_clean_tag(clean, seq_counter=seq_counter))
            seq_counter += 1

    for file_path in sorted(file_paths):
        if not file_path.exists():
            continue
        for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            clean = line.strip()
            if not clean:
                continue
            key = normalize_proxy_key(clean)
            if not key or key in seen or key in blacklist_keys:
                continue
            seen.add(key)
            merged.append(build_clean_tag(clean, seq_counter=seq_counter))
            seq_counter += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(merged), encoding="utf-8")
    return len(merged)


def build_merge_from_urls(
    urls_file: Path | str,
    output_path: Path | str,
    log_file: Path | str | None = None,
    *,
    logger: logging.Logger | None = None,
) -> dict[str, object]:
    """Собирает merge.txt из URL-источников без TCP-проверки и whitelist/blacklist."""
    urls_file_path = Path(urls_file).resolve()
    output_path_path = Path(output_path).resolve()
    log_file_path = Path(log_file).resolve() if log_file else None

    if logger is None:
        logger = get_project_logger("merge_configs")
        if log_file_path is not None:
            merge_handler = logging.handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            merge_handler.setLevel(logging.INFO)
            merge_handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(merge_handler)

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

    logger.info("Загружено %d URL-источников", len(urls))
    partial_files: list[Path] = []

    for idx, url in enumerate(urls, start=1):
        file_path = download_dir / f"{idx}.txt"
        if not should_download(url, file_path):
            logger.info("[%d/%d] Пропуск: %s (локальный файл актуален)", idx, len(urls), url)
            partial_files.append(file_path)
            continue

        logger.info("[%d/%d] Загрузка: %s", idx, len(urls), url)
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
            partial_files.append(file_path)
            logger.info("[%d/%d] Успешно: %s -> %d конфигов", idx, len(urls), url, len(filtered.splitlines()))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%d/%d] Ошибка загрузки %s: %s", idx, len(urls), url, exc)

    if not partial_files:
        logger.warning("Не найдено ни одного валидного промежуточного файла для объединения")
        return {
            "output_path": str(output_path_path),
            "merged_count": 0,
            "partial_files": [str(path) for path in partial_files],
            "logger": logger,
        }

    blacklist_path = output_path_path.parent / "blacklist.txt"
    whitelist_path = output_path_path.parent / "whitelist.txt"
    dedupe_blacklist_file(blacklist_path)
    dedupe_whitelist_file(whitelist_path)

    merged_whitelist_path = output_path_path.parent / "merge_whitelist.txt"
    merged_whitelist: list[str] = []
    if whitelist_path.exists():
        merged_whitelist.extend(
            [line.strip() for line in whitelist_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        )

    logger.info("Начало объединения %d файлов в %s", len(partial_files), output_path_path)
    merged_count = merge_files(partial_files, output_path_path, blacklist_path=blacklist_path, extra_lines=merged_whitelist)

    if merged_whitelist:
        merged_whitelist_path.write_text("\n".join(merged_whitelist), encoding="utf-8")
        logger.info("Whitelist загружен в %s и очищен от дубликатов", merged_whitelist_path)

    if blacklist_path.exists():
        logger.info("Черный список учтён: %d запрещённых прокси удалено из итогового файла", len(load_blacklist_keys(blacklist_path)))
    logger.info("Итог: объединено %d уникальных конфигов в %s", merged_count, output_path_path)
    logger.info("Завершено успешно")
    return {
        "output_path": str(output_path_path),
        "merged_count": merged_count,
        "partial_files": [str(path) for path in partial_files],
        "logger": logger,
    }

