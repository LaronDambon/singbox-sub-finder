"""Кэш вердиктов сервиса — ТЕГИ в server_tags, без отдельной таблицы.

Раньше вердикты жили в service_bans с ключом (exit_ip, service). Ключ был
фиктивным: браузер выходной IP не узнавал и подставлял обрезанный ключ
строки, поэтому «бан по IP» не работал, а PK склеивал разные конфиги с
общим префиксом.

Теперь вердикт — обычный тег, как gemini-api:

    tag=<метка сервиса>, key=normalize_proxy_key(line)
    passed=1   -> проверено, работает
    passed=0   -> проверено, не работает
    строки нет -> не проверяли

Ключ — хэш конкретного конфига, поэтому вердикт строго персональный: две
строки с разными параметрами это два разных вердикта, даже если адрес один.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


class DbVerdictCache:
    """Вердикты сервиса поверх server_tags. Интерфейс прежний.

    Один класс на ОБА транспорта: браузерный и API-шный. Различаются они
    МЕТКОЙ (gemini-chat / gemini-api), поэтому смешивать вердикты больше
    нечему — у каждого своя строка в таблице.
    """

    #: Вердикты, которые означают «сервис отказал» -> passed=0.
    BAN_VERDICTS = ("blocked", "ban")

    def __init__(self, tag: str, *, store=None, ttl_hours: float | None = None) -> None:
        from config.settings import get_settings
        from script.server_store import ServerStore

        self.tag = str(tag)
        #: Имя сервиса для отчётов — совпадает с меткой.
        self.service = self.tag
        self._store = store or ServerStore(get_settings().paths.servers_db_file)
        if ttl_hours is None:
            try:
                ttl_hours = float(
                    get_settings().setting("singbox.verdict_ttl_hours", 0) or 0
                )
            except Exception:  # noqa: BLE001 — без настройки берём значение модуля
                ttl_hours = 0.0
        self.ttl_hours = float(ttl_hours or ServerStore.VERDICT_TTL_HOURS)

    # ------------------------------------------------------------------ чтение
    def _rows(self) -> dict:
        """{proxy_key: (passed, checked_at)} по этой метке."""
        with self._store._connect() as conn:  # noqa: SLF001 — свой же класс
            out = {}
            for r in conn.execute(
                "SELECT key, passed, checked_at FROM server_tags WHERE tag = ?",
                (self.tag,),
            ):
                out[str(r["key"])] = (int(r["passed"] or 0), str(r["checked_at"] or ""))
            return out

    def _age_hours(self, stamp: str) -> float | None:
        try:
            ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (time.time() - ts.timestamp()) / 3600.0
        except Exception:  # noqa: BLE001 — кривая метка = устаревший вердикт
            return None

    def get(self, line: str) -> dict | None:
        """Свежий вердикт для строки прокси или None."""
        entry = self._rows().get(self.key_for(line))
        if not entry:
            return None
        passed, stamp = entry
        age = self._age_hours(stamp)
        if age is None or age > self.ttl_hours:
            return None
        return {
            "verdict": "ok" if passed else "blocked",
            "reason": "",
            "age_hours": round(age, 1),
        }

    def known_keys(self) -> set:
        """Ключи, про которые есть ЛЮБАЯ запись (в том числе устаревшая)."""
        return set(self._rows())

    def known_ok(self) -> set:
        """Ключи, подтверждённые как рабочие (passed=1, любой давности)."""
        return {k for k, (passed, _stamp) in self._rows().items() if passed}

    def has_any(self, line: str) -> bool:
        return self.key_for(line) in self._rows()

    # ------------------------------------------------------------------ запись
    def put(self, line: str, result: dict) -> None:
        """Запоминает вердикт тегом. unknown/unstable не пишем вовсе."""
        verdict = str(result.get("verdict") or "").strip().lower()
        if verdict != "ok" and verdict not in self.BAN_VERDICTS:
            # unknown/unstable: сервер не доказал ни то, ни другое — пусть
            # его проверят ещё раз, а не считают забаненным.
            return
        passed = 0 if verdict in self.BAN_VERDICTS else 1
        self._store.record_service_bans([{
            "service": self.tag,
            "proxy_key": self.key_for(line),
            "ban": 0 if passed else 1,
            "verdict": verdict,
        }])

    # ------------------------------------------------------------------ прочее
    @staticmethod
    def key_for(line: str) -> str:
        """Ключ прокси без учёта имени и тэга — хэш конкретного конфига."""
        from script.downloader import normalize_proxy_key

        return normalize_proxy_key(line) or str(line).strip()

    def stats(self) -> dict:
        verdicts = {"ok": 0, "blocked": 0}
        for passed, _stamp in self._rows().values():
            verdicts["ok" if passed else "blocked"] += 1
        return {"entries": len(self._rows()), "verdicts": verdicts}
