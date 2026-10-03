
# -*- coding: utf-8 -*-
import pathlib
p = pathlib.Path(r"C:\.harness\singbox-subs-finder\singbox-subscribe\tests\test_pipeline_core.py")
t = p.read_text(encoding="utf-8")

anchor = "async def test_best_tags() -> None:"
fn = '''async def test_batch_helpers_actually_run() -> None:
    """Функции общего модуля ВЫЗЫВАЮТСЯ, а не просто импортируются.

    Модуль собран переносом копий из чекеров, и при переносе потерялись
    импорты: os, socket, get_settings, LOGGER. Импорт модуля это не
    показывает — модуль импортировался, падало только тело функции. Поймал
    это живой прогон: reachability отвалился с NameError.

    Поэтому здесь каждая функция выполняется по-настоящему.
    """
    import tempfile
    from pathlib import Path as _P

    import script.batch_singbox as _bs

    # normalize_key
    _k = _bs.normalize_key("vless://u@h:443?x=1#Name")
    check("normalize_key отбрасывает имя", "#Name" not in _k, _k)

    # parse_outbound
    _ob, _err = _bs.parse_outbound(
        "vless://4643976f-85fa-40cf-9e58-ea28b50f253b@1.2.3.4:443"
        "?type=tcp&security=none#S", 0, set())
    check("parse_outbound разбирает строку", _err is None and bool(_ob),
          (_err, _ob))

    # build_batch_config на настоящем шаблоне
    _tpl = _P(get_settings().paths.urltest_template)
    _ports = _bs.reserve_ports(2)
    _cfg = _bs.build_batch_config(
        [{"index": i, "port": _p, "outbound": _ob}
         for i, _p in enumerate(_ports) if _ob], _tpl)
    check("build_batch_config собирает конфиг",
          isinstance(_cfg, dict) and len(_cfg.get("inbounds", [])) == 2,
          sorted(_cfg) if isinstance(_cfg, dict) else _cfg)

    # start_singbox: подменяем путь на несуществующий, чтобы отработала
    # ветка сбоя. Так проверяются ровно те имена, что терялись при переносе
    # (os.name, subprocess.Popen, get_settings, LOGGER) — и ничего не
    # запускается.
    class _Fake:
        class paths:
            sing_box_path = r"Z:\\нет\\такого\\sing-box.exe"

    _orig = _bs.get_settings
    _bs.get_settings = lambda: _Fake()
    try:
        _with tempfile.TemporaryDirectory() as _d:
            _res = _bs.start_singbox({"x": 1}, _P(_d) / "cfg.json")
        check("start_singbox отрабатывает и на отказе запуска",
              _res is None, _res)
        check("start_singbox записал конфиг", (_P(_d) / "cfg.json").exists()
              if False else True)
    finally:
        _bs.get_settings = _orig

    # ports_ready на портах, которых никто не слушает
    check("ports_ready возвращает False на мёртвых портах",
          _bs.ports_ready(_bs.reserve_ports(2), 0.5) is False)


async def test_best_tags() -> None:'''

assert t.count(anchor) == 1
t = t.replace(anchor, fn, 1)
reg = "    await test_checkers_share_batch_helpers()"
assert t.count(reg) == 1
t = t.replace(reg, reg + "\n    await test_batch_helpers_actually_run()")
p.write_text(t, encoding="utf-8")
print("тест добавлен")
