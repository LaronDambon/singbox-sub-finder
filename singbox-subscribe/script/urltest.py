import json
import re
import sys
import warnings
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]

import requests
warnings.filterwarnings("ignore", category=Warning, module=r"requests")

from script.core import generate_debug_configs_with_singbox
from script.downloader import build_clean_tag, normalize_proxy_key, load_blacklist_keys, dedupe_blacklist_file
from script.logger_utils import get_project_logger, setup_project_logging
from config.settings import BATCH_SIZE, URLTEST_URL, TIMEOUT, WHITELIST_FILE, BLACKLIST_FILE

setup_project_logging(console_level=20)
LOGGER = get_project_logger("debug_ping_runner")


def load_merge_lines(merge_path: str | Path) -> List[str]:
    path = Path(merge_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"merge file not found: {path}")
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def parse_ping_from_output(output_lines: List[str]) -> List[Tuple[str, int | None]]:
    results: List[Tuple[str, int | None]] = []
    for line in output_lines:
        if "outbound/urltest" not in line or ("available" not in line and "unavailable" not in line):
            continue
        match = re.search(r"outbound/urltest\[[^\]]+\]: outbound\s+(.+?)\s+(available|unavailable)", line)
        if not match:
            continue
        tag = match.group(1).strip()
        status = match.group(2).lower()
        ping_value = None
        ping_match = re.search(r"(\d+)ms", line)
        if ping_match:
            ping_value = int(ping_match.group(1))
        results.append((tag, ping_value if status == "available" else None))
    return results


def append_line(path: str | Path, line: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if fh.tell() > 0:
            fh.write("\n")
        fh.write(line)


def build_ping_result_line(proxy_line: str, seq_counter: int, ping_ms: int | None) -> str:
    cleaned = build_clean_tag(proxy_line, seq_counter=seq_counter, include_country=True)
    if ping_ms is None:
        return cleaned
    return f"{cleaned}-ping-{ping_ms}"


def ensure_blacklist(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(line, encoding="utf-8")
        return

    existing = {normalize_proxy_key(l.strip()): l for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()}
    key = normalize_proxy_key(line)
    if key and key not in existing:
        with path.open("a", encoding="utf-8") as fh:
            if fh.tell() > 0:
                fh.write("\n")
            fh.write(line)


def run_debug_ping_cycle(
    merge_path: str | Path,
    *,
    batch_size: int = BATCH_SIZE,
    urltest: str = URLTEST_URL,
    timeout: float = TIMEOUT,
    whitelist_path: str | Path | None = WHITELIST_FILE,
    blacklist_path: str | Path | None = BLACKLIST_FILE,
) -> dict:
    merge_file = Path(merge_path).resolve()
    lines = load_merge_lines(merge_file)

    whitelist_output = Path(whitelist_path) if whitelist_path else merge_file.with_suffix(".whitelist.txt")
    blacklist_output = Path(blacklist_path) if blacklist_path else merge_file.with_suffix(".blacklist.txt")

    if whitelist_output.exists():
        whitelist_output.unlink()

    total = len(lines)
    processed = 0
    written = 0
    whitelist_count = 0
    blacklist_count = 0

    for start in range(0, total, batch_size):
        batch = lines[start:start + batch_size]
        processed += len(batch)
        batch_end = min(start + len(batch), total)
        LOGGER.info("Checking batch %d-%d/%d", start + 1, batch_end, total)

        try:
            result = generate_debug_configs_with_singbox(
                threads=4,
                urltest=urltest,
                ping_limit=500,
                merge_lines=batch,
            )
        except Exception as exc:
            LOGGER.exception(
                "Batch %d-%d failed during sing-box generation; skipping this batch.",
                start + 1,
                batch_end,
            )
            continue

        output_lines = result.get("output", [])
        parsed = parse_ping_from_output(output_lines)
        parsed_nodes = result.get("parsed_nodes", [])
        parsed_node_lines = result.get("parsed_node_lines", [])
        tag_to_line = {node.get("tag"): line for node, line in zip(parsed_nodes, parsed_node_lines) if isinstance(node, dict) and node.get("tag")}

        available_tags = {tag for tag, ping_ms in parsed if ping_ms is not None}
        batch_whitelist = 0
        batch_blacklist = 0
        output_serial = start

        for tag, original_line in tag_to_line.items():
            output_serial += 1
            if tag in available_tags:
                ping_ms = next(p for t, p in parsed if t == tag)
                append_line(whitelist_output, build_ping_result_line(original_line, seq_counter=output_serial, ping_ms=ping_ms))
                whitelist_count += 1
                batch_whitelist += 1
            else:
                ensure_blacklist(blacklist_output, build_ping_result_line(original_line, seq_counter=output_serial, ping_ms=None))
                blacklist_count += 1
                batch_blacklist += 1
            written += 1

        missing_tags = [tag for tag, _ in parsed if tag not in tag_to_line]
        if missing_tags:
            LOGGER.warning(
                "Batch %d-%d parsed %d tags not found in original batch: %s",
                start + 1,
                min(start + len(batch), total),
                len(missing_tags),
                missing_tags,
            )

        LOGGER.info(
            "Batch %d-%d result: %d available, %d blacklist",
            start + 1,
            min(start + len(batch), total),
            batch_whitelist,
            batch_blacklist,
        )

    dedupe_blacklist_file(Path(blacklist_output))
    return {
        "merge_path": str(merge_file),
        "batch_size": batch_size,
        "processed": processed,
        "written": written,
        "whitelist_count": whitelist_count,
        "blacklist_count": blacklist_count,
        "whitelist_path": str(whitelist_output),
        "blacklist_path": str(blacklist_output),
    }
