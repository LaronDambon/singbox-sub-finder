"""Кэш вердиктов сервиса — В БАЗЕ, а не в файле рядом с профилем.

Зачем переносить. Вердикты (кто отвечал, кто нет) — это данные о серверах,
такие же, как stable и capabilities, и место им в servers.db. Файл рядом с
профилем браузера был плох тремя вещами:

  * жил вне репозитория, поэтому его не видели ни отчёты, ни SQL-запросы;
  * не переживал смену BROWSER_SESSIONS_DIR (профиль переехал — кэш потерялся);
  * не давал выбрать «кого проверять следующим» запросом.

Таблица — service_bans: она уже хранит (exit_ip, service) -> ban, и её
дополняют колонки checked_by / verdict / kind. Дублирующей сущности не заводим.

Сроки жизни прежние: здоровье сервера меняется быстро, репутация IP — нет.
"""

from __future__ import annotations

import time

#: Сколько часов вердикт считается свежим. Здоровье сервера меняется быстро,
#: поэтому ok живёт час: замерено на умирающем сервере — он был «рабочим» в
#: 12:20, а к 13:07 не открывал уже ничего.
TTL_HOURS_BY_VERDICT = {
    "ok": 1.0,
    "blocked": 6.0,
    "unstable": 1.0,
    "unknown": 1.0,
}

#: Долгий срок для стены «подозрительный трафик»: это не случайность, а
#: репутация адреса в закрытой базе Google.
SORRY_TTL_HOURS = 24 * 30


def _now_iso() -> str:
    from script.server_store import _now

    return _now()


class DbVerdictCache:
    """Кэш вердиктов в servers.db. Интерфейс как у прежнего файлового.

    Один класс на ОБА транспорта проверки сервиса: браузерный
    (browser/batch.py) и API-шный (script/reachability_check.py). Словарь
    вердиктов общий, отличается только пометка checked_by («browser» или
    «api») — по ней в отчёте видно происхождение вердикта, а ключ и сроки
    жизни у них одни и те же.
    """

    #: Вердикты, которые означают «сервис отказал» и превращаются в ban=1.
    #: Список, а не одно слово, потому что транспорты называют отказ
    #: по-разному: браузер — blocked, API — ban.
    BAN_VERDICTS = ("blocked", "ban")

    def __init__(self, service: str, *, store=None) -> None:
        from config.settings import get_settings
        from script.server_store import ServerStore

        self.service = str(service)
        self._store = store or ServerStore(get_settings().paths.servers_db_file)

    # ------------------------------------------------------------------ чтение
    def _ttl_seconds(self, verdict: str, kind: str) -> float:
        if kind == "sorry":
            return SORRY_TTL_HOURS * 3600.0
        hours = TTL_HOURS_BY_VERDICT.get(str(verdict or ""), 6.0)
        return hours * 3600.0

    def _rows(self) -> dict:
        """{proxy_key: (verdict, reason, kind, last_checked)} по сервису."""
        with self._store._connect() as conn:  # noqa: SLF001 — свой же класс
            self._store._ensure_service_bans_table(conn)  # noqa: SLF001
            out = {}
            for r in conn.execute(
                "SELECT proxy_key, verdict, reason, kind, last_checked, ban "
                "FROM service_bans WHERE service = ? AND proxy_key != ''",
                (self.service,),
            ):
                out[r["proxy_key"]] = {
                    "verdict": r["verdict"] or ("blocked" if r["ban"] else ""),
                    "reason": r["reason"] or "",
                    "kind": r["kind"] or "",
                    "at": r["last_checked"] or "",
                }
            return out

    def get(self, line: str) -> dict | None:
        """Свежий вердикт для строки прокси или None.

        Принимает СТРОКУ ПРОКСИ (как прежний файловый кэш): ключ считается
        внутри, чтобы вызывающий код не менялся.
        """
        key = self.key_for(line)
        entry = self._rows().get(key)
        if not entry:
            return None
        verdict = entry.get("verdict") or ""
        if not verdict:
            return None
        # Возраст считаем от last_checked. Кривая метка времени — считаем
        # вердикт устаревшим, а не «свежим навечно».
        try:
            from datetime import datetime, timezone

            ts = datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = time.time() - ts.timestamp()
        except Exception:  # noqa: BLE001
            return None
        if age > self._ttl_seconds(verdict, entry.get("kind") or ""):
            return None
        return {
            "verdict": verdict,
            "reason": entry.get("reason") or "",
            "kind": entry.get("kind") or "",
            "age_hours": round(age / 3600.0, 1),
        }

    def known_keys(self) -> set:
        """Ключи, про которые есть ЛЮБАЯ запись (в том числе устаревшая).

        Нужно, чтобы отличить «новый, никогда не проверялся» от «проверялся
        давно»: первые идут в проверку первыми.
        """
        return set(self._rows())

    def known_ok(self) -> set:
        """Ключи, подтверждённые как рабочие (verdict=ok, любой давности).

        Именно этим отличается «метка заслужена» от «метка осталась с прошлого
        раза»: сверяем с базой, а не с догадкой.
        """
        return {
            k for k, v in self._rows().items()
            if (v.get("verdict") or "") == "ok"
        }

    def has_any(self, line: str) -> bool:
        """Есть ли ЛЮБАЯ запись (в том числе устаревшая).

        Нужно для порядка проверок: серверы без записи — неисследованная
        территория, их проверяем первыми.
        """
        return self.key_for(line) in self._rows()

    # ------------------------------------------------------------------ запись
    def put(self, line: str, result: dict) -> None:
        """Запоминает вердикт. Значения cookies тут не бывают по построению."""
        reason = result.get("reason") or ""
        verdict = result.get("verdict") or ""
        kind = "sorry" if "unusual traffic" in reason.lower() else ""
        # ban=1 только для настоящего отказа. Отказ бывает и без кода ошибки:
        # API отвечает 200 и телом «регион не поддерживается», поэтому
        # смотрим на ВЕРДИКТ, а не на статус.
        ban = 1 if verdict in self.BAN_VERDICTS else 0
        self._store.record_service_bans([{
            "proxy_key": self.key_for(line),
            "service": self.service,
            "exit_ip": result.get("exit_ip") or self.key_for(line)[:60],
            "ban": ban,
            "reason": reason,
            "checked_by": result.get("checked_by") or "browser",
            "verdict": verdict,
            "kind": kind,
        }])

    # ------------------------------------------------------------------ прочее
    @staticmethod
    def key_for(line: str) -> str:
        """Ключ прокси без учёта имени и тэга."""
        from script.downloader import normalize_proxy_key

        return normalize_proxy_key(line) or str(line).strip()

    def stats(self) -> dict:
        rows = self._rows()
        from collections import Counter

        return {
            "entries": len(rows),
            "verdicts": dict(Counter(v.get("verdict") or "?" for v in rows.values())),
        }
