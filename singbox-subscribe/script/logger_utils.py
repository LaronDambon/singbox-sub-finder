import logging
import logging.handlers
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = ROOT / "logs"


def setup_project_logging(
    log_dir: str | Path | None = None,
    console_level: int = logging.WARNING,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    log_dir_path = Path(log_dir or DEFAULT_LOG_DIR)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("singbox_sub")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def add_rotating_file_handler(filename: str, level: int) -> logging.Handler:
        handler = logging.handlers.RotatingFileHandler(
            log_dir_path / filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        return handler

    add_rotating_file_handler("info.log", logging.INFO)
    add_rotating_file_handler("warnings.log", logging.WARNING)
    add_rotating_file_handler("fatal.log", logging.ERROR)
    add_rotating_file_handler("debug.log", logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    def _showwarning(message, category, filename, lineno, file=None, line=None):
        logger.warning("%s:%s: %s", filename, lineno, message)

    warnings.showwarning = _showwarning

    def _handle_uncaught_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = _handle_uncaught_exception
    return logger


def get_project_logger(name: str | None = None) -> logging.Logger:
    logger = logging.getLogger("singbox_sub")
    if not logger.handlers:
        setup_project_logging()
    if name:
        return logger.getChild(name)
    return logger


@contextmanager
def capture_stdout_stderr_to_logger(logger: logging.Logger | None = None) -> Iterator[None]:
    """Перенаправляет print() и stderr в логгер проекта."""
    target_logger = logger or get_project_logger()

    class _LoggerWriter:
        def __init__(self, level: int):
            self.level = level

        def write(self, message: str) -> int:
            if not message:
                return 0
            for line in message.splitlines():
                if line.strip():
                    target_logger.log(self.level, line)
            return len(message)

        def flush(self) -> None:
            return None

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _LoggerWriter(logging.INFO)
    sys.stderr = _LoggerWriter(logging.WARNING)
    try:
        yield
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
