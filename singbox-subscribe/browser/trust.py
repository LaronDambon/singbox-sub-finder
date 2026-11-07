"""Доверие меткам сервисов: метка ставится ТОЛЬКО браузерной проверкой.

Зачем. Метку [gemini] умели ставить двое: service_access (HTTP-запрос) и
browser_service (настоящий браузер из-под сессии). HTTP-проверка не видит
заглушку — она рендерится клиентски после входа, — поэтому её метки попадали
в конфиг и давали серверы, которые у клиента не работают. Замер на боевой
базе: из 56 серверов с меткой [gemini] браузером были подтверждены РОВНО 2.

Теперь метку ставит только browser_service. Этот модуль приводит базу в
соответствие: снимает метку у всех, чей вердикт в кэше не «ok».

Имя сервиса и имя метки — РАЗНЫЕ вещи. Сервис остаётся gemini (от него зависят
URL, профиль сессии, правила разбора страницы и таблица вердиктов), а метка
читается человеком и живёт в конфиге (singbox.checkers.browser_service.tag).
Поэтому обе величины — отдельные параметры, и по умолчанию метка берётся из
конфига, а не из имени сервиса.
"""

from __future__ import annotations

from browser import cache as bcache
from browser import paths


def _trust_pass() -> str:
    """Метка прохода для ручной правки из CLI.

    Профиль пишется сбросом, а сброс в базе опознаётся по метке прохода.
    Здесь проход ровно один (эта команда), поэтому метка — уникальное
    значение времени: следующая проверка начнёт свой проход и снова
    перепишет профиль.
    """
    from datetime import datetime, timezone

    return "trust-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def strip_unverified(service: str = "gemini", *, store=None, dry_run: bool = False,
                     log=None, tag: str | None = None) -> dict:
    """Снимает метку сервиса у серверов без подтверждённого вердикта.

    service — имя сервиса (gemini): по нему ищется кэш вердиктов.
    tag     — как называется метка в базе. По умолчанию берётся из конфига
              (singbox.checkers.browser_service.tag), потому что метка и имя
              сервиса совпадают далеко не всегда.

    Возвращает {total, verified, stripped, kept}. Значения cookies не читаются.
    """
    from config.settings import get_settings
    from script.server_store import ServerStore
    from browser.db_cache import DbVerdictCache

    def say(msg):
        if log is not None:
            log(msg)

    settings = get_settings()
    if tag is None:
        tag = str(settings.setting("singbox.checkers.browser_service.tag", "") or service)
    # Метки ПРЕЖНИХ прогонов тоже снимаются: база набита ими с тех пор, когда
    # метка совпадала с именем сервиса, и без этого старый [gemini] остался бы
    # висеть рядом с новым [gemini-chat].
    #
    # "gemini-api" сюда НЕ входит: это метка другого транспорта (HTTP-проверка
    # по API), и браузер не вправе её снимать — он подтверждает или не
    # подтверждает ЧАТ, а не доступ к API.
    legacy_tags = [t for t in ("gemini",) if t != tag]

    store = store or ServerStore(settings.paths.servers_db_file)
    # Тот же кэш, что и у чекера: вердикты лежат в базе, поэтому и сверяться
    # надо с ней, иначе «подтверждённым» окажется сервер, которого браузер
    # не проверял.
    cache = DbVerdictCache(service, store=store)
    confirmed = cache.known_ok()

    tagged = 0
    verified = []
    unverified = []
    wanted = {tag.strip().lower(), *(t.lower() for t in legacy_tags)}
    # LIKE идёт по КАЖДОМУ имени категории, а не только по текущему тегу:
    # строки со старой меткой (gemini) иначе не попадали бы в выборку вовсе,
    # и снять их было бы нечем — они висели бы в базе вечно.
    likes = sorted(wanted)
    where = " OR ".join("capabilities LIKE ?" for _ in likes)
    with store._connect() as conn:  # noqa: SLF001 — свой же класс хранилища
        rows = conn.execute(
            "SELECT key, capabilities FROM servers WHERE " + where,
            tuple("%" + t + "%" for t in likes),
        ).fetchall()

    for row in rows:
        caps = (row["capabilities"] or "").split(",")
        tags = [t.strip().lower() for t in caps if t.strip()]
        # Сравнение ПО ГРАНИЦАМ тегов, а не по вхождению подстроки: иначе
        # "gemini" совпал бы с "gemini-chat", а LIKE — с "gemini-pro".
        if not (wanted & set(tags)):
            continue
        tagged += 1
        # Сверяемся по ключу через публичный метод: без заглядывания внутрь,
        # чтобы кэш можно было менять, не ломая этот инструмент.
        if row["key"] in confirmed:
            verified.append(row["key"])
        else:
            unverified.append(row["key"])

    say("С меткой [%s]: %d" % (tag, tagged))
    say("  подтверждено браузером : %d" % len(verified))
    say("  без подтверждения      : %d" % len(unverified))

    if dry_run or not unverified:
        return {"total": tagged, "verified": len(verified),
                "stripped": 0, "kept": len(verified)}

    # Снимаем метку сбросом профиля в пустоту: сервис у этих серверов не
    # подтверждён, а списков «какие теги вытеснять» больше нет — вытеснение
    # целиком делает сброс. Теги других категорий (speed-, Global,
    # openrouter) вернёт ближайший проход проверки: он пишет профиль заново.
    rows_out = [
        {
            "key": key,
            "country": "",
            "capabilities": "",
            "pass": _trust_pass(),
        }
        for key in unverified
    ]
    changed = store.record_enrichment(rows_out)
    say("  метка снята у %d серверов" % changed)
    return {"total": tagged, "verified": len(verified),
            "stripped": changed, "kept": len(verified)}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python browser.py trust",
        description="Оставить метку сервиса только у подтверждённых браузером",
    )
    parser.add_argument("service", nargs="?", default="gemini",
                        help="имя сервиса (gemini, claude, chatgpt)")
    parser.add_argument("--tag", default=None,
                        help="имя метки в базе; по умолчанию из config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="только показать")
    args = parser.parse_args(argv)

    stats = strip_unverified(args.service, dry_run=args.dry_run, log=print,
                             tag=args.tag)
    print()
    print("итог:", stats)
    if args.dry_run:
        print("(dry-run: изменения не записаны)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
