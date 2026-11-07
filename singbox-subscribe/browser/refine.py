"""Проверка сервиса по тегам БД: берём серверы с меткой и уточняем её браузером.

Схема в два прохода
-------------------
1. ГРУБЫЙ ОТБОР (дёшево, много серверов). Reachability проверяет цель gemini
   по TTFB и ставит метку [gemini] всем, кто вообще дотянулся до страницы.
   Метка НЕ означает «Gemini работает»: заглушку по стране HTTP-запрос не
   видит, потому что она рендерится клиентски после входа.

2. УТОЧНЕНИЕ (дорого, мало серверов). Этот модуль берёт из базы ТОЛЬКО тех,
   у кого метка [gemini] уже есть, и открывает страницу настоящим браузером
   из-под сохранённой сессии. Так заглушка видна, и вердикт становится
   честным: ok или ban.

Почему последовательно
----------------------
Параллельные заходы под ОДНИМ аккаунтом Google считает необычной активностью
и показывает стену «unusual traffic» (проверено: 12 параллельных сессий
привели к /sorry/ на весь IP). Поэтому проверка идёт по одному серверу с
паузой. Это медленно, но кэш делает повторные прогоны почти бесплатными.

Куки входа при этом НЕ трогаются: чистится только кэш ВЕРДИКТОВ. Сессия
остаётся в профиле, поэтому «каждый раз как новый» относится к результатам,
а не к логину.
"""

from __future__ import annotations

import time

from browser import paths, services

#: Пауза между серверами, сек. Сглаживает всплеск запросов к Google.
DEFAULT_PAUSE = 3.0


def lines_with_tag(store, tag: str, *, limit: int | None = None):
    """Строки ЖИВЫХ серверов, у которых в capabilities есть тег.

    ВАЖНО: check_pool() НЕ отдаёт колонку capabilities (там только
    key, line, stable, ping_ms, available), поэтому выборка делается запросом
    к самой базе, а не перебором пула. На этом легко ошибиться: перебор пула
    молча возвращал ноль серверов по любому тегу.

    Берём только пригодных к проверке: не исключённых и с stable не ниже
    нижнего порога — по ним и поднимается sing-box.
    """
    from config.settings import get_settings

    wanted = str(tag).strip().lower()
    if not wanted:
        return []
    floor = int(get_settings().core.purge_stable_below)
    out = []
    # capabilities — строка «openai,gemini»; ищем тег по границам запятых,
    # чтобы "gemini" не совпал с "gemini-pro"-подобным тегом.
    with store._connect() as conn:  # noqa: SLF001 — свой же класс хранилища
        rows = conn.execute(
            """
            SELECT line, capabilities FROM servers
            WHERE line != '' AND excluded = 0 AND stable >= ?
              AND capabilities LIKE ?
            ORDER BY stable DESC, key ASC
            """,
            (floor, "%" + wanted + "%"),
        ).fetchall()
    for row in rows:
        tags = [t.strip().lower() for t in (row["capabilities"] or "").split(",") if t.strip()]
        if wanted in tags:
            out.append(row["line"])
            if limit and len(out) >= limit:
                break
    return out


def refine_by_tag(service_name: str, *, store=None, tag: str | None = None,
                  limit: int | None = None, pause: float = DEFAULT_PAUSE,
                  cache=None, workers: int = 1, timeout_ms: int = 25_000,
                  wait_seconds: float = 15.0, log=None) -> dict:
    """Уточняет метку браузером по серверам из базы.

    Возвращает {line: {verdict, reason, elapsed}}.
    workers=1 по умолчанию: см. «Почему последовательно» в докстроке модуля.
    """
    from config.settings import get_settings
    from script.batch_singbox import (build_batch_config, parse_outbound,
                                      ports_ready, start_singbox, stop_singbox)
    from script.core import release_ports, reserve_ports
    from script.server_store import ServerStore
    from browser import batch as bbatch

    spec = services.get_service(service_name)
    tag = tag or spec.name
    store = store or ServerStore(get_settings().paths.servers_db_file)

    def say(msg):
        if log is not None:
            log(msg)

    lines = lines_with_tag(store, tag, limit=limit)
    say("Серверов с меткой [%s]: %d" % (tag, len(lines)))
    if not lines:
        return {}

    # Поднимаем sing-box на эти серверы: локальный порт на каждый.
    used = set()
    entries = []
    for i, ln in enumerate(lines):
        ob, err = parse_outbound(ln, i, used)
        if err:
            continue
        entries.append({"index": i, "port": 0, "outbound": ob})
    if not entries:
        return {}
    ports = reserve_ports(len(entries))
    for e, p in zip(entries, ports):
        e["port"] = p

    S = get_settings()
    cfg = build_batch_config(entries, S.paths.countrytest_template)
    cp = S.paths.sing_box_output_dir / ("refine_%s.json" % spec.name)
    proc = start_singbox(cfg, cp)
    ok = proc is not None and proc.poll() is None and ports_ready(
        [e["port"] for e in entries], 20.0)
    say("sing-box поднят: %s (%d серверов)" % (ok, len(entries)))
    if not ok:
        stop_singbox(proc)
        release_ports(ports)
        return {}

    results = {}
    try:
        targets = []
        for e, p in zip(entries, ports):
            targets.append((lines[e["index"]], "127.0.0.1:%d" % p))
        # workers=1: последовательно, чтобы не злить Google.
        if workers > 1:
            results = bbatch.check_batch(
                spec.name, targets, workers=workers, timeout_ms=timeout_ms,
                wait_seconds=wait_seconds, cache=cache, log=say)
        else:
            for i, (line, proxy) in enumerate(targets):
                one = bbatch.check_batch(
                    spec.name, [(line, proxy)], workers=1,
                    timeout_ms=timeout_ms, wait_seconds=wait_seconds,
                    cache=cache, log=None)
                results.update(one)
                res = one.get(line) or {}
                say("  [%d/%d] %-8s %-5ss %s" % (
                    i + 1, len(targets), res.get("verdict", "?"),
                    res.get("elapsed", "?"), (res.get("reason") or "")[:44]))
                if i + 1 < len(targets):
                    time.sleep(max(0.0, pause))
    finally:
        stop_singbox(proc)
        release_ports(ports)

    return results

class _nullctx:
    """Пустой контекст-менеджер: когда кэша нет, блокировка не нужна."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _key_of(line: str) -> str:
    """Канонический ключ прокси (как в кэше и базе)."""
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(line) or str(line).strip()


def refine_lines(service_name: str, lines, *, max_checks: int = 25,
                 pause: float = DEFAULT_PAUSE, cache=None, logger=None,
                 budget_seconds: float | None = None) -> dict:
    """Проверяет ПЕРЕДАННЫЕ строки (а не выборку по тегу из базы).

    Нужен конвейеру: этап уже отобрал серверы по метке, и повторять запрос к
    базе незачем. Порядок строк сохраняется, поэтому лимит прогона
    (max_checks) отсекает один и тот же хвост — и следующий цикл продолжает
    с него, а не начинает заново.
    """
    from config.settings import get_settings
    from script.batch_singbox import (build_batch_config, parse_outbound,
                                      ports_ready, start_singbox, stop_singbox)
    from script.core import release_ports, reserve_ports
    from browser import batch as bbatch

    spec = services.get_service(service_name)
    lines = [str(l).strip() for l in lines if str(l).strip()]
    if not lines:
        return {}

    def say(msg):
        if logger is not None:
            logger.info(msg)

    # Сначала спрашиваем кэш: платить за уже известный вердикт нечем.
    results = {}
    pending = []
    for line in lines:
        cached = cache.get(line) if cache is not None else None
        if cached is not None:
            results[line] = dict(cached, from_cache=True)
        else:
            pending.append(line)

    say('Browser: из кэша %d, к проверке %d' % (len(results), len(pending)))
    if not pending:
        return results

    # Бюджет ВРЕМЕНИ важнее бюджета количества. Стоимость проверки разная:
    # заглушка отсекается за 3 с, рабочий сервер занимает 20-40 с. Поэтому
    # фиксированное число серверов либо не успевает (таймаут этапа), либо
    # простаивает. Считаем по времени: как только остаток меньше, чем
    # разумная цена одной проверки, останавливаемся — недоделанные останутся
    # на следующий цикл и НЕ потеряются.
    started_at = time.monotonic()
    per_server_guess = 12.0        # средняя цена одной проверки, сек

    # ПОРЯДОК ВАЖЕН: кандидатов сотни, за прогон проверяем десятки. Сначала
    # те, у кого вердикта НЕТ вовсе — это неисследованная территория, и именно
    # там могут найтись рабочие серверы. Повторные проверки (вердикт устарел)
    # идут после: их ответ мы уже примерно знаем.
    # Через ПУБЛИЧНЫЙ метод, а не через внутренности: кэш бывает файловый и
    # в базе, и лезть в _data/_lock напрямую — верный способ сломать один из
    # них (именно так и падало: «DbVerdictCache object has no attribute _lock»).
    known = cache.known_keys() if cache is not None else set()
    fresh_targets = [l for l in pending if _key_of(l) not in known]
    seen_before = [l for l in pending if _key_of(l) in known]
    pending = fresh_targets + seen_before
    if fresh_targets:
        say('Browser: новых %d, повторных %d' % (len(fresh_targets), len(seen_before)))

    todo = pending[:max(0, int(max_checks))] if max_checks else pending
    skipped = len(pending) - len(todo)
    if skipped:
        say('Browser: лимит прогона %d, отложено до следующего цикла: %d'
            % (max_checks, skipped))

    if budget_seconds is not None:
        affordable = int(max(0.0, budget_seconds) // per_server_guess)
        if affordable < len(todo):
            say('Browser: бюджет %.0f с позволяет ~%d проверок из %d'
                % (budget_seconds, affordable, len(todo)))
            todo = todo[:affordable]
        if not todo:
            say('Browser: бюджета времени не хватает ни на одну проверку — пропуск')
            return results

    used = set()
    entries = []
    for i, ln in enumerate(todo):
        ob, err = parse_outbound(ln, i, used)
        if err:
            continue
        entries.append({'index': i, 'port': 0, 'outbound': ob})
    if not entries:
        return results

    ports = reserve_ports(len(entries))
    for e, p in zip(entries, ports):
        e['port'] = p

    S = get_settings()
    cfg = build_batch_config(entries, S.paths.countrytest_template)
    cp = S.paths.sing_box_output_dir / ('browser_%s.json' % spec.name)
    proc = start_singbox(cfg, cp)
    ok = proc is not None and proc.poll() is None and ports_ready(
        [e['port'] for e in entries], 20.0)
    say('Browser: sing-box поднят для %d серверов: %s' % (len(entries), ok))
    if not ok:
        stop_singbox(proc)
        release_ports(ports)
        return results

    try:
        for e, port in zip(entries, ports):
            line = todo[e['index']]
            if budget_seconds is not None:
                spent = time.monotonic() - started_at
                if spent >= budget_seconds:
                    say('Browser: бюджет исчерпан (%.0f с), остаток уйдёт '
                        'в следующий цикл' % spent)
                    break
            one = bbatch.check_batch(
                spec.name, [(line, '127.0.0.1:%d' % port)], workers=1,
                timeout_ms=28000, wait_seconds=16.0, cache=cache, log=None)
            res = one.get(line) or {}
            results[line] = res
            say('Browser [%s] %s (%ss)' % (res.get('verdict'), line[:52],
                                           res.get('elapsed')))
            if pause:
                time.sleep(max(0.0, pause))
    finally:
        stop_singbox(proc)
        release_ports(ports)

    return results