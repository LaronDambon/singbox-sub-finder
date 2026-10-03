"""Отбор «лучших» серверов: помечает те, что грузят сервис быстрее прочих.

Зачем. Списки разбиты по профилю достижимости: в группе [gemini] лежат все
серверы, которые Gemini открывается. Но внутри группы они очень разные —
и по скорости, и по задержке. Клиенту незачем держать в одной группе и
тормоз, и быстрый канал: если ему нужен Gemini, он возьмёт самый быстрый.

Поэтому для каждого профиля вычисляется рейтинг и его верхняя часть
получает дополнительный тег, например [gemini-best]. Остальные серверы
попадают в список как обычно, со своими обычными тегами: тег «best» — это
не замена, а добавка, поэтому фильтр по тегу best находит лучших, а
отсутствие тега его не исключает.

Как считается рейтинг. По умолчанию серверы ранжируются по скорости
(speed_mbps), потому что ради скорости проверка и затевается. Если
измерения нет, используется задержка (ping_ms), а если нет и её — счётчик
жизни stable. Серверы без единого измеримого признака в рейтинг не попадают:
считать их «лучшими» не на чем.

Модуль ничего не знает ни про базу, ни про sing-box и работает на обычных
словарях записей, поэтому проверяется тестами без сети и без БД.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Какие признаки по важности: первый измеримый и определяет место.
#: Скорость важнее задержки (она включает в себя и задержку), а счётчик
#: жизни — последний: он вообще не про скорость.
RANK_KEYS = ("speed_mbps", "speed_down", "ping_ms", "stable")

#: Направление «лучше»: True — больше значит лучше, False — меньше.
HIGHER_IS_BETTER = {"speed_mbps": True, "speed_down": True, "speed_up": True,
                    "ping_ms": False, "stable": True}


@dataclass(frozen=True)
class BestRules:
    """Правила отбора.

    top        — сколько лучших оставить на каждый профиль;
    min_group  — минимальный размер группы, иначе «лучшие» выбирать не из чего
                 (из одного участника «лучший» — это просто он сам, тег
                 ничего не сообщает);
    tag_suffix — как тег называется рядом с именем профиля.
    """

    top: int = 1
    min_group: int = 2
    tag_suffix: str = "-best"


def _num(value):
    """Число из значения или None. Строки и мусор отбрасываются молча."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num


def _rank_key(record: dict):
    """Ключ сортировки по первому измеримому признаку.

    Возвращает (признак, значение) либо None, если измерять нечего.
    Признак сравнивается только внутри одного и того же признака: скорость
    в МБ/с и задержка в мс — разные величины, смешивать их в один список
    нельзя.
    """
    for name in RANK_KEYS:
        value = _num(record.get(name))
        if value is not None:
            return name, value
    return None


def caps_of(record: dict) -> list[str]:
    """Профиль сервера: строка capabilities через запятую -> список тегов."""
    raw = record.get("capabilities") or ""
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def rank_group(records) -> list[tuple[str, dict, float]]:
    """Сортирует записи от лучшей к худшей. Возвращает [(признак, запись, значение)].

    ВАЖНО: в одном рейтинге сравниваются только ОДНИ И ТЕ ЖЕ величины.
    Скорость в МБ/с и задержка в мс — разные единицы, и сравнивать их
    между собой численно бессмысленно: сервер с пингом 50 «победил» бы
    сервер со скоростью 9 МБ/с, просто потому что 50 больше 9.

    Поэтому сначала выбирается признак, доступный у наибольшего числа
    серверов группы, и ранжируются только они. Остальные в этом рейтинге
    не участвуют — для них будет отдельный раунд по своему признаку.
    """
    if not records:
        return []
    counts: dict[str, int] = {}
    for rec in records:
        info = _rank_key(rec)
        if info is not None:
            counts[info[0]] = counts.get(info[0], 0) + 1
    if not counts:
        return []
    # При равенстве побеждает более ранний признак в RANK_KEYS: скорость
    # важнее задержки.
    primary = max(counts, key=lambda name: (counts[name], -RANK_KEYS.index(name)))

    ranked = []
    for rec in records:
        value = _num(rec.get(primary))
        if value is not None:
            ranked.append((rec, primary, value))
    better = HIGHER_IS_BETTER.get(primary, True)
    ranked.sort(key=lambda item: item[2], reverse=better)
    return [(name, rec, value) for rec, name, value in ranked]

def pick_best(records, *, rules: BestRules | None = None,
              skip_profiles: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Ключи лучших серверов по каждому профилю: {"gemini": [ключ, ...]}.

    Один сервер может оказаться лучшим сразу в нескольких профилях — это
    нормально и именно так и работает метка.

    skip_profiles — теги, которые не имеют смысла ранжировать. В частности
    теги скорости: «лучший по скорости» там и так выражается самой скоростью,
    а из двух серверов с одинаковой скоростью лучшим окажется случайный.
    """
    rules = rules or BestRules()
    groups: dict[str, list[dict]] = {}
    for rec in records:
        for tag in caps_of(rec):
            if tag in skip_profiles:
                continue
            groups.setdefault(tag, []).append(rec)

    best: dict[str, list[str]] = {}
    for tag, members in groups.items():
        if len(members) < rules.min_group:
            continue
        ranked = rank_group(members)
        if not ranked:
            continue
        top = ranked[: max(1, rules.top)]
        keys = [rec.get("key") for _n, rec, _v in top if rec.get("key")]
        if keys:
            best[tag] = keys
    return best


def add_best_tags(records, *, rules: BestRules | None = None,
                  skip_profiles: tuple[str, ...] = ()) -> list[dict]:
    """Возвращает копии записей, у лучших добавлен тег профиля-best.

    Исходные записи не меняются: колонка capabilities — это обычный текст,
    и переписывать её на месте значило бы потерять исходный профиль.
    """
    rules = rules or BestRules()
    best = pick_best(records, rules=rules, skip_profiles=skip_profiles)
    out: list[dict] = []
    for rec in records:
        key = rec.get("key")
        tags = caps_of(rec)
        extra = [
            f"{tag}{rules.tag_suffix}"
            for tag, keys in best.items()
            if key in keys
        ]
        if not extra:
            out.append(rec)
            continue
        new_rec = dict(rec)
        new_rec["capabilities"] = ",".join(tags + extra)
        out.append(new_rec)
    return out

