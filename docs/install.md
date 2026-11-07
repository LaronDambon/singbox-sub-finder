# Установка и запуск

Установка — обычная: клонируем репозиторий и ставим зависимости. Всё живёт в
**одной папке**, никаких установщиков и системных каталогов.

## Что нужно

| | |
|---|---|
| **Python 3.10+** | https://www.python.org/downloads/ — при установке отметьте **«Add python.exe to PATH»** |
| **git** | https://git-scm.com/download/win |

Проверить, что всё на месте:

```powershell
python --version     # Windows
python3 --version    # Linux / macOS
git --version
```

> **Windows:** если `python --version` открывает Microsoft Store или пишет
> «не найдено» — Python не установлен либо не попал в `PATH`. Поставьте его
> заново с галочкой «Add python.exe to PATH».

## Установка

### Windows (PowerShell)

```powershell
cd $env:USERPROFILE
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Если активация окружения запрещена политикой выполнения:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Либо вообще без активации — просто зовите интерпретатор из окружения напрямую:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Linux / macOS

```bash
cd ~
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Если `python3 -m venv` ругается на отсутствующий модуль:

```bash
sudo apt install python3-venv python3-pip     # Debian / Ubuntu
sudo dnf install python3-pip                  # Fedora
```

## Конфигурация

Создать `config.yaml` со всеми настройками и комментариями:

```bash
cd singbox-subscribe
python -m pipeline config init
```

Файл появится в корне репозитория. Запускать команду не обязательно:
умолчания в схеме уже рабочие, и программа стартует без конфига. Но с файлом
видно, что и где настраивается.

Секреты (токены и ключи) лежат **отдельно**, в `.env` — этот файл в
`.gitignore` и не коммитится:

```bash
cp .env.example .env
```

Дальше впишите в него то, что нужно именно вам:

```
GH_DEPLOY_TOKEN=...      # только для деплоя в GitHub
GH_READ_TOKEN=...        # read-only токен, вшивается в ссылки
GEMINI_API_KEY=...       # для api_service
OPENROUTER_API_KEY=...   # для api_service
```

Обычные настройки в `.env` не пишутся — только секреты. Всё остальное
живёт в `config.yaml`.

## Структура

Всё лежит в одной папке — и код, и ваши файлы:

```
singbox-sub-finder/
  start.py, requirements.txt       точка входа и зависимости
  config.yaml                      настройки (создаётся командой выше)
  .env                             секреты (в .gitignore)
  config/subs/urls.json            список ссылок на подписки
  config/templates/*.json          шаблоны конфигурации для деплоя
  config/reachability_targets.json цели проверки reachability
  source/servers.db                центральная база
  source/whitelist.txt, merge.txt  выгрузки
  source/downloaded/               скачанные подписки
  logs/                            логи
  output/                          результат локального деплоя
  singbox-subscribe/               ПАКЕТ: код
```

Пути в `config.yaml` пишутся **относительно корня репозитория** —
`config/templates`, а не `singbox-subscribe/config/templates`.

## Обновление

```bash
git pull
.venv/bin/pip install -r requirements.txt        # Windows: .\.venv\Scripts\pip.exe
```

Ваши файлы при этом не трогаются: `config.yaml`, `.env`, `source/`,
`logs/` и `config/` — это данные, а не код.

## Браузер для проверки сервисов

Chromium нужен только чекеру `browser_service`, а он тяжёлый. По умолчанию
он выключен. Поставить, когда понадобится:

```bash
.venv/bin/python -m playwright install chromium                    # Linux / macOS
.\.venv\Scripts\python.exe -m playwright install chromium          # Windows
```

На Linux без графической оболочки дополнительно нужны системные библиотеки
(`playwright install-deps chromium`, спросит пароль sudo).

## Проверка готовности

Перед первым запуском стоит посмотреть, что настроено, а что нет:

```bash
python singbox-subscribe/preflight.py
```

## Запуск

По умолчанию основной запуск — `start.py`, который поднимает API и, при
необходимости, планировщик для `main.py`.

```bash
python start.py
```

Одноразовый режим (поиск серверов -> очередь -> проверка -> экспорт):

```bash
python singbox-subscribe/main.py
```

Полезные ключи:

```bash
python singbox-subscribe/main.py --checkers url_probe,country   # выбрать алгоритмы
python singbox-subscribe/main.py --no-discovery                 # только проверка и экспорт
python singbox-subscribe/main.py --interval 1800                # цикл каждые 30 минут
python singbox-subscribe/main.py --json                         # отчёт в JSON
```

Управление ядром — в разделе [Ядро pipeline](pipeline.md).
