# Фильтры при генерации конфига

Какие серверы попадут в итоговый конфиг, решает шаблон. Есть два способа
отбора, и они не мешают друг другу:

| Способ | Где живёт | Когда использовать |
|---|---|---|
| **`sql`** | внутри шаблона, в `outbounds` | основной: любой отбор по базе |
| `server_filter` | ключ шаблона | старый способ, оставлен для совместимости |

---

# Блок `sql` в шаблоне

Вместо списка серверов в `outbounds` ставится объект с ключом `sql`:

```json
{
  "tag": "alldeploy",
  "type": "urltest",
  "outbounds": [
    { "sql": "SELECT line FROM servers WHERE available = 1 AND excluded = 0" }
  ]
}
```

Скрипт выполняет запрос и **ставит на место этого объекта** строки, которые
вернула база. Остальные элементы списка остаются как есть:

```json
"outbounds": [
  { "sql": "SELECT ... LIMIT 5" },
  "alldeploy-reserved"
]
```

## Что скрипт берёт из результата

Колонку `line` (ссылку) — из неё парсится узел. Если `line` нет, берётся
первая колонка. Колонки `tag` и `key` тоже понимаются, но для конфига
нужна именно ссылка.

Каждая выбранная строка становится **узлом конфига**, а её тег —
участником группы. Поэтому сервер, которого нет в списке `uris`, всё равно
попадёт в конфиг: SQL — это источник, а не просто фильтр.

## Права только на чтение

Запрос выполняется на соединении `mode=ro` с `PRAGMA query_only = ON`.
`INSERT`, `UPDATE`, `DELETE` из шаблона физически невозможны.

## Плейсхолдер `alldeploy`

В тексте запроса можно писать `alldeploy` — перед выполнением он
заменяется меткой сервиса. Один и тот же шаблон работает на все группы:

```json
{ "sql": "SELECT s.line FROM servers s JOIN server_tags t ON t.key = s.key
          WHERE t.tag = 'alldeploy' AND t.passed = 1" }
```

Для группы `openrouter` это станет `t.tag = 'openrouter'`, для
`youtube` — `t.tag = 'youtube'` и так далее.

Работают и остальные подстановки: `alldeploy-best` → `<метка>-best`,
`alldeploy-reserved` → `<метка>-reserved`.

## Пример: лучшие 5 и резерв 6–26

Группа лучших:

```json
{
  "tag": "alldeploy",
  "type": "urltest",
  "outbounds": [
    { "sql": "SELECT s.line FROM servers s JOIN server_tags t ON t.key = s.key
              WHERE t.tag = 'alldeploy' AND t.kind = 'service' AND t.passed = 1
                AND s.available = 1 AND s.excluded = 0
              ORDER BY t.ping_ms IS NULL, t.ping_ms LIMIT 5" },
    "alldeploy-reserved"
  ],
  "url": "alldeploy",
  "interval": "2m"
}
```

Группа резерва:

```json
{
  "tag": "alldeploy-reserved",
  "type": "urltest",
  "outbounds": [
    { "sql": "SELECT s.line FROM servers s JOIN server_tags t ON t.key = s.key
              WHERE t.tag = 'alldeploy' AND t.kind = 'service' AND t.passed = 1
                AND s.available = 1 AND s.excluded = 0
              ORDER BY t.ping_ms IS NULL, t.ping_ms LIMIT 21 OFFSET 5" }
  ],
  "url": "alldeploy",
  "interval": "10m"
}
```

Тегов `-best` и `-reserve` больше нет: уровень сервера задаётся не тегом,
а местом в сортировке по задержке до цели.

## Пример: только чистые IP

Google и Claude не любят подключения с серверных хостингов:

```json
{ "sql": "SELECT s.line FROM servers s JOIN server_tags t ON t.key = s.key
          WHERE t.tag = 'alldeploy' AND t.kind = 'service' AND t.passed = 1
            AND s.available = 1 AND s.excluded = 0
            AND NOT EXISTS (SELECT 1 FROM server_tags g
                            WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'dc')
          ORDER BY t.ping_ms IS NULL, t.ping_ms LIMIT 5" }
```

## Пример: только мобильные операторы

```json
AND EXISTS (SELECT 1 FROM server_tags g
            WHERE g.key = s.key AND g.kind = 'geo' AND g.tag = 'mobile')
```

## Пример: без `{all}`

`{all}` подставляет все узлы конфига. С SQL он обычно не нужен: состав
группы задаёт запрос. Если `{all}` оставить рядом с `sql`, в группу
попадут и все узлы, и выбранные запросом.

Полный список готовых запросов — в [Базе данных](database.md#sql-рецепты-для-фильтров).

---

# `server_filter` (старый способ)

Ключ шаблона, отбирает серверы по данным базы: профилю доступности, стране,
региону, задержке, stable и протоколу. Это не то же самое, что старый
`filter`: тот ищет по **имени** узла, а `server_filter` — по колонкам базы.

```json
{
  "server_filter": {
    "capabilities": {"any": ["Global"], "none": ["speed-blocked"]},
    "exclude_countries": ["RU"],
    "max_ping_ms": 1500,
    "min_stable": 3
  }
}
```

Такой фильтр — это ровно «все серверы, у которых есть Global, кроме России».

| Ключ | Смысл |
|---|---|
| `capabilities` | объект `any` / `all` / `none` по тегам профиля |
| `include_countries` / `exclude_countries` | кодом (`RU`), флагом или названием (`Russia`, `Россия`) |
| `country_regions` / `exclude_country_regions` | `EU`, `ASIA`, `AMERICAS`, `AFRICA`, `OCEANIA`, `MIDDLE_EAST` |
| `min_stable` | минимальный счётчик жизней |
| `max_ping_ms` | максимальная задержка; серверы с **неизвестной** задержкой не проходят |
| `protocols` / `exclude_protocols` | `vless`, `trojan`, `vmess` и т.д. |
| `name_regex` / `exclude_name_regex` | регулярка по имени строки |
| `require_country` | `true` — оставить только серверы с известной страной |

Проверка строгая: неизвестный ключ, неизвестный регион, нераспознанная
страна и некомпилируемая регулярка **останавливают сборку** с внятным
сообщением.

Страна хранится в базе как эмодзи-флаг и заполнена у части серверов. У
остальных `country` пуст: `exclude_countries` их не тронет, а
`require_country: true` отсеет.

---

# Проверка перед публикацией

Сборка конфига отказывается выполняться, если `route.final` или любое
правило маршрута указывает на группу **без участников**: sing-box такие
группы считает нормальными и молча не выпускает наружу трафик.

Узлы, на которые не ссылается ни одна группа и ни одно правило, из конфига
удаляются: парсер отдаёт все живые серверы, но клиент их всё равно не
увидит.
