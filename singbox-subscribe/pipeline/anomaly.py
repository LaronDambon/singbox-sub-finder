"""Архив аномальных батчей: конфиг и вывод sing-box в папку логов.

ЗАЧЕМ
-----
Штатный батч не оставляет следов: собранный конфиг удаляется сразу после
прогона, а вывод процесса живёт только в оперативной памяти и исчезает
вместе с ним. Пока всё идёт хорошо, это правильно — иначе диск забился бы
за сутки.

Но когда батч кончается ничем («no_results», 0.1 с, ни одной строки), улик
не остаётся ВООБЩЕ: непонятно ни что было в конфиге, ни что сказал sing-box.
Именно так выглядел разбор, где батчи умирали за 0.1 секунды, а в логе было
только «sing-box не вернул ни одного результата».

Поэтому при аномальном исходе конфиг и ПОЛНЫЙ вывод процесса кладутся в:

    logs/singbox/<id>/
        batch.json     собранный конфиг батча
        singbox.log    stdout+stderr процесса целиком, как есть
        meta.json      исход, код возврата, время, размер, версия

Идентификатор печатается в общем логе рядом с предупреждением, поэтому связь
двусторонняя: из warning.log видно, куда идти, а из meta.json — откуда пришло.

ЧТО ИМЕННО ХРАНИТСЯ
-------------------
Порог задаётся настройкой singbox.keep_failed_batches:

    error      хранить только error   (no_results, config_error, spawn_error)
    warning    хранить error и warning (плюс подозрительный partial)
    info       хранить всё, включая штатные батчи
    off        не хранить ничего

ПОЧЕМУ В ОПЕРАТИВКЕ
-------------------
Вывод процесса копится в памяти и уходит на диск ТОЛЬКО при аномалии. Писать
каждый батч в файл и удалять при успехе значило бы перезаписывать SSD на
каждом прогоне — а батчей за цикл сотни.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline.batch import BatchOutcome
from pipeline.logging_setup import get_logger

LOGGER = get_logger("singbox.archive")

#: Папка архива внутри каталога логов.
ARCHIVE_DIR_NAME = "singbox"

#: Сколько байт вывода процесса держим в памяти на один батч.
#:
#: Батч на 150 серверов при debug-уровне печатает сотни килобайт. При шести
#: воркерах это единицы мегабайт — терпимо. Ограничение нужно на случай
#: патологического вывода (цикл в sing-box): без него один батч съест память
#: всего процесса.
MAX_CAPTURE_BYTES = 4 * 1024 * 1024

#: Уровни хранения, от самого тихого к самому подробному.
LEVELS = ("off", "error", "warning", "info")


@dataclass
class AnomalyCapture:
    """Накопитель вывода одного батча.

    Живёт ровно столько, сколько идёт батч. Строки складываются сюда по мере
    чтения, но на диск ничего не пишется — только если исход окажется
    аномальным.
    """

    batch_index: int
    size: int
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    chunks: list[str] = field(default_factory=list)
    bytes_seen: int = 0
    truncated: bool = False

    def feed(self, line: str) -> None:
        """Дописать строку вывода. Хвост сверх лимита отбрасывается."""
        if self.bytes_seen >= MAX_CAPTURE_BYTES:
            self.truncated = True
            return
        raw = line if line.endswith("\n") else line + "\n"
        self.bytes_seen += len(raw.encode("utf-8", "replace"))
        self.chunks.append(raw)

    @property
    def text(self) -> str:
        body = "".join(self.chunks)
        if self.truncated:
            body += (
                "\n[вывод обрезан: превышен лимит %d МБ на батч]\n"
                % (MAX_CAPTURE_BYTES // (1024 * 1024))
            )
        return body


def batch_id(index: int, started_at: datetime) -> str:
    """Идентификатор батча для связи общего лога с архивом.

    Читается человеком и сортируется по времени:
    ``b0042-20261108T124750``. Номер батча впереди — по нему из warning.log
    сразу видно, какой именно батч сохранён.
    """
    stamp = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return "b%04d-%s" % (index, stamp)


#: Исходы, которые считаются аномальными на уровне "error".
_ERROR_OUTCOMES = (
    BatchOutcome.NO_RESULTS,
    BatchOutcome.CONFIG_ERROR,
    BatchOutcome.SPAWN_ERROR,
)


def outcome_level(outcome: BatchOutcome, *, silent: int, size: int) -> str:
    """Каким уровнем заслужил батч: "error", "warning" или "info".

    Штатный батч — "info": он не сохраняется, если порог строже. Так значение
    ``warning`` в конфиге означает «пиши только то, что заслужило
    предупреждение», а ``info`` — «пиши вообще всё».
    """
    if outcome in _ERROR_OUTCOMES:
        return "error"

    # Батч отработал, но часть серверов промолчала. Формально не ошибка, но
    # на практике именно так начинаются массовые сбои: сначала «молчат
    # некоторые», потом «молчат все». Поэтому это warning.
    if outcome is BatchOutcome.PARTIAL and silent:
        # Мелочь на фоне большого батча предупреждением не считаем: пара
        # молчащих серверов из полутора сотен — обычное дело.
        if size and silent >= max(5, size // 4):
            return "warning"
        return "info"

    return "info"


def should_keep(level: str, threshold: str) -> bool:
    """Попадает ли уровень батча в порог хранения."""
    if threshold not in LEVELS or threshold == "off":
        return False
    if level == "off":
        return False
    return LEVELS.index(level) <= LEVELS.index(threshold)


def save_anomaly(
    *,
    root: Path,
    capture: AnomalyCapture,
    config_text: str | None,
    report,
    returncode: int | None,
    threshold: str,
    limit: int,
) -> str | None:
    """Сохраняет батч в архив, если он того заслужил.

    Возвращает идентификатор сохранённого батча или None. Ошибки записи не
    пробрасываются: не суметь сохранить улику — плохо, но уронить из-за этого
    проверку хуже.
    """
    if limit <= 0 or threshold == "off":
        return None

    level = outcome_level(
        report.outcome,
        silent=int(getattr(report, "silent", 0) or 0),
        size=int(capture.size or 0),
    )
    if not should_keep(level, threshold):
        return None

    ident = batch_id(capture.batch_index, capture.started_at)
    folder = Path(root) / ARCHIVE_DIR_NAME / ident
    try:
        folder.mkdir(parents=True, exist_ok=True)

        if config_text is not None:
            (folder / "batch.json").write_text(config_text, encoding="utf-8")
        (folder / "singbox.log").write_text(capture.text, encoding="utf-8")

        finished = datetime.now(timezone.utc)
        meta = {
            "id": ident,
            "batch_index": capture.batch_index,
            "size": capture.size,
            "level": level,
            "outcome": report.outcome.value,
            "error": (report.error or "")[:500],
            "returncode": returncode,
            "started_at": capture.started_at.isoformat(),
            "finished_at": finished.isoformat(),
            "elapsed": round(float(getattr(report, "elapsed", 0.0) or 0.0), 2),
            "alive": int(getattr(report, "alive", 0) or 0),
            "dead": int(getattr(report, "dead", 0) or 0),
            "silent": int(getattr(report, "silent", 0) or 0),
            "lines_seen": int(getattr(report, "lines_seen", 0) or 0),
            "fatal_lines": list(getattr(report, "fatal_lines", []) or [])[:10],
            "output_bytes": capture.bytes_seen,
            "output_truncated": capture.truncated,
        }
        (folder / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.warning("Не удалось сохранить архив батча %s: %s", ident, exc)
        return None

    prune(root, keep=limit)
    return ident


def prune(root: Path, *, keep: int) -> None:
    """Оставляет последние ``keep`` архивов, остальные удаляет.

    Сортировка по имени: идентификатор начинается с номера батча и содержит
    время, поэтому лексикографический порядок совпадает с хронологическим.
    """
    if keep <= 0:
        return
    base = Path(root) / ARCHIVE_DIR_NAME
    try:
        folders = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return
    extra = len(folders) - keep
    if extra <= 0:
        return
    removed = 0
    for folder in folders[:extra]:
        try:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    if removed:
        LOGGER.info("Архив батчей: удалено старых записей %d (лимит %d)", removed, keep)


__all__ = [
    "ARCHIVE_DIR_NAME",
    "AnomalyCapture",
    "LEVELS",
    "MAX_CAPTURE_BYTES",
    "batch_id",
    "outcome_level",
    "prune",
    "save_anomaly",
    "should_keep",
]
