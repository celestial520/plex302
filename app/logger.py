"""日志：文件轮转 + 内存环形缓冲（给 WebUI 看）。"""
from __future__ import annotations

import logging
import logging.handlers
import re
import threading
import time
from collections import deque
from typing import Any, Dict, Iterable, List

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


# ---------------------------------------------------------------- 分类与分级
# 日志页面要能一眼看出「哪个环节出问题」，所以把消息前缀映射成业务模块。
# 前缀本来就是各模块自己打的（[115事件] / [扫描] / [302] ...），这里只做归类，
# 不改任何调用点的输出格式 —— 老的 app.log 也能正确分类。
_MODULE_RULES: List[tuple[str, str]] = [
    ("[115事件]", "事件入库"),
    ("[监听]", "事件入库"),
    ("[115Open]", "115接口"),
    ("[Provider]", "115接口"),
    ("[115]", "115接口"),
    ("[扫描]", "扫描入库"),
    ("[索引]", "扫描入库"),
    ("[全量]", "扫描入库"),
    ("[定时]", "扫描入库"),
    ("[CMS]", "扫描入库"),
    ("[Plex]", "Plex"),
    ("[302]", "播放302"),
    ("[回源]", "播放302"),
    ("[CD2]", "CD2"),
    ("[配置]", "配置"),
    ("[微信]", "通知"),
    ("[企业微信]", "通知"),
    ("[WebUI]", "WebUI"),
    ("[日志]", "WebUI"),
]

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*\|\s*([A-Za-z]+)\s*\|\s?(.*)$"
)


def classify(message: str) -> str:
    for prefix, name in _MODULE_RULES:
        if message.startswith(prefix):
            return name
    return "系统"


def parse(line: str) -> Dict[str, Any]:
    """把一行文本日志解析成结构化记录；解析不了就整行当消息，不丢内容。"""
    raw = str(line)
    m = _LINE_RE.match(raw)
    if m:
        date, clock, level, msg = m.groups()
    else:
        date, clock, level, msg = "", "", "INFO", raw
    lv = (level or "INFO").upper()
    if lv in ("WARN", "WARNING"):
        lv = "WARN"
    elif lv in ("CRITICAL", "FATAL"):
        lv = "ERROR"
    elif lv not in ("ERROR", "INFO", "DEBUG"):
        lv = "INFO"
    return {
        "date": date,
        "time": clock,
        "level": lv,
        "module": classify(msg),
        "msg": msg,
        "raw": raw,
    }


def tail_lines(lines: int = 200) -> List[str]:
    with _lock:
        items = list(_ring)
    return items[-max(1, min(int(lines), 2000)):]


def filter_rows(
    rows: List[Dict[str, Any]], keyword: str = "", level: str = "all", module: str = ""
) -> List[Dict[str, Any]]:
    out = rows
    if keyword:
        kw = keyword.lower()
        out = [r for r in out if kw in r["raw"].lower()]
    lv = str(level or "all").lower()
    if lv in ("warn", "important", "warning"):
        out = [r for r in out if r["level"] in ("WARN", "ERROR")]
    elif lv in ("error", "bad"):
        out = [r for r in out if r["level"] == "ERROR"]
    if module:
        out = [r for r in out if r["module"] == module]
    return out


def summarize(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按模块汇总：多少条信息 / 警告 / 错误，最后一条错误是什么。

    日志页顶部用它显示「哪里出错」，比翻几千行靠谱。
    """
    mods: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        item = mods.setdefault(
            r["module"],
            {"module": r["module"], "info": 0, "warn": 0, "error": 0, "last": "", "last_error": ""},
        )
        if r["level"] == "ERROR":
            item["error"] += 1
            item["last_error"] = r["msg"][:100]
        elif r["level"] == "WARN":
            item["warn"] += 1
        else:
            item["info"] += 1
        if r["time"]:
            item["last"] = r["time"]
    ordered = sorted(mods.values(), key=lambda x: (-x["error"], -x["warn"], x["module"]))
    return ordered


def tail(lines: int = 200, keyword: str = "") -> list[str]:
    with _lock:
        items: Iterable[str] = list(_ring)
    if keyword:
        kw = keyword.lower()
        items = [x for x in items if kw in x.lower()]
    return list(items)[-max(1, min(int(lines), 2000)):]


def clear_ring() -> None:
    """只清 WebUI 的内存缓冲，磁盘上的 app.log 保留。"""
    with _lock:
        _ring.clear()
    info("[日志] WebUI 日志缓冲已清空（app.log 保留，可用于事后排查）")


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
