"""part id → 文件绝对路径 的索引。

为什么需要它：Plex 的 `/library/parts/<id>` 在部分版本/配置下会 302 到媒体
文件本身（见 `plex.part_file` 的注释），拿路径时会变成下载整部电影、线程卡死。
而 `/library/sections/<key>/all` 是稳定可用的，只是要整库遍历一次。

所以后台把「媒体库里的 Part.id → Part.file」建成一张表，持久化到
`/config/part_index.json`，播放时直接查表：
  - 命中 → 立刻拿到路径，正常走 115 直链
  - 未命中 → 触发一次后台重建，本次先回源 Plex（不影响观看）

这张表只读 Plex，不写任何 Plex 数据，也不碰用户的媒体文件。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict

from . import config, logger, plex

INDEX_FILE = config.CONFIG_DIR / "part_index.json"
PAGE_SIZE = 500
REBUILD_TTL = 12 * 3600.0  # 12 小时重建一次，够新了

_index: Dict[str, str] = {}
_built_at: float = 0.0
_building = False
_lock = threading.Lock()


def _norm(path: str) -> str:
    """Windows 反斜杠 + 重复斜杠都收敛成 POSIX 风格。"""
    text = str(path or "").replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _load_disk() -> None:
    global _index, _built_at
    try:
        raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    items = raw.get("parts")
    if isinstance(items, dict):
        _index = {str(k): _norm(v) for k, v in items.items() if k and v}
        _built_at = float(raw.get("built_at") or 0.0)


def _save_disk() -> None:
    try:
        config.ensure_dirs()
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"built_at": _built_at, "parts": _index}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(INDEX_FILE)
    except OSError as err:
        logger.warn(f"[索引] 保存 part 索引失败：{err}")


_load_disk()


def lookup(part_id: Any) -> str:
    key = str(part_id or "")
    if not key:
        return ""
    return _index.get(key, "")


def size() -> int:
    return len(_index)


def state() -> Dict[str, Any]:
    return {
        "count": len(_index),
        "built_at": int(_built_at),
        "building": _building,
        "age": int(time.time() - _built_at) if _built_at else -1,
    }


def stale() -> bool:
    return not _built_at or (time.time() - _built_at) > REBUILD_TTL


def _build() -> None:
    global _index, _built_at, _building
    started = time.time()
    fresh: Dict[str, str] = {}
    try:
        secs = plex.sections(force=True)
        for sec in secs:
            if sec.get("type") not in ("movie", "show", "episode", "clip"):
                continue
            start = 0
            while True:
                resp = plex.request(
                    "GET",
                    f"/library/sections/{sec['key']}/all",
                    params={"X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE_SIZE},
                    timeout=30,
                )
                try:
                    mc = (resp.json() or {}).get("MediaContainer") or {}
                except ValueError:
                    logger.warn(f"[索引] 媒体库「{sec.get('title')}」返回非 JSON，跳过")
                    break
                items = mc.get("Metadata") or []
                for item in items:
                    for media in item.get("Media") or []:
                        for part in media.get("Part") or []:
                            pid = part.get("id")
                            path = part.get("file")
                            if pid and path:
                                fresh[str(pid)] = _norm(path)
                total = int(mc.get("totalSize") or 0)
                start += len(items)
                if not items or (total and start >= total):
                    break
                time.sleep(0.05)  # 别把 Plex 打满
    except Exception as err:  # noqa: BLE001
        logger.warn(f"[索引] 建 part 索引失败：{err}")
        with _lock:
            _building = False
        return

    with _lock:
        if fresh:
            _index = fresh
            _built_at = time.time()
            _save_disk()
        _building = False
    logger.info(
        f"[索引] part 索引就绪：{len(fresh)} 个文件，耗时 {time.time() - started:.1f} 秒"
        "（播放时按 part 直接查表，不再向 Plex 要路径）"
    )


def rebuild(blocking: bool = False) -> bool:
    """建索引。默认后台线程跑，返回是否真的启动了。"""
    global _building
    with _lock:
        if _building:
            return False
        _building = True
    if blocking:
        _build()
        return True
    threading.Thread(target=_build, name="part-index", daemon=True).start()
    return True


def ensure(min_interval: float = 300.0) -> None:
    """索引过期/为空时在后台补建；调用方永远不等它。"""
    if _building:
        return
    if not stale() and _index:
        return
    if _index and (time.time() - _built_at) < min_interval:
        return
    rebuild()


def start_background() -> None:
    """启动时调一次：表为空就建，避免第一次播放必然失败。"""
    if not _index:
        rebuild()
    elif stale():
        rebuild()
