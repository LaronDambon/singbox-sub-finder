## Введение
- Собирает списки серверов из источников в `config/subs/urls.json`.
- Объединяет, очищает и генерирует `source/merge.txt`.
- Запускает `urltest` через `sing-box` для получения ping/доступности и формирует whitelist/blacklist.
- Первая полная проверка будет долгой 200к+ серверов. Далее все нерабочие попадут в blacklist и поиск рабочих конфигов будет сильно быстрее. 
- Сохранят отфильтрованные сервера `source/whitelist.txt` их можно использовать в любом клиенте без генерации конфига для sing-box.
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
