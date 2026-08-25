"""Центральная база серверов (SQLite) — единственный источник правды по stable.

Заменяет связку whitelist.txt/blacklist.txt:
  * каждый известный сервер хранится одной записью с ключом normalize_proxy_key;
  * stable накапливается между запусками (+1 успех, -1 неудача);
  * списки выводятся из базы фильтром: stable > WHITELIST_EXPORT_MIN_STABLE —
    в экспорт (whitelist.txt, /api/whitelist);
  * серверы со stable < PURGE_STABLE_BELOW исключаются из проверочного пула
    (флаг excluded) и могут быть возвращены вручную (CLI reset-excluded).

Зоны stable при настройках по умолчанию (export>1, purge<0):
  stable <= -1  — мёртвые, из проверки исключены
  stable 0..1   — в ротации проверки, в списки не попадают
  stable >= 2   — подтверждённые, идут в экспорт списков

CLI:
  python -m script.server_store stats
  python -m script.server_store export [--min-stable N] [--max-stable N] [--out FILE]
  python -m script.server_store purge [--below N] [--hard]
  python -m script.server_store reset-excluded
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]

STABLE_SUFFIX_RE = re.compile(r"-stable-(-?\d+)\s*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    key          TEXT PRIMARY KEY,
    line         TEXT NOT NULL,
    stable       INTEGER NOT NULL DEFAULT 0,
    ping_ms      INTEGER,
    protocol     TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT '',
    available    INTEGER,
    checks       INTEGER NOT NULL DEFAULT 0,
    fails        INTEGER NOT NULL DEFAULT 0,
    excluded     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    last_checked TEXT
);
CREATE INDEX IF NOT EXISTS idx_servers_stable ON servers(stable);
CREATE INDEX IF NOT EXISTS idx_servers_excluded ON servers(excluded);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_stable_from_line(line: str) -> int:
    """Достаёт stable из конца строки (после #). Если поля нет — по умолчанию 0."""
    match = STABLE_SUFFIX_RE.search(line)
    if match:
        return int(match.group(1))
    return 0


class ServerStore:
    """Обёртка над SQLite-базой серверов.

    Короткие соединения на операцию + WAL-режим: базу безопасно читает Flask API,
    пока main.py/urltest пишет результаты проверки.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Нормализация накопленных значений под текущий потолок stable:
            # без этого старые записи с большим stable вымывались бы слишком медленно.
            try:
                from config.settings import STABLE_MAX

                cap = max(int(STABLE_MAX), 0)
                conn.execute("UPDATE servers SET stable = ? WHERE stable > ?", (cap, cap))
            except Exception:  # noqa: BLE001 — база должна открываться даже без settings
                pass
        self._maybe_migrate_legacy()

    # ------------------------------------------------------------------ util
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _key_of(line: str) -> str:
        # Ленивый импорт: downloader импортирует этот модуль на верхнем уровне.
        from script.downloader import normalize_proxy_key

        return normalize_proxy_key(line)

    def load_stable_map(self) -> dict[str, int]:
        """Карта ключ -> stable для всех серверов (замена чтения whitelist/blacklist)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT key, stable FROM servers").fetchall()
        return {row["key"]: row["stable"] for row in rows}

    def load_country_map(self) -> dict[str, str]:
        """Карта ключ -> эмодзи страны для серверов с известной страной.

        Позволяет пропустить дорогой скоростной тест, если страна уже
        определена в прошлых циклах (по имени или через speedtest).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, country FROM servers WHERE country != ''"
            ).fetchall()
        return {row["key"]: row["country"] for row in rows}

    # ----------------------------------------------------------- регистрация
    def upsert_lines(self, lines: Iterable[str]) -> dict[str, int]:
        """Регистрирует серверы из подписок/исходников.

        Новый сервер получает stable=0 и попадает в ротацию проверки.
        Существующий обновляет last_seen (и line, если его ещё ни разу
        не проверяли), НЕ сбрасывая stable и не снимая excluded:
        мёртвый сервер не воскресает от повторного появления в подписке.
        """
        added = updated = skipped = 0
        now = _now()
        with self._connect() as conn:
            for raw in lines:
                clean = str(raw).strip()
                if not clean:
                    continue
                key = self._key_of(clean)
                if not key:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM servers WHERE key = ?", (key,)
                ).fetchone()
                if exists:
                    updated += 1
                    conn.execute(
                        """
                        UPDATE servers SET
                            last_seen = ?,
                            line = CASE WHEN available IS NULL THEN ? ELSE line END
                        WHERE key = ?
                        """,
                        (now, clean, key),
                    )
                else:
                    added += 1
                    conn.execute(
                        "INSERT INTO servers (key, line, stable, first_seen, last_seen)"
                        " VALUES (?, ?, 0, ?, ?)",
                        (key, clean, now, now),
                    )
        return {"added": added, "updated": updated}

    def record_results(self, rows: Sequence[dict]) -> None:
        """Записывает результаты проверки пачкой (один commit на batch).

        row: {key, available: bool, line?: str, ping_ms?: int|None,
              country?: str, protocol?: str}
        stable: +1 при успехе, -1 при неудаче.
        """
        if not rows:
            return
        from config.settings import STABLE_MAX

        stable_cap = max(int(STABLE_MAX), 0)
        now = _now()
        with self._connect() as conn:
            for row in rows:
                ok = bool(row.get("available"))
                delta = 1 if ok else -1
                conn.execute(
                    """
                    INSERT INTO servers (key, line, stable, ping_ms, protocol, country,
                                         available, checks, fails, first_seen, last_seen, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        stable = MIN(stable + ?, ?),
                        ping_ms = COALESCE(?, servers.ping_ms),
                        protocol = CASE WHEN ? != '' THEN ? ELSE servers.protocol END,
                        country = CASE WHEN ? != '' THEN ? ELSE servers.country END,
                        available = ?,
                        checks = checks + 1,
                        fails = fails + ?,
                        line = COALESCE(?, servers.line),
                        last_checked = ?,
                        last_seen = ?
                    """,
                    (
                        row["key"],
                        row.get("line") or "",
                        delta,
                        row.get("ping_ms"),
                        row.get("protocol") or "",
                        row.get("country") or "",
                        1 if ok else 0,
                        0 if ok else 1,
                        now, now, now,
                        delta,
                        stable_cap,
                        row.get("ping_ms"),
                        row.get("protocol") or "", row.get("protocol") or "",
                        row.get("country") or "", row.get("country") or "",
                        1 if ok else 0,
                        0 if ok else 1,
                        row.get("line"),
                        now, now,
                    ),
                )

    # ----------------------------------------------------------- запросы DB
    def check_pool(self) -> list[sqlite3.Row]:
        """Пул проверки: все активные серверы, лучшие (высокий stable, низкий пинг) — первыми."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT key, line, stable, ping_ms FROM servers
                WHERE excluded = 0
                ORDER BY stable DESC, ping_ms IS NULL, ping_ms ASC, key ASC
                """
            ).fetchall()

    def export_lines(
        self,
        *,
        min_stable: int | None = None,
        max_stable: int | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Строки серверов по фильтру stable.

        min_stable=1 -> только stable > 1 (строго больше);
        max_stable=0 -> только stable < 0 (строго меньше).
        Сортировка: stable DESC, затем пинг по возрастанию (без пинга — в конце).
        """
        query = "SELECT line, stable, ping_ms FROM servers WHERE line != ''"
        params: list[object] = []
        if min_stable is not None:
            query += " AND stable > ?"
            params.append(min_stable)
        if max_stable is not None:
            query += " AND stable < ?"
            params.append(max_stable)
        query += " ORDER BY stable DESC, ping_ms IS NULL, ping_ms ASC, key ASC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row["line"] for row in rows]

    def export_to_file(
        self,
        path: str | Path,
        *,
        min_stable: int | None = None,
        max_stable: int | None = None,
    ) -> int:
        lines = self.export_lines(min_stable=min_stable, max_stable=max_stable)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        return len(lines)

    # -------------------------------------------------- удаление из проверки
    def purge_dead(self, below: int = 0, *, hard: bool = False) -> int:
        """Удаляет серверы со stable < below из проверочного списка.

        По умолчанию (hard=False) выставляет excluded=1 — запись остаётся в базе
        для статистики и повторного включения вручную. hard=True физически удаляет.
        """
        with self._connect() as conn:
            if hard:
                cur = conn.execute("DELETE FROM servers WHERE stable < ?", (below,))
            else:
                cur = conn.execute(
                    "UPDATE servers SET excluded = 1 WHERE stable < ? AND excluded = 0",
                    (below,),
                )
            return cur.rowcount

    def reset_excluded(self) -> int:
        """Возвращает исключённые серверы в ротацию проверки (stable -> 0)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE servers SET excluded = 0, stable = 0 WHERE excluded = 1"
            )
            return cur.rowcount

    def stats(self) -> dict:
        with self._connect() as conn:
            total, active, excluded = conn.execute(
                "SELECT COUNT(*), SUM(excluded = 0), SUM(excluded = 1) FROM servers"
            ).fetchone()
            zones = {
                "dead_lt_0": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable < 0"
                ).fetchone()[0],
                "testing_0_1": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable >= 0 AND stable <= 1"
                ).fetchone()[0],
                "proven_gt_1": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable > 1"
                ).fetchone()[0],
            }
        return {
            "db_path": str(self.db_path),
            "total": total or 0,
            "active": active or 0,
            "excluded": excluded or 0,
            "zones": zones,
        }

    # ------------------------------------------------------- миграция legacy
    LEGACY_WHITELIST = ROOT / "source" / "whitelist.txt"
    LEGACY_BLACKLIST = ROOT / "source" / "blacklist.txt"

    def _maybe_migrate_legacy(self) -> None:
        """Однократный импорт старых whitelist.txt/blacklist.txt в пустую базу."""
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
        if count:
            return
        wl = Path(self.LEGACY_WHITELIST)
        bl = Path(self.LEGACY_BLACKLIST)
        if not wl.exists() and not bl.exists():
            return
        imported_wl = self._import_legacy_file(wl, excluded=False)
        # Blacklist импортируем вторым и помечаем excluded, чтобы семантика
        # "не проверять" сохранилась; конфликты ключей разрешаются в пользу whitelist.
        imported_bl = self._import_legacy_file(bl, excluded=True, ignore_existing=True)
        marker = self.db_path.with_suffix(".migrated.json")
        marker.write_text(
            json.dumps({"whitelist": imported_wl, "blacklist": imported_bl}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _import_legacy_file(self, path: Path, *, excluded: bool, ignore_existing: bool = False) -> int:
        if not path.exists():
            return 0
        now = _now()
        inserted = 0
        verb = "INSERT OR IGNORE" if ignore_existing else "INSERT OR REPLACE"
        with self._connect() as conn:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                clean = raw.strip()
                if not clean:
                    continue
                key = self._key_of(clean)
                if not key:
                    continue
                stable = parse_stable_from_line(clean)
                try:
                    from config.settings import STABLE_MAX

                    stable = min(stable, max(int(STABLE_MAX), 0))
                except Exception:  # noqa: BLE001
                    pass
                cur = conn.execute(
                    verb + " INTO servers (key, line, stable, excluded, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (key, clean, stable, 1 if excluded else 0, now, now),
                )
                inserted += cur.rowcount
        return inserted


def main() -> int:
    from config.settings import SERVERS_DB_FILE, WHITELIST_EXPORT_MIN_STABLE, PURGE_STABLE_BELOW

    parser = argparse.ArgumentParser(description="Central server store CLI")
    parser.add_argument("--db", default=str(SERVERS_DB_FILE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats")
    p_export = sub.add_parser("export")
    p_export.add_argument("--min-stable", type=int, default=None)
    p_export.add_argument("--max-stable", type=int, default=None)
    p_export.add_argument("--limit", type=int, default=None)
    p_export.add_argument("--out", default=None)
    p_purge = sub.add_parser("purge")
    p_purge.add_argument("--below", type=int, default=PURGE_STABLE_BELOW)
    p_purge.add_argument("--hard", action="store_true")
    sub.add_parser("reset-excluded")

    args = parser.parse_args()
    store = ServerStore(args.db)

    if args.cmd == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "export":
        min_stable = args.min_stable
        if min_stable is None and args.max_stable is None:
            min_stable = WHITELIST_EXPORT_MIN_STABLE
        if args.out:
            n = store.export_to_file(args.out, min_stable=min_stable, max_stable=args.max_stable)
            print(f"Exported {n} lines -> {args.out}")
        else:
            lines = store.export_lines(min_stable=min_stable, max_stable=args.max_stable, limit=args.limit)
            print("\n".join(lines))
    elif args.cmd == "purge":
        affected = store.purge_dead(args.below, hard=args.hard)
        action = "Deleted" if args.hard else "Excluded"
        print(f"{action} {affected} servers with stable < {args.below}")
    elif args.cmd == "reset-excluded":
        revived = store.reset_excluded()
        print(f"Returned {revived} servers to check rotation (stable reset to 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())