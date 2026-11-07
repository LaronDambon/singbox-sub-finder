"""Проверка доступности сервисов по правилам (service check).

По аналогии с reachability_check: один процесс sing-box на батч прокси
(N mixed-inbound на 127.0.0.1, каждый жёстко привязан к своему outbound),
параллельные HTTP-запросы через локальные порты. Но вместо профиля целей
здесь каждый СЕРВИС проверяется по своим ПРАВИЛАМ из
config/service_checks.json, и выносится вердикт:

  ok      - сервис отвечает приложением (открывается с этого exit-IP);
  ban     - сервис явно отказал в доступе по региону;
  unknown - не удалось определить (не совпало ни одно правило, сеть/ошибка).

Результат по паре (exit-IP, сервис) пишется в таблицу service_bans базы
servers.db: exit_ip, service, ban (1/0/NULL), плюс текст причины и время.

Зачем пара (exit-IP, сервис), а не (прокси, сервис): у разных прокси бывает
общий выходной IP (один VPN-сервер, несколько конфигов) — бан выдаётся по IP,
и знание «этот IP забанен gemini» позволяет отсеять все конфиги с тем же
выходом, не проверяя каждый заново.

Как добавить новый сервис: запись в config/service_checks.json (см. комментарий
в файле). Код здесь знает только механику HTTP и общий формат правил — сами
сигнатуры (страницы-заглушки, редиректы) живут целиком в конфиге.

CLI:
  python -m script.service_check '<proxy_line>'
  python -m script.service_check '<line1>' '<line2>' ...
"""

from __future__ import annotations

import json
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
from script.server_store import ServerStore

LOGGER = get_project_logger("service_check")

# Ожидание готовности inbound-портов при старте sing-box, сек.
STARTUP_WAIT_SECONDS = 10.0
# Максимум inbound'ов (и прокси) в одном процессе sing-box.
MAX_BATCH_INBOUNDS = 100

VERDICT_OK = "ok"
VERDICT_BAN = "ban"
VERDICT_UNKNOWN = "unknown"


# --- Загрузка конфига сервисов ------------------------------------------------
def load_services(path: str | Path | None = None) -> dict:
    """Читает config/service_checks.json: defaults + список сервисов.

    Поля каждого сервиса подставляются из defaults, если не заданы.
    """
    path = Path(path) if path else ROOT / "config" / "service_checks.json"
    if not path.exists():
        raise FileNotFoundError(f"Файл правил сервисов не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    services = []
    for s in data.get("services") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        url = str(s.get("url") or "").strip()
        if not name or not url:
            continue
        # defaults можно переопределить и на уровне сервиса (headers и т.п.)
        merged_defaults = dict(defaults)
        merged_defaults.update(s.get("defaults") or {})
        services.append({
            "name": name,
            "title": str(s.get("title") or name),
            "url": url,
            "rules": list(s.get("rules") or []),
            "default_verdict": str(s.get("default_verdict") or VERDICT_UNKNOWN),
            "defaults": merged_defaults,
        })
    return {"defaults": defaults, "services": services}


# --- Пул из базы и пропуск забаненных -----------------------------------------
def _banned_keys_per_service(store: ServerStore | None = None) -> dict[str, set[str]]:
    """Карта {service: {proxy_key}} — прокси, уже забаненные этим сервисом.

    Читается из service_bans. Используется ПЕРЕД проверкой: чтобы «не гонять
    мусор», не тратить inbound-порт и запрос на прокси, который этим сервисом
    уже отвергнут.
    """
    store = store or ServerStore(get_settings().paths.servers_db_file)
    out: dict[str, set[str]] = {}
    for service in load_services()["services"]:
        name = service["name"]
        keys = store.banned_proxy_keys(name)
        if keys:
            out[name] = keys
    return out


def db_pool_lines(store: ServerStore, services: list[str]) -> list[str]:
    """Пул проверки из базы (check_pool) — whitelist-подход.

    Берём только ЖИВЫЕ серверы (excluded=0, stable >= порога), отсекаем
    прокси, которые забанены ВСЕМИ запрошенными сервисами (их проверять
    бессмысленно — по каждому сервису вердикт уже известен). Прокси, забаненные
    частью сервисов, остаются: по ним проверяются только небаненные сервисы.
    """
    lines = [row["line"] for row in store.check_pool()]
    if not services:
        return lines
    banned_map = _banned_keys_per_service(store)
    wanted = set(services)
    def _banned_everywhere(line: str) -> bool:
        key = normalize_key(line)
        return wanted and all(key in banned_map.get(s, set()) for s in wanted)
    return [l for l in lines if not _banned_everywhere(l)]


# --- Применение правил --------------------------------------------------------
def _as_list(value) -> list[str]:
    """Подстрока или список альтернатив -> список."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _match_condition(cond: dict, *, status: int | None,
                     final_url: str, body: str) -> bool:
    """Одно условие 'if'. Все перечисленные ключи должны выполниться (AND)."""
    if "status_in" in cond:
        if status not in [int(x) for x in _as_list(cond["status_in"])]:
            return False
    if "final_url_contains" in cond:
        if not any(s in final_url for s in _as_list(cond["final_url_contains"])):
            return False
    if "final_url_not_contains" in cond:
        if any(s in final_url for s in _as_list(cond["final_url_not_contains"])):
            return False
    if "body_contains" in cond:
        if not any(s in body for s in _as_list(cond["body_contains"])):
            return False
    if "body_not_contains" in cond:
        if any(s in body for s in _as_list(cond["body_not_contains"])):
            return False
    return True


def apply_rules(rules: list[dict], *, status: int | None,
                final_url: str, body: str,
                default_verdict: str = VERDICT_UNKNOWN) -> tuple[str, str]:
    """Прогоняет ответ через список правил. Возвращает (verdict, reason).

    Первое совпавшее правило определяет вердикт. Важно: ban-правила должны
    идти раньше ok-правил — на странице-заглушке gemini есть и маркер
    приложения (BardChatUi), и маркер заглушки (RegionBoundary).
    """
    for rule in rules:
        cond = rule.get("if") or {}
        if isinstance(cond, dict) and _match_condition(
            cond, status=status, final_url=final_url, body=body,
        ):
            return str(rule.get("verdict") or VERDICT_UNKNOWN), str(
                rule.get("reason") or ""
            )
    return default_verdict, ""


# --- Проба одного сервиса через один порт -------------------------------------
def _rule_markers(rules: list[dict]) -> list[str]:
    """Все подстроки, которые правила ищут в теле (body_contains/not_contains).

    По ним решается, когда чтение тела можно прекратить: если все маркеры
    уже найдены (или один из запрещённых найден), вердикт дальше не
    изменится, и качать остаток страницы бессмысленно.
    """
    markers: list[str] = []
    for rule in rules:
        cond = rule.get("if") or {}
        if not isinstance(cond, dict):
            continue
        for key in ("body_contains", "body_not_contains"):
            for value in _as_list(cond.get(key)):
                if value and value not in markers:
                    markers.append(value)
    return markers


def _drain_body(resp, *, max_bytes: int, max_seconds: float,
                markers: "list[str] | None" = None) -> str:
    """Дочитывает тело до max_bytes и обрывает. Возвращает текст.

    Тело ИНТЕРЕСНО только ради маркеров правил, а они лежат в первых
    килобайтах: у gemini "BardChatUi" встречается уже на ~4 КБ при странице
    в 500 КБ. Поэтому чтение прекращается, как только все маркеры найдены —
    качать остаток и тратить на него таймаут незачем.

    Замерено на живых прокси: TTFB страницы gemini доходит до 17.7 с, и при
    бюджете запроса в 15 с сервер, который на самом деле ОТКРЫТ, получал
    таймаут и терял метку. Ограничение чтения первыми килобайтами убирает
    эту потерю: маркер виден задолго до истечения бюджета.
    """
    chunks: list[bytes] = []
    total = 0
    started = time.monotonic()
    pending = list(markers or [])
    try:
        for chunk in resp.iter_content(16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if pending:
                # Текст ищем в уже прочитанном с запасом на границу чанка,
                # иначе маркер, разорванный между чанками, не найдётся.
                text = b"".join(chunks).decode("utf-8", "replace")
                pending = [m for m in pending if m not in text]
                if not pending:
                    break
            if total >= max_bytes or (time.monotonic() - started) >= max_seconds:
                break
    except Exception:  # noqa: BLE001 — обрезанное тело лучше отсутствия
        pass
    return b"".join(chunks).decode("utf-8", "replace")


def _probe_service(port: int, service: dict) -> dict:
    """Одна проба «прокси-порт -> сервис». Возвращает результат-словарь."""
    defaults = service.get("defaults") or {}
    timeout_ms = int(defaults.get("timeout_ms") or 15000)
    max_bytes = int(defaults.get("max_body_bytes") or 2_000_000)
    drain_ms = int(defaults.get("drain_timeout_ms") or 25000)
    connect_ms = int(defaults.get("connect_timeout_ms") or 8000)
    headers = dict(defaults.get("headers") or {})
    proxies = {"http": f"http://127.0.0.1:{port}",
               "https": f"http://127.0.0.1:{port}"}
    out = {
        "service": service["name"],
        "status": None,
        "final_url": "",
        "verdict": VERDICT_UNKNOWN,
        "reason": "",
        "error": None,
    }
    try:
        # Таймауты раздельные: connect — короткий (до узла), read — по бюджету
        # сервиса. Раньше ОДНО число шло и туда, и туда, поэтому медленный, но
        # живой сервер (TTFB до 17.7 с у gemini) отваливался по таймауту.
        resp = requests.get(
            service["url"], proxies=proxies,
            timeout=(connect_ms / 1000.0, timeout_ms / 1000.0),
            headers=headers, allow_redirects=True, stream=True,
        )
        out["status"] = resp.status_code
        out["final_url"] = str(resp.url)
        body = _drain_body(resp, max_bytes=max_bytes,
                           max_seconds=drain_ms / 1000.0,
                           markers=_rule_markers(service["rules"]))
        resp.close()
        verdict, reason = apply_rules(
            service["rules"], status=out["status"],
            final_url=out["final_url"], body=body,
            default_verdict=service.get("default_verdict", VERDICT_UNKNOWN),
        )
        out["verdict"] = verdict
        out["reason"] = reason
    except Exception as exc:  # noqa: BLE001 — ошибка сети это unknown, не падение
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def _resolve_exit_ip(port: int, defaults: dict) -> str | None:
    """Выходной IP прокси через ip-api. None — не удалось узнать.

    HTTP, а не HTTPS: у ip-api нет сертификата для прямого домена, а нам
    важен только адрес, не защита канала.
    """
    url = str(defaults.get("exit_ip_url")
              or "http://ip-api.com/json/?fields=query")
    timeout_ms = int(defaults.get("exit_ip_timeout_ms") or 8000)
    proxies = {"http": f"http://127.0.0.1:{port}",
               "https": f"http://127.0.0.1:{port}"}
    try:
        resp = requests.get(url, proxies=proxies, timeout=timeout_ms / 1000.0)
        ip = str(resp.json().get("query") or "").strip()
        return ip or None
    except Exception:  # noqa: BLE001
        return None


# --- Батч-прогон ---------------------------------------------------------------
def _run_batch(items: list[tuple[str, int]], results: dict[str, dict],
               services: list[dict], defaults: dict, *, concurrency: int, depth: int = 0,
               banned_keys: dict[str, set[str]] | None = None) -> None:
    """items: [(line, index)] — проверяет батч одним процессом sing-box.

    При инфраструктурном сбое старта делит батч пополам (рекурсия) —
    тот же приём, что в reachability_check.
    """
    if not items:
        return

    banned_keys = banned_keys if banned_keys is not None else {}

    used_tags: set[str] = set()
    entries: list[dict] = []
    for line, idx in items:
        outbound, err = parse_outbound(line, idx, used_tags)
        if err:
            results[line] = {"error": err, "services": {}}
            continue
        entries.append({"index": idx, "port": 0, "outbound": outbound})

    active = list(entries)
    if not active:
        return

    ports = reserve_ports(len(active))
    for e, p in zip(active, ports):
        e["port"] = p

    template = ROOT / "config" / "countrytest_template.json"
    config = build_batch_config(active, template)
    config_path = PROJECT_ROOT / "source" / "tests" / (
        f"service_batch_{items[0][1]}_{len(items)}.json"
    )
    proc = start_singbox(config, config_path)

    started_ok = False
    if proc is not None and proc.poll() is None:
        started_ok = ports_ready([e["port"] for e in active],
                                 STARTUP_WAIT_SECONDS)

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
                "Service батч из %d прокси не стартовал, делю пополам (%d + %d)",
                len(items), mid, len(items) - mid,
            )
            _run_batch(items[:mid], results, services, defaults,
                       concurrency=concurrency, depth=depth + 1,
                       banned_keys=banned_keys)
            _run_batch(items[mid:], results, services, defaults,
                       concurrency=concurrency, depth=depth + 1,
                       banned_keys=banned_keys)
        else:
            for line, _ in items:
                if line not in results:
                    results[line] = {
                        "error": "sing-box не смог обработать этот конфиг",
                        "services": {},
                    }
        return

    try:
        port_by_index = {e["index"]: e["port"] for e in active}
        pairs = [(line, port_by_index[idx]) for line, idx in items
                 if idx in port_by_index]
        for line, _ in pairs:
            results.setdefault(line, {"error": None, "services": {}, "exit_ip": None})

        # Пропуск забаненных: probe сервиса не выполняется, если пара
        # (line, service) уже в ban=0/1? — в ban=1. Заранее известные ok не
        # пропускаются (проверка повторяется — вердикт может смениться).
        pairs_jobs = []
        for line, port in pairs:
            key = normalize_key(line)
            for service in services:
                if key in banned_keys.get(service["name"], set()):
                    continue
                pairs_jobs.append((line, port, service))
        jobs = pairs_jobs
        # Параллелизм по ЧИСЛУ ПРОКСИ, а не общим числом воркеров.
        # Прежние 8 воркеров на 111 серверов x 2 сервиса = 222 пробы давали
        # 222/8 x (до 15 с) и гарантированный таймаут всей проверки (>180 с,
        # это и видно в логе как «service_access не уложился в таймаут»).
        # Столько же одновременных проб, сколько прокси, — безопасно: каждая
        # идёт через СВОЙ inbound-порт, очереди внутри sing-box не возникает.
        workers = max(1, min(len(jobs), max(concurrency, len(pairs))))
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_probe_service, port, service): (line, service["name"])
                for line, port, service in jobs
            }
            for fut in as_completed(futures):
                line, name = futures[fut]
                entry = fut.result()
                with lock:
                    results[line]["services"][name] = entry

        # exit-IP резолвим ДО завершения процесса: после terminate
        # inbound-порты закрываются и узнать выход уже нельзя.
        # Воркеров — по числу прокси, а не жёсткие 4: этот запрос идёт на
        # ip-api и занимал больше времени, чем сами пробы сервисов, потому что
        # 111 адресов обрабатывались по четыре за раз.
        with ThreadPoolExecutor(max_workers=max(1, min(len(pairs), 32))) as pool:
            ip_futures = {
                pool.submit(_resolve_exit_ip, port, defaults): line
                for line, port in pairs
            }
            for fut in as_completed(ip_futures):
                line = ip_futures[fut]
                try:
                    results[line]["exit_ip"] = fut.result()
                except Exception:  # noqa: BLE001
                    results[line]["exit_ip"] = None
    finally:
        stop_singbox(proc)
        release_ports(ports)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass


def batch_service_check(proxy_lines: list[str], *,
                        concurrency: int = 8,
                        batch_size: int = MAX_BATCH_INBOUNDS,
                        config_path: str | Path | None = None,
                        record: bool = True,
                        banned_keys: dict[str, set[str]] | None = None) -> dict[str, dict]:
    """Проверяет список прокси по правилам сервисов.

    Возвращает {proxy_line: {"error": None|str, "services": {
        name: {status, final_url, verdict, reason, error}},
        "exit_ip": str|None}}.

    record=True: вердикты ok/ban пишутся в service_bans с exit-IP прокси.
    banned_keys: {service: {proxy_key}} — пары, которые НЕ проверяются
    (прокси уже забанен этим сервисом). None = без пропуска.
    """
    cfg = load_services(config_path)
    services = cfg["services"]
    defaults = cfg.get("defaults") or {}

    lines = [l.strip() for l in proxy_lines if l and l.strip()]
    results: dict[str, dict] = {}
    for i in range(0, len(lines), batch_size):
        chunk = lines[i:i + batch_size]
        # items — это [(line, index)]: индекс нужен только для уникальных тэгов
        # outbound'ов внутри батча. enumerate даёт (index, line), поэтому
        # разворачиваем его на месте, а не в _run_batch.
        _run_batch([(line, idx) for idx, line in enumerate(chunk)],
                   results, services, defaults,
                   concurrency=concurrency, banned_keys=banned_keys)

    if record and results:
        _record_verdicts(results, defaults)

    return results


def _record_verdicts(results: dict[str, dict], defaults: dict) -> None:
    """Пишет вердикты ok/ban в service_bans c exit-IP.

    unknown не пишется: нет смысла засорять таблицу неопределёнными
    ответами — при следующей проверке пара будет опрошена снова.
    """
    store = ServerStore(get_settings().paths.servers_db_file)
    rows = []
    for line, res in results.items():
        if res.get("error"):
            continue
        # exit-IP уже разрешён в _run_batch, пока процесс sing-box был жив.
        exit_ip = res.get("exit_ip")
        if not exit_ip:
            continue
        for name, entry in res["services"].items():
            verdict = entry.get("verdict")
            if verdict not in (VERDICT_OK, VERDICT_BAN):
                continue
            rows.append({
                "exit_ip": exit_ip,
                "service": name,
                "ban": 1 if verdict == VERDICT_BAN else 0,
                "reason": entry.get("reason") or "",
                "proxy_key": normalize_key(line),
            })
    if rows:
        written = store.record_service_bans(rows)
        LOGGER.info("Service check: записано %d вердиктов в service_bans", written)


def banned_ips(service: str) -> set[str]:
    """Exit-IP, забаненные данным сервисом (по последним записям)."""
    return ServerStore(get_settings().paths.servers_db_file).banned_ips(service)


# --- CLI ------------------------------------------------------------------------
def _cmd_pool() -> int:
    """Пул из базы: берём check_pool, пропускаем прокси, забаненные всеми сервисами.

    Это «whitelist-подход»: проверяется только живой пул из базы, не мусор.
    """
    store = ServerStore(get_settings().paths.servers_db_file)
    services = [s["name"] for s in load_services()["services"]]
    lines = db_pool_lines(store, services)
    print(f"Пул из базы: {len(lines)} прокси для проверки {services}")
    if not lines:
        print("Пул пуст (или все прокси уже забанены всеми сервисами).")
        return 0
    results = batch_service_check(lines)
    for line, res in results.items():
        short = line[:70] + ("..." if len(line) > 70 else "")
        if res.get("error"):
            print(f"{short}  ERROR: {res['error']}")
            continue
        parts = []
        for name, entry in res["services"].items():
            mark = {"ok": "+", "ban": "!", "unknown": "?"}[entry["verdict"]]
            extra = f" ({entry['reason']})" if entry["reason"] else ""
            parts.append(f"{name}={mark}{extra}")
        ip = res.get("exit_ip") or "?"
        print(f"{short}  [{ip}]  " + "  ".join(parts))
    return 0


def _cmd_status() -> int:
    """Отчёт: по каждому сервису счётчики + список прокси с ok."""
    store = ServerStore(get_settings().paths.servers_db_file)
    report = store.service_report()
    services = [s["name"] for s in load_services()["services"]]
    print("Состояние сервисов:")
    for name in services:
        r = report.get(name)
        if not r:
            print(f"  {name}: нет данных (не проверялся)")
            continue
        print(f"  {name}: ok={r['ok_ips']} IPs, {r['ok_proxies']} прокси; "
              f"banned={r['banned_ips']} IPs; unknown={r['unknown']}; "
              f"последняя проверка: {r['last_checked']}")
        # для каждого сервиса: список прокси с ok
        rows = store.service_working_rows(name)
        if rows:
            print(f"    Работающие прокси для {name}: {len(rows)}")
            for row in rows[:20]:
                print(f"      [{row['exit_ip']}] stable={row['stable']} "
                      f"ping={row['ping_ms']}ms  {row['line'][:70]}")
            if len(rows) > 20:
                print(f"      ... и ещё {len(rows) - 20}")
    return 0


def main() -> int:
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Использование: python -m script.service_check <команда>")
        print("  python -m script.service_check '<proxy_line>' [line2 ...]  # проверка строк")
        print("  python -m script.service_check status                     # отчёт по сервисам")
        print("  python -m script.service_check pool                       # пул из базы, пропуск banned")
        return 0
    if args[0] == "status":
        return _cmd_status()
    if args[0] == "pool":
        return _cmd_pool()
    results = batch_service_check(args)
    for line, res in results.items():
        short = line[:60] + ("..." if len(line) > 60 else "")
        if res.get("error"):
            print(f"{short}  ERROR: {res['error']}")
            continue
        parts = []
        for name, entry in res["services"].items():
            mark = {"ok": "+", "ban": "!", "unknown": "?"}[entry["verdict"]]
            extra = f" ({entry['reason']})" if entry["reason"] else ""
            parts.append(f"{name}={mark}{extra}")
        ip = res.get("exit_ip") or "?"
        print(f"{short}  [{ip}]  " + "  ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())