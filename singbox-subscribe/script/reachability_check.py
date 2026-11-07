"""Профилирование достижимости прокси до целевых сайтов (reachability check).

По аналогии с country_check: один процесс sing-box на батч прокси (N mixed-inbound
на 127.0.0.1, каждый жёстко привязан к своему outbound), и параллельные HTTP-запросы
через эти локальные порты. Но вместо geo-проб (определение страны) каждый порт
опрашивается по СПИСКУ ЦЕЛЕВЫХ сайтов из config/reachability_targets.json.

Цель: узнать, до каких целевых сайтов (openrouter, gemini, youtube, telegram, ...)
прокси реально дозванивается и с какой латентностью. Профиль записывается в БД
(колонка capabilities); тэги [name] в итоговую строку добавляются только при
экспорте в whitelist для генерации итогового конфига.

Особые метки собираются блоком "merge" того же файла: группа — это список
тэгов целей и порог min_matches. Сервер, прошедший порог, помечается в БД
одним общим тегом вместо тегов группы. Пример: [Global] — любые 3 из 3 целей
gstatic / yandex-portal / cloudflare (generate_204). Раньше [Global] требовал
пройти ВСЕ цели профиля разом, и добавление девятой цели отнимало метку у
всех разом.

Задержка здесь — это TTFB: время до ЗАГОЛОВКОВ ответа (stream=True), а не
время выкачивания страницы. Тело обрывается через REACHABILITY_BODY_BYTES.
Порог — REACHABILITY_MAX_PING_MS (или max_ping_ms цели), и сравнивается
именно с TTFB: полное время загрузки страницы с порогом пинга не сравнимо
(openai отвечает 404 за 3.4 с на живом прокси — это нормальный TTFB).

Правила и вердикт (то, чем цель отличается от обычной)
----------------------------------------------------
У цели может быть блок "rules" — те же правила, что у браузерной проверки
сервиса (browser/services.py -> browser/batch.py), и тот же вердикт:
ok / ban / unknown. Тогда цель проверяется не «по задержке», а по ОТВЕТУ:
статус и ТЕЛО ответа прогоняются через правила, и вердикт едет в бренч
pipeline/checkers/builtin/reachability.py -> service_bans — ТУ ЖЕ таблицу,
которую пишет браузер. Поэтому «api test» и «браузерный тест» — это один и
тот же тест: одна группа сервисов, одна база, один результат.

Разница только в транспорте: браузер открывает страницу клиентом, а здесь
идёт HTTP-запрос с ключом из окружения (см. _expand_env). Ключ живёт в
переменной окружения, а не в конфиге: файл с целями лежит в репозитории.

Задержка у такой цели всё равно меряется (TTFB), но порог к вердикту
отношения не имеет: сервис, ответивший «регион не поддерживается» за 300 мс,
НЕ работает, хотя порог прошёл.

CLI:
  python -m script.reachability_check '<proxy_line>'
  python -m script.reachability_check --targets openrouter,gemini '<proxy_line>'
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ROOT — папка пакета (код). Рабочие файлы (source/, logs/, config/)
# лежат в корне репозитория: PROJECT_ROOT. Пути в конфиге — от него же.
ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent

from config.settings import get_settings

from script.logger_utils import get_project_logger
from script.core import release_ports, reserve_ports
from script.batch_singbox import (
    build_batch_config, normalize_key, parse_outbound,
    ports_ready, start_singbox, stop_singbox,
)

LOGGER = get_project_logger("reachability_check")

# --- Параметры probing -------------------------------------------------------
# Таймаут CONNECT одного целевого запроса, сек. Остальные параметры — из
# настроек, и читаются они в точке использования: REACHABILITY_TIMEOUT в
# _probe_target, REACHABILITY_BODY_* там же, REACHABILITY_MAX_PING_MS в
# load_targets/_probe_target/_log_probe_stats, а параллелизм внутри одного
# прокси (REACHABILITY_PER_PROXY_CONCURRENCY) — в _run_batch.
PROBE_CONNECT_TIMEOUT = 4.0
# Ожидание готовности inbound-портов при старте sing-box, сек.
STARTUP_WAIT_SECONDS = 10.0
# Максимум inbound'ов (и прокси) в одном процессе sing-box.
MAX_BATCH_INBOUNDS = 100

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --- Правила API-целей -------------------------------------------------------
#: Исходы, которыми может закончиться проба. Те же три слова, что у браузерной
#: проверки сервиса и у таблицы service_bans: сервис работает, сервис отказал,
#: неизвестно.
API_OK = "ok"
API_BAN = "ban"
API_UNKNOWN = "unknown"

#: Подстановка $env:ИМЯ и $env:ИМЯ:префикс в заголовках целей. Ключ API нельзя
#: держать в config/reachability_targets.json: файл лежит в репозитории.
_ENV_REF = re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*)")
#: $env:ИМЯ:префикс — «Bearer » перед ключом и тому подобное.
_ENV_REF_PREFIXED = re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*):([^\s]*)")

#: Любая ссылка $env: — включая имена, которые не разобрал регексп выше
#: (кириллица, дефис, пробел). Нужна для зачистки остатков: неподставленная
#: ссылка в заголовке хуже пустого заголовка, потому что сервис ответит на неё
#: как на неверный ключ, и это будет выглядеть отказом сервиса.
_ENV_REF_ANY = re.compile(r"\$env:[^\s,;]*")

#: Ключи правила, которые являются УСЛОВИЕМ ответа. Всё остальное (verdict,
#: reason) — назначение правила. Нужен, чтобы условия можно было писать плоско,
#: в одной строке с вердиктом.
_RULE_CONDITION_KEYS = frozenset({
    "status_in", "body_contains", "body_contains_all", "body_not_contains",
    "body_json_success", "body_json_error_status",
})


# --- Подстановка секретов и разбор ответа по правилам -------------------------
def _expand_env(text: str) -> str:
    """Раскрывает $env:ИМЯ (и $env:ИМЯ:префикс) в строке заголовка.

    Ключи API не лежат в config/reachability_targets.json: файл — часть
    репозитория, а ключ — секрет. Поэтому в конфиге стоит ссылка на переменную
    окружения ("Authorization": "$env:OPENROUTER_API_KEY:Bearer "), а значение
    подставляется здесь, в момент запроса.

    Неизвестная переменная даёт ПУСТУЮ строку, а не "$env:X" в заголовке:
    отправлять сервису литерал "$env:X" бессмысленно, и лучше получить
    честный отказ по ключу, чем загадочный ответ.
    """
    if not text or "$env:" not in text:
        return text

    def with_prefix(match: re.Match) -> str:
        value = os.environ.get(match.group(1), "")
        if not value:
            return ""
        # Префикс отделяется от ключа РОВНО ОДНИМ пробелом.
        #
        # Тут была ошибка, стоившая целой цели: хвостовой пробел из шаблона
        # ("...:Bearer ") попадал в ЗНАЧЕНИЕ заголовка, а requests такое
        # значение отвергает — InvalidHeader "Invalid leading whitespace" —
        # и проба падала ДО запроса. Со стороны это выглядело как «сервис не
        # ответил»: ни статуса, ни тела, вердикт unknown у ВСЕХ серверов.
        # Теперь в конфиге пишется "$env:OPENROUTER_API_KEY:Bearer" (без
        # пробела), а разделитель ставится здесь и только между частями.
        prefix = match.group(2).strip()
        return (prefix + " " + value) if prefix else value

    def plain(match: re.Match) -> str:
        return os.environ.get(match.group(1), "")

    # Сначала форма с префиксом: у неё в шаблоне есть двоеточие, и обычная
    # замена съела бы имя переменной вместе с ним.
    text = _ENV_REF_PREFIXED.sub(with_prefix, text)
    text = _ENV_REF.sub(plain, text)
    # Остатки ссылок вычищаются ЦЕЛИКОМ, даже если имя переменной записано
    # так, что регексп его не разобрал (кириллица, дефис). Отправлять сервису
    # литерал "$env:НЕТ_ТАКОЙ" бессмысленно: он уйдёт в заголовок как ключ,
    # сервис ответит 401, и это будет выглядеть как «сервис не пускает», хотя
    # на самом деле не подставилась переменная.
    text = _ENV_REF_ANY.sub("", text)
    # Хвостовой пробел срезается у ВСЕГО значения: даже если он остался от
    # шаблона или от самого ключа, requests отвергнет заголовок целиком
    # (Invalid leading whitespace) и проба не уйдёт вовсе.
    return text.strip()


def expand_headers(headers: dict | None) -> dict:
    """Заголовки цели с раскрытыми $env:-ссылками; пустые выбрасываются."""
    out: dict = {}
    for name, value in (headers or {}).items():
        expanded = _expand_env(str(value))
        if expanded:
            out[str(name)] = expanded
    return out


def _rule_conditions(rule: dict) -> dict:
    """Условия правила: их можно писать и вложенно ("if"), и плоско.

    Плоская запись — не украшение. В файле целей строка правила обязана
    читаться человеком с одного взгляда:

        {"verdict": "ban", "status_in": [403], "body_contains": ["..."], "reason": "..."}

    Вложенный вариант ("if": {...}) тоже поддерживается — он привычнее тем,
    кто писал правила для script/service_check.py, и оба разбираются одним
    кодом, чтобы не держать две почти одинаковые ветки.

    Ключи самого правила (verdict, reason) в условия не попадают: у них
    другое назначение.
    """
    if not isinstance(rule, dict):
        return {}
    nested = rule.get("if")
    cond = dict(nested) if isinstance(nested, dict) else {}
    for key, value in rule.items():
        if key in _RULE_CONDITION_KEYS or key == "if":
            cond.setdefault(key, value)
    return cond


def _rule_markers(rules: list[dict]) -> list[str]:
    """Подстроки, которые правила ищут в теле.

    По ним решается, когда чтение тела можно прекратить: если все маркеры уже
    найдены (или найден запрещённый), вердикт дальше не изменится. Приём взят
    из script/service_check.py, где он экономит секунды на каждой пробе.
    """
    markers: list[str] = []
    for rule in rules:
        cond = _rule_conditions(rule)
        if not cond:
            continue
        for key in ("body_contains", "body_contains_all", "body_not_contains"):
            value = cond.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                text = str(item or "")
                if text and text not in markers:
                    markers.append(text)
    return markers


def _as_list(value) -> list[str]:
    """Подстрока или список альтернатив -> список строк."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _json_field(body: str, path: str):
    """Значение поля JSON по пути "error.status"; None, если не разобрать.

    Тело отвечает не всегда (обрыв чтения, HTML вместо JSON), поэтому ошибка
    разбора — это None, а не исключение: правило по такому условию просто не
    совпадёт, и вердикт останется default_verdict.
    """
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return None
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list):
            try:
                data = data[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return data


def _match_rule(cond: dict, *, status: int | None, body: str) -> bool:
    """Одно правило: все перечисленные условия должны выполниться (AND).

    Принимает и само правило, и его блок "if" — разбор делает
    _rule_conditions, чтобы у обоих вызывающих (api_verdict и _rule_markers)
    был один и тот же взгляд на то, где лежат условия.
    """
    cond = _rule_conditions(cond)
    if not cond:
        return False
    if "status_in" in cond:
        try:
            allowed = [int(x) for x in _as_list(cond["status_in"])]
        except (TypeError, ValueError):
            allowed = []
        if status not in allowed:
            return False
    if "body_contains" in cond:
        if not any(s in body for s in _as_list(cond["body_contains"])):
            return False
    if "body_contains_all" in cond:
        if not all(s in body for s in _as_list(cond["body_contains_all"])):
            return False
    if "body_not_contains" in cond:
        if any(s in body for s in _as_list(cond["body_not_contains"])):
            return False
    if "body_json_success" in cond:
        # Значение поля success в JSON-ответе. OpenRouter отвечает
        # {"success": false, "error": "..."} — это отказ, а не ответ сервиса.
        value = _json_field(body, "success")
        want = cond["body_json_success"]
        if isinstance(want, str):
            want = want.strip().lower() in ("1", "true", "yes")
        if value is not want:
            return False
    if "body_json_error_status" in cond:
        value = _json_field(body, "error.status")
        if str(value or "") not in _as_list(cond["body_json_error_status"]):
            return False
    return True


def api_verdict(rules: list[dict] | None, *, status: int | None, body: str,
                default: str = API_UNKNOWN) -> tuple[str, str]:
    """Прогоняет ответ через правила цели. Возвращает (вердикт, причина).

    Первое совпавшее правило задаёт вердикт — ровно как в браузерной проверке
    сервиса (browser/batch.py). Поэтому порядок правил в файле целей важен:
    ban-правила обязаны стоять раньше ok-правил, иначе «регион не
    поддерживается» с кодом 200 будет принят за рабочий сервис.
    """
    for rule in rules or []:
        # Передаётся ПРАВИЛО, а не rule["if"]: условия допустимо писать плоско,
        # в одной строке с вердиктом, и разбирает их _rule_conditions. Прежний
        # вызов брал только rule["if"], поэтому плоская запись молча не
        # совпадала НИКОГДА — цель с правилами вечно возвращала default.
        if _match_rule(rule, status=status, body=body):
            return (str(rule.get("verdict") or default),
                    str(rule.get("reason") or ""))
    return default, ""


# --- Загрузка списка целевых сайтов ------------------------------------------
def load_targets(path: str | Path | None = None) -> list[dict]:
    """Читает config/reachability_targets.json и возвращает список целевых сайтов.

    Каждая цель: {name, tag, url, max_ping_ms, timeout_ms, expected_statuses,
    headers, rules}. Пороги по умолчанию подставляются, если не заданы.

    Блок "rules" (может быть и вложен в "api") превращает цель из «дотянулся
    по задержке» в «сервис работает / отказал»: вердикт считается по статусу и
    телу ответа. Цели без правил работают как раньше — по TTFB.
    """
    path = Path(path) if path else Path(str(get_settings().paths.reachability_targets_file))
    if not path.exists():
        raise FileNotFoundError(f"Файл целевых сайтов не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(targets, list):
        raise ValueError(f"Неверный формат {path}: ожидается список 'targets'")
    out = []
    # Порог TTFB по умолчанию, если цель не задала max_ping_ms.
    default_ping = int(get_settings().reach.max_ping_ms or 500)
    for t in targets:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("tag") or "").strip()
        url = str(t.get("url") or "").strip()
        if not name or not url:
            continue
        # Блок api — короткая форма для цели-сервиса: url и правила лежат в
        # нём, а name/tag/max_ping_ms берутся снаружи, как у обычной цели.
        api = t.get("api") if isinstance(t.get("api"), dict) else {}
        if api:
            url = str(api.get("url") or url).strip()
        rules = t.get("rules") or api.get("rules") or []
        # Ключ API подставляется из окружения. Раскрываем здесь, на загрузке,
        # чтобы у цели в руках был готовый набор заголовков, а не шаблон.
        raw_headers = dict(t.get("headers") or api.get("headers") or {})
        if not raw_headers:
            raw_headers = {"User-Agent": BROWSER_UA}
        out.append({
            "name": name,
            "tag": str(t.get("tag") or name).strip(),
            "url": url,
            "max_ping_ms": int(t.get("max_ping_ms") or api.get("max_ping_ms") or default_ping),
            "timeout_ms": int(t.get("timeout_ms") or api.get("timeout_ms") or 6000),
            "expected_statuses": list(t.get("expected_statuses") or [200, 204, 301, 302]),
            "headers": expand_headers(raw_headers),
            # Список правил: цель с ним проверяется по ответу, а не по TTFB.
            "rules": [r for r in rules if isinstance(r, dict)],
            "default_verdict": str(
                t.get("default_verdict") or api.get("default_verdict") or API_UNKNOWN
            ),
        })
    return out


# --- Группы слияния тегов (блок merge) ---------------------------------------
def _merge_list(rule: dict) -> list:
    """Список целей группы: ключ list, а для совместимости ещё и tags.

    Структура записи в файле:
        {"tag": "Global", "list": ["gstatic", "yandex-portal", "cloudflare"]}
    """
    for key in ("list", "tags"):
        value = rule.get(key)
        if isinstance(value, (list, tuple)) and value:
            return list(value)
    return []


def _merge_rule(rule: dict) -> dict | None:
    """Одна запись блока merge: {tag, list, min_matches, ...} или None, если пуста.

    min_matches по умолчанию — вся длина списка (нужны все цели группы); число
    меньше длины задаёт «любые N из». remove_tags по умолчанию True: смысл
    записи в том, чтобы вместо N тегов группы в профиле остался ОДИН общий.
    """
    tag = str(rule.get("tag") or "").strip()
    tags = list(dict.fromkeys(
        str(t).strip() for t in _merge_list(rule) if str(t).strip()
    ))
    if not tag or not tags:
        return None
    raw_min = rule.get("min_matches")
    try:
        min_matches = int(raw_min) if raw_min is not None else len(tags)
    except (TypeError, ValueError):
        min_matches = len(tags)
    # Порог вне диапазона 1..len(tags) сделал бы группу либо вечной, либо
    # недостижимой: «3 из 2» не наберёт никто, «0 из 3» — любой сервер.
    min_matches = max(1, min(min_matches, len(tags)))
    return {
        "tag": tag,
        "list": tags,
        # tags — тот же список под прежним именем: на него смотрят вызовы,
        # написанные до появления ключа list.
        "tags": tags,
        "min_matches": min_matches,
        "remove_tags": bool(rule.get("remove_tags", True)),
    }


def load_merge_rules(path: str | Path | None = None) -> list[dict]:
    """Группы слияния тегов из блока merge файла reachability_targets.json.

    Структура блока::

        "merge": [
          {"tag": "Global", "list": ["gstatic", "yandex-portal", "cloudflare"]},
          {"tag": "Core",   "list": ["openai", "gemini"], "min_matches": 1}
        ]

    Сервер, прошедший min_matches целей из list, получает ОДИН общий тег tag
    вместо тегов этих целей (при remove_tags: true). Пример: три generate_204
    (gstatic, yandex-portal, cloudflare) -> [Global].

    Единственный источник групп — этот файл: настройка REACHABILITY_MERGE_TAGS
    удалена, чтобы правило не приходило из двух мест и не расходилось с
    профилем целей, который лежит рядом.

    Ошибки здесь не поднимаются: недоступный или битый файл — это отсутствие
    групп, а не падение прогона. Профиль целей грузится отдельно и о своей
    беде сообщает сам.
    """
    try:
        cfg_path = Path(path) if path else Path(
            str(get_settings().paths.reachability_targets_file)
        )
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    raw = data.get("merge") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    rules: list[dict] = []
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        parsed = _merge_rule(rule)
        # Один тег — одна группа: вторая запись с тем же tag не добавит
        # ничего, кроме путаницы в порядке применения.
        if parsed and not any(parsed["tag"] == r["tag"] for r in rules):
            rules.append(parsed)
    if not rules:
        return []
    warn_about_missing_merge_targets(rules)
    return rules


def warn_about_missing_merge_targets(rules: list[dict],
                                     targets: list[dict] | None = None) -> list[str]:
    """Логирует группы merge, ссылающиеся на цели, которых в профиле нет.

    Опечатка в имени тега не ломает прогон, но делает группу недостижимой:
    min_matches из трёх тегов не наберётся, если один из них не проверяется.
    Возвращает имена отсутствующих тегов — ради этого списка функция и
    вызывается повторно из чекера, когда цели уже загружены.
    """
    if not rules:
        return []
    if targets is None:
        try:
            targets = load_targets()
        except Exception:  # noqa: BLE001 — предупреждение не стоит падения
            return []
    known = {
        str(t.get("tag") or t.get("name") or "").strip().lower() for t in targets
    }
    missing = sorted({
        tag for rule in rules for tag in rule["tags"] if tag.lower() not in known
    })
    if missing:
        LOGGER.warning(
            "Merge: теги %s перечислены в группах, но таких целей нет в профиле — "
            "группа останется недостижимой", ", ".join(missing),
        )
    return missing


# --- Кэш в рамках одного запуска --------------------------------------------
_profile_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


# --- Разбор прокси и сборка батч-конфига (как в country_check) --------------
# --- Reachability-проба конкретного порта по всем целям ----------------------
def _drain_body(resp, *, max_bytes: int, max_seconds: float,
                keep: bool = False, markers: list[str] | None = None):
    """Дочитывает первые max_bytes тела и обрывает его.

    Возвращает число байт, а при keep=True — (число байт, текст): текст нужен
    целям с правилами, где вердикт считается по ТЕЛУ ответа. Обычным целям
    тело не нужно вовсе, и они получают прежний int — декодировать мегабайты
    HTML ради числа байт было бы чистой тратой.

    Тот же приём, что _drain в script/speed_check.py: тело читается порциями
    и закрывается на первом же превышении, поэтому страница не уезжает целиком
    (gemini.google.com один занимал 856 КБ на каждый прокси). Ошибки чтения
    здесь не важны — TTFB уже измерен по заголовкам.

    markers — подстроки правил: как только все они найдены, чтение
    прекращается. Маркеры лежат в первых килобайтах, а таймаут чтения
    ограничен, поэтому без этой остановки медленный, но рабочий сервис
    терял бы вердикт по таймауту.
    """
    total = 0
    started = time.monotonic()
    chunks: list[bytes] = [] if keep else None
    pending = list(markers or [])
    try:
        for chunk in resp.iter_content(16384):
            if not chunk:
                continue
            total += len(chunk)
            if keep:
                chunks.append(chunk)
                if pending:
                    # Ищем в прочитанном с запасом на границу чанка: маркер,
                    # разорванный между порциями, иначе не найдётся.
                    text = b"".join(chunks).decode("utf-8", "replace")
                    pending = [m for m in pending if m not in text]
                    if not pending:
                        break
            if total >= max_bytes or (time.monotonic() - started) >= max_seconds:
                break
    except Exception:  # noqa: BLE001 — обрезанное тело лучше отсутствия ответа
        pass
    if not keep:
        return total
    return total, b"".join(chunks).decode("utf-8", "replace")


def _probe_target(port: int, target: dict, *, max_ping_ms: int | None = None) -> dict:
    """Одна проба «прокси -> целевой сайт». Меряет TTFB, а не время загрузки.

    stream=True отдаёт управление, как только пришли заголовки ответа, — это и
    есть задержка до целевого сайта. Дальше тело добирается порциями и
    сразу обрывается. Сравнивается с порогом именно TTFB: время выкачивания
    страницы к пингу отношения не имеет.

    Цель с блоком правил проверяется иначе: ok ставится по ВЕРДИКТУ правил
    (статус + тело), а не по порогу задержки. Порог у неё остаётся только
    справочной величиной — сервис, ответивший «регион не поддерживается» за
    300 мс, порог прошёл, а работать от этого не стал.
    """
    connect_to = PROBE_CONNECT_TIMEOUT
    # Таймаут READ одного целевого запроса, сек (REACHABILITY_TIMEOUT).
    read_to = float(get_settings().reach.timeout or 6.0)
    tm = target.get("timeout_ms")
    if tm:
        read_to = max(0.5, tm / 1000.0)
    # Тело ответа нужно только чтобы убедиться, что соединение живое: читаем
    # первые ~64 КБ и обрываем (тот же приём, что _drain в script/speed_check.py).
    body_max_bytes = int(get_settings().reach.body_bytes or 65536)
    body_max_seconds = float(get_settings().reach.body_seconds or 2.0)
    # Порог TTFB по умолчанию, если не заданы ни явный порог, ни порог цели.
    default_ping = int(get_settings().reach.max_ping_ms or 500)
    # Явный max_ping_ms (настройка конвейера) важнее порога цели; если его
    # нет — берём max_ping_ms цели, иначе REACHABILITY_MAX_PING_MS.
    threshold = int(max_ping_ms or target.get("max_ping_ms") or default_ping)

    rules = target.get("rules") or []
    start = time.monotonic()
    entry = {
        "ok": False,
        "status": None,
        "latency_ms": None,   # синоним ttfb_ms, оставлен для совместимости
        "ttfb_ms": None,
        "body_bytes": 0,
        "max_ping_ms": threshold,
        "error": None,
    }
    if rules:
        # Поля вердикта появляются только у целей-сервисов: у обычных целей
        # их нет, и по наличию "verdict" вызывающий код отличает один режим
        # от другого.
        entry["verdict"] = API_UNKNOWN
        entry["reason"] = ""
    proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
    session = requests.Session()
    session.trust_env = False  # игнорировать системные HTTP(S)_PROXY
    resp = None
    try:
        resp = session.get(
            target["url"],
            proxies=proxies,
            timeout=(connect_to, read_to),
            headers=target.get("headers") or {"User-Agent": BROWSER_UA},
            allow_redirects=True,
            stream=True,
        )
        # Заголовки получены — это и есть TTFB (включая редиректы).
        ttfb_ms = round((time.monotonic() - start) * 1000.0, 1)
        entry["status"] = resp.status_code
        entry["ttfb_ms"] = ttfb_ms
        entry["latency_ms"] = ttfb_ms
        if rules:
            # Тело читается ради вердикта: сигнал отказа лежит ИМЕННО в нём
            # ("User location is not supported", {"success": false}), а статус
            # у него бывает и 200, и 400, и 403.
            entry["body_bytes"], body = _drain_body(
                resp,
                max_bytes=body_max_bytes,
                max_seconds=min(body_max_seconds, read_to),
                keep=True,
                markers=_rule_markers(rules),
            )
            verdict, reason = api_verdict(
                rules, status=resp.status_code, body=body,
                default=str(target.get("default_verdict") or API_UNKNOWN),
            )
            entry["verdict"] = verdict
            entry["reason"] = reason
            # Ответ получен (заголовки пришли) — значит сервис доступен С
            # ЭТОГО адреса. Отказ по региону это ban, а не «не дотянулся»:
            # прокси жив, через него не работает КОНКРЕТНЫЙ сервис.
            entry["ok"] = verdict == API_OK
        else:
            entry["body_bytes"] = _drain_body(
                resp,
                max_bytes=body_max_bytes,
                max_seconds=min(body_max_seconds, read_to),
            )
            # "дозвонился" = получил любой HTTP-статус (в т.ч. 403/429/404 — это
            # отказ ПОСЛЕ прохода через прокси, сам прокси жив) И уложился в порог.
            entry["ok"] = ttfb_ms <= threshold
    except Exception as exc:  # noqa: BLE001
        entry["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        session.close()
    return entry


def _probe_port(port: int, targets: list[dict], *,
                max_ping_ms: int | None = None) -> dict:
    """Проверяет доступность целевых сайтов через локальный порт-прокси.

    Возвращает {target_name: {"ok", "status", "ttfb_ms"/"latency_ms",
    "body_bytes", "error"}}. Сервер считается достижившим цель, если пришли
    заголовки ответа (нет connect/read-сбоя) И TTFB <= порога.
    """
    return {t["name"]: _probe_target(port, t, max_ping_ms=max_ping_ms) for t in targets}


def _probe_single(port: int, target: dict, *,
                  max_ping_ms: int | None = None,
                  gate: threading.Semaphore | None = None) -> tuple[str, dict]:
    """Одна проба: один прокси — одна цель.

    Раньше все цели обходились последовательно внутри одного прокси, и батч
    из двух серверов с восемью целями растягивался на 80+ секунд: каждая цель
    ждала своего connect+read таймаута по очереди. Теперь цели независимы и
    уходят в общий пул потоков.

    gate — семафор на конкретный прокси: не даёт нескольким целям одновременно
    открывать соединения через один узел, иначе в замер TTFB попадает очередь
    внутри sing-box (см. REACHABILITY_PER_PROXY_CONCURRENCY).
    """
    if gate is None:
        return target["name"], _probe_target(port, target, max_ping_ms=max_ping_ms)
    with gate:
        return target["name"], _probe_target(port, target, max_ping_ms=max_ping_ms)


# --- Батч-исполнение ---------------------------------------------------------
def _run_batch(items: list[tuple[str, int]], results: dict[str, dict],
               targets: list[dict], concurrency: int, depth: int = 0,
               max_ping_ms: int | None = None) -> None:
    """items: [(line, index)] — проверяет батч одним процессом sing-box.

    При инфраструктурном сбое старта делит батч пополам (рекурсия).
    """
    if not items:
        return

    used_tags: set[str] = set()
    entries: list[dict] = []
    for line, idx in items:
        outbound, err = parse_outbound(line, idx, used_tags)
        if err:
            results[line] = {"error": err, "targets": {}}
            continue
        entries.append({"index": idx, "port": 0, "outbound": outbound})

    active = [e for e in entries]
    if not active:
        return

    ports = reserve_ports(len(active))
    for e, p in zip(active, ports):
        e["port"] = p

    config = build_batch_config(active, ROOT / str(get_settings().paths.countrytest_template))
    config_path = PROJECT_ROOT / "source" / "tests" / f"reach_batch_{items[0][1]}_{len(items)}.json"
    proc = start_singbox(config, config_path)

    started_ok = False
    if proc is not None and proc.poll() is None:
        started_ok = ports_ready([e["port"] for e in active], STARTUP_WAIT_SECONDS)

    if not started_ok:
        stop_singbox(proc)
        release_ports(ports)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass
        if len(items) > 1 and depth < 8:
            mid = len(items) // 2
            LOGGER.warning(
                "Reach батч из %d прокси не стартовал, делю пополам (%d + %d)",
                len(items), mid, len(items) - mid,
            )
            _run_batch(items[:mid], results, targets, concurrency, depth + 1, max_ping_ms)
            _run_batch(items[mid:], results, targets, concurrency, depth + 1, max_ping_ms)
        else:
            for line, _ in items:
                if line not in results:
                    results[line] = {"error": "sing-box не смог обработать этот конфиг", "targets": {}}
        return

    try:
        port_by_index = {e["index"]: e["port"] for e in active}
        pairs = [(line, port_by_index[idx]) for line, idx in items if idx in port_by_index]

        # Параллелим сразу по двум осям: прокси И цели. Иначе восемь целей
        # шли последовательно и батч из двух серверов занимал больше минуты.
        # Но через ОДИН прокси цели ждут друг друга (gate): иначе все восемь
        # соединений встают в очередь внутри sing-box и TTFB выходит в разы
        # больше настоящей задержки до сайта.
        # Сколько целей одновременно опрашиваем через один и тот же прокси
        # (REACHABILITY_PER_PROXY_CONCURRENCY). Каждая проба открывает НОВОЕ
        # соединение с узлом (TCP+TLS внутри sing-box), поэтому при
        # одновременных 8 пробах они стоят в очереди друг за другом и в
        # измеренный TTFB попадает время ожидания в этой очереди: замер в 3-6
        # раз больше настоящей задержки до сайта, и ни один живой сервер не
        # проходит порог. По умолчанию — одна цель за раз на прокси, а
        # параллелизм остаётся между прокси.
        per_proxy = max(1, int(get_settings().reach.per_proxy_concurrency or 1))
        gates = {e["port"]: threading.Semaphore(per_proxy) for e in active}
        jobs = [(line, port, target) for line, port in pairs for target in targets]
        # Потоков нужно ровно столько, сколько одновременных соединений держит
        # sing-box: по одному на прокси (или столько, сколько разрешает
        # REACHABILITY_PER_PROXY_CONCURRENCY). Больше не нужно: остальное всё
        # равно упрётся в gate своего прокси.
        workers = max(1, min(
            len(jobs), max(concurrency, len(pairs) * per_proxy)
        ))
        lock = threading.Lock()
        for line, _ in pairs:
            results.setdefault(line, {"error": None, "targets": {}})
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _probe_single, port, target,
                    max_ping_ms=max_ping_ms, gate=gates.get(port),
                ): (line, target["name"])
                for line, port, target in jobs
            }
            for fut in as_completed(futures):
                line, _expected = futures[fut]
                name, entry = fut.result()
                with lock:
                    results[line]["targets"][name] = entry
    finally:
        stop_singbox(proc)
        release_ports(ports)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass


def batch_reachability_check(proxy_lines: list[str], *,
                             concurrency: int | None = None,
                             batch_size: int = MAX_BATCH_INBOUNDS,
                             targets: list[dict] | None = None,
                             target_names: list[str] | None = None,
                             max_ping_ms: int | None = None) -> dict[str, dict]:
    """Проверяет доступность до целевых сайтов для списка прокси.

    targets — список целей; если None (или пусто), берётся список из
    config/reachability_targets.json. target_names — фильтр по именам.
    max_ping_ms — порог TTFB в мс: None = у каждой цели свой max_ping_ms,
    а где его нет — REACHABILITY_MAX_PING_MS из config/settings.py; число = жёсткое
    переопределение порога для всех целей.

    Возвращает {proxy_line: {"error": str|None, "targets": {name: {ok, status,
    ttfb_ms/latency_ms, body_bytes, error}}}}.
    """
    if concurrency is None:
        concurrency = get_settings().reach.concurrency
    concurrency = max(1, int(concurrency))

    all_targets = targets if targets is not None else load_targets()
    if target_names:
        wanted = set(target_names)
        all_targets = [t for t in all_targets if t["name"] in wanted]

    results: dict[str, dict] = {}
    pending: list[str] = []
    seen_keys: set[str] = set()

    with _cache_lock:
        for raw in proxy_lines:
            line = str(raw).strip()
            if not line:
                continue
            key = normalize_key(line)
            if key in _profile_cache:
                cached = _profile_cache[key]
                results[line] = dict(cached) if cached else {"error": "кэш запуска", "targets": {}}
                continue
            seen_keys.add(key)
            pending.append(line)

    if not pending:
        return results

    indexed = [(line, i) for i, line in enumerate(pending)]
    total_batches = (len(indexed) + batch_size - 1) // batch_size
    for bi, start in enumerate(range(0, len(indexed), batch_size)):
        chunk = indexed[start:start + batch_size]
        LOGGER.info(
            "Reach batch %d/%d: %d прокси, %d целей (concurrency=%d)",
            bi + 1, total_batches, len(chunk), len(all_targets), concurrency,
        )
        batch_started = time.monotonic()
        _run_batch(chunk, results, all_targets, concurrency, 0, max_ping_ms)
        LOGGER.info(
            "Reach batch %d/%d завершён за %.2fs", bi + 1, total_batches,
            time.monotonic() - batch_started,
        )
    _log_probe_stats(results, all_targets, max_ping_ms)

    with _cache_lock:
        for line in pending:
            res = results.get(line)
            key = normalize_key(line)
            if res and res.get("targets"):
                _profile_cache[key] = dict(res)

    return results


def _log_probe_stats(results: dict[str, dict], targets: list[dict],
                     max_ping_ms: int | None = None) -> None:
    """Итог по пробам: сколько целей пройдено по TTFB и сколько байт скачано.

    Тело обрывается через REACHABILITY_BODY_BYTES, поэтому трафик на сервер
    урезан в разы по сравнению с полной выкачкой страниц.
    """
    ok_by_target = {t["name"]: 0 for t in targets}
    body_bytes = 0
    for res in results.values():
        for name, entry in (res.get("targets") or {}).items():
            if name not in ok_by_target:
                continue
            if entry.get("ok"):
                ok_by_target[name] += 1
            body_bytes += int(entry.get("body_bytes") or 0)
    default_ping = (
        max_ping_ms if max_ping_ms is not None
        else int(get_settings().reach.max_ping_ms or 500)
    )
    detail = ", ".join(
        f"{name}={ok_by_target[name]}/{len(results)} (порог "
        f"{next((t.get('max_ping_ms') for t in targets if t['name'] == name), default_ping)}мс)"
        for name in ok_by_target
    )
    LOGGER.info(
        "Reach итог: %d/%d серверов с тэгами; тела прочитано %.1f КБ "
        "(порог по умолчанию %dмс) | %s",
        sum(1 for r_ in results.values()
            if r_.get("targets") and any(e.get("ok") for e in r_["targets"].values())),
        len(results),
        body_bytes / 1024.0,
        default_ping,
        detail,
    )


def check_reachability(proxy_line: str, *, targets: list[dict] | None = None,
                       target_names: list[str] | None = None,
                       max_ping_ms: int | None = None) -> dict:
    """Совместимость: профиль одного прокси (обёртка над батчем из 1)."""
    res_map = batch_reachability_check(
        [proxy_line], targets=targets, target_names=target_names,
        max_ping_ms=max_ping_ms,
    )
    return res_map.get(proxy_line.strip()) or {"error": "нет результата", "targets": {}}


def _reached_tags(targets: list[dict], reach_result: dict[str, dict]) -> list[str]:
    """Теги целей, которые прокси прошёл. Порядок — как в списке целей.

    Тег, совпавший с уже добавленным, пропускается: два имени цели могут
    указывать на один URL и один тег, и профиль тогда молча терял бы часть
    совпадений группы merge (порог «3 из 3» не набрался бы у сервера,
    прошедшего все три URL, — просто потому что целей с таким тегом две).
    """
    out: list[str] = []
    for t in targets:
        if not (reach_result.get(t["name"]) or {}).get("ok"):
            continue
        tag = str(t.get("tag") or t["name"])
        if tag not in out:
            out.append(tag)
    return out


def service_pings(targets: list[dict], reach_result: dict[str, dict],
                  merge_rules: list[dict] | None = None) -> dict[str, int]:
    """Задержка до каждой ДОСТИГНУТОЙ цели: {"youtube": 200, "google": 100}.

    Профиль (список тегов) говорит только «цель достигнута». Для отбора
    лучших нужна сама цифра: у двух серверов с тегом [youtube] задержки
    различаются в десятки раз, и по тегу этого не видно. Раньше TTFB
    выбрасывался сразу после сравнения с порогом.

    Пишутся только УСПЕШНЫЕ пробы: задержка неудачной — это время ожидания
    таймаута (секунды), а не отклик сервиса. Попади она в базу, «лучшим»
    стал бы тот, кто отвечает медленнее всех.

    Замеры групп merge пишутся ПОД ИМЕНЕМ ГРУППЫ, а не её целей: для отбора
    внутри группы [Global] нужен один признак, а теги её целей в профиле
    свёрнуты, и отдельного «gstatic-best» в конфиге не будет. Берётся
    лучший (минимальный) замер группы: сервер отзывчив к группе, если
    хотя бы одна её цель отвечает быстро.
    """
    if merge_rules is None:
        merge_rules = load_merge_rules()
    out: dict[str, int] = {}
    for t in targets:
        entry = reach_result.get(t["name"]) or {}
        # Замер пишется только по УСПЕШНОЙ пробе. Для цели-сервиса успех — это
        # вердикт ok: замер пробы, закончившейся баном, в рейтинг «лучших» не
        # годится, там нечего ранжировать.
        if not entry.get("ok"):
            continue
        ttfb = entry.get("ttfb_ms")
        if ttfb is None:
            ttfb = entry.get("latency_ms")
        try:
            ms = int(round(float(ttfb)))
        except (TypeError, ValueError):
            continue
        if ms < 0:
            continue
        tag = str(t.get("tag") or t["name"])
        # Замер пишется И ПОД ТЕГОМ ЦЕЛИ, и (для целей группы merge) под
        # именем группы — оба нужны, и вот почему.
        #
        # Группа merge сворачивает теги ТОЛЬКО когда набран её порог
        # (3 из 3): сервер, прошедший одну cloudflare, сохраняет тег
        # [cloudflare]. Если писать замер лишь под именем группы, такой
        # сервер получит {"Global": 250} при профиле "cloudflare" — и отбор
        # не найдёт замера для группы [cloudflare] никогда. На боевой базе
        # это выглядело так: cloudflare есть у 36 серверов, а
        # [cloudflare-best] не появляется ни у одного.
        prev = out.get(tag)
        if prev is None or ms < prev:
            out[tag] = ms
        # Плюс имя группы: у сервера с профилем "Global" теги её целей
        # свёрнуты, отдельного [cloudflare] в конфиге нет, и ранжировать
        # группу [Global] было бы не по чему.
        for rule in merge_rules or []:
            names = {str(n).lower() for n in rule["tags"]}
            if tag.lower() not in names:
                continue
            group_tag = rule["tag"]
            prev_group = out.get(group_tag)
            if prev_group is None or ms < prev_group:
                out[group_tag] = ms
            break
    return out


def profile_merge_tags(targets: list[dict], reach_result: dict[str, dict],
                       merge_rules: list[dict] | None = None) -> list[str]:
    """Общие теги групп, набранных прокси (например ["Global"]).

    Порядок — как в списке групп; первое правило, забравшее тег, выигрывает.
    """
    if merge_rules is None:
        merge_rules = load_merge_rules()
    reached = _reached_tags(targets, reach_result)
    lowered = {tag.lower() for tag in reached}
    matched: list[str] = []
    claimed: set[str] = set()
    for rule in merge_rules or []:
        tag = rule["tag"]
        if tag in claimed:
            continue
        # Цель группы засчитывается только если её тег вообще проверялся:
        # иначе опечатка в merge давала бы совпадение «всех» тегов на пустом месте.
        hits = [t for t in rule["tags"] if t.lower() in lowered]
        if len(hits) >= rule["min_matches"]:
            matched.append(tag)
            claimed.add(tag)
    return matched


def profile_to_tags(targets: list[dict], reach_result: dict[str, dict],
                    merge_rules: list[dict] | None = None) -> list[str]:
    """Превращает результат пробы в упорядоченный список тэгов [name].

    Тэг добавляется для каждой цели, где ok=True. Порядок — как в списке целей.

    Группы merge сворачивают свои теги в один общий: сервер, прошедший три
    generate_204, получает один тэг Global вместо трёх тегов этих целей
    (у группы remove_tags=True). Теги остальных целей остаются на месте.
    """
    if merge_rules is None:
        merge_rules = load_merge_rules()
    reached = _reached_tags(targets, reach_result)
    merged: list[str] = []
    hidden: set[str] = set()
    # Регистр имени группы и её тегов: сервер, прошедший цель yandex-portal,
    # не должен получать сворачивание группы ["yandex-portal"].
    lowered = {tag.lower() for tag in reached}
    for rule in merge_rules or []:
        tag = rule["tag"]
        hits = [t for t in rule["tags"] if t.lower() in lowered]
        if len(hits) < rule["min_matches"]:
            continue
        # Тег группы может быть и именем самой цели: тогда сворачивать нечего.
        if rule["remove_tags"]:
            hidden.update(t.lower() for t in rule["tags"] if t != tag)
        if tag not in merged:
            merged.append(tag)
    out = merged + [
        tag for tag in reached
        if tag.lower() not in hidden and tag not in merged
    ]
    return list(dict.fromkeys(out))


def service_verdicts(targets: list[dict], reach_result: dict[str, dict]) -> dict[str, dict]:
    """Вердикты целей-сервисов: {"gemini": {verdict, reason, status, ttfb_ms}}.

    Отдаются ТОЛЬКО цели с правилами (блок rules/api) — обычные цели профиля
    сюда не попадают, у них нет вердикта, есть только «дотянулся».

    Эти вердикты идут в бренч чекера-дополнения, оттуда в service_bans и в
    service_pings — ровно туда же, куда пишет браузерная проверка. Это и есть
    «одна логика»: словарь вердиктов (ok/ban/unknown) общий, отличаются только
    транспорт пробы и таблица целей.
    """
    out: dict[str, dict] = {}
    for t in targets:
        if not t.get("rules"):
            continue
        entry = reach_result.get(t["name"]) or {}
        out[str(t["name"])] = {
            "verdict": str(entry.get("verdict") or API_UNKNOWN),
            "reason": str(entry.get("reason") or ""),
            "status": entry.get("status"),
            "ttfb_ms": entry.get("ttfb_ms") if entry.get("ttfb_ms") is not None
            else entry.get("latency_ms"),
            "error": entry.get("error"),
            # Дошёл ли запрос вообще. Нужно, чтобы отличить «сервис отказал»
            # от «запрос не ушёл»: у первой причины ответ есть, у второй нет.
            "answered": entry.get("status") is not None,
        }
    return out


def profile_is_global(targets: list[dict], reach_result: dict[str, dict],
                      merge_rules: list[dict] | None = None,
                      global_tag: str | None = None) -> bool:
    """True, если прокси набрал группу с тегом Global (кандидат на [Global]).

    Раньше здесь было «достиг ВСЕХ целей списка»: порог задавала не цель, а
    весь профиль, и стоило добавить девятую цель — метка [Global] пропадала
    у всех. Теперь порог берётся из группы merge (три generate_204), а
    остальные цели на неё не влияют.
    """
    if global_tag is None:
        global_tag = str(getattr(get_settings().reach, "global_tag", "") or "Global")
    return global_tag in profile_merge_tags(targets, reach_result, merge_rules)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Reachability profile for proxy lines")
    parser.add_argument("lines", nargs="*", help="proxy lines (or read stdin)")
    parser.add_argument("--targets", default=None, help="comma-separated target names to probe")
    args = parser.parse_args()

    if not args.lines:
        data = sys.stdin.read()
        args.lines = [l for l in data.splitlines() if l.strip()]

    if len(sys.argv) < 2 and not args.lines:
        print("Использование: python -m script.reachability_check '<proxy_line>'")
        sys.exit(1)

    name_filter = [s.strip() for s in (args.targets or "").split(",") if s.strip()] or None
    for line in args.lines:
        res = check_reachability(line, target_names=name_filter)
        print("===== " + (line[:80]) + " =====")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("targets") and not res.get("error"):
            all_targets = load_targets()
            merge_rules = load_merge_rules()
            reached = profile_to_tags(all_targets, res["targets"], merge_rules)
            print("Тэги: " + (", ".join(f"[{t}]" for t in reached) or "(нет)"))
            for name, entry in res["targets"].items():
                print(
                    f"  {name}: ttfb={entry.get('ttfb_ms')}мс "
                    f"(порог {entry.get('max_ping_ms')}мс, статус {entry.get('status')}, "
                    f"тело {entry.get('body_bytes')}Б)"
                    + (f" ошибка: {entry['error']}" if entry.get("error") else "")
                )
            for rule in merge_rules:
                hits = [t for t in rule["tags"]
                        if (res["targets"].get(t) or {}).get("ok")]
                mark = "=>" if len(hits) >= rule["min_matches"] else "  "
                print(
                    f"{mark} [{rule['tag']}]: {len(hits)}/{rule['min_matches']} "
                    f"из {list(rule['tags'])}"
                )