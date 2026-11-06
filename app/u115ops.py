"""115 写操作：建目录 / 移动 / 重命名 / 删除 / 转存 / 离线。

115 没有“移动并改名”一个接口，所以整理一个文件是两步：
  1) POST /files/move  pid=<目标目录 cid>  fid=<文件 id>
  2) POST /files/edit  fid=<文件 id>  file_name=<目标文件名>
顺序不能反：先改名的文件在源目录里可能撞名。

所有接口都是网页版 Cookie 通道，和直链用的是同一个登录态。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from . import u115web

BASE = u115web.BASE


def _post(
    cfg: Dict[str, Any],
    path: str,
    data: Dict[str, Any],
    timeout: int = 20,
    base: Optional[str] = None,
) -> Dict[str, Any]:
    u115web.pace()  # 写操作同样受 115 风控节奏约束
    url = (base or BASE) + path
    headers = {
        "Cookie": u115web._cookie(cfg),  # noqa: SLF001
        "User-Agent": u115web.DEFAULT_UA,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        resp = requests.post(url, data=data, headers=headers, timeout=timeout)
    except requests.RequestException as err:
        raise u115web.Web115Error(f"请求 115 失败：{err}") from err
    if resp.status_code == 405:
        raise u115web.Web115Error("115 Cookie 已失效或被风控（405），稍后再试或重新复制 Cookie")
    if resp.status_code != 200:
        raise u115web.Web115Error(f"115 返回 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as err:
        raise u115web.Web115Error("115 返回的不是 JSON，Cookie 可能已失效") from err
    if not isinstance(payload, dict):
        raise u115web.Web115Error("115 返回格式异常")
    state = payload.get("state")
    errno = payload.get("errno") or payload.get("errNo") or 0
    if state is False or errno in u115web.EXPIRE_ERRNOS:
        msg = payload.get("error") or payload.get("message") or f"errno={errno}"
        if errno in u115web.EXPIRE_ERRNOS or "登录" in str(msg):
            raise u115web.Web115Error(f"115 Cookie 已失效：{msg}")
        raise u115web.Web115Error(f"115 操作失败：{msg}")
    return payload


def _pick(item: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


# ------------------------------------------------------------------ 目录
_dir_cache: Dict[str, Dict[str, Any]] = {}
DIR_TTL = 180.0


def _invalidate_dirs() -> None:
    _dir_cache.clear()


def find_dir(cfg: Dict[str, Any], path: str, force: bool = False) -> Optional[str]:
    """按路径找目录 cid；不存在返回 None。短缓存，避免整理时反复逐级列目录。"""
    key = "/" + str(path or "").strip("/")
    if not force:
        hit = _dir_cache.get(key)
        if hit and time.time() - hit["at"] < DIR_TTL:
            return hit["cid"] or None
    parts = [p for p in str(path or "").strip("/").split("/") if p]
    cid = "0"
    for part in parts:
        items = u115web.list_dir(cfg, cid, force=True)
        node = None
        for item in items:
            name = str(_pick(item, "n", "file_name", "name"))
            if name.strip().lower() == part.strip().lower():
                node = item
                break
        if node is None:
            return None
        cid = str(_pick(node, "cid", "id", default=""))
        if not cid:
            return None
    _dir_cache[key] = {"cid": cid, "at": time.time()}
    return cid


def mkdir(cfg: Dict[str, Any], parent_cid: str, name: str) -> str:
    payload = _post(cfg, "/files/add", {"pid": str(parent_cid), "cname": name})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    cid = str(_pick(data, "cid", "file_id", "id", default="") or payload.get("cid") or "")
    u115web.clear_path_cache()
    u115web._dir_cache.pop(str(parent_cid), None)  # noqa: SLF001
    _invalidate_dirs()
    if not cid:
        # 有的版本不回 cid，重新列一次目录拿
        found = find_dir(cfg, _join(_path_of(cfg, parent_cid), name))
        if found:
            return found
        raise u115web.Web115Error(f"创建目录失败：{name}")
    return cid


def ensure_dir(cfg: Dict[str, Any], path: str) -> str:
    """逐级确保目录存在，返回最终 cid。"""
    hit = find_dir(cfg, path)
    if hit:
        return hit
    parts = [p for p in str(path or "").strip("/").split("/") if p]
    cid = "0"
    for index, part in enumerate(parts):
        items = u115web.list_dir(cfg, cid, force=index == len(parts) - 1)
        found = None
        for item in items:
            if str(_pick(item, "n", "file_name", "name")).strip().lower() == part.strip().lower():
                found = item
                break
        if found:
            cid = str(_pick(found, "cid", "id", default=""))
        else:
            cid = mkdir(cfg, cid, part)
        if not cid:
            raise u115web.Web115Error(f"创建目录失败：{part}")
    return cid


def _path_of(cfg: Dict[str, Any], cid: str) -> str:
    try:
        return u115web.folder_path(cfg, cid)
    except u115web.Web115Error:
        return "/"


def _join(folder: str, name: str) -> str:
    folder = (folder or "/").rstrip("/")
    return (folder + "/" + name.lstrip("/")) if folder else "/" + name.lstrip("/")


# ------------------------------------------------------------------ 文件
def move(cfg: Dict[str, Any], file_id: str, dst_cid: str) -> None:
    try:
        _post(cfg, "/files/move", {"pid": str(dst_cid), "fid": str(file_id)})
    except u115web.Web115Error as err:
        # 少数版本要带 aid，失败时补一次；真正的业务错误会在这里再抛出来
        if not any(k in str(err) for k in ("参数", "不支持", "签名", "非法")):
            raise
        _post(cfg, "/files/move", {"pid": str(dst_cid), "fid": str(file_id), "aid": 1})
    u115web._dir_cache.pop(str(dst_cid), None)  # noqa: SLF001
    u115web.clear_path_cache()
    u115web.clear_file_cache()
    _invalidate_dirs()


def rename(cfg: Dict[str, Any], file_id: str, new_name: str) -> None:
    try:
        _post(cfg, "/files/edit", {"fid": str(file_id), "file_name": new_name})
    except u115web.Web115Error as err:
        if not any(k in str(err) for k in ("参数", "不支持", "签名", "非法")):
            raise
        # 老接口不认时退回批量改名
        _post(cfg, "/files/batch_rename", {f"files_new_name[{file_id}]": new_name})
    u115web.clear_path_cache()
    u115web.clear_file_cache()


def remove(cfg: Dict[str, Any], file_id: str, parent_cid: str = "0") -> None:
    _post(cfg, "/files/delete", {"pid": str(parent_cid), "fid": str(file_id)})
    u115web._dir_cache.pop(str(parent_cid), None)  # noqa: SLF001
    u115web.clear_path_cache()


def place(cfg: Dict[str, Any], file_id: str, dst_dir: str, new_name: str) -> None:
    """把文件移动到 dst_dir 并改名（幂等：先移动再改名）。"""
    cid = ensure_dir(cfg, dst_dir)
    move(cfg, file_id, cid)
    rename(cfg, file_id, new_name)


# ------------------------------------------------------------------ 分享转存
SHARE_RE = re.compile(
    r"https?://(?:www\.)?(?:115|115cdn|anxia)\.com/s/([0-9a-zA-Z]+)(?:[?#][^\s]*)?"
)
CODE_RE = re.compile(r"(?:提取码|访问码|密码|password|pwd)[\s:：=]*([0-9a-zA-Z]{4,8})", re.I)


def parse_share(text: str) -> Optional[Dict[str, str]]:
    match = SHARE_RE.search(text or "")
    if not match:
        return None
    share_code = match.group(1)
    code = ""
    q = re.search(r"[?&](?:password|pwd)=([0-9a-zA-Z]{4,8})", text, re.I)
    if q:
        code = q.group(1)
    else:
        c = CODE_RE.search(text or "")
        if c:
            code = c.group(1)
    return {"share_code": share_code, "receive_code": code}


def share_snap(cfg: Dict[str, Any], share_code: str, receive_code: str) -> Dict[str, Any]:
    payload = u115web._get(  # noqa: SLF001
        cfg,
        "/share/snap",
        {"share_code": share_code, "receive_code": receive_code, "offset": 0, "limit": 100, "cid": 0},
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("list") or data.get("data") or []
    items = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "id": str(_pick(row, "fid", "file_id", "cid", "id")),
                "name": str(_pick(row, "n", "file_name", "name")),
                "is_dir": str(_pick(row, "cid", "category", default="")) not in ("", "0")
                or bool(_pick(row, "is_dir", default=False)),
                "size": int(_pick(row, "s", "file_size", "size", default=0) or 0),
            }
        )
    return {"items": items, "count": int(data.get("count") or len(items)), "raw": data}


def share_receive(
    cfg: Dict[str, Any], share_code: str, receive_code: str, dst_cid: str, file_ids: Iterable[str]
) -> None:
    ids = ",".join(str(x) for x in file_ids if x)
    _post(
        cfg,
        "/share/receive",
        {"share_code": share_code, "receive_code": receive_code, "cid": str(dst_cid), "file_id": ids},
        timeout=30,
    )
    u115web._dir_cache.pop(str(dst_cid), None)  # noqa: SLF001
    u115web.clear_path_cache()


# ------------------------------------------------------------------ 离线（磁力 / ed2k）
def offline_add(cfg: Dict[str, Any], url: str, dst_cid: str) -> Dict[str, Any]:
    payload = _post(
        cfg,
        "/offline/add_task_urls",
        {"urls": url, "wp_path_id": str(dst_cid)},
        timeout=30,
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    info_hash = str(_pick(data, "info_hash", "hash", default="") or "")
    return {"ok": True, "info_hash": info_hash, "raw": data}


def offline_list(cfg: Dict[str, Any], page: int = 1) -> List[Dict[str, Any]]:
    payload = u115web._get(cfg, "/offline/list", {"page": page, "limit": 30})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("list") or []
    return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []


def wait_received(
    cfg: Dict[str, Any], share_code: str, dst_dir: str, timeout: float = 45.0
) -> List[Dict[str, Any]]:
    """转存后等 115 落盘（一般 1-3 秒），返回目标目录里新出现的条目。"""
    deadline = time.time() + timeout
    last: List[Dict[str, Any]] = []
    while time.time() < deadline:
        cid = find_dir(cfg, dst_dir)
        if cid:
            items = u115web.list_dir(cfg, cid, force=True)
            last = items
            if items:
                return items
        time.sleep(u115web.API_GAP)
    return last
