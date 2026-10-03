"""Сериализация запусков sing-box.

Почему это нужно
----------------
Все штатные способы запустить sing-box в этом проекте меняют состояние
ПРОЦЕССА, а не локальное:

  * ``script/core.py`` — ``os.chdir(ROOT)`, глобальный ``providers``,
    общий файл ``sing-box/merged_config.json` и порт из ``fresh_inbound_port()`;
  * ``script/country_check.py` и ``script/reachability_check.py` — тоже
    ``os.chdir(ROOT)` и общие временные файлы в ``source/tests/``.

Два таких запуска одновременно (например два воркера проверки) гарантированно
ломаются: один процесс перехватывает ``chdir``, файлы конфигов затирают друг
друга, а inbound-порт заканчивается одинаковым — ``SING_BOX_PORT=7891``
в `.env` фиксирует его для всех батчей сразу.

Поэтому запуск sing-box выполняется под общим замком. Параллелизм при этом
сохраняется там, где он безопасен: скачивание подписок, работа с базой и
остальная обработка батча идут одновременно, а вот сборка конфига и запуск
sing-box — по очереди.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

_lock: asyncio.Lock | None = None


def get_lock() -> asyncio.Lock:
    """Общий замок запуска sing-box (ленивый: создаётся внутри event loop)."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def run_exclusive(func: Callable[..., Any], /, *args, **kwargs) -> Any:
    """Выполняет блокирующий запуск sing-box, не давая пересечься с другим.

    Блокирующий код дополнительно уходит в поток, чтобы event loop не вставал.
    """
    loop = asyncio.get_running_loop()
    from functools import partial

    target = partial(func, **kwargs) if kwargs else func
    async with get_lock():
        return await loop.run_in_executor(None, target, *args)
