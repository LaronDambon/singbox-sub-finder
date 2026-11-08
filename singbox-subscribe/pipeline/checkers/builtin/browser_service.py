"""Чекер сервиса через браузер: уточняет метку [gemini] из-под сессии.

Место в конвейере
-----------------
Идёт ПОСЛЕ reachability. Reachability ставит грубую метку [gemini] всем, кто
вообще дотянулся до страницы по TTFB, — но заглушку по стране и стену
«подозрительный трафик» HTTP-запрос не видит: они рисуются клиентски после
входа. Этот чекер берёт серверы С УЖЕ ПОСТАВЛЕННОЙ меткой и говорит честно:
работает (ok) или заглушка (ban).

Почему не решает живость
------------------------
decides_availability = False: браузер не должен влиять на stable и available.
Если проверка не удалась, сервер не становится мёртвым — он просто остаётся
с прежней меткой.

Таймаут и кэш
-------------
Проверка дорогая (3-20 с на сервер, заходы последовательные, чтобы не злить
Google). Этап даёт ограниченный бюджет времени, поэтому чекер:

  * берёт вердикты из кэша (30 дней для грязных IP, 6 часов для остальных);
  * проверяет не больше max_checks серверов за прогон;
  * сохраняет вердикты, чтобы следующий прогон продолжил с места остановки.

Так полный пул разбирается за несколько циклов, а не за один длинный проход.
"""

from __future__ import annotations

from config.settings import get_settings
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.browser_service")


class BrowserServiceChecker(Checker):
    """Проверка сервиса настоящим браузером из-под сохранённой сессии."""

    name = "browser_service"
    description = "Браузер: работает ли сервис на самом деле -> ban/ok (уточняет [gemini])"
    #: Живость решает url_probe: здесь только уточнение метки.
    decides_availability = False
    #: Мёртвые не проверяются: браузер нужен лишь тем, кто дошёл до страницы.
    run_on_dead = False
    #: Категория тегов, которую чекер ЗАМЕНЯЕТ: метки сервисов.
    #: Сервер, который сейчас НЕ проходит проверку, не должен носить метку.
    #:
    #: Своя метка — "gemini-chat" (ставится при ok). Голое "gemini" —
    #: метка ПРЕЖНИХ прогонов, оставшаяся в базе с тех пор, когда HTTP-проверка
    #: ставила её сама. Она тоже входит в категорию: иначе рядом с новой меткой
    #: навсегда осталась бы старая.
    #:
    #: "gemini-api" — ЧУЖАЯ метка (её ставит HTTP-проверка по API), поэтому её
    #: здесь НЕТ: подтвердить её браузером — да, стереть при ok — нет.
    own_tags: tuple = ("gemini", "gemini-chat", "claude", "chatgpt")

    def __init__(self) -> None:
        self.service = "gemini"
        #: Метка, которой помечается РАБОТАЮЩИЙ сервис. Имя сервиса и имя
        #: тега — разные вещи: сервис остаётся gemini (от него зависят URL,
        #: профиль сессии и правила разбора), а метки читаются человеком
        #: и показывают, КАКИМ транспортом доступ подтверждён:
        #:   "gemini-api"  — отвечает API (дёшево, HTTP-запрос с ключом);
        #:   "gemini-chat" — открывается сам чат в браузере (дорого, честно).
        self.tag = "gemini-chat"
        #: Метка, которую ставит ДРУГОЙ транспорт (HTTP-проверка). Сама по себе
        #: она слабее: заглушку по стране HTTP-запрос не видит, потому что та
        #: рисуется клиентски после входа. Поэтому при ok она остаётся (кто-то
        #: же её поставил), а при доказанном бане — снимается вместе со своей.
        self.peer_tag = "gemini-api"
        #: Метки, любой из которых достаточно, чтобы попробовать браузер.
        #: Все они означают одно: сервер ДОТЯНУЛСЯ до ресурса Google.
        #:   gemini-chat — отвечает сам чат (браузер подтвердил в прошлый раз);
        #:   gemini-api  — отвечает API Gemini (HTTP-проверка уже прошла);
        #:   Global  — группа generate_204 (gstatic + yandex + cloudflare);
        #:   gstatic — www.gstatic.com/generate_204;
        #:   youtube — www.youtube.com/generate_204.
        #: Брали только метку цели — и кандидатов выходило 2-5 на весь пул,
        #: хотя рабочие серверы у пользователя были. Доступ к gemini с сервера,
        #: помеченного [Global], подтверждён вручную: метка про конкретную цель
        #: ничего не говорит о том, пустит ли Google в сам сервис.
        #: Голое "gemini" оставлено НАМЕРЕННО: база набита метками прошлых
        #: прогонов, и без него серверы, помеченные до переименования, выпали
        #: бы из отбора, пока метка не переставится.
        self.accept_tags = ("gemini-chat", "gemini-api", "gemini",
                            "global", "gstatic", "youtube")
        self.enabled = True
        #: ЛИМИТА НЕТ. Раньше за прогон проверялось 25 серверов, остальные
        #: «когда-нибудь потом» — при 393 кандидатах до последнего очередь не
        #: доходила никогда, и метки получали одни и те же первые 25.
        #: Теперь проверяются ВСЕ, у кого нет свежего вердикта; следующий
        #: чекер этапа не начинается, пока этот не закончит.
        #: Повторные прогоны дешёвые: вердикты лежат в базе и живут по TTL
        #: (ok — час, blocked — шесть часов, стена трафика — 30 дней).
        self.max_checks = 0          # 0 = без ограничения
        self.pause = 1.0
        self._cache = None
        #: Ключи с ДЕЙСТВУЮЩИМ баном сервиса. Отбор идёт по метке
        #: reachability, а это — дополнительная экономия: забаненный сервер
        #: (заглушка по стране, стена трафика) повторно не проверяем, пока
        #: бан не истечёт по TTL.
        self._banned_keys: set = set()
        #: Бюджет времени этапа; уточняется в setup().
        self._enrich_timeout = 180.0

    async def setup(self, ctx) -> None:
        """Готовит кэш вердиктов. Без профиля сессии чекер выключается."""
        from browser import paths
        from browser.db_cache import DbVerdictCache

        # Имя сервиса и метка читаются ИЗ СВОЕГО БЛОКА конфига
        # (singbox.checkers.browser_service), а не из старых плоских имён:
        # раньше эти строки искали ключи browser_service/browser_tag, которых
        # в схеме не было, и всегда возвращали None — то есть настройки
        # сервиса и метки из config.yaml не действовали вообще.
        self.service = str(ctx.setting("singbox.checkers.browser_service.service", self.service) or self.service)
        self.tag = str(ctx.setting("singbox.checkers.browser_service.tag", self.tag) or self.tag)
        # 0 = без ограничения (см. __init__).
        self.max_checks = int(ctx.setting("singbox.checkers.browser_service.max_checks", self.max_checks))
        self.pause = float(ctx.setting("singbox.checkers.browser_service.pause", self.pause))
        self._enrich_timeout = float(ctx.setting("enrich_timeout", 180))

        if not bool(ctx.setting("singbox.checkers.browser_service.enabled", True)):
            LOGGER.info("Browser service: выключен в config.yaml")
            self.enabled = False
            return

        profile = paths.profile_dir(self.service)
        if not profile.exists():
            # Профиля нет — логина не было. Молча выключаемся: браузер без
            # сессии увидит ту же страницу, что и обычный HTTP-запрос.
            LOGGER.warning(
                "Browser service: профиль %s не найден, чекер выключен "
                "(нужен python browser.py login %s из папки singbox-subscribe)",
                profile, self.service,
            )
            self.enabled = False
            return

        # Одно хранилище на всё: и баны, и кэш вердиктов. Создаём его ДО
        # обращения к кэшу — иначе порядок строк решает, работает чекер или
        # падает с UnboundLocalError.
        from config.settings import get_settings
        from script.server_store import ServerStore

        store = ServerStore(get_settings().paths.servers_db_file)
        try:
            self._banned_keys = store.banned_proxy_keys(self.service)
        except Exception as exc:  # noqa: BLE001 — без банов просто шире отбор
            LOGGER.warning("Browser service: баны не загружены: %s", exc)

        # Кэш вердиктов живёт В БАЗЕ (servers.db, таблица service_bans), а не
        # файлом рядом с профилем: это данные о серверах, им место там же, где
        # stable и capabilities. Прежний файловый кэш просто перестаёт
        # читаться, вердикты наберутся заново.
        self._cache = DbVerdictCache(self.service, store=store)
        stats = self._cache.stats()
        limit = ("без ограничения" if not self.max_checks
                 else "до %d за прогон" % self.max_checks)
        LOGGER.info(
            "Browser service: сервис %s, метка [%s], проверок: %s (пауза %.1f с), "
            "в кэше %d записей: %s",
            self.service, self.tag, limit, self.pause, stats["entries"],
            stats.get("verdicts") or "",
        )

    def wants(self, ctx: CheckContext, line: str) -> bool:
        """Кандидат — тот, кто дотянулся до ЛЮБОГО ресурса Google.

        Достаточно одной из меток accept_tags: gemini, Global, gstatic,
        youtube. Все они означают «сервер дозвонился до Google» — этого хватает,
        чтобы браузер попробовал открыть сам сервис.

        Почему не только [gemini]: метка про конкретную цель не говорит, пустит
        ли Google в сервис. Пользователь вручную подтвердил доступ к Gemini с
        сервера, помеченного [Global], и таких серверов в пуле много. Отбор по
        одной цели давал 2-5 кандидатов на весь пул — отсюда и «gemini вообще
        не находится».

        Метка НЕ значит «сервис работает»: заглушку по стране и стену трафика
        она не видит. Она значит ровно «дотянулся». Судит браузер.
        """
        if not self.enabled:
            return False

        caps = getattr(ctx, "capabilities_of", None)
        if not callable(caps):
            # Контекст без карты тегов: отбирать нечем, берём всех. Такой путь
            # возможен только при вызове чекера вне этапа проверки.
            return True
        tags = {t.strip().lower() for t in (caps(line) or "").split(",") if t.strip()}
        wanted = {str(t).strip().lower() for t in self.accept_tags}
        wanted.add(self.tag.strip().lower())
        if not (tags & wanted):
            return False
        # Уже забанен сервисом и бан не истёк — второй раз не платим.
        return _key_of(line) not in self._banned_keys

    async def check(self, ctx: CheckContext) -> CheckResult:
        if not self.enabled or self._cache is None:
            return CheckResult()

        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="нет серверов с меткой для браузерной проверки")

        from browser import refine

        LOGGER.info(
            "Browser service: кандидатов %d, беру до %d (лимит прогона)",
            len(lines), self.max_checks,
        )
        try:
            # Ни лимита числа, ни бюджета времени: чекер обязан довести
            # список до конца. Этап его дождётся (enrich_timeout поднят).
            LOGGER.info(
                "Browser service: проверяю ВСЕХ кандидатов (%d), лимита нет",
                len(lines),
            )
            results = await run_exclusive(
                refine.refine_lines, self.service, sorted(lines),
                max_checks=0, pause=self.pause,
                cache=self._cache, logger=LOGGER,
                budget_seconds=None,
            )
        except Exception as exc:  # noqa: BLE001 — чекер не должен ронять прогон
            LOGGER.error("Browser service: проверка не удалась: %s", exc)
            return CheckResult(summary="ошибка: %s" % str(exc)[:100])

        outcomes = {}
        ok = blocked = unstable = unknown = 0
        for line in lines:
            res = results.get(line)
            if res is None:
                # Не проверялся в этот раз (лимит или кэш без записи) — не трогаем.
                continue
            verdict = res.get("verdict")
            if verdict == "ok":
                ok += 1
                # Метка — именно self.tag ("gemini-chat"), а НЕ имя сервиса:
                # здесь стояло self.service, и браузерная проверка помечала
                # серверы старым голым "gemini" — то есть метки двух разных
                # транспортов (API и чата) были неразличимы.
                capabilities = self.tag
            elif verdict == "blocked":
                blocked += 1
                # Заглушка: своей метки не ставим. Снимать чужие отсюда
                # нельзя — снятие по списку категорий уносило метку целой
                # категории ("gemini-api" от HTTP-проверки) заодно со своей.
                # Чистку делает сам проход: профиль переписывает первый
                # ответивший чекер, и следующая проверка начнёт с чистого
                # листа. Здесь достаточно не дописывать свою метку.
                capabilities = ""
            elif verdict == "unstable":
                # Сервер отвечает по-разному от захода к заходу. Метку НЕ
                # ставим и НЕ снимаем: он не доказал, что сервис работает, но
                # и не отказал. Следующий прогон спросит снова — кэш у
                # unstable короткий (час).
                unstable += 1
                continue
            else:
                # unknown: ничего не меняем — сервер сам выпадет из ротации,
                # если перестанет работать.
                unknown += 1
                continue
            # Подтверждение браузером — ровно своя метка: она ДОПИСЫВАЕТСЯ к
            # профилю прохода, поэтому метку API ("gemini-api"), поставленную
            # другим транспортом, ничто не затирает.
            outcomes[line] = CheckOutcome(
                ok=None,
                capabilities=capabilities,
                detail=res.get("reason"),
            )

        summary = "ok %d, ban %d, нестабильных %d, без вердикта %d" % (
            ok, blocked, unstable, unknown)
        return CheckResult(outcomes=outcomes, summary=summary)

def _key_of(line: str) -> str:
    """Канонический ключ прокси (как в базе)."""
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(line)