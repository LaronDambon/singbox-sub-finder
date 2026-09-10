# Changelog

Все заметные изменения проекта документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

## [2.2.0]

### Added
- **Временный чс серверов** — защита рабочих конфигов от ложного бана в час-пики
  (метод работает вместе со stable и скрыто):
  - в базу добавлены колонки `fail_streak` (серия неудач подряд) и `temp_ban_until`
    (момент окончания бана); миграция старой базы — автоматически при первом открытии;
  - `TEMP_BAN_FAILS` (по умолчанию `2`) неудач подряд → сервер на `TEMP_BAN_HOURS`
    (по умолчанию `12`) часов исключается из проверки и из whitelist;
  - первая проверка после бана решающая: успех → восстановление, неудача →
    полноценный чс (`stable=FULL_BAN_STABLE=-2`);
  - серверы во временном чсе не попадают в `whitelist.txt`, `/api/whitelist`,
    `/api/servers` и merge-пул, даже если их stable выше порога экспорта;
  - CLI: `python -m script.server_store reset-temp-bans`.
- `PURGE_STABLE_BELOW` теперь по умолчанию `-1`: `stable=-1` остаётся в списках
  проверки, в полноценный чс уходит только `stable=-2`. Ошибочно исключённые
  ранее серверы (stable >= порога чс) один раз возвращаются в ротацию при первом
  открытии базы (маркер `servers.tempban-migrated.json`).
- Стейт-машина переходов состояния вынесена в чистую функцию
  `script.server_store.compute_next_state()` — её используют и БД (`record_results`),
  и цикл проверки (`urltest._evaluate_nodes`); в логах батчей и итоговом логе
  появились счётчики `temp-ban`/`temp_banned`, в `stats()` — зоны
  `full_ban`/`probation`/`testing`/`proven`/`temp_banned`.

### Changed
- **Конфигурация переведена на окружение**: `config/settings.py` удалён, вместо него
  тонкий `config/env.py` — все значения читаются из переменных окружения
  (`.env` в корне репозитория; приоритет: системное окружение > `.env` > дефолт).
  Добавлен полноценный шаблон `.env.example` со всеми переменными и комментариями.
  Новые env-переменные: `URLTEST_URL`, `URLTEST_TIMEOUT` (бывш. TIMEOUT),
  `URLTEST_BATCH_SIZE` (бывш. BATCH_SIZE), `TEMP_BAN_FAILS`, `TEMP_BAN_HOURS`,
  `FULL_BAN_STABLE`, `SING_BOX_PATH`, `SING_BOX_OUTPUT_DIR`, `CONFIG_TEMPLATE_DIR`;
  переименованные читаются с новыми именами во всех модулях
  (`main.py`, `urltest.py`, `downloader.py`, `core.py`, `country_check.py`,
  `reachability_check.py`, `server_store.py`, `gensub_api.py`, `deploy_config.py`).

## [2.1.1]

### Added
- Мульти-деплой в `deploy_config.py`: один запуск собирает и пушит конфиги по
  нескольким выбранным шаблонам — `--templates имя.json [еще.json ...]`,
  `--templates all`, интерактивный выбор `--select` или env
  `DEPLOY_TEMPLATES` (имена через запятую/пробел).
  - Итоговый файл в репозитории называется именем шаблона: из `--path` берётся
    только каталог (`--path configs/latest/config.json` + шаблон `sbc-1.14.json`
    -> `configs/latest/sbc-1.14.json`).
  - Whitelist-URI загружается один раз на всю пачку, ветка репозитория
    определяется один раз; сбой одного шаблона не прерывает остальные
    (`--stop-on-error` — прерывает).
- Авто-деплой в `urltest.py` поддерживает мульти-режим: при заданном
  `DEPLOY_TEMPLATES` (`all` или список имён через запятую/пробел) после цикла
  проверки деплоится каждый шаблон (файл = имя шаблона); без переменной —
  один `DEPLOY_TEMPLATE`, как раньше.
- Плейсхолдер текущего IP в шаблонах деплоя: в JSON вместо адреса пишется
  `{{SERVER_IP}}` (имя настраивается `DEPLOY_IP_PLACEHOLDER`), на этапе деплоя
  он замещается актуальным IP из `DEPLOY_IP_SOURCE` (`--ip-source`) — путь к
  локальному файлу (первая непустая строка) или http(s)-ссылка. Значение
  задаётся в `.env` (`DEPLOY_IP_SOURCE=/home/ray/ubuntu_server_ip/current_ip.txt`).

## [2.1.0] - 2026-09-03

### Added
- Авто-деплой собранного конфига в (private) GitHub-репозиторий после каждого цикла
  проверки: `deploy_config.py` (REST API без локального git).
  - Разделение ключей: деплой-токен (write) только для пуша, отдельный read-only
    токен вшивается в итоговую ссылку на скачивание из приватного репо.
  - Включение: `DEPLOY_ENABLED=1` в `.env`; настройки — `GH_DEPLOY_REPO`,
    `DEPLOY_TEMPLATE`, `DEPLOY_PATH`, `DEPLOY_CREATE_REPO`.
  - Новый шаблон сборки `config/templates/sbc-1.14.json` (под sing-box 1.14).
- Поддержка `python-dotenv` + `load_dotenv` в `start.py` (теперь и в `config/env.py`)
  (`.env`) в корне; приоритет системных переменных окружения сохранён.
- Фильтр конфигов с insecure TLS-fingerprint (`fp=/fingerprint=unsafe|none|disabled`)
  на этапе скачивания и при сборке merge — такие узлы sing-box отвергает и
  роняют весь батч проверки: `has_unsafe_fingerprint()` учитывает и query-параметры,
  и поле внутри base64-конфига `vmess://`.

### Changed
- `socks.py`: надёжный разбор адреса/порта (любые нецифровые хвосты у порта
  отрезаются); битые строки (`host:port&security=...`) пропускаются, парсер больше
  не роняет разбор и не создаёт ложных узлов.
- `ss.py`: строки `ss://...?security=reality&pbk=...&sid=...&flow=...` (на деле VLESS-Reality
  с ошибочным префиксом) распознаются и отбрасываются вместо ложного shadowsocks-узла.
- `urltest_template.json`/`countrytest_template.json`: убран `independent_cache`
  (не совместим с актуальным sing-box).
- Проверка URL по умолчанию возвращена на лёгкий `https://cp.cloudflare.com/gen_204`
  (тяжёлый speed.cloudflare.com остался закомментирован).

### Fixed
- Зависшие sing-box после таймаута больше не «съедают» следующий батч:
  свежий эфемерный inbound-порт на каждый запуск (`fresh_inbound_port()`).
- Надёжное завершение sing-box вместе с дочерними процессами на Windows
  (`taskkill /F /T`) — `_terminate_process_tree()`.
- Любой ненулевой/отсутствующий exit-код sing-box теперь пишется в ошибку с реальным
  выводом (до 80 строк, перекодировано в ASCII), а не теряется молча.
- Вывод sing-box с ANSI-escape и emoji больше не роняет файловый логгер в cp1251
  (`_clean_log_line()`).

### Removed
- `tests/` (тесты `test_server_store.py` и `__init__.py`) — вынесены из репозитория.

## [2.0.0] - 2026-08-25

### Breaking changes
- Состояние серверов перенесено из текстовых файлов whitelist.txt/blacklist.txt
  в центральную базу SQLite: singbox-subscribe/source/servers.db.
  При первом запуске база автоматически наполняется из старых файлов
  (значения stable сохраняются) — ручная миграция не требуется.

### Added
- script/server_store.py — центральное хранилище серверов (SQLite, WAL):
  фильтры по stable (export_lines/check_pool), purge_dead, reset_excluded,
  статистика; CLI: python -m script.server_store stats|export|purge|reset-excluded.
- HTTP API: GET /api/servers?min_stable=&max_stable=&limit= и /api/servers/stats;
  /api/whitelist теперь читает базу (stable > порога).
- Потолок стабильности STABLE_MAX (по умолчанию 5): умерший давний сервер
  выпадает из списков за конечное число провальных циклов.
- Тесты хранилища и чистых функций цикла: singbox-subscribe/tests/.

### Changed
- stable ограничен диапазоном [PURGE_STABLE_BELOW, STABLE_MAX]; зоны:
  stable < 0 — исключён из проверки, 0..1 — в ротации, > 1 — в экспорте списков.
- Страна сервера считается известной, если эмодзи есть в имени ИЛИ уже лежит
  в базе, — такие серверы не отправляются на дорогую проверку страны.
- Подписки скачиваются параллельно (SUB_DOWNLOAD_CONCURRENCY, по умолчанию 6).

### Performance
- Проверка страны переписана по мотивам Throne (throneproj/Throne):
  один процесс sing-box на батч прокси (inbound->outbound правила маршрутизации,
  каждый локальный порт привязан к своему конфигу) вместо процесса на каждый
  прокси; гео определяется одним лёгким запросом
  (Cloudflare trace -> api.ip.sb -> ipinfo.io) вместо списка speedtest.net,
  который блокировал датацентровые IP (~40% отказов).
  На живых данных: 6/6 успешных определений за ~10с против ~60% ранее.

### Removed
- Логика blacklist/prune_whitelist_by_blacklist (заменена флагом excluded в базе).
- GeoLite2-Country.mmdb больше не хранится в репозитории.

## [1.0.0] и ранее

История до внедрения версионирования — см. коммиты.
