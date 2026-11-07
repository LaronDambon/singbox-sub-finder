"""Чекер доступности сервисов: claude.ai, gemini.google.com и другие.

Чекер-ДОПОЛНЕНИЕ (decides_availability=False): он не решает, жив ли
прокси, а выясняет, пускает ли его ВЫХОДНОЙ IP в конкретные сервисы.
Результат — вердикты ok/ban, которые уходят в таблицу service_bans
(ключ — пара exit_ip + service), а в строку сервера добавляются тэги
[claude] / [gemini], если сервис ОТКРЫТ.

Зачем отдельный чекер, а не разовая проверка. Проверка сервиса стоит
полноценного HTTPS-запроса на каждый живой прокси, поэтому её незачем
повторять для всех подряд: сначала отрабатывают решающий url_probe (кто
вообще жив), затем reachability (кто вообще выходит в сеть), и только
потом этот чекер — по живым. Порядок обеспечивает реестр чекеров.

Бан сервисом выдаётся на выходной IP, а не на прокси: у разных конфигов
одного VPN выход общий. Знание «этот IP забанен gemini» позволяет
пропустить проверку всех конфигов с тем же выходом — именно поэтому
вердикты складываются в service_bans, а не только в тэги.

Настройки (.env):

    SERVICE_CHECK_ENABLED=true          # включить чекер
    SERVICE_CHECK_MIN_STABLE=0          # проверять серверы со stable >= порога
    SERVICE_CHECK_CONCURRENCY=8         # потоков на батч
    SERVICE_CHECK_TAG_OPEN=service      # префикс тэга открытого сервиса

Правила самих сервисов (URL, сигнатуры заглушек) — в
config/service_checks.json, править код для нового сервиса не нужно.
"""

from __future__ import annotations

from config.settings import get_settings
from pipeline.checkers.base import CheckContext, CheckOutcome, CheckResult, Checker
from pipeline.checkers.builtin._runner import run_exclusive
from pipeline.logging_setup import get_logger

LOGGER = get_logger("checkers.service_access")


class ServiceAccessChecker(Checker):
    """Проверка доступа к сервисам (claude, gemini, ...) по правилам из конфига."""

    name = "service_access"
    description = "Доступ к сервисам (claude/gemini) -> баны в service_bans + тэги [name]"
    decides_availability = False
    #: Проверяем только живые серверы: мёртвому прокси сервис недоступен
    #: по определению, и тратить на него запрос незачем.
    run_on_dead = False

    def __init__(self) -> None:
        svc = getattr(get_settings(), "service", None)
        self.enabled = bool(svc and svc.enabled)
        self.min_stable = int(svc.min_stable) if svc else 0
        self.concurrency = int(svc.concurrency) if svc else 8
        self.tag_prefix = str(svc.tag_open) if svc else "service"
        self._services: list[dict] = []
        #: Тэги, которые чекер ЗАМЕНЯЕТ при записи (своя категория).
        self._own_tags: tuple[str, ...] = ()

    async def setup(self, ctx) -> None:
        svc = getattr(get_settings(), "service", None)
        self.enabled = bool(svc and svc.enabled)
        if svc:
            self.min_stable = int(svc.min_stable)
            self.concurrency = int(svc.concurrency)
            self.tag_prefix = str(svc.tag_open or "service")
        if not self.enabled:
            return
        from script.service_check import load_services

        try:
            cfg = await ctx.run_sync(load_services)
            self._services = cfg["services"]
        except Exception as exc:  # noqa: BLE001 — нет конфига = фича выключена
            LOGGER.warning("Правила сервисов не загружены, чекер выключен: %s", exc)
            self._services = []
        # Тэги открытых сервисов — категория этого чекера: сервер, который
        # СЕЙЧАС не прошёл gemini, не должен носить старый тэг [gemini].
        self._own_tags = self.compute_own_tags(self._services)
        if self._services:
            LOGGER.info(
                "Service access: сервисы %s, проверка для stable >= %d, "
                "параллелизм %d",
                ", ".join(s["name"] for s in self._services),
                self.min_stable, self.concurrency,
            )

    @staticmethod
    def compute_own_tags(services: list[dict], prefix: str = "service") -> tuple[str, ...]:
        """Тэги, которые чекер вытесняет при записи результата.

        Имена сервисов (gemini, claude) — своя категория: они означают
        «сервис ОТКРЫТ с этого прокси». Прокси с баном их теряет, поэтому
        старый тэг обязан исчезать, а не жить вечно.

        К голому имени сервиса добавляются ОБЕ метки доступа: "gemini-api"
        (её ставит HTTP-проверка) и "gemini-chat" (браузерная). Без них бан
        снимал бы только метку прошлых прогонов, а рабочие метки двух
        транспортов остались бы висеть на сервере, который сервис не открывает.
        Имя метки чата читается из конфига: она настраиваемая.
        """
        names = {str(s.get("name", "")).strip() for s in services}
        names.add(prefix)
        names.discard("")
        # Метки доступа, которые НЕ совпадают с именем сервиса. Их ровно две,
        # и обе относятся к gemini:
        #   "gemini-api"  — ставит HTTP-проверка (цель reachability);
        #   "gemini-chat" — ставит браузерная проверка (читается из конфига).
        # Придумывать "имя + api" для остальных сервисов НЕЛЬЗЯ: таких меток
        # не существует, и в категорию попадали бы имена-фантомы.
        names.add("gemini-api")
        try:
            from config.settings import get_settings

            browser_tag = str(get_settings().setting(
                "singbox.checkers.browser_service.tag", "") or "").strip()
            if browser_tag:
                names.add(browser_tag)
        except Exception:  # noqa: BLE001 — без настройки остаётся "gemini-api"
            pass
        return tuple(sorted(names))

    def wants(self, ctx: CheckContext, line: str) -> bool:
        """Только живые серверы нужного stable (решает решающий чекер)."""
        if not self.enabled or not self._services:
            return False
        stable = getattr(ctx, "stable_of", None)
        if callable(stable):
            value = stable(line)
            if value is not None and int(value) < self.min_stable:
                return False
        return True

    async def check(self, ctx: CheckContext) -> CheckResult:
        if not self.enabled or not self._services:
            return CheckResult()

        lines = self.filter_lines(ctx)
        if not lines:
            return CheckResult(summary="нет живых серверов для проверки сервисов")

        from script.service_check import batch_service_check

        LOGGER.info("Service access: проверяю %d живых серверов", len(lines))
        try:
            checked = await run_exclusive(
                batch_service_check, sorted(lines),
                concurrency=self.concurrency, record=True,
            )
        except Exception as exc:  # noqa: BLE001 — чекер не должен ронять прогон
            LOGGER.error("Проверка сервисов не удалась: %s", exc)
            return CheckResult(summary=f"ошибка: {exc}"[:120])

        outcomes: dict[str, CheckOutcome] = {}
        opened = 0
        banned = 0
        for line in lines:
            res = checked.get(line) or {}
            tags: list[str] = []
            for name, entry in (res.get("services") or {}).items():
                if entry.get("verdict") == "ok":
                    tags.append(name)
                elif entry.get("verdict") == "ban":
                    banned += 1
            if tags:
                opened += 1
                LOGGER.info("Service access [%s]: %s (exit %s)",
                            ",".join(tags), line[:70], res.get("exit_ip") or "?")
            # ВАЖНО: метку [gemini] здесь НЕ ставим.
            #
            # HTTP-проверка не видит заглушку: она рендерится клиентски после
            # входа. Проверено на живых серверах — этот чекер выдавал
            # «55/98 с доступом», а браузерная проверка тех же адресов
            # показывала заглушку почти везде. Метка от него попадала в конфиг
            # и давала серверы, которые у клиента не работают.
            #
            # Вердикты (ok/ban) по-прежнему пишутся в service_bans внутри
            # batch_service_check — эта таблица полезна для отчётов. Но
            # СОБСТВЕННО МЕТКУ ставит только browser_service, который ходит
            # настоящим браузером из-под сессии.
            outcomes[line] = CheckOutcome(
                ok=None,
                # Ни одной метки и никакого снятия: чекер только собирает
                # вердикты в service_bans, а профиль сервера не трогает.
                capabilities="",
                detail=res.get("error"),
            )

        return CheckResult(
            outcomes=outcomes,
            summary=(
                f"{opened}/{len(lines)} с доступом по HTTP, {banned} банов "
                f"(метку ставит только browser_service)"
            ),
        )