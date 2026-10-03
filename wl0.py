
# -*- coding: utf-8 -*-
import sys, json, pathlib, collections
sys.path.insert(0, r"C:\.harness\singbox-subs-finder\singbox-subscribe")
from config.settings import get_settings
s = get_settings()

u = pathlib.Path(s.paths.urls_file)
print("=== urls.json,", u.stat().st_size, "байт ===")
print(u.read_text(encoding="utf-8")[:300] or "(пусто)")

w = pathlib.Path(s.paths.whitelist_file)
lines = [l.strip() for l in w.read_text(encoding="utf-8").splitlines() if l.strip()]
print()
print("=== Текущий whitelist.txt:", len(lines), "строк ===")
print("первые 3:", [l[:70] for l in lines[:3]])

# разбираем теги
import re
tagcount = collections.Counter()
withtag = 0
for l in lines:
    # singbox: формат "имя, теги" или теги в квадратных скобках
    m = re.findall(r"\[([^\]]+)\]", l)
    if m:
        withtag += 1
        for t in m:
            for part in t.split(","):
                part = part.strip()
                if part:
                    tagcount[part] += 1
print()
print("строк с тегами в скобках:", withtag, "из", len(lines))
print()
print("=== самые частые теги ===")
for t, n in tagcount.most_common(20):
    print("   %-28s %d" % (t, n))
print()
print("уникальных тегов:", len(tagcount))
