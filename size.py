
# -*- coding: utf-8 -*-
import sqlite3, pathlib, json
db = pathlib.Path(r"C:\.harness\singbox-subs-finder\source\servers.db")
print("файл БД:", db.exists(), db.stat().st_size // 1024, "КБ" if db.exists() else "")
con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
cur = con.cursor()
print()
print("таблицы:", [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")])
print()
for q, label in [
    ("SELECT COUNT(*) FROM servers", "всего серверов"),
    ("SELECT COUNT(*) FROM servers WHERE available=1", "доступных"),
    ("SELECT COUNT(*) FROM servers WHERE excluded=1", "исключённых"),
    ("SELECT COUNT(*) FROM urls", "подписок/строк urls"),
]:
    try:
        print("  %-22s %s" % (label, cur.execute(q).fetchone()[0]))
    except Exception as exc:
        print("  %-22s нет такой таблицы/колонки (%s)" % (label, str(exc)[:40]))
print()
try:
    print("распределение по available:")
    for r in cur.execute(
            "SELECT available, COUNT(*), AVG(stable), MAX(stable) "
            "FROM servers GROUP BY available"):
        print("   available=%s  n=%-6s средний stable=%.1f  макс=%.1f" % r)
except Exception as exc:
    print("  ", exc)
con.close()
