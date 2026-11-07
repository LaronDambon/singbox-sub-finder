"""Где лежат программа и данные. Одно правило на весь проект.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ
----------------------
Раньше всё было в одном месте: код, config.yaml, база, логи и выходные
файлы лежали в одном каталоге, куда скрипт себя и клонировал. Работало это
ровно до первой системной установки: в C:/Program Files и в /opt писать
нельзя без прав администратора, а обновление программы затирало бы базу
вместе с настройками.

Поэтому мест теперь ДВА, и они разной природы:

    ПРОГРАММА (код)   Windows: %ProgramFiles%/singbox-subs-finder
                      Linux:   /opt/singbox-subs-finder
                      macOS:   /usr/local/opt/singbox-subs-finder

    ДАННЫЕ (изменяемые)  Windows: %LOCALAPPDATA%/singbox-subs-finder
                         Linux:   $XDG_DATA_HOME/singbox-subs-finder
                                  (~/.local/share/singbox-subs-finder)
                         macOS:   ~/Library/Application Support/singbox-subs-finder

Программа — то, что можно стереть и поставить заново: .git, .venv, код.
Данные — то, что терять нельзя: config.yaml, .env, база серверов, подписки,
шаблоны, логи и выходные файлы. Обновление трогает первое и не касается
второго, и для второй папки прав администратора не нужно вовсе.

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
--------------------
Обе папки переопределяются, и это не украшение: тесты, перенос установки
на другой диск и второй экземпляр программы на той же машине без этого
невозможны.

    SINGBOX_PROGRAM_DIR   куда поставлена программа
    SINGBOX_DATA_DIR      где лежат изменяемые данные

Путь к config.yaml и .env тоже можно задать напрямую — SINGBOX_CONFIG_FILE
и SINGBOX_ENV_FILE. Это нужно службам и контейнерам, где конфиг приезжает
извне и лежать в профиле пользователя не должен.

ЧТО ИМЕННО СЧИТАЕТСЯ ДАННЫМИ
----------------------------
data_root() — корень, а раскладка внутри него одна и та же:

    config.yaml      настройки (коммитить нечего — файл вне репозитория)
    .env             секреты
    config/subs/     urls.json и прочие подписки
    config/templates/ шаблоны конфига для деплоя
    config/reachability_targets.json  цели проверки
    source/          база серверов, merge.txt, whitelist.txt, blacklist.txt
    logs/            логи
    output/          собранные конфиги

Прежние установки («всё в клоне репозитория») продолжают работать: если
config.yaml нашёлся рядом с пакетом, данные берутся оттуда — см.
resolve_data_root().
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Переменная окружения: куда поставлена программа.
ENV_PROGRAM_DIR = "SINGBOX_PROGRAM_DIR"
#: Переменная окружения: где лежат изменяемые данные.
ENV_DATA_DIR = "SINGBOX_DATA_DIR"
#: Переменная окружения: путь к config.yaml напрямую.
ENV_CONFIG_FILE = "SINGBOX_CONFIG_FILE"
#: Переменная окружения: путь к .env напрямую.
ENV_ENV_FILE = "SINGBOX_ENV_FILE"

#: Имя папки в системных каталогах.
APP_NAME = "singbox-subs-finder"

#: Папка пакета (здесь лежит этот файл, он же — корень кода).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: Признак: пакет лежит внутри git-клона, а не в системной папке.
#: Нужен, чтобы отличить «поставлено системно» от «запущено из репозитория».
REPO_MARKERS = (".git", "requirements.txt", "bootstrap.ps1", "bootstrap.sh")


def is_windows() -> bool:
    return os.name == "nt"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _local_appdata() -> Path:
    """%LOCALAPPDATA% с разумным запасным вариантом.

    Переменная есть почти всегда, но в службах и в урезанном окружении её
    может не быть: пустой путь дал бы данные в корне диска.
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base)
    return Path(os.path.expanduser("~")) / "AppData" / "Local"


def _program_files() -> Path:
    base = os.environ.get("ProgramFiles") or os.environ.get("ProgramW6432")
    if base:
        return Path(base)
    return Path(r"C:\Program Files")


def default_program_dir() -> Path:
    """Системный каталог программы.

    На Windows — Program Files, на Linux — /opt, на macOS — /usr/local/opt.
    Здесь лежит КОД; писать сюда без прав администратора нельзя, и это
    нормально: программу ставят и обновляют один раз, а данные живут в
    профиле пользователя.
    """
    if is_windows():
        return _program_files() / APP_NAME
    if sys.platform == "darwin":
        return Path("/usr/local/opt") / APP_NAME
    return Path("/opt") / APP_NAME


def default_data_dir() -> Path:
    """Каталог изменяемых данных — в профиле пользователя, без root.

    Windows: %LOCALAPPDATA%/singbox-subs-finder
    Linux:   $XDG_DATA_HOME/singbox-subs-finder или ~/.local/share/...
    macOS:   ~/Library/Application Support/singbox-subs-finder

    Раскладка та же, что у browser/paths.py: два модуля про одно и то же
    место обязаны отвечать одинаково, иначе на одной машине окажется две
    папки с данными.
    """
    if is_windows():
        return _local_appdata() / APP_NAME
    if sys.platform == "darwin":
        return Path(os.path.expanduser("~")) / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(
        Path(os.path.expanduser("~")) / ".local" / "share"
    )
    return Path(base) / APP_NAME


def program_dir() -> Path:
    """Каталог программы: переменная окружения важнее системного умолчания."""
    override = _env(ENV_PROGRAM_DIR)
    return Path(os.path.expanduser(override)) if override else default_program_dir()


def _looks_like_checkout(path: Path) -> bool:
    """Похоже ли это на клон репозитория (а не на системную установку)."""
    try:
        return any((path / marker).exists() for marker in REPO_MARKERS)
    except OSError:
        return False


def resolve_data_root() -> Path:
    """Где на самом деле лежат данные установки.

    Порядок такой:

      1. SINGBOX_DATA_DIR — если задан, спорить не о чем;
      2. рядом с пакетом, ЕСЛИ там уже есть config.yaml или .env.

    Второй пункт — не небрежность, а совместимость. Программа несколько
    лет жила в клоне репозитория, и у всех, кто обновится, данные лежат
    именно там. Молча переехать в профиль пользователя значило бы потерять
    базу и настройки из виду: программа поднялась бы с умолчаниями, а
    человек искал бы, куда делся его whitelist.

    Порядок проверки (переменная, потом старая папка, потом профиль) даёт
    предсказуемый переезд: НОВАЯ установка сразу раскладывается по системным
    каталогам, а СТАРАЯ продолжает работать как работала, пока файлы лежат
    на прежнем месте.
    """
    override = _env(ENV_DATA_DIR)
    if override:
        return Path(os.path.expanduser(override))

    legacy = PACKAGE_ROOT.parent
    if (legacy / "config.yaml").exists() or (legacy / ".env").exists():
        return legacy
    return default_data_dir()


#: Кэш: корень данных считается один раз за процесс. Каталог не может
#: переехать посреди работы, а resolve_data_root зовут из горячих мест.
_DATA_ROOT: list[Path] = []


def data_root() -> Path:
    """Корень данных с кэшированием (см. resolve_data_root)."""
    if not _DATA_ROOT:
        _DATA_ROOT.append(resolve_data_root())
    return _DATA_ROOT[0]


def reset_cache() -> None:
    """Сбросить кэш. Только для тестов: они подменяют переменные окружения."""
    _DATA_ROOT.clear()


def config_file() -> Path:
    """Путь к config.yaml: явная переменная важнее раскладки."""
    override = _env(ENV_CONFIG_FILE)
    return Path(os.path.expanduser(override)) if override else data_root() / "config.yaml"


def env_file() -> Path:
    """Путь к .env (секреты): явная переменная важнее раскладки."""
    override = _env(ENV_ENV_FILE)
    return Path(os.path.expanduser(override)) if override else data_root() / ".env"


def code_root_of(package: Path | None = None) -> Path:
    """Корень кода: каталог, в котором лежит пакет singbox-subscribe.

    Отделён от data_root намеренно: requirements.txt и .git принадлежат
    программе, а не данным, и в системной установке лежат в /opt или
    Program Files.
    """
    return (PACKAGE_ROOT if package is None else Path(package)).parent


def config_file_path() -> Path:
    """Псевдоним config_file() — чтобы имя читалось в месте вызова."""
    return config_file()


def env_file_path() -> Path:
    """Псевдоним env_file() — чтобы имя читалось в месте вызова."""
    return env_file()


def ensure_data_dirs(root: Path | None = None) -> Path:
    """Создаёт каталоги данных и возвращает корень.

    Без этого первый же запуск после установки падает на записи лога: папки
    ещё нет, а логгер уже пишет.
    """
    base = data_root() if root is None else Path(root)
    for relative in (
        "config/subs",
        "config/templates",
        "source/downloaded",
        "logs",
        "output",
    ):
        (base / relative).mkdir(parents=True, exist_ok=True)
    return base


def program_writable() -> bool:
    """Можно ли писать в каталог программы.

    Нужно установщику: в Program Files и в /opt писать нельзя, и попытка
    создать там .venv заканчивается «Permission denied» без объяснений.
    Пустая проверка (создать и удалить файл) — единственный надёжный способ
    узнать это на любой файловой системе.
    """
    probe = program_dir() / ".write_probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def describe() -> dict[str, str]:
    """Короткая сводка путей — её печатают установщик и preflight."""
    return {
        "программа": str(program_dir()),
        "данные": str(data_root()),
        "config.yaml": str(config_file()),
        ".env": str(env_file()),
    }


__all__ = [
    "APP_NAME",
    "ENV_CONFIG_FILE",
    "ENV_DATA_DIR",
    "ENV_ENV_FILE",
    "ENV_PROGRAM_DIR",
    "PACKAGE_ROOT",
    "REPO_MARKERS",
    "code_root_of",
    "config_file",
    "config_file_path",
    "data_root",
    "default_data_dir",
    "default_program_dir",
    "describe",
    "ensure_data_dirs",
    "env_file",
    "env_file_path",
    "is_windows",
    "program_dir",
    "program_writable",
    "reset_cache",
    "resolve_data_root",
]
