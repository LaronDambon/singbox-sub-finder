"""Отбор «лучших» и «резервных» серверов ВНУТРИ каждой группы.

Зачем. Списки разбиты по профилю достижимости: в группе [youtube] лежат все
серверы, через которые YouTube открывается. Но открывается он по-разному:
у одного отклик 180 мс, у другого 3000. Клиенту незачем держать в группе и
тормоз, и быстрый: если ему нужен YouTube, он возьмёт самый отзывчивый.

Поэтому для КАЖДОЙ группы считается рейтинг ПО ЗАДЕРЖКЕ ДО ЭТОЙ КОНКРЕТНОЙ
ЦЕЛИ (колонка service_pings: {"youtube": 200, "google": 100}). Верхние N
получают тег [youtube-best], следующие M — [youtube-reserve]. Уровни именно
два: лучшие и запас на случай, если лучшие перестанут отвечать.

Почему НЕ по скорости. Замер скорости (speed_mbps) общий для сервера и к
конкретному сервису отношения не имеет: быстрый канал до спидтест-сервера
ничего не говорит о том, как отзывается YouTube. Раньше рейтинг считался
по гибриду скорость/пинг, и «лучшим по gemini» оказывался сервер, который
gemini вообще не проходил. Задержка до цели — единственный признак, который
измерен ИМЕННО для этой группы.

Порядок отбора. Лучшие выбираются ПОСЛЕ всех фильтров (см. вызов в
server_store.export_tagged_lines): если посчитать их раньше, фильтр по
профилю вырежет часть группы, и «лучшим» окажется не тот, кто лучший на
самом деле.

Сервер без замеров (нет service_pings по этой цели) в лучшие не попадает:
считать его лучшим не на чем. Он уходит в резерв — он всё ещё в группе.

Модуль ничего не знает ни про базу, ни про sing-box и работает на обычных
словарях записей, поэтому проверяется тестами без сети и без БД.
"""

from __future__ import annotations
from dataclasses import dataclass

#: Максимальная задержка, которая вообще считается замером (мс).
#: Всё, что больше, — это не «медленно», а «не дозвонились»: держать такое
#: в рейтинге бессмысленно, оно и так хуже любого измеренного значения.
MAX_MEANINGFUL_MS = 60_000


@dataclass(frozen=True)
class BestRules:
    """Правила отбора.

    top        — сколько ЛУЧШИХ на каждую группу (тег [tag-best]);
    reserve    — сколько РЕЗЕРВНЫХ следом (тег [tag-reserve]); 0 = не метить;
    min_group  — минимальный размер группы для ранжирования. По умолчанию
                 1: единственный сервер группы И ЕСТЬ её лучший, и без метки
                 группа "<метка>-best" в шаблоне не собиралась бы вовсе.
                 Раньше здесь стояло 2, и малочисленные сервисы (gemini-chat,
                 Global) оставались без уровней;
    tag_suffix — суффикс тега лучших;
    reserve_suffix — суффикс тега резерва;
    global_best_tag — тег абсолютных лучших по всему пулу.
    """

    top: int = 1
    reserve: int = 0
    min_group: int = 1
    tag_suffix: str = "-best"
    reserve_suffix: str = "-reserve"
    global_best_tag: str = "global-best"


def caps_of(record: dict) -> list[str]:
    """Профиль сервера: строка capabilities через запятую -> список тегов."""
    raw = record.get("capabilities") or ""
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _pings_of(record: dict) -> dict:
    """Замеры задержки до целей: {"youtube": 200, ...}.

    Принимает и готовый словарь, и JSON-строку из колонки базы.
    """
    from script.server_store import load_service_pings

    return load_service_pings(record.get("service_pings"))


def ping_for_target(record: dict, tag: str) -> float | None:
    """Задержка до конкретной цели в мс, либо None если замера нет.

    Ищем по имени цели, а не по тегу группы: тег группы (Global) — это
    свёртка нескольких целей, и задержки у них разные. Для группы берётся
    ЛУЧШАЯ из замеров её целей — сервер считается отзывчивым к группе,
    если хотя бы одна её цель отвечает быстро.
    """
    pings = _pings_of(record)
    if not pings:
        return None
    value = pings.get(tag)
    if value is None:
        # Регистр имени цели в разных прогонах мог отличаться.
        lowered = tag.lower()
        for name, ms in pings.items():
            if str(name).lower() == lowered:
                value = ms
                break
    if value is None:
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms < 0 or ms > MAX_MEANINGFUL_MS:
        return None
    return ms


def group_ping(record: dict, tag: str) -> float | None:
    """Задержка сервера до ГРУППЫ tag.

    Для тега-свёртки (Global) это СРЕДНЕЕ по всем целям группы. Раньше здесь
    был МИНИМУМ, и он давал неверный рейтинг: сервер, у которого из трёх
    целей быстро отвечает одна, а две не отвечают вовсе, выглядел для
    группы лучшим — то есть «хорош для Global» получал тот, кто Global как
    раз и не проходит. Среднее по целям этой ошибки не делает: чтобы иметь
    хороший балл, надо отвечать по всем целям сразу.

    Считается только по ИЗМЕРЕННЫМ целям: цель, к которой сервер не
    дозвонился, замер не даёт и в среднее не входит. Поэтому сервер с
    единственной быстрой целью остаётся ранжированным — он честно прошёл
    эту цель, — но балл его теперь взвешен по остальным.
    """
    # Тег-свёртка (merge): готовый замер с именем группы НЕ берём.
    # Чекер reachability кладёт туда МИНИМУМ по целям группы, и именно он
    # превращал "Global" в рейтинг «одна цель быстрая — остальные неважны».
    # На живой базе это давало серверу Global=390 при целях 1359/1203/390.
    #
    # Берём СРЕДНЕЕ по всем элементам списка группы, а не минимум: сервер
    # считается хорошим для группы, только если отвечает по всем её целям
    # сразу. Элемент списка — это имя в service_pings, поэтому в списке
    # может стоять и метка уровня ("yandex-reserve", "cloudflared-best"):
    # тогда в среднее идёт замер именно этой метки.
    names = _group_members(tag)
    if names:
        values: list[float] = []
        for name in names:
            ms = ping_for_target(record, name)
            if ms is not None:
                values.append(ms)
        if values:
            return sum(values) / len(values)
        # Ни один элемент группы не измерен: у сервера нет данных по этой
        # группе вообще. Возвращаем None, а не собственный замер тега, —
        # иначе сервер попал бы в рейтинг группы без единого её замера.
        return None
    # Обычный тег (не группа merge): замер самой цели.
    return ping_for_target(record, tag)


def _group_members(tag: str) -> list[str]:
    """Цели группы merge с этим тегом, В ПОРЯДКЕ ЗАПИСИ из файла.

    Список берётся как есть, а не «по смыслу цели». Поэтому элемент списка
    может быть не только именем цели, но и МЕТКОЙ УРОВНЯ:

        {"tag": "Global", "list": ["gstatic", "yandex-reserve", "cloudflared-best"]}

    Раньше здесь стояла привязка к имени "Global" и три захардкоженные
    цели, и от неё пришлось отказаться: группа merge — это просто имя.
    Групп может быть несколько и любых ("Global", "Core", "Fast"), а их
    состав задаётся ровно тем, что написано в "list".

    Группа без замеров возвращает пустой список: ранжировать её не по чему,
    и group_ping вернёт None.
    """
    try:
        from script.reachability_check import load_merge_rules

        for rule in load_merge_rules() or []:
            if str(rule.get("tag")) == tag:
                return [str(t).strip() for t in (rule.get("list") or ()) if str(t).strip()]
    except Exception:  # noqa: BLE001 — без файла групп просто нет
        pass
    return []


def _rank(members: list[dict], tag: str) -> tuple[list[dict], list[dict]]:
    """Делит группу на (измеренные по возрастанию задержки, без замеров).

    Замеренные идут первыми и упорядочены по задержке до ЭТОЙ цели. Серверы
    без замера сохраняют исходный порядок: сравнивать их между собой не на
    чем, а порядок строки в базе — это stable DESC, то есть живучесть.
    """
    measured: list[tuple[float, int, dict]] = []
    unmeasured: list[dict] = []
    for index, rec in enumerate(members):
        ms = group_ping(rec, tag)
        if ms is None:
            unmeasured.append(rec)
        else:
            measured.append((ms, index, rec))
    # Индекс вторым ключом: при равной задержке порядок остаётся устойчивым,
    # иначе один и тот же пул давал бы разные метки от прогона к прогону.
    measured.sort(key=lambda item: (item[0], item[1]))
    return [rec for _ms, _i, rec in measured], unmeasured


def _ranked(members: list[dict], tag: str) -> list[dict]:
    """Полный порядок группы: сначала измеренные по возрастанию задержки,
    затем остальные в порядке строк базы.

    Уровни нарезаются ИЗ ЭТОГО списка, поэтому «резервный» не может
    оказаться быстрее «лучшего», а «без метки» — быстрее обоих.
    """
    measured, unmeasured = _rank(members, tag)
    return measured + unmeasured


def pick_best(records, *, rules: BestRules | None = None,
              skip_profiles: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Ключи лучших по каждой группе: {"youtube": [ключ, ...]}.

    Один сервер может оказаться лучшим сразу в нескольких группах — это
    нормально и именно так и работает метка.

    skip_profiles — теги, которые не ранжируются. Сюда попадают теги
    скорости: «лучший по скорости» там и так выражен самой скоростью.
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
        # Порог — ровно один участник, и только он. Раньше требовалось ДВА,
        # и группа из одного сервера оставалась без уровней вовсе: метки
        # [gemini-chat-best] не существовало, хотя сервер в группе был, и
        # фильтр "<метка>-best" в шаблоне не находил НИКОГО.
        #
        # Замеров тоже может не быть ни у кого (прогон проверки не дошёл до
        # этой цели) — тогда порядок берётся из строк базы, то есть
        # stable DESC. Это не случайный выбор: stable — счётчик успешных
        # прогонов, и первая строка здесь и есть самый живучий сервер.
        if len(members) < max(1, rules.min_group):
            continue
        ordered = _ranked(members, tag)
        keys = [r.get("key") for r in ordered[: max(1, rules.top)] if r.get("key")]
        if keys:
            best[tag] = keys
    return best


def pick_reserve(records, *, rules: BestRules | None = None,
                 skip_profiles: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Ключи резерва по каждой группе: следующие за лучшими.

    Резерв берётся ИЗ ТОГО ЖЕ ранжированного списка, что и лучшие, поэтому
    «резервный» никогда не окажется быстрее «лучшего». Серверы без замеров
    идут после измеренных: они всё ещё в группе, но доказательств у них нет.
    """
    rules = rules or BestRules()
    if rules.reserve <= 0:
        return {}
    groups: dict[str, list[dict]] = {}
    for rec in records:
        for tag in caps_of(rec):
            if tag in skip_profiles:
                continue
            groups.setdefault(tag, []).append(rec)

    reserve: dict[str, list[str]] = {}
    for tag, members in groups.items():
        # Тот же порог, что и у лучших: группа из одного сервера уже отдала
        # его в best, и резерва в ней быть не может.
        if len(members) < max(1, rules.min_group):
            continue
        ordered = _ranked(members, tag)
        # Срез идёт СРАЗУ ЗА лучшими, по тому же порядку. Раньше здесь
        # требовалось не меньше двух ИЗМЕРЕННЫХ, и в группе, где замер есть
        # у одного, резерв не ставился вообще — вопреки собственной доке
        # модуля («сервер без замеров уходит в резерв»).
        tail = ordered[max(1, rules.top): max(1, rules.top) + rules.reserve]
        keys = [r.get("key") for r in tail if r.get("key")]
        if keys:
            reserve[tag] = keys
    return reserve


def pick_global(records, *, rules: BestRules | None = None,
                skip_profiles: tuple[str, ...] = ()) -> list[str]:
    """Абсолютные лучшие по всему пулу — по сумме задержек до целей.

    Критерий — средняя задержка по ВСЕМ измеренным целям сервера, а не по
    одной: сервер, лучший в одной группе, может быть никаким в остальных, и
    глобальный список нужен именно как «хорош везде». Серверы без единого
    замера в глобальный список не попадают.
    """
    rules = rules or BestRules()
    scored: list[tuple[float, int, dict]] = []
    for index, rec in enumerate(records):
        pings = _pings_of(rec)
        values = [
            float(ms) for ms in pings.values()
            if isinstance(ms, (int, float)) and 0 <= float(ms) <= MAX_MEANINGFUL_MS
        ]
        if not values:
            continue
        scored.append((sum(values) / len(values), index, rec))
    scored.sort(key=lambda item: (item[0], item[1]))
    count = max(rules.top, rules.min_group)
    return [rec.get("key") for _s, _i, rec in scored[:count] if rec.get("key")]


def add_best_tags(records, *, rules: BestRules | None = None,
                  skip_profiles: tuple[str, ...] = ()) -> list[dict]:
    """Возвращает копии записей с добавленными метками уровней.

    Лучшие получают [tag-best], резерв — [tag-reserve], абсолютные лучшие —
    [global-best]. Метка — это ДОБАВКА, а не замена: обычные теги остаются,
    поэтому фильтр по тегу best находит лучших, а его отсутствие никого не
    исключает.

    Исходные записи не меняются: колонка capabilities — обычный текст, и
    переписывать её на месте значило бы потерять исходный профиль.
    """
    rules = rules or BestRules()
    best_map = pick_best(records, rules=rules, skip_profiles=skip_profiles)
    reserve_map = pick_reserve(records, rules=rules, skip_profiles=skip_profiles)
    global_keys = set(pick_global(records, rules=rules, skip_profiles=skip_profiles))

    out: list[dict] = []
    for rec in records:
        key = rec.get("key")
        tags = caps_of(rec)
        extra: list[str] = []
        for tag, keys in best_map.items():
            if key in keys:
                extra.append(f"{tag}{rules.tag_suffix}")
        # Резерв берётся из того же ранжированного списка СЛЕДОМ за лучшими
        # (см. pick_reserve), поэтому пересечься с ними он не может по
        # построению. Отдельной проверки здесь не нужно: она была бы мёртвой.
        for tag, keys in reserve_map.items():
            if key in keys:
                extra.append(f"{tag}{rules.reserve_suffix}")
        if key in global_keys:
            extra.append(rules.global_best_tag)
        if not extra:
            out.append(rec)
            continue
        new_rec = dict(rec)
        # Порядок: сначала исходный профиль, затем метки уровней без дублей.
        new_rec["capabilities"] = ",".join(list(dict.fromkeys(tags + extra)))
        out.append(new_rec)
    return out
