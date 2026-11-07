"""Мастер первичной настройки: спрашивает и записывает в файлы.

Зачем отдельный шаг
-------------------
Программа работает «из коробки»: умолчания в config.yaml уже рабочие. Но
несколько решений зависят от человека, и принять их за него нельзя:

  * какие чекеры запускать (от этого зависит время проверки и нужные ключи);
  * куда девать собранный конфиг (никуда / в файл / в GitHub);
  * есть ли ключи для проверки сервисов.

Раньше это выяснялось по ходу: пустой whitelist.txt, чекер, который молча
выключился, деплой без токена. Мастер задаёт эти вопросы один раз и сразу
пишет ответы в нужные места — config.yaml для настроек, .env для секретов.

Мастер НЕ спрашивает про то, что и так работает: пути, таймауты, лимиты
остаются умолчаниями, пока их не тронут руками.

Запуск:

    python singbox-subscribe/setup.py            # диалог
    python singbox-subscribe/setup.py --yes      # принять всё по умолчанию
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer import (  # noqa: E402  — путь настраивается выше
    InstallError,
    Ui,
    is_windows,
    venv_python,
)

CONFIG_FILE = PROJECT_ROOT / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"

#: Наборы чекеров на выбор. Порядок — от самого дешёвого к полному: время
#: проверки растёт вместе с ним, и человек должен видеть, что выбирает.
#: Порядок — от самого дешёвого к полному: время проверки растёт вместе с
#: набором, и человек должен видеть, что именно выбирает. Первый набор
#: совпадает с умолчанием схемы (singbox.enabled_checkers), чтобы «просто
#: нажать Enter» давало ровно то, что описано в документации.
PRESETS = [
    ("recommended", "Рекомендуемый",
     ["url_probe", "reachability", "api_service", "browser_service"],
     "жив ли сервер, куда дотягивается и не закрыт ли сервис по региону. "
     "Нужны GEMINI_API_KEY и OPENROUTER_API_KEY (бесплатные)"),
    ("minimal", "Минимальный", ["url_probe", "reachability"],
     "без проверки сервисов — ключи не нужны вовсе"),
    ("full", "Полный",
     ["url_probe", "country", "reachability", "api_service", "browser_service",
      "service_access", "speed"],
     "плюс страна, скорость и доступ к сервисам. Самый долгий"),
]


# ===========================================================================
# диалог
# ===========================================================================
def ask(ui: Ui, question: str, *, default: str = "", validate=None) -> str:
    """Спрашивает строку. Пустой ввод берёт значение по умолчанию."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{question}{suffix}: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        if validate is not None:
            problem = validate(answer)
            if problem:
                ui.warn(problem)
                continue
        return answer


def ask_yes_no(ui: Ui, question: str, *, default: bool = False) -> bool:
    mark = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{mark}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def choose(ui: Ui, title: str, options: list[tuple[str, str]], *,
           default: int = 0) -> int:
    """Выбор одного пункта из списка. Возвращает индекс."""
    ui.plain()
    ui.plain(title)
    for i, (_key, label) in enumerate(options, 1):
        mark = " (по умолчанию)" if i - 1 == default else ""
        ui.plain(f"  {i}) {label}{mark}")
    while True:
        try:
            answer = input(f"Номер [1-{len(options)}]: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        ui.warn(f"Введите число от 1 до {len(options)}")


def choose_many(ui: Ui, title: str, options: list[tuple[str, str]],
                *, preselected: list[str]) -> list[str]:
    """Выбор нескольких пунктов (через запятую). Пусто — оставить как есть."""
    ui.plain()
    ui.plain(title + " (номера через запятую, пусто — оставить как есть)")
    for i, (key, label) in enumerate(options, 1):
        mark = "*" if key in preselected else " "
        ui.plain(f" {mark}{i}) {label}")
    while True:
        try:
            answer = input("Номера: ").strip()
        except EOFError:
            return preselected
        if not answer:
            return preselected
        picked: list[str] = []
        bad = False
        for part in answer.replace(",", " ").split():
            if not part.isdigit() or not (1 <= int(part) <= len(options)):
                ui.warn(f"нет такого номера: {part}")
                bad = True
                break
            picked.append(options[int(part) - 1][0])
        if not bad:
            return picked


# ===========================================================================
# чтение и запись
# ===========================================================================
def load_config() -> dict:
    """Читает config.yaml. Пустой словарь, если файла нет."""
    try:
        import yaml
    except ImportError as exc:
        raise InstallError("нет PyYAML — сначала запустите install") from exc
    if not CONFIG_FILE.exists():
        return {}
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def save_config(ui: Ui, config: dict) -> None:
    """Пишет config.yaml, СОХРАНЯЯ комментарии и порядок строк.

    Через ruamel.yaml, а не PyYAML: обычный дамп выбросил бы все пояснения к
    настройкам, и файл, который человек читает, превратился бы в набор
    голых строк. Если ruamel нет — откатываемся на PyYAML и честно
    предупреждаем.
    """
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 100
        with CONFIG_FILE.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle)
        merge(data, config)
        with CONFIG_FILE.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle)
        return
    except ImportError:
        ui.warn("ruamel.yaml не установлен — комментарии в config.yaml "
                "будут потеряны")

    import yaml as plain_yaml
    current = load_config()
    merge(current, config)
    text = plain_yaml.safe_dump(current, allow_unicode=True,
                                sort_keys=False, default_flow_style=False)
    CONFIG_FILE.write_text(text, encoding="utf-8")


def merge(base: dict, patch: dict) -> dict:
    """Рекурсивно накладывает patch на base (на месте)."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def write_env(ui: Ui, values: dict[str, str]) -> None:
    """Обновляет .env, не теряя уже вписанные ключи.

    Файл переписывается построчно: строки, которых мы не касаемся, остаются
    на месте вместе со своими комментариями.
    """
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            name = stripped.split("=", 1)[0].strip()
            if name in remaining:
                out.append(f"{name}={remaining.pop(name)}")
                continue
        out.append(line)

    if remaining:
        # Не нашли строку — дописываем в конец, чтобы ключ точно попал в файл.
        out.append("")
        out.append("# Добавлено мастером настройки")
        for name, value in remaining.items():
            out.append(f"{name}={value}")

    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def mask(value: str) -> str:
    if not value:
        return "(пусто)"
    return value[:6] + "…" + value[-4:] if len(value) > 12 else "***"


# ===========================================================================
# мастер
# ===========================================================================
def run_wizard(ui: Ui, *, assume_yes: bool = False) -> int:
    ui.title("Мастер настройки")
    ui.plain("Отвечайте на вопросы или жмите Enter, чтобы принять значение")
    ui.plain("в скобках. Ничего необратимого здесь нет: всё пишется")
    ui.plain("в config.yaml и .env, и это можно переписать руками.")

    if not CONFIG_FILE.exists():
        ui.plain()
        ui.warn("config.yaml не найден — сначала запустите install")
        return 1

    config = load_config()
    patch: dict = {}

    # ---------------------------------------------------------- 1. чекеры
    ui.step("Что проверять")
    ui.plain()
    current = (config.get("singbox") or {}).get("enabled_checkers") or []
    ui.plain(f"Сейчас включено: {', '.join(current) if current else '(пусто)'}")
    ui.plain()
    ui.plain("Готовые наборы:")
    for i, (_key, label, checkers, why) in enumerate(PRESETS, 1):
        ui.plain(f"  {i}) {label}")
        ui.plain(f"       {why}")

    if assume_yes:
        choice = 1
    else:
        choice = choose(
            ui, "Какой набор взять?",
            [(k, label) for k, label, _c, _w in PRESETS] + [("custom", "Указать вручную")],
            default=1,
        )

    if choice < len(PRESETS):
        selected = PRESETS[choice][2]
    else:
        options = [
            ("url_probe", "доступность и задержка (решает, жив ли сервер)"),
            ("country", "страна сервера"),
            ("reachability", "профиль целевых сайтов"),
            ("service_access", "доступ к сервисам (claude/gemini)"),
            ("api_service", "сервис по API (нужны ключи)"),
            ("browser_service", "сервис через браузер (нужен chromium)"),
            ("speed", "скорость скачивания"),
        ]
        selected = choose_many(ui, "Отметьте, что включать:", options,
                               preselected=current or ["url_probe"])
        if not selected:
            ui.warn("не выбрано ничего — оставляю как было")
            selected = current

    if "url_probe" not in selected:
        selected = ["url_probe"] + list(selected)
        ui.warn("url_probe обязателен: без него нечего решать, жив ли сервер")

    patch.setdefault("singbox", {})["enabled_checkers"] = list(selected)
    ui.ok("Будут запускаться: " + ", ".join(selected))

    needs_keys = any(c in selected for c in ("api_service", "service_access"))
    needs_browser = "browser_service" in selected

    # ---------------------------------------------------------- 2. ключи
    ui.step("Ключи сервисов")
    env_patch: dict[str, str] = {}
    if not needs_keys:
        ui.ok("Не нужны: среди выбранных чекеров нет проверки сервисов")
    else:
        ui.plain()
        ui.plain("Проверка сервисов ходит в их API с ключом. Ключи бесплатные,")
        ui.plain("запросы не расходуют токены. Нужны:")
        ui.plain("  GEMINI_API_KEY     — https://aistudio.google.com/apikey")
        ui.plain("  OPENROUTER_API_KEY — https://openrouter.ai/keys")
        existing = read_env()
        for name, url in (("GEMINI_API_KEY", "aistudio.google.com/apikey"),
                          ("OPENROUTER_API_KEY", "openrouter.ai/keys")):
            have = existing.get(name, "")
            if have:
                ui.plain()
                ui.plain(f"{name}: уже задан ({mask(have)})")
                if assume_yes or not ask_yes_no(ui, "Заменить?", default=False):
                    continue
            if assume_yes:
                continue
            ui.plain()
            value = ask(ui, f"{name} (Enter — пропустить)")
            if value:
                env_patch[name] = value
        if not env_patch and not existing:
            ui.warn("Ключи не заданы — проверка сервисов ставить метки не сможет")
            if assume_yes or ask_yes_no(ui, "Убрать её из чекеров?", default=True):
                selected = [c for c in selected if c not in ("api_service", "service_access")]
                patch["singbox"]["enabled_checkers"] = list(selected)
                ui.ok("Чекеры сервисов убраны: " + ", ".join(selected))

    if needs_browser and not assume_yes:
        ui.plain()
        ui.warn("browser_service требует chromium и входа в аккаунт:")
        ui.plain(f"  {venv_python(PROJECT_ROOT / '.venv')} -m playwright install chromium")
        ui.plain("  python singbox-subscribe/browser.py login gemini")

    # ---------------------------------------------------------- 3. деплой
    ui.step("Куда девать собранный конфиг")
    deploy_options = [
        ("off", "Никуда — только source/whitelist.txt"),
        ("local", "В файл на этой машине (без токенов)"),
        ("github", "В GitHub-репозиторий (нужен токен)"),
        ("both", "И в файл, и в GitHub"),
    ]
    deploy_choice = 0 if assume_yes else choose(ui, "Выберите:", deploy_options, default=0)
    deploy_key = deploy_options[deploy_choice][0]

    deploy = patch.setdefault("deploy", {})
    if deploy_key == "off":
        deploy["enabled"] = False
        ui.ok("Деплой выключен")
    else:
        deploy["enabled"] = True
        deploy["local"] = deploy_key in ("local", "both")
        if deploy["local"]:
            deploy["local_path"] = "output"
            ui.ok("Локальный деплой: output/")
        if deploy_key in ("github", "both"):
            if assume_yes:
                ui.warn("GitHub-деплой пропущен: нужен ваш логин/репозиторий")
            else:
                repo = ask(ui, "Репозиторий (owner/repo)")
                if repo:
                    deploy["repo"] = repo
                    ui.ok(f"Репозиторий: {repo}")
                else:
                    ui.warn("Репозиторий не задан — GitHub-деплой не заработает")
                token = ask(ui, "GH_DEPLOY_TOKEN (Enter — пропустить)")
                if token:
                    env_patch["GH_DEPLOY_TOKEN"] = token
                else:
                    ui.plain("  Токен: https://github.com/settings/tokens "
                             "(право Contents: read and write)")

    # ---------------------------------------------------------- 4. запись
    ui.step("Запись настроек")
    save_config(ui, patch)
    ui.ok(f"config.yaml обновлён")

    if env_patch:
        write_env(ui, env_patch)
        ui.ok(f".env обновлён: {', '.join(env_patch)}")
    else:
        ui.ok(".env не менялся")

    # ---------------------------------------------------------- 5. итог
    ui.title("Настройка завершена")
    ui.plain(f"Чекеры:  {', '.join(patch['singbox']['enabled_checkers'])}")
    ui.plain("Деплой:  " + ("выключен" if not deploy.get("enabled")
                            else ("локально" if deploy.get("local") else "GitHub")))
    ui.plain()
    ui.plain("Проверить результат:")
    ui.plain("  python singbox-subscribe/preflight.py")
    ui.plain()
    ui.plain("Запустить:")
    ui.plain("  python start.py")
    return 0


def read_env() -> dict[str, str]:
    """Пары из .env (только непустые значения)."""
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup",
        description="Мастер настройки singbox-subs-finder.",
    )
    parser.add_argument("--yes", action="store_true",
                        help="принять все значения по умолчанию, без вопросов")
    args = parser.parse_args(argv)
    try:
        return run_wizard(Ui(), assume_yes=args.yes)
    except InstallError as exc:
        Ui().fail(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        Ui().warn("прервано пользователем — файлы не изменены")
        return 130


if __name__ == "__main__":
    sys.exit(main())
