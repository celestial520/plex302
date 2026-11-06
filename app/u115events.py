"""115 生活事件（115 App / 网页里的「最近操作」）。

CMS 整理一部片子，在 115 看来就是「上传 / 移动 / 改名 / 复制 / 删除」这些动作。
`/behavior/detail` 是 115 给客户端用的操作流水，按时间倒序返回，带
`file_id / parent_id / file_name / type / update_time`，正好可以还原出
「哪个目录多了一个文件」，再去做 Plex 局部扫描。

和 `/files/recent` 的区别：
  - `/files/recent` 只是“最近碰过的文件列表”，被 limit 截断就永久丢了；
  - 生活事件有唯一 id 和游标，能从上次位置继续，不会因为一轮拉太多而漏。

接口（web 通道风控比 app 轻，优先用它）：
  GET https://webapi.115.com/behavior/detail?limit=100&offset=0
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import u115web

EVENT_BASE = os.getenv("P115_EVENT_BASE", "https://webapi.115.com").rstrip("/")

# 115 的事件类型（和 p115client BEHAVIOR_TYPE_TO_NAME 对齐）
TYPE_NAMES: Dict[int, str] = {
    1: "upload_image",
    2: "upload_file",
    3: "star_image",
    4: "star_file",
    5: "move_image",
    6: "move_file",
    7: "browse_image",
    8: "browse_video",
    9: "browse_audio",
    10: "browse_document",
    14: "receive_files",
    17: "new_folder",
    18: "copy_folder",
    19: "folder_label",
    20: "folder_rename",
    22: "delete_file",
    23: "copy_file",
    24: "file_rename",
}
TYPE_LABELS: Dict[int, str] = {
    1: "上传图片", 2: "上传文件", 3: "收藏图片", 4: "收藏文件", 5: "移动图片",
    6: "移动文件", 7: "浏览图片", 8: "浏览视频", 9: "浏览音频", 10: "浏览文档",
    14: "接收文件", 17: "新建目录", 18: "复制目录", 19: "目录标签",
    20: "目录改名", 22: "删除", 23: "复制文件", 24: "文件改名",
}
# 纯浏览/收藏/标签：与入库无关，直接丢弃
IGNORE_TYPES = {3, 4, 7, 8, 9, 10, 19}
# 目录类事件：扫描目录本身（而不是它所在的父目录）
DIR_EVENT_TYPES = {17, 18, 20}
TYPE_DELETE = 22
# 移动 / 改名：官方接口看不到这两类（它只按「上传时间」认文件），
# 而 CMS 整理恰恰就是先移动到 media 目录、再改名，所以必须靠事件补。
TYPE_MOVE = 6
TYPE_RENAME = 24


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _pick(raw: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return default


def _norm(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    etype = _as_int(_pick(raw, "type", "behavior_type", "event_type", default=0))
    name = str(_pick(raw, "file_name", "name", "file_title", "n", default="") or "").strip()
    if not name:
        return None
    category = str(_pick(raw, "file_category", "file_type", "fc", default="1"))
    is_dir = category == "0" or bool(raw.get("is_dir")) or bool(raw.get("is_folder"))
    ts = _as_int(_pick(raw, "update_time", "utime", "time", "create_time", default=0))
    event_id = str(_pick(raw, "id", "event_id", "behavior_id", default="") or "")
    if not event_id:
        # 没有事件 id 时用字段拼一个，至少保证重启不重复处理
        event_id = "{}:{}:{}:{}".format(
            etype,
            _pick(raw, "file_id", "fid", default=""),
            _pick(raw, "parent_id", "cid", "pid", default=""),
            ts,
        )
    return {
        "id": event_id,
        "type": etype,
        "type_name": TYPE_NAMES.get(etype, str(etype)),
        "label": TYPE_LABELS.get(etype, "变更"),
        "name": name,
        "file_id": str(_pick(raw, "file_id", "fid", default="") or ""),
        "parent_id": str(_pick(raw, "parent_id", "cid", "pid", default="") or ""),
        "parent_name": str(_pick(raw, "parent_name", "pn", default="") or ""),
        "is_dir": is_dir,
        "size": _as_int(_pick(raw, "file_size", "size", "s", default=0)),
        "sha1": str(_pick(raw, "sha1", "sha", "file_sha1", default="") or ""),
        "pick_code": str(_pick(raw, "pick_code", "pc", default="") or ""),
        "time": ts,
    }


def _rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data: Any = payload.get("data")
    if isinstance(data, dict):
        data = data.get("list") or data.get("data") or data.get("items") or []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def fetch(cfg: Dict[str, Any], limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """拉一页操作流水（新 → 旧）。"""
    params = {"limit": int(limit), "offset": int(offset)}
    payload = u115web._get(cfg, "/behavior/detail", params, base=EVENT_BASE)  # noqa: SLF001
    rows = _rows(payload)
    if not rows:
        # 偶发空返回：隔一下重试一次（社区反馈过这个现象）
        payload = u115web._get(cfg, "/behavior/detail", params, base=EVENT_BASE)  # noqa: SLF001
        rows = _rows(payload)
    out = []
    for raw in rows:
        item = _norm(raw)
        if item:
            out.append(item)
    return out


def test(cfg: Dict[str, Any], limit: int = 10) -> Dict[str, Any]:
    """测试 Cookie + 事件接口，返回条数和最新一条。"""
    items = fetch(cfg, limit=limit)
    sample = items[0] if items else None
    path = ""
    if sample and sample.get("parent_id"):
        path = u115web.folder_path(cfg, sample["parent_id"]).rstrip("/") + "/" + sample["name"]
    return {"ok": True, "count": len(items), "sample": sample, "path": path}
