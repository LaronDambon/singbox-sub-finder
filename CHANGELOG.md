# Changelog

Все заметные изменения проекта документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

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
