# Центральная база серверов

Единственный источник правды о серверах — SQLite-база `source/servers.db`.
Код доступа: [script/server_store.py](../singbox-subscribe/script/server_store.py)
(класс `ServerStore`), схема очередей — [pipeline/database.py](../singbox-subscribe/pipeline/database.py).

## Как открыть

Только чтение, чтобы случайно не испортить базу во время работы pipeline:

```bash
sqlite3 "file:source/servers.db?mode=ro" "SELECT COUNT(*) FROM servers;"
```

В коде то же самое:

```python
import sqlite3
conn = sqlite3.connect("file:source/servers.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only = ON")
```

База работает в режиме WAL, поэтому читать её можно одновременно с записью
pipeline.

## Таблицы

| Таблица | Что хранит |
|---|---|
| `servers` | сам сервер: строка, стабильность, живость, замеры |
| `server_tags` | теги сервера: пройденные сервисы и свойства IP |
| `asn_info` | справочник автономных систем, расширяется сам |
| `check_queue` | очередь на проверку |
| `result_queue` | вердикты от службы проверки к службе записи |
| `service_bans` | баны по паре (exit_ip, сервис) |

---

## `servers` — один сервер

| Колонка | Тип | Что значит |
|---|---|---|
| `key` | TEXT PK | нормализованный ключ (`normalize_proxy_key`); по нему идёт поиск |
| `line` | TEXT | сама ссылка `vless://...` / `vmess://...` |
| `stable` | INTEGER | «жизни»: удачный пинг поднимает до 96, неудача снимает 1 |
| `ever_pinged` | INTEGER | 0/1, пинговался ли хоть раз |
| `ping_ms` | INTEGER | задержка последней проверки, мс |
| `protocol` | TEXT | `vless`, `vmess`, `ss`, ... |
| `country` | TEXT | эмодзи-флаг страны |
| `available` | INTEGER | **1** — пинганулся в последней проверке, **0** — нет, **NULL** — не проверялся |
| `checks` | INTEGER | сколько раз проверялся |
| `fails` | INTEGER | сколько раз провалился |
| `capabilities` | TEXT | устаревшая строка тегов через запятую; читать `server_tags` |
| `capabilities_pass` | TEXT | метка прохода, писавшего профиль |
| `excluded` | INTEGER | 0/1, исключён из ротации |
| `speed_mbps` | REAL | скорость, МБ/с |
| `speed_down` | REAL | скачивание |
| `speed_up` | REAL | отдача |
| `service_pings` | TEXT | устаревший JSON замеров; читать `server_tags` |
| `first_seen` | TEXT | ISO-время первого появления |
| `last_seen` | TEXT | когда последний раз видели в подписке |
| `last_checked` | TEXT | когда последний раз проверяли |

Индексы: `idx_servers_stable(stable)`, `idx_servers_excluded(excluded)`.

### Зоны `stable`

| Диапазон | Значение |
|---|---|
| `-1` | умерший: в проверку не попадает, после purge `excluded=1` |
| `0` | на границе: одна неудача до смерти |
| `1..95` | живёт за счёт остатка «щита» |
| `>= 96` | полный «щит» после удачного пинга |

Пороги в `config.yaml`: `core.purge_stable_below` (0),
`core.new_server_stable` (5), `core.shield_cycles` (96).

---

## `server_tags` — теги сервера

Одна строка на пару (тег, сервер). Здесь сведены обе прежние колонки:
`capabilities` и `service_pings`.

```sql
CREATE TABLE server_tags (
    tag      TEXT    NOT NULL,   -- 'openrouter', 'isp-megafon', 'dc'
    key      TEXT    NOT NULL,   -- ссылка на servers.key
    ping_ms  INTEGER,            -- замер до цели; NULL = не мерили
    passed   INTEGER NOT NULL DEFAULT 0,
    kind     TEXT    NOT NULL DEFAULT 'service',
    PRIMARY KEY (tag, key)
);
CREATE INDEX idx_server_tags_key ON server_tags(key);
```

| Колонка | Значение |
|---|---|
| `tag` | имя тега |
| `ping_ms` | задержка до цели этого тега (TTFB), мс; `NULL` если не измерялась |
| `passed` | 1 — цель пройдена (или свойство подтверждено), 0 — только измерено |
| `kind` | `service` — пройденный сервис, `geo` — свойство IP |

### Два вида тегов

**`kind = 'service'`** — до каких сервисов сервер дотягивается:

```
openrouter  youtube  telegram  microsoft  openai  gemini-api  yandex  global
```

`passed = 1` значит «цель пройдена», `passed = 0` — «дотянулись, но сервис
отказал или слишком медленно». Второе тоже полезно: по нему видно, что
проверка была, а не что её не было.

**`kind = 'geo'`** — что за IP (ставит чекер `ip_info`):

| Тег | Значит |
|---|---|
| `isp-megafon` | провайдер |
| `city-novosibirsk` | город |
| `region-sicily` | регион, если отличается от города |
| `as-56630` | номер автономной системы |
| `dc` | датацентр — плохо для Google и Claude |
| `res` | не датацентр (ставится только если источник это знает) |
| `mobile` | мобильный оператор |

Гео-теги пишутся с `passed = 1`: это подтверждённые факты о сервере. Проход
сервисов их не стирает — `reset` чистит только свой `kind`.

---

## `asn_info` — справочник провайдеров

```sql
CREATE TABLE asn_info (
    asn         TEXT PRIMARY KEY,   -- '20473'
    name        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',   -- dc | res | mobile
    source      TEXT NOT NULL DEFAULT '',   -- peeringdb | learned
    seen_dc     INTEGER NOT NULL DEFAULT 0,
    seen_res    INTEGER NOT NULL DEFAULT 0,
    seen_mobile INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT ''
);
```

Таблица **расширяется сама**. Каждая проверка `ip_info` приносит от
ip-api.com флаги `hosting` и `mobile` для конкретного IP; они копятся
голосами по номеру AS. Набралось `learn_min_votes` наблюдений и совпало
`learn_ratio` — провайдер получает устойчивый `kind`, и дальше все его
серверы классифицируются без единого запроса.

---

## Очереди и баны

`check_queue` — что ещё не проверено:

| Колонка | Значение |
|---|---|
| `key`, `line` | сервер |
| `status` | `pending` / `in_progress` / `done` / `failed` |
| `source` | `pool` (из базы), `discovery` (из подписок), `retry` |
| `attempts` | сколько раз брали в работу |
| `enqueued_at`, `updated_at`, `last_error` | служебные |

`result_queue` — вердикты от проверки к записи (в обычном прогоне не
используется, запись идёт сразу).

`service_bans` — бан по паре (`exit_ip`, `service`): работает ли сервис с
этого адреса. Ключ — IP выхода, а не прокси: один и тот же IP может стоять
за десятком ссылок.

---

# SQL-рецепты для фильтров

Ниже — готовые запросы. Они же подставляются в шаблон конфига (см.
[Фильтры](filters.md)).

## Живые серверы

```sql
SELECT line FROM servers
WHERE available = 1 AND excluded = 0 AND line != ''
ORDER BY ping_ms IS NULL, ping_ms;
```

## Серверы, проходящие сервис

```sql
SELECT s.line FROM servers s
JOIN server_tags t ON t.key = s.key
WHERE t.tag = 'openrouter'
  AND t.kind = 'service'
  AND t.passed = 1
  AND s.available = 1 AND s.excluded = 0
ORDER BY t.ping_ms IS NULL, t.ping_ms;
```

## Лучшие 5 по задержке до цели

```sql
... ORDER BY t.ping_ms IS NULL, t.ping_ms LIMIT 5;
```

## Резерв: следующие 21 (позиции 6–26)

```sql
... ORDER BY t.ping_ms IS NULL, t.ping_ms LIMIT 21 OFFSET 5;
```

## Чистые IP: без датацентров

```sql
SELECT s.line FROM servers s
JOIN server_tags t ON t.key = s.key
WHERE t.tag = 'openrouter' AND t.kind = 'service' AND t.passed = 1
  AND s.available = 1 AND s.excluded = 0
  AND NOT EXISTS (
      SELECT 1 FROM server_tags g
      WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'dc'
  )
ORDER BY t.ping_ms IS NULL, t.ping_ms;
```

## Только подтверждённо домашние (не «неизвестные»)

```sql
  AND EXISTS (
      SELECT 1 FROM server_tags g
      WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'res'
  )
```

## Только мобильные операторы

```sql
  AND EXISTS (
      SELECT 1 FROM server_tags g
      WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'mobile'
  )
```

## По провайдеру, городу или номеру AS

```sql
  AND EXISTS (SELECT 1 FROM server_tags g
              WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'isp-megafon');

  AND EXISTS (SELECT 1 FROM server_tags g
              WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'city-novosibirsk');

  AND EXISTS (SELECT 1 FROM server_tags g
              WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'as-20473');
```

Последнее отсекает весь Vultr одной строкой, не перечисляя имена.

## По стране

```sql
SELECT line FROM servers
WHERE available = 1 AND excluded = 0 AND country = '🇩🇪';
```

## С минимальной скоростью

```sql
SELECT line FROM servers
WHERE available = 1 AND excluded = 0
  AND speed_mbps IS NOT NULL AND speed_mbps >= 5
ORDER BY speed_mbps DESC;
```

## Без серверов, которые сервис явно отклонил

```sql
  AND NOT EXISTS (
      SELECT 1 FROM server_tags g
      WHERE g.key = s.key AND g.tag = 'openrouter' AND g.passed = 0
  )
```

## Статистика: сколько серверов по тегам

```sql
SELECT tag, kind, COUNT(*) AS servers
FROM server_tags
WHERE passed = 1
GROUP BY tag, kind
ORDER BY servers DESC;
```

## Статистика: сколько провайдеров выучено

```sql
SELECT source, kind, COUNT(*) FROM asn_info GROUP BY source, kind;
```

## Очередь на проверку

```sql
SELECT status, COUNT(*) FROM check_queue GROUP BY status;
```

---

# Почему не `LIKE`

Раньше теги лежали строкой `'gemini-api,yandex'`, и фильтр писался так:

```sql
WHERE capabilities LIKE '%openai%'      -- плохо
```

Проблемы:

* подстрока ловит лишнее: `'%openai%'` совпадёт и с `openai-chat`;
* индекс не используется вообще — SQLite читает всю таблицу.

С `server_tags` то же самое пишется точным равенством и работает по
индексу:

```sql
WHERE t.tag = 'openai'                  -- хорошо
```

`PRIMARY KEY (tag, key)` — это и есть индекс, отсортированный по тегу.
Проверить план запроса:

```sql
EXPLAIN QUERY PLAN
SELECT s.line FROM servers s JOIN server_tags t ON t.key = s.key
WHERE t.tag = 'openrouter' AND t.passed = 1;
```

Должно быть `SEARCH t USING INDEX ... (tag=?)`, а не `SCAN`.
