## Введение
- Собирает списки серверов из источников в `config/subs/urls.json`.
- Объединяет, очищает и генерирует `source/merge.txt`.
- Запускает `urltest` через `sing-box`; результат (+1/-1 к `stable`) пишется в центральную базу `source/servers.db` (SQLite).
- Списки — это проекции базы по фильтру `stable`: `stable > WHITELIST_EXPORT_MIN_STABLE` → `whitelist.txt` (и `/api/whitelist`), `stable < PURGE_STABLE_BELOW` → полноценный чс (исключаются из пула проверки).
- **Временный чс** (защита рабочих серверов от час-пиков): `TEMP_BAN_FAILS` неудач подряд отправляют сервер в бан на `TEMP_BAN_HOURS` часов — он исчезает из проверки и из whitelist, а первая проверка после бана решает его судьбу: успех → восстановление, неудача → полноценный чс (`stable=-2`).
- Первая полная проверка будет долгой (200к+ серверов). Далее мёртвые серверы выпадают из пула проверки и поиск рабочих конфигов идёт значительно быстрее.
- Отфильтрованные сервера сохраняются в `source/whitelist.txt`, их можно использовать в любом клиенте без генерации конфига для sing-box.
- Либо можно сразу же собрать автономный конфиг для запуска sing-box.

## Установка (локально)
1. Клонируйте репозиторий:

```bash
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
```

2. Создайте виртуальное окружение и активируйте его:

```bash
# Windows
python -m venv .venv
.\.venv\Scripts\activate
```
```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

3. Установите зависимости:

```bash
pip install -r requirements.txt
```

## Быстрый запуск
По умолчанию основной запуск — `start.py`, который поднимает API и, при необходимости, планировщик для `main.py`.

Запуск вручную:

```bash
python start.py
```

Одноразовый режим (сборка и urltest):

```bash
python singbox-subscribe/main.py
```

## Конфигурация
- Все настройки — переменные окружения: скопируйте `.env.example` в `.env` и заполните
  свои значения. Читаются модулем [singbox-subscribe/config/env.py](singbox-subscribe/config/env.py#L1);
  приоритет: системное окружение (setx/export) > `.env` > значение по умолчанию.
- Файлы источников URL: `singbox-subscribe/config/subs/*.json`.
- Шаблоны конфигураций: `singbox-subscribe/config/templates/`.

## sing-box
- По умолчанию в папке репозитория присутствуют собранные бинарные файлы `sing-box` для Linux и Windows: `sing-box/sing-box` и `sing-box/sing-box.exe`.

- Вы можете указать путь в переменной окружения `SING_BOX_PATH` (или в `.env`); по умолчанию используется `sing-box/sing-box.exe` (Windows) или `sing-box/sing-box` (Linux/macOS).

## Определение страны сервера (country_check)
Реализация по мотивам [Throne](https://github.com/throneproj/Throne) (core/server/test_utils/speedtest_utils.go):
- **один** процесс sing-box на батч прокси: каждому прокси — свой mixed-inbound на 127.0.0.1 и правило маршрутизации inbound → outbound, т.е. локальный порт жёстко привязан к своему конфигу (у Throne то же самое через outbound.DialContext в одном запущенном ядре);
- потоки Python параллельно стучатся в свои порты (COUNTRY_CHECK_CONCURRENCY, по умолчанию 8; в Throne — countryConcurrency=5);
- страна определяется одним лёгким запросом через цепочку: Cloudflare cdn-cgi/trace → api.ip.sb/geoip → ipinfo.io/json (вместо списка серверов speedtest.net, который блокирует датацентровые IP и давал ~40% отказов);
- ISO-код сразу превращается в эмодзи флага и пишется в базу (колонка country), поэтому повторных проверок для известных стран нет.

Если страна уже известна (эмодзи в имени или запись в базе) — сервер вообще не отправляется на проверку.

## Центральная база серверов (servers.db)
Единственный источник правды о серверах и их стабильности — SQLite-база `singbox-subscribe/source/servers.db` (модуль [script/server_store.py](singbox-subscribe/script/server_store.py), класс `ServerStore`).

При первом запуске база автоматически наполняется из старых `whitelist.txt`/`blacklist.txt` (значения stable сохраняются).

Зоны stable (при настройках по умолчанию):

| Диапазон | Значение |
|---|---|---|
| `stable = -2` | полноценный чс: исключён из проверочного пула (`excluded=1`) |
| `stable = -1` | «подозрительный»: остаётся в списках проверки, в whitelist не попадает |
| `stable 0..1` | в ротации проверки, в списки не попадает |
| `stable >= 2` | подтверждённый: попадает в `whitelist.txt` и `/api/whitelist` |

Пороги настраиваются переменными окружения:
- `WHITELIST_EXPORT_MIN_STABLE` (по умолчанию `1`) — экспорт списков: только `stable >` порога;
- `PURGE_STABLE_BELOW` (по умолчанию `-1`) — полноценный чс: из проверки исключаются все со `stable <` порога.

### Временный чс (защита от час-пиков)
В периоды высокой нагрузки рабочие серверы могут провалить пару проверок подряд.
Чтобы такие серверы не вылетали насовсем, работает «временный чс» (скрыто, вместе со stable):
- `TEMP_BAN_FAILS` (по умолчанию `2`) неудач **подряд** → сервер попадает во временный чс:
  `TEMP_BAN_HOURS` (по умолчанию `12`) часов он не проверяется и не попадает в whitelist
  (даже если его stable формально выше порога экспорта);
- первая проверка после окончания бана решающая: успех → бан и серия неудач сбрасываются,
  сервер возвращается в работу; неудача → полноценный чс (`stable=FULL_BAN_STABLE=-2`).

Состояние хранится в колонках `fail_streak` (серия неудач подряд) и `temp_ban_until`
(момент окончания бана); при первом открытии старой базы они добавляются автоматически,
а ошибочно исключённые ранее серверы (stable >= порога чс) один раз возвращаются в ротацию.

CLI управления базой (из папки `singbox-subscribe`):

```bash
python -m script.server_store stats                 # статистика по зонам stable
python -m script.server_store export --min-stable 1 # вывести список stable > 1
python -m script.server_store export --max-stable -1 # вывести полноценный чс (stable < -1)
python -m script.server_store purge --below -1       # исключить полноценный чс из проверки (--hard — удалить физически)
python -m script.server_store reset-excluded         # вернуть исключённые в ротацию (сброс состояния)
python -m script.server_store reset-temp-bans        # снять все временные чс
```

HTTP API (дополнительно к `/api/whitelist`, который теперь читает базу):
- `GET /api/servers/stats` — статистика базы;
- `GET /api/servers?min_stable=1` — выборка `stable > 1`; `?max_stable=-1` — `stable < -1` (полноценный чс); поддержан `limit`. Серверы в действующем временном чсе не отдаются.

## Логи
- Логи записываются в папку `logs/`.
- Логирование настроено с ротацией и разделением по уровням: `all.log`, `info.log`, `debug.log`, `warnings.log`, `fatal.log`.

## Автозапуск (Linux)
- Для непрерывной работы можно добавить системный unit (systemd) или cron, который запускает `start.py`.

Пример systemd unit (создайте `/etc/systemd/system/singbox-sub-finder.service`):

```bash
sudo nano /etc/systemd/system/singbox-subscribe.service
```
Вставьте следующее, и ОБЯЗАТЕЛЬНО замените USER на свой.

```ini
[Unit]
Description=Sing-box subscribe runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=USER
WorkingDirectory=/home/USER/singbox-sub-finder
ExecStart=/home/USER/singbox-sub-finder/.venv/bin/python start.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

После создания:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now singbox-sub-finder
```


## Полезные ссылки
- Репозиторий по шаблонам: https://github.com/AvenCores/goida-vpn-configs
- Релизы sing-box: https://github.com/SagerNet/sing-box/releases

## Проблемы при запуске sing-box
- При запуске `sing-box` возможны следующие распространённые проблемы:
	- неверная архитектура/платформа бинарника (Windows `.exe` не запустится в Linux);
	- отсутствие права на исполнение у файла (`chmod +x sing-box` на Linux);
	- порт, который использует `sing-box`, может быть заблокирован фаерволлом. Для Ubuntu с `ufw` откройте порт, который вы используете, например:

```bash
sudo ufw allow <PORT>/tcp
```

	- если `sing-box` должен слушать привилегированный порт (<1024), убедитесь, что запуск идёт от пользователя с нужными правами.

Если понадобятся подсказки по диагностике ошибок запуска — пришлите вывод консоли или логи из `logs/`.

## Вклад
- Пулл-реквесты приветствуются. Открывайте issues для багов или обсуждения новых фич.

## Лицензия
- GNU General Public License v3.0