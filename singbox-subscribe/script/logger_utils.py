"""Совместимая обёртка над pipeline.logging_setup.

Раньше здесь жил самостоятельный модуль логирования, который каждый
импортирующий модуль вызывал на верхнем уровне (``setup_project_logging()``),
из-за чего обработчики пересоздавались по несколько раз за процесс, а одна
запись писалась сразу в четыре файла.

Теперь реальная настройка живёт в ``pipeline/logging_setup.py``, а этот модуль —
тонкий шим: старые вызовы ``get_project_logger``/``setup_project_logging``/
``capture_stdout_stderr_to_logger`` работают, но идут в общий настроенный логгер.

Новый код должен импортировать напрямую:

    from pipeline.logging_setup import get_logger
    LOGGER = get_logger(__name__)
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pipeline.logging_setup import (
    capture_stdout_stderr_to_logger as _capture,
)
from pipeline.logging_setup import (
    cleanup_old_logs,
    get_logger,
    log_throttle_summary,
    setup_logging,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = ROOT / "logs"


def setup_project_logging(
    log_dir: str | Path | None = None,
    console_level: int = logging.WARNING,
    *,
    level: int | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    retention_days: int | None = None,
    force: bool = False,
) -> logging.Logger:
    """Старый API. Настраивает логирование только при первом вызове.

    ``console_level`` здесь — уровень консоли, общий порог файлов задаётся
    переменной окружения LOG_LEVEL. Если логирование уже настроено (а это
    почти всегда: его настраивает main.py), вызов ничего не меняет.
    """
    return setup_logging(
        level=level,
        console_level=console_level,
        log_dir=log_dir,
        max_bytes=max_bytes,
        backup_count=backup_count,
        retention_days=retention_days,
        force=force,
    )


def get_project_logger(name: str | None = None) -> logging.Logger:
    """Старый API. Возвращает логгер проекта (с настройкой по умолчанию)."""
    return get_logger(name)


@contextmanager
def capture_stdout_stderr_to_logger(
    logger: logging.Logger | None = None,
) -> Iterator[None]:
    """Старый API. Заворачивает print() в логгер на время блока."""
    with _capture(logger or get_project_logger("stdout")):
        yield


__all__ = [
    "capture_stdout_stderr_to_logger",
    "cleanup_old_logs",
    "get_project_logger",
    "log_throttle_summary",
    "setup_project_logging",
]
