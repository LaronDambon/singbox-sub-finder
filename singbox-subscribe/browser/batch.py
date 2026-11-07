"""Батчевая проверка сервиса по всему живому пулу — через прокси серверов.

Зачем отдельный модуль
----------------------
Одиночная проверка тратит время на СЕТЬ, а не на браузер. Замерено:
запуск Chromium 0.08 с, создание контекста 0.06 с, а загрузка страницы
3-5 с и до 16.7 с на медленном сервере. Поэтому ускорение даёт только
ПАРАЛЛЕЛИЗМ и кэш, а не экономия на запуске.

Как устроено
------------
Прокси в Playwright задаётся на КОНТЕКСТ, а не на страницу, поэтому каждый
сервер получает свой контекст. Контекст стоит 0.06 с — это дёшево.

Вход берётся ОДИН раз из persistent-профиля сервиса (storage_state, 63
cookies за 0.13 с) и передаётся всем контекстам. Так параллельные воркеры
работают из-под одной сессии, а профиль при этом открывается ровно один раз:
launch_persistent_context нельзя запускать дважды на одной папке.

Экономии
--------
* блокировка картинок, шрифтов и стилей — на распознавание «приложение или
  заглушка» они не влияют (замерено 5.25 с -> 4.55 с на страницу);
* кэш вердикта по серверу: у разных конфигов одного VPN общий выходной IP,
  и второй раз платить за ту же страницу незачем.

Секреты
-------
Значения cookies НЕ читаются в результаты и не логируются: для вердикта нужно
только содержимое страницы. storage_state живёт в памяти процесса.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed

from browser import paths, services

#: Типы ресурсов, не влияющих на распознавание приложения и заглушки.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

#: Воркеров по умолчанию. Каждый — свой контекст; больше 6 на слабой машине
#: начинают мешать друг другу за CPU и канал.
DEFAULT_WORKERS = 4

#: Сколько раз повторять сервер, у которого вердикт не определился.
DEFAULT_ATTEMPTS = 2

VERDICT_OK = "ok"
VERDICT_BLOCKED = "blocked"
VERDICT_UNKNOWN = "unknown"
#: Сервер отвечает по-разному от захода к заходу. Ни рабочий, ни забаненный:
#: метку по такому не ставим и не снимаем — он просто не годится.
VERDICT_UNSTABLE = "unstable"


def _settle_verdict(attempts) -> tuple:
    """Итог по нескольким заходам: нужна СОГЛАСОВАННОСТЬ.

    attempts: [(verdict, reason), ...] в порядке выполнения.

    Правила:
      * есть заглушка -> blocked (её подтверждать не нужно);
      * два ok подряд     -> ok;
      * расхождение ok/блок/unknown -> unstable;
      * один заход        -> как есть (сеть не дала сделать второй).
    """
    if not attempts:
        return VERDICT_UNKNOWN, "нет попыток"
    verdicts = [v for v, _ in attempts]
    if VERDICT_BLOCKED in verdicts:
        return VERDICT_BLOCKED, next(r for v, r in attempts if v == VERDICT_BLOCKED)

    known = [v for v in verdicts if v != VERDICT_UNKNOWN]
    if len(set(known)) > 1:
        return (VERDICT_UNSTABLE,
                "нестабилен: " + ", затем ".join(verdicts))
    if known and known[0] == VERDICT_OK:
        if len(known) >= 2:
            return VERDICT_OK, next(r for v, r in attempts if v == VERDICT_OK)
        return (VERDICT_UNSTABLE,
                "ok лишь однажды из %d заходов" % len(attempts))
    return attempts[-1][0], attempts[-1][1]


def verdict_from_text(spec, text: str):
    """Вердикт по тексту страницы. Возвращает (verdict, reason).

    Порядок проверок важен:
      1. заглушка (страна, стена трафика) — сразу ban;
      2. признаки АНОНИМНОГО состояния — значит вход не применился, и сервис
         через этот выход не работает, даже если страница отрисовалась;
      3. признаки ВХОДА + элементы приложения — только это ok.
    """
    low = (text or "").lower()

    for marker in spec.blocked_markers:
        if marker.lower() in low:
            return VERDICT_BLOCKED, "заглушка: %s" % marker

    # Вход не применился: Google отдал анонимную страницу. Живой сервер тут
    # ни при чём — под аккаунтом пользователя сервис не откроется.
    anon = [m for m in getattr(spec, "anon_markers", ()) if m.lower() in low]
    if anon:
        return VERDICT_BLOCKED, "не выполнен вход: %s" % anon[0]

    hits = [m for m in spec.ok_markers if m.lower() in low]
    login = [m for m in getattr(spec, "login_markers", ()) if m.lower() in low]

    # Признаков входа достаточно: анонимные маркеры уже исключены выше.
    # Элементы приложения («Спросить Gemini») добавляются только как
    # подтверждение, но их может не быть — на странице с историей чатов поле
    # ввода подписано иначе, и это нормальное рабочее состояние.
    if login:
        return VERDICT_OK, "вход выполнен: %s" % ", ".join((login + hits)[:3])
    if len(hits) >= spec.ok_threshold:
        # Элементы приложения есть, а признаков входа нет: похоже на анонимную
        # страницу нового образца. Не подтверждаем.
        return VERDICT_UNKNOWN, "нет признаков входа"
    return VERDICT_UNKNOWN, "ни заглушки, ни приложения"


def load_login_state(service: str):
    """Выгружает storage_state из persistent-профиля сервиса.

    Профиль открывается ровно один раз и сразу закрывается — дальше сессия
    живёт в памяти и раздаётся контекстам. Значения cookies наружу не уходят.
    """
    from playwright.sync_api import sync_playwright

    profile = paths.profile_dir(service)
    if not profile.exists():
        return None
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), headless=True)
        try:
            return ctx.storage_state()
        finally:
            ctx.close()


def check_batch(service_name: str, targets, *, workers: int = DEFAULT_WORKERS,
                attempts: int = DEFAULT_ATTEMPTS, timeout_ms: int = 25_000,
                wait_seconds: float | None = None,
                cache=None, log=None, state=None) -> dict:
    """Проверяет список целей. targets: [(line, "127.0.0.1:port"), ...].

    Возвращает {line: {verdict, reason, elapsed, attempts}}.

    Работа раздаётся через ОЧЕРЕДЬ, а не кусками. Замер на 12 серверах:
    при статичных кусках батч занял 202 с, потому что один медленный сервер
    (95 с) держал весь свой воркер, а остальные простаивали. С очередью
    свободный воркер сразу берёт следующую цель, и время батча определяется
    самым медленным ОДИНОЧНЫМ сервером, а не суммой по куску.
    """
    spec = services.get_service(service_name)
    paths.ensure_dirs(service_name)
    if wait_seconds is not None:
        spec = replace(spec, wait_seconds=float(wait_seconds))
    results = {}
    lock = threading.Lock()

    def say(msg):
        if log is not None:
            log(msg)

    todo = []
    for line, proxy in targets:
        cached = cache.get(line) if cache is not None else None
        if cached is not None:
            results[line] = dict(cached, from_cache=True)
        else:
            todo.append((line, proxy))
    if results:
        say("Из кэша: %d, к проверке: %d" % (len(results), len(todo)))
    if not todo:
        return results

    if state is None:
        state = load_login_state(service_name)
    say("Сессия загружена: cookies=%d" % len((state or {}).get("cookies") or []))

    # Общая очередь целей: воркер берёт следующую, как только освободился.
    queue = list(todo)
    queue_lock = threading.Lock()
    done = [0]

    def next_item():
        with queue_lock:
            if not queue:
                return None
            return queue.pop(0)

    def run_worker():
        """Свой Playwright и свой браузер на поток.

        Объекты sync-API Playwright привязаны к создавшему их потоку, и
        передавать их между потоками нельзя («Cannot switch to a different
        thread»). Запуск Chromium стоит 0.08 с — на фоне загрузки страницы
        это ничто, а ограничение снимается целиком.
        """
        from playwright.sync_api import sync_playwright as _sync

        with _sync() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            try:
                while True:
                    item = next_item()
                    if item is None:
                        return
                    line, res = check_one(browser, item)
                    with lock:
                        results[line] = res
                        if cache is not None and res["verdict"] != VERDICT_UNKNOWN:
                            cache.put(line, res)
                        done[0] += 1
                        say("  [%d/%d] %-8s %-5ss %s" % (
                            done[0], len(todo), res["verdict"], res["elapsed"],
                            res["reason"][:44]))
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass

    def check_one(browser, item):
        line, proxy = item
        started = time.time()
        verdict, reason = VERDICT_UNKNOWN, ""
        used = 0
        ctx = None
        try:
            # Свой контекст на сервер: прокси задаётся именно на контексте.
            ctx = browser.new_context(
                storage_state=state,
                proxy={"server": "http://" + str(proxy).replace("http://", "")},
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            ctx.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in BLOCKED_RESOURCE_TYPES
                else route.continue_(),
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.new_page()
            # Проверка СТАБИЛЬНОСТИ, а не одного удачного захода.
            # Живой сервер отвечает одинаково дважды; моргающий — по-разному.
            # Замерено на умирающем сервере: за час он дал ok, ok, ok, затем
            # заглушку и полную смерть — один заход принял бы его за рабочий.
            rounds = max(2, int(attempts))
            attempts_made = []
            for i in range(rounds):
                used += 1
                v, why = _probe_once(page, spec, timeout_ms)
                attempts_made.append((v, why))
                # Заглушка есть заглушка: подтверждать нечего.
                if v == VERDICT_BLOCKED:
                    break
                # Сетевой отказ: второй заход стоит столько же и почти никогда
                # не меняет ответ.
                if v == VERDICT_UNKNOWN and "не открылось" in why:
                    break
                # Не определились — пробуем ещё раз (в пределах лимита).
                if v == VERDICT_UNKNOWN and i + 1 < rounds:
                    continue
                # Первый ok обязательно подтверждаем вторым заходом.
                if v == VERDICT_OK and i + 1 < rounds:
                    continue
                break

            verdict, reason = _settle_verdict(attempts_made)
        except Exception as exc:  # noqa: BLE001 — воркер не должен падать
            reason = "%s: %s" % (type(exc).__name__, str(exc)[:80])
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        return line, {
            "verdict": verdict,
            "reason": reason,
            "elapsed": round(time.time() - started, 1),
            "attempts": used,
        }

    pool_size = max(1, min(workers, len(todo)))
    say("Целей: %d, воркеров: %d" % (len(todo), pool_size))
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = [pool.submit(run_worker) for _ in range(pool_size)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                say("Воркер упал: %s" % exc)

    return results


#: Запросы, отказ в которых означает «сервис мёртв», а не «нет аналитики».
#: Замерено на двух серверах: рабочий отдавал единственный сбой на
#: google.com/ccm/collect (телеметрия), нерабочий — четыре, среди которых
#: gemini.google.com/_/BardChatUi/data/batchexecute, то есть САМ API Gemini.
#: Поэтому решает не число сбоев, а их КАЧЕСТВО: сбой самого API — приговор.
BACKEND_MARKERS = (
    "batchexecute",          # основной RPC Gemini
    "BardChatUi/data",       # его путь
    "onegoogle",             # аккаунтные данные (аватар, состояние входа)
    "asyncdata",
)

#: Телеметрия и трекеры: их обрыв НИЧЕГО не говорит о работоспособности
#: сервиса, и именно из-за них прежняя проверка по числу сбоев ошибалась.
IGNORED_MARKERS = (
    "ccm/collect", "gtm.js", "googletagmanager", "play.google.com/log",
    "gen_204", "/log?",
)


#: Типы обрывов, которые ничего не значат: страница сама отменяет запросы
#: (переход, повтор, отмена префетча). Именно они путали прежнюю проверку —
#: на РАБОЧЕМ сервере их три, и все безобидные.
BENIGN_FAILURES = ("ERR_ABORTED",)


def _is_real_failure(failure) -> bool:
    """Настоящий ли сетевой обрыв (а не отмена самим браузером)."""
    text = str(failure or "")
    if not text:
        return False
    return not any(b in text for b in BENIGN_FAILURES)


def _is_backend_failure(url: str) -> bool:
    """Относится ли адрес к бэкенду сервиса (а не к телеметрии)."""
    text = str(url)
    if any(m in text for m in IGNORED_MARKERS):
        return False
    return any(m in text for m in BACKEND_MARKERS)


def attach_failure_counter(page):
    """Считает обрывы ВАЖНЫХ запросов (бэкенд сервиса, а не телеметрия).

    Возвращает список, куда обработчик складывает адреса. Оболочка Gemini
    рисуется из кэша, поэтому «Новый чат» появляется и на сервере, который
    физически не может дотянуться до бэкенда: клиент у пользователя увидит
    красивую, но мёртвую страницу. Замерено на двух серверах:
    рабочий — 0 обрывов и 1060 символов страницы,
    нерабочий — 2 обрыва и 812 символов (недогруженный аватар).
    """
    failures = []

    def on_failed(request):
        failure = getattr(request, "failure", None)
        # Считаем ТОЛЬКО настоящие обрывы, и только к важным адресам.
        # Отмены (ERR_ABORTED) на рабочем сервере тоже есть — они нормальны.
        if failure and _is_real_failure(failure) and _is_backend_failure(request.url):
            failures.append("%s (%s)" % (request.url, failure))

    page.on("requestfailed", on_failed)
    return failures


def _probe_once(page, spec, timeout_ms: int):
    """Одна попытка: открыть сервис и прочитать вердикт по тексту страницы.

    Заглушка ПОБЕЖДАЕТ: если признак блокировки появился в любой момент, он
    перекрывает ранее увиденный ok. Так снимается ложный ok, который возникал
    из-за того, что навигация страницы рисуется раньше самой заглушки.
    """
    try:
        page.goto(spec.url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        return VERDICT_UNKNOWN, "не открылось: %s" % type(exc).__name__

    failures = attach_failure_counter(page)

    deadline = time.time() + spec.wait_seconds
    best = (VERDICT_UNKNOWN, "маркеры не появились за %.0f с" % spec.wait_seconds)
    # Небольшая пауза после ok: заглушка может дорисоваться позже навигации.
    settle_after_ok = 5.0
    ok_seen_at = None
    while time.time() < deadline:
        try:
            text = page.inner_text("body") or ""
        except Exception:  # noqa: BLE001
            text = ""
        verdict, reason = verdict_from_text(spec, text)
        if verdict == VERDICT_BLOCKED:
            return VERDICT_BLOCKED, reason
        if verdict == VERDICT_OK:
            if ok_seen_at is None:
                ok_seen_at = time.time()
                best = (VERDICT_OK, reason)
            elif time.time() - ok_seen_at >= settle_after_ok:
                # Маркеры есть, но оболочка рисуется и без работающего бэкенда:
                # если за время ожидания накопились обрывы к хостам Google,
                # сервис у пользователя не заработает — это тоже бан.
                if failures:
                    return (VERDICT_BLOCKED,
                            "бэкенд недоступен: %s" % failures[0][-70:])
                return VERDICT_OK, reason
        page.wait_for_timeout(350)

    # Время вышло: если маркеры были, но бэкенд обрывался — сервер негодный.
    if best[0] == VERDICT_OK and failures:
        return (VERDICT_BLOCKED, "бэкенд недоступен: %s" % failures[0][-70:])
    return best