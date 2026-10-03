"""Запуск одного батча sing-box с живым чтением вывода.

Отличие от прежнего ``script.core.generate_debug_configs_with_singbox``:

  * **результат не ждёт конца запуска.** Строки вывода читаются по мере
    появления и сразу разбираются службой 2 (``pipeline.collector``). Если
    батч убили по таймауту, всё, что sing-box успел напечатать, уже сохранено:
    серверы, ответившие до дедлайна, попадут в базу, а молчавшие вернутся
    в очередь как непроверенные.

  * **у каждого батча свой тег на сервер.** Тег вида ``000012`` не содержит
    ни пробелов, ни эмодзи, поэтому строку вывода sing-box можно разобрать
    однозначно и вернуть серверу ровно его результат.

  * **у каждого батча свой конфиг и свой порт.** Никакого общего
    ``merged_config.json` и никакой фиксированной ``SING_BOX_PORT`` —
    поэтому батчи действительно идут параллельно.

  * **сбой конфигурации отделён от «серверы не отвечают».** Если sing-box упал
    на разборе конфига, это ``BatchOutcome.CONFIG_ERROR` — батч делится
    пополам, чтобы найти кривую строку.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pipeline.batch import BatchClock, BatchOutcome, BatchReport
from pipeline.collector import BatchCollector
from pipeline.logging_setup import get_logger

LOGGER = get_logger("singbox")

#: Признаки того, что виноват конфиг, а не серверы.
CONFIG_ERROR_MARKERS = (
    "decode config",
    "failed to decode",
    "json:",
    "unsupported",
    "unknown field",
    "invalid character",
    "unexpected end of",
)

#: Признаки того, что процесс не смог занять порт. Это не «кривый конфиг»:
#: строки в нём нормальные, нужен просто другой порт.
PORT_ERROR_MARKERS = ("address already in use", "bind:", "only one usage of each socket")

_FATAL_LINE_RE = re.compile(r"FATAL\[\d+\]\s*(?P<msg>.*)")


class ConfigBuildError(Exception):
    """Не удалось собрать конфиг sing-box для батча."""


@dataclass
class BatchConfig:
    """Готовый конфиг батча и соответствие «тег sing-box -> строка прокси»."""

    config: dict
    tag_to_line: dict[str, str]
    unparsable: list[str]


def build_batch_config(lines: list[str], *, tag_prefix: str,
                       template_path: Path) -> BatchConfig:
    """Собирает конфиг батча, раздавая серверам короткие числовые теги.

    Короткий тег — ключевая идея: он однозначно разбирается в выводе sing-box
    и не путается с человекочитаемым именем, где есть пробелы и эмодзи.
    """
    from script import core as core_mod

    # script.core разбирает ссылки через глобальное состояние providers —
    # ставим пустой и возвращаем как было. chdir(ROOT) больше НЕ делается:
    # пути к шаблону и парсерам передаются абсолютными, а разбор строк с
    # локальной схемой диск не трогает. chdir — это состояние процесса,
    # и два потока (event loop и executor чекеров), делающие его каждый
    # на свою строку, могли оставить процесс в чужом каталоге.
    previous_providers = core_mod.providers
    try:
        core_mod.providers = {"exclude_protocol": "", "subscribes": []}
        core_mod.init_parsers()
        template = core_mod.load_template(template_path)

        nodes: list[dict] = []
        tag_to_line: dict[str, str] = {}
        unparsable: list[str] = []
        errors: list[str] = []

        for raw in lines:
            try:
                parsed = core_mod.get_nodes(raw)
            except Exception as exc:  # noqa: BLE001 — кривая строка не должна ронять батч
                errors.append(f"{type(exc).__name__}: {exc}")
                parsed = None
            if not parsed:
                unparsable.append(raw)
                continue
            # Тег ставим на каждый узел строки: у одной ссылки бывает
            # несколько прокси, и каждому нужен свой результат.
            for node in parsed:
                if not isinstance(node, dict):
                    unparsable.append(raw)
                    continue
                tag = f"{tag_prefix}{len(tag_to_line):06d}"
                node["tag"] = tag
                tag_to_line[tag] = raw
                nodes.append(node)

        # Разбились почти ВСЕ строки, причём с исключениями — значит сломан
        # сам разбор, а не серверы. Такое ни в коем случае нельзя отправлять в
        # чёрный список: иначе один неудачный запуск выписывает всю базу.
        if len(lines) > 1 and len(errors) >= len(lines) * 0.9:
            raise ConfigBuildError(
                f"разбор ссылок сломан ({len(errors)} из {len(lines)}): {errors[0][:160]}"
            )

        if not nodes:
            raise ConfigBuildError("ни одну строку не удалось превратить в outbound")

        config = core_mod.build_singbox_config_from_nodes(template, nodes)
        return BatchConfig(config=config, tag_to_line=tag_to_line, unparsable=unparsable)
    finally:
        core_mod.providers = previous_providers


def apply_port(config: dict, port: int) -> dict:
    """Прописывает порт во все mixed-inbound 127.0.0.1 — свой на каждый батч."""
    for inbound in config.get("inbounds", []):
        if inbound.get("type") == "mixed" and inbound.get("listen") == "127.0.0.1":
            inbound["listen_port"] = port
    return config


def classify_failure(fatal_lines: list[str]) -> tuple[BatchOutcome, str]:
    """Отличает «сломан конфиг» от «занят порт» и прочих сбоев запуска."""
    text = " ".join(fatal_lines).lower()
    for marker in PORT_ERROR_MARKERS:
        if marker in text:
            return BatchOutcome.CONFIG_ERROR, f"порт занят: {marker}"
    for marker in CONFIG_ERROR_MARKERS:
        if marker in text:
            return BatchOutcome.CONFIG_ERROR, f"невалидный конфиг: {marker}"
    if fatal_lines:
        first = fatal_lines[0]
        match = _FATAL_LINE_RE.search(first)
        return BatchOutcome.CONFIG_ERROR, (match.group("msg") if match else first)[:200]
    return BatchOutcome.NO_RESULTS, "sing-box не вернул ни одного результата"


def _kill_sync(proc) -> None:
    """Гасит sing-box БЕЗ единого await — единственное, что работает при отмене.

    Когда задачу батча отменяют, event loop может быть в состоянии отмены, и
    любой await внутри очистки рискует снова бросить CancelledError, так и не
    дойдя до конца. Поэтому здесь только синхронные вызовы ОС, каждый под
    подавлением: очистка обязана завершиться целиком.

    Именно этот путь закрывает «Event loop is closed»: процесс убит до того,
    как цикл событий закроется, поэтому каналы subprocess не остаются
    висящими на объектах, которые уничтожает интерпретатор.
    """
    if proc.returncode is not None:
        return
    if os.name == "nt":
        # terminate() на Windows не трогает дочерние процессы, а они держат
        # порт и мешают следующему батчу.
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
    # Process.kill() у asyncio.Process синхронный и сам по себе не бросает
    # CancelledError, но ValueError возможен, если каналы уже закрыты.
    with contextlib.suppress(ProcessLookupError, OSError, ValueError):
        proc.kill()


def _cancel_tasks(*tasks) -> None:
    """Просит задачи-помощники завершиться, не дожидаясь их."""
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()


async def _terminate(proc) -> None:
    """Достоверно убивает sing-box вместе с детьми."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        # terminate() на Windows не трогает дочерние процессы, а они держат
        # порт и мешают следующему батчу.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=10)


async def run_batch(
    lines: list[str],
    *,
    index: int,
    singbox_path: Path,
    template_path: Path,
    clock: BatchClock,
    config_dir: Path,
    tag_prefix: str,
    urltest: str = "",
) -> BatchReport:
    """Запускает один батч sing-box и собирает результаты на лету."""
    report = BatchReport(index=index, size=len(lines), tag_prefix=tag_prefix)

    try:
        built = build_batch_config(lines, tag_prefix=tag_prefix, template_path=template_path)
    except ConfigBuildError as exc:
        report.outcome = BatchOutcome.CONFIG_ERROR
        report.error = str(exc)
        # Строки НЕ непригодные, а непроверенные: конфиг не собрался.
        # Отправлять их в чёрный список нельзя — они просто не проверены.
        report.unparsable = []
        report.retry_lines = list(lines)
        LOGGER.warning("Батч %d: конфиг не собрался: %s", index, exc)
        return report
    except Exception as exc:  # noqa: BLE001
        report.outcome = BatchOutcome.CONFIG_ERROR
        report.error = f"{type(exc).__name__}: {exc}"
        report.unparsable = []
        report.retry_lines = list(lines)
        LOGGER.warning("Батч %d: ошибка сборки конфига: %s", index, exc)
        return report

    report.tag_to_line = dict(built.tag_to_line)
    report.unparsable = list(built.unparsable)

    from script.core import find_free_port

    port = find_free_port()
    config = apply_port(built.config, port)
    if urltest:
        for outbound in config.get("outbounds", []):
            if isinstance(outbound.get("outbounds"), list):
                outbound["url"] = urltest

    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"batch_{index:04d}_{tag_prefix}.json"
    try:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        report.outcome = BatchOutcome.CONFIG_ERROR
        report.error = f"не удалось записать конфиг: {exc}"
        report.retry_lines = [built.tag_to_line[t] for t in built.tag_to_line]
        return report

    collector = BatchCollector(expected=len(built.tag_to_line), name=f"батч {index}")
    finished = asyncio.Event()

    try:
        proc = await asyncio.create_subprocess_exec(
            str(singbox_path), "run", "-c", str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(config_path.parents[2]),
        )
    except Exception as exc:  # noqa: BLE001
        report.outcome = BatchOutcome.SPAWN_ERROR
        report.error = f"{type(exc).__name__}: {exc}"
        report.retry_lines = [built.tag_to_line[t] for t in built.tag_to_line]
        LOGGER.error("Батч %d: sing-box не запустился: %s", index, exc)
        _unlink(config_path)
        return report

    async def pump() -> None:
        """Читает вывод sing-box построчно и сразу разбирает каждую строку."""
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, OSError):
                return
            if not raw:
                return
            collector.feed(raw.decode("utf-8", "replace"))
            if collector.complete:
                # Все серверы батча отчитались — можно закрывать процесс.
                finished.set()
                return

    async def watch_exit() -> None:
        await proc.wait()
        finished.set()

    reader = asyncio.create_task(pump())
    watcher = asyncio.create_task(watch_exit())
    timed_out = False

    # Дальше живут ДВА await-участка: сам запуск батча (ожидание finished) и его
    # штатная уборка. Отмена задачи может прийти в любой из них, поэтому оба
    # закрыты одним обработчиком: он глушит sing-box СИНХРОННО и пробрасывает
    # CancelledError дальше ровно один раз.
    try:
        try:
            await asyncio.wait_for(finished.wait(), timeout=clock.timeout)
        except asyncio.TimeoutError:
            timed_out = True

        # Убиваем процесс, но НЕ обрываем чтение: всё, что sing-box успел
        # напечатать до смерти, должно попасть в результат.
        await _terminate(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(finished.wait(), timeout=3)
    except asyncio.CancelledError:
        # Сюда попадает отмена батча: grace-таймаут Supervisor.stop_all, Ctrl+C
        # или внешний task.cancel(). Штатный путь сюда не доходит.
        #
        # Именно этот блок и был источником мусора в конце вывода: отмена
        # пролетала мимо _terminate, sing-box оставался висеть вместе с
        # открытыми каналами, и при закрытии цикла событий интерпретатор
        # печатал «Event loop is closed» и «I/O operation on closed pipe».
        #
        # Здесь НИ ОДНОГО await: во время отмены loop может быть в состоянии
        # отмены, и любой await внутри уборки рискует снова бросить
        # CancelledError, не дойдя до конца. Только синхронные вызовы ОС.
        _cancel_tasks(reader, watcher)
        _kill_sync(proc)
        _unlink(config_path)
        raise

    # Штатный путь: дочитываем помощников. gather с return_exceptions=True
    # возвращает их отмену как результат, поэтому обычный CancelledError
    # (отменённые нами задачи) наружу не выпускает — в отличие от голого
    # contextlib.suppress, который глотал бы и настоящую отмену этого батча.
    _cancel_tasks(reader, watcher)
    try:
        await asyncio.gather(reader, watcher, return_exceptions=True)
    except asyncio.CancelledError:
        # Отмена пришла ровно на уборке: процесс уже убит, но конфиг и
        # помощников всё равно нужно прибрать — иначе вернёмся в тот же мусор.
        _cancel_tasks(reader, watcher)
        _kill_sync(proc)
        _unlink(config_path)
        raise
    _unlink(config_path)

    return _finish_report(report, collector, built.tag_to_line, timed_out, clock)


def _finish_report(report: BatchReport, collector: BatchCollector,
                   tag_to_line: dict[str, str], timed_out: bool,
                   clock: BatchClock) -> BatchReport:
    """Сводит счётчик коллектора в отчёт батча и решает его судьбу."""
    counts = collector.counts()
    report.verdicts = dict(collector.results)
    report.alive = counts["alive"]
    report.dead = counts["dead"]
    report.lines_seen = counts["lines"]
    report.dns_failures = collector.dns_failures
    report.dial_timeouts = collector.dial_timeouts
    report.fatal_lines = list(collector.fatal_lines)
    report.elapsed = clock.elapsed

    silent_tags = [tag for tag in tag_to_line
                   if collector.verdict(tag).verdict.value == "no_result"]
    report.silent = len(silent_tags)
    report.retry_lines = [tag_to_line[tag] for tag in silent_tags]

    answered = report.alive + report.dead
    if not answered:
        report.outcome, reason = classify_failure(collector.fatal_lines)
        report.error = reason
    elif report.silent:
        report.outcome = BatchOutcome.PARTIAL
    else:
        report.outcome = BatchOutcome.COMPLETED

    level = LOGGER.info if report.outcome.has_verdicts else LOGGER.warning
    level("%s", report.summary())
    if report.error:
        LOGGER.warning("Батч %d: %s", report.index, report.error)
    return report


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
