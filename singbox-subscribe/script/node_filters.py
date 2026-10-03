"""Расширенная фильтрация серверов при генерации конфигов.

Старый «filter» в шаблонах умеет ровно одно: включить или исключить узлы
по регулярке к ИМЕНИ узла. Он не видит ни capabilities (какие сервисы сервер
открывает), ни страны, ни задержки, ни stable.

Этот модуль работает с ЗАПИСЯМИ серверов, а не с именами:

    {
      "capabilities": {"any": ["Global"], "none": ["speed-blocked"]},
      "exclude_countries": ["RU"],
      "min_stable": 3,
      "max_ping_ms": 1500,
      "protocols": ["vless", "trojan"]
    }

Что модуль гарантирует:
  * неизвестный ключ — ОШИБКА, а не тихий проход. Раньше опечатка в фильтре
    просто не влияла на результат, и конфиг уезжал в репозиторий «с
    фильтром», которого на самом деле не было;
  * регулярка, которая не компилируется, называет себя в сообщении. Раньше
    "[" в keywords ронял сборку всего конфига с re.error и без указания,
    какой именно фильтр виноват;
  * include — это именно include: пустой результат даёт ПУСТУЮ выборку.
    Раньше include со списком, в который ничего не подошло, молча
    возвращал все узлы целиком.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from script.country_data import COUNTRIES


class FilterError(ValueError):
    """Фильтр описан неверно — конфиг из-за него строить нельзя."""


#: Какие ключи вообще понимает server_filter. Всё остальное — опечатка.
ALLOWED_KEYS = frozenset({
    "capabilities",
    "include_countries",
    "exclude_countries",
    "country_regions",
    "exclude_country_regions",
    "min_stable",
    "max_ping_ms",
    "protocols",
    "exclude_protocols",
    "name_regex",
    "exclude_name_regex",
    "require_country",
})


# --- разрешение названий стран --------------------------------------------
# Флаг эмодзи кодирует ISO-код региональными индикаторами, поэтому
# эмодзи -> код считается точно, без таблицы: chr(0x1F1E6 + буква).


def _emoji_to_code(emoji: str) -> str | None:
    points = [ord(ch) for ch in emoji]
    if len(points) != 2 or not all(0x1F1E6 <= p <= 0x1F1FF for p in points):
        return None
    return "".join(chr(p - 0x1F1E6 + ord("A")) for p in points)


def _build_lookups() -> tuple[dict, dict, dict]:
    by_emoji: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    by_code: dict[str, dict] = {}
    for code, info in COUNTRIES.items():
        entry = {"code": code, "emoji": info[0], "name_en": info[1],
                 "name_ru": info[2], "region": info[3]}
        by_code[code.upper()] = entry
        if entry["emoji"]:
            by_emoji.setdefault(entry["emoji"], entry)
        for name in (code, info[1], info[2]):
            if name:
                by_name.setdefault(str(name).strip().lower(), entry)
    return by_emoji, by_name, by_code


_BY_EMOJI, _BY_NAME, _BY_CODE = _build_lookups()


def resolve_country(value: str) -> dict | None:
    """Страна по коду RU, флагу U0001F1F7U0001F1FA, «Russia» или «Россия»."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    code = _emoji_to_code(raw)
    if code:
        return _BY_CODE.get(code) or _BY_EMOJI.get(raw)
    upper = raw.upper()
    if upper in _BY_CODE:
        return _BY_CODE[upper]
    low = raw.lower()
    if low in ALIASES:
        return _BY_CODE.get(ALIASES[low])
    return _BY_NAME.get(low)


def country_codes(value: str) -> frozenset:
    """Все коды, которым соответствует ввод пользователя."""
    entry = resolve_country(value)
    if entry:
        return frozenset({entry["code"], entry["code"].upper()})
    return frozenset()


def country_regions(value: str) -> frozenset:
    """Регион(ы) страны: EU, ASIA, AMERICAS, AFRICA, OCEANIA, ..."""
    entry = resolve_country(value)
    return frozenset({entry["region"]}) if entry else frozenset()


KNOWN_REGIONS = frozenset({"EU", "ASIA", "AMERICAS", "AFRICA", "OCEANIA",
                          "MIDDLE_EAST", "OTHER"})



#: Синонимы, которых нет в справочнике. В ISO названия официальные —
#: «Russian Federation», «United Kingdom», — а в шаблонах люди пишут
#: «Russia», «UK». Без этих псевдонимов фильтр отвечал бы «страна не
#: распознана» на самом обычном написании.
ALIASES: dict[str, str] = {
    "russia": "RU",
    "russian federation": "RU",
    "россия": "RU",
    "рф": "RU",
    "uk": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "великобритания": "GB",
    "англия": "GB",
    "британия": "GB",
    "usa": "US",
    "сша": "US",
    "америка": "US",
    "южная корея": "KR",
    "корея": "KR",
    "китай": "CN",
    "hong kong": "HK",
    "гонконг": "HK",
    "taiwan": "TW",
    "тайвань": "TW",
    "emirates": "AE",
    "эмираты": "AE",
    "czechia": "CZ",
    "чехия": "CZ",
    "netherlands": "NL",
    "нидерланды": "NL",
    "holland": "NL",
    "индонезия": "ID",
    "vietnam": "VN",
    "вьетнам": "VN",
    "iran": "IR",
    "иран": "IR",
    "turkey": "TR",
    "турция": "TR",
    "türkiye": "TR",
    "brasil": "BR",
    "бразилия": "BR",
    "mexico": "MX",
    "мексика": "MX",
    "argentina": "AR",
    "аргентина": "AR",
    "egypt": "EG",
    "египет": "EG",
    "nigeria": "NG",
    "нигерия": "NG",
    "south korea": "KR",
    "soviet union": "RU",
}
def _as_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)) and all(isinstance(v, str) for v in value):
        return list(value)
    raise FilterError(f"{where}: ожидался список строк, получено {type(value).__name__}")


def _as_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise FilterError(f"{where}: ожидалось целое число, получено {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    raise FilterError(f"{where}: ожидалось целое число, получено {value!r}")


def _as_bool(value: Any, where: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "да"):
        return True
    if text in ("0", "false", "no", "off", "нет", ""):
        return False
    raise FilterError(f"{where}: ожидалось true/false, получено {value!r}")


def _compile(pattern: Any, where: str) -> re.Pattern | None:
    """Компилирует регулярку, называя её в ошибке.

    Раньше re.error из keywords поднимался без указания фильтра, и виновника
    приходилось искать вручную по всем шаблонам проекта.
    """
    if pattern is None or pattern == "":
        return None
    if not isinstance(pattern, str):
        raise FilterError(f"{where}: ожидалась регулярка-строка, получено {type(pattern).__name__}")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise FilterError(
            f"{where}: не компилируется регулярное выражение {pattern!r}: {exc}"
        ) from exc


def _norm_caps(value: Any, where: str) -> frozenset:
    """Теги capabilities приводятся к нижнему регистру."""
    return frozenset(t.strip().lower() for t in _as_list(value, where) if t.strip())


def _norm_regions(value: Any, where: str) -> frozenset:
    out = set()
    for item in _as_list(value, where):
        code = item.strip().upper()
        if code not in KNOWN_REGIONS:
            raise FilterError(
                f"{where}: неизвестный регион {item!r}. Допустимо: " + ", ".join(sorted(KNOWN_REGIONS))
            )
        out.add(code)
    return frozenset(out)


def _norm_countries(value: Any, where: str) -> frozenset:
    """Страны принимаются кодом, флагом и названием — на русском и английском.

    Неопознанное имя — ошибка, а не «исключить всех молча»: молчаливый
    промах в названии страны выглядел бы как «серверов не нашлось».
    """
    out = set()
    for item in _as_list(value, where):
        entry = resolve_country(item)
        if not entry:
            raise FilterError(
                f"{where}: не удалось распознать страну {item!r}"
            )
        out.add(entry["code"])
    return frozenset(out)


@dataclass(frozen=True)
class ServerFilter:
    """Готовый к применению фильтр серверов."""

    caps_any: frozenset = frozenset()
    caps_all: frozenset = frozenset()
    caps_none: frozenset = frozenset()
    include_countries: frozenset = frozenset()
    exclude_countries: frozenset = frozenset()
    include_regions: frozenset = frozenset()
    exclude_regions: frozenset = frozenset()
    min_stable: int | None = None
    max_ping_ms: int | None = None
    protocols: frozenset = frozenset()
    exclude_protocols: frozenset = frozenset()
    name_re: re.Pattern | None = None
    exclude_name_re: re.Pattern | None = None
    require_country: bool = False

    @property
    def is_empty(self) -> bool:
        """Ни одного условия — отбор не нужен."""
        return not (
            self.caps_any or self.caps_all or self.caps_none
            or self.include_countries or self.exclude_countries
            or self.include_regions or self.exclude_regions
            or self.min_stable is not None or self.max_ping_ms is not None
            or self.protocols or self.exclude_protocols
            or self.name_re or self.exclude_name_re or self.require_country
        )

    def describe(self) -> str:
        """Человеческое описание — для лога перед сборкой конфига."""
        if self.is_empty:
            return "без фильтра"
        parts: list[str] = []
        if self.caps_any:
            parts.append("любой из " + "/".join(sorted(self.caps_any)))
        if self.caps_all:
            parts.append("все из " + "/".join(sorted(self.caps_all)))
        if self.caps_none:
            parts.append("кроме " + "/".join(sorted(self.caps_none)))
        if self.include_countries:
            parts.append("страна в " + "/".join(sorted(self.include_countries)))
        if self.exclude_countries:
            parts.append("кроме стран " + "/".join(sorted(self.exclude_countries)))
        if self.include_regions:
            parts.append("регион в " + "/".join(sorted(self.include_regions)))
        if self.exclude_regions:
            parts.append("кроме регионов " + "/".join(sorted(self.exclude_regions)))
        if self.min_stable is not None:
            parts.append(f"stable >= {self.min_stable}")
        if self.max_ping_ms is not None:
            parts.append(f"ping <= {self.max_ping_ms} мс")
        if self.protocols:
            parts.append("протокол в " + "/".join(sorted(self.protocols)))
        if self.exclude_protocols:
            parts.append("кроме " + "/".join(sorted(self.exclude_protocols)))
        if self.require_country:
            parts.append("страна известна")
        if self.name_re:
            parts.append("имя ~ " + self.name_re.pattern)
        if self.exclude_name_re:
            parts.append("имя НЕ ~ " + self.exclude_name_re.pattern)
        return ", ".join(parts)

    def matches(self, server: Mapping[str, Any]) -> bool:
        """Подходит ли запись сервера под этот фильтр."""
        caps = _tags(server.get("capabilities"))
        if self.caps_any and not (caps & self.caps_any):
            return False
        if self.caps_all and not self.caps_all <= caps:
            return False
        if self.caps_none and (caps & self.caps_none):
            return False

        country = str(server.get("country") or "").strip()
        codes = country_codes(country)
        if self.require_country and not country:
            return False
        if self.include_countries and not (codes & self.include_countries):
            return False
        if self.exclude_countries and (codes & self.exclude_countries):
            return False
        if self.include_regions and not (country_regions(country) & self.include_regions):
            return False
        if self.exclude_regions and (country_regions(country) & self.exclude_regions):
            return False

        if self.min_stable is not None:
            stable = server.get("stable")
            if stable is None or int(stable) < self.min_stable:
                return False
        if self.max_ping_ms is not None:
            ping = server.get("ping_ms")
            if ping is None or int(ping) > self.max_ping_ms:
                return False

        proto = str(server.get("protocol") or "").strip().lower()
        if self.protocols and proto not in self.protocols:
            return False
        if self.exclude_protocols and proto in self.exclude_protocols:
            return False

        name = str(server.get("name") or server.get("line") or "")
        if self.name_re is not None and not self.name_re.search(name):
            return False
        if self.exclude_name_re is not None and self.exclude_name_re.search(name):
            return False
        return True

    def apply(self, servers: Iterable[Mapping[str, Any]]) -> list:
        """Оставляет только подходящие записи."""
        rows = list(servers)
        if self.is_empty:
            return rows
        return [s for s in rows if self.matches(s)]


def _tags(value: Any) -> frozenset:
    """capabilities хранится строкой «openai,gemini,» — в множество тегов."""
    if not value:
        return frozenset()
    if isinstance(value, (list, tuple, set)):
        return frozenset(str(v).strip().lower() for v in value if str(v).strip())
    return frozenset(t.strip().lower() for t in str(value).split(",") if t.strip())


def build_filter(spec: Any) -> ServerFilter:
    """Собирает ServerFilter из словаря-описания. Проверяет всё подряд.

    Единственное место, где живёт знание о ключах: неизвестный ключ —
    FilterError с перечнем допустимых, чтобы опечатку было видно сразу,
    а не через неделю по пустому конфигу в репозитории.
    """
    if spec is None:
        return ServerFilter()
    if not isinstance(spec, Mapping):
        raise FilterError(
            f"server_filter должен быть объектом JSON, получено {type(spec).__name__}"
    )
    unknown = [k for k in spec if k not in ALLOWED_KEYS]
    if unknown:
        raise FilterError(
            f"неизвестные ключи server_filter: {sorted(unknown)}. Допустимо: {sorted(ALLOWED_KEYS)}"
    )

    caps = spec.get("capabilities") or {}
    if not isinstance(caps, Mapping):
        raise FilterError(
        f"capabilities должен быть объектом с any/all/none, получено {type(caps).__name__}"
    )
    unknown_caps = [k for k in caps if k not in ("any", "all", "none")]
    if unknown_caps:
        raise FilterError(
            f"в capabilities неизвестны ключи {sorted(unknown_caps)}. Допустимо: any, all, none"
    )

    return ServerFilter(
        caps_any=_norm_caps(caps.get("any"), "capabilities.any"),
        caps_all=_norm_caps(caps.get("all"), "capabilities.all"),
        caps_none=_norm_caps(caps.get("none"), "capabilities.none"),
        include_countries=_norm_countries(spec.get("include_countries"), "include_countries"),
        exclude_countries=_norm_countries(spec.get("exclude_countries"), "exclude_countries"),
        include_regions=_norm_regions(spec.get("country_regions"), "country_regions"),
        exclude_regions=_norm_regions(spec.get("exclude_country_regions"), "exclude_country_regions"),
        min_stable=_as_int(spec.get("min_stable"), "min_stable"),
        max_ping_ms=_as_int(spec.get("max_ping_ms"), "max_ping_ms"),
        protocols=frozenset(
            p.strip().lower() for p in _as_list(spec.get("protocols"), "protocols") if p.strip()
        ),
        exclude_protocols=frozenset(
            p.strip().lower() for p in _as_list(spec.get("exclude_protocols"), "exclude_protocols") if p.strip()
        ),
        name_re=_compile(spec.get("name_regex"), "name_regex"),
        exclude_name_re=_compile(spec.get("exclude_name_regex"), "exclude_name_regex"),
        require_country=_as_bool(spec.get("require_country"), "require_country"),
    )