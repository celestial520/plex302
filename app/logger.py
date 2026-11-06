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
    logger = logging.getLogger("plex302")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", "%Y-%m-%d %H:%M:%S")
        # 日志目录不可写（CI runner、只读文件系统等）时只保留控制台输出。
        # 写日志绝不能把业务主流程带崩 —— 曾经因为 mkdir('/config') 权限不足
        # 让「测试 Cookie」的风控分支直接抛 PermissionError。
        try:
            config.ensure_dirs()
            fh = logging.handlers.RotatingFileHandler(
                config.LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    _logger = logger
    return logger


def log(message: str, level: str = "INFO") -> None:
    line = str(message).replace("\n", " ")
    with _lock:
        _ring.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {level:<5} | {line}")
    try:
        logger = _setup()
        getattr(logger, level.lower(), logger.info)(line)
    except Exception:  # noqa: BLE001 日志失败一律吞掉，不能影响播放和接口
        pass


def push_external(line: str, level: str = "INFO") -> None:
    """把外部组件（nginx 302 决策日志）产生的行只推进 WebUI 环形缓冲。

    不写 app.log：nginx 自己已经落盘到 /var/log/nginx/302.log 和 error.log，
    再写一份只会重复、还会让日志文件翻倍。
    """
    with _lock:
        _ring.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {level:<5} | {str(line).replace(chr(10), ' ')}")


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
