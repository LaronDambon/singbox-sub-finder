"""Смоук-тест центральной базы серверов и чистых функций urltest.

Запуск: python -m tests.test_server_store   (или просто python tests/test_server_store.py)
Зависимостей кроме stdlib не требует; реальная база проекта не затрагивается.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from script.server_store import ServerStore, parse_stable_from_line


def make_line(uuid: str, host: str, port: int) -> str:
    return f"vless://{uuid}@{host}:{port}?encryption=none&security=tls#tag {host}"


def test_upsert_and_stable(tmp: Path) -> None:
    # Изолируемся от legacy-файлов проекта: миграция не должна срабатывать.
    ServerStore.LEGACY_WHITELIST = tmp / "__no_whitelist.txt"
    ServerStore.LEGACY_BLACKLIST = tmp / "__no_blacklist.txt"
    db = tmp / "t1.db"
    store = ServerStore(db)
    lines = [
        make_line("u1", "1.1.1.1", 443),
        make_line("u2", "2.2.2.2", 443),
        make_line("u3", "3.3.3.3", 443),
    ]
    res = store.upsert_lines(lines)
    assert res == {"added": 3, "updated": 0}, res
    res2 = store.upsert_lines(lines)
    assert res2 == {"added": 0, "updated": 3}, res2

    keys = list(store.load_stable_map().keys())
    assert len(keys) == 3

    k1 = store._key_of(lines[0])
    k2 = store._key_of(lines[1])
    k3 = store._key_of(lines[2])

    # Цикл 1: u1 ok, u2 fail, u3 fail
    store.record_results([
        {"key": k1, "available": True, "line": lines[0] + "-ping-100-stable-1", "ping_ms": 100},
        {"key": k2, "available": False, "line": lines[1] + "-stable--1"},
        {"key": k3, "available": False, "line": lines[2] + "-stable--1"},
    ])
    smap = store.load_stable_map()
    assert smap[k1] == 1 and smap[k2] == -1 and smap[k3] == -1, smap

    # Цикл 2: u1 снова ok -> stable=2 (попадает в экспорт stable>1), u2 fail -> -2
    store.record_results([
        {"key": k1, "available": True, "line": lines[0] + "-ping-90-stable-2", "ping_ms": 90},
        {"key": k2, "available": False, "line": lines[1] + "-stable--2"},
    ])
    smap = store.load_stable_map()
    assert smap[k1] == 2 and smap[k2] == -2, smap

    # Потолок stable: сколько бы успехов подряд ни было, выше STABLE_MAX не растёт
    from config.settings import STABLE_MAX

    for _ in range(10):
        store.record_results([
            {"key": k1, "available": True, "line": lines[0], "ping_ms": 80},
        ])
    smap = store.load_stable_map()
    assert smap[k1] == STABLE_MAX, (smap[k1], STABLE_MAX)

    exported = store.export_lines(min_stable=1)  # строго stable > 1
    assert len(exported) == 1 and exported[0] == lines[0], exported

    dead = store.export_lines(max_stable=0)  # строго stable < 0
    assert len(dead) == 2, dead

    stats = store.stats()
    assert stats["zones"]["proven_gt_1"] == 1
    assert stats["zones"]["dead_lt_0"] == 2
    print("[ok] upsert/stable/export filters")


def test_pool_and_purge(tmp: Path) -> None:
    # Изолируемся от legacy-файлов проекта: миграция не должна срабатывать.
    ServerStore.LEGACY_WHITELIST = tmp / "__no_whitelist.txt"
    ServerStore.LEGACY_BLACKLIST = tmp / "__no_blacklist.txt"
    db = tmp / "t2.db"
    store = ServerStore(db)
    lines = [make_line(f"a{i}", f"9.9.9.{i}", 443) for i in range(1, 5)]
    store.upsert_lines(lines)
    keys = {}
    for ln in lines:
        keys[ln] = store._key_of(ln)

    # a1 дважды ок (stable 2), a2 один раз ок (1), a3 fail (-1), a4 не проверялся (0)
    store.record_results([
        {"key": keys[lines[0]], "available": True, "line": lines[0], "ping_ms": 50},
        {"key": keys[lines[1]], "available": True, "line": lines[1], "ping_ms": 200},
    ])
    store.record_results([
        {"key": keys[lines[0]], "available": True, "line": lines[0], "ping_ms": 60},
        {"key": keys[lines[2]], "available": False, "line": lines[2]},
    ])

    pool_before = [r["key"] for r in store.check_pool()]
    assert len(pool_before) == 4
    # Сортировка: стабильные первыми
    assert pool_before[0] == keys[lines[0]], pool_before

    removed = store.purge_dead(0)  # stable < 0 -> из проверки
    assert removed == 1, removed
    pool_after = [r["key"] for r in store.check_pool()]
    assert keys[lines[2]] not in pool_after and len(pool_after) == 3, pool_after

    # Повторная регистрация в подписке НЕ возвращает мёртвый сервер в пул
    store.upsert_lines([lines[2]])
    assert keys[lines[2]] not in [r["key"] for r in store.check_pool()]

    # reset-excluded возвращает его в ротацию со stable=0
    revived = store.reset_excluded()
    assert revived == 1
    pool_reset = [r["key"] for r in store.check_pool()]
    assert keys[lines[2]] in pool_reset and len(pool_reset) == 4

    # Снова неудача -> stable=-1 -> hard delete физически удаляет запись
    store.record_results([{"key": keys[lines[2]], "available": False, "line": lines[2]}])
    store.purge_dead(0, hard=True)
    assert store.stats()["total"] == 3
    assert store.stats()["total"] == 3
    print("[ok] check pool / purge / reset / hard delete")


def test_legacy_migration(tmp: Path) -> None:
    db = tmp / "t3.db"
    wl = tmp / "whitelist.txt"
    bl = tmp / "blacklist.txt"
    good = make_line("g1", "7.7.7.7", 443) + "-ping-120-stable-3"
    mid = make_line("g2", "7.7.7.8", 443) + "-ping-220-stable-0"
    bad = make_line("b1", "8.8.8.8", 443) + "-stable--1"
    wl.write_text(good + "\n" + mid + "\n", encoding="utf-8")
    bl.write_text(bad + "\n", encoding="utf-8")

    ServerStore.LEGACY_WHITELIST = wl
    ServerStore.LEGACY_BLACKLIST = bl
    store = ServerStore(db)

    smap = store.load_stable_map()
    kgood = store._key_of(good)
    kbad = store._key_of(bad)
    assert smap[kgood] == 3, smap
    assert parse_stable_from_line(mid) == 0
    assert kbad in smap

    pool_keys = {r["key"] for r in store.check_pool()}
    assert kbad not in pool_keys, "legacy blacklist должен быть исключён"
    assert kgood in pool_keys
    print("[ok] legacy migration whitelist/blacklist -> db")


def test_evaluate_finalize(tmp: Path) -> None:
    from script.urltest import _evaluate_nodes, _finalize_rows

    raw1 = make_line("e1", "5.5.5.5", 443)          # страны в имени нет
    raw2 = "vless://uuid@6.6.6.6:443?security=tls#🇩🇪 Germany"  # эмодзи в имени
    tag_to_line = {"t1": raw1, "t2": raw2}
    parsed = [("t1", 80), ("t2", None)]
    stable_map = {ServerStore._key_of(raw2): 1}

    decisions, cc_lines = _evaluate_nodes(tag_to_line, parsed, dict(stable_map), serial_start=0, purge_below=0)
    by_tag = {d["tag"]: d for d in decisions}
    d1, d2 = by_tag["t1"], by_tag["t2"]
    assert d1["is_ok"] is True and d1["new_stable"] == 1
    assert d2["is_ok"] is False and d2["new_stable"] == 0, d2
    # raw1 без страны в имени -> нужен speedtest; у raw2 эмодзи есть -> не нужен
    assert ServerStore._key_of(raw1) in {ServerStore._key_of(x) for x in cc_lines}
    assert not any(ServerStore._key_of(raw2) == ServerStore._key_of(x) for x in cc_lines)

    rows = _finalize_rows(decisions, {})
    row1 = next(r for r in rows if r["key"] == ServerStore._key_of(raw1))
    assert row1["available"] is True and "-ping-80-stable-1" in row1["line"], row1
    row2 = next(r for r in rows if r["key"] == ServerStore._key_of(raw2))
    assert row2["available"] is False and "-stable-0" in row2["line"], row2

    # Исключение: сервер с prev=0 и fail -> new=-1 < purge_below=0
    parsed_fail = [("t1", None)]
    dec2, _ = _evaluate_nodes({"t1": raw1}, parsed_fail, {}, serial_start=10, purge_below=0)
    assert dec2[0]["is_excluded"] is True and dec2[0]["new_stable"] == -1

    # Потолок stable в решениях: prev >> STABLE_MAX не даёт роста выше капа
    from config.settings import STABLE_MAX

    dec_cap, _ = _evaluate_nodes(
        {"t1": raw1}, [("t1", 50)], {ServerStore._key_of(raw1): 9999},
        serial_start=40, purge_below=0,
    )
    assert dec_cap[0]["new_stable"] == STABLE_MAX, dec_cap[0]
    assert "-stable-" + str(STABLE_MAX) in next(
        r["line"] for r in _finalize_rows(dec_cap, {}) if r["key"] == ServerStore._key_of(raw1)
    )

    # --- Страна известна из базы -> скоростной тест НЕ нужен, эмодзи переносится ---
    known = {ServerStore._key_of(raw1): "🇩🇪"}
    dec3, cc3 = _evaluate_nodes(
        {"t1": raw1}, [("t1", 120)], {}, serial_start=20, purge_below=0,
        known_countries=known,
    )
    assert ServerStore._key_of(raw1) not in {ServerStore._key_of(x) for x in cc3}, (
        "сервер с известной страной не должен попадать в список проверки страны"
    )
    assert dec3[0]["country_tag"] == "🇩🇪", dec3[0]
    rows3 = _finalize_rows(dec3, {})
    assert "🇩🇪" in rows3[0]["line"] and "-ping-120-stable-1" in rows3[0]["line"], rows3[0]

    # --- Эмодзи в имени тоже исключает из проверки (без обращения к базе) ---
    dec4, cc4 = _evaluate_nodes(
        {"t1": raw2}, [("t1", 90)], {}, serial_start=30, purge_below=0,
    )
    assert len(cc4) == 0 and dec4[0]["country_tag"] == "🇩🇪"

    print("[ok] evaluate/finalize pure helpers (+known-country skip)")
def test_country_check_helpers(tmp: Path) -> None:
    from script import country_check as cc

    # ISO -> эмодзи
    assert cc.iso_to_emoji("DE") == "🇩🇪"
    assert cc.iso_to_emoji("us") == "🇺🇸"
    assert cc.iso_to_emoji("") is None
    assert cc.iso_to_emoji("DEU") is None
    assert cc.iso_to_emoji(None) is None

    # Парсеры geo-ответов
    class FakeResp:
        def __init__(self, text, payload):
            self.text = text
            self._payload = payload
        def json(self):
            return self._payload

    trace = "fl=1\nh=www.cloudflare.com\nip=1.2.3.4\nloc=de\ntls=TLSv1.3\n"
    assert cc._parse_cloudflare_trace(FakeResp(trace, {})) == "DE"
    assert cc._parse_cloudflare_trace(FakeResp("noloc here", {})) is None
    assert cc._parse_ip_sb(FakeResp("", {"country_code": "jp"})) == "JP"
    assert cc._parse_ip_sb(FakeResp("", {"country_code": "JPN"})) is None
    assert cc._parse_ipinfo(FakeResp("", {"country": "FR"})) == "FR"

    # Сборка батч-конфига: N inbound -> N outbound, wireguard уходит в endpoints
    entries = [
        {"index": 0, "port": 20001, "outbound": {"tag": "a", "type": "vless", "server": "1.1.1.1"}},
        {"index": 1, "port": 20002, "outbound": {"tag": "b", "type": "wireguard", "server": "2.2.2.2"}},
    ]
    tpl = ROOT / "config" / "countrytest_template.json"
    cfg = cc._build_batch_config(entries, tpl)
    assert len(cfg["inbounds"]) == 2
    assert cfg["inbounds"][0]["listen_port"] == 20001
    assert {r["outbound"] for r in cfg["route"]["rules"][:2]} == {"a", "b"}
    assert [r["inbound"] for r in cfg["route"]["rules"][:2]] == [["cin-0"], ["cin-1"]]
    assert cfg["route"]["final"] == "direct"
    types = {o["type"] for o in cfg["outbounds"]}
    assert "vless" in types and "direct" in types
    assert cfg["endpoints"][0]["tag"] == "b"
    assert all(o.get("type") != "selector" for o in cfg["outbounds"])

    print("[ok] country_check helpers (iso emoji / parsers / batch config)")
def _run_all(tmp: Path) -> None:
    test_upsert_and_stable(tmp)
    test_pool_and_purge(tmp)
    test_legacy_migration(tmp)
    test_evaluate_finalize(tmp)
    test_country_check_helpers(tmp)


def main() -> int:
    import shutil

    # Временная папка рядом с репозиторием: не зависит от системного TEMP и прав доступа.
    tmp = ROOT / ".tmp_tests"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        _run_all(tmp)
        print("")
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())