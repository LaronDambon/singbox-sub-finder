"""Центральная база серверов (SQLite) — единственный источник правды по stable.

Заменяет связку whitelist.txt/blacklist.txt:
  * каждый известный сервер хранится одной записью с ключом normalize_proxy_key;
  * stable накапливается между запусками (+1 успех, -1 неудача);
  * временный чс (защита от час-пиков): TEMP_BAN_FAILS неудач ПОДРЯД отправляют
    сервер в бан на TEMP_BAN_HOURS часов — из проверки и из whitelist он исчезает
    «скрыто», а первая проверка после окончания бана решает его судьбу:
    успех -> восстановление, неудача -> полноценный чс (stable = FULL_BAN_STABLE);
  * списки выводятся из базы фильтром: stable > WHITELIST_EXPORT_MIN_STABLE —
    в экспорт (whitelist.txt, /api/whitelist); серверы во временном чс не
    экспортируются, даже если их stable формально проходит порог;
  * серверы со stable < PURGE_STABLE_BELOW (по умолчанию только stable=-2 —
    полноценный чс) исключаются из проверочного пула (флаг excluded) и могут
    быть возвращены вручную (CLI reset-excluded).

Зоны stable при настройках по умолчанию (export>1, purge<-1, full ban=-2):
  stable = -2   — полноценный чс: исключён из проверки (excluded=1)
  stable = -1   — «подозрительный»: остаётся в списках проверки, в whitelist не попадает
  stable 0..1   — в ротации проверки, в списки не попадают
  stable >= 2   — подтверждённые, идут в экспорт списков
Колонки временного чс:
  fail_streak    — счётчик подряд идущих неудачных проверок
  temp_ban_until — ISO-момент окончания временного чса (NULL — бана нет)

CLI:
  python -m script.server_store stats
  python -m script.server_store export [--min-stable N] [--max-stable N] [--out FILE]
  python -m script.server_store purge [--below N] [--hard]
  python -m script.server_store reset-excluded
  python -m script.server_store reset-temp-bans
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from config.env import (
    FULL_BAN_STABLE,
    PURGE_STABLE_BELOW,
    SERVERS_DB_FILE,
    STABLE_MAX,
    TEMP_BAN_FAILS,
    TEMP_BAN_HOURS,
    WHITELIST_EXPORT_MIN_STABLE,
)

ROOT = Path(__file__).resolve().parents[1]

STABLE_SUFFIX_RE = re.compile(r"-stable-(-?\d+)\s*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    key            TEXT PRIMARY KEY,
    line           TEXT NOT NULL,
    stable         INTEGER NOT NULL DEFAULT 0,
    fail_streak    INTEGER NOT NULL DEFAULT 0,
    temp_ban_until TEXT,
    ping_ms        INTEGER,
    protocol       TEXT NOT NULL DEFAULT '',
    country        TEXT NOT NULL DEFAULT '',
    available      INTEGER,
    checks         INTEGER NOT NULL DEFAULT 0,
    fails          INTEGER NOT NULL DEFAULT 0,
    capabilities   TEXT NOT NULL DEFAULT '',
    excluded       INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    last_checked   TEXT
);
CREATE INDEX IF NOT EXISTS idx_servers_stable ON servers(stable);
CREATE INDEX IF NOT EXISTS idx_servers_excluded ON servers(excluded);
"""

# Индекс создаётся в __init__ ПОСЛЕ миграции колонок (в старой базе колонки
# temp_ban_until может ещё не быть).
TEMP_BAN_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_servers_temp_ban ON servers(temp_ban_until)"

# Фильтр «сервер не в действующем временном чс». Сравнение ISO-строк
# лексикографическое — оба значения пишутся в одном формате (UTC, секунды).
_NOT_BANNED_SQL = "(temp_ban_until IS NULL OR temp_ban_until = '' OR temp_ban_until <= ?)"
_BANNED_SQL = "(temp_ban_until IS NOT NULL AND temp_ban_until != '' AND temp_ban_until > ?)"

UPSERT_RESULT_SQL = """
INSERT INTO servers (
    key, line, stable, fail_streak, temp_ban_until, ping_ms, protocol,
    country, capabilities, available, checks, fails, first_seen, last_seen, last_checked
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
    stable         = excluded.stable,
    fail_streak    = excluded.fail_streak,
    temp_ban_until = excluded.temp_ban_until,
    ping_ms        = COALESCE(excluded.ping_ms, servers.ping_ms),
    protocol       = CASE WHEN excluded.protocol != '' THEN excluded.protocol ELSE servers.protocol END,
    country        = CASE WHEN excluded.country != '' THEN excluded.country ELSE servers.country END,
    capabilities   = CASE WHEN excluded.capabilities != '' THEN excluded.capabilities ELSE servers.capabilities END,
    available      = excluded.available,
    checks         = servers.checks + 1,
    fails          = servers.fails + excluded.fails,
    line           = CASE WHEN excluded.line != '' THEN excluded.line ELSE servers.line END,
    last_checked   = excluded.last_checked,
    last_seen      = excluded.last_seen
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_stable_from_line(line: str) -> int:
    """Достаёт stable из конца строки (после #). Если поля нет — по умолчанию 0."""
    match = STABLE_SUFFIX_RE.search(line)
    if match:
        return int(match.group(1))
    return 0


def compute_next_state(
    prev_stable: int,
    prev_fail_streak: int,
    prev_temp_ban_until: str | None,
    *,
    ok: bool,
    now: str | None = None,
    stable_max: int | None = None,
    temp_ban_fails: int | None = None,
    temp_ban_hours: float | None = None,
    full_ban_stable: int | None = None,
) -> dict:
    """Чистая функция перехода состояния сервера после одной проверки.

    Правила (метод «временного чса», работает вместе со stable и скрыто):
      * успех: stable +1 (потолок STABLE_MAX), серия неудач и бан сбрасываются;
      * неудача: stable -1 (пол — FULL_BAN_STABLE), fail_streak +1;
      * TEMP_BAN_FAILS неудач подряд -> временный чс: temp_ban_until =
        now + TEMP_BAN_HOURS, stable удерживается на уровне выше полного чса
        (сервер ещё НЕ в полноценном чс, но из проверки и whitelist исчезает);
      * бан истёк и повторная проверка снова провалена -> полноценный чс:
        stable = FULL_BAN_STABLE (дальше его исключит purge из ротации);
      * внеплановая проверка во время действия бана — состояние удерживается,
        эскалации нет (сервер уже «наказан»).

    Возвращает {stable, fail_streak, temp_ban_until}.
    """
    cap = max(int(STABLE_MAX if stable_max is None else stable_max), 0)
    ban_fails = max(int(TEMP_BAN_FAILS if temp_ban_fails is None else temp_ban_fails), 1)
    ban_hours = float(TEMP_BAN_HOURS if temp_ban_hours is None else temp_ban_hours)
    full_ban = int(FULL_BAN_STABLE if full_ban_stable is None else full_ban_stable)

    if now is None:
        base = datetime.now(timezone.utc)
    else:
        base = datetime.fromisoformat(str(now))
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)

    if ok:
        return {
            "stable": min(int(prev_stable) + 1, cap),
            "fail_streak": 0,
            "temp_ban_until": None,
        }

    fail_streak = int(prev_fail_streak) + 1
    ban_until = (str(prev_temp_ban_until).strip() if prev_temp_ban_until else "") or None

    if ban_until:
        if ban_until > base.isoformat(timespec="seconds"):
            # Бан ещё действует (внеплановая проверка): удерживаем состояние.
            return {
                "stable": max(int(prev_stable) - 1, full_ban + 1),
                "fail_streak": fail_streak,
                "temp_ban_until": ban_until,
            }
        # Бан истёк — это решающая повторная проверка: провал = полноценный чс.
        return {
            "stable": full_ban,
            "fail_streak": fail_streak,
            "temp_ban_until": None,
        }

    if fail_streak >= ban_fails and int(prev_stable) > full_ban:
        # TEMP_BAN_FAILS неудач подряд -> временный чс на TEMP_BAN_HOURS часов.
        until = (base + timedelta(hours=ban_hours)).isoformat(timespec="seconds")
        return {
            "stable": max(int(prev_stable) - 1, full_ban + 1),
            "fail_streak": fail_streak,
            "temp_ban_until": until,
        }

    return {
        "stable": max(int(prev_stable) - 1, full_ban),
        "fail_streak": fail_streak,
        "temp_ban_until": None,
    }


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
            # Нормализация накопленных значений под текущие границы stable:
            # без этого старые записи с большим |stable| вымывались бы слишком
            # медленно (или наоборот выпадали мгновенно).
            cap = max(int(STABLE_MAX), 0)
            full_ban = int(FULL_BAN_STABLE)
            conn.execute("UPDATE servers SET stable = ? WHERE stable > ?", (cap, cap))
            conn.execute("UPDATE servers SET stable = ? WHERE stable < ?", (full_ban, full_ban))
        self._ensure_temp_ban_columns()
        with self._connect() as conn:
            conn.execute(TEMP_BAN_INDEX_SQL)
        self._maybe_migrate_stable_zones()
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
        return {row["key"]: (row["stable"] or 0) for row in rows}

    def load_state_map(self) -> dict[str, dict]:
        """Карта ключ -> {stable, fail_streak, temp_ban_until}.

        Полное состояние для стейт-машины compute_next_state(): его используют
        цикл проверки (urltest) для решений и суффиксов строк.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, stable, fail_streak, temp_ban_until FROM servers"
            ).fetchall()
        return {
            row["key"]: {
                "stable": row["stable"] or 0,
                "fail_streak": row["fail_streak"] or 0,
                "temp_ban_until": (row["temp_ban_until"] or "").strip() or None,
            }
            for row in rows
        }

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

    def load_capabilities_map(self) -> dict[str, str]:
        """Карта ключ -> профиль достижимости (capabilities строка).

        Профиль — упорядоченный список тэгов целей, например
        "openrouter,gemini,youtube". Пустой/отсутствующий профиль означает,
        что сервер ещё не профилирован (или не проходил reachability-проверку).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, capabilities FROM servers WHERE capabilities != ''"
            ).fetchall()
        return {row["key"]: row["capabilities"] for row in rows}

    def record_capabilities(self, rows: Sequence[dict]) -> int:
        """Обновляет только профиль достижимости для ключей.

        row: {key, capabilities: str} — перезаписывает профиль целиком.
        Возвращает количество обновлённых записей.
        """
        if not rows:
            return 0
        now = _now()
        updated = 0
        with self._connect() as conn:
            for row in rows:
                caps = str(row.get("capabilities") or "").strip()
                cur = conn.execute(
                    """
                    UPDATE servers SET capabilities = ?, last_checked = ?, last_seen = ?
                    WHERE key = ?
                    """,
                    (caps, now, now, row["key"]),
                )
                updated += cur.rowcount
        return updated

    def get_capabilities(self, key: str) -> str:
        """Профиль достижимости одного сервера по ключу ('' если нет)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT capabilities FROM servers WHERE key = ?", (key,)
            ).fetchone()
        return (row["capabilities"] if row else "") or ""

    # ----------------------------------------------------------- регистрация
    def upsert_lines(self, lines: Iterable[str]) -> dict[str, int]:
        """Регистрирует серверы из подписок/исходников.

        Новый сервер получает stable=0 и попадает в ротацию проверки.
        Существующий обновляет last_seen (и line, если его ещё ни разу
        не проверяли), НЕ сбрасывая stable/fail_streak и не снимая excluded
        и временный чс: мёртвый сервер не воскресает от повторного появления
        в подписке.
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
        return {"added": added, "updated": updated, "skipped": skipped}

    def record_results(self, rows: Sequence[dict]) -> None:
        """Записывает результаты проверки пачкой (один commit на batch).

        row: {key, available: bool, line?: str, ping_ms?: int|None,
              country?: str, protocol?: str, capabilities?: str}

        Переход состояния считает compute_next_state():
          * успех  — stable +1 (до STABLE_MAX), серия неудач и бан сбрасываются;
          * неудача — stable -1, fail_streak +1; TEMP_BAN_FAILS подряд ->
            временный чс (temp_ban_until = now + TEMP_BAN_HOURS); провал
            решающей проверки после бана — полноценный чс (stable=FULL_BAN_STABLE).
        """
        if not rows:
            return
        now = _now()
        with self._connect() as conn:
            keys = list({row["key"] for row in rows})
            prev: dict[str, sqlite3.Row] = {}
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                for r in conn.execute(
                    "SELECT key, stable, fail_streak, temp_ban_until FROM servers "
                    f"WHERE key IN ({placeholders})",
                    chunk,
                ):
                    prev[r["key"]] = r
            for row in rows:
                ok = bool(row.get("available"))
                p = prev.get(row["key"])
                nxt = compute_next_state(
                    p["stable"] if p else 0,
                    p["fail_streak"] if p else 0,
                    p["temp_ban_until"] if p else None,
                    ok=ok,
                    now=now,
                )
                conn.execute(
                    UPSERT_RESULT_SQL,
                    (
                        row["key"],
                        row.get("line") or "",
                        nxt["stable"],
                        nxt["fail_streak"],
                        nxt["temp_ban_until"],
                        row.get("ping_ms"),
                        row.get("protocol") or "",
                        row.get("country") or "",
                        row.get("capabilities") or "",
                        1 if ok else 0,
                        0 if ok else 1,
                        now, now, now,
                    ),
                )

    # ----------------------------------------------------------- запросы DB
    def check_pool(self) -> list[sqlite3.Row]:
        """Пул проверки: активные серверы вне действующего временного чса.

        Лучшие (высокий stable, низкий пинг) — первыми. Серверы во временном
        чсе (temp_ban_until > now) в пул не попадают; как только бан истёк,
        сервер автоматически возвращается в ротацию для решающей проверки.
        """
        now = _now()
        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT key, line, stable, ping_ms, fail_streak, temp_ban_until FROM servers
                WHERE excluded = 0 AND {_NOT_BANNED_SQL}
                ORDER BY stable DESC, ping_ms IS NULL, ping_ms ASC, key ASC
                """,
                (now,),
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
        max_stable=-1 -> только stable < -1 (строго меньше).
        Серверы в действующем временном чсе не экспортируются никогда:
        «помеченные временным чс не попадают в whitelist».
        Сортировка: stable DESC, затем пинг по возрастанию (без пинга — в конце).
        """
        now = _now()
        query = f"SELECT line, stable, ping_ms, capabilities FROM servers WHERE line != '' AND {_NOT_BANNED_SQL}"
        params: list[object] = [now]
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

    def _capability_tags(self, caps: str, global_tag: str) -> str:
        """Превращает профиль (список тэгов через запятую) в суффикс тэгов.

        Соглашение хранения в БД (колонка capabilities):
          * "Global"              — сервер достиг ВСЕХ целевых сайтов
                                     -> экспортируется одним тэгом [Global];
          * "openrouter,gemini"   — сервер достиг этих целей
                                     -> экспортируется как [openrouter][gemini].
        Тэг Global НЕ дублируется, если он среди разложенных тэгов.
        """
        if not caps:
            return ""
        tags = [t.strip() for t in caps.split(",") if t.strip()]
        if any(t == global_tag for t in tags):
            return f"[{global_tag}]"
        return "".join(f"[{t}]" for t in tags)

    def export_tagged_lines(self, *,
                            min_stable: int | None = None,
                            max_stable: int | None = None,
                            global_tag: str = "Global") -> list[str]:
        """Строки серверов по фильтру stable с добавленными capability-тэгами.

        ВАЖНО: capability-профиль ХРАНИТСЯ в БД, тэги [name] добавляются
        только здесь, на этапе экспорта для генерации итогового конфига.
        Сервер, у которого профиль полностью состоит из global_tag (достиг
        всех целей), получает ровно один тэг [Global]. Серверы в действующем
        временном чсе не экспортируются.
        """
        now = _now()
        query = f"SELECT line, stable, ping_ms, capabilities FROM servers WHERE line != '' AND {_NOT_BANNED_SQL}"
        params: list[object] = [now]
        if min_stable is not None:
            query += " AND stable > ?"
            params.append(min_stable)
        if max_stable is not None:
            query += " AND stable < ?"
            params.append(max_stable)
        query += " ORDER BY stable DESC, ping_ms IS NULL, ping_ms ASC, key ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            base = row["line"]
            caps = row["capabilities"] or ""
            # Если в профиле уже есть Global-метка храним её как global_tag;
            # иначе — обычный профиль целей.
            if caps:
                # Сервер, прошедший все проверки, помечен в БД ровно global_tag.
                suffix = self._capability_tags(caps, global_tag)
                # Не добавляем Global повторно, если он уже в строке.
                if suffix and f"[{global_tag}]" not in base:
                    base = base + suffix
            out.append(base)
        return out

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

    def export_tagged_to_file(
        self,
        path: str | Path,
        *,
        min_stable: int | None = None,
        max_stable: int | None = None,
        global_tag: str = "Global",
    ) -> int:
        """Экспорт whitelist-строк С capability-тэгами ([name] / [Global]).

        Это «whitelist для генерации»: тэги добавляются только здесь, на этапе
        выгрузки строк, чтобы итоговый sing-box конфиг мог фильтровать outbound'ы
        по достижимости целей. Профиль при этом остаётся в БД, а не в merge-пуле.
        """
        lines = self.export_tagged_lines(
            min_stable=min_stable, max_stable=max_stable, global_tag=global_tag,
        )
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        return len(lines)

    # --------------------------------------------------- удаление из проверки
    def purge_dead(self, below: int | None = None, *, hard: bool = False) -> int:
        """Удаляет серверы со stable < below из проверочного списка.

        По умолчанию below = PURGE_STABLE_BELOW (-1): в полноценный чс уходят
        только stable <= FULL_BAN_STABLE (-2), «подозрительные» (stable=-1)
        остаются в ротации. hard=False выставляет excluded=1 — запись остаётся
        в базе для статистики и повторного включения вручную; hard=True
        физически удаляет.
        """
        if below is None:
            below = int(PURGE_STABLE_BELOW)
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
        """Возвращает исключённые серверы в ротацию проверки с чистым листом."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE servers SET excluded = 0, stable = 0, fail_streak = 0, "
                "temp_ban_until = NULL WHERE excluded = 1"
            )
            return cur.rowcount

    def reset_temp_bans(self) -> int:
        """Снимает все временные чс и сбрасывает серии неудач."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE servers SET temp_ban_until = NULL, fail_streak = 0 "
                "WHERE temp_ban_until IS NOT NULL AND temp_ban_until != ''"
            )
            return cur.rowcount

    def stats(self) -> dict:
        now = _now()
        full_ban = int(FULL_BAN_STABLE)
        min_export = int(WHITELIST_EXPORT_MIN_STABLE)
        with self._connect() as conn:
            total, active, excluded = conn.execute(
                "SELECT COUNT(*), SUM(excluded = 0), SUM(excluded = 1) FROM servers"
            ).fetchone()
            zones = {
                # stable <= FULL_BAN_STABLE — полноценный чс (исключены purge'ом).
                "full_ban": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable <= ?", (full_ban,)
                ).fetchone()[0],
                # stable = FULL_BAN_STABLE + 1 («подозрительные») — в ротации,
                # в списки не попадают.
                "probation": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable = ?", (full_ban + 1,)
                ).fetchone()[0],
                # 0..порога экспорта — в ротации проверки.
                "testing": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable > ? AND stable <= ?",
                    (full_ban + 1, min_export),
                ).fetchone()[0],
                # stable > порога экспорта — подтверждённые.
                "proven": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable > ?", (min_export,)
                ).fetchone()[0],
                # Сейчас в действующем временном чсе (не считая excluded).
                "temp_banned": conn.execute(
                    f"SELECT COUNT(*) FROM servers WHERE excluded = 0 AND {_BANNED_SQL}",
                    (now,),
                ).fetchone()[0],
            }
        return {
            "db_path": str(self.db_path),
            "total": total or 0,
            "active": active or 0,
            "excluded": excluded or 0,
            "zones": zones,
        }

    # --------------------------------------------------------- миграции/legacy
    LEGACY_WHITELIST = ROOT / "source" / "whitelist.txt"
    LEGACY_BLACKLIST = ROOT / "source" / "blacklist.txt"

    def _ensure_temp_ban_columns(self) -> None:
        """Добавляет колонки временного чса в существующую базу (один раз).

        Старые базы созданы без fail_streak/temp_ban_until — SQLite требует
        ALTER TABLE для каждой недостающей колонки.
        """
        with self._connect() as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(servers)").fetchall()]
            if "fail_streak" not in cols:
                conn.execute(
                    "ALTER TABLE servers ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0"
                )
            if "temp_ban_until" not in cols:
                conn.execute("ALTER TABLE servers ADD COLUMN temp_ban_until TEXT")

    def _maybe_migrate_stable_zones(self) -> None:
        """Однократный возврат «старых» исключённых серверов в ротацию.

        Раньше порог исключения был stable < 0: первый же провал (stable=-1)
        навсегда убирал сервер из проверки — в том числе рабочие серверы,
        временно перегруженные в час-пик. Теперь stable=-1 остаётся в ротации
        (временный чс наступает по серии неудач), поэтому старые excluded-
        записи со stable >= PURGE_STABLE_BELOW возвращаются в пул один раз.
        Маркер рядом с базой защищает от повторного запуска.
        """
        marker = self.db_path.with_suffix(".tempban-migrated.json")
        if marker.exists():
            return
        with self._connect() as conn:
            revived = conn.execute(
                "UPDATE servers SET excluded = 0, fail_streak = 0, temp_ban_until = NULL "
                "WHERE excluded = 1 AND stable >= ?",
                (int(PURGE_STABLE_BELOW),),
            ).rowcount
        marker.write_text(
            json.dumps({"revived": revived, "purge_below": int(PURGE_STABLE_BELOW)}, ensure_ascii=False),
            encoding="utf-8",
        )

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
        full_ban = int(FULL_BAN_STABLE)
        with self._connect() as conn:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                clean = raw.strip()
                if not clean:
                    continue
                key = self._key_of(clean)
                if not key:
                    continue
                stable = min(parse_stable_from_line(clean), max(int(STABLE_MAX), 0))
                stable = max(stable, full_ban)
                cur = conn.execute(
                    verb + " INTO servers (key, line, stable, excluded, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (key, clean, stable, 1 if excluded else 0, now, now),
                )
                inserted += cur.rowcount
        return inserted


def main() -> int:
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
    p_purge.add_argument("--below", type=int, default=int(PURGE_STABLE_BELOW))
    p_purge.add_argument("--hard", action="store_true")
    sub.add_parser("reset-excluded")
    sub.add_parser("reset-temp-bans")

    args = parser.parse_args()
    store = ServerStore(args.db)

    if args.cmd == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "export":
        min_stable = args.min_stable
        if min_stable is None and args.max_stable is None:
            min_stable = int(WHITELIST_EXPORT_MIN_STABLE)
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
        print(f"Returned {revived} servers to check rotation (state reset)")
    elif args.cmd == "reset-temp-bans":
        cleared = store.reset_temp_bans()
        print(f"Cleared temp ban for {cleared} servers (fail streaks reset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
