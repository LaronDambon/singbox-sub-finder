import sqlite3, sys
sys.path.insert(0, "singbox-subscribe")
from config.settings import init_settings, get_settings
init_settings()
p = get_settings().paths.servers_db_file
con = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
print("server_tags всего:", con.execute("SELECT COUNT(*) FROM server_tags").fetchone()[0])
print("  service:", con.execute("SELECT COUNT(*) FROM server_tags WHERE kind='service'").fetchone()[0])
print("  geo:", con.execute("SELECT COUNT(*) FROM server_tags WHERE kind='geo'").fetchone()[0])
print("servers со stable -999:", con.execute("SELECT COUNT(*) FROM servers WHERE stable = -999").fetchone()[0])
print("очередь pending:", con.execute("SELECT COUNT(*) FROM check_queue WHERE status='pending'").fetchone()[0])
con.close()
