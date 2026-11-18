import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, "singbox-subscribe")
from config.settings import init_settings, get_settings
init_settings()
from script.batch_singbox import (build_batch_config, parse_outbound, reserve_ports,
                                  ports_ready, start_singbox, stop_singbox)
from script.core import release_ports
from browser import batch as bbatch, services
from browser.check import check_service

p = Path(str(get_settings().paths.servers_db_file))
con = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
row = con.execute("SELECT line FROM servers WHERE line LIKE '%zfapi.z%' LIMIT 1").fetchone()
con.close()
line = row[0]
used = set()
out, err = parse_outbound(line, 0, used)
ports = reserve_ports(1)
entry = {"index": 0, "port": ports[0], "outbound": out}
cfg = build_batch_config([entry], Path(str(get_settings().paths.countrytest_template)))
proc = start_singbox(cfg, Path("source/tests/_bc_probe.json"))
try:
    ports_ready([entry["port"]], 10)
    proxy = "127.0.0.1:%d" % entry["port"]
    spec = services.get_service("gemini")

    state = bbatch.load_login_state("gemini")
    print("storage_state cookies:", len((state or {}).get("cookies") or []))
    names = {c.get("name") for c in (state or {}).get("cookies") or []}
    print("есть SID/SAPISID в state:", bool({"SID", "SAPISID"} & names))

    res = bbatch.check_batch("gemini", [(line, proxy)], workers=1,
                             timeout_ms=45000, wait_seconds=spec.wait_seconds, log=None)
    r = res.get(line) or {}
    print("BATCH  ->", r.get("verdict"), "|", r.get("reason", "")[:90])

    r2 = check_service("gemini", proxy=proxy, headless=True)
    print("SINGLE ->", r2.get("verdict"), "|", r2.get("reason", "")[:90])
finally:
    stop_singbox(proc); release_ports(ports)
