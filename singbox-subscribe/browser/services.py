"""Правила распознавания сервисов: где «работает», а где «заглушка».

Маркеры получены СРАВНЕНИЕМ реальных страниц, а не догадкой. Для Gemini
были два сохранённых файла с ОДНИМ И ТЕМ ЖЕ аккаунтом:

    заглушка (сервер не пускает)   -> «не поддерживается в вашей стране»
    рабочая   (сервер пускает)     -> «Новый чат», «Спросить Gemini»

Важно, какие маркеры НЕ работают: BardChatUi есть в обеих страницах (12/12),
а RegionBoundary — это вообще имя JS-ассета (QueryRegionBoundary-*.js) в
манифесте обычной страницы. Прежние правила банили по ним и выкашивали
рабочие серверы.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceSpec:
    """Как отличить рабочую страницу сервиса от заглушки."""

    name: str
    url: str
    #: Если ЛЮБАЯ из строк найдена — доступ закрыт (по стране/аккаунту).
    blocked_markers: tuple = ()
    #: Если найдена ЛЮБАЯ — сервис работает.
    ok_markers: tuple = ()
    #: Признаки ВХОДА: разделы, которые есть только у авторизованного
    #: пользователя. Проверено на живых серверах: анонимная страница содержит
    #: «Новый чат» и «Библиотека»... нет — содержит только «Новый чат», а
    #: «Новый блокнот», «Библиотека» и «Запланировано» появляются лишь после
    #: входа. Поэтому «Новый чат» НЕ годится как признак работы сервиса.
    login_markers: tuple = ()
    #: Признаки АНОНИМНОГО состояния: если они есть, вход не применился,
    #: и сервис через этот выход не работает — каким бы живым он ни был.
    anon_markers: tuple = ()
    #: Минимум совпадений ok-маркеров, чтобы считать страницу рабочей.
    ok_threshold: int = 1
    #: Сколько ждать появления ok-маркера, секунд.
    wait_seconds: float = 20.0


GEMINI = ServiceSpec(
    name="gemini",
    url="https://gemini.google.com/app",
    blocked_markers=(
        "не поддерживается в вашей стране",
        "Мы сообщим, когда это изменится",
        "isn't available in your country",
        "is not available in your country",
        "not available in your region",
        "not supported in your country",
        "country is not supported",
        "account-rejected-container",
        # Стена «подозрительный трафик»: Google отдаёт её на IP, помеченные как
        # дата-центровые/подозрительные. Это ТОЖЕ бан: через такой сервер
        # Gemini не работает, хотя причина не региональная, а репутационная.
        # Проверено: с домашнего IP страница открывается, через проверяемые
        # серверы — всегда /sorry/ с «unusual traffic».
        "unusual traffic",
        "our systems have detected",
        "/sorry/index",
    ),
    # Признаки РАБОТАЮЩЕГО приложения. Проверены на реальном сервере, который
    # открывает Gemini: там есть навигация («Новый чат», «Библиотека») и
    # аккаунт, но НЕТ «Спросить Gemini», если открылась история чатов, а не
    # страница нового чата. Поэтому набор должен покрывать оба случая.
    #
    # Ложный ok снимается не сужением списка, а выдержкой: заглушка побеждает
    # в любой момент, а ok должен продержаться (см. _probe_once в batch.py).
    # «Новый чат» убран из ok-маркеров: он есть и на анонимной странице,
    # поэтому по нему проходили серверы, где Gemini под аккаунтом не работал.
    ok_markers=(
        "Спросить Gemini",
        "Ask Gemini",
        "Введите запрос",
        "Enter a prompt",
    ),
    # Раздел есть только у вошедшего. Не привязано к аккаунту: подойдёт
    # любой пользователь, поэтому смена аккаунта фильтр не ломает.
    login_markers=(
        "Новый блокнот",
        "Библиотека",
        "Запланировано",
        "New notebook",
        "Library",
        "Scheduled",
    ),
    # Просьба войти = вход не применился.
    #
    # Только ФРАЗЫ, а не отдельные слова. Голые "Sign in" / "Войти" есть и в
    # HTML-шаблоне любой страницы (строка меню аккаунта), поэтому по ним живой
    # сервер получал бан с причиной «не выполнен вход» — замерено: все 7
    # записей в базе были такими ложными.
    anon_markers=(
        "Чтобы сохранить историю",
        "Sign in to save activity",
    ),
    wait_seconds=25.0,
)

CLAUDE = ServiceSpec(
    name="claude",
    url="https://claude.ai/",
    blocked_markers=(
        "app-unavailable-in-region",
        "not available in your region",
        "isn't available in your country",
        "unusual traffic",
        "our systems have detected",
        "/sorry/index",
    ),
    # Просьба войти — фразой, не отдельным словом (см. комментарий у gemini).
    anon_markers=(
        "Sign in to save activity",
        "Чтобы сохранить историю",
    ),
    ok_markers=(
        "New chat",
        "Start new chat",
        "Новый чат",
    ),
    wait_seconds=20.0,
)

CHATGPT = ServiceSpec(
    name="chatgpt",
    url="https://chatgpt.com/",
    blocked_markers=(
        "not available in your country",
        "not supported in your country",
    ),
    ok_markers=("New chat", "What can I help with"),
    wait_seconds=20.0,
)

#: Реестр сервисов по имени.
SERVICES: dict = {
    GEMINI.name: GEMINI,
    CLAUDE.name: CLAUDE,
    CHATGPT.name: CHATGPT,
}


#: Вердикты, которыми заканчивается проверка сервиса. Их ТРИ, и они одни и те
#: же у обоих транспортов: браузер (browser/batch.py) и API (script/
#: reachability_check.py). Одинаковые слова — не косметика: вердикты ложатся
#: в ОДНУ таблицу service_bans и в ОДНУ колонку service_pings, и «работает»
#: там должно значить одно и то же, чем бы проверка ни шла.
VERDICT_OK = "ok"
VERDICT_BLOCKED = "blocked"
VERDICT_UNSTABLE = "unstable"
VERDICT_UNKNOWN = "unknown"


def api_verdicts() -> dict[str, dict]:
    """API-цели проверки сервисов, по имени сервиса.

    Читаются из ТОГО ЖЕ config/reachability_targets.json, по которому работает
    reachability: цель с блоком правил — это сервис, проверяемый по HTTP-ответу.
    Имя цели обязано совпадать с именем сервиса (gemini, claude, ...), потому
    что вердикты пишутся в общие с браузером таблицы по этому ключу.

    Так у двух тестов ОДИН список сервисов: добавили цель в файл целей — она
    сразу есть и у браузера (через tag), и у API (через имя).

    Файл может отсутствовать или не разбираться — это «API-целей нет», а не
    падение: вызывающий код просто не получает кандидатов.
    """
    try:
        from script.reachability_check import load_targets

        return {
            t["name"]: t for t in load_targets()
            if t.get("rules") and t.get("name")
        }
    except Exception:  # noqa: BLE001 — нет файла = нет API-целей
        return {}


def get_service(name: str) -> ServiceSpec:
    """Сервис по имени или понятная ошибка со списком известных."""
    key = str(name).strip().lower()
    if key not in SERVICES:
        raise KeyError(
            "Неизвестный сервис %r. Известные: %s"
            % (name, ", ".join(sorted(SERVICES)))
        )
    return SERVICES[key]