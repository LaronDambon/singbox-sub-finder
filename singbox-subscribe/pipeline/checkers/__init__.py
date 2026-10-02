"""Проверяющие алгоритмы серверов.

Публичный вход:

    from pipeline.checkers import Checker, CheckOutcome, CheckResult, build_checkers

Свои алгоритмы кладутся файлами в ``pipeline/checkers/custom/`` — реестр
подхватывает их автоматически, ничего регистрировать не нужно.
"""

from __future__ import annotations

from pipeline.checkers.base import (
    CheckContext,
    CheckOutcome,
    CheckResult,
    Checker,
    FunctionChecker,
    available_lines,
)
from pipeline.checkers.registry import (
    build as build_checkers,
    describe,
    discover,
    sort_checkers,
)

__all__ = [
    "CheckContext",
    "CheckOutcome",
    "CheckResult",
    "Checker",
    "FunctionChecker",
    "available_lines",
    "build_checkers",
    "describe",
    "discover",
    "sort_checkers",
]
