"""Единое логирование проекта.

Один модуль на весь репозиторий: любая точка кода берёт логгер одной
строкой —  ```from pipeline.logging_setup import get_logger``` — и получает
настроенные обработчики без собственных настроек.

Что исправлено по сравнению со старой реализацией (script/logger_utils.py):

  * ОДНА настройка на процесс. Раньше каждый модуль на верхнем уровне дёргал
    `setup_project_logging()` (main.py, urltest.py, country_check.py ...), и каждый
    вызов удалял и заново создавал ВСЕ обработчики: уровни сбрасывались,
    а в конкурентных потоках логи терялись. Здесь настройка идемпотентна:
    повторный вызов без `force` ничего не меняет.
  * Нет четырёхкратного дублирования. Раньше одна запись INFO писалась сразу в
    info.log, warnings.log, fatal.log и debug.log. Теперь главный файл получает
    всё, что прошло порог, а per-level файлы — ТОЛЬКО свой уровень
    (ExactLevelFilter). Один и тот же лог больше не хранится четыре раза.
  * Настраиваемость. Уровни, формат, ротация, количество копий и очистка по
    возрасту задаются переменными окружения (см. config/env.py, раздел ЛОГИРОВАНИЕ).
  * `LOG_LEVEL` — это ПОРОГ, а не "какой файл смотреть": при LOG_LEVEL=ERROR в
    логи попадают только ошибки, при LOG_LEVEL=INFO — info и всё более серьёзное.
  * Защита от "пулемёта": ThrottleFilter гасит тысячи одинаковых сообщений,
    оставляя первые N и одну сводную строку.
  * Сторонние библиотеки (urllib3, werkzeug, paramiko ...) приглушены и не
    подмешиваются в файлы проекта.
  * Консоль всегда в UTF-8 с errors="replace": эмодзи-флаги стран больше не
    роняют вывод UnicodeEncodeError.

Форматы файлов настраиваются переменной LOG_FORMAT: ``text`` (по умолчанию) или
``json`` — построчный JSON, удобно для сбора в Loki/Elastic/Grafana.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from config.env import (
    LOG_BACKUP_COUNT,
    LOG_CAPTURE_STDOUT,
    LOG_CONSOLE_LEVEL,
    LOG_DIR_PATH,
    LOG_FILE_NAME,
    LOG_FORMAT,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    LOG_PER_LEVEL_FILES,
    LOG_RETENTION_DAYS,
    LOG_THROTTLE_LIMIT,
    LOG_THROTTLE_WINDOW,
)

# Корневой логгер проекта. Дочерние логгеры берут имя от модуля
# (get_logger(__name__)), поэтому в записи видно, откуда пришло сообщение.
ROOT_LOGGER_NAME = "singbox"

# Файлы по уровням: (имя файла, уровень logging.X).
# ExactLevelFilter не даёт WARNING попасть в info.log и т.п.
PER_LEVEL_FILES: tuple[tuple[str, int], ...] = (
    ("debug.log", logging.DEBUG),
    ("info.log", logging.INFO),
    ("warning.log", logging.WARNING),
    ("error.log", logging.ERROR),
)

# Сторонние библиотеки: оставляем только WARNING и выше, в файлы проекта не идут.
NOISY_LOGGERS: tuple[tuple[str, int], ...] = (
    ("urllib3", logging.WARNING),
    ("requests", logging.WARNING),
    ("paramiko", logging.WARNING),
    ("scp", logging.WARNING),
    ("charset_normalizer", logging.WARNING),
    ("werkzeug", logging.WARNING),
    ("PIL", logging.WARNING),
)

_lock = threading.RLock()
_configured = False


# --------------------------------------------------------------------------- #
# Фильтры
# --------------------------------------------------------------------------- #
class ExactLevelFilter(logging.Filter):
    """Пропускает записи РОВНО одного уровня.

    Нужен для отдельных файлов на уровень: без него в info.log попадали бы и
    warning, и error (handler.setLevel(INFO) — это минимум, а не равенство).
    """

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self.level


class ThrottleFilter(logging.Filter):
    """Гасит поток одинаковых сообщений.

    Битая подписка или невалидный батч sing-box порождают тысячи почти
    одинаковых строк. Здесь первые ``limit`` повторов проходят как есть, дальше
    сообщение глушится, а по истечении окна печатается одна сводка со счётчиком.
    """

    def __init__(self, limit: int = 20, window: float = 60.0):
        super().__init__()
        self.limit = max(1, int(limit))
        self.window = max(0.0, float(window))
        self._state: dict[tuple, tuple[int, int, float]] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.levelno, record.name, record.getMessage()[:200])
        now = time.monotonic()
        with self._lock:
            count, suppressed, first = self._state.get(key, (0, 0, now))
            count += 1
            if now - first > self.window:
                # Окно прошло — счётчики сбрасываются, сообщение снова видно.
                self._state[key] = (count, 0, now)
                return True
            if count <= self.limit:
                self._state[key] = (count, suppressed, first)
                return True
            suppressed += 1
            self._state[key] = (count, suppressed, first)
            return False

    def summary(self) -> list[str]:
        """Строки-сводки по заглушённым сообщениям (для финального отчёта)."""
        with self._lock:
            return [
                f"{level} {name}: {msg!r} — подавлено {suppressed} повторов"
                for (level, name, msg), (_c, sup, _f) in self._state.items()
                if sup
            ]


# --------------------------------------------------------------------------- #
# Форматтеры
# --------------------------------------------------------------------------- #
class TextFormatter(logging.Formatter):
    """Человекочитаемый формат: время, уровень, модуль, сообщение."""

    def __init__(self, *, with_module: bool):
        fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        if with_module:
            fmt = "%(asctime)s %(levelname)-8s %(name)s (%(module)s): %(message)s"
        super().__init__(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")


class JsonFormatter(logging.Formatter):
    """Построчный JSON — для аггрегаторов логов (Loki/Elastic/Grafana)."""

    _EXTRA_KEYS = (
        "url", "server", "protocol", "country", "stable", "ping_ms",
        "batch", "checker", "duration", "attempt",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        for key in self._EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Короткий формат без даты — для интерактивного вывода."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
def _ensure_utf8(stream: IO[str] | None) -> None:
    """Переводит поток в UTF-8 с errors='replace'.

    Эмодзи-флаги стран в именах серверов и русские сообщения роняют консоль
    Windows (cp1251) с UnicodeEncodeError. Здесь это лечится один раз, а не
    вырезанием символов из текста сообщения.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding in {"utf8", ""}:
            return
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError, AttributeError):
        # Поток не поддерживает перенастройку (перенаправленный, подменённый) —
        # это не повод ронять логирование.
        pass


class SafeFileHandler(logging.handlers.RotatingFileHandler):
    """Ротация по размеру, но без шумных трассировок в stderr.

    Если файл логов недоступен (нет прав, каталог занят, диск полон), обычный
    ``RotatingFileHandler`` на КАЖДОЙ записи печатает «--- Logging error ---»
    с полной трассировкой. Здесь обработчик один раз сообщает об этом в консоль
    и отключает себя: процесс продолжает работать, а в логе нет мусора.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._broken = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._broken:
            return
        try:
            super().emit(record)
        except Exception as exc:  # noqa: BLE001 — файловый лог не роняет процесс
            self._broken = True
            print(
                f"[WARNING] Файл логов недоступен, файловые логи отключены: "
                f"{self.baseFilename} ({type(exc).__name__}: {exc})",
                file=sys.stderr,
                flush=True,
            )


def _make_file_handler(path: Path, level: int, *, exact: bool,
                        max_bytes: int, backups: int, formatter: logging.Formatter,
                        throttle: ThrottleFilter) -> logging.Handler:
    handler = SafeFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if exact:
        handler.addFilter(ExactLevelFilter(level))
    handler.addFilter(throttle)
    return handler


def cleanup_old_logs(log_dir: Path | None = None, retention_days: int | None = None,
                     *, now: float | None = None) -> list[Path]:
    """Удаляет файлы логов старше порога возраста.

    ``retention_days=0`` отключает очистку (остаётся только ротация по размеру).
    Возвращает список удалённых файлов — их стоит упомянуть в логе.
    """
    directory = Path(log_dir or LOG_DIR_PATH)
    days = int(retention_days if retention_days is not None else LOG_RETENTION_DAYS)
    if days <= 0 or not directory.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed: list[Path] = []
    # Ротация создаёт файлы app.log.1, app.log.2, warning.log.3 ...
    for path in directory.glob("*.log*"):
        try:
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            # Файл мог быть уже удалён ротацией или заблокирован — не мешаем.
            continue
    return removed


def _install_exception_and_warning_hooks(logger: logging.Logger) -> None:
    """Необработанные исключения и warnings пишутся в общий лог."""
    warnings.showwarning = lambda message, category, filename, lineno, file=None, line=None: (
        logger.warning("%s:%s: %s: %s", filename, lineno, category.__name__, message)
    )

    previous_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Необработанное исключение", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = _hook


# --------------------------------------------------------------------------- #
# Настройка
# --------------------------------------------------------------------------- #
def setup_logging(
    *,
    level: int | None = None,
    console_level: int | None = None,
    log_dir: str | Path | None = None,
    file_name: str | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    retention_days: int | None = None,
    per_level_files: bool | None = None,
    log_format: str | None = None,
    capture_stdout: bool | None = None,
    force: bool = False,
) -> logging.Logger:
    """Настраивает логирование проекта. Идемпотентна.

    Без ``force`` повторный вызов ничего не делает — безопасно вызывать из любого
    модуля при импорте. ``force=True` пересобирает обработчики с новыми
    параметрами (используется в тестах и в main).
    """
    global _configured

    with _lock:
        root = logging.getLogger(ROOT_LOGGER_NAME)
        if _configured and not force:
            return root

        cfg_level = LOG_LEVEL if level is None else int(level)
        cfg_console = (LOG_CONSOLE_LEVEL if console_level is None else int(console_level))
        cfg_dir = Path(log_dir or LOG_DIR_PATH)
        cfg_file = file_name or LOG_FILE_NAME
        cfg_max_bytes = int(max_bytes if max_bytes is not None else LOG_MAX_BYTES)
        cfg_backups = int(backup_count if backup_count is not None else LOG_BACKUP_COUNT)
        cfg_days = int(retention_days if retention_days is not None else LOG_RETENTION_DAYS)
        cfg_per_level = (
            bool(per_level_files) if per_level_files is not None else bool(LOG_PER_LEVEL_FILES)
        )
        cfg_format = (log_format or LOG_FORMAT or "text").lower()
        cfg_capture = (
            bool(capture_stdout) if capture_stdout is not None else bool(LOG_CAPTURE_STDOUT)
        )

        # Чистый срез: сначала снимаем старые обработчики, иначе повторные
        # вызовы копили бы дубликаты и логировали в файлы дважды.
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 — закрытие обработчика не критично
                pass

        cfg_dir.mkdir(parents=True, exist_ok=True)
        removed = cleanup_old_logs(cfg_dir, cfg_days)

        root.setLevel(cfg_level)
        root.propagate = False
        # Служебные трассировки самого logging в stderr не нужны: ошибки записи
        # обрабатываются в SafeFileHandler.
        logging.raiseExceptions = False

        file_formatter: logging.Formatter = (
            JsonFormatter() if cfg_format == "json" else TextFormatter(with_module=True)
        )
        throttle = ThrottleFilter(LOG_THROTTLE_LIMIT, LOG_THROTTLE_WINDOW)

        # --- главный файл: всё, что прошло порог LOG_LEVEL -------------------
        main_file = _make_file_handler(
            cfg_dir / cfg_file, cfg_level, exact=False,
            max_bytes=cfg_max_bytes, backups=cfg_backups,
            formatter=file_formatter, throttle=throttle,
        )
        root.addHandler(main_file)

        # --- отдельные файлы по уровням (только свой уровень) ----------------
        if cfg_per_level:
            for name, lvl in PER_LEVEL_FILES:
                if lvl < cfg_level:
                    # Ниже порога файл не имеет смысла — иначе в нём всегда пусто.
                    continue
                root.addHandler(_make_file_handler(
                    cfg_dir / name, lvl, exact=True,
                    max_bytes=cfg_max_bytes, backups=cfg_backups,
                    formatter=file_formatter, throttle=throttle,
                ))

        # --- консоль ----------------------------------------------------------
        _ensure_utf8(sys.stdout)
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(cfg_console)
        console.setFormatter(ConsoleFormatter())
        console.addFilter(throttle)
        root.addHandler(console)

        # --- сторонние библиотеки -------------------------------------------
        for name, lvl in NOISY_LOGGERS:
            noisy = logging.getLogger(name)
            noisy.setLevel(lvl)
            noisy.propagate = False

        _install_exception_and_warning_hooks(root)
        atexit.register(lambda: [h.flush() for h in root.handlers])

        root.throttle = throttle  # type: ignore[attr-defined]
        _configured = True

        if removed:
            root.info(
                "Очистка логов: удалено %d файлов старше %d дн.",
                len(removed), cfg_days,
            )
        root.debug(
            "Логирование настроено: level=%s console=%s dir=%s per_level=%s format=%s",
            logging.getLevelName(cfg_level), logging.getLevelName(cfg_console),
            cfg_dir, cfg_per_level, cfg_format,
        )
        if cfg_capture:
            capture_stdout_stderr_to_logger(root, permanent=True)
        return root


def get_logger(name: str | None = None) -> logging.Logger:
    """Логгер проекта. Единственная точка, которую зовёт прикладной код.

    ```get_logger(__name__)``` — имя модуля попадает в запись, поэтому видно,
    какой именно модуль написал сообщение.
    """
    if not _configured:
        setup_logging()
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if not name or name == ROOT_LOGGER_NAME:
        return root
    # 'singbox.script.core' -> 'singbox.script.core'; 'core' -> 'singbox.core'
    if name.startswith(ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def is_configured() -> bool:
    return _configured


def log_throttle_summary(logger: logging.Logger | None = None) -> None:
    """Печатает сводку по подавленным повторам — в конце длительного этапа."""
    target = logger or get_logger("throttle")
    root = logging.getLogger(ROOT_LOGGER_NAME)
    throttle = getattr(root, "throttle", None)
    if not isinstance(throttle, ThrottleFilter):
        return
    for line in throttle.summary():
        target.info("Подавлено повторов: %s", line)


@contextmanager
def capture_stdout_stderr_to_logger(
    logger: logging.Logger | None = None, *, permanent: bool = False,
) -> Iterator[None]:
    """Заворачивает print()/sys.stderr в логгер.

    Нужно там, где код печатает вместо логирования (сторонние библиотеки,
    argparse-скрипты). По умолчанию действует только внутри ``with``;
    ``permanent=True`` перенаправляет поток до конца процесса.
    """
    target = logger or get_logger("stdout")
    original_stdout, original_stderr = sys.stdout, sys.stderr

    class _Writer:
        def __init__(self, level: int, fallback: IO[str]):
            self._level = level
            self._fallback = fallback

        def write(self, message: str) -> int:
            if not message:
                return 0
            for line in message.splitlines():
                if line.strip():
                    target.log(self._level, line.rstrip())
            return len(message)

        def flush(self) -> None:
            try:
                self._fallback.flush()
            except Exception:  # noqa: BLE001 — поток мог быть уже закрыт
                pass

    sys.stdout = _Writer(logging.INFO, original_stdout)
    sys.stderr = _Writer(logging.WARNING, original_stderr)
    try:
        yield
    finally:
        if not permanent:
            sys.stdout, sys.stderr = original_stdout, original_stderr
