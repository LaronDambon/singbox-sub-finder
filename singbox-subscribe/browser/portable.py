"""Перенос сессии между машинами: экспорт и импорт.

Зачем это нужно
---------------
Профиль Chromium переносить КОПИРОВАНИЕМ нельзя. Cookies там зашифрованы, а
мастер-ключ обёрнут в DPAPI — механизм Windows, привязанный к конкретному
пользователю. Проверено на живом профиле: 61 кука, все с префиксом v10
(AES-GCM), открытых значений НОЛЬ, а ключ лежит в Local State в поле
os_crypt.encrypted_key. На Ubuntu DPAPI нет, поэтому скопированная папка
превращается в нерасшифровываемый мусор.

Что переносится вместо неё
--------------------------
Playwright умеет отдавать cookies УЖЕ РАСШИФРОВАННЫМИ — это storage_state
(JSON с полями name/value/domain/...). Он платформо-независим: экспортировали
на Windows, загрузили на Ubuntu, вход сохранился.

Файл экспорта шифруется мастер-паролем (scrypt + Fernet): переносимый
артефакт не должен лежать открытым текстом, ведь это доступ к аккаунту.

Команды
-------
    python browser.py session export gemini --out gemini.session
    python browser.py session import gemini --in  gemini.session
"""

from __future__ import annotations

import base64
import getpass
import json
import os
from pathlib import Path

#: Как называть файл по умолчанию.
DEFAULT_SUFFIX = ".session"

#: Параметры вывода ключа из мастер-пароля. n=2**14 подобран так, чтобы
#: разблокировка занимала доли секунды на слабой машине (Ubuntu-сервер).
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def _derive_key(password: str, salt: bytes) -> bytes:
    """Ключ шифрования из мастер-пароля и соли."""
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_state(state: dict, password: str) -> bytes:
    """Шифрует storage_state мастер-паролем. Возвращает байты файла."""
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    key = _derive_key(password, salt)
    token = Fernet(key).encrypt(json.dumps(state).encode("utf-8"))
    # Формат: magic | salt | token — самодостаточно, пароль нужен только один.
    return b"SBSESS1" + salt + token


def decrypt_state(blob: bytes, password: str) -> dict:
    """Расшифровывает файл сессии. Бросает ValueError на неверный пароль."""
    from cryptography.fernet import Fernet, InvalidToken

    if not blob.startswith(b"SBSESS1"):
        raise ValueError("Это не файл сессии (нет сигнатуры SBSESS1)")
    salt, token = blob[7:23], blob[23:]
    key = _derive_key(password, salt)
    try:
        raw = Fernet(key).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("Неверный мастер-пароль") from exc
    return json.loads(raw.decode("utf-8"))


def export_session(service: str, password: str, out_path: Path | None = None,
                   *, log=None) -> Path:
    """Выгружает сессию сервиса в зашифрованный файл."""
    from browser import paths

    def say(msg):
        if log is not None:
            log(msg)

    profile = paths.profile_dir(service)
    if not profile.exists():
        raise FileNotFoundError(
            "Профиль %s не найден — сначала выполните: python browser_login.py %s"
            % (profile, service)
        )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), headless=True)
        try:
            state = ctx.storage_state()
        finally:
            ctx.close()

    cookies = state.get("cookies") or []
    origins = state.get("origins") or []
    blob = encrypt_state(state, password)

    target = Path(out_path) if out_path else Path(service + DEFAULT_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)

    say("Экспорт: %d cookies, %d origins" % (len(cookies), len(origins)))
    say("Файл: %s (%.1f КБ, зашифрован)" % (target, len(blob) / 1024.0))
    return target


def import_session(service: str, password: str, in_path: Path,
                   *, log=None) -> int:
    """Загружает сессию из файла в профиль сервиса.

    Возвращает число записанных cookies. Профиль создаётся, если его нет.
    """
    from browser import paths

    def say(msg):
        if log is not None:
            log(msg)

    blob = Path(in_path).read_bytes()
    state = decrypt_state(blob, password)
    cookies = state.get("cookies") or []
    origins = state.get("origins") or []

    profile = paths.ensure_dirs(service)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), headless=True)
        try:
            if cookies:
                ctx.add_cookies(cookies)
            say("Записано cookies: %d" % len(cookies))
            if origins:
                # localStorage тоже переносим: некоторые SPA держат там токен.
                for origin in origins:
                    page = ctx.new_page()
                    try:
                        page.goto(origin.get("origin") or "about:blank",
                                  wait_until="domcontentloaded", timeout=30000)
                        for item in origin.get("localStorage") or []:
                            page.evaluate(
                                "([k, v]) => localStorage.setItem(k, v)",
                                [item.get("name"), item.get("value")],
                            )
                    except Exception:  # noqa: BLE001 — не критично
                        pass
                    finally:
                        page.close()
                say("Записано origins: %d" % len(origins))
        finally:
            ctx.close()
    say("Профиль: %s" % profile)
    return len(cookies)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python browser.py session",
        description="Перенос сессии между машинами (шифрованный файл)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="выгрузить сессию в файл")
    ex.add_argument("service", nargs="?", default="gemini")
    ex.add_argument("--out", default=None)

    im = sub.add_parser("import", help="загрузить сессию из файла")
    im.add_argument("service", nargs="?", default="gemini")
    im.add_argument("--in", dest="inp", required=True)

    args = parser.parse_args(argv)

    # Пароль спрашиваем интерактивно и НЕ показываем. Через аргумент командной
    # строки его передавать нельзя: он попадёт в историю оболочки и в список
    # процессов, то есть станет виден всем на машине.
    pw1 = getpass.getpass("Мастер-пароль: ")
    if args.cmd == "export":
        pw2 = getpass.getpass("Повторите: ")
        if pw1 != pw2:
            print("Пароли не совпадают")
            return 2
        path = export_session(args.service, pw1, Path(args.out) if args.out else None,
                              log=print)
        print()
        print("Перенесите файл на сервер и выполните там:")
        print("  python browser.py session import %s --in %s"
              % (args.service, path.name))
        return 0

    try:
        n = import_session(args.service, pw1, Path(args.inp), log=print)
    except ValueError as exc:
        print("Ошибка: %s" % exc)
        return 1
    print()
    print("Проверьте: python browser_check.py %s --show" % args.service)
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
