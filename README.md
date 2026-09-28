## Введение
- Собирает списки серверов из источников в `config/subs/urls.json`.
- Объединяет, очищает и генерирует `source/merge.txt`.
- Запускает `urltest` через `sing-box`; результат (`stable`) пишется в центральную базу `source/servers.db` (SQLite).
- **Whitelist** (`whitelist.txt`, `/api/whitelist`) — серверы, пинговавшиеся в ПОСЛЕДНЕЙ проверке (`available=1`): пинганулся → добавляется в список, не пинганулся → не добавляется.
- **«Щит» от удаления**: сервер, пинговавшийся хотя бы раз (`ever_pinged=1`), защищён от удаления на `SHIELD_CYCLES=96` проверок. Каждый успех ставит `stable` не ниже `96`, каждая неудача списывает `1`.
- Новый сервер до первого удачного пинга имеет `stable=NEW_SERVER_STABLE=5`. Нижний порог `stable` — `0`: в проверку попадают **все** серверы со `stable >= 0`; дойдя до `-1`, сервер больше не импортируется из базы.
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

При первом запуске база автоматически наполняется из старых `whitelist.txt`/`blacklist.txt` (whitelist-серверы считаются пинговавшимися и получают щит, blacklist — умершие).

Зоны stable (при настройках по умолчанию, порог `0`, мёртвый `-1`):

| Диапазон | Значение |
|---|---|
| `stable = -1` | умерший: не импортируется в проверку (`excluded=1` после purge) |
| `stable = 0` | на границе: одна неудачная проверка до смерти |
| `stable 1..95` | живёт за счёт остатка «щита» |
| `stable >= 96` | под полным щитом после удачного пинга; в whitelist по результату последней проверки (`available=1`) |

Пороги настраиваются переменными окружения:
- `PURGE_STABLE_BELOW` (по умолчанию `0`) — нижний порог: из проверки исключаются все со `stable <` порога (то есть умершие `stable=-1`);
- `NEW_SERVER_STABLE` (по умолчанию `5`) — stable нового сервера до первого удачного пинга;
- `SHIELD_CYCLES` (по умолчанию `96`) — «щит» после удачного пинга: минимум неудачных проверок, которые сервер переживёт.

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