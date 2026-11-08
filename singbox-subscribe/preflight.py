"""Предстартовые проверки: что должно быть готово ДО запуска конвейера.

Зачем это здесь
---------------
Установка у клиента падала не в одном месте, а в четырёх, и каждый раз
по-разному: то нет токена GitHub и деплой молча не срабатывал, то нет ключа
OpenRouter и цель в reachability_targets.json получала ПУСТОЙ заголовок
(см. _expand_env: неизвестная переменная -> ""), то браузерной проверке не
хватало логина и чекер тихо выключался с одной строчкой в логе.

Общее у всех этих случаев одно: НАСТРОЙКА НЕ СООТВЕТСТВУЕТ ЗАПУСКУ, а узнаёт
об этом пользователь сильно позже — по пустому whitelist.txt или по метке,
которой нет. Здесь те же самые условия проверяются ДО старта, и на каждую
непройденную проверку выдаётся инструкция: как добавить или как выключить.

Что проверяется — то, что реально будет вызвано
-----------------------------------------------
Список проверок не хардкожен: он выводится из двух источников.

  * Какие чекеры включены — config.yaml, singbox.enabled_checkers. Выключен
    api_service — его ключи не проверяются вовсе (иначе требовали бы ключ за
    то, что не запустится).
  * Какие цели требуют ключ — config/reachability_targets.json. Заголовок
    "$env:OPENROUTER_API_KEY:Bearer" сам объявляет свою зависимость, поэтому
    список ключей берётся из файла, а не из второго списка в коде. Добавили
    цель с новым ключом — проверка появилась сама.

Проверка НЕ роняет запуск
-------------------------
preflight ничего не останавливает: он возвращает отчёт, а решает вызывающий.
Это осознанно. Часть проверок — про необязательные вещи (деплой можно не
делать, браузерную проверку можно не включать), и запрещать старт из-за них
значило бы превратить подсказку в блокер. Единственный, кто вправе не
запуститься, — сам пользователь, прочитав инструкцию.

    python -m preflight            # показать отчёт
    python -m preflight --strict   # выход 1, если есть ошибки
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Корень пакета singbox-subscribe — исполняемый код (config/, script/, pipeline/).
ROOT = Path(__file__).resolve().parent

#: Корень репозитория — РАБОЧИЕ файлы пользователя: config/ (шаблоны и
#: подписки), source/ (база и списки), logs/, output/. Пути в config.yaml
#: пишутся относительно него, поэтому относительные пути считаются отсюда.
PROJECT_ROOT = ROOT.parent

#: Три состояния проверки. OK — можно работать; WARN — работать можно, но
#: что-то не будет сделано; FAIL — то, что включено, работать не будет.
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_ORDER = {FAIL: 0, WARN: 1, OK: 2}
_LABEL = {OK: "OK  ", WARN: "ВНИМ", FAIL: "ОШИБ"}


@dataclass
class Check:
    """Одна проверка: что проверяли, чем кончилось, что делать."""

    key: str
    title: str
    status: str = OK
    #: Что именно обнаружено — короткая строка для колонки «состояние».
    detail: str = ""
    #: Как починить. Пустая строка — чинить нечего.
    fix: str = ""
    #: Как выключить то, что требует этой проверки. Тоже необязательно.
    disable: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def lines(self) -> list[str]:
        """Строки отчёта для одной проверки."""
        out = ["[%s] %s — %s" % (_LABEL[self.status], self.title, self.detail)]
        if self.fix and self.status != OK:
            for i, line in enumerate(self.fix.splitlines()):
                out.append(("      -> " if i == 0 else "         ") + line)
        if self.disable and self.status != OK:
            out.append("         либо выключить: " + self.disable)
        return out


@dataclass
class Report:
    """Итог прогона: список проверок и удобные сводки."""

    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        """True, если нет ни одной ОШИБКИ (предупреждения не считаются)."""
        return not self.failed

    def sorted_checks(self) -> list[Check]:
        """Сначала то, что сломано: чинить начинают с верхнего."""
        return sorted(self.checks, key=lambda c: (_ORDER[c.status], c.title))

    def render(self, *, color: bool = False) -> str:
        """Текст отчёта. Без цветов по умолчанию: лог пишется в файл."""
        rows = self.sorted_checks()
        head = "Проверка готовности установки"
        out = [head, "=" * len(head), ""]
        for check in rows:
            out.extend(check.lines())
        out.append("")
        bad = len(self.failed)
        warn = len(self.warned)
        if bad:
            out.append(
                "Итог: ошибок %d, предупреждений %d. "
                "Запуск возможен, но перечисленное выше работать не будет." % (bad, warn)
            )
        elif warn:
            out.append("Итог: ошибок нет, предупреждений %d." % warn)
        else:
            out.append("Итог: все проверки пройдены (%d)." % len(rows))
        return "\n".join(out)

    def as_dict(self) -> dict:
        """Машинный вид — для --json и для вызова из start.py."""
        return {
            "ok": self.ok,
            "failed": len(self.failed),
            "warned": len(self.warned),
            "checks": [
                {
                    "key": c.key,
                    "title": c.title,
                    "status": c.status,
                    "detail": c.detail,
                    "fix": c.fix,
                    "disable": c.disable,
                }
                for c in self.sorted_checks()
            ],
        }


# ============================================================================
# ИСТОЧНИКИ ДАННЫХ: конфиг, цели, .env
# ============================================================================

#: Загружен ли .env в окружение этого процесса. Один раз на процесс:
#: повторный вызов безвреден, но и смысла не имеет.
_DOTENV_LOADED = [False]


def load_secrets() -> None:
    """Подтягивает .env в окружение ПРОЦЕССА.

    Это нужно, потому что ключи читаются не из конфига, а из окружения:
    script/reachability_check.py:_expand_env подставляет os.environ["GEMINI_API_KEY"]
    в заголовок запроса. Запущенный как модуль (python -m preflight) процесс
    окружения из .env не получает — start.py его загружает, а здесь вызова не
    было, и проверка объявляла ключи отсутствующими при живом .env.

    override=False: уже заданные в окружении переменные не перезаписываются —
    .env остаётся удобством, а не способом подменить боевой ключ.
    """
    if _DOTENV_LOADED[0]:
        return
    _DOTENV_LOADED[0] = True
    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001 — пакет необязателен, как и в deploy_config
        return
    for candidate in (PROJECT_ROOT / ".env", ROOT / ".env"):
        if candidate.exists():
            try:
                load_dotenv(candidate, override=False)
            except Exception:  # noqa: BLE001 — битый .env не должен ронять проверку
                pass
            return


def _settings():
    """Настройки из config.yaml. None, если конфиг не собрался.

    Падение здесь — это тоже результат проверки, а не повод уронить
    preflight: пользователь должен увидеть инструкцию, а не трейсбек.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    load_secrets()
    try:
        from config.settings import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001 — о проблеме сообщит проверка config
        return None


def _read(settings, path: str, default: Any = None) -> Any:
    """Значение настройки по короткому или полному пути.

    Проверки зовут setting() — так устроен объект из config.settings. Но в
    тестах и в вызывающем коде сюда приходит и голый Config, у которого
    есть только value(). Принимаем оба: иначе проверку нельзя вызвать
    иначе как через get_settings(), и её поведение нечем зафиксировать.
    """
    getter = getattr(settings, "setting", None)
    if callable(getter):
        return getter(path, default)
    getter = getattr(settings, "value", None)
    if callable(getter):
        return getter(path, default)
    return default


def _enabled_checkers(settings) -> set:
    """Какие чекеры включены в config.yaml (singbox.enabled_checkers)."""
    raw = _read(settings, "singbox.enabled_checkers", None)
    if raw is None:
        return set()
    if isinstance(raw, str):
        items = [x.strip().lower() for x in raw.replace(",", " ").split()]
    else:
        items = [str(x).strip().lower() for x in raw]
    return {x for x in items if x}


def _load_targets(settings) -> list:
    """Цели из reachability_targets.json. Пустой список, если файла нет."""
    if settings is None:
        return []
    # Путь берём из СХЕМЫ (reachability.urls_file). Старое имя
    # paths.reachability_targets_file — это ярлык переезда
    # (config/settings.py:_FIELD_MOVES), и обращение по нему через
    # setting() не находит поле: ярлыки работают для доступа точкой, а не
    # для строкового пути.
    raw = _read(settings, "reachability.urls_file", None)
    if not raw:
        raw = _read(settings, "paths.reachability_targets_file", "") or ""
    path = Path(str(raw or ""))
    if not path.is_absolute():
        # В config.yaml пути пишутся относительно корня репозитория.
        path = PROJECT_ROOT / path
    if not path.exists():
        # Второй шанс — обычное место в пакете: конфиг мог не донести путь.
        path = PROJECT_ROOT / "config" / "reachability_targets.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — о разборе сообщит проверка целей
        return []
    targets = data.get("targets") if isinstance(data, dict) else data
    return targets if isinstance(targets, list) else []


#: Заголовок вида "$env:ИМЯ" или "$env:ИМЯ:префикс". Разбор ОДИН в один с
#: script/reachability_check.py:_expand_env — иначе проверка требовала бы ключ
#: там, где запрос его не использует, и наоборот.
def _env_names_in(value) -> list:
    """Имена переменных окружения, упомянутые в строке/списке/словаре."""
    names: list = []
    if isinstance(value, str):
        pos = 0
        while True:
            pos = value.find("$env:", pos)
            if pos < 0:
                break
            rest = value[pos + 5:]
            name = ""
            for ch in rest:
                if ch.isalnum() or ch == "_":
                    name += ch
                else:
                    break
            if name:
                names.append(name)
            pos += 5
    elif isinstance(value, dict):
        for item in value.values():
            names.extend(_env_names_in(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            names.extend(_env_names_in(item))
    return names


def _target_key_names(target: dict) -> list:
    """Какие переменные окружения нужны ЭТОЙ цели.

    Смотрим и headers снаружи, и вложенный блок api: короткая форма цели
    (см. load_targets) держит заголовки именно там.
    """
    names = _env_names_in(target.get("headers") or {})
    api = target.get("api") if isinstance(target.get("api"), dict) else {}
    names.extend(_env_names_in(api.get("headers") or {}))
    return names


def _target_has_rules(target: dict) -> bool:
    """Есть ли у цели правила: такие цели проверяет api_service."""
    if target.get("rules"):
        return True
    api = target.get("api") if isinstance(target.get("api"), dict) else {}
    return bool(api.get("rules"))


#: Значения из .env, прочитанные один раз. Нужны потому, что ключи целей
#: подставляет _expand_env из ОКРУЖЕНИЯ, а .env разбирается отдельно —
#: config.py:read_env_file читает файл сам и в os.environ его не выкладывает.
#: Проверять только окружение значило бы объявлять ключи отсутствующими при
#: живом .env (и наоборот — не заметить ключ, лежащий только в .env).
_ENV_VALUES: dict = {}


def _env_file_values() -> dict:
    """Содержимое .env как словарь. Пустой словарь, если файла нет."""
    if _ENV_VALUES:
        return _ENV_VALUES
    for candidate in (PROJECT_ROOT / ".env", ROOT / ".env"):
        if not candidate.exists():
            continue
        try:
            from config.config import read_env_file

            _ENV_VALUES.update(read_env_file(candidate))
        except Exception:  # noqa: BLE001 — разбор не удался, останемся без .env
            pass
        break
    return _ENV_VALUES


def _seen(name: str) -> bool:
    """Задан ли ключ: в окружении ИЛИ в .env.

    Пустая строка — это НЕ «задан»: _expand_env вернёт "", и заголовок уйдёт
    пустым, а сервис ответит отказом по ключу. Ровно тот случай, ради которого
    проверка и написана.

    Смотрим оба источника, потому что рабочий процесс читает оба: config.py
    берёт секреты из файла .env, а _expand_env подставляет их уже из
    окружения (start.py выкладывает .env в окружение через load_dotenv).
    """
    if str(os.environ.get(name, "") or "").strip():
        return True
    return bool(str(_env_file_values().get(name, "") or "").strip())


def _mask(value: str) -> str:
    """Короткая безопасная пометка о секрете. Значение НЕ показываем."""
    text = str(value or "").strip()
    if not text:
        return "пусто"
    return "задан (%d симв.)" % len(text)


# ============================================================================
# ПРОВЕРКИ
# ============================================================================

def check_python() -> Check:
    """Версия Python: ниже 3.10 код не соберётся (аннотации вида str | None)."""
    v = sys.version_info
    detail = "Python %d.%d.%d" % (v.major, v.minor, v.micro)
    if v < (3, 10):
        return Check(
            "python", "Версия Python", FAIL, detail,
            fix="Нужен Python 3.10 или новее: в коде используется синтаксис "
                "объединения типов (str | None), который старшие версии не читают.\n"
                "Установите 3.12 и создайте окружение заново:\n"
                "  py -3.12 -m venv .venv",
        )
    return Check("python", "Версия Python", OK, detail)


def check_config_file() -> Check:
    """Есть ли config.yaml. Без него работают только значения по умолчанию."""
    path = PROJECT_ROOT / "config.yaml"
    if not path.exists():
        return Check(
            "config_file", "Файл config.yaml", FAIL, "не найден: %s" % path,
            fix="Создайте конфиг из шаблона:\n"
                "  cd singbox-subscribe\n"
                "  python -m pipeline config init\n"
                "Команда создаст config.yaml со всеми настройками и комментариями.",
        )
    return Check("config_file", "Файл config.yaml", OK, str(path))


def check_config_loads(settings) -> Check:
    """Собирается ли конфиг: опечатка в имени поля ловится здесь, не в работе."""
    if settings is None:
        return Check(
            "config_load", "Разбор config.yaml", FAIL, "конфиг не собрался",
            fix="Проверьте config.yaml: скорее всего опечатка в имени поля или "
                "неверный отступ.\nПоказать разобранный конфиг:\n"
                "  python -m pipeline config show",
        )
    problems = []
    try:
        # describe() возвращает все поля со значениями и источниками; заодно
        # это доказательство, что конфиг реально прочитан, а не собран по
        # умолчаниям в обход файла.
        described = settings.describe()
        if not described:
            problems.append("конфиг пуст")
    except Exception as exc:  # noqa: BLE001
        problems.append(str(exc))
    if problems:
        return Check(
            "config_load", "Разбор config.yaml", FAIL, "; ".join(problems),
            fix="Проверьте config.yaml и повторите:\n"
                "  python -m pipeline config show",
        )
    return Check("config_load", "Разбор config.yaml", OK, "прочитан")


def check_env_file() -> Check:
    """Есть ли .env. В нём ЖИВУТ ТОЛЬКО СЕКРЕТЫ, обычные настройки — в config.yaml."""
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return Check(
            "env_file", "Файл .env (секреты)", WARN, "не найден: %s" % path,
            fix="Создайте .env с ключами и токенами (обычные настройки туда НЕ "
                "пишутся — они в config.yaml):\n"
                "  GH_DEPLOY_TOKEN=...\n"
                "  GEMINI_API_KEY=...\n"
                "  OPENROUTER_API_KEY=...\n"
                "Файл в .gitignore и не коммитится.",
            disable="ничего не делать, если ни один сервис и деплой не нужны",
        )
    return Check("env_file", "Файл .env (секреты)", OK, str(path))


def check_deploy_mode(settings) -> Check:
    """Выбран ли хоть один способ публикации, когда деплой включён.

    Самая частая ошибка настройки: deploy.enabled: true, а ни local, ни repo
    не заданы. Деплой тогда молча ничего не делает — конвейер отрабатывает,
    файла нет, и непонятно, почему. Проверка называет это прямо.
    """
    if settings is None:
        return Check("deploy_mode", "Способ публикации", WARN, "конфиг не прочитан")
    if not bool(_read(settings, "deploy.enabled", False)):
        return Check("deploy_mode", "Способ публикации", OK,
                     "деплой выключен (deploy.enabled: false)")
    local = bool(_read(settings, "deploy.local", False))
    repo = str(_read(settings, "deploy.repo", "") or "").strip()
    if not local and not repo:
        return Check(
            "deploy_mode", "Способ публикации", FAIL,
            "деплой включён, но не выбран ни один способ",
            fix="Выберите, куда публиковать собранный конфиг:\n"
                "  deploy:\n"
                "    enabled: true\n"
                "    local: true              # писать файл на диск этой машины"
                "  (токены не нужны)\n"
                "    repo: owner/repo         # и/или пушить в GitHub\n\n"
                "Можно включить оба сразу: конфиг соберётся один раз и уйдёт\n"
                "и на диск, и в репозиторий.",
            disable="в config.yaml: deploy.enabled: false",
        )
    what = []
    if local:
        what.append("локально в %s" % _read(settings, "deploy.local_path", ""))
    if repo:
        what.append("в GitHub %s" % repo)
    return Check("deploy_mode", "Способ публикации", OK, " + ".join(what))


def check_github_deploy(settings) -> Check:
    """Деплой в GitHub: включён ли и есть ли токен на запись.

    Проверка нужна потому, что провал деплоя НЕ роняет конвейер (см.
    ExportStage._deploy: ошибки не поднимаются). Без этой проверки о
    отсутствующем токене узнают по отсутствию файла в репозитории.
    """
    if settings is None:
        return Check("github_deploy", "Деплой в GitHub", WARN, "конфиг не прочитан",
                     fix="Сначала починьте config.yaml (см. выше).")
    enabled = bool(_read(settings, "deploy.enabled", False))
    token = _read(settings, "secrets.gh_deploy_token", "")
    repo = str(_read(settings, "deploy.repo", "") or "").strip()
    if not enabled:
        return Check(
            "github_deploy", "Деплой в GitHub", OK,
            "выключен (deploy.enabled: false)",
        )
    if not repo:
        # Пустой repo при выключенном локальном деплое — это ошибка
        # (её называет check_deploy_mode), а при включённом — норма: значит
        # публикуем только на диск, и токен не нужен вовсе.
        if bool(_read(settings, "deploy.local", False)):
            return Check(
                "github_deploy", "Деплой в GitHub", OK,
                "не используется (deploy.repo пуст, публикация локальная)",
            )
        return Check(
            "github_deploy", "Деплой в GitHub", FAIL, "не указан репозиторий",
            fix="Укажите, куда публиковать конфиг:\n"
                "  deploy:\n    repo: пользователь/репозиторий\n\n"
                "Либо перейдите на локальный деплой — он не требует ни\n"
                "репозитория, ни токенов:\n"
                "  deploy:\n    local: true",
            disable="в config.yaml: deploy.enabled: false",
        )
    # Токен может лежать и в .env (обычный путь), и прийти значением из
    # конфига/переопределения — поэтому проверяем оба места, а не только
    # то, что успело доехать до Settings.
    if not (_seen("GH_DEPLOY_TOKEN") or _seen_env_or_value(token)):
        return Check(
            "github_deploy", "Деплой в GitHub", FAIL,
            "включён, но токена на запись нет",
            fix="Создайте токен GitHub с правом Contents: read and write и "
                "положите его в .env корня репозитория:\n"
                "  GH_DEPLOY_TOKEN=ghp_...\n"
                "Он нужен только для публикации конфига, читать репозитории "
                "может отдельный GH_READ_TOKEN.",
            disable="в config.yaml: deploy.enabled: false",
        )
    return Check("github_deploy", "Деплой в GitHub", OK,
                 "включён, репозиторий %s, токен %s" % (repo, _mask(token)))


def _dir_owner_hint(path) -> str:
    """Чей это каталог — одной строкой. Пусто, если выяснить не удалось.

    Нужно ровно затем, чтобы «Permission denied» не заставлял гадать: почти
    всегда каталог принадлежит root после запуска через sudo, и человеку
    достаточно увидеть это словом, а не догадываться по коду ошибки.
    """
    try:
        import pwd  # только POSIX: на Windows модуля нет
    except ImportError:
        return ""
    try:
        stat = path.stat()
        owner = pwd.getpwuid(stat.st_uid).pw_name
        current = pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001 — подсказка не стоит падения проверки
        return ""
    if owner == current:
        return ""
    return " Каталог принадлежит пользователю %s, а вы — %s." % (owner, current)


def check_local_deploy(settings) -> Check:
    """Локальный деплой: можно ли записать файл туда, куда указано.

    Локальному деплою НЕ нужны ни репозиторий, ни токены — только каталог с
    правом записи. Проверяем каталог заранее: иначе конфиг молча не
    появится, а причина выяснится по пустой папке.
    """
    if settings is None:
        return Check("local_deploy", "Локальный деплой", WARN, "конфиг не прочитан")
    if not bool(_read(settings, "deploy.enabled", False)):
        return Check("local_deploy", "Локальный деплой", OK,
                     "деплой выключен (deploy.enabled: false)")
    if not bool(_read(settings, "deploy.local", False)):
        return Check("local_deploy", "Локальный деплой", OK,
                     "выключен (deploy.local: false)")
    raw = str(_read(settings, "deploy.local_path", "") or "")
    if not raw:
        return Check(
            "local_deploy", "Локальный деплой", FAIL, "не задан каталог",
            fix="Укажите, куда писать конфиг:\n"
                "  deploy:\n    local_path: singbox-subscribe/output",
        )
    target = Path(raw)
    if not target.is_absolute():
        # В config.yaml пути пишутся относительно корня репозитория.
        target = PROJECT_ROOT / target
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        owner = _dir_owner_hint(target)
        return Check(
            "local_deploy", "Локальный деплой", FAIL,
            "нет доступа на запись: %s" % exc,
            fix="Каталог существует, но принадлежит не тому пользователю, "
                "от которого вы запускаете программу.%s\n\n"
                "Самый частый случай: репозиторий когда-то клонировали или "
                "запускали через sudo — тогда каталоги принадлежат root, а "
                "обычный пользователь писать в них не может. Убедиться:\n"
                "  ls -ld %s\n\n"
                "Вернуть себе владение (Linux/macOS):\n"
                "  sudo chown -R $USER:$USER %s\n\n"
                "Если это не подходит — укажите другой каталог:\n"
                "  deploy:\n    local_path: /путь/куда/есть/доступ"
                % (owner, target, target.parent),
            disable="в config.yaml: deploy.local: false",
        )
    return Check("local_deploy", "Локальный деплой", OK, "пишет в %s" % target)


def check_ip_source(settings) -> Check:
    """Источник IP нужен ТОЛЬКО если шаблон содержит плейсхолдер.

    Подстановка идёт лишь при наличии плейсхолдера в шаблоне (см.
    deploy_config._apply_server_ip): нет плейсхолдера — источник не читается
    вовсе. Поэтому пустой ip_source сам по себе не ошибка, и требовать его
    «на всякий случай» значило бы придираться к рабочей настройке.
    """
    if settings is None:
        return Check("ip_source", "Источник IP для шаблона", WARN, "конфиг не прочитан")
    if not bool(_read(settings, "deploy.enabled", False)):
        return Check("ip_source", "Источник IP для шаблона", OK,
                     "деплой выключен")
    ph = str(_read(settings, "deploy.ip_placeholder", "") or "").strip()
    if not ph:
        return Check("ip_source", "Источник IP для шаблона", OK,
                     "плейсхолдер не задан — подстановки нет")
    # Какие шаблоны будут собираться.
    templates = _configured_templates(settings)
    used_in = []
    try:
        for name in templates:
            path = PROJECT_ROOT / "config" / "templates" / name
            if path.exists() and ph in path.read_text(encoding="utf-8", errors="replace"):
                used_in.append(name)
    except Exception:  # noqa: BLE001 — не смогли прочитать = считаем, что нет
        pass
    src = str(_read(settings, "deploy.ip_source", "") or "").strip()
    if not used_in:
        return Check(
            "ip_source", "Источник IP для шаблона", OK,
            "в шаблонах нет %s — источник не нужен" % ph,
        )
    if not src:
        return Check(
            "ip_source", "Источник IP для шаблона", FAIL,
            "шаблон(ы) %s содержат %s, а источник не задан"
            % (", ".join(used_in), ph),
            fix="Плейсхолдер %s есть в шаблоне, значит адрес надо откуда-то\n"
                "взять. Укажите файл или http(s)-ссылку:\n"
                "  deploy:\n"
                "    ip_source: /path/to/current_ip.txt\n\n"
                "Источник — первая непустая строка файла или тело ответа."
                % ph,
            disable="уберите %s из шаблона (тогда адрес берётся как есть)" % ph,
        )
    # Источник задан — проверим, что он читается.
    try:
        if src.lower().startswith(("http://", "https://")):
            info = "ссылка (проверяется в момент деплоя)"
        else:
            p = Path(src)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            if not p.exists():
                return Check(
                    "ip_source", "Источник IP для шаблона", FAIL,
                    "файл не найден: %s" % p,
                    fix="Проверьте deploy.ip_source в config.yaml: файла нет "
                        "по указанному пути.",
                )
            first = ""
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    first = line.strip()
                    break
            if not first:
                return Check(
                    "ip_source", "Источник IP для шаблона", FAIL,
                    "файл пуст: %s" % p,
                    fix="В файле должна быть первая непустая строка с адресом.",
                )
            info = "файл %s -> %s" % (p, first)
    except Exception as exc:  # noqa: BLE001
        return Check("ip_source", "Источник IP для шаблона", FAIL,
                     "не удалось прочитать: %s" % exc)
    return Check("ip_source", "Источник IP для шаблона", OK,
                 "нужен для %s; %s" % (", ".join(used_in), info))


def _configured_templates(settings) -> list:
    """Какие шаблоны будут собираться: deploy.templates, иначе deploy.template."""
    raw = _read(settings, "deploy.templates", None)
    if isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = [x for x in str(raw or "").replace(",", " ").split() if x]
    if not items:
        one = str(_read(settings, "deploy.template", "") or "").strip()
        items = [one] if one else []
    # "all" разворачиваем в реальные имена файлов.
    if any(i.lower() == "all" for i in items):
        try:
            folder = PROJECT_ROOT / "config" / "templates"
            if folder.exists():
                items = [f.name for f in sorted(folder.glob("*.json"))]
        except Exception:  # noqa: BLE001
            pass
    return items


def _seen_env_or_value(value) -> bool:
    """Секрет задан: либо прямо в конфиге, либо взят из окружения.

    Секреты читаются из .env, и в схеме они помечены secret=True. Значение
    может прийти и через overrides, поэтому проверяем оба источника.
    """
    return bool(str(value or "").strip())


def check_github_read(settings) -> Check:
    """Токен на чтение: вшивается в ссылки скачивания подписок."""
    if settings is None:
        return Check("github_read", "Токен GitHub на чтение", WARN, "конфиг не прочитан")
    token = _read(settings, "secrets.gh_read_token", "")
    if not (_seen("GH_READ_TOKEN") or _seen_env_or_value(token)):
        return Check(
            "github_read", "Токен GitHub на чтение", WARN, "не задан",
            fix="Нужен, только если подписки лежат в приватном репозитории: "
                "токен с правом Contents: read подставляется в ссылки.\n"
                "  GH_READ_TOKEN=ghp_...\n"
                "Для публичных ссылок можно не задавать.",
        )
    return Check("github_read", "Токен GitHub на чтение", OK, _mask(token))


def check_api_service(settings) -> Check:
    """api_service: включён ли и есть ли ключи для тех целей, что он проверяет.

    Список ключей НЕ хардкожен: он читается из тех же целей, которые чекер
    будет проверять (browser/services.py:api_verdicts -> load_targets).
    Поэтому добавленная цель с новым ключом даёт новую проверку сама.
    """
    if settings is None:
        return Check("api_service", "Проверка сервисов по API", WARN, "конфиг не прочитан")
    enabled = _enabled_checkers(settings)
    if "api_service" not in enabled:
        return Check(
            "api_service", "Проверка сервисов по API", OK,
            "выключен (singbox.enabled_checkers)",
        )

    targets = _load_targets(settings)
    if not targets:
        return Check(
            "api_service", "Проверка сервисов по API", WARN, "нет файла целей",
            fix="Нужен config/reachability_targets.json со списком целей: без "
                "него проверять нечего и чекер выключится сам.",
        )

    # Ключи нужны только целям С ПРАВИЛАМИ: их и проверяет api_service.
    needed: dict = {}
    for target in targets:
        if not isinstance(target, dict) or not _target_has_rules(target):
            continue
        name = str(target.get("name") or target.get("tag") or "?").strip()
        for env_name in _target_key_names(target):
            needed.setdefault(env_name, []).append(name)

    if not needed:
        return Check(
            "api_service", "Проверка сервисов по API", OK,
            "включён; целей, требующих ключ, нет",
        )

    missing = sorted(n for n in needed if not _seen(n))
    present = sorted(n for n in needed if _seen(n))
    if missing:
        detail = "не хватает ключей: %s" % ", ".join(missing)
        fix_lines = []
        for env_name in missing:
            fix_lines.append("%s — нужен целям: %s" % (
                env_name, ", ".join(sorted(set(needed[env_name])))))
        return Check(
            "api_service", "Проверка сервисов по API", FAIL, detail,
            fix="Добавьте в .env корня репозитория:\n  %s\n\n%s"
                % ("\n  ".join("%s=..." % n for n in missing), "\n".join(fix_lines)),
            disable="уберите api_service из "
                    "singbox.enabled_checkers в config.yaml",
        )
    return Check(
        "api_service", "Проверка сервисов по API", OK,
        "включён; ключи на месте (%s)" % ", ".join(present),
    )


def check_browser_service(settings) -> Check:
    """browser_service: включён ли, и есть ли профиль с ВЫПОЛНЕННЫМ входом.

    Логин — не формальность: без профиля чекер выключается сам (см.
    browser_service.setup: «профиль не найден, чекер выключен»). Браузер без
    сессии увидит ту же страницу, что и обычный HTTP-запрос, поэтому проверка
    теряет смысл — о чём пользователь обязан узнать заранее.
    """
    if settings is None:
        return Check("browser_service", "Браузерная проверка", WARN, "конфиг не прочитан")
    enabled = _enabled_checkers(settings)
    if "browser_service" not in enabled:
        return Check(
            "browser_service", "Браузерная проверка", OK,
            "выключена (singbox.enabled_checkers)",
        )
    if not bool(_read(settings, "singbox.checkers.browser_service.enabled", True)):
        return Check(
            "browser_service", "Браузерная проверка", OK,
            "выключена (browser_service.enabled: false)",
        )

    service = str(_read(settings, "singbox.checkers.browser_service.service", "gemini") or "gemini")
    tag = str(_read(settings, "singbox.checkers.browser_service.tag", "") or service)

    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from browser import paths
        profile = paths.profile_dir(service)
    except Exception as exc:  # noqa: BLE001
        return Check(
            "browser_service", "Браузерная проверка", WARN,
            "не удалось определить папку профиля: %s" % exc,
        )

    if not profile.exists():
        return Check(
            "browser_service", "Браузерная проверка", FAIL,
            "включена, но профиля %s нет — вход не выполнялся" % service,
            fix="Выполните вход ОДИН раз (откроется окно браузера, "
                "войдите в %s вручную). Команда запускается ИЗ ПАПКИ "
                "singbox-subscribe:\n"
                "  cd singbox-subscribe\n"
                "  python browser.py login %s\n\n"
                "Профиль сохранится здесь: %s\n"
                "Папка лежит ВНЕ репозитория, cookies в git не попадут."
                % (service, service, profile),
            disable="уберите browser_service из "
                    "singbox.enabled_checkers в config.yaml",
        )

    # Профиль есть — проверяем, что в нём действительно есть cookies входа.
    cookies = _cookie_count(profile)
    if cookies == 0:
        return Check(
            "browser_service", "Браузерная проверка", FAIL,
            "профиль %s есть, но cookies входа в нём нет" % service,
            fix="Профиль создан, но вход не завершён. Повторите и войдите "
                "до конца (из папки singbox-subscribe):\n"
                "  python browser.py login %s\n\n"
                "Убедиться вручную, что доступ есть:\n"
                "  python browser.py check %s --proxy 127.0.0.1:ПОРТ"
                % (service, service),
            disable="уберите browser_service из "
                    "singbox.enabled_checkers в config.yaml",
        )

    # Работа рядом с api_service — НЕ дублирование и не повод для
    # предупреждения. У Google разные точки и разная политика доступности:
    # веб-приложение чата и API отвечают независимо, и сервер, пускающий в
    # одно, может не пускать в другое. Поэтому обе проверки ставят СВОИ
    # метки ("gemini-chat" и "gemini-api") и обе имеют смысл.
    both = "api_service" in _enabled_checkers(settings)
    peer = "%s-api" % service if service == "gemini" else service
    return Check(
        "browser_service", "Браузерная проверка", OK,
        "включена; профиль %s, cookies: %d, метка [%s]%s"
        % (service, cookies, tag,
           "; api_service рядом ставит свою метку [%s]" % peer if both else ""),
    )


def _cookie_count(profile: Path) -> int:
    """Сколько cookies лежит в профиле Chromium. 0 — значит вход не сделан.

    Считаем ЗАПИСИ, а не файлы: значения cookies не читаем и никуда не
    выводим — проверке достаточно факта «вход был».
    """
    import sqlite3

    db = Path(profile) / "Default" / "Network" / "Cookies"
    if not db.exists():
        # В части сборок Chromium файл лежит прямо в профиле.
        alt = list(Path(profile).glob("**/Cookies"))
        if not alt:
            return 0
        db = alt[0]
    try:
        # Копию не делаем: при закрытом браузере чтение безопасно. Если файл
        # занят, open в режиме только для чтения всё равно проходит.
        uri = "file:%s?mode=ro" % str(db).replace("?", "%3f").replace("#", "%23")
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            return int(conn.execute("select count(*) from cookies").fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — не смогли прочитать = считаем, что нет
        return 0


def check_checkers_exist(settings) -> Check:
    """Все ли имена в enabled_checkers существуют. Опечатка = молчаливый пропуск."""
    if settings is None:
        return Check("checkers_exist", "Имена чекеров", WARN, "конфиг не прочитан")
    enabled = _enabled_checkers(settings)
    if not enabled:
        return Check(
            "checkers_exist", "Имена чекеров", FAIL, "не задан ни один чекер",
            fix="Перечислите проверки в config.yaml:\n"
                "  singbox:\n    enabled_checkers: [url_probe, reachability, "
                "api_service]",
        )
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from pipeline.checkers.registry import build

        known = {getattr(c, "name", "") for c in build(selection=sorted(enabled))}
    except Exception as exc:  # noqa: BLE001
        return Check(
            "checkers_exist", "Имена чекеров", WARN,
            "не удалось собрать реестр: %s" % exc,
        )
    unknown = sorted(enabled - known)
    if unknown:
        return Check(
            "checkers_exist", "Имена чекеров", FAIL,
            "неизвестные имена: %s" % ", ".join(unknown),
            fix="Такого чекера нет, и он будет молча пропущен. Доступные: %s\n"
                "Исправьте singbox.enabled_checkers в config.yaml."
                % ", ".join(sorted(n for n in known if n)),
        )
    return Check("checkers_exist", "Имена чекеров", OK,
                 "включены: %s" % ", ".join(sorted(enabled)))


def check_singbox_binary(settings) -> Check:
    """Бинарник sing-box: без него не работает НИ ОДНА проверка.

    url_probe поднимает sing-box для каждой пачки, поэтому это единственная
    проверка, провал которой означает «не заработает ничего».
    """
    if settings is None:
        return Check("singbox_binary", "Бинарник sing-box", WARN, "конфиг не прочитан")
    # Настройка называется singbox.path (kind="binary") и уже умеет принимать
    # и папку поставки, и файл: resolve_binary сам ищет sing-box.exe внутри
    # каталога. Повторять этот обход здесь не нужно — иначе проверка и рабочий
    # код разошлись бы в том, какой путь считается годным.
    raw = _read(settings, "singbox.path", None)
    if raw is None:
        raw = _read(settings, "paths.sing_box_path", "") or ""
    if not raw:
        return Check(
            "singbox_binary", "Бинарник sing-box", FAIL, "путь не задан",
            fix="Скачайте sing-box и укажите путь в config.yaml — можно файл, "
                "можно папку поставки (бинарник найдётся сам):\n"
                "  singbox:\n    path: singbox-subscribe/sing-box",
        )
    # Разрешение пути отдаём той же функции, что и рабочий код
    # (config.resolve_binary): она знает и про bare-имя внутри пакета, и про
    # папку поставки. Своя арифметика здесь уже разошлась с ней один раз —
    # "sing-box.exe" проверка искала в корне репозитория, а конвейер брал из
    # пакета, и годная установка получала ложную ошибку.
    from config.config import resolve_binary

    path = resolve_binary(raw)
    if path is None or not Path(path).exists():
        return Check(
            "singbox_binary", "Бинарник sing-box", FAIL,
            "файла нет: %s" % (path if path is not None else raw),
            fix="Проверьте singbox.path в config.yaml: путь ведёт на "
                "несуществующий файл.\nСкачайте sing-box и положите его туда "
                "или исправьте путь.",
        )
    return Check("singbox_binary", "Бинарник sing-box", OK, str(path))


def check_db_dir(settings) -> Check:
    """Каталог базы: если его нельзя создать, конвейер упадёт на первом шаге."""
    if settings is None:
        return Check("db_dir", "Каталог базы", WARN, "конфиг не прочитан")
    raw = str(_read(settings, "paths.servers_db_file", "") or "")
    if not raw:
        return Check("db_dir", "Каталог базы", FAIL, "путь к базе не задан",
                     fix="Укажите paths.servers_db_file в config.yaml.")
    path = Path(raw)
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "db_dir", "Каталог базы", FAIL, "нет доступа на запись: %s" % exc,
            fix="Дайте права на запись в каталог:\n  %s" % parent,
        )
    exists = "есть" if path.exists() else "будет создана при первом запуске"
    return Check("db_dir", "Каталог базы", OK, "%s (%s)" % (parent, exists))


# ============================================================================
# ПРОГОН
# ============================================================================

def run_all(*, settings=None) -> Report:
    """Выполняет все проверки и возвращает отчёт.

    settings можно передать готовый, чтобы не читать config.yaml второй раз
    (так делает start.py). Без него конфиг читается здесь.
    """
    if settings is None:
        settings = _settings()
    report = Report()
    report.add(check_python())
    report.add(check_config_file())
    report.add(check_config_loads(settings))
    report.add(check_env_file())
    # Дальше — ровно то, что будет вызвано в процессе.
    report.add(check_singbox_binary(settings))
    report.add(check_checkers_exist(settings))
    report.add(check_db_dir(settings))
    report.add(check_deploy_mode(settings))
    report.add(check_local_deploy(settings))
    report.add(check_github_deploy(settings))
    report.add(check_ip_source(settings))
    report.add(check_github_read(settings))
    report.add(check_api_service(settings))
    report.add(check_browser_service(settings))
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI: показать отчёт. --strict делает ошибки кодом возврата."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m preflight",
        description="Проверка готовности установки: что настроено, а что нет.",
    )
    parser.add_argument("--strict", action="store_true",
                        help="вернуть код 1, если есть ошибки")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 — не критично, просто кодировка вывода
        pass

    # Секреты нужны и проверкам, и самим текстам: без этого ключи из .env
    # выглядели бы отсутствующими при живом файле.
    load_secrets()
    report = run_all()
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())
    if args.strict and not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
