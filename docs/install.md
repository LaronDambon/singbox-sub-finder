# Установка и запуск

## Автоматически

Загрузчик скачивает репозиторий и запускает установку:

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/LaronDambon/singbox-sub-finder/main/bootstrap.sh | bash
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/LaronDambon/singbox-sub-finder/main/bootstrap.ps1 | iex
```

Он проверяет Python 3 и git, клонирует репозиторий в `./singbox-subs-finder`
и запускает внутри `install.sh` / `install.ps1`. Повторный запуск обновляет
существующую установку, не затирая `config.yaml`, `.env` и базу.

Что можно задать заранее (переменными окружения): `SINGBOX_DIR` — куда
ставить, `SINGBOX_BRANCH` — ветка, `SINGBOX_REPO` — адрес репозитория.

## В уже скачанном репозитории

```bash
./singbox-subscribe/install.sh       # Linux / macOS
.\singbox-subscribe\install.ps1      # Windows
```

Ключи:

| Ключ | Что делает |
|---|---|
| `--browser` | сразу поставить chromium для `browser_service` (~150 МБ) |
| `--upgrade` | переустановить пакеты, даже если они уже стоят |
| `--recreate-venv` | пересоздать `.venv` с нуля |
| `--quiet` | меньше подробностей |

Установщик создаёт `.venv`, ставит зависимости, создаёт `config.yaml` и
`.env` (если их нет), готовит каталоги `source/`, `logs/`, `output/` и
показывает отчёт готовности. Существующие файлы он не трогает.

**Браузер по умолчанию не ставится.** Chromium нужен только чекеру
`browser_service`, а это тяжёлая загрузка. Поставьте его ключом `--browser`
или позже:

```bash
.venv/bin/python -m playwright install chromium          # Linux / macOS
.\.venv\Scripts\python.exe -m playwright install chromium  # Windows
```

На Linux без графической оболочки дополнительно нужны системные библиотеки
(`playwright install-deps chromium`, спросит пароль sudo).

## Вручную

Если автоматика не подходит:

```bash
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
```

## Обновление

```bash
./singbox-subscribe/update.sh       # Linux / macOS
.\singbox-subscribe\update.ps1      # Windows
```

Обновляет код (`git pull --ff-only`) и пакеты. `config.yaml`, `.env`,
`source/`, `logs/` и `config/` **не трогаются** — это данные установки, а не
код. Если в репозитории есть незакоммиченные правки, обновление скажет об
этом и остановится; ключ `--force` обновляет принудительно.

## Мастер настройки

```bash
./singbox-subscribe/setup.sh        # Linux / macOS
.\singbox-subscribe\setup.ps1       # Windows
```

Спрашивает только то, что нельзя решить за человека: какие чекеры запускать,
есть ли ключи для проверки сервисов, куда девать собранный конфиг. Ответы
пишутся в `config.yaml` и `.env`; комментарии в `config.yaml` сохраняются.
Ключ `--yes` принимает всё по умолчанию, без вопросов.

## Быстрый запуск

По умолчанию основной запуск — `start.py`, который поднимает API и, при необходимости, планировщик для `main.py`.

Запуск вручную:

```bash
python start.py
```

Одноразовый режим (поиск серверов -> очередь -> проверка -> экспорт):

```bash
python singbox-subscribe/main.py
```

Полезные ключи запуска:

```bash
python singbox-subscribe/main.py --checkers url_probe,country   # выбрать алгоритмы
python singbox-subscribe/main.py --no-discovery                 # только проверка и экспорт
python singbox-subscribe/main.py --interval 1800                # цикл каждые 30 минут
python singbox-subscribe/main.py --json                         # отчёт в JSON
```

Управление ядром — в разделе [Ядро pipeline](pipeline.md).

Перед первым запуском стоит проверить готовность установки — что настроено, а
что нет:

```bash
python singbox-subscribe/preflight.py
```
