"""日志：文件轮转 + 内存环形缓冲（给 WebUI 看）。"""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from collections import deque
from typing import Iterable

from . import config

_ring: deque[str] = deque(maxlen=3000)
_lock = threading.RLock()
_logger: logging.Logger | None = None


def _setup() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    config.ensure_dirs()
    logger = logging.getLogger("plex302")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(
            config.LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    _logger = logger
    return logger


def log(message: str, level: str = "INFO") -> None:
    logger = _setup()
    line = str(message).replace("\n", " ")
    with _lock:
        _ring.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {level:<5} | {line}")
    getattr(logger, level.lower(), logger.info)(line)


def info(message: str) -> None:
    log(message, "INFO")


def warn(message: str) -> None:
    log(message, "WARNING")


def error(message: str) -> None:
    log(message, "ERROR")


def tail(lines: int = 200, keyword: str = "") -> list[str]:
    with _lock:
        items: Iterable[str] = list(_ring)
    if keyword:
        kw = keyword.lower()
        items = [x for x in items if kw in x.lower()]
    return list(items)[-max(1, min(int(lines), 2000)):]


def read_file_tail(lines: int = 300) -> list[str]:
    path = config.LOG_DIR / "app.log"
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", "ignore")
        return data.splitlines()[-lines:]
    except OSError:
        return []
