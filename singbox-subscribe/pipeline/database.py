"""Доступ всех узлов pipeline к центральной базе.

Обёртка над ``script.server_store.ServerStore`` (там живёт логика stable и
«щита» от удаления — её не дублируем), к которой добавлены:

  * таблица ``check_queue`` — ОЧЕРЕДЬ ПРОВЕРКИ. Она в базе, а не в памяти,
    поэтому переживает перезапуск, её видно через CLI и HTTP API, и её можно
    пополнять из разных этапов;
  * асинхронные методы: обычный ``await db.xxx()``. SQLite — синхронный, поэтому
    запросы уходят в пул потоков и не блокируют event loop; запись дополнительно
    сериализуется замком, чтобы параллельные батчи не ловили SQLITE_BUSY.

Кто что делает с базой:

  * поиск (discovery) — ``upsert_lines()` + ``enqueue()`: расширяет базу новыми
    серверами из ссылок и ставит их в очередь;
  * проверка (check) — ``refill_from_pool()` забирает живой пул из базы по
    фильтрам stable, ``claim_batch()` раздаёт батчи воркерам, ``record_results()`
    перезаписывает базу по результатам тестов;
  * экспорт — ``export_whitelist()`` — единственная файловая выгрузка:
    ``blacklist.txt``/``merge.txt`` не пишутся, их состояние живёт в базе.
"""

from __future__ import annotations

import json

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from config.settings import setting
from pipeline.logging_setup import get_logger

LOGGER = get_logger("database")


def _key_of(raw: str) -> str:
    """Ключ строки прокси — так же, как в самой очереди."""
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(raw)

QUEUE_PENDING = "pending"
QUEUE_IN_PROGRESS = "in_progress"
QUEUE_DONE = "done"
QUEUE_FAILED = "failed"

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_queue (
    key         TEXT PRIMARY KEY,
    line        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    source      TEXT NOT NULL DEFAULT 'pool',
    attempts    INTEGER NOT NULL DEFAULT 0,
    enqueued_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_check_queue_status ON check_queue(status);
CREATE INDEX IF NOT EXISTS idx_check_queue_source ON check_queue(source);
"""

#: Источники постановки в очередь (для отчётов и фильтров в CLI/API).
SOURCE_POOL = "pool"
SOURCE_DISCOVERY = "discovery"
SOURCE_RETRY = "retry"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Очередь результатов: служба проверки сюда КЛАДЁТ вердикты, отдельная
#: служба записи забирает и переносит их в таблицу серверов. В обычном
#: прогоне очередь не используется — там запись идёт сразу (DirectSink).
RESULT_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS result_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    applied     INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
)
"""

#: Индекс для claim_results(), который служба записи дёргает каждые 2 с.
#: Без него идёт полный скан таблицы; с ним выборка идёт по индексу
#: (измерено: 4.77 мс -> 0.16 мс на 5000 строк, в 30 раз).
RESULT_QUEUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_result_queue_applied
    ON result_queue(applied, id)
"""

#: Сколько дней хранить уже разобранные порции. applied=1 строки нужны
#: только для разбора ошибок; без чистки таблица растёт всю жизнь службы.
RESULT_QUEUE_KEEP_DAYS = 3


@dataclass(slots=True)
class QueueItem:
    """Одна строка прокси, ожидающая проверки."""

    key: str
    line: str
    source: str = SOURCE_POOL
    attempts: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QueueItem":
        return cls(
            key=row["key"], line=row["line"],
            source=row["source"], attempts=row["attempts"],
        )


@dataclass(slots=True)
class ResultBatch:
    """Порция вердиктов, ждущая записи в таблицу серверов.

    payload хранится как JSON: подробности батча нужны только службе
    записи, а разбирать их в SQL не требуется.
    """

    id: int
    payload: dict[str, Any]

    @property
    def rows(self) -> list[dict]:
        return list(self.payload.get("rows") or [])

    @property
    def retry(self) -> list[str]:
        return list(self.payload.get("retry") or [])

    @property
    def unparsable(self) -> list[str]:
        return list(self.payload.get("unparsable") or [])

    @property
    def completed(self) -> list[str]:
        """Ключи строк, снятых с работы."""
        return list(self.payload.get("completed") or [])


@dataclass
class QueueStats:
    pending: int = 0
    in_progress: int = 0
    done: int = 0
    failed: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.pending + self.in_progress + self.done + self.failed

    def as_dict(self) -> dict:
        return {
            "pending": self.pending,
            "in_progress": self.in_progress,
            "done": self.done,
            "failed": self.failed,
            "total": self.total,
            **self.extra,
        }


def read_queue_stats(db_path: str | Path | None = None) -> QueueStats:
    """Счётчики очереди одним коротким соединением.

    Для синхронных потребителей (HTTP API, CLI), где ради одного SELECT не
    нужно поднимать пул потоков. Если таблицы очереди ещё нет (база ещё не
    открывалась ядром) — возвращаются нули, а не ошибка.
    """
    path = Path(db_path or setting("SERVERS_DB_FILE"))
    stats = QueueStats()
    if not path.exists():
        return stats
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM check_queue GROUP BY status"
        ).fetchall()
    except sqlite3.OperationalError:
        return stats
    finally:
        conn.close()

    counters = {
        QUEUE_PENDING: "pending",
        QUEUE_IN_PROGRESS: "in_progress",
        QUEUE_DONE: "done",
        QUEUE_FAILED: "failed",
    }
    for row in rows:
        field_name = counters.get(row["status"])
        if field_name:
            setattr(stats, field_name, row["n"])
    return stats


class Database:
    """Асинхронная обёртка над центральной базой и очередью проверки."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        executor_workers: int = 4,
        shield_cycles: int | None = None,
    ) -> None:
        # Импорт внутри конструктора: server_store тянет настройки, а он не
        # должен импортировать pipeline (иначе циклическая зависимость).
        from script.server_store import ServerStore

        self.path = Path(db_path or setting("SERVERS_DB_FILE"))
        self._store = ServerStore(self.path)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, executor_workers),
            thread_name_prefix="db",
        )
        # Одна запись за раз: SQLite допускает одного писателя, а батчи идут
        # параллельно. Замок не даёт получать SQLITE_BUSY.
        self._write_lock = asyncio.Lock()
        self._purge_below = int(setting("PURGE_STABLE_BELOW"))
        self._shield_cycles = int(
            setting("SHIELD_CYCLES") if shield_cycles is None else shield_cycles,
        )
        self._ensure_queue_schema()

    # ------------------------------------------------------------------ util
    def _ensure_queue_schema(self) -> None:
        """Создаёт обе очереди и их индексы — один раз на процесс.

        Раньше схема result_queue выполнялась заново в КАЖДОЙ операции с ней;
        это лишний DDL-разбор и лишняя блокировка схемы каждые 2 секунды.
        """
        with self._store._connect() as conn:  # noqa: SLF001 — свой же класс обёртки
            conn.executescript(QUEUE_SCHEMA)
            conn.executescript(RESULT_QUEUE_TABLE)
            conn.executescript(RESULT_QUEUE_INDEX)

    async def _run(self, func, *args, **kwargs):
        """Выполняет блокирующий вызов в пуле потоков."""
        loop = asyncio.get_running_loop()
        if kwargs:
            from functools import partial

            func = partial(func, **kwargs)
        return await loop.run_in_executor(self._executor, func, *args)

    async def _write(self, func, *args, **kwargs):
        """Выполняет ЗАПИСЬ под замком (один писатель на всю базу)."""
        async with self._write_lock:
            return await self._run(func, *args, **kwargs)

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------- регистрация
    async def upsert_lines(self, lines: Iterable[str]) -> dict[str, int]:
        """Регистрирует серверы из ссылок. Новый stable = NEW_SERVER_STABLE."""
        return await self._write(self._store.upsert_lines, list(lines))

    async def blacklist_unparsable(self, lines: Iterable[str]) -> int:
        """Убирает из ротации строки, которые нечем проверять."""
        return await self._write(self._store.blacklist_unparsable, list(lines))

    # ------------------------------------------------------- очередь
    async def enqueue(self, lines: Iterable[str], *, source: str = SOURCE_POOL) -> int:
        """Ставит серверы в очередь проверки. Повторная постановка обновляет line."""
        from script.downloader import normalize_proxy_key

        now = _now()
        payload: list[tuple] = []
        for raw in lines:
            clean = str(raw).strip()
            if not clean:
                continue
            key = normalize_proxy_key(clean)
            if not key:
                continue
            payload.append((key, clean, QUEUE_PENDING, source, 0, now, now))
        if not payload:
            return 0
        return await self._write(self._enqueue_sync, payload)

    def _enqueue_sync(self, payload: list[tuple]) -> int:
        with self._store._connect() as conn:
            # Уже проверенные в этом же прогоне переводим обратно в pending,
            # чтобы новый цикл их перепроверил, а не пропустил молча.
            cur = conn.executemany(
                """
                INSERT INTO check_queue
                    (key, line, status, source, attempts, enqueued_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    line       = excluded.line,
                    -- Повтор обязан вернуть строку в очередь: пока она висит
                    -- в in_progress, её никто не возьмёт, батч-то отработал.
                    status     = CASE WHEN excluded.source = 'retry' THEN 'pending'
                                      WHEN check_queue.status = 'done' THEN 'pending'
                                      ELSE check_queue.status END,
                    source     = excluded.source,
                    attempts   = CASE WHEN check_queue.status = 'done' THEN 0
                                      ELSE check_queue.attempts
                                           + (CASE WHEN excluded.source = 'retry'
                                                   THEN 1 ELSE 0 END) END,
                    updated_at = excluded.updated_at,
                    last_error = NULL
                """,
                payload,
            )
            return cur.rowcount

    async def refill_from_pool(
        self, *, limit: int | None = None, min_stable: int | None = None,
    ) -> int:
        """Наполняет очередь живым пулом из базы.

        Именно это «собирает по фильтрам из базы список на проверку»: берутся
        серверы с ``excluded=0`` и ``stable >= min_stable`` (по умолчанию
        PURGE_STABLE_BELOW), лучшие — высокий stable, малый пинг — первыми.
        """
        threshold = int(self._purge_below if min_stable is None else min_stable)
        pool = await self._run(self._store.check_pool)
        lines = [row["line"] for row in pool if row["line"] and row["stable"] >= threshold]
        if limit is not None:
            lines = lines[:limit]
        if not lines:
            return 0
        return await self.enqueue(lines, source=SOURCE_POOL)

    async def claim_batch(self, size: int) -> list[QueueItem]:
        """Забирает до ``size`` строк из очереди и помечает их как взятые в работу.

        Одно действие под BEGIN IMMEDIATE: два воркера не получат одну строку.
        """
        if size <= 0:
            return []
        return await self._write(self._claim_batch_sync, size)

    def _claim_batch_sync(self, size: int) -> list[QueueItem]:
        now = _now()
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT key, line, source, attempts FROM check_queue
                    WHERE status = ?
                    ORDER BY attempts ASC, enqueued_at ASC, key ASC
                    LIMIT ?
                    """,
                    (QUEUE_PENDING, size),
                ).fetchall()
                if not rows:
                    conn.commit()
                    return []
                keys = [(row["key"], now) for row in rows]
                conn.executemany(
                    f"UPDATE check_queue SET status = ?, attempts = attempts + 1, "
                    f"updated_at = ? WHERE key = ?",
                    [(QUEUE_IN_PROGRESS, updated, key) for key, updated in keys],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return [QueueItem.from_row(row) for row in rows]

    async def complete_batch(self, items: Sequence[QueueItem], *, error: str | None = None,
                         skip_keys: Sequence[str] = ()) -> None:
        """Снимает батч с работы: done — или failed, если был сбой.

        skip_keys — строки, которые уже вернули в очередь на повтор. Их нельзя
        закрывать как сделанные: иначе повтор тут же затирается, очередь
        выглядит пустой, и серверы молча теряются.
        """
        skip = set(skip_keys)
        now = _now()
        status = QUEUE_FAILED if error else QUEUE_DONE
        payload = [
            (status, now, error or "", item.key)
            for item in items
            if item.key not in skip
        ]
        if not payload:
            return
        await self._write(self._complete_batch_sync, payload)

    def _complete_batch_sync(self, payload: list[tuple]) -> None:
        with self._store._connect() as conn:
            conn.executemany(
                "UPDATE check_queue SET status = ?, updated_at = ?, last_error = ? "
                "WHERE key = ?",
                payload,
            )

    async def requeue_stale(self, *, statuses: Sequence[str] = (QUEUE_IN_PROGRESS, QUEUE_FAILED)) -> int:
        """Возвращает в pending всё, что осталось в работе после падения.

        Нужен при старте: процесс мог быть убит посреди батча, и без этого
        строки навсегда залипли бы в in_progress.
        """
        marks = ",".join("?" for _ in statuses)
        return await self._write(
            self._requeue_stale_sync, list(statuses), _now(), f"status IN ({marks})",
        )

    def _requeue_stale_sync(self, statuses: list[str], now: str, clause: str) -> int:
        with self._store._connect() as conn:
            cur = conn.execute(
                f"UPDATE check_queue SET status = ?, updated_at = ? WHERE {clause}",
                [QUEUE_PENDING, now, *statuses],
            )
            return cur.rowcount

    async def queue_stats(self) -> QueueStats:
        rows = await self._run(self._queue_stats_sync)
        stats = QueueStats()
        for row in rows:
            if row["status"] == QUEUE_PENDING:
                stats.pending = row["n"]
            elif row["status"] == QUEUE_IN_PROGRESS:
                stats.in_progress = row["n"]
            elif row["status"] == QUEUE_DONE:
                stats.done = row["n"]
            elif row["status"] == QUEUE_FAILED:
                stats.failed = row["n"]
        return stats

    def _queue_stats_sync(self) -> list[sqlite3.Row]:
        with self._store._connect() as conn:
            return conn.execute(
                "SELECT status, COUNT(*) AS n FROM check_queue GROUP BY status"
            ).fetchall()

    async def clear_queue(self, *, statuses: Sequence[str] = (QUEUE_DONE,)) -> int:
        """Очищает очередь (по умолчанию — убранные проверенные)."""
        marks = ",".join("?" for _ in statuses)
        return await self._write(
            self._clear_queue_sync, list(statuses), f"status IN ({marks})",
        )

    def _clear_queue_sync(self, statuses: list[str], clause: str) -> int:
        with self._store._connect() as conn:
            cur = conn.execute(f"DELETE FROM check_queue WHERE {clause}", statuses)
            return cur.rowcount

    # --------------------------------------------------------- результаты
    async def record_enrichment(self, rows: Sequence[dict]) -> int:
        """Дописывает страну и профиль, НЕ трогая stable и available.

        Страна и reachability считаются отдельным проходом по уже живым
        серверам. Отправлять их в record_results нельзя: там пересчитывается
        stable, и сервер получил бы второе изменение за тот же цикл.
        """
        if not rows:
            return 0
        return await self._run(self._store.record_enrichment, list(rows))

    async def record_results(self, rows: Sequence[dict]) -> None:
        """Перезаписывает базу по результатам проверки батча.

        Переход stable (успех/провал, «щит») считает ServerStore — это
        единственное место, где живёт такая логика.
        """
        if not rows:
            return
        await self._write(self._store.record_results, list(rows))

    # ------------------------------------------------- очередь вердиктов
    async def push_result(self, payload: dict) -> int:
        """Кладёт порцию вердиктов в очередь записи.

        Служба проверки вызывает это вместо прямой записи в базу: вердикт
        считается уже тогда, когда sing-box ответил, а перенос в таблицу
        серверов делает отдельная служба — проверка не ждёт записи.
        """
        blob = json.dumps(payload, ensure_ascii=False)
        return await self._write(self._push_result_sync, _now(), blob)

    def _push_result_sync(self, now: str, blob: str) -> int:
        with self._store._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO result_queue (created_at, payload) VALUES (?, ?)",
                (now, blob),
            )
            return int(cursor.lastrowid or 0)

    async def claim_results(self, limit: int = 32) -> list:
        """Забирает неприменённые порции вердиктов (FIFO)."""
        rows = await self._run(self._claim_results_sync, int(limit))
        out: list[ResultBatch] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                LOGGER.warning("Порция вердиктов #%s повреждена, пропускаю", row["id"])
                continue
            out.append(ResultBatch(id=int(row["id"]), payload=payload))
        return out

    def _claim_results_sync(self, limit: int) -> list:
        with self._store._connect() as conn:
            return conn.execute(
                "SELECT id, payload FROM result_queue WHERE applied = 0 "
                "ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()

    async def purge_old_results(self, *, keep_days: int | None = None) -> int:
        """Удаляет давно разобранные порции. Вернёт, сколько убрано.

        Порция с applied=1 нужна только чтобы посмотреть, что было, если
        запись упала. В режиме служб живёт она днями, и без чистки каждый
        следующий claim_results() скан��ит всё накопившееся.
        """
        days = RESULT_QUEUE_KEEP_DAYS if keep_days is None else max(0, int(keep_days))

        def _purge_sync() -> int:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).isoformat(timespec="seconds")
            with self._store._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM result_queue WHERE applied = 1 AND created_at < ?",
                    (cutoff,),
                )
                return int(cur.rowcount or 0)

        return await self._write(_purge_sync)


    async def result_stats(self) -> dict:
        """Сколько вердиктов ждёт записи."""
        def _count_sync() -> list:
            with self._store._connect() as conn:
                return conn.execute(
                    "SELECT "
                    "COALESCE(SUM(CASE WHEN applied = 0 THEN 1 ELSE 0 END), 0) AS pending, "
                    "COALESCE(SUM(CASE WHEN error != '' THEN 1 ELSE 0 END), 0) AS failed "
                    "FROM result_queue"
                ).fetchall()

        rows = await self._run(_count_sync)
        row = rows[0] if rows else {"pending": 0, "failed": 0}
        return {"pending": int(row["pending"]), "failed": int(row["failed"])}

    async def apply_result(self, batch: "ResultBatch", error: str = "") -> None:
        """Переносит вердикты в базу и убирает их из очереди.

        Порядок повторяет прямую запись: сначала результаты, потом возврат
        в очередь молчавших серверов, потом чёрный список, и только потом
        снятие батча с работы — обязательно с skip_keys, иначе возврат на
        повтор тут же затрётся статусом done и повтор пропадёт.
        """
        rows = batch.rows
        if rows:
            await self.record_results(rows)
        if batch.retry:
            await self.enqueue(batch.retry, source=SOURCE_RETRY)
        if batch.unparsable:
            await self.blacklist_unparsable(batch.unparsable)
        if batch.completed:
            items = [QueueItem(key=key, line="") for key in batch.completed]
            skip = {_key_of(line) for line in batch.retry}
            await self.complete_batch(items, skip_keys=skip)
        await self._write(self._drop_result_sync, batch.id)

    async def mark_result_failed(self, batch_id: int, error: str) -> None:
        """Помечает порцию как разобранную, но с ошибкой.

        Порция остаётся в таблице для разбора, но в очередь не возвращается:
        иначе одна битая порция перебиралась бы вечно и заблокировала бы
        запись нормальных вердиктов за ней.
        """
        await self._write(self._fail_result_sync, batch_id, error[:500])

    def _fail_result_sync(self, batch_id: int, error: str) -> int:
        with self._store._connect() as conn:
            cursor = conn.execute(
                "UPDATE result_queue SET applied = 1, error = ? WHERE id = ?",
                (error, batch_id),
            )
            return cursor.rowcount or 0

    def _drop_result_sync(self, batch_id: int) -> int:
        with self._store._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM result_queue WHERE id = ?", (batch_id,),
            )
            return cursor.rowcount or 0

    # ------------------------------------------------------------ запросы
    # Имена load_* повторяют ServerStore, чтобы чекерам не приходилось знать,
    # как устроена обёртка.
    async def load_stable_map(self) -> dict[str, int]:
        """key -> stable для всех серверов (нужно для переходов в чек-стадии)."""
        return await self._run(self._store.load_stable_map)

    async def load_stable_for(self, keys: Iterable[str]) -> dict[str, int]:
        """key -> stable только для перечисленных ключей (быстрый путь)."""
        return await self._run(self._store.load_stable_for, list(keys))

    async def load_country_map(self) -> dict[str, str]:
        """key -> уже известная страна: не проверяем заново то, что определено."""
        return await self._run(self._store.load_country_map)

    async def load_capabilities_map(self) -> dict[str, str]:
        """key -> профиль достижимости."""
        return await self._run(self._store.load_capabilities_map)

    # Короткие псевдонимы — читаемее в коде этапов.
    stable_map = load_stable_map
    country_map = load_country_map

    async def stats(self) -> dict:
        return await self._run(self._store.stats)

    async def purge_dead(self, below: int | None = None) -> int:
        return await self._write(self._store.purge_dead, below)

    async def export_whitelist(self, path: str | Path, *, global_tag: str = "Global") -> int:
        return await self._run(
            self._store.export_whitelist_to_file, Path(path), global_tag=global_tag,
        )

    @property
    def store(self):
        """Доступ к низкоуровневому ServerStore (CLI, API, миграции)."""
        return self._store
