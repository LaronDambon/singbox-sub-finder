"""Центральная база серверов (SQLite) — единственный источник правды по stable.

Новый режим — «щит» от удаления для серверов, которые хотя бы раз пинговались:
  * каждый известный сервер хранится одной записью с ключом normalize_proxy_key;
  * stable — счётчик жизней сервера:
      - новый сервер до первого удачного пинга получает NEW_SERVER_STABLE
        (по умолчанию 5) — несколько попыток доказать жизнеспособность;
      - каждый УДАЧНЫЙ пинг ставит stable не ниже SHIELD_CYCLES (по умолчанию 96):
        «щит» от удаления — сервер переживёт 96 неудачных проверок подряд;
      - каждая НЕУДАЧНАЯ проверка вычитает 1;
      - нижний порог — 0: в цикл проверки импортируются серверы со
        stable >= PURGE_STABLE_BELOW, а дойдя до порога - 1 (по умолчанию -1)
        сервер считается умершим и больше НЕ импортируется из базы;
  * колонка ever_pinged — 1, если сервер пинговался хотя бы раз (отслеживание
    таких конфигов и статистика);
  * пул проверки (check_pool / merge.txt) — ВСЕ живые серверы: excluded=0
    и stable >= PURGE_STABLE_BELOW, без ограничений по количеству;
  * whitelist (whitelist.txt, /api/whitelist) — проекция ПОСЛЕДНЕЙ проверки:
    сервер пинганулся (available=1) -> добавляется в whitelist, не пинганулся ->
    не добавляется, но остаётся в пуле проверки, пока жив (stable >= порога).

Зоны stable при настройках по умолчанию (порог 0, мёртвый -1):
  stable = -1   — умерший: не импортируется в проверку (excluded=1 после purge)
  stable = 0    — на границе: одна неудачная проверка до смерти
  stable 1..95  — живёт за счёт остатка щита
  stable >= 96  — под полным щитом после удачного пинга
Колонка available — результат последней проверки (1/0/NULL — ещё не проверялся).

CLI:
  python -m script.server_store stats
  python -m script.server_store export [--min-stable N] [--max-stable N] [--out FILE]
  python -m script.server_store purge [--below N] [--hard]
  python -m script.server_store reset-excluded
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from config.settings import get_settings
from utils.tool import set_country_in_name

ROOT = Path(__file__).resolve().parents[1]

STABLE_SUFFIX_RE = re.compile(r"-stable-(-?\d+)\s*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    key            TEXT PRIMARY KEY,
    line           TEXT NOT NULL,
    stable         INTEGER NOT NULL DEFAULT 0,
    ever_pinged    INTEGER NOT NULL DEFAULT 0,
    ping_ms        INTEGER,
    protocol       TEXT NOT NULL DEFAULT '',
    country        TEXT NOT NULL DEFAULT '',
    available      INTEGER,
    checks         INTEGER NOT NULL DEFAULT 0,
    fails          INTEGER NOT NULL DEFAULT 0,
    capabilities   TEXT NOT NULL DEFAULT '',
    excluded       INTEGER NOT NULL DEFAULT 0,
    -- Замер скорости в МБ/с. Раньше измерение выбрасывалось и оставался
    -- только тег вроде speed-slow, по которому нельзя отличить 0.4 МБ/с
    -- от 0.9. Для отбора «лучших» нужна сама цифра.
    speed_mbps     REAL,
    speed_down     REAL,
    speed_up       REAL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    last_checked   TEXT
);
CREATE INDEX IF NOT EXISTS idx_servers_stable ON servers(stable);
CREATE INDEX IF NOT EXISTS idx_servers_excluded ON servers(excluded);
"""

UPSERT_RESULT_SQL = """
INSERT INTO servers (
    key, line, stable, ever_pinged, ping_ms, protocol, country, capabilities,
    speed_mbps, speed_down, speed_up,
    available, checks, fails, first_seen, last_seen, last_checked
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
    stable         = excluded.stable,
    ever_pinged    = MAX(servers.ever_pinged, excluded.ever_pinged),
    ping_ms        = COALESCE(excluded.ping_ms, servers.ping_ms),
    -- COALESCE, а не прямое присваивание: сервер без замера не должен
    -- затирать результат прошлого измерения пустым значением.
    speed_mbps     = COALESCE(excluded.speed_mbps, servers.speed_mbps),
    speed_down     = COALESCE(excluded.speed_down, servers.speed_down),
    speed_up       = COALESCE(excluded.speed_up, servers.speed_up),
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
def _as_float(value):
    """Число из строки/словаря или None. Кривые значения молча игнорируются."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None



def parse_stable_from_line(line: str) -> int:
    """Достаёт stable из конца строки (после #). Если поля нет — по умолчанию 0."""
    match = STABLE_SUFFIX_RE.search(line)
    if match:
        return int(match.group(1))
    return 0


def compute_next_state(
    prev_stable: int,
    *,
    ok: bool,
    shield_cycles: int | None = None,
    alive_min: int | None = None,
) -> int:
    """Чистая функция перехода stable после одной проверки (режим «щита»).

    Правила:
      * УСПЕХ: сервер получает «щит» от удаления — stable выставляется не ниже
        SHIELD_CYCLES (по умолчанию 96): столько неудачных проверок подряд он
        ещё проживёт. Сервер считается пинговавшимся (ever_pinged=1);
      * НЕУДАЧА: stable -1, но не ниже (PURGE_STABLE_BELOW - 1). Дойдя до -1,
        сервер считается умершим и больше не импортируется в цикл проверки.

    Нижний порог stable = PURGE_STABLE_BELOW (0): в пул проверки попадают
    серверы со stable >= порога. Новый сервер до первого удачного пинга имеет
    stable = NEW_SERVER_STABLE (5) — у него есть несколько попыток доказать
    жизнеспособность, после чего он умирает.
    """
    shield = int(get_settings().core.shield_cycles if shield_cycles is None else shield_cycles)
    alive_min = int(get_settings().core.purge_stable_below if alive_min is None else alive_min)
    prev = int(prev_stable)
    if ok:
        return max(prev, shield)
    return max(prev - 1, alive_min - 1)


def _merge_tags(current: str, incoming: str, replace: Sequence[str] = ()) -> str:
    """Склеивает списки тэгов через запятую, сохраняя порядок и убирая дубли.

    Тэги из РАЗНЫХ чекеров дополняют друг друга: сервер может иметь и
    [gemini], и [speed-fast]. Но внутри одной категории тэг — это ОДИН
    из многих, и новый заменяет старый: без этого прогон, решивший сервер
    быстрым, оставлял рядом метку прошлого прогона "[speed-slow][speed-ok]".

    replace — префиксы категорий, которые новый тэг затирает ("speed-").
    """
    old_tags = [t.strip() for t in current.split(",") if t.strip()]
    new_tags = [t.strip() for t in incoming.split(",") if t.strip()]
    if replace:
        old_tags = [t for t in old_tags if not t.startswith(tuple(replace))]
    merged: list[str] = []
    for tag in old_tags + new_tags:
        if tag not in merged:
            merged.append(tag)
    return ",".join(merged)


class ServerStore:
    """Обёртка над SQLite-базой серверов.

    Короткие соединения на операцию + WAL-режим: базу безопасно читает Flask API,
    пока main.py/urltest пишет результаты проверки.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # WAL задаётся здесь, при создании магазина, и дальше просто
            # сохраняется в файле базы — см. комментарий в _connect().
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._ensure_ever_pinged_column(conn)
            # Нормализация накопленных значений: stable не бывает ниже
            # «мёртвой» отметки (нижний порог - 1).
            dead = self._dead_stable()
            conn.execute("UPDATE servers SET stable = ? WHERE stable < ?", (dead, dead))
        self._maybe_migrate_stable_zones()
        self._maybe_migrate_shield()
        self._maybe_migrate_legacy()

    # ------------------------------------------------------------------ util
    def _connect(self) -> sqlite3.Connection:
        """Открывает соединение.

        journal_mode=WAL выставляется ОДИН раз при создании магазина.
        Это DDL: он пишет заголовок базы и берёт блокировку схемы. Выполнять
        его на каждом открытии смысла нет, режим сохраняется в файле базы
        сам. Измерено: PRAGMA занимал 0.43 мс из 0.75 мс открытия, при
        4+ соединениях на батч это заметная накладная нагрузка.
        """
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _connect_managed(self) -> "contextlib.closing[sqlite3.Connection]":
        """Соединение, которое коммитит И закрывается.

        "with conn:" в sqlite3 только коммитит транзакцию и НЕ закрывает
        соединение — оно живёт до сборки мусора.
        """
        return contextlib.closing(self._connect())

    @staticmethod
    def _dead_stable() -> int:
        """Значение stable, при котором сервер считается умершим.

        Живые серверы имеют stable >= PURGE_STABLE_BELOW (нижний порог, 0);
        дойдя до порога - 1 (по умолчанию -1), сервер выпадает из цикла
        проверки и больше не импортируется из базы.
        """
        return int(get_settings().core.purge_stable_below) - 1

    @staticmethod
    def _ensure_ever_pinged_column(conn: sqlite3.Connection) -> None:
        """Миграция схемы: добавляет колонку ever_pinged в старую базу."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
        # CREATE TABLE IF NOT EXISTS не добавляет колонки в уже созданную
        # таблицу, поэтому новые поля приходится докувать на месте. Без этого
        # старая база падала бы на INSERT с «no such column».
        for name, decl in (
            ("ever_pinged", "INTEGER NOT NULL DEFAULT 0"),
            ("speed_mbps", "REAL"),
            ("speed_down", "REAL"),
            ("speed_up", "REAL"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE servers ADD COLUMN {name} {decl}")

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

    def server_records_by_key(self, lines: Iterable[str]) -> dict[str, dict]:
        """Ключ -> запись сервера для переданных строк подписки.

        Нужно расширенной фильтрации: страна, профиль целей, задержка и
        stable лежат в колонках базы, а не в самой строке. Читаются только
        запрошенные ключи, а не вся таблица.
        """
        keys = list(dict.fromkeys(self._key_of(ln) for ln in lines if ln))
        if not keys:
            return {}
        out: dict[str, dict] = {}
        with self._connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                for r in conn.execute(
                    "SELECT key, line, stable, ping_ms, capabilities, country, protocol "
                    f"FROM servers WHERE key IN ({placeholders})",
                    chunk,
                ):
                    out[r["key"]] = {
                        "key": r["key"],
                        "line": r["line"],
                        "stable": r["stable"],
                        "ping_ms": r["ping_ms"],
                        "capabilities": r["capabilities"] or "",
                        "country": r["country"] or "",
                        "protocol": r["protocol"] or "",
                    }
        return out

    def load_stable_for(self, keys: Sequence[str]) -> dict[str, int]:
        """Карта ключ -> stable ТОЛЬКО для перечисленных ключей.

        Диспетчеру при записи одного батча нужны значения stable handful'а
        серверов, но он читал всю таблицу. Инструментированный прогон
        показывал, что на этом уходила половина всего времени работы с БД:
        полный скан на каждый батч вместо точечного чтения по primary key.
        """
        keys = list(dict.fromkeys(k for k in keys if k))
        if not keys:
            return {}
        out: dict[str, int] = {}
        with self._connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT key, stable FROM servers WHERE key IN ({placeholders})",
                    chunk,
                ):
                    out[r["key"]] = r["stable"] or 0
        return out

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

        Новый сервер получает stable=NEW_SERVER_STABLE (по умолчанию 5) и
        попадает в ротацию проверки: до первого удачного пинга у него есть
        несколько попыток доказать жизнеспособность. Существующий обновляет
        last_seen (и line, если его ещё ни разу не проверяли), НЕ сбрасывая
        stable/ever_pinged и не снимая excluded: мёртвый сервер не воскресает
        от повторного появления в подписке.
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
                        "INSERT INTO servers"
                        " (key, line, stable, ever_pinged, first_seen, last_seen)"
                        " VALUES (?, ?, ?, 0, ?, ?)",
                        (key, clean, int(get_settings().core.new_server_stable), now, now),
                    )
        return {"added": added, "updated": updated, "skipped": skipped}

    def blacklist_unparsable(self, lines: Iterable[str]) -> int:
        """Отправляет неразбираемые строки в полноценный чс.

        Строка, из которой ни один парсер не может собрать узел, не
        проверяется вообще: она лишь занимает слот в батче проверки и засоряет
        лог. Помечаем её excluded=1 и stable = PURGE_STABLE_BELOW - 1 (умерший
        сервер) — тогда она выпадает из пула проверки (check_pool, фильтр
        stable >= PURGE_STABLE_BELOW) и попадает в blacklist.txt.

        Существующие серверы не теряются: обновляется только флаг/зона,
        строка (line) и страна остаются как были.

        Возвращает количество уникальных ключей, переведённых в чс.
        """
        now = _now()
        dead = self._dead_stable()
        unique: dict[str, str] = {}
        for raw in lines:
            clean = str(raw).strip()
            if not clean:
                continue
            key = self._key_of(clean)
            if not key:
                continue
            unique.setdefault(key, clean)
        if not unique:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO servers"
                " (key, line, stable, ever_pinged, excluded, available,"
                "  first_seen, last_seen)"
                " VALUES (?, ?, ?, 0, 1, 0, ?, ?)",
                [(key, line, dead, now, now) for key, line in unique.items()],
            )
            conn.executemany(
                "UPDATE servers SET excluded = 1, stable = ?, available = 0, last_seen = ?"
                " WHERE key = ?",
                [(dead, now, key) for key in unique],
            )
        return len(unique)

    def record_results(self, rows: Sequence[dict]) -> None:
        """Записывает результаты проверки пачкой (один commit на batch).

        row: {key, available: bool, line?: str, ping_ms?: int|None,
              country?: str, protocol?: str, capabilities?: str}

        Переход stable считает compute_next_state() («щит» от удаления):
          * успех  — stable не ниже SHIELD_CYCLES (по умолчанию 96), и сервер
            навсегда помечается ever_pinged=1 (он пинговался хотя бы раз);
          * неудача — stable -1, но не ниже PURGE_STABLE_BELOW - 1 (на -1
            сервер считается умершим и больше не импортируется).
        available запоминает результат ПОСЛЕДНЕЙ проверки — это критерий
        whitelist: пинганулся -> в whitelist, не пинганулся -> нет.
        """
        if not rows:
            return
        now = _now()
        with self._connect() as conn:
            keys = list({row["key"] for row in rows})
            prev: dict[str, int] = {}
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                for r in conn.execute(
                    "SELECT key, stable FROM servers "
                    f"WHERE key IN ({placeholders})",
                    chunk,
                ):
                    prev[r["key"]] = r["stable"] or 0
            # Один executemany вместо conn.execute на каждую строку:
            # 40 строк батча были 40 обращениями к базе вместо одного.
            # Сам UPSERT_RESULT_SQL уже рассчитан на пакетную вставку.
            params = []
            for row in rows:
                ok = bool(row.get("available"))
                new_stable = compute_next_state(prev.get(row["key"], 0), ok=ok)
                params.append((
                    row["key"],
                    row.get("line") or "",
                    new_stable,
                    1 if ok else 0,
                    row.get("ping_ms"),
                    row.get("protocol") or "",
                    row.get("country") or "",
                    row.get("capabilities") or "",
                    _as_float(row.get("speed_mbps")),
                    _as_float(row.get("speed_down")),
                    _as_float(row.get("speed_up")),
                    1 if ok else 0,
                    0 if ok else 1,
                    now, now, now,
                ))
            conn.executemany(UPSERT_RESULT_SQL, params)

    def record_enrichment(self, rows: Sequence[dict]) -> int:
        """Обновляет ТОЛЬКО обогащение: страна и профиль достижимости.

        Отдельный метод нужен потому, что страна и reachability считаются
        отдельным проходом уже ПОСЛЕ того, как сервер признан живым. Если
        слать их через record_results, stable пересчитался бы второй раз за
        тот же цикл проверки и сервер получал бы и за живого, и за мёртвого
        два изменения подряд.

        Пустые значения не затирают уже известные.

        capabilities — это ОБЪЕДИНЕНИЕ, а не замена: чекеров-дополнений
        несколько (профиль достижимости, категория скорости), и они пишут
        в одну и ту же колонку по очереди. Если бы второй затирал первого,
        тэг [gemini] исчезал бы там, где появился [speed-fast].
        Возвращает количество обновлённых серверов.
        """
        if not rows:
            return 0
        updated = 0
        now = _now()
        with self._connect() as conn:
            for row in rows:
                country = row.get("country") or ""
                capabilities = row.get("capabilities") or ""
                speed = (
                    _as_float(row.get("speed_mbps")),
                    _as_float(row.get("speed_down")),
                    _as_float(row.get("speed_up")),
                )
                if not country and not capabilities and not any(s is not None for s in speed):
                    continue
                if capabilities:
                    current = conn.execute(
                        "SELECT capabilities FROM servers WHERE key = ?",
                        (row["key"],),
                    ).fetchone()
                    capabilities = _merge_tags(
                        (current["capabilities"] if current else "") or "",
                        capabilities,
                        replace=row.get("capabilities_replace") or (),
                    )
                cursor = conn.execute(
                    """
                    UPDATE servers SET
                        country = CASE WHEN ? <> '' THEN ? ELSE country END,
                        capabilities = CASE WHEN ? <> '' THEN ? ELSE capabilities END,
                        speed_mbps = COALESCE(?, speed_mbps),
                        speed_down = COALESCE(?, speed_down),
                        speed_up = COALESCE(?, speed_up),
                        last_seen = ?
                    WHERE key = ?
                    """,
                    (country, country, capabilities, capabilities,
                     speed[0], speed[1], speed[2], now, row["key"]),
                )
                updated += cursor.rowcount or 0
        return updated

    # ----------------------------------------------------------- запросы DB
    def check_pool(self) -> list[sqlite3.Row]:
        """Пул проверки: ВСЕ живые серверы со stable >= PURGE_STABLE_BELOW.

        Живым считается сервер, не исключённый (excluded=0) и имеющий stable
        не ниже нижнего порога (по умолчанию 0). Умершие (stable = -1) в пул
        не попадают и больше не импортируются из базы. Лучшие (высокий stable,
        низкий пинг) — первыми.
        """
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT key, line, stable, ping_ms, available FROM servers
                WHERE excluded = 0 AND stable >= ?
                ORDER BY stable DESC, ping_ms IS NULL, ping_ms ASC, key ASC
                """,
                (int(get_settings().core.purge_stable_below),),
            ).fetchall()

    def export_lines(
        self,
        *,
        min_stable: int | None = None,
        max_stable: int | None = None,
        limit: int | None = None,
        only_available: bool = False,
    ) -> list[str]:
        """Строки серверов по фильтру.

        min_stable=1 -> только stable > 1 (строго больше);
        max_stable=-1 -> только stable < -1 (строго меньше).
        only_available=True -> только серверы, ПИНГОВАВШИЕСЯ в последней
        проверке (available=1) — это критерий whitelist.
        Сортировка: stable DESC, затем пинг по возрастанию (без пинга — в конце).
        """
        query = "SELECT line, stable, ping_ms, capabilities FROM servers WHERE line != ''"
        params: list[object] = []
        if only_available:
            query += " AND available = 1 AND excluded = 0"
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
        query = query.replace(
            "SELECT line, stable, ping_ms, capabilities FROM servers",
            "SELECT line, stable, ping_ms, capabilities, country FROM servers",
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            set_country_in_name(row["line"], row["country"] or "")
            for row in rows
        ]

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
                            global_tag: str = "Global",
                            only_available: bool = False,
                            server_filter=None,
                            best_top: int = 0) -> list[str]:
        """Строки серверов по фильтру с добавленными capability-тэгами.

        ВАЖНО: capability-профиль ХРАНИТСЯ в БД, тэги [name] добавляются
        только здесь, на этапе экспорта для генерации итогового конфига.
        Сервер, у которого профиль полностью состоит из global_tag (достиг
        всех целей), получает ровно один тэг [Global].
        """
        query = ("SELECT line, key, stable, ping_ms, capabilities, country, "
                "speed_mbps, speed_down, speed_up FROM servers WHERE line != ''")
        params: list[object] = []
        if only_available:
            query += " AND available = 1 AND excluded = 0"
        if min_stable is not None:
            query += " AND stable > ?"
            params.append(min_stable)
        if max_stable is not None:
            query += " AND stable < ?"
            params.append(max_stable)
        query += " ORDER BY stable DESC, ping_ms IS NULL, ping_ms ASC, key ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        if server_filter is not None:
            rows = [
                r for r in rows
                if server_filter.matches({
                    "line": r["line"],
                    "stable": r["stable"],
                    "ping_ms": r["ping_ms"],
                    "capabilities": r["capabilities"] or "",
                    "country": r["country"] or "",
                    "protocol": "",
                })
            ]
        # Метки «best»: в группе одного профиля лучшие получают
        # дополнительный тег. Считается ПОСЛЕ фильтра, иначе фильтр по
        # профилю вырезал бы часть группы и «лучшим» оказался бы не тот.
        best_by_key: dict[str, str] = {}
        if best_top:
            from script.best_tags import BestRules, add_best_tags

            # Теги скорости не ранжируются: там «лучший» — это и есть весь
            # смысл тега, а из двух одинаковых выберется случайный.
            skip = tuple(sorted({
                s.strip()
                for r in rows
                for s in (r["capabilities"] or "").split(",")
                if s.strip().startswith("speed-")
            }))
            marked = add_best_tags(
                [dict(r) for r in rows], rules=BestRules(top=best_top),
                skip_profiles=skip,
            )
            for rec in marked:
                base_caps = rec.get("capabilities") or ""
                best_by_key[rec["key"]] = rec.get("capabilities") or base_caps

        out = []
        for row in rows:
            # Флаг страны — источник истины в колонке country: страна
            # определяется ПОСЛЕ записи строки в базу, так что в самой строке
            # её может не быть, и фильтр по странам такую строку не находит.
            base = set_country_in_name(row["line"], row["country"] or "")
            caps = best_by_key.get(row["key"]) or row["capabilities"] or ""
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
        only_available: bool = False,
    ) -> int:
        lines = self.export_lines(
            min_stable=min_stable, max_stable=max_stable, only_available=only_available,
        )
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
        only_available: bool = False,
    ) -> int:
        """Экспорт строк С capability-тэгами ([name] / [Global]) в файл.

        Это «whitelist для генерации»: тэги добавляются только здесь, на этапе
        выгрузки строк, чтобы итоговый sing-box конфиг мог фильтровать outbound'ы
        по достижимости целей. Профиль при этом остаётся в БД, а не в merge-пуле.
        """
        lines = self.export_tagged_lines(
            min_stable=min_stable, max_stable=max_stable,
            global_tag=global_tag, only_available=only_available,
        )
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        return len(lines)

    def export_whitelist_lines(self, *, global_tag: str = "Global",
                               server_filter=None, best_top: int = 0) -> list[str]:
        """Whitelist-строки: серверы, ПИНГОВАВШИЕСЯ в последней проверке.

        Критерий нового принципа: available=1 (последняя проверка успешна) —
        без порога stable. Не пинганулся -> не добавляется, но остаётся в пуле
        проверки. Тэги [name] / [Global] добавляются здесь же.

        server_filter — опциональный ServerFilter (script.node_filters):
        отбор по capabilities, стране, региону, задержке, stable и протоколу.
        Применяется ДО того, как строка станет outbound, потому что страна и
        профиль лежат в колонках базы, а в самой строке подписки их может
        не быть вовсе.
        """
        return self.export_tagged_lines(
            only_available=True, global_tag=global_tag,
            server_filter=server_filter, best_top=best_top,
        )

    def export_whitelist_to_file(self, path: str | Path, *, global_tag: str = "Global") -> int:
        """Whitelist = available=1 (последняя проверка успешна), в файл."""
        return self.export_tagged_to_file(path, only_available=True, global_tag=global_tag)

    # --------------------------------------------------- удаление из проверки
    def purge_dead(self, below: int | None = None, *, hard: bool = False) -> int:
        """Исключает серверы со stable < below из проверочного списка.

        По умолчанию below = PURGE_STABLE_BELOW (0): исключаются умершие
        серверы со stable = -1. hard=False выставляет excluded=1 — запись
        остаётся в базе для статистики и повторного включения вручную;
        hard=True физически удаляет.
        """
        if below is None:
            below = int(get_settings().core.purge_stable_below)
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
        """Возвращает исключённые серверы в ротацию, выдавая «щит».

        Пинговавшийся когда-либо сервер (ever_pinged=1) получает полный щит
        SHIELD_CYCLES, новый — NEW_SERVER_STABLE.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE servers SET
                    excluded = 0,
                    stable = CASE WHEN ever_pinged = 1 THEN ? ELSE ? END
                WHERE excluded = 1
                """,
                (int(get_settings().core.shield_cycles), int(get_settings().core.new_server_stable)),
            )
            return cur.rowcount

    def stats(self) -> dict:
        alive_min = int(get_settings().core.purge_stable_below)
        with self._connect() as conn:
            total, active, excluded = conn.execute(
                "SELECT COUNT(*), SUM(excluded = 0), SUM(excluded = 1) FROM servers"
            ).fetchone()
            zones = {
                # Живые: stable >= нижнего порога — импортируются в цикл проверки.
                "alive": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable >= ?", (alive_min,)
                ).fetchone()[0],
                # Умершие: stable < порога (по умолчанию -1) — не импортируются.
                "dead": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable < ?", (alive_min,)
                ).fetchone()[0],
                # Хотя бы раз пинговались -> был/есть «щит» от удаления.
                "shielded": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE ever_pinged = 1"
                ).fetchone()[0],
                # Пинганулся в последней проверке -> в whitelist.
                "online": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable >= ? AND available = 1",
                    (alive_min,),
                ).fetchone()[0],
                # Не пинганулся в последней проверке -> вне whitelist, в запасе.
                "reserve": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable >= ? AND available = 0",
                    (alive_min,),
                ).fetchone()[0],
                # Ещё ни разу не проверялись.
                "unchecked": conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE stable >= ? AND available IS NULL",
                    (alive_min,),
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

    def _maybe_migrate_stable_zones(self) -> None:
        """Однократный возврат «старых» исключённых серверов в ротацию.

        Старые политики исключали сервер уже после первого провала (stable < 0)
        или по серии неудач (временный чс). Теперь из проверки исключаются
        только серверы ниже нижнего порога (stable < PURGE_STABLE_BELOW), поэтому
        старые excluded-записи со stable >= порога возвращаются в пул один раз.
        Маркер рядом с базой защищает от повторного запуска.
        """
        marker = self.db_path.with_suffix(".tempban-migrated.json")
        if marker.exists():
            return
        with self._connect() as conn:
            revived = conn.execute(
                "UPDATE servers SET excluded = 0 "
                "WHERE excluded = 1 AND stable >= ?",
                (int(get_settings().core.purge_stable_below),),
            ).rowcount
        marker.write_text(
            json.dumps({"revived": revived, "purge_below": int(get_settings().core.purge_stable_below)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _maybe_migrate_shield(self) -> None:
        """Однократная инициализация ever_pinged для старой базы.

        Историю пингов задним числом взять неоткуда, поэтому считаем, что
        сервер пинговался, если последняя проверка успешна (available=1) или
        он накопил положительный stable (> 0). Такие живые серверы сразу
        получают полный щит SHIELD_CYCLES; умершие не воскрешаются.
        """
        marker = self.db_path.with_suffix(".shield-migrated.json")
        if marker.exists():
            return
        with self._connect() as conn:
            marked = conn.execute(
                "UPDATE servers SET ever_pinged = 1 "
                "WHERE ever_pinged = 0 AND (available = 1 OR stable > 0)"
            ).rowcount
            shielded = conn.execute(
                "UPDATE servers SET stable = MAX(stable, ?) "
                "WHERE ever_pinged = 1 AND stable >= ?",
                (int(get_settings().core.shield_cycles), int(get_settings().core.purge_stable_below)),
            ).rowcount
        marker.write_text(
            json.dumps({"ever_pinged": marked, "shielded": shielded}, ensure_ascii=False),
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
        # Whitelist — серверы, пинговавшиеся в прошлой жизни: полный щит и
        # ever_pinged=1. Blacklist — умершие (ниже нижнего порога).
        stable = self._dead_stable() if excluded else int(get_settings().core.shield_cycles)
        ever_pinged = 0 if excluded else 1
        available = 0 if excluded else 1
        with self._connect() as conn:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                clean = raw.strip()
                if not clean:
                    continue
                key = self._key_of(clean)
                if not key:
                    continue
                cur = conn.execute(
                    verb + " INTO servers"
                    " (key, line, stable, ever_pinged, excluded, available,"
                    "  first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, clean, stable, ever_pinged, 1 if excluded else 0,
                     available, now, now),
                )
                inserted += cur.rowcount
        return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Central server store CLI")
    parser.add_argument("--db", default=get_settings().paths.servers_db_file)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats")
    p_export = sub.add_parser("export")
    p_export.add_argument("--min-stable", type=int, default=None)
    p_export.add_argument("--max-stable", type=int, default=None)
    p_export.add_argument("--limit", type=int, default=None)
    p_export.add_argument("--out", default=None)
    p_purge = sub.add_parser("purge")
    p_purge.add_argument("--below", type=int, default=int(get_settings().core.purge_stable_below))
    p_purge.add_argument("--hard", action="store_true")
    sub.add_parser("reset-excluded")

    args = parser.parse_args()
    store = ServerStore(args.db)

    if args.cmd == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "export":
        # Без явных фильтров экспортируем whitelist: серверы, пинговавшиеся
        # в последней проверке (available=1).
        only_available = args.min_stable is None and args.max_stable is None
        if args.out:
            n = store.export_to_file(
                args.out, min_stable=args.min_stable, max_stable=args.max_stable,
                only_available=only_available,
            )
            print(f"Exported {n} lines -> {args.out}")
        else:
            lines = store.export_lines(
                min_stable=args.min_stable, max_stable=args.max_stable,
                limit=args.limit, only_available=only_available,
            )
            print("\n".join(lines))
    elif args.cmd == "purge":
        affected = store.purge_dead(args.below, hard=args.hard)
        action = "Deleted" if args.hard else "Excluded"
        print(f"{action} {affected} servers with stable < {args.below}")
    elif args.cmd == "reset-excluded":
        revived = store.reset_excluded()
        print(f"Returned {revived} servers to check rotation (state reset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
