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
TMP = HERE.parent.parent / "_pipeline_test_tmp"


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
from pipeline.logging_setup import cleanup_old_logs, setup_logging  # noqa: E402
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
    check("merge.txt записан", (TMP / "merge.txt").exists(), export.get("merge"))
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
    checkers = build_checkers("url_probe,country,reachability")
    names = [c.name for c in checkers]
    check("реестр отдал все три чекера", names == ["url_probe", "country", "reachability"], names)

    failures = []
    for checker in checkers:
        try:
            await checker.setup(ctx)
        except Exception as exc:  # noqa: BLE001
            failures.append(checker.name + ": " + type(exc).__name__ + ": " + str(exc))
    check("setup всех штатных чекеров проходит", not failures, failures)
    await db.close()


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
    check("полный батч получает старт + полную долю", abs(big.timeout - 115) < 0.01, big.timeout)
    check("малый батч получает меньший таймаут", small.timeout < big.timeout, small.timeout)
    check("малый батч всё равно получает время на старт", small.timeout >= 15, small.timeout)
    check("таймаут не обнуляется на крошечных батчах", tiny.timeout >= 15, tiny.timeout)
    check("дедлайн считается от запуска", big.remaining <= big.timeout)


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


async def _amain() -> None:
    test_logging()
    test_redaction()
    await test_collector_three_states()
    await test_batch_planning()
    await test_batch_clock()
    await test_failure_classification()
    await test_config_error_split()
    await test_retry_survives_completion()
    await test_enrichment_keeps_stable()
    await test_singbox_lock()
    await test_database()
    await test_real_checkers_setup()
    await test_discovery_local()
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
