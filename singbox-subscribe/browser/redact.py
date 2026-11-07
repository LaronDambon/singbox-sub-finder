"""Защита от утечки секретов в логи, чат и файлы."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping, Sequence

#: Имена полей, значения которых нельзя печатать никогда.
SENSITIVE_NAMES = frozenset({
    "authorization", "cookie", "set-cookie", "token", "access_token",
    "refresh_token", "id_token", "session", "sessionid", "sid", "csrftoken",
    "xsrf", "password", "passwd", "secret", "api_key", "apikey", "auth",
    "ssid", "hsid", "sapisid", "sapisidhash", "osid",
})

#: Длинные строки без пробелов, похожие на токен/ключ.
_TOKENISH = re.compile(r"^[A-Za-z0-9_.-]{32,}$")


def fingerprint(value: str, *, length: int = 8) -> str:
    """Короткий необратимый отпечаток значения.

    Позволяет сказать «это та же самая сессия», не раскрывая её: по 8
    hex-символам значение не восстановить, но отпечатки не путаются.
    """
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()
    return digest[:length]


def is_sensitive_name(name: str) -> bool:
    """Является ли имя поля секретным."""
    return str(name).strip().lower() in SENSITIVE_NAMES


def looks_like_token(value: str) -> bool:
    """Похоже ли значение на токен: длинное и без пробелов."""
    return bool(_TOKENISH.match(str(value).strip()))


def redact_value(name: str, value: str) -> str:
    """Безопасное представление значения для лога.

    Секретное или похожее на токен значение заменяется отпечатком;
    остальное (например, домен) печатается как есть — оно не тайна и
    помогает отладке.
    """
    text = "" if value is None else str(value)
    if is_sensitive_name(name) or looks_like_token(text):
        return "<%s:%d chars, fp=%s>" % (name or "value", len(text), fingerprint(text))
    return text


def describe_cookies(cookies: Sequence[Mapping]) -> str:
    """Сводка по cookies БЕЗ значений: сколько и для каких доменов.

    Единственная функция, которой разрешено «рассказывать» о cookies.
    Возвращает строку, безопасную для лога и для показа в чате.
    """
    if not cookies:
        return "cookies: 0"
    domains: dict = {}
    for c in cookies:
        d = str(c.get("domain") or "").lstrip(".") or "(no domain)"
        domains[d] = domains.get(d, 0) + 1
    top = sorted(domains.items(), key=lambda kv: -kv[1])[:6]
    parts = ", ".join("%s=%d" % (d, n) for d, n in top)
    return "cookies: %d (%s)" % (len(cookies), parts)


def assert_no_secrets(text: str, *, what: str = "text") -> None:
    """Последний барьер перед выводом в чат.

    Если сюда случайно попали cookies, исключение остановит вывод.
    """
    lowered = str(text).lower()
    needles = ("set-cookie:", "authorization: bearer", "sapisid=", "__secure-1psid=")
    for needle in needles:
        if needle in lowered:
            raise ValueError("Обнаружен секрет в %s — вывод остановлен" % what)
