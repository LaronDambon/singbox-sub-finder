# Changelog

Все заметные изменения проекта документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

## [2.1.0] - 2026-09-03

### Added
- Авто-деплой собранного конфига в (private) GitHub-репозиторий после каждого цикла
  проверки: `deploy_config.py` (REST API без локального git).
  - Разделение ключей: деплой-токен (write) только для пуша, отдельный read-only
    токен вшивается в итоговую ссылку на скачивание из приватного репо.
  - Включение: `DEPLOY_ENABLED=1` в `.env`; настройки — `GH_DEPLOY_REPO`,
    `DEPLOY_TEMPLATE`, `DEPLOY_PATH`, `DEPLOY_CREATE_REPO`.
  - Новый шаблон сборки `config/templates/sbc-1.14.json` (под sing-box 1.14).
- Поддержка `python-dotenv` + `load_dotenv` в `start.py` и `settings.py`
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
