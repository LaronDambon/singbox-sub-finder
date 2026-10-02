"""Этапы pipeline — по одному классу на этап.

Каждый этап лежит в своей папке/модуле и умеет только своё:

  * ``discovery.py``  — поиск новых серверов из ссылок (расширяет базу);
  * ``check.py``      — проверка батчей из очереди (воркеры + чекеры);
  * ``writer.py``     — превращение результатов чекеров в записи базы;
  * ``export.py``     — очистка ротации, экспорт списков, деплой.

Собраны вместе в ``pipeline.engine.Pipeline`` — он же решает, когда какой
этап запускается и как они пересекаются по времени.
"""

from pipeline.stages.check import CheckStage
from pipeline.stages.discovery import DiscoveryStage
from pipeline.stages.export import ExportStage
from pipeline.stages.writer import build_rows, count_results, format_line

__all__ = [
    "CheckStage",
    "DiscoveryStage",
    "ExportStage",
    "build_rows",
    "count_results",
    "format_line",
]
