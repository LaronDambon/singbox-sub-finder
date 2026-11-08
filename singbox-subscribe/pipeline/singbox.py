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

from pipeline.anomaly import AnomalyCapture, save_anomaly
from pipeline.batch import BatchClock, BatchOutcome, BatchReport
from pipeline.collector import BatchCollector
from pipeline.logging_setup import get_logger
from pipeline.offenders import drop_group_children, offender_index, prune_config

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

#: Сколько outbound'ов разрешено вырезать из одного батча.
#:
#: Один-два кривых узла — обычное дело (битая ссылка попала в подписку).
#: Десяток на батч из 50 — это уже не ссылки, а сломанный сборщик или
#: шаблон; тогда резать дальше бессмысленно, батч должен упасть как есть,
#: чтобы ошибку было видно. Заодно это страховка от вырезания всего батча
#: по одному узлу за запуск.
MAX_PRUNE_PER_BATCH = 5

#: Сколько раз пересобрать батч после вырезания виновника.
MAX_SELFHEAL_ROUNDS = 8


class ConfigBuildError(Exception):
    """Не удалось собрать конфиг sing-box для батча."""


class SpawnError(Exception):
    """sing-box не удалось запустить как процесс (не про содержимое конфига)."""


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
    archive_root: Path | None = None,
    archive_level: str = "off",
    archive_limit: int = 0,
) -> BatchReport:
    """Запускает один батч sing-box и собирает результаты на лету.

    archive_root/archive_level/archive_limit — куда и при каком пороге
    складывать конфиг и вывод при аномальном исходе. При archive_level="off"
    (или лимите 0) вывод всё равно читается, но никуда не пишется.

    Кривая ссылка, попавшая в батч, роняет sing-box ЦЕЛИКОМ — вся проверка
    умирает за десятые доли секунды, и виноват в этом один узел из полусотни.
    Поэтому запуск идёт циклом: sing-box называет номер сломанного outbound'а,
    мы вырезаем его из конфига и запускаем снова. Строка-виновник уходит в
    report.pruned_lines (её отправит в чёрный список диспетчер), остальные
    проверяются как обычно.
    """
    report = BatchReport(index=index, size=len(lines), tag_prefix=tag_prefix)
    capture = AnomalyCapture(batch_index=index, size=len(lines))

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
        _archive_if_needed(
            report, capture=capture, config_text=None, returncode=None,
            archive_root=archive_root, archive_level=archive_level,
            archive_limit=archive_limit,
        )
        return report
    except Exception as exc:  # noqa: BLE001
        report.outcome = BatchOutcome.CONFIG_ERROR
        report.error = f"{type(exc).__name__}: {exc}"
        report.unparsable = []
        report.retry_lines = list(lines)
        LOGGER.warning("Батч %d: ошибка сборки конфига: %s", index, exc)
        _archive_if_needed(
            report, capture=capture, config_text=None, returncode=None,
            archive_root=archive_root, archive_level=archive_level,
            archive_limit=archive_limit,
        )
        return report

    report.tag_to_line = dict(built.tag_to_line)
    report.unparsable = list(built.unparsable)

    from script.core import find_free_port

    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"batch_{index:04d}_{tag_prefix}.json"

    #: Кто уже вырезан. Строки уходят в report.pruned_lines — диспетчер
    #: отправит их в чёрный список, чтобы батч не собирался с ними снова.
    pruned_lines: list[str] = []
    pruned_tags: set[str] = set()
    config = built.config
    collector = None
    timed_out = False
    returncode: int | None = None
    config_text = ""

    for round_no in range(MAX_SELFHEAL_ROUNDS + 1):
        port = find_free_port()
        config = apply_port(config, port)
        if urltest:
            for outbound in config.get("outbounds", []):
                if isinstance(outbound.get("outbounds"), list):
                    outbound["url"] = urltest

        # Теги, оставшиеся в конфиге после вырезаний. Группы чистим ДО запуска:
        # висячая ссылка на вырезанный outbound — это уже другая ошибка
        # («outbound not found»), и батч упал бы снова, но по чужой причине.
        live_tags = {
            str(item.get("tag"))
            for section in ("outbounds", "endpoints")
            for item in (config.get(section) or [])
            if isinstance(item, dict) and item.get("tag")
        }
        missing = pruned_tags - live_tags
        if missing:
            drop_group_children(config, missing)

        expected = {
            tag: line for tag, line in built.tag_to_line.items() if tag in live_tags
        }
        config_text = json.dumps(config, ensure_ascii=False, indent=2)
        try:
            config_path.write_text(config_text, encoding="utf-8")
        except OSError as exc:
            report.outcome = BatchOutcome.CONFIG_ERROR
            report.error = f"не удалось записать конфиг: {exc}"
            report.retry_lines = list(built.tag_to_line.values())
            report.pruned_lines = pruned_lines
            _archive_if_needed(
                report, capture=capture, config_text=config_text, returncode=None,
                archive_root=archive_root, archive_level=archive_level,
                archive_limit=archive_limit,
            )
            return report

        collector = BatchCollector(expected=len(expected), name=f"батч {index}")
        try:
            timed_out, returncode = await _run_once(
                config_path=config_path, singbox_path=singbox_path,
                collector=collector, capture=capture, clock=clock,
            )
        except SpawnError as exc:
            report.outcome = BatchOutcome.SPAWN_ERROR
            report.error = str(exc)
            report.retry_lines = list(built.tag_to_line.values())
            report.pruned_lines = pruned_lines
            LOGGER.error("Батч %d: sing-box не запустился: %s", index, exc)
            _archive_if_needed(
                report, capture=capture, config_text=config_text, returncode=None,
                archive_root=archive_root, archive_level=archive_level,
                archive_limit=archive_limit,
            )
            return report

        if collector.complete or timed_out:
            break

        # Батч умер, ничего не успев сказать. Если sing-box назвал виновника —
        # вырезаем его и пробуем снова; иначе это не про конкретную строку.
        fatal = list(collector.fatal_lines)
        offender = offender_index(" ".join(fatal))
        if offender is None:
            break
        if len(pruned_lines) >= MAX_PRUNE_PER_BATCH:
            LOGGER.warning(
                "Батч %d: вырезано уже %d outbound'ов, дальше не режу — "
                "похоже на поломку сборщика или шаблона, а не на битые ссылки",
                index, len(pruned_lines),
            )
            break

        section, pos = offender
        items = config.get(section)
        victim_tag = ""
        if (isinstance(items, list) and 0 <= pos < len(items)
                and isinstance(items[pos], dict)):
            victim_tag = str(items[pos].get("tag") or "")
        if not victim_tag:
            break

        raw = built.tag_to_line.get(victim_tag)
        if prune_config(config, section, pos) is None:
            break
        pruned_tags.add(victim_tag)

        reason = _fatal_reason(fatal)
        if raw:
            pruned_lines.append(raw)
            report.tag_to_line.pop(victim_tag, None)
            LOGGER.warning(
                "Батч %d: sing-box не запустился (%s) — вырезаю %s[%d] "
                "и проверяю остальные %d; строка уйдёт в чёрный список",
                index, reason, section, pos, len(report.tag_to_line),
            )
        else:
            # Номер указал на служебный outbound (selector/urltest/direct):
            # это уже не битая ссылка, а сломанный шаблон.
            LOGGER.warning(
                "Батч %d: sing-box не запустился (%s) — вырезаю служебный "
                "%s[%d] и пробую снова", index, reason, section, pos,
            )

    report.pruned_lines = pruned_lines
    # Вырезанные строки не проверены: ни вернуть в очередь, ни записать
    # «не отозвался» по ним нельзя — их судьбу решает чёрный список.
    total_to_line = {
        tag: line for tag, line in built.tag_to_line.items()
        if line not in set(pruned_lines)
    }
    report = _finish_report(report, collector, total_to_line, timed_out, clock)
    _archive_if_needed(
        report, capture=capture, config_text=config_text, returncode=returncode,
        archive_root=archive_root, archive_level=archive_level,
        archive_limit=archive_limit,
    )
    return report


def _fatal_reason(fatal_lines: list[str]) -> str:
    """Короткая причина падения для лога и meta.json."""
    for line in fatal_lines:
        match = _FATAL_LINE_RE.search(line)
        if match:
            return match.group("msg").strip()[:200]
    return " ".join(fatal_lines)[:200] if fatal_lines else "без объяснений"


async def _run_once(
    *,
    config_path: Path,
    singbox_path: Path,
    collector: BatchCollector,
    capture: AnomalyCapture,
    clock: BatchClock,
) -> tuple[bool, int | None]:
    """Один запуск sing-box на уже записанном конфиге.

    Возвращает (timed_out, returncode). Всё, что процесс успел напечатать,
    уже разобрано: строки идут и в коллектор (вердикты), и в capture (улики).

    Здесь же живёт защита от отмены: она обязана прибрать процесс СИНХРОННО,
    потому что во время отмены event loop может быть в состоянии отмены, и
    любой await рискует снова бросить CancelledError, не дойдя до конца.
    """
    finished = asyncio.Event()

    try:
        proc = await asyncio.create_subprocess_exec(
            str(singbox_path), "run", "-c", str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(config_path.parents[2]),
        )
    except Exception as exc:  # noqa: BLE001
        raise SpawnError(f"{type(exc).__name__}: {exc}") from exc

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, OSError):
                return
            if not raw:
                return
            text = raw.decode("utf-8", "replace")
            capture.feed(text)
            collector.feed(text)
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
        _cancel_tasks(reader, watcher)
        _kill_sync(proc)
        raise

    # Штатный путь: дочитываем помощников. gather с return_exceptions=True
    # возвращает их отмену как результат, поэтому обычный CancelledError
    # (отменённые нами задачи) наружу не выпускает.
    _cancel_tasks(reader, watcher)
    try:
        await asyncio.gather(reader, watcher, return_exceptions=True)
    except asyncio.CancelledError:
        _cancel_tasks(reader, watcher)
        _kill_sync(proc)
        raise

    return timed_out, proc.returncode


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


def _archive_if_needed(report: BatchReport, *, capture: AnomalyCapture,
                       config_text: str | None, returncode: int | None,
                       archive_root: Path | None, archive_level: str,
                       archive_limit: int) -> None:
    """Складывает конфиг и вывод батча в архив, если исход того заслужил.

    Идентификатор печатается рядом с предупреждением: без него из общего лога
    непонятно, куда идти за подробностями.
    """
    if archive_root is None:
        return
    ident = save_anomaly(
        root=archive_root,
        capture=capture,
        config_text=config_text,
        report=report,
        returncode=returncode,
        threshold=archive_level,
        limit=archive_limit,
    )
    if ident:
        LOGGER.warning(
            "Батч %d: сохранён в logs/%s/%s (конфиг, вывод, meta.json)",
            report.index, "singbox", ident,
        )


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
