"""Где живут профили браузера. По умолчанию — ВНЕ репозитория.

Это главная защита от утечки: папку, до которой git не дотягивается,
невозможно закоммитить случайно. .gitignore остаётся второй линией,
но полагаться только на него нельзя — одна опечатка, и cookies окажутся
в публичном репозитории.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Переменная окружения для переопределения места хранения сессий.
ENV_SESSIONS_DIR = "BROWSER_SESSIONS_DIR"


def default_sessions_dir() -> Path:
    """Папка сессий по умолчанию — вне репозитория, в профиле пользователя."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "singbox-subscribe-browser"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "singbox-subscribe-browser"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return Path(base) / "singbox-subscribe-browser"


def sessions_dir() -> Path:
    """Корень сессий: переменная окружения важнее значения по умолчанию."""
    override = (os.environ.get(ENV_SESSIONS_DIR) or "").strip()
    return Path(override).expanduser() if override else default_sessions_dir()


def service_dir(service: str) -> Path:
    """Папка одного сервиса. Имя проверяется, чтобы не выйти за корень."""
    name = str(service).strip()
    bad = "/" in name or chr(92) in name or ":" in name
    if not name or name in {".", ".."} or bad:
        raise ValueError("Недопустимое имя сервиса: %r" % (service,))
    return sessions_dir() / name


def profile_dir(service: str) -> Path:
    """Профиль Chromium (user-data-dir) для сервиса."""
    return service_dir(service) / "profile"


def ensure_dirs(service: str) -> Path:
    """Создаёт папки сервиса и возвращает путь профиля."""
    path = profile_dir(service)
    path.mkdir(parents=True, exist_ok=True)
    return path


def repo_root() -> Path:
    """Корень пакета singbox-subscribe (для проверок «не внутри ли репо»)."""
    return Path(__file__).resolve().parents[1]


def assert_outside_repo(path: Path) -> None:
    """Страховка: папка сессий не должна оказаться внутри репозитория.

    Если пользователь сам выставил BROWSER_SESSIONS_DIR внутрь репо, это его
    право, но предупредить обязаны — именно так cookies и уезжают на GitHub.
    """
    import warnings

    try:
        Path(path).resolve().relative_to(repo_root())
    except ValueError:
        return  # снаружи — всё хорошо
    warnings.warn(
        "Папка сессий браузера находится ВНУТРИ репозитория (%s). "
        "Cookies могут попасть в git. Вынесите её через %s."
        % (path, ENV_SESSIONS_DIR),
        RuntimeWarning,
        stacklevel=3,
    )
