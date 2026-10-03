#!/usr/bin/env python3
"""Тесты ядра pipeline: логирование, очередь, проверяющие алгоритмы, прогон.

Запуск (из папки singbox-subscribe):

    python tests/test_pipeline_core.py

Тесты не трогают рабочую базу и рабочие логи: используется временный каталог
рядом с тестом, который удаляется в конце. Реальные check_queue/серверы
не затрагиваются, сетевых запросов и запусков sing-box нет — вместо них
подставляются тестовые чекеры.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

# Временный каталог — в корне репозитория (не внутри пакета: там могут быть
# права только на чтение, если пакет поставлен как библиотека).
# В имени есть номер процесса: два одновременных прогона иначе удаляли бы
# временные файлы друг друга на середине и давали невоспроизводимые падения.
TMP = HERE.parent.parent / f"_pipeline_test_tmp_{os.getpid()}"


def _prepare_env() -> None:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ["LOG_DIR"] = str(TMP / "logs")
    os.environ["LOG_LEVEL"] = "DEBUG"
    os.environ["LOG_CONSOLE_LEVEL"] = "ERROR"
    os.environ["LOG_PER_LEVEL_FILES"] = "1"


_prepare_env()

from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker  # noqa: E402
from pipeline.database import Database, read_queue_stats  # noqa: E402
from pipeline.engine import Pipeline  # noqa: E402
import logging  # noqa: E402
from pipeline.logging_setup import (  # noqa: E402
    ROOT_LOGGER_NAME,
    ThrottleFilter,
    cleanup_old_logs,
    get_logger,
    log_throttle_summary,
    setup_logging,
)
from script.server_store import ServerStore  # noqa: E402

# Тестовая база не должна втягивать реальные whitelist/blacklist первой миграцией.
ServerStore.LEGACY_WHITELIST = TMP / "no_whitelist.txt"
ServerStore.LEGACY_BLACKLIST = TMP / "no_blacklist.txt"

PASSED: list[bool] = []


def check(label: str, condition, extra: object = "") -> None:
    PASSED.append(bool(condition))
    mark = "PASS" if condition else "FAIL"
    print(f"{mark}  {label}" + (f"  {extra}" if extra else ""), flush=True)


# --------------------------------------------------------------------- логи
def _record(level: int, name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 0, message, (), None)


def test_throttle_summary() -> None:
    """Сводка по подавленным повторам не должна ронять прогон.

    Раньше в f-строке стояло имя suppressed, которого в цикле нет: NameError
    падал в самом конце 25-минутного прогона, когда все этапы уже
    отработали, а планировщик считал прогон упавшим.
    """
    print("\n--- сводка подавленных повторов ---")
    throttle = ThrottleFilter(limit=2, window=3600.0)
    for _ in range(7):
        throttle.filter(_record(logging.INFO, "singbox.engine", "повтор"))
    throttle.filter(_record(logging.INFO, "singbox.engine", "разовый"))
    summary = throttle.summary()

    check("сводка не падает", isinstance(summary, list))
    check("в сводке одна подавленная строка", len(summary) == 1, {"строк": len(summary)})
    check("указан счётчик подавлений", bool(summary) and "5" in summary[0], summary)
    check("уровень именем, а не числом",
          bool(summary) and summary[0].startswith("INFO "), summary)
    check("уникальное сообщение не попало",
          all("разовый" not in s for s in summary), summary)

    # Сломанная сводка не должна ронять вызывающий код.
    class _Broken(ThrottleFilter):
        def summary(self):
            raise RuntimeError("сводка сломана")

    root = logging.getLogger(ROOT_LOGGER_NAME)
    old = getattr(root, "throttle", None)
    root.throttle = _Broken(limit=1, window=1.0)
    try:
        log_throttle_summary(get_logger("throttle"))
        survived = True
    except Exception:  # noqa: BLE001
        survived = False
    finally:
        if old is None:
            try:
                del root.throttle
            except AttributeError:
                pass
        else:
            root.throttle = old
    check("битая сводка не роняет прогон", survived)


def test_logging() -> None:
    print("\n--- логирование ---")
    log = setup_logging()
    for level, emit in (
        ("DEBUG", log.debug), ("INFO", log.info), ("WARNING", log.warning),
        ("ERROR", log.error), ("CRITICAL", log.critical),
    ):
        emit(f"сообщение уровня {level}")
    setup_logging()  # повторный вызов обязан быть безопасным
    log.info("сообщение после повторного setup")

    log_dir = TMP / "logs"

    def levels(name: str) -> set:
        path = log_dir / name
        if not path.exists():
            return set()
        found = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) > 2:
                found.add(parts[2])
        return found

    check("app.log создан", (log_dir / "app.log").exists())
    check("нет повторов после повторного setup",
          sum(1 for l in (log_dir / "app.log").read_text(encoding="utf-8").splitlines()
              if "сообщение уровня INFO" in l) == 1)
    check("app.log хранит все уровни",
          levels("app.log") == {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
          levels("app.log"))
    check("info.log только INFO", levels("info.log") == {"INFO"}, levels("info.log"))
    check("warning.log только WARNING", levels("warning.log") == {"WARNING"})
    check("error.log только ERROR", levels("error.log") == {"ERROR"})
    check("debug.log только DEBUG", levels("debug.log") == {"DEBUG"})

    stale = log_dir / "stale.log"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    removed = cleanup_old_logs(log_dir, 14)
    check("очистка логов по возрасту", [p.name for p in removed] == ["stale.log"], removed)
    check("свежие логи не удалены", (log_dir / "app.log").exists())


# ------------------------------------------------------------- тестовые чекеры
class AliveChecker(Checker):
    """Живыми считаем строки на чётных позициях батча."""

    name = "alive"
    description = "тестовая проверка"
    decides_availability = True

    async def check(self, ctx: CheckContext) -> CheckResult:
        outcomes = {}
        for i, line in enumerate(ctx.lines):
            alive = i % 2 == 0
            outcomes[line] = CheckOutcome(
                ok=alive, ping_ms=(50 + i * 13) if alive else None,
                detail=None if alive else "не ответил",
            )
        return CheckResult(
            outcomes=outcomes, summary=f"{sum(1 for o in outcomes.values() if o.ok)}/{len(outcomes)}",
        )


class EnrichChecker(Checker):
    """Дополнение: страна и профиль достижимости."""

    name = "enrich"
    description = "обогащение страной и тэгами"

    async def check(self, ctx: CheckContext) -> CheckResult:
        return CheckResult(
            outcomes={
                line: CheckOutcome(ok=None, country="\U0001F1E9\U0001F1EA", capabilities="Global")
                for line in ctx.lines
            },
            summary=f"{len(ctx.lines)} обогащено",
        )


class BrokenChecker(Checker):
    """Чекер, который всегда падает: прогон не должен ломаться."""

    name = "broken"
    description = "всегда падает"

    async def check(self, ctx: CheckContext) -> CheckResult:
        raise RuntimeError("сломанный чекер")


LINES = [
    "vless://11111111-aaaa-bbbb-cccc-000000000001@example.com:443?type=ws&security=tls#one",
    "trojan://pass1@example.org:443#two",
    "ss://YWVzLTI1Ni1nY206cGFzc3dvcmRAMTI3LjAuMC4xOjgzODg#three",
    "vmess://eyJ2IjoiMiIsInBzIjoidGVzdCIsImFkZCI6IjEuMi4zLjQiLCJwb3J0IjoiNDQzIiwiaWQiOiJhYmMiLCJwYXRoIjoiL2ciLCJ0bHMiOiJ0bHMifQ==#four",
    "hysteria2://pass@5.6.7.8:443#five",
]


# --------------------------------------------------------------- очередь/БД
async def test_database() -> None:
    print("\n--- база и очередь ---")
    db = Database(TMP / "queue.db")

    upsert = await db.upsert_lines(LINES)
    check("upsert добавил серверы", upsert["added"] == len(LINES), upsert)

    await db.enqueue(LINES)
    stats = await db.queue_stats()
    check("все серверы в очереди", stats.pending == len(LINES), stats.as_dict())

    first = await db.claim_batch(2)
    check("claim взял батч из 2", len(first) == 2, len(first))
    check("claim уменьшил pending", (await db.queue_stats()).pending == len(LINES) - 2)

    second = await db.claim_batch(2)
    check("второй claim отдал ДРУГИЕ строки",
          len(second) == 2 and not ({i.key for i in first} & {i.key for i in second}))
    check("всё в работе", (await db.queue_stats()).in_progress == 4)

    await db.complete_batch(first)
    after = await db.queue_stats()
    check("батч снят с работы", after.in_progress == 2 and after.done == 2, after.as_dict())

    await db.requeue_stale(statuses=("done",))
    check("requeue_stale возвращает обработанное в очередь",
          (await db.queue_stats()).pending == 3, (await db.queue_stats()).as_dict())

    check("sync-чтение очереди совпадает с async",
          read_queue_stats(TMP / "queue.db").pending == 3,
          read_queue_stats(TMP / "queue.db").as_dict())
    await db.close()


# ----------------------------------------------------------------- прогон
async def test_pipeline() -> None:
    print("\n--- прогон pipeline ---")
    settings = {
        "batch_size": 2,
        "check_workers": 2,
        "discovery": False,
        "write_merge": True,
        "export_lists": True,
        "merge_file": str(TMP / "merge.txt"),
        "whitelist_file": str(TMP / "whitelist.txt"),
        "blacklist_file": str(TMP / "blacklist.txt"),
    }
    pipeline = Pipeline(db_path=TMP / "run.db", settings=settings)
    pipeline.checkers = [AliveChecker(), EnrichChecker(), BrokenChecker()]
    pipeline.ctx.checkers = pipeline.checkers
    from pipeline.stages.check import CheckStage

    pipeline.check_stage = CheckStage(pipeline.ctx, pipeline.checkers)

    await pipeline.db.upsert_lines(LINES)
    report = await asyncio.wait_for(pipeline.run(discovery=False, export=True), timeout=60)

    check("прогон завершился без ошибок", report["ok"], report.get("error") or "")
    stage = report.get("check", {})
    check("батчей обработано", stage.get("batches", 0) >= 3, stage.get("batches"))
    check("живые посчитаны", stage.get("ok", 0) > 0, stage.get("ok"))
    check("мёртвые посчитаны", stage.get("failed", 0) > 0, stage.get("failed"))
    check("очередь опустела", stage.get("queue", {}).get("pending") == 0, stage.get("queue"))
    check("сломанный чекер-дополнение не сорвал батчи",
          stage.get("queue", {}).get("failed") == 0, stage.get("queue"))

    store = pipeline.db.store
    rows = store.export_lines()
    check("все серверы в базе", len(rows) == len(LINES), len(rows))
    check("в строке есть пинг и stable", any("-ping-" in r and "-stable-" in r for r in rows))
    check("страна записана в базу", any("\U0001F1E9" in r for r in rows))
    check("профиль записан в БД",
          all("Global" in v for v in store.load_capabilities_map().values()))
    check("[Global] добавляется только на экспорте",
          any("[Global]" in t for t in store.export_tagged_lines(global_tag="Global")))
    check("у живых stable вырос до щита",
          all(int(r.rsplit("-stable-", 1)[1]) == 96 for r in rows if "-ping-" in r))

    # Повторный прогон: этап проверки должен переиспользоваться корректно.
    report2 = await asyncio.wait_for(pipeline.run(discovery=False, export=True), timeout=60)
    check("повторный прогон отработал", report2["check"]["batches"] >= 3,
          report2["check"]["batches"])

    export = report2.get("export", {})
    check("merge.txt и blacklist.txt не выгружаются (состояние живёт в БД)",
          not (TMP / "merge.txt").exists() and not (TMP / "blacklist.txt").exists(),
          export)
    check("whitelist.txt записан", (TMP / "whitelist.txt").exists(), export.get("whitelist"))
    await pipeline.close()


async def test_singbox_lock() -> None:
    """Запуски sing-box не должны пересекаться: генератор конфига меняет
    cwd и глобальное состояние процесса."""
    print("\n--- сериализация запусков sing-box ---")
    from pipeline.checkers.builtin._runner import get_lock, run_exclusive

    active = 0
    peak = 0

    def job():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.05)
        active -= 1

    await asyncio.gather(*(run_exclusive(job) for _ in range(4)))
    check("ни один запуск sing-box не пересэкся с другим", peak == 1,
          "пик одновременных = " + str(peak))
    check("замок переиспользуемый", get_lock() is get_lock())


async def test_redaction() -> None:
    """Токен в URL деплоя не должен попадать в логи и отчёты."""
    print("\n--- маскирование секретов ---")
    from pipeline.stages.export import redact

    secret = "https://ghp_SECRET123:x-oauth-basic@raw.githubusercontent.com/o/r/main/c.json"
    masked = redact(secret)
    check("токен замаскирован", "ghp_SECRET123" not in masked, masked)
    check("хост и путь сохранены", "raw.githubusercontent.com/o/r/main/c.json" in masked)
    plain = "https://raw.githubusercontent.com/o/r/main/c.json"
    check("обычный URL не тронут", redact(plain) == plain)



async def test_real_checkers_setup() -> None:
    """Штатные чекеры должны проходить setup на реальном контексте.

    Ловит ошибки вида «метод есть не на том объекте» — их не видит прогон
    с подставными чекерами.
    """
    print("\n--- подготовка штатных чекеров ---")
    from pipeline.checkers import build_checkers
    from pipeline.context import PipelineContext, default_settings

    db = Database(TMP / "setup.db")
    await db.upsert_lines(LINES)
    ctx = PipelineContext(settings=default_settings(), db=db)
    # ВСЕ четыре, а не три. Раньше speed был исключён из списка, и
    # тесты были зелёными при модуле чекера скорости, который не импортируется
    # вообще: ошибка в нём обнаружилась лишь на живом запуске. Список задан
    # явно и неполон ровно настолько, чтобы это повторить.
    checkers = build_checkers("url_probe,country,reachability,speed")
    names = [c.name for c in checkers]
    check("реестр отдал все четыре чекера",
          names == ["url_probe", "country", "reachability", "speed"], names)

    failures = []
    for checker in checkers:
        try:
            await checker.setup(ctx)
        except Exception as exc:  # noqa: BLE001
            failures.append(checker.name + ": " + type(exc).__name__ + ": " + str(exc))
    check("setup всех штатных чекеров проходит", not failures, failures)
    await db.close()


async def test_every_module_imports() -> None:
    """Каждый модуль проекта обязан импортироваться.

    Тесты проверяют поведение выбранных модулей и молчат, если какой-то
    модуль не импортируется вовсе. Именно так уехала ошибка в чекере
    скорости: 265 проверок проходили, а прямой импорт модуля падал. Модуль
    настроек держит список настроек, и опечатка в имени даёт ошибку только
    в том, кто это имя читает, — то есть позже всего.
    """
    print("\n--- импорт всех модулей ---")
    import importlib

    root = Path(__file__).resolve().parent.parent
    broken: list[str] = []
    checked = 0
    for path in sorted(root.rglob("*.py")):
        if ".venv" in str(path) or path.name == "__init__.py":
            continue
        rel = path.relative_to(root)
        if rel.parts[0] == "tests":
            continue
        mod = ".".join(rel.with_suffix("").parts)
        checked += 1
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            broken.append(mod + ": " + type(exc).__name__ + ": " + str(exc)[:90])
    check("модулей проверено", checked > 50, checked)
    check("все модули импортируются", not broken, broken)


async def test_discovery_local() -> None:
    """Этап поиска целиком на локальном файле-подписке (file://), без сети."""
    print("\n--- этап поиска (локальная подписка) ---")
    import json as _json

    root = TMP / "discovery"
    subs_dir = root / "config" / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)
    feed = root / "feed.txt"
    feed.write_text("\n".join(LINES), encoding="utf-8")
    urls_file = subs_dir / "urls.json"
    # Ключи в urls.json — числа (load_urls сортирует источники по ним).
    urls_file.write_text(_json.dumps({"1": feed.as_uri()}), encoding="utf-8")

    from pipeline.context import PipelineContext, default_settings
    from pipeline.stages.discovery import DiscoveryStage

    db = Database(TMP / "discovery.db")
    settings = default_settings().merged({"urls_file": str(urls_file)})
    ctx = PipelineContext(settings=settings, db=db)
    report = await DiscoveryStage(ctx).run(db)

    check("поиск отработал", report["ok"], report.get("error") or "")
    check("ссылки разобраны", report.get("parsable", 0) == len(LINES), report)
    check("серверы добавлены в базу", report.get("added", 0) == len(LINES), report)
    check("серверы поставлены в очередь", report.get("enqueued", 0) == len(LINES), report)
    check("очередь наполнена", (await db.queue_stats()).pending == len(LINES))
    await db.close()




async def test_urltest_parsing() -> None:
    """Разбор строк urltest: задержка должна читаться в обоих форматах.

    Формат менялся вместе с версией sing-box, и смена прошла молча: печать
    стала «available: 414ms» вместо «available (414ms)», регулярка перестала
    находить задержку, и все 1572 сервера остались с ping_ms = NULL. Из-за
    этого фильтр по задержке отбирал пустое множество.
    """
    print("\n--- разбор вывода urltest ---")
    from pipeline.collector import RESULT_RE

    def parse(line):
        m = RESULT_RE.search(line)
        return (m.group("tag"), m.group("state"), m.group("ping")) if m else None

    got = parse("outbound/urltest[proxy]: outbound b00001_000002 available: 414ms")
    check("новый формат sing-box 1.14 читает задержку",
          got is not None and got[2] == "414" and got[1] == "available", got)

    got = parse("outbound/urltest[proxy]: outbound b1 available (1919ms)")
    check("старый формат со скобками тоже читается",
          got is not None and got[2] == "1919", got)

    got = parse("outbound/urltest[proxy]: outbound b2 available(88ms)")
    check("скобки без пробела тоже читаются",
          got is not None and got[2] == "88", got)

    got = parse("outbound/urltest[proxy]: outbound bad unavailable")
    check("у unavailable задержки нет — это не ошибка разбора",
          got is not None and got[1] == "unavailable" and got[2] is None, got)

    got = parse("outbound/urltest[proxy]: outbound \U0001F1E8\U0001F1E6 [x] ss-CA#15 available: 120ms")
    check("тег с эмодзи и пробелами не ломает разбор",
          got is not None and got[0].endswith("ss-CA#15") and got[2] == "120", got)

    check("строка без outbound/urltest игнорируется",
          RESULT_RE.search("outbound/mixed[x]: tcp server started") is None)

    # Сквозная проверка: вердикт ALIVE обязан нести задержку.
    from pipeline.collector import BatchCollector, Verdict
    col = BatchCollector(expected=1, name="t")
    col.feed("outbound/urltest[proxy]: outbound t1 available: 750ms")
    res = list(col.results.values())
    check("сборщик положил задержку в вердикт",
          bool(res) and res[0].ping_ms == 750 and res[0].verdict is Verdict.ALIVE,
          [(r.verdict, r.ping_ms) for r in res])
    col2 = BatchCollector(expected=1, name="t")
    col2.feed("outbound/urltest[proxy]: outbound t2 unavailable: dial tcp i/o timeout")
    res2 = list(col2.results.values())
    check("у мёртвого сервера задержка остаётся пустой",
          bool(res2) and res2[0].verdict is Verdict.DEAD and res2[0].ping_ms is None,
          [(r.verdict, r.ping_ms) for r in res2])


async def test_settings_loading() -> None:
    """Загрузка настроек: приоритеты, типы, устойчивость к мусору."""
    print("\n--- загрузка настроек ---")
    from config.settings import (
        Settings, get_settings, init_settings, parse_level, read_env_file,
        setting, _BY_ENV_NAME,
    )

    tmp = TMP / "env_for_settings"
    tmp.mkdir(parents=True, exist_ok=True)
    env_file = tmp / ".env"
    env_file.write_text(
        "# комментарий\n"
        "URLTEST_BATCH_SIZE=42\n"
        'REACHABILITY_GLOBAL_TAG="Из .env"\n'
        "PIPELINE_CHECK_WORKERS=not-a-number\n"
        "SPEED_ENABLED=no\n",
        encoding="utf-8",
    )
    check(".env разбирается: комментарии и кавычки",
          read_env_file(env_file) == {
              "URLTEST_BATCH_SIZE": "42",
              "REACHABILITY_GLOBAL_TAG": "Из .env",
              "PIPELINE_CHECK_WORKERS": "not-a-number",
              "SPEED_ENABLED": "no",
          }, read_env_file(env_file))
    check("отсутствующий файл не мешает",
          read_env_file(tmp / "нет-такого.env") == {})

    s = Settings.from_env(overrides={}, env_file=env_file)
    check("значение из .env прочитано", s.urltest.batch_size == 42, s.urltest.batch_size)
    check("кавычки сняты", s.reach.global_tag == "Из .env", s.reach.global_tag)
    # Умолчание здесь — из поля Settings (2), а не из репозиторного
    # .env: тест читает СВОЙ файл, поэтому значения репозитория не видны.
    check("битое число -> умолчание, а не падение",
          s.pipeline.check_workers == 2, s.pipeline.check_workers)
    check("строка «no» понята как False", s.speed.enabled is False, s.speed.enabled)
    check("PIPELINE_BATCH_SIZE наследует URLTEST_BATCH_SIZE",
          s.pipeline.batch_size == 42, s.pipeline.batch_size)

    # Перекрытие кодом сильнее .env И сильнее мусора.
    check("override сильнее .env",
          Settings.from_env(overrides={"URLTEST_BATCH_SIZE": 7},
                            env_file=env_file).urltest.batch_size == 7)
    # И приводится к типу: мусор в override не должен уехать строкой.
    check("override тоже приводится к типу",
          Settings.from_env(overrides={"URLTEST_BATCH_SIZE": "ерунда"},
                            env_file=env_file).urltest.batch_size == 42)

    check("уровень понимает имя и число",
          parse_level("DEBUG") == 10 and parse_level("25") == 25)
    check("неизвестный уровень не роняет", parse_level("ерунда") == 20)
    # Сам тестовый запуск выставляет LOG_CONSOLE_LEVEL в окружении
    # процесса (см. _prepare_env), а системное окружение важнее .env.
    # Поэтому наследование проверяем, убрав его на время проверки.
    saved_console = os.environ.pop("LOG_CONSOLE_LEVEL", None)
    try:
        check("LOG_CONSOLE_LEVEL наследует LOG_LEVEL",
              Settings.from_env(overrides={"LOG_LEVEL": "WARNING"},
                                env_file=env_file).log.console_level == 30)
    finally:
        if saved_console is not None:
            os.environ["LOG_CONSOLE_LEVEL"] = saved_console

    # Пути считаются от корня, а не из .env.
    check("путь от корня проекта",
          s.paths.servers_db_file.name == "servers.db", s.paths.servers_db_file)
    check("переопределяемый путь берётся из окружения",
          str(Settings.from_env(
              overrides={"CONFIG_TEMPLATE_DIR": r"C:\шблоны"},
              env_file=env_file).paths.config_template_dir) == r"C:\шблоны")

    check("все пути заполнены",
          all(getattr(s.paths, n) is not None for n in (
              "urls_file", "servers_db_file", "urltest_template",
              "sing_box_path", "custom_checkers_dir"))
          and s.log.dir_path is not None)
    check("теги скорости собраны",
          s.speed.tier_tags["fast"] == "speed-fast"
          and s.speed.tier_tags["geo"] == "speed-geo", s.speed.tier_tags)

    # as_dict ключуется именем из .env: полей с одинаковым именем (timeout,
    # concurrency, enabled) по нескольку, и по именам полей словарь молча
    # терял бы часть настроек.
    d = s.as_dict()
    check("as_dict отдаёт все 90 настроек", len(d) == 90, len(d))
    check("as_dict ключуется именами из .env", "SPEED_ENABLED" in d, sorted(d)[:3])
    check("as_dict без потерь на повторяющихся именах",
          d["URLTEST_TIMEOUT"] == 10.0 and d["COUNTRY_CHECK_TIMEOUT"] == 6.0
          and d["REACHABILITY_TIMEOUT"] == 6.0, "таймауты слиплись")

    # Один объект на процесс + понятная ошибка вместо AttributeError.
    got = init_settings()
    check("init_settings отдаёт объект", isinstance(got, Settings))
    check("get_settings отдаёт тот же объект", get_settings() is got)

    # Повторный init_settings() без перекрытий обязан вернуть тот же объект,
    # а не пересобрать его: иначе второй вызов тихо отменил бы перекрытия
    # первого (например аргументы командной строки).
    check("повторный init_settings() не пересобирает",
          init_settings() is got)
    check("перекрытие переживает повторный вызов",
          init_settings(PIPELINE_BATCH_SIZE=77).pipeline.batch_size == 77,
          "перекрытие потерялось: повторный вызов его обнулил")
    init_settings(PIPELINE_BATCH_SIZE=s.urltest.batch_size)

    # Неизвестное имя должно ПАдать. Раньше setting() возвращал None, и
    # опечатка уезжала в сравнение и всплывала позже, в другом месте.
    # Именно так потерялся SPEED_TIER_TAGS: поля не было в таблице
    # синонимов, и ошибка обнаружилась на живом запуске, а не в тестах.
    try:
        setting("SPEED_ENABLE")
        check("опечатка в имени падает", False, "исключения не было")
    except KeyError as exc:
        check("опечатка в имени падает", True)
        check("в ошибке есть подсказка", "SPEED_ENABLED" in str(exc), str(exc)[:120])

    # Атрибутный доступ ловит опечатку сам, без всякой проверки строк.
    try:
        s.speed.enabld
        check("опечатка в атрибуте падает сама", False, "исключения не было")
    except AttributeError:
        check("опечатка в атрибуте падает сама", True)

    # Каждая настройка .env доступна и по секции, и по имени — значения те же.
    # Сравниваем с s — он собран из файла теста. got получен раньше, ДО
    # перекрытия PIPELINE_BATCH_SIZE=77, и к этому моменту уже другой.
    _g = get_settings()
    check("секции и имена дают одно значение",
          all(setting(n) == getattr(getattr(_g, sec), fld)
              for n, (sec, fld) in _BY_ENV_NAME.items()), "расхождение секций")

async def test_best_tags() -> None:
    """Отбор «лучших»: лучший по профилю получает дополнительный тег."""
    print("\n--- метки лучших серверов ---")
    from script.best_tags import (
        BestRules, add_best_tags, caps_of, pick_best, rank_group,
    )

    recs = [
        {"key": "a", "speed_mbps": 2.0, "capabilities": "gemini,openai"},
        {"key": "b", "speed_mbps": 9.0, "capabilities": "gemini,openai"},
        {"key": "c", "ping_ms": 50, "capabilities": "gemini"},
        {"key": "d", "capabilities": "gemini"},
    ]

    check("профиль разбирается из строки capabilities",
          caps_of(recs[0]) == ["gemini", "openai"], caps_of(recs[0]))

    best = pick_best(recs)
    check("лучший по скорости выбран в обоих профилях",
          best.get("gemini") == ["b"] and best.get("openai") == ["b"], best)

    # Главная ловушка: задержка 50 мс численно больше 9 МБ/с, но сравнивать
    # миллисекунды с мегабайтами в секунду бессмысленно. Рейтинг обязан
    # держать группы в одних единицах.
    ranked = rank_group(recs)
    check("в одном рейтинге только один признак",
          {name for name, _rec, _val in ranked} == {"speed_mbps"},
          [(n, r["key"]) for n, r, _v in ranked])
    check("больше скорость — выше место",
          [r["key"] for _n, r, _v in ranked] == ["b", "a"],
          [r["key"] for _n, r, _v in ranked])

    marked = add_best_tags(recs)
    tags = {r["key"]: r["capabilities"] for r in marked}
    check("лучший получил тег best",
          "gemini-best" in tags["b"] and "openai-best" in tags["b"], tags["b"])
    check("остальные не тронуты",
          tags["a"] == "gemini,openai" and tags["d"] == "gemini", tags)
    check("исходные записи не мутируются",
          recs[1]["capabilities"] == "gemini,openai", recs[1]["capabilities"])

    check("без измерений лучших нет",
          pick_best([{"key": "x", "capabilities": "gemini"}]) == {})

    # Теги скорости ранжировать бессмысленно: там «лучший» — это сам тег.
    tier = [{"key": "t1", "speed_mbps": 1.0, "capabilities": "speed-slow"},
            {"key": "t2", "speed_mbps": 5.0, "capabilities": "speed-slow"}]
    check("теги скорости исключены из ранжирования",
          pick_best(tier, skip_profiles=("speed-slow",)) == {})

    check("top задаёт число лучших",
          len(pick_best(recs, rules=BestRules(top=2)).get("gemini", [])) == 2)
    small = [{"key": "s1", "speed_mbps": 1.0, "capabilities": "gemini"}]
    check("из одного участника лучший не выбирается", pick_best(small) == {})

    blind = [{"key": "q1", "capabilities": "gemini"},
             {"key": "q2", "capabilities": "gemini"}]
    check("группа без замеров остаётся без меток", pick_best(blind) == {})

    slow_ping = [{"key": "p1", "ping_ms": 900, "capabilities": "gemini"},
                 {"key": "p2", "ping_ms": 120, "capabilities": "gemini"}]
    check("без скорости ранжирует задержка, меньше — лучше",
          pick_best(slow_ping).get("gemini") == ["p2"], pick_best(slow_ping))


async def test_collector_three_states() -> None:
    """Служба 2 различает ответил / не ответил / не отозвался."""
    print("\n--- служба 2: три состояния ---")
    from pipeline.collector import Verdict, summarize

    flag = "\U0001F1E9\U0001F1EA"
    alive_tag = flag + " [prov] b000000"
    dead_tag = flag + " [prov] b000001"
    dns_tag = flag + " [prov] b000002"
    lines = [
        "INFO[0000] sing-box started (0.03s)",
        "DEBUG[0000] outbound/urltest[proxy]: outbound " + alive_tag + " available (127ms)",
        "DEBUG[0005] outbound/urltest[proxy]: outbound " + dead_tag + " unavailable: dial tcp 1.2.3.4:443: i/o timeout",
        "DEBUG[0005] outbound/urltest[proxy]: outbound " + dns_tag + " unavailable: lookup dead.example: no such host",
        "DEBUG[0015] dns: lookup domain dead.example",
    ]
    col = summarize(lines, expected=4, name="тест")
    check("разобрано 3 ответа", len(col.results) == 3, col.counts())
    check("доступный сервер получил пинг", col.verdict(alive_tag).ping_ms == 127)
    check("недоступный помечен DEAD", col.verdict(dead_tag).verdict is Verdict.DEAD)
    check("причина неудачи про dial", "dial tcp" in col.verdict(dead_tag).reason)
    check("причина неудачи про dns", col.verdict(dns_tag).reason == "dns")
    check("четвёртый сервер не отозвался", col.verdict("b000003").verdict is Verdict.NO_RESULT)
    counts = col.counts()
    check("счёт: 1 жив, 2 мёртвых, 1 молчит",
          (counts["alive"], counts["dead"], counts["silent"]) == (1, 2, 1), counts)
    check("посчитан таймаут соединения", col.dial_timeouts >= 1, col.dial_timeouts)
    check("посчитана ошибка dns", col.dns_failures >= 1, col.dns_failures)

    incremental = summarize([], expected=4, name="построчно")
    for line in lines:
        incremental.feed(line)
    check("построчно и пачкой дают одно и то же",
          incremental.counts() == counts, (incremental.counts(), counts))

    # Вывод приходит блоками, а не построчно: строка может порваться
    # посередине. Случайные разбиения на куски обязаны давать тот же результат,
    # что и построчный разбор.
    import random

    text = "\n".join(lines)
    random.seed(7)
    mismatches = 0
    for _ in range(200):
        chunked = summarize([], expected=4, name="кусками")
        pos = 0
        while pos < len(text):
            step = random.randint(1, 40)
            chunked.feed_chunk(text[pos:pos + step])
            pos += step
        chunked.flush()
        if chunked.counts() != counts or chunked.results.keys() != col.results.keys():
            mismatches += 1
    check("200 случайных разбиений на куски дают тот же результат",
          mismatches == 0, f"расхождений: {mismatches}")



async def test_batch_planning() -> None:
    """Дробление остатка на параллельные батчи."""
    print("\n--- планирование батчей ---")
    from pipeline.batch import plan_batches

    check("131 сервер, батч 100, 3 слота -> [100, 15, 16]",
          plan_batches(131, 100, 3) == [100, 15, 16], plan_batches(131, 100, 3))
    check("30 серверов, батч 100, 3 слота -> [10, 10, 10]",
          plan_batches(30, 100, 3) == [10, 10, 10], plan_batches(30, 100, 3))
    check("200 серверов, батч 100, 2 слота -> [100, 100]",
          plan_batches(200, 100, 2) == [100, 100], plan_batches(200, 100, 2))
    check("ничего не теряется при дроблении",
          sum(plan_batches(131, 100, 3)) == 131 and sum(plan_batches(30, 100, 3)) == 30)
    check("ни один батч не больше размера",
          all(size <= 100 for size in plan_batches(131, 100, 3)))
    check("пусто -> пусто", plan_batches(0, 100, 3) == [])
    check("остаток меньше двух минимальных не дробится",
          plan_batches(10, 100, 3, min_chunk=8) == [10], plan_batches(10, 100, 3, min_chunk=8))


async def test_batch_clock() -> None:
    """У каждого батча свой дедлайн, пропорциональный размеру."""
    print("\n--- личные таймауты батчей ---")
    from pipeline.batch import BatchClock

    big = BatchClock(100, base_timeout=100, batch_size=100, min_timeout=15)
    small = BatchClock(10, base_timeout=100, batch_size=100, min_timeout=15)
    tiny = BatchClock(1, base_timeout=100, batch_size=100, min_timeout=15)

    # Дедлайн = старт + запас + 5с * 1.5 * ceil(N/10). Раньше доля была
    # пропорцией размера батча к размеру батча, из-за чего полный батч
    # получал константу независимо от числа серверов, а sing-box с его
    # 10 одновременными пробами физически не успевал отдать все вердикты.
    waves_100 = math.ceil(100 / BatchClock.PROBE_SLOTS)
    waves_10 = math.ceil(10 / BatchClock.PROBE_SLOTS)
    want_big = 15 + 100 + BatchClock.PROBE_STEP * BatchClock.HEADROOM * waves_100
    want_small = 15 + 100 + BatchClock.PROBE_STEP * BatchClock.HEADROOM * waves_10
    check("дедлайн считается по волнам sing-box, а не пропорцией батча",
          abs(big.timeout - want_big) < 0.01, (big.timeout, want_big))
    check("малый батч получает меньший таймаут", small.timeout < big.timeout, small.timeout)
    check("малый батч всё равно получает время на старт", small.timeout >= 15, small.timeout)
    check("таймаут не обнуляется на крошечных батчах", tiny.timeout >= 15, tiny.timeout)
    check("дедлайн считается от запуска", big.remaining <= big.timeout)

    # Дедлайн обязан перекрывать измеренное время батча: на реальных
    # серверах 100 ответили за ~54 с, 200 — за ~134 с.
    for n, measured in ((100, 53.9), (200, 133.7)):
        clock = BatchClock(n, base_timeout=20, batch_size=100, min_timeout=10)
        check(f"дедлайн для {n} серверов перекрывает измеренное",
              clock.timeout > measured, (clock.timeout, measured))


async def test_failure_classification() -> None:
    """Сбой конфига отличается от занятого порта и от «просто нет ответа»."""
    print("\n--- разбор причин сбоя батча ---")
    from pipeline.batch import BatchOutcome
    from pipeline.singbox import classify_failure

    bind = ["FATAL[0000] start service: listen tcp 127.0.0.1:7891: bind: Only one usage of each socket"]
    cfg = ['FATAL[0000] decode config: json: unknown field "outboundz"']
    outcome, reason = classify_failure(bind)
    check("занятый порт опознан и назван словом 'порт'",
          outcome is BatchOutcome.CONFIG_ERROR and "порт" in reason, reason)
    outcome, reason = classify_failure(cfg)
    check("битый конфиг опознан",
          outcome is BatchOutcome.CONFIG_ERROR and "конфиг" in reason, reason)
    outcome, reason = classify_failure([])
    check("без строк с ошибкой это отсутствие результатов, а не падение",
          outcome is BatchOutcome.NO_RESULTS, outcome)



async def test_enrichment_keeps_stable() -> None:
    """Обогащение не должно второй раз двигать stable за тот же цикл."""
    print("\n--- обогащение не трогает stable ---")
    db = Database(TMP / "enrich.db")
    await db.upsert_lines(LINES)
    flag = "\U0001F1E9\U0001F1EA"
    key = list(await db.load_stable_map())[0]
    before = (await db.load_stable_map())[key]
    updated = await db.record_enrichment(
        [{"key": key, "country": flag, "capabilities": "Global"}])
    after = (await db.load_stable_map())[key]
    check("обогащение записало запись", updated == 1, updated)
    check("stable не изменился", after == before, (before, after))
    check("страна записалась в базу",
          (await db.load_country_map()).get(key) == flag)
    await db.record_enrichment([{"key": key, "country": "", "capabilities": ""}])
    check("пустое значение не затирает страну",
          (await db.load_country_map()).get(key) == flag)
    await db.close()



async def test_config_error_split() -> None:
    """Батч, не собравшийся из-за кривой строки, делится пополам.

    Проверяем ровно то, ради чего деление и делалось: плохая строка
    находится, а остальные батчи всё равно проверяются.
    """
    print("\n--- деление сбойного батча пополам ---")
    import pipeline.singbox as sb
    from pipeline.batch import BatchOutcome, BatchReport
    from pipeline.dispatcher import BatchDispatcher

    bad_line = "ss://BROKEN-LINE-THAT-CANNOT-BE-PARSED"
    good = [f"ss://good-{i}" for i in range(8)]
    lines = good[:4] + [bad_line] + good[4:]
    seen_sizes: list[int] = []

    async def fake_run_batch(batch_lines, **kwargs):
        seen_sizes.append(len(batch_lines))
        if bad_line in batch_lines:
            return BatchReport(
                index=kwargs.get("index", 0),
                size=len(batch_lines),
                outcome=BatchOutcome.CONFIG_ERROR,
                error="decode config: сломанная строка",
                retry_lines=list(batch_lines),
            )
        report = BatchReport(index=kwargs.get("index", 0), size=len(batch_lines),
                             outcome=BatchOutcome.COMPLETED)
        for i, line in enumerate(batch_lines):
            tag = f"t{i:06d}"
            report.tag_to_line[tag] = line
            from pipeline.collector import ServerResult, Verdict
            report.verdicts[tag] = ServerResult(tag=tag, verdict=Verdict.ALIVE, ping_ms=50)
            report.alive += 1
        return report

    original = sb.run_batch
    sb.run_batch = fake_run_batch
    try:
        dispatcher = BatchDispatcher(
            None, batch_size=100, slots=1, max_split_depth=3,
            singbox_path=Path("sing-box.exe"),
            template_path=Path("config/urltest_template.json"),
            config_dir=TMP / "cfgsplit",
        )
        reports = await dispatcher.run_lines(lines)
    finally:
        sb.run_batch = original

    check("исходный набор режется пополам", seen_sizes[0] == 9, seen_sizes)
    check("после деления проверяются части", max(seen_sizes[1:]) <= 5, seen_sizes)

    checked: set[str] = set()
    for report in reports:
        for raw in report.tag_to_line.values():
            checked.add(raw)
    check("хорошие серверы проверены несмотря на сбой",
          set(good).issubset(checked), sorted(set(good) - checked))

    # Кривая строка обязана остаться непроверенной, а не «мёртвой».
    bad_reports = [r for r in reports if bad_line in (r.unparsable or [])]
    check("кривая строка найдена и помечена непригодной", len(bad_reports) == 1,
          [r.unparsable for r in reports])



async def test_retry_survives_completion() -> None:
    """Сервер, отправленный на повтор, не должен закрываться как сделанный.

    Это ровно тот случай, из-за которого повтор молча пропадал: батч отдавал
    строки обратно в очередь, а следующий вызов тут же ставил им status=done.
    """
    print("\n--- повтор переживает complete_batch ---")
    db = Database(TMP / "retry.db")
    await db.upsert_lines(LINES)
    await db.enqueue(LINES)
    items = await db.claim_batch(len(LINES))
    check("батч взят", len(items) == len(LINES), len(items))

    keep = [items[0].line, items[2].line]
    retry = [item.line for item in items if item.line not in keep]
    await db.enqueue(retry, source="retry")

    from pipeline.dispatcher import _key_of
    skip = {_key_of(line) for line in retry}
    await db.complete_batch(items, skip_keys=skip)

    stats = await db.queue_stats()
    check("повтор остался в очереди", stats.pending == len(retry), stats.as_dict())
    check("проверенные строки закрыты", stats.done == len(keep), stats.as_dict())

    # Повтор действительно можно взять снова — это и есть «ещё один круг».
    again = await db.claim_batch(len(retry))
    check("повторный батч берёт именно повторные строки",
          {item.line for item in again} == set(retry),
          [i.line[:30] for i in again])

    # attempts вырос, значит круги считаются и потолок ретраев работает.
    attempts = {item.line: item.attempts for item in again}
    check("счётчик попыток вырос у повторных строк",
          all(v >= 1 for v in attempts.values()), attempts)
    await db.close()



async def test_speed_checker() -> None:
    """Категория скорости и распознавание блокировки — без обращения к сети."""
    print("\n--- чекер скорости ---")
    from script.speed_check import SpeedConfig, detect_blocked, tier_of

    cfg = SpeedConfig(min_mbps=1.0, good_mbps=5.0)
    check("быстрый сервер -> fast", tier_of(9.0, cfg, False) == "fast")
    check("средний -> ok", tier_of(2.5, cfg, False) == "ok")
    check("на границе ok", tier_of(1.0, cfg, False) == "ok", tier_of(1.0, cfg, False))
    check("медленный -> slow", tier_of(0.3, cfg, False) == "slow")
    check("без замера -> unknown", tier_of(None, cfg, False) == "unknown")
    check("заблокирован важнее скорости", tier_of(50.0, cfg, True) == "blocked")

    # Адрес замера теперь собирает script/speed_sources.py: у части
    # источников объём задаётся прямо в ссылке.
    from script.speed_sources import (
        BY_KEY, DEFAULT_SOURCES, SpeedSource, average, download_url,
        resolve_sources, SourceResult, SpeedSample,
    )

    url = download_url(BY_KEY["cloudflare"], 4194304)
    check("в URL замера добавлен размер", url.endswith("bytes=4194304"), url)
    odd = SpeedSource("x", "X", "https://x.dev/down?a=1", bytes_query=True)
    check("разделитель не ломается, если уже есть параметр",
          download_url(odd, 4194304) == "https://x.dev/down?a=1&bytes=4194304",
          download_url(odd, 4194304))
    plain = SpeedSource("y", "Y", "https://x.dev/down")
    check("источник без поддержки объёма не трогает адрес",
          download_url(plain, 100) == "https://x.dev/down")

    # --- несколько источников одновременно ---
    check("источников по умолчанию пять", len(DEFAULT_SOURCES) == 5,
          len(DEFAULT_SOURCES))
    keys = [s.key for s in DEFAULT_SOURCES]
    check("источники из разных сетей", len(set(keys)) == 5, keys)
    # Порядок пользователя сохраняется: если он перечислил источники в
    # определённом порядке, замер пойдёт именно в таком порядке, а не по
    # алфавиту — так видно в логе, что было задумано.
    check("порядок пользователя сохраняется",
          [s.key for s in resolve_sources(["google", "ovh"])] == ["google", "ovh"],
          [s.key for s in resolve_sources(["google", "ovh"])])
    check("выбор строкой работает",
          [s.key for s in resolve_sources("ovh,cloudflare")] == ["ovh", "cloudflare"],
          [s.key for s in resolve_sources("ovh,cloudflare")])
    check("повтор источника не дублируется",
          len(resolve_sources(["ovh", "ovh"])) == 1)
    check("пустой список = все источники",
          resolve_sources(()) == DEFAULT_SOURCES)
    try:
        resolve_sources("hetzner")
        check("неизвестный источник — ошибка", False)
    except ValueError as exc:
        check("неизвестный источник — ошибка с подсказкой",
              "hetzner" in str(exc) and "cloudflare" in str(exc), str(exc)[:90])

    # --- правило усреднения ---
    good = [SourceResult("a", "down", True, 1, 0.1, 10.0),
            SourceResult("b", "down", True, 1, 0.1, 20.0)]
    bad = [SourceResult("c", "down", False, 0, 0.0, None, "обрыв")]
    avg, n = average(good + bad, "down")
    check("среднее только по ответившим", abs(avg - 15.0) < 0.001 and n == 2, (avg, n))
    avg2, n2 = average(bad, "down")
    check("нет ответов — среднего нет", avg2 is None and n2 == 0, (avg2, n2))
    mixed = good + [SourceResult("d", "up", True, 1, 0.25, 4.0)]
    avg3, n3 = average(mixed, "up")
    check("направления считаются раздельно", avg3 == 4.0 and n3 == 1, (avg3, n3))

    # --- сводка замера ---
    s_ok = SpeedSample(down_mbps=10.0, up_mbps=4.0, total_mbps=7.0, sources=tuple(mixed))
    check("итог — среднее из двух направлений", s_ok.total_mbps == 7.0)
    s_half = SpeedSample(down_mbps=10.0, up_mbps=None, total_mbps=10.0,
                         sources=tuple(good))
    check("одно направление — итог равен ему", s_half.total_mbps == 10.0)
    s_none = SpeedSample(total_mbps=None, sources=tuple(bad))
    check("без успешных замеров итог не выдумывается",
          s_none.total_mbps is None and s_none.ok_sources == 0)
    check("в сводке видно, кто ответил",
          "down:a=10.00" in __import__("script.speed_sources", fromlist=["x"])
          .describe(s_ok))

    check("403 считается блокировкой", detect_blocked(403, ""))
    check("429 считается блокировкой", detect_blocked(429, ""))
    check("200 с нормальным телом не блокировка", not detect_blocked(200, "<html>ok</html>"))
    check("редирект на вход — блокировка",
          detect_blocked(200, "", "https://accounts.google.com/signin"))
    check("капча в теле — блокировка",
          detect_blocked(200, "Our systems have detected unusual traffic"))
    check("нет ответа — не блокировка, а недоступность",
          not detect_blocked(None, ""))


async def test_capabilities_merge() -> None:
    """Чекеры-дополнения не затирают друг друга в capabilities."""
    print("\n--- capabilities копятся, а не затираются ---")
    from script.server_store import ServerStore, _merge_tags

    check("новый тэг добавляется к существующему",
          _merge_tags("gemini,openrouter", "speed-fast") == "gemini,openrouter,speed-fast",
          _merge_tags("gemini,openrouter", "speed-fast"))
    check("повтор того же тэга не дублируется",
          _merge_tags("speed-fast", "speed-fast") == "speed-fast")
    check("порядок старых тэгов сохраняется",
          _merge_tags("Global", "speed-slow") == "Global,speed-slow")
    check("из пустого просто новый", _merge_tags("", "speed-ok") == "speed-ok")
    check("несколько новых тэгов через запятую",
          _merge_tags("gemini", "speed-fast,speed-blocked") == "gemini,speed-fast,speed-blocked")

    db = Database(TMP / "caps.db")
    await db.upsert_lines(LINES)
    key = list(await db.load_stable_map())[0]
    await db.record_enrichment([{"key": key, "capabilities": "gemini"}])
    await db.record_enrichment([{"key": key, "capabilities": "speed-fast"}])
    caps = (await db.load_capabilities_map()).get(key, "")
    check("в базе оба тэга пережили два прохода", caps == "gemini,speed-fast", caps)
    await db.close()



async def test_country_flag_reaches_export() -> None:
    """Флаг страны определён ПОСЛЕ записи строки — он всё равно должен попасть в экспорт."""
    print("\n--- флаг страны доходит до whitelist ---")
    from utils.tool import set_country_in_name

    line = "ss://Y2hhY2hh@1.2.3.4:9443#ss 000003-stable-96"
    tagged = set_country_in_name(line, "\U0001F1F0\U0001F1F7")
    check("флаг добавлен к строке без страны",
          tagged.endswith("#\U0001F1F0\U0001F1F7 ss 000003-stable-96"), tagged)
    check("имя прокси не потеряно", "000003-stable-96" in tagged)
    check("повторно страну не дублируем",
          set_country_in_name(tagged, "\U0001F1F0\U0001F1F7") == tagged)
    check("флаги разных стран не дублируются",
          set_country_in_name(tagged, "\U0001F1F1\U0001F1F7") == tagged)
    check("строка без имени получает флаг",
          set_country_in_name("ss://Y2hhY2hh@1.2.3.4:9443", "\U0001F1F0\U0001F1F7")
          == "ss://Y2hhY2hh@1.2.3.4:9443#\U0001F1F0\U0001F1F7")
    check("без страны строка не меняется", set_country_in_name(line, "") == line)

    import base64 as _b64
    import json as _json

    vmess = "vmess://" + _b64.b64encode(
        _json.dumps({"v": "2", "ps": "vless 0001-stable-5"}).encode()
    ).decode()
    vmess_tagged = set_country_in_name(vmess, "\U0001F1F0\U0001F1F7")
    ps = _json.loads(_b64.b64decode(vmess_tagged[8:]))["ps"]
    check("в vmess флаг попал в поле ps", ps.startswith("\U0001F1F0\U0001F1F7"), ps)

    # Сквозной путь: страна в колонке country -> флаг в экспортируемой строке.
    db = Database(TMP / "flags.db")
    await db.upsert_lines(["ss://Y2hhY2hh@9.9.9.9:9443#ss 000009-stable-5"])
    key = list(await db.load_stable_map())[0]
    await db.record_enrichment([{"key": key, "country": "\U0001F1F8\U0001F1EC"}])
    exported = db.store.export_tagged_lines()
    check("экспорт начинается с флага страны",
          any(l.split("#", 1)[1].startswith("\U0001F1F8\U0001F1EC ") for l in exported),
          exported)
    check("имя прокси при этом не потеряно",
          any("000009-stable-5" in l for l in exported), exported)
    await db.close()


async def test_geo_block_and_tag_replace() -> None:
    """Гео-блок Gemini — отдельная метка, категория скорости не копится."""
    print("\n--- гео-блок и замена категории ---")
    from script.speed_check import SpeedConfig, detect_geo_blocked, tier_of
    from script.server_store import _merge_tags

    cfg = SpeedConfig()
    check("текст 'не работает в вашей стране' распознан",
          detect_geo_blocked("Gemini isn't available in your country yet."))
    check("редирект на справку по странам распознан",
          detect_geo_blocked("", "https://support.google.com/gemini/answer/13575153"))
    check("обычная страница не гео-блок", not detect_geo_blocked("<html>Meet Gemini</html>"))
    check("гео важнее скорости", tier_of(50.0, cfg, False, True) == "geo")
    check("скорость меряется и при гео-блоке", tier_of(9.0, cfg, True, True) == "geo")
    check("без гео капча остаётся blocked", tier_of(9.0, cfg, True, False) == "blocked")

    # Категория ЗАМЕНЯЕТ старую, а не дописывается.
    check("старая speed-метка выброшена",
          _merge_tags("speed-slow", "speed-ok", replace=["speed-"]) == "speed-ok",
          _merge_tags("speed-slow", "speed-ok", replace=["speed-"]))
    check("чужие тэги сохраняются при замене",
          _merge_tags("gemini,openai,speed-slow", "speed-fast", replace=["speed-"])
          == "gemini,openai,speed-fast",
          _merge_tags("gemini,openai,speed-slow", "speed-fast", replace=["speed-"]))
    check("без replace старые метки копятся (профиль целей)",
          _merge_tags("gemini", "openai") == "gemini,openai")

    db = Database(TMP / "replace.db")
    await db.upsert_lines(LINES)
    key = list(await db.load_stable_map())[0]
    await db.record_enrichment([{"key": key, "capabilities": "speed-slow"}])
    await db.record_enrichment([{"key": key, "capabilities": "speed-fast",
                                "capabilities_replace": ["speed-"]}])
    caps = (await db.load_capabilities_map()).get(key, "")
    check("в базе осталась одна метка скорости", caps == "speed-fast", caps)
    await db.close()


# ------------------------------------------------- очередь вердиктов (службы)
async def test_queued_sink_defers_write() -> None:
    """Проверка в режиме служб не пишет в базу сама — вердикт уходит в очередь.

    Ровно этим QueuedSink отличается от DirectSink: пока порция лежит в
    result_queue, ни таблица серверов, ни очередь проверки не тронуты.
    Запись делает отдельная служба (см. следующий тест).
    """
    print("\n--- QueuedSink: вердикт ждёт в очереди ---")
    from pipeline.services.sinks import QueuedSink

    db = Database(TMP / "sink.db")
    await db.upsert_lines(LINES)
    await db.enqueue(LINES)
    items = await db.claim_batch(len(LINES))
    check("батч взят в работу", len(items) == len(LINES), len(items))

    stable_before = await db.load_stable_map()
    rows = [
        {"key": items[0].key, "available": True, "ping_ms": 42, "line": items[0].line},
        {"key": items[1].key, "available": False, "line": items[1].line},
    ]
    sink = QueuedSink(db)
    await sink.submit(rows=rows, retry=[items[2].line], unparsable=[],
                      items=list(items))

    stats = await db.result_stats()
    check("порция лежит в очереди результатов", stats["pending"] == 1, stats)
    check("sink засчитал одну отправку", sink.submitted == 1, sink.submitted)

    batches = await db.claim_results(10)
    check("порция забирается из очереди", len(batches) == 1, len(batches))
    batch = batches[0]
    check("в порции оба вердикта", len(batch.rows) == 2, batch.rows)
    check("в порции повтор", batch.retry == [items[2].line], batch.retry)
    check("в порции ключи, снятые с работы",
          set(batch.completed) == {item.key for item in items}, batch.completed)
    check("неразбираемых в порции нет", batch.unparsable == [], batch.unparsable)

    # Главное: до применения порции база серверов обязана остаться прежней.
    check("таблица серверов не тронута до записи",
          await db.load_stable_map() == stable_before)
    queue = await db.queue_stats()
    check("батч всё ещё в работе, повтор не вернулся",
          queue.pending == 0 and queue.in_progress == len(LINES) and queue.done == 0,
          queue.as_dict())

    # Совсем пустая порция не должна засорять очередь результатов.
    await QueuedSink(db).submit(rows=[], retry=[], unparsable=[], items=[])
    check("пустая порция не кладётся в очередь",
          (await db.result_stats())["pending"] == 1, await db.result_stats())
    await db.close()


async def test_results_service_applies_batches() -> None:
    """Служба записи забирает порции и переносит их в таблицу серверов."""
    print("\n--- служба записи применяет порции ---")
    from pipeline.context import PipelineContext, default_settings
    from pipeline.services.results import ResultsService
    from pipeline.services.sinks import QueuedSink

    db = Database(TMP / "results.db")
    await db.upsert_lines(LINES)
    await db.enqueue(LINES)
    items = await db.claim_batch(len(LINES))
    alive_key, dead_key = items[0].key, items[1].key
    stable_before = await db.load_stable_map()

    await QueuedSink(db).submit(
        rows=[{"key": alive_key, "available": True, "ping_ms": 33,
               "line": items[0].line},
              {"key": dead_key, "available": False, "line": items[1].line}],
        retry=[], unparsable=[], items=list(items))

    ctx = PipelineContext(settings=default_settings(), db=db)
    service = ResultsService(ctx, interval=0.01, limit=10)
    await service.setup()
    report = await service.run_once()

    check("служба включилась", service.enabled)
    check("порция применена", service.applied == 1, report)
    check("счётчики службы: живых 1, мёртвых 1",
          (service.alive, service.dead) == (1, 1), (service.alive, service.dead))

    check("очередь вердиктов опустела", (await db.result_stats())["pending"] == 0,
          await db.result_stats())
    check("применённая порция больше не выдаётся", await db.claim_results(10) == [])

    stable_after = await db.load_stable_map()
    check("живой сервер обновлён в таблице серверов",
          stable_after.get(alive_key, 0) > stable_before.get(alive_key, 0),
          (stable_before.get(alive_key), stable_after.get(alive_key)))
    check("мёртвый сервер обновлён и потерял stable",
          stable_after.get(dead_key, 0) < stable_before.get(dead_key, 0),
          (stable_before.get(dead_key), stable_after.get(dead_key)))
    # Вердикт должен быть виден в самой таблице серверов: пинг записан,
    # а в выборку живых (критерий whitelist) попал только отвечавший.
    import sqlite3

    conn = sqlite3.connect(TMP / "results.db")
    conn.row_factory = sqlite3.Row
    try:
        pings = {row["key"]: row["ping_ms"]
                 for row in conn.execute("SELECT key, ping_ms FROM servers")}
    finally:
        conn.close()
    check("пинг записан в таблицу серверов", pings.get(alive_key) == 33, pings)
    alive_lines = db.store.export_lines(only_available=True)
    check("в живые попал только отвечавший сервер",
          len(alive_lines) == 1 and alive_lines[0] == items[0].line, alive_lines)

    queue = await db.queue_stats()
    check("батч снят с работы",
          queue.in_progress == 0 and queue.done == len(LINES), queue.as_dict())

    # Пустая очередь — обычное состояние службы, лишний проход не должен падать.
    await service.run_once()
    check("проход по пустой очереди ничего не ломает",
          (service.applied, service.errors) == (1, 0), service.stats())
    await db.close()


async def test_retry_returns_to_queue_after_apply() -> None:
    """Повтор из порции возвращается в очередь и НЕ затирается как сделанный.

    Критичный случай: снятие батча с работы идёт после возврата повтора.
    Стоит забыть skip_keys — и повтор тут же станет done, а сервер молча
    потеряется: очередь пуста, крутить больше нечего.
    """
    print("\n--- повтор переживает apply_result ---")
    from pipeline.services.sinks import QueuedSink

    db = Database(TMP / "retry_apply.db")
    await db.upsert_lines(LINES)
    await db.enqueue(LINES)
    items = await db.claim_batch(len(LINES))
    retry_line = items[0].line
    keep = len(LINES) - 1

    await QueuedSink(db).submit(rows=[], retry=[retry_line], unparsable=[],
                              items=list(items))
    batch = (await db.claim_results(1))[0]
    await db.apply_result(batch)

    stats = await db.queue_stats()
    check("повтор вернулся в очередь со статусом pending",
          stats.pending == 1, stats.as_dict())
    check("это ровно одна повторная строка, а не весь батч",
          stats.pending == 1 and stats.in_progress == 0, stats.as_dict())
    check("остальные строки закрыты как сделанные",
          stats.done == keep, stats.as_dict())
    check("повтор не посчитан сделанным", stats.done == len(LINES) - 1, stats.as_dict())
    check("sync-чтение очереди видит тот же pending",
          read_queue_stats(TMP / "retry_apply.db").pending == 1,
          read_queue_stats(TMP / "retry_apply.db").as_dict())

    # Повтор действительно можно взять снова — это и есть «ещё один круг».
    again = await db.claim_batch(1)
    check("повторный батч берёт именно повторную строку",
          bool(again) and again[0].line == retry_line,
          [item.line[:40] for item in again])
    check("счётчик попыток у повтора вырос",
          bool(again) and again[0].attempts >= 1, again[0].attempts if again else None)
    check("очередь вердиктов после применения пуста",
          (await db.result_stats())["pending"] == 0)
    await db.close()


async def test_mark_result_failed() -> None:
    """Битая порция помечается разобранной с ошибкой и больше не перебирается."""
    print("\n--- битая порция не крутится вечно ---")
    import sqlite3

    from pipeline.context import PipelineContext, default_settings
    from pipeline.database import RESULT_QUEUE_TABLE
    from pipeline.services.results import ResultsService

    db = Database(TMP / "failed.db")
    batch_id = await db.push_result({"rows": [{"available": True}],
                                     "retry": [], "unparsable": [],
                                     "completed": []})
    check("порция получила номер", batch_id > 0, batch_id)

    await db.mark_result_failed(batch_id, "KeyError: 'key'")
    check("помеченная порция больше не выдаётся", await db.claim_results(10) == [])
    check("очередь результатов считает её разобранной",
          (await db.result_stats())["pending"] == 0, await db.result_stats())

    # Порция остаётся в таблице для разбора — но уже с текстом ошибки.
    conn = sqlite3.connect(TMP / "failed.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(RESULT_QUEUE_TABLE)
        stored = [dict(row) for row in conn.execute(
            "SELECT id, applied, error FROM result_queue ORDER BY id")]
    finally:
        conn.close()
    check("порция осталась в таблице для разбора", len(stored) == 1, stored)
    check("порция помечена обработанной", stored[0]["applied"] == 1, stored[0])
    check("текст ошибки сохранён", "KeyError" in stored[0]["error"], stored[0])

    # И главное: битая порция не должна блокировать нормальные за ней.
    await db.upsert_lines(LINES)
    await db.record_results([{"key": list(await db.load_stable_map())[0],
                              "available": True, "ping_ms": 12}])
    good_key = list(await db.load_stable_map())[0]
    await db.push_result({"rows": [{"key": good_key, "available": True,
                                    "ping_ms": 77, "line": "ss://x@1.2.3.4:443#ok"}],
                          "retry": [], "unparsable": [], "completed": []})

    ctx = PipelineContext(settings=default_settings(), db=db)
    service = ResultsService(ctx, interval=0.01, limit=10)
    await service.run_once()
    check("следующая нормальная порция применена", service.applied == 1,
          service.stats())
    check("очередь результатов опустела", (await db.result_stats())["pending"] == 0)
    await db.close()


async def test_collector_service_fills_queue() -> None:
    """Сборщик берёт живой пул из базы и ставит серверы в очередь проверки."""
    print("\n--- служба сборки батчей ---")
    from pipeline.context import PipelineContext, default_settings
    from pipeline.services.collector import CollectorService

    db = Database(TMP / "collector.db")
    await db.upsert_lines(LINES)
    ctx = PipelineContext(settings=default_settings(), db=db)
    service = CollectorService(ctx, interval=0.01)

    check("до прохода очередь пуста", (await db.queue_stats()).pending == 0)
    await service.setup()
    report = await service.run_once()

    check("служба включена по умолчанию", service.enabled)
    check("run_once вернул число добавленных серверов",
          service.added == len(LINES) and str(len(LINES)) in report,
          (report, service.added))
    stats = await db.queue_stats()
    check("очередь наполнена живым пулом из базы",
          stats.pending == len(LINES), stats.as_dict())

    # Второй проход ничего не теряет и не плодит дубликаты.
    await service.run_once()
    check("повторный проход не теряет серверы",
          (await db.queue_stats()).pending == len(LINES),
          (await db.queue_stats()).as_dict())
    check("счётчик добавленного копится по проходам",
          service.added == len(LINES) * 2, service.added)

    # Выключенный сборщик не должен ходить в базу.
    off = CollectorService(ctx, interval=0.01, enabled=False)
    await off.setup()
    off_report = await off.run_once()
    check("enabled=False выключает сборщик", not off.enabled, off_report)
    check("выключенный сборщик ничего не добавил",
          off.added == 0, off.added)
    check("выключенный сборщик базу не трогает",
          service.added == len(LINES) * 2, service.added)

    # Пустая база: возвращается ноль, а не исключение.
    empty = Database(TMP / "collector_empty.db")
    empty_ctx = PipelineContext(settings=default_settings(), db=empty)
    empty_service = CollectorService(empty_ctx, interval=0.01, enabled=True)
    await empty_service.setup()
    await empty_service.run_once()
    check("пустая база: добавлено 0", empty_service.added == 0, empty_service.added)
    await empty.close()
    await db.close()


async def test_service_loop_survives_failure() -> None:
    """Базовый цикл службы переживает падение и продолжает работать.

    Служба не должна умирать из-за одного сбоя прохода: ошибка считается,
    служба ждёт паузу и пробует снова.
    """
    print("\n--- цикл службы устойчив к падению ---")
    import pipeline.services.base as svc_base
    from pipeline.context import PipelineContext, default_settings
    from pipeline.services.base import Service

    class FlakyService(Service):
        """Первые два прохода падают, дальше работает."""

        name = "flaky"
        interval = 0.01

        def __init__(self, ctx) -> None:
            super().__init__(ctx)
            self.attempts = 0

        async def run_once(self) -> str:
            self.attempts += 1
            if self.attempts <= 2:
                raise RuntimeError("сбой прохода %d" % self.attempts)
            return "проход %d выполнен" % self.attempts

    db = Database(TMP / "flaky.db")
    ctx = PipelineContext(settings=default_settings(), db=db)
    service = FlakyService(ctx)

    # Настоящий backoff (2 секунды) тут не нужен: проверяем логику цикла,
    # а не умение ждать. Останавливаем цикл через stop-событие.
    original_backoff = svc_base.BACKOFF_START
    svc_base.BACKOFF_START = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(service.loop(stop), name="test.flaky")
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if service.runs >= 1 and service.errors >= 2:
                break
            await asyncio.sleep(0.01)
        alive = service.runs >= 1 and service.errors >= 2
    finally:
        stop.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        svc_base.BACKOFF_START = original_backoff

    check("служба пережила два падения и отработала", alive, service.stats())
    check("проходы посчитаны", service.runs >= 1, service.runs)
    check("падения посчитаны", service.errors == 2, service.errors)
    check("последняя ошибка описана", "RuntimeError" in service.last_error,
          service.last_error)
    check("каждая попытка либо удалась, либо упала",
          service.attempts == service.runs + service.errors,
          (service.attempts, service.runs, service.errors))
    check("цикл завершился по stop", task.done() and not task.cancelled())

    # После остановки служба больше не крутится.
    errors_after_stop = service.errors
    runs_after_stop = service.runs
    await asyncio.sleep(0.05)
    check("после stop цикл остановился",
          (service.runs, service.errors) == (runs_after_stop, errors_after_stop),
          service.stats())
    check("в stats видны те же счётчики",
          service.stats()["errors"] == 2 and service.stats()["runs"] >= 1,
          service.stats())
    await db.close()


def test_server_filters() -> None:
    """Расширенные фильтры: отбор по профилю, стране, региону, задержке."""
    from script.node_filters import FilterError, build_filter, resolve_country

    # Страна понимается кодом, флагом и названием — на обоих языках.
    ru = resolve_country("RU")
    check("страна по коду RU", ru is not None and ru["code"] == "RU")
    check("страна по флагу", resolve_country("\U0001F1F7\U0001F1FA") is not None)
    check("страна по имени Russia", resolve_country("Russia") is not None)
    check("страна по имени Россия", resolve_country("\u0420\u043e\u0441\u0441\u0438\u044f") is not None)
    check("синоним UK -> GB",
          (resolve_country("UK") or {}).get("code") == "GB", resolve_country("UK"))

    srv_ru = {"line": "a", "capabilities": "global,gemini", "country": "\U0001F1F7\U0001F1FA",
             "stable": 5, "ping_ms": 100, "protocol": "vless"}
    srv_de = {"line": "b", "capabilities": "gemini", "country": "\U0001F1E9\U0001F1EA",
             "stable": 2, "ping_ms": 800, "protocol": "trojan"}
    srv_noc = {"line": "c", "capabilities": "openai", "country": "",
             "stable": 9, "ping_ms": None, "protocol": "vless"}
    rows = [srv_ru, srv_de, srv_noc]

    # «Все серверы, у которых есть global, исключить Россию»: под any
    # сервер без global не проходит, а global-сервер из России отсекается.
    f = build_filter({"capabilities": {"any": ["Global"]}, "exclude_countries": ["RU"]})
    check("Global, кроме России: Россия отсечена",
          f.apply(rows) == [], [r["country"] for r in f.apply(rows)])

    # Тот же global-сервер, но без исключения России — проходит.
    f = build_filter({"capabilities": {"any": ["Global"]}})
    check("Global без исключения берёт Россию",
          f.apply(rows) == [srv_ru], [r["country"] for r in f.apply(rows)])

    f = build_filter({"capabilities": {"all": ["gemini"]}})
    check("capabilities.all требует все перечисленные теги",
          f.apply(rows) == [srv_ru, srv_de], len(f.apply(rows)))

    f = build_filter({"capabilities": {"none": ["openai"]}})
    check("capabilities.none исключает по тегу",
          f.apply(rows) == [srv_ru, srv_de], len(f.apply(rows)))

    f = build_filter({"min_stable": 3})
    check("min_stable отсекает слабых", f.apply(rows) == [srv_ru, srv_noc], len(f.apply(rows)))

    # Неизвестная задержка не проходит под max_ping_ms: иначе фильтр
    # молча пропускал бы серверы, для которых задержка не измерена.
    f = build_filter({"max_ping_ms": 500})
    check("max_ping_ms отсекает и неизвестную задержку",
          f.apply(rows) == [srv_ru], [r["line"] for r in f.apply(rows)])

    f = build_filter({"protocols": ["vless"], "require_country": True})
    check("protocols + require_country", f.apply(rows) == [srv_ru], len(f.apply(rows)))

    f = build_filter({"country_regions": ["EU"]})
    check("регион EU берёт Россию и Германию, без страны — нет",
          f.apply(rows) == [srv_ru, srv_de], len(f.apply(rows)))

    f = build_filter({"exclude_name_regex": "^[ab]$"})
    check("exclude_name_regex по имени строки",
          f.apply(rows) == [srv_noc], [r["line"] for r in f.apply(rows)])

    # Ошибки обязаны быть громкими: иначе фильтр молча ничего не делает
    # и конфиг уезжает «с фильтром», которого не было.
    for bad, why in [
        ({"capabilties": ["x"]}, "опечатка в ключе"),
        ({"exclude_countries": ["Росссия"]}, "опечатка в стране"),
        ({"name_regex": "["}, "битая регулярка"),
        ({"country_regions": ["\u0415\u0432\u0440\u043e\u043f\u0430"]}, "неизвестный регион"),
        ({"capabilities": {"anyy": ["x"]}}, "опечатка в capabilities"),
    ]:
        raised = False
        try:
            build_filter(bad)
        except FilterError:
            raised = True
        check("отказ на " + why, raised)

    check("пустой фильтр ничего не режет",
          build_filter(None).apply(rows) == rows)


def test_validate_config_groups() -> None:
    """Пустая группа, в которую ведёт маршрут, — это сломанный конфиг.

    sing-box такие группы не считает ошибкой: он запускается и молча
    не выпускает трафик. Проверка deploy смотрела только «есть ли хоть
    один реальный outbound» — этого хватало, и сломанный конфиг уезжал
    в репозиторий.
    """
    from script.core import validate_config_groups

    good = {
        "outbounds": [
            {"type": "vless", "tag": "real"},
            {"type": "urltest", "tag": "proxy", "outbounds": ["real"]},
        ],
        "route": {"final": "proxy"},
    }
    check("нормальный конфиг проходит", validate_config_groups(good) == [])

    broken = {
        "outbounds": [
            {"type": "vless", "tag": "real"},
            {"type": "urltest", "tag": "proxy", "outbounds": []},
            {"type": "urltest", "tag": "Telegram", "outbounds": ["real"]},
        ],
        "route": {"final": "proxy", "rules": [{"domain": ["t.me"], "outbound": "Telegram"}]},
    }
    failed = False
    try:
        validate_config_groups(broken, strict=True)
    except ValueError:
        failed = True
    check("пустая группа в route.final запрещена", failed)

    # Без strict пустая группа только сообщается — так веб-выдача
    # показывает предупреждение, а деплой запрещает.
    check("strict=False только сообщает",
          validate_config_groups(broken, strict=False) == ["proxy"])

async def _amain() -> None:
    test_logging()
    test_throttle_summary()
    test_redaction()
    test_server_filters()
    test_validate_config_groups()
    await test_settings_loading()
    await test_best_tags()
    await test_urltest_parsing()
    await test_collector_three_states()
    await test_batch_planning()
    await test_batch_clock()
    await test_failure_classification()
    await test_config_error_split()
    await test_retry_survives_completion()
    await test_speed_checker()
    await test_capabilities_merge()
    await test_country_flag_reaches_export()
    await test_geo_block_and_tag_replace()
    await test_enrichment_keeps_stable()
    await test_singbox_lock()
    await test_database()
    await test_every_module_imports()
    await test_real_checkers_setup()
    await test_discovery_local()
    await test_queued_sink_defers_write()
    await test_results_service_applies_batches()
    await test_retry_returns_to_queue_after_apply()
    await test_mark_result_failed()
    await test_collector_service_fills_queue()
    await test_service_loop_survives_failure()
    await test_pipeline()


def main() -> int:
    try:
        asyncio.run(_amain())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nИТОГО: {sum(PASSED)}/{len(PASSED)} проверок пройдено")
    return 0 if all(PASSED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
