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

    Замок снимается только после того, как РАБОТА реально закончилась, а не
    после того, как её перестали ждать. Раньше вызывающая сторона оборачивала
    это в asyncio.wait_for, и по таймауту await прерывался — но поток с
    sing-box продолжал жить и держать свои порты, а замок уже был свободен.
    Следующий чекер (service_access, speed) стартовал поверх работающего
    предыдущего и получал занятые порты. Именно это выглядело как «конфиги
    запускаются мимо pipeline».

    Поэтому поток и замок связываются явно: пока future не завершится, замок
    не отпускается, даже если нас перестали ждать.
    """
    loop = asyncio.get_running_loop()
    from functools import partial

    target = partial(func, **kwargs) if kwargs else func
    lock = get_lock()
    await lock.acquire()
    future = loop.run_in_executor(None, target, *args)
    # Отпускаем замок строго в колбэке завершения future, а не в finally
    # ожидающего: отмена ожидания (wait_for/CancelledError) не должна
    # освобождать замок, пока sing-box ещё работает.
    released = False

    def _release(_fut=None):
        nonlocal released
        if not released:
            released = True
            loop.call_soon_threadsafe(lock.release)

    future.add_done_callback(_release)
    try:
        return await future
    except asyncio.CancelledError:
        # Ждать перестали, но работа продолжается: замок отпустит _release,
        # когда поток действительно закончит. Пробрасываем отмену наверх.
        raise
