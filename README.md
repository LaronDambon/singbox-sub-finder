## Введение
- Собирает списки серверов из источников в `config/subs/urls.json`.
- Объединяет, очищает и генерирует `source/merge.txt`.
- Запускает `urltest` через `sing-box`; результат (+1/-1 к `stable`) пишется в центральную базу `source/servers.db` (SQLite).
- Списки — это проекции базы по фильтру `stable`: `stable > WHITELIST_EXPORT_MIN_STABLE` → `whitelist.txt` (и `/api/whitelist`), `stable < PURGE_STABLE_BELOW` → удаление из проверочного списка (исключаются из пула проверки).
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
- Основные пути и настройки — [singbox-subscribe/config/settings.py](singbox-subscribe/config/settings.py#L1).
- Файлы источников URL: `singbox-subscribe/config/subs/*.json`.
- Шаблоны конфигураций: `singbox-subscribe/config/templates/`.

## sing-box
- По умолчанию в папке репозитория присутствуют собранные бинарные файлы `sing-box` для Linux и Windows: `sing-box/sing-box` и `sing-box/sing-box.exe`.

- Вы можете указать путь в `SING_BOX_PATH` в [config/settings.py](singbox-subscribe/config/settings.py#L1) или установить переменную окружения `SING_BOX_PATH`. 

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
| `stable <= -1` | мёртвый: исключён из проверочного пула (`excluded=1`) |
| `stable 0..1` | в ротации проверки, в списки не попадает |
| `stable >= 2` | подтверждённый: попадает в `whitelist.txt` и `/api/whitelist` |

Пороги настраиваются переменными окружения:
- `WHITELIST_EXPORT_MIN_STABLE` (по умолчанию `1`) — экспорт списков: только `stable >` порога;
- `PURGE_STABLE_BELOW` (по умолчанию `0`) — удаление из проверочного списка: все со `stable <` порога.

CLI управления базой (из папки `singbox-subscribe`):

```bash
python -m script.server_store stats                 # статистика по зонам stable
python -m script.server_store export --min-stable 1 # вывести список stable > 1
python -m script.server_store export --max-stable 0 # вывести мёртвые (stable < 0)
python -m script.server_store purge --below 0       # исключить мёртвые из проверки (--hard — удалить физически)
python -m script.server_store reset-excluded        # вернуть исключённые в ротацию (stable -> 0)
```

HTTP API (дополнительно к `/api/whitelist`, который теперь читает базу):
- `GET /api/servers/stats` — статистика базы;
- `GET /api/servers?min_stable=1` — выборка `stable > 1`; `?max_stable=0` — `stable < 0`; поддержан `limit`.

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