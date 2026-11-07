"""Кэш вердиктов проверки сервиса.

Зачем: проверка страницы стоит 3-17 секунд, а у разных конфигов одного VPN
выходной IP общий. Второй раз платить за ту же страницу незачем.

Ключ — канонический ключ прокси (как в базе), потому что выходной IP нам на
этом этапе неизвестен: его определение само требует запроса через прокси и
стоит времени. Ключ прокси даёт ту же экономию для повторных прогонов и для
одинаковых строк из разных подписок.

Хранится в папке сессий (ВНЕ репозитория), в JSON. Значений cookies тут нет
и быть не может: только вердикт и причина.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

#: Сколько часов вердикт считается свежим по умолчанию.
DEFAULT_TTL_HOURS = 6

#: Сроки по видам вердикта. Здоровье сервера меняется быстро, а репутация IP
#: в базе Google — медленно, поэтому сроки разные:
#:
#:   ok      — час. Замерено на умирающем сервере: он был «рабочим» в 12:20,
#:             а к 13:07 не открывал уже ничего. Шестичасовой кэш держал бы
#:             мёртвый сервер в конфиге весь этот срок.
#:   blocked — шесть часов. Заглушка по стране стабильнее живости, но тоже
#:             может измениться при смене маршрута.
#:   sorry   — тридцать дней. Это не случайность, а репутация адреса в
#:             закрытой базе Google: попал однажды — останешься надолго.
TTL_HOURS_BY_VERDICT = {
    "ok": 1.0,
    "blocked": 6.0,
    "unstable": 1.0,
    "unknown": 1.0,
}

#: Долгий срок для стены «подозрительный трафик».
SORRY_TTL_HOURS = 24 * 30


class VerdictCache:
    """Кэш вердиктов с TTL. Потокобезопасен."""

    def __init__(self, service: str, *, ttl_hours: float = DEFAULT_TTL_HOURS,
                 path: Path | None = None) -> None:
        self.ttl_seconds = float(ttl_hours) * 3600.0
        self._lock = threading.Lock()
        self._data: dict = {}
        self.path = Path(path) if path else None
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except Exception:  # noqa: BLE001 — битый кэш не повод падать
            self._data = {}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass  # кэш — необязательная оптимизация

    def known_keys(self) -> set:
        """Ключи, про которые есть ЛЮБАЯ запись (см. одноимённый метод в DB-кэше)."""
        with self._lock:
            return set(self._data)

    @staticmethod
    def key_for(line: str) -> str:
        """Ключ прокси без учёта имени и тэга."""
        from script.downloader import normalize_proxy_key

        return normalize_proxy_key(line) or str(line).strip()

    def _ttl_for(self, entry: dict) -> float:
        """Срок жизни зависит от вердикта: у «грязного IP» он длиннее всех.

        Свой срок на каждый вид вердикта — см. TTL_HOURS_BY_VERDICT. Явно
        заданный ttl_hours при создании кэша работает как верхняя граница,
        поэтому настройкой можно только УКОРОТИТЬ срок, но не растянуть ok.
        """
        if entry.get("kind") == "sorry":
            return SORRY_TTL_HOURS * 3600.0
        hours = TTL_HOURS_BY_VERDICT.get(str(entry.get("verdict") or ""), None)
        if hours is None:
            return self.ttl_seconds
        return min(hours * 3600.0, self.ttl_seconds)

    def get(self, line: str):
        """Свежий вердикт или None."""
        key = self.key_for(line)
        with self._lock:
            entry = self._data.get(key)
        if not isinstance(entry, dict):
            return None
        age = time.time() - float(entry.get("at") or 0)
        if age > self._ttl_for(entry):
            return None
        entry = dict(entry)
        entry.pop("at", None)
        entry["age_hours"] = round(age / 3600.0, 1)
        return entry

    def put(self, line: str, result: dict) -> None:
        """Запоминает вердикт (без cookies — их тут не бывает)."""
        key = self.key_for(line)
        reason = result.get("reason") or ""
        entry = {
            "verdict": result.get("verdict"),
            "reason": reason,
            "elapsed": result.get("elapsed"),
            # kind отделяет репутационный бан IP от регионального: у первого
            # срок жизни в разы больше, потому что он не «оттает» сам.
            "kind": "sorry" if "unusual traffic" in reason.lower() else "",
            # ttl_hours в запись не пишем: срок считается при чтении, поэтому
            # правка TTL_HOURS_BY_VERDICT действует и на уже сохранённые записи.
            "at": time.time(),
        }
        with self._lock:
            self._data[key] = entry
            self._save()

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data)}