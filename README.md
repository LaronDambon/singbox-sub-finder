# singbox-subs-finder

Собирает рабочие прокси из подписок, проверяет их через `sing-box` и отдаёт
готовый список или конфиг.

## Установка

Нужен **Python 3.10+** и **git**. Клонируем репозиторий и ставим зависимости —
всё живёт в одной папке.

Windows (PowerShell):

```powershell
cd $env:USERPROFILE
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux / macOS:

```bash
cd ~
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **Windows:** при установке Python отметьте галочку
> **«Add python.exe to PATH»**. Если `python --version` открывает Microsoft
> Store — Python не установлен.

Подробности, включая ограничения политики выполнения и установку браузера
для `browser_service` — в [документации по установке](docs/install.md).

## Настройка

Создайте `config.yaml` со всеми настройками и комментариями:

```bash
cd singbox-subscribe
python -m pipeline config init
```

Запускать не обязательно: умолчания в схеме уже рабочие. Секреты (токены и
ключи) лежат отдельно, в `.env` — скопируйте его из шаблона:

```bash
cp .env.example .env
```

## Обновление

```bash
git pull
pip install -r requirements.txt
```

`config.yaml`, `.env`, база и логи не трогаются — это ваши данные.

## Запуск

```bash
python start.py
```

`start.py` поднимает HTTP API и планировщик. Первый прогон долгий: он
проверяет всё, что нашлось в подписках. Дальше мёртвые серверы выпадают из пула
и цикл идёт заметно быстрее.

Разовый прогон без планировщика:

```bash
python singbox-subscribe/main.py
```

Результат: `source/whitelist.txt` — рабочие серверы. Их можно вставить в любой
клиент как есть, без сборки конфига для sing-box.

Ссылки на подписки — в `config/subs/urls.json`. Остальные настройки — в
`config.yaml`, у каждой есть комментарий; умолчания уже рабочие.

## Секреты

Всё, что является ключом или токеном, лежит **только** в `.env` (он в
`.gitignore` и не коммитится):

```bash
cp .env.example .env
```

| Переменная | Для чего | Нужна, если |
|---|---|---|
| `GH_DEPLOY_TOKEN` | публикация конфига в GitHub | включён GitHub-деплой |
| `GH_READ_TOKEN` | чтение приватного репозитория с подпиской | подписка приватная |
| `GEMINI_API_KEY` | проверка доступа к Gemini | включён чекер `api_service` |
| `OPENROUTER_API_KEY` | проверка доступа к OpenRouter | включён чекер `api_service` |

Больше в `.env` ничего не нужно: обычные настройки оттуда **не читаются**,
они живут в `config.yaml`.

### Нужно ли что-то заполнять

Ничего из перечисленного не обязательно, чтобы просто получить список серверов.

**Деплой.** По умолчанию выключен (`deploy.enabled: false`), и тогда
`GH_DEPLOY_TOKEN` не нужен. Публикация в GitHub включается так:

```yaml
deploy:
  enabled: true
  repo: ваш-логин/ваш-репозиторий
```

Если репозиторий не нужен, включите **локальный** деплой — тот же конфиг
просто пишется в файл рядом, без GitHub и без единого токена:

```yaml
deploy:
  enabled: true
  local: true
  local_path: output
```

**Проверка сервисов.** Чекер `api_service` включён по умолчанию и требует
`GEMINI_API_KEY` и `OPENROUTER_API_KEY`. Он отвечает на вопрос «работает ли
сервис с этого адреса», и без ключей его запросы отклоняются, так что либо
заведите бесплатные ключи, либо уберите чекер — вместе с ним уходят и ключи:

```yaml
singbox:
  enabled_checkers:
    - url_probe
    - country
    - reachability
```

Полный список доступных чекеров — `python -m pipeline checkers`.

**Браузерная проверка** (`browser_service`) выключена по умолчанию и требует
отдельной подготовки (`playwright install chromium` и вход в аккаунт). Она
нужна, только если API-проверки мало.

### Проверить, что всё настроено

```bash
python singbox-subscribe/preflight.py
```

Показывает, чего не хватает и как это добавить или выключить. Запуск ничего не
блокирует. `start.py` делает ту же проверку при каждом старте; отключается
через `PREFLIGHT=off`.

## Документация

Полное описание — в [docs/](docs/README.md): архитектура, все настройки,
проверки, база, деплой, разбор проблем.

## Лицензия

См. [LICENSE](LICENSE).
