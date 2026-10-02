"""Ядро pipeline: очередь, база, проверяющие алгоритмы, логирование.

Точка входа для прикладного кода (обычно это main.py):

    from pipeline import Pipeline, run_pipeline, get_logger

    LOGGER = get_logger(__name__)

    async with Pipeline(checkers="url_probe,country") as pipeline:
        report = await pipeline.run()

Для запуска из командной строки:

    python -m pipeline run                  # полный прогон
    python -m pipeline checkers             # какие алгоритмы доступны
    python -m pipeline status               # состояние базы и очереди
    python -m pipeline queue                # счётчики очереди
"""

from pipeline.logging_setup import (
    cleanup_old_logs,
    get_logger,
    log_throttle_summary,
    setup_logging,
)

__all__ = [
    "Pipeline",
    "run_pipeline",
    "get_logger",
    "setup_logging",
    "cleanup_old_logs",
    "log_throttle_summary",
    "Database",
    "PipelineContext",
    "Settings",
    "Checker",
    "CheckContext",
    "CheckOutcome",
    "CheckResult",
]


def __getattr__(name: str):
    """Ленивый экспорт: тяжёлые модули (база, чекеры) грузятся по требованию.

    Благодаря этому ``import pipeline`` остаётся дешёвым и не тянет за собой
    sqlite и бинарник sing-box.
    """
    if name in {"Pipeline", "run_pipeline"}:
        from pipeline import engine

        return getattr(engine, name)
    if name == "Database":
        from pipeline.database import Database

        return Database
    if name in {"PipelineContext", "Settings"}:
        from pipeline import context

        return getattr(context, name)
    if name in {"Checker", "CheckContext", "CheckOutcome", "CheckResult"}:
        from pipeline import checkers

        return getattr(checkers, name)
    raise AttributeError(f"module 'pipeline' has no attribute {name!r}")
