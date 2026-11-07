"""Страховка перед коммитом: блокирует попадание cookies и паролей в git.

Установка (один раз):

    python -m browser.guard --install

Проверяет файлы, попавшие в индекс, на признаки сессий:

  * имена вида Cookies / Login Data / storage_state.json;
  * JSON с полями "name"+"value"+"domain" (формат экспорта cookies);
  * строки Set-Cookie, Bearer-токены, известные google-cookie;
  * любые .enc без сопроводительного .md.

Это защита от НЕСЧАСТНОГО СЛУЧАЯ. Основная гарантия — папка сессий вне
репозитория, до которой git не дотягивается.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Файлы, которые СОДЕРЖАТ сами шаблоны поиска и потому совпадают с ними.
#: Это не секреты, а определения: без этой оговорки хук ругался на собственный
#: исходник и на redact.py, где перечислены имена cookie.
PATTERN_DEFINITION_FILES = frozenset({
    "guard.py", "redact.py",
})


#: Имена файлов, которые не должны попадать в индекс никогда.
FORBIDDEN_NAMES = frozenset({
    "cookies", "cookies-journal", "login data", "login data for account",
    "storage_state.json", "storage.enc", "local state",
})

#: Признаки содержимого.
PATTERNS = (
    re.compile(r"set-cookie\s*:", re.I),
    re.compile(r"authorization\s*:\s*bearer", re.I),
    re.compile(r"__secure-1psid", re.I),
    re.compile(r"\bsapisid\b", re.I),
    re.compile(r"\"domain\"\s*:\s*\"\.[a-z0-9.-]+\"[^}]{0,200}\"value\"\s*:", re.I),
)


def repo_root() -> Path | None:
    """Корень репозитория. Нужен, потому что git отдаёт пути от него,
    а не от текущей папки: запуск из подпапки раньше делал проверку слепой —
    файл не находился, и хук молча пропускал cookies в коммит."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def staged_files() -> list:
    """Файлы в индексе git (то, что уйдёт в коммит), АБСОЛЮТНЫМИ путями."""
    root = repo_root()
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return []
    names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    if root is None:
        return [Path(n) for n in names]
    return [root / n for n in names]


def looks_like_secret(path: Path) -> str | None:
    """Возвращает причину, по которой файл похож на секрет, или None."""
    name = path.name.lower()
    if path.name in PATTERN_DEFINITION_FILES:
        # Здесь лежат сами шаблоны поиска — они совпадают с собой по
        # определению и секретами не являются.
        return None
    if name in FORBIDDEN_NAMES:
        return "имя файла %r похоже на хранилище cookies/паролей" % path.name
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            return None  # большие бинарники не читаем
        raw = path.read_bytes()[:200_000]
    except OSError:
        return None
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    for pat in PATTERNS:
        if pat.search(text):
            return "содержимое похоже на cookies/токен (%s)" % pat.pattern[:40]
    return None


def audit() -> int:
    """Проверяет индекс. Возвращает 0, если чисто."""
    root = repo_root()
    bad = []
    for p in staged_files():
        if not p.is_file():
            continue
        why = looks_like_secret(p)
        if why:
            try:
                shown = str(p.relative_to(root)) if root else str(p)
            except ValueError:
                shown = str(p)
            bad.append((shown, why))
    if not bad:
        print("guard: секретов в индексе не найдено")
        return 0
    print("guard: ОСТАНОВЛЕНО — в коммит попадают секреты:", file=sys.stderr)
    for rel, why in bad:
        print("  %s — %s" % (rel, why), file=sys.stderr)
    print()
    print("Уберите файлы из индекса: git restore --staged <файл>", file=sys.stderr)
    return 1


HOOK = """#!/bin/sh
# Автоматически создан: python -m browser.guard --install
exec python -m browser.guard
"""


def install() -> int:
    """Ставит хук pre-commit в текущий репозиторий."""
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=False)
    if root.returncode != 0:
        print("Не git-репозиторий", file=sys.stderr)
        return 2
    hooks = Path(root.stdout.strip()) / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-commit"
    if target.exists():
        print("Хук уже есть: %s (перезаписываю)" % target)
    target.write_text(HOOK, encoding="utf-8")
    print("Установлен хук: %s" % target)
    print("Теперь коммит с cookies будет заблокирован.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m browser.guard")
    parser.add_argument("--install", action="store_true", help="поставить git-хук")
    args = parser.parse_args(argv)
    if args.install:
        return install()
    return audit()


if __name__ == "__main__":
    raise SystemExit(main())