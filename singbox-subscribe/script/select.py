# -*- coding: utf-8 -*-
"""Выборка серверов из центральной базы ТОЛЬКО НА ЧТЕНИЕ.

Зачем модуль: блок "filter" в шаблоне заменяется блоком "sql". Скрипт
выполняет SQL-запрос, забирает из результата имена и подставляет их вместо
фильтра. Всё. Никакой магии.

    "sql": "SELECT line FROM servers WHERE available = 1 AND stable >= 0"

Запрос может содержать плейсхолдер "alldeploy": перед выполнением он
заменяется меткой сервиса (ровно как в filter), поэтому один и тот же
шаблон работает на все метки сразу.

Что скрипт берёт из результата:
  * колонка tag  — значение как есть;
  * колонка line — имя тега из URI, то есть фрагмент после '#';
  * колонка key  — значение как есть.

Права только на чтение: соединение открывается в режиме mode=ro (URI) и
дополнительно PRAGMA query_only=ON. INSERT/UPDATE/DELETE из шаблона
физически невозможны.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping


class SelectError(RuntimeError):
    """SQL-запрос не выполнен или вернул не то, что нужно."""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.exists():
        raise SelectError(f"База не найдена: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Вторая линия защиты: даже если кто-то откроет запись, PRAGMA
    # запретит любые изменения в этой сессии.
    conn.execute("PRAGMA query_only = ON")
    return conn


def _tag_from_line(line: str) -> str:
    """Имя тега из URI.

    У vless тег лежит во фрагменте после '#', у vmess — в поле ps, поэтому
    фрагмент подходит не всегда. Строку разбирает тот же парсер, что и при
    сборке конфига; если разобрать не удалось — берём фрагмент.
    """
    s = str(line or "").strip()
    if not s:
        return ""
    if "://" in s:
        try:
            from script import core as _core
            from script.downloader import normalize_proxy_key

            _core.init_parsers()
            nodes = _core.get_nodes(s)
            if nodes and isinstance(nodes[0], dict):
                return _core.tag_for_node(nodes[0], normalize_proxy_key(s))
        except Exception:  # noqa: BLE001
            pass
        head, sep, tail = s.partition("#")
        if sep and tail.strip():
            return tail.strip()
    return s


def _row_to_tag(row: sqlite3.Row) -> str:
    keys = row.keys()
    if "tag" in keys:
        return str(row["tag"] or "").strip()
    if "line" in keys:
        return _tag_from_line(row["line"])
    if "key" in keys:
        return _tag_from_line(row["key"])
    raise SelectError("SQL-запрос должен возвращать колонку tag, line или key")


def run_sql(
    db_path: str | Path,
    sql: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> list[str]:
    """Выполняет SELECT из шаблона и возвращает имена тегов."""
    query = str(sql or "").strip()
    if not query:
        raise SelectError("блок sql пуст")
    conn = _connect(db_path)
    try:
        rows = conn.execute(query, dict(params or {})).fetchall()
    except sqlite3.Error as exc:
        raise SelectError(f"SQL-запрос не выполнен: {exc}") from exc
    finally:
        conn.close()
    return [tag for tag in (_row_to_tag(r) for r in rows) if tag]


def select_tags(db_path: str | Path, sql: str, *,
                params: Mapping[str, Any] | None = None) -> list[str]:
    """Как run_sql, но возвращает именно теги (для блока sql в шаблоне)."""
    return run_sql(db_path, sql, params=params)


__all__ = ["SelectError", "run_sql", "select_tags"]
