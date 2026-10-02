"""Запись результатов проверки в базу.

Отдельный этап: превращает ``CheckResult` чекеров в строки-записи для
``ServerStore.record_results()` и в строку прокси с аккуратным именем
(протокол, номер, страна, пинг, stable).

Правило именования (общее для всего проекта):

    vmess://...#vmess 000123-🇩🇪-ping-127-stable-96
        ^ исходная ссылка    ^ счётчик  ^ страна  ^ пинг  ^ стабильность
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Sequence

from script.downloader import build_clean_tag, normalize_proxy_key
from script.server_store import compute_next_state
from pipeline.checkers.base import CheckOutcome, CheckResult
from pipeline.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pipeline.database import QueueItem

LOGGER = get_logger("stages.writer")

#: stable нового сервера, если его ещё нет в карте.
DEFAULT_NEW_STABLE = 5


def format_line(
    raw_line: str,
    *,
    serial: int,
    ok: bool,
    outcome: CheckOutcome,
    stable: int,
) -> str:
    """Собирает строку прокси с тегом: страна, пинг и stable."""
    country = outcome.country or ""
    tag = build_clean_tag(
        raw_line,
        seq_counter=serial,
        include_country=ok,
        country_tag=country or None,
    )
    if ok and outcome.ping_ms is not None:
        tag = f"{tag}-ping-{outcome.ping_ms}"
    return f"{tag}-stable-{stable}"


def build_rows(
    items: Sequence["QueueItem"],
    result: CheckResult,
    *,
    stable_map: dict[str, int],
    start_serial: int = 0,
    new_server_stable: int = DEFAULT_NEW_STABLE,
) -> tuple[list[dict], dict[str, int]]:
    """Строки-записи для базы + обновлённая карта stable.

    Для сервера, которому чекер не дал вердикта (ок=None — sing-box не
    отработал), stable НЕ меняется: инфраструктурный сбой не должен убивать
    серверы. В базу такая строка не пишется вообще — она остаётся в очереди.
    """
    rows: list[dict] = []
    serial = start_serial
    for item in items:
        outcome = result.outcomes.get(item.line)
        if outcome is None or outcome.ok is None:
            continue
        serial += 1
        key = item.key or normalize_proxy_key(item.line)
        prev = stable_map.get(key, int(new_server_stable))
        new_stable = compute_next_state(prev, ok=bool(outcome.ok))
        stable_map[key] = new_stable
        rows.append(
            {
                "key": key,
                "available": bool(outcome.ok),
                "line": format_line(
                    item.line,
                    serial=serial,
                    ok=bool(outcome.ok),
                    outcome=outcome,
                    stable=new_stable,
                ),
                "ping_ms": outcome.ping_ms,
                "country": outcome.country or "",
                "protocol": _protocol(item.line),
                "capabilities": outcome.capabilities or "",
            }
        )
    return rows, stable_map


def _protocol(line: str) -> str:
    from utils import tool

    return (tool.get_protocol(line) or "").lower()


def count_results(outcomes: dict[str, CheckOutcome]) -> dict[str, int]:
    """Сколько серверов ответило, не ответило и осталось непроверенным."""
    ok = failed = unknown = 0
    for outcome in outcomes.values():
        if outcome.ok is True:
            ok += 1
        elif outcome.ok is False:
            failed += 1
        else:
            unknown += 1
    return {"ok": ok, "failed": failed, "unknown": unknown}


def total_unparsable(results: Iterable[CheckResult]) -> list[str]:
    """Собирает строки, которые нечем проверять, из всех чекеров батча."""
    seen: dict[str, None] = {}
    for result in results:
        for line in result.unparsable:
            seen.setdefault(line, None)
    return list(seen)
