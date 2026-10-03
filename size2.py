
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"C:\.harness\singbox-subs-finder\singbox-subscribe")
from config.settings import get_settings
import sqlite3, pathlib
s = get_settings()
for label, p in [("БД серверов", s.paths.servers_db_file),
                 ("подписки", s.paths.urls_file),
                 ("whitelist", s.paths.whitelist_file)]:
    print("%-12s %s  %s" % (label, p, ("есть, " + str(p.stat().st_size // 1024) + " КБ")
                             if p.exists() else "НЕТ ФАЙЛА"))

db = s.paths.servers_db_file
if db.exists():
    con = sqlite3.connect("file:%s?mode=ro" % pathlib.Path(db).as_posix(), uri=True)
    cur = con.cursor()
    print()
    print("таблицы:", [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
    for q, label in [
        ("SELECT COUNT(*) FROM servers", "всего серверов"),
        ("SELECT COUNT(*) FROM servers WHERE available=1", "доступных"),
        ("SELECT COUNT(*) FROM servers WHERE excluded=1", "исключённых"),
    ]:
        try:
            print("  %-22s %s" % (label, cur.execute(q).fetchone()[0]))
        except Exception as exc:
            print("  %-22s (%s)" % (label, str(exc)[:50]))
    print()
    try:
        for r in cur.execute("SELECT available, COUNT(*), AVG(stable) "
                             "FROM servers GROUP BY available"):
            print("   available=%s n=%-6s средний stable=%.1f" % r)
    except Exception as exc:
        print("  ", str(exc)[:80])
    con.close()
