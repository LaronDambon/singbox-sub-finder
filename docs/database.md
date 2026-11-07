# Центральная база серверов

Единственный источник правды о серверах и их стабильности — SQLite-база `source/servers.db` (модуль [script/server_store.py](../singbox-subscribe/script/server_store.py), класс `ServerStore`).

При первом запуске база автоматически наполняется из старых `whitelist.txt`/`blacklist.txt` (whitelist-серверы считаются пинговавшимися и получают щит, blacklist — умершие).

Зоны stable (при настройках по умолчанию, порог `0`, мёртвый `-1`):

| Диапазон | Значение |
|---|---|
| `stable = -1` | умерший: не импортируется в проверку (`excluded=1` после purge) |
| `stable = 0` | на границе: одна неудачная проверка до смерти |
| `stable 1..95` | живёт за счёт остатка «щита» |
| `stable >= 96` | под полным щитом после удачного пинга; в whitelist по результату последней проверки (`available=1`) |

Пороги настраиваются в `config.yaml`:
- `core.purge_stable_below` (по умолчанию `0`) — нижний порог: из проверки исключаются все со `stable <` порога (то есть умершие `stable=-1`);
- `core.new_server_stable` (по умолчанию `5`) — stable нового сервера до первого удачного пинга;
- `core.shield_cycles` (по умолчанию `96`) — «щит» после удачного пинга: минимум неудачных проверок, которые сервер переживёт.

### Принцип whitelist и «щит» от удаления (stable)
- **Whitelist = последняя проверка.** Принципа «2 раза не пинганулся — убрать из
  whitelist» больше нет: сервер пинганулся → добавляется в whitelist
  (`whitelist.txt`, `/api/whitelist`, деплой), не пинганулся → не добавляется.
  На следующем цикле он снова проверяется, и список обновляется целиком.
- **«Щит» для пинговавшихся.** В базе есть колонка `ever_pinged` (1 — сервер
  пинговался хотя бы раз). Каждый удачный пинг ставит `stable` не ниже
  `SHIELD_CYCLES=96`: сервер переживёт до 96 неудачных проверок подряд. Каждая
  неудача списывает `-1`, поэтому сервер со щитом постепенно «истекает».
- **Новый сервер** до первого удачного пинга имеет `NEW_SERVER_STABLE=5`:
  несколько попыток доказать жизнеспособность.
- **Мёртвый порог — `0`.** В цикл проверки импортируются все серверы со
  `stable >= PURGE_STABLE_BELOW=0` (`check_pool`); дойдя до `-1`, сервер больше
  не импортируется из базы. `stats()`/лог цикла показывают зоны
  `alive`/`dead`/`shielded`/`online`/`reserve`/`unchecked`.
- Колонки старой модели (`fail_streak`, `temp_ban_until`, `countries`,
  `dirty_tags`) больше не используются; старая база мигрирует автоматически
  (маркеры `servers.tempban-migrated.json`, `servers.shield-migrated.json`).

CLI управления базой (из папки `singbox-subscribe`):

```bash
python -m script.server_store stats                 # статистика по зонам stable
python -m script.server_store export                # whitelist: пинговавшиеся в последней проверке (available=1)
python -m script.server_store export --min-stable 1 # вывести список stable > 1
python -m script.server_store export --max-stable 0  # вывести умерших (stable < 0)
python -m script.server_store purge --below 0        # исключить умерших из проверки (--hard — удалить физически)
python -m script.server_store reset-excluded         # вернуть исключённые в ротацию (сброс состояния)
```

HTTP API (дополнительно к `/api/whitelist`, который теперь читает базу):
- `GET /api/servers/stats` — статистика базы;
- `GET /api/servers?available=1` — whitelist (пинговавшиеся в последней проверке); `?min_stable=1` — выборка `stable > 1`; `?max_stable=0` — `stable < 0` (умершие, не импортируются); поддержан `limit`.
