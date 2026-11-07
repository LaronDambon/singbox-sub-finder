"""Установка, обновление и первичная настройка — общая логика.

Зачем это здесь, а не в .ps1/.sh
-------------------------------
Команд три (установка, обновление, мастер) и две системы (Windows, Linux).
Если писать каждую пару отдельно, получится шесть почти одинаковых скриптов,
и любой шаг придётся чинить шесть раз — а разойдутся они на первом же.

Поэтому весь смысл живёт ЗДЕСЬ, на Python, который и так нужен программе.
А install.ps1/install.sh/update.*/setup.* — тонкие обёртки: находят Python,
передают аргументы и зовут этот модуль. Отдельно на каждую ОС остаётся
только то, что действительно различается: имя интерпретатора, активация
venv и разделители путей.

Что модуль НЕ делает
--------------------
Не трогает файлы пользователя без нужды. Обновление сохраняет config.yaml,
.env, source/, logs/ и config/ — это данные установки, а не код. Мастер
настройки пишет только ту настройку, о которой его спросили.

Ручной запуск (когда обёртка недоступна):

    python singbox-subscribe/installer.py install
    python singbox-subscribe/installer.py update
    python singbox-subscribe/installer.py setup
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Папка пакета (здесь лежит этот файл).
ROOT = Path(__file__).resolve().parent

#: Корень репозитория: рабочие файлы (config.yaml, .env, source/, logs/).
PROJECT_ROOT = ROOT.parent

#: Адрес репозитория — нужен, когда скрипт запущен вне клона и должен его
#: скачать сам (см. bootstrap).
REPO_URL = "https://github.com/LaronDambon/singbox-sub-finder.git"

#: Минимальная версия Python. 3.10 — из-за `X | None` в аннотациях и
#: match-подобных конструкций в коде пакета.
MIN_PYTHON = (3, 10)

#: Каталог venv внутри корня репозитория.
VENV_DIR_NAME = ".venv"

#: Пакеты, без которых не работает ничего.
REQUIRED_MODULES = ("flask", "yaml", "requests")

#: Пакет для браузерной проверки: и модуль, и сам браузер ставятся отдельно.
BROWSER_MODULE = "playwright"


# ===========================================================================
# вывод
# ===========================================================================
class Ui:
    """Печать с шагами и цветом (цвет — только если терминал его понимает).

    Цвет отключается сам, когда вывод перенаправлен в файл или в пайп: иначе
    в логах установки оказываются escape-последовательности.
    """

    def __init__(self, *, color: bool | None = None, quiet: bool = False) -> None:
        if color is None:
            color = (
                sys.stdout.isatty()
                and os.environ.get("NO_COLOR") is None
                and os.environ.get("TERM") != "dumb"
            )
        self.color = color
        self.quiet = quiet
        self._n = 0

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def step(self, text: str) -> None:
        self._n += 1
        print()
        print(self._paint(f"[{self._n}] {text}", "1;36"))

    def ok(self, text: str) -> None:
        print("    " + self._paint("OK  ", "32") + text)

    def info(self, text: str) -> None:
        if not self.quiet:
            print("        " + text)

    def warn(self, text: str) -> None:
        print("    " + self._paint("ВНИМ", "33") + " " + text)

    def fail(self, text: str) -> None:
        print("    " + self._paint("ОШИБ", "31") + " " + text)

    def title(self, text: str) -> None:
        print()
        print(self._paint(text, "1"))
        print(self._paint("=" * len(text), "1"))

    def plain(self, text: str = "") -> None:
        print(text)


class InstallError(RuntimeError):
    """Ошибка, после которой продолжать бессмысленно."""


# ===========================================================================
# определение окружения
# ===========================================================================
def is_windows() -> bool:
    return os.name == "nt"


def python_command() -> str:
    """Как звать Python в этой системе (для подсказок пользователю)."""
    return "py -3" if is_windows() else "python3"


def venv_python(venv_dir: Path) -> Path:
    """Путь к интерпретатору внутри venv — на Windows и Linux он разный."""
    if is_windows():
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_activate_hint(venv_dir: Path) -> str:
    """Команда активации venv для этой ОС."""
    if is_windows():
        return r".\.venv\Scripts\activate"
    return "source .venv/bin/activate"


def check_python_version() -> tuple[bool, str]:
    version = sys.version_info
    text = "%d.%d.%d" % (version.major, version.minor, version.micro)
    if (version.major, version.minor) < MIN_PYTHON:
        need = ".".join(str(x) for x in MIN_PYTHON)
        return False, f"{text} — нужен Python {need} или новее"
    return True, text


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    """Запуск внешней команды с внятной ошибкой вместо голого трейсбека."""
    try:
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, check=check,
            capture_output=capture, text=True,
        )
    except FileNotFoundError as exc:
        raise InstallError(f"команда не найдена: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise InstallError(
            f"команда завершилась с ошибкой (код {exc.returncode}): "
            f"{' '.join(cmd)}" + (f"\n{detail}" if detail else "")
        ) from exc


def module_available(python: Path, module: str) -> bool:
    """Есть ли модуль в этом интерпретаторе (без импорта в свой процесс)."""
    result = subprocess.run(
        [str(python), "-c", f"import {module}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def find_git() -> str | None:
    return shutil.which("git")


# ===========================================================================
# шаги установки
# ===========================================================================
def ensure_venv(ui: Ui, *, recreate: bool = False) -> Path:
    """Создаёт .venv в корне репозитория и возвращает путь к интерпретатору."""
    venv_dir = PROJECT_ROOT / VENV_DIR_NAME
    python = venv_python(venv_dir)

    if recreate and venv_dir.exists():
        ui.info(f"Удаляю прежнее окружение: {venv_dir}")
        shutil.rmtree(venv_dir, ignore_errors=True)

    if python.exists():
        ui.ok(f"Окружение уже есть: {venv_dir}")
        return python

    ui.info(f"Создаю виртуальное окружение: {venv_dir}")
    run([sys.executable, "-m", "venv", str(venv_dir)])

    # На Linux venv может быть без pip (python3-venv не доустановлен).
    if not python.exists():
        raise InstallError(
            "виртуальное окружение создалось, но интерпретатора в нём нет.\n"
            "На Debian/Ubuntu поставьте пакет: sudo apt install python3-venv"
        )
    if not module_available(python, "pip"):
        run([str(python), "-m", "ensurepip", "--upgrade"], check=False)

    ui.ok(f"Окружение создано: {venv_dir}")
    return python


def install_requirements(ui: Ui, python: Path, *, upgrade: bool = False) -> None:
    """Ставит зависимости из requirements.txt."""
    req = PROJECT_ROOT / "requirements.txt"
    if not req.exists():
        raise InstallError(f"нет файла зависимостей: {req}")

    missing = [m for m in REQUIRED_MODULES if not module_available(python, m)]
    if missing and not upgrade:
        ui.info("Не хватает: " + ", ".join(missing))

    ui.info("pip install -r requirements.txt")
    cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
           "-r", str(req)]
    if upgrade:
        cmd.append("--upgrade")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-8:]) if detail else ""
        raise InstallError(
            "не удалось поставить зависимости.\n"
            "Если это ошибка сети — проверьте прокси/VPN и повторите.\n" + tail
        )
    ui.ok("Зависимости установлены")


def install_browser(ui: Ui, python: Path) -> bool:
    """Ставит chromium для browser_service. Возвращает, получилось ли."""
    if not module_available(python, BROWSER_MODULE):
        ui.warn("модуль playwright не установлен — пропускаю")
        return False
    ui.info("playwright install chromium (~150 МБ, это надолго)")
    result = subprocess.run(
        [str(python), "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        ui.warn("chromium поставить не удалось; браузерную проверку можно "
                "включить позже командой:")
        ui.info(f"{python} -m playwright install chromium")
        return False
    if not is_windows():
        # На Linux самого браузера мало — нужны системные библиотеки.
        ui.info("playwright install-deps chromium (может спросить пароль sudo)")
        subprocess.run(
            [str(python), "-m", "playwright", "install-deps", "chromium"],
            capture_output=True, text=True,
        )
    ui.ok("Браузер chromium готов")
    return True


def init_config(ui: Ui, python: Path, *, force: bool = False) -> None:
    """Создаёт config.yaml, если его нет. Существующий не трогает."""
    config_file = PROJECT_ROOT / "config.yaml"
    if config_file.exists() and not force:
        ui.ok(f"config.yaml уже есть — оставляю как есть")
        return
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [str(python), "-m", "pipeline", "config", "init"] + (["--force"] if force else []),
        cwd=str(ROOT), capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"config.yaml не создан:\n{detail}")
    ui.ok("config.yaml создан")


def init_env_file(ui: Ui, python: Path) -> None:
    """Создаёт .env из шаблона, если его нет. Пустой — тоже создаём."""
    env_file = PROJECT_ROOT / ".env"
    example = PROJECT_ROOT / ".env.example"
    if env_file.exists():
        ui.ok(".env уже есть — оставляю как есть")
        return
    if not example.exists():
        ui.warn(".env.example не найден — создайте .env вручную")
        return
    shutil.copyfile(example, env_file)
    ui.ok(".env создан из .env.example (ключи пока пустые)")


def prepare_dirs(ui: Ui) -> None:
    """Создаёт рабочие каталоги: без них первый запуск ругается."""
    for name in ("source", "logs", "output"):
        path = PROJECT_ROOT / name
        path.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "source" / "downloaded").mkdir(parents=True, exist_ok=True)
    ui.ok("Рабочие каталоги готовы: source/, logs/, output/")


def check_singbox_binary(ui: Ui) -> bool:
    """Есть ли бинарник sing-box (в пакете или по пути из конфига)."""
    local = ROOT / "sing-box"
    names = ("sing-box.exe", "sing-box") if is_windows() else ("sing-box", "sing-box.exe")
    for name in names:
        candidate = local / name
        if candidate.exists():
            ui.ok(f"Бинарник sing-box на месте: {candidate}")
            return True
    ui.warn("бинарник sing-box не найден в sing-box-subscribe/sing-box/")
    ui.info("Скачать: https://github.com/SagerNet/sing-box/releases")
    ui.info("Затем положите файл в singbox-subscribe/sing-box/ "
            "или укажите путь в config.yaml (singbox.path)")
    return False


def run_preflight(ui: Ui, python: Path) -> int:
    """Показывает отчёт готовности. Ошибки не прерывают установку."""
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [str(python), "-m", "preflight"], cwd=str(ROOT),
        capture_output=True, text=True, env=env,
    )
    text = (result.stdout or "").strip()
    if text:
        ui.plain()
        ui.plain(text)
    return result.returncode


# ===========================================================================
# команды
# ===========================================================================
def cmd_install(args) -> int:
    ui = Ui(quiet=args.quiet)
    ui.title("Установка singbox-subs-finder")

    ok, version = check_python_version()
    if not ok:
        ui.fail(version)
        return 1
    ui.ok(f"Python {version}")

    if not (PROJECT_ROOT / "requirements.txt").exists():
        ui.fail(f"не найден корень репозитория: {PROJECT_ROOT}")
        ui.info("Запускайте скрипт из клонированного репозитория.")
        return 1
    ui.ok(f"Репозиторий: {PROJECT_ROOT}")

    ui.step("Виртуальное окружение")
    python = ensure_venv(ui, recreate=args.recreate_venv)

    ui.step("Зависимости")
    install_requirements(ui, python, upgrade=args.upgrade)

    ui.step("Рабочие файлы")
    init_config(ui, python)
    init_env_file(ui, python)
    prepare_dirs(ui)
    check_singbox_binary(ui)

    if args.browser:
        ui.step("Браузер для проверки сервисов")
        install_browser(ui, python)
    else:
        ui.step("Браузер для проверки сервисов")
        ui.info("Пропускаю: нужен только для чекера browser_service,")
        ui.info("а он по умолчанию выключен. Поставить позже:")
        ui.info(f"  {python} -m playwright install chromium")

    ui.step("Проверка готовности")
    run_preflight(ui, python)

    ui.title("Готово")
    ui.plain("Запуск:")
    ui.plain(f"  {venv_activate_hint(PROJECT_ROOT / VENV_DIR_NAME)}")
    ui.plain("  python start.py")
    ui.plain()
    ui.plain("Дальше стоит прогнать мастер настройки — он спросит про чекеры,")
    ui.plain("ключи и деплой:")
    ui.plain("  python singbox-subscribe/setup.py")
    return 0


def cmd_update(args) -> int:
    ui = Ui(quiet=args.quiet)
    ui.title("Обновление singbox-subs-finder")

    ok, version = check_python_version()
    if not ok:
        ui.fail(version)
        return 1

    git = find_git()
    if git is None:
        ui.warn("git не найден — обновляю только зависимости и конфиг")
    else:
        ui.step("Обновление кода")
        if (PROJECT_ROOT / ".git").exists():
            status = subprocess.run(
                [git, "status", "--porcelain"], cwd=str(PROJECT_ROOT),
                capture_output=True, text=True,
            )
            if status.stdout.strip() and not args.force:
                ui.warn("в репозитории есть незакоммиченные изменения:")
                for line in status.stdout.strip().splitlines()[:8]:
                    ui.info(line)
                ui.info("Они останутся на месте: git pull откажется работать,")
                ui.info("если правки задевают те же файлы. Повторите с --force,")
                ui.info("чтобы обновить принудительно (правки будут отброшены).")
            else:
                cmd = [git, "pull", "--ff-only"] + (["--force"] if args.force else [])
                result = subprocess.run(cmd, cwd=str(PROJECT_ROOT),
                                        capture_output=True, text=True)
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    ui.fail("git pull не удался:")
                    ui.info(detail)
                    ui.info("Разберитесь с изменениями или запустите с --force.")
                    return 1
                summary = (result.stdout or "").strip().splitlines()
                ui.ok(summary[-1] if summary else "код обновлён")
        else:
            ui.warn("это не git-репозиторий — код не обновляю")
            ui.info(f"Клонируйте заново: git clone {REPO_URL}")

    ui.step("Зависимости")
    python = venv_python(PROJECT_ROOT / VENV_DIR_NAME)
    if not python.exists():
        ui.warn("окружения нет — создаю")
        python = ensure_venv(ui)
    install_requirements(ui, python, upgrade=True)

    ui.step("Файлы установки")
    ui.ok("config.yaml, .env, source/, logs/ не тронуты — это ваши данные")
    prepare_dirs(ui)

    ui.step("Проверка после обновления")
    run_preflight(ui, python)

    ui.title("Обновление завершено")
    ui.plain("Ваши настройки и база сохранены.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="installer",
        description="Установка, обновление и настройка singbox-subs-finder.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="установить с нуля")
    p_install.add_argument("--browser", action="store_true",
                           help="сразу поставить chromium для browser_service")
    p_install.add_argument("--upgrade", action="store_true",
                           help="обновить пакеты, даже если они уже стоят")
    p_install.add_argument("--recreate-venv", action="store_true",
                           help="пересоздать .venv с нуля")
    p_install.add_argument("--quiet", action="store_true", help="меньше подробностей")
    p_install.set_defaults(func=cmd_install)

    p_update = sub.add_parser("update", help="обновить код и зависимости")
    p_update.add_argument("--force", action="store_true",
                          help="обновить, даже если есть локальные изменения")
    p_update.add_argument("--quiet", action="store_true", help="меньше подробностей")
    p_update.set_defaults(func=cmd_update)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except InstallError as exc:
        Ui().fail(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        Ui().warn("прервано пользователем")
        return 130


if __name__ == "__main__":
    sys.exit(main())
