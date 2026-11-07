"""Чекер сервиса по API: работает ли сервис с этого адреса (дешёвый браузерный тест).

Место в конвейере
-----------------
Идёт ПОСЛЕ reachability — ровно там же, где browser_service, и делает ту же
работу, только другим транспортом. Reachability ставит грубую метку: «сервер
дотянулся до страницы по TTFB». Что на этой странице — HTTP-запрос не видно:
заглушку по стране Google рисует клиентски после входа. Поэтому метку берёт
этот чекер и говорит честно: сервис работает (ok) или отказал (ban).

Зачем он нужен, если есть браузер. У сервисов, кроме страницы, есть ЧЕСТНЫЙ
машинный ответ, и он читается обычным запросом:

    Gemini     400 {"error":{"code":400,"message":"User location is not
               supported for the API use.","status":"FAILED_PRECONDITION"}}
    OpenRouter 403 {"success": false, "error": "Access denied by security policy."}

Это тот же самый отказ по региону, сказанный прямо. Разница только в цене:
браузер — 3-20 секунд на сервер и заходы по одному (иначе Google показывает
стену «unusual traffic»), API — 0.2-2 секунды на сервер и пачками, без сессии
и без браузера. На прогоне в сотни серверов это разница в часы.

ОДНА логика с браузером
-----------------------
Это не «вторая проверка», а тот же тест другим транспортом, поэтому и живёт
он в том же месте — рядом с browser_service:

  * список сервисов один: цели с правилами из config/reachability_targets.json
    (browser/services.py -> api_verdicts). Добавили цель — она есть у обоих;
  * словарь вердиктов один: ok / blocked / ban / unstable / unknown, и разбор
    ответа по правилам — тот же принцип, что в browser/batch.py;
  * база одна: таблица service_bans в servers.db, кэш DbVerdictCache. Второй
    таблицы для «API-вердиктов» не заводится — иначе два теста разошлись бы
    ровно там, где должны сходиться;
  * бренч один: вердикт становится меткой сервиса ровно как у браузера.

Про РАЗНЫЕ ТОЧКИ. Дублированием это не является: у Google разные точки и
разная политика доступности. Веб-приложение чата и API отвечают независимо,
и сервер, пускающий в одно, может не пускать в другое. Поэтому обе проверки
имеют смысл одновременно, и метки у них РАЗНЫЕ — "gemini-chat" у браузера и
"gemini-api" у API: по метке видно, каким транспортом доступ подтверждён.

Выбор между ними — про ЦЕНУ, а не про правильность: браузер тратит 3-20 с на
сервер и заходит по одному, API — 0.2-2 с и пачками. Если нужен только факт
«сервис отвечает», хватит API; если нужна именно доступность чата под
аккаунтом, без браузера не обойтись.

Почему не решает живость
------------------------
decides_availability = False: проверка сервиса не влияет на stable и available.
Не удалась — сервер не становится мёртвым, он просто не получает метку сервиса.
"""

from __future__ import annotations

from config.settings import get_settings
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.api_service")


def _key_of(line: str) -> str:
    """Канонический ключ прокси (как в базе)."""
    from script.downloader import normalize_proxy_key

    return normalize_proxy_key(line)


class ApiServiceChecker(Checker):
    """Проверка сервиса HTTP-запросом с ключом: работает он с этого адреса или нет."""

    name = "api_service"
    description = "API: работает ли сервис с этого адреса -> ban/ok (дешёвый браузерный тест)"
    #: Живость решает url_probe: здесь только уточнение метки сервиса.
    decides_availability = False
    #: Мёртвые не проверяются: сервис нужен лишь тем, кто дотянулся.
    run_on_dead = False
    #: Категория тегов, которую чекер ЗАМЕНЯЕТ: имена сервисов. Та же
    #: категория, что у browser_service, — метку ставит тот транспорт, что
    #: отработал, а второй её не дублирует.
    #: "gemini-api" — метка ЭТОГО чекера (ставится при ok), "gemini-chat" —
    #: метка браузерной проверки: имя метки берётся из конфига и не обязано
    #: совпадать с именем сервиса. Голое "gemini" — метка прошлых прогонов.
    own_tags: tuple = ("gemini", "gemini-api", "gemini-chat", "claude", "chatgpt")

    @staticmethod
    def tag_of(service: str) -> str:
        """Имя метки для сервиса, доступ к которому подтверждён по API.

        Имя сервиса и имя метки — разные вещи. Для gemini метка — "gemini-api":
        так видно, КАКИМ транспортом доступ подтверждён (API или браузер),
        и метка не путается с "gemini-chat" от browser_service.
        """
        name = str(service or "").strip()
        if not name:
            return ""
        return "gemini-api" if name == "gemini" else name

    def __init__(self) -> None:
        #: Имена сервисов, которые проверяются по API. Список ОБЩИЙ с
        #: браузером (общие цели в reachability_targets.json) — поэтому здесь
        #: он не хардкодится, а загружается в setup().
        self.services: dict = {}
        #: Кэш вердиктов и баны — по каждому сервису свой.
        self._cache: dict = {}
        self._banned_keys: dict = {}
        self._concurrency = 8
        self._min_stable = 0
        self.enabled = True

    async def setup(self, ctx) -> None:
        """Готовит список API-целей, кэш вердиктов и загруженные баны."""
        from browser import services
        from browser.db_cache import DbVerdictCache
        from script.server_store import ServerStore

        self.services = services.api_verdicts()
        if not self.services:
            # Целей с правилами нет — проверять нечего. Это не ошибка: файл
            # целей может быть ещё не расширен блоками rules.
            LOGGER.warning(
                "API service: в конфиге целей нет целей с правилами — чекер выключен",
            )
            self.enabled = False
            return

        # Настройки берём из секции service: предмет проверки тот же, что у
        # browser_service, и отдельного набора ручек ему не нужно.
        svc = getattr(get_settings(), "service", None)
        self._min_stable = int(getattr(svc, "min_stable", 0) or 0)
        self._concurrency = int(getattr(svc, "concurrency", 8) or 8)

        store = ServerStore(get_settings().paths.servers_db_file)
        for name in sorted(self.services):
            self._cache[name] = DbVerdictCache(name, store=store)
            try:
                self._banned_keys[name] = store.banned_proxy_keys(name)
            except Exception as exc:  # noqa: BLE001 — без банов просто шире отбор
                LOGGER.warning("API service: баны %s не загружены: %s", name, exc)
                self._banned_keys[name] = set()

        total = sum(c.stats()["entries"] for c in self._cache.values())
        LOGGER.info(
            "API service: сервисы %s, проверка для stable >= %d, параллелизм %d, "
            "в кэше %d записей",
            ", ".join(sorted(self.services)), self._min_stable,
            self._concurrency, total,
        )

    def wants(self, ctx: CheckContext, line: str) -> bool:
        """Кандидат — живой сервер, ещё не забаненный ВСЕМИ проверяемыми сервисами.

        Отбора по метке, как у браузера (accept_tags), здесь нет намеренно.
        Метка reachability нужна браузеру, чтобы не открывать сессию зря, а
        API-запрос стоит копейки — дешевле спросить весь живой пул, чем
        пропустить сервис, который на самом деле работает.

        Что действительно экономит: сервер, уже отказавший сервису, повторно не
        спрашивается, пока бан не истечёт по TTL (заглушка по стране — 6 часов,
        репутационная стена — 30 дней).
        """
        if not self.enabled or not self.services:
            return False

        stable = getattr(ctx, "stable_of", None)
        if callable(stable):
            value = stable(line)
            if value is not None and int(value) < self._min_stable:
                return False

        key = _key_of(line)
        if key and all(key in (self._banned_keys.get(s) or set())
                       for s in self.services):
            return False
        return True

    def _pending(self, lines: list[str]) -> dict:
        """Кого из строк надо спросить по каждому сервису.

        Два повода не спрашивать: сервис уже отказал этому прокси (бан) и по
        паре есть СВЕЖИЙ вердикт в кэше. Второе — главная экономия прогона:
        сроки жизни вердиктов те же, что у браузера (ok — час, blocked — шесть
        часов), и повторный прогон почти ничего не стоит.
        """
        out: dict = {}
        for name in sorted(self.services):
            cache = self._cache.get(name)
            if cache is None:
                continue
            todo = [line for line in lines if cache.get(line) is None]
            if todo:
                out[name] = todo
        return out

    async def check(self, ctx: CheckContext) -> CheckResult:
        if not self.enabled or not self.services:
            return CheckResult()

        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="нет живых серверов для API-проверки сервисов")

        todo = self._pending(lines)
        if not todo:
            return CheckResult(summary="все вердикты свежие, проверять нечего")

        # ОДИН батч sing-box на все сервисы сразу: цель — это просто ещё один
        # URL через тот же локальный порт. Поднимать sing-box по разу на сервис
        # означало бы платить за старт столько раз, сколько сервисов.
        targets = [self.services[name] for name in sorted(todo)]
        checks = sorted({line for group in todo.values() for line in group})
        LOGGER.info(
            "API service: сервисов %d, серверов %d (из %d живых)",
            len(targets), len(checks), len(lines),
        )
        try:
            results = await run_exclusive(self._batch, checks, targets=targets)
        except Exception as exc:  # noqa: BLE001 — чекер не должен ронять прогон
            LOGGER.error("API service: проверка не удалась: %s", exc)
            return CheckResult(summary="ошибка: %s" % str(exc)[:100])

        from script.reachability_check import service_verdicts

        # Вердикты собираются СНАЧАЛА в таблицу (строка -> сервис -> вердикт), и
        # только потом превращаются в бренчи. Так «кого метить» решается в одном
        # месте, а не по дороге, где порядок сервисов решал бы, кто победил.
        verdicts: dict = {}
        counts = {"ok": 0, "ban": 0, "unstable": 0, "unknown": 0, "errors": 0}
        pending: list = []
        for name in sorted(todo):
            target = self.services[name]
            for line in todo[name]:
                # Вердикт лежит во ВЛОЖЕННОМ блоке "targets": результат пробы —
                # это {"error", "targets": {имя цели: запись}}. Если передать
                # service_verdicts сам результат, она не найдёт ключ цели и
                # вернёт "unknown" ВСЕГДА — проверка отработает впустую, а в
                # отчёте будет «без вердикта» при живых ответах сервиса.
                probed = (results.get(line) or {}).get("targets") or {}
                entry = service_verdicts([target], probed).get(name)
                if entry is None:
                    counts["errors"] += 1
                    continue
                verdict = str(entry.get("verdict") or "unknown")
                counts[verdict if verdict in counts else "unknown"] += 1
                if verdict in ("ok", "blocked", "ban", "unstable", "unknown"):
                    verdicts.setdefault(line, {})[name] = entry
                # В кэш пишем только то, на что сервис ОТВЕТИЛ. Иначе
                # «запрос не ушёл» (сервер лёг, таймаут) записалось бы как
                # вердикт сервиса — а это разные вещи: сбой пробы не значит,
                # что сервис закрыт.
                if entry.get("answered"):
                    pending.append((self._cache.get(name), line, {
                        "verdict": verdict,
                        "reason": entry.get("reason") or "",
                        "checked_by": "api",
                    }))

        outcomes = {}
        for line, by_service in verdicts.items():
            tags: list[str] = []
            denied: list[str] = []
            reasons: list[str] = []
            for name, entry in sorted(by_service.items()):
                verdict = str(entry.get("verdict") or "unknown")
                if verdict == "ok":
                    # Метка сервиса может НЕ совпадать с его именем: доступ,
                    # подтверждённый HTTP-запросом, помечается "<сервис> api"
                    # (gemini -> "gemini-api"), а браузерный — своим именем из
                    # конфига ("gemini-chat"). Прежде здесь стояло голое name,
                    # и метки двух транспортов были неразличимы.
                    tags.append(self.tag_of(name))
                elif verdict in ("blocked", "ban"):
                    denied.append(name)
                else:
                    # unknown/unstable: сервер не доказал, что сервис работает,
                    # но и не отказал. Метку не ставим и не снимаем.
                    continue
                if entry.get("reason"):
                    reasons.append("%s: %s" % (name, entry["reason"]))
            if not tags and not denied:
                continue
            # Метки только ДОПИСЫВАЮТСЯ. Раньше здесь стояло снятие по списку
            # категорий, и это была дорогая ошибка: список снимал метку
            # КАТЕГОРИИ, а не одну метку сервиса, поэтому отказ gemini уносил
            # и честно заработанный [openrouter].
            #
            # Отказ сервиса теперь ничего не снимает по одной простой причине:
            # профиль целиком переписывает ПЕРВЫЙ писатель прохода
            # (reachability). Его ответ и есть полная картина на этот проход,
            # а чужие теги до этого просто не успели появиться.
            outcomes[line] = CheckOutcome(
                ok=None,
                capabilities=",".join(tags),
                detail="; ".join(reasons) or None,
            )

        # Вердикты в базу: следующий прогон возьмёт их из кэша.
        saved = 0
        for cache, line, payload in pending:
            if cache is None:
                LOGGER.warning("API service: нет кэша для вердикта %r", payload)
                continue
            try:
                cache.put(line, payload)
                saved += 1
            except Exception as exc:  # noqa: BLE001 — кэш не важнее прогона
                LOGGER.warning("API service: вердикт не сохранён: %s", exc)
        LOGGER.info("API service: вердиктов собрано %d, сохранено %d", len(pending), saved)

        summary = "ok %d, ban %d, нестабильных %d, без вердикта %d, сбоев %d" % (
            counts["ok"], counts["ban"], counts["unstable"],
            counts["unknown"], counts["errors"])
        return CheckResult(outcomes=outcomes, summary=summary)

    # ------------------------------------------------------------------ проба
    @staticmethod
    def _batch(lines: list[str], *, targets: list, concurrency: int = 8) -> dict:
        """Пробы одним батчем sing-box. Синхронно: вызывается под run_exclusive."""
        from script.reachability_check import batch_reachability_check

        return batch_reachability_check(
            list(lines), targets=targets, concurrency=concurrency,
        )
