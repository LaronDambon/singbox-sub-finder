# singbox-subs-finder

Собирает рабочие прокси из подписок, проверяет их через `sing-box` и отдаёт
готовый список или конфиг.

## Установка

Одной командой — она сама скачает репозиторий и всё поставит.

Linux / macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/LaronDambon/singbox-sub-finder/main/bootstrap.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/LaronDambon/singbox-sub-finder/main/bootstrap.ps1 | iex
```

Скрипт проверит Python и git, развернёт репозиторий, создаст окружение и
поставит зависимости.

| | Windows | Linux / macOS |
|---|---|---|
| **программа** | `%ProgramFiles%\singbox-subs-finder` | `/opt/singbox-subs-finder` |
| **данные** | `%LOCALAPPDATA%\singbox-subs-finder` | `~/.local/share/singbox-subs-finder` |

Программа и данные лежат **врозь**: обновление кода не задевает базу,
`config.yaml` и `.env`. На Windows установка в `Program Files` требует
прав администратора — запустите PowerShell от имени администратора.
Подробности и переменные для смены каталогов — в
[документации по установке](docs/install.md).

<details>
<summary>Если репозиторий уже скачан</summary>

```bash
./singbox-subscribe/install.sh       # Linux / macOS
.\singbox-subscribe\install.ps1      # Windows
```

А если хочется всё вручную — так, как было раньше:

```bash
git clone https://github.com/LaronDambon/singbox-sub-finder
cd singbox-sub-finder
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate      # Linux / macOS
pip install -r requirements.txt
```

</details>

## Настройка

Мастер спросит про чекеры, ключи и деплой — и запишет ответы в `config.yaml`
и `.env`. Запускать не обязательно: умолчания уже рабочие.

```bash
./singbox-subscribe/setup.sh       # Linux / macOS
.\singbox-subscribe\setup.ps1      # Windows
```

## Обновление

```bash
./singbox-subscribe/update.sh      # Linux / macOS
.\singbox-subscribe\update.ps1     # Windows
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
