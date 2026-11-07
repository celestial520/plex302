"""115 **官方开放平台**的文件操作（列目录 / 建目录 / 移动 / 重命名 / 删除）。

整理功能全程只走这里，不碰网页 Cookie 的 /files* 接口 —— 那才是 115
风控最严的一条链路。所有调用带 access_token，并统一按配置的间隔限速，
避免短时间打太多请求。

接口与参数对齐 115 开放平台 SDK：
  GET  /open/ufile/files       cid, limit, offset, show_dir, cur
  POST /open/folder/add        pid, file_name
  POST /open/folder/get_info   path
  POST /open/ufile/move        file_ids(逗号分隔), to_cid
  POST /open/ufile/update      file_id, file_name
  POST /open/ufile/delete      file_ids, parent_id
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from . import logger, u115open

_last_call = 0.0
_dir_cache: Dict[str, Dict[str, Any]] = {}
_DIR_TTL = 120.0


class OpenFSError(Exception):
    pass


def _pace(cfg: Dict[str, Any]) -> None:
    global _last_call
    gap = float(((cfg.get("organize") or {}).get("api_gap")) or 1.5)
    wait = gap - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _call(cfg: Dict[str, Any], method: str, path: str,
          data: Optional[Dict[str, Any]] = None,
          params: Optional[Dict[str, Any]] = None,
          retry: bool = True) -> Any:
    _pace(cfg)
    token = u115open.ensure_token(cfg)
    try:
        resp = requests.request(
            method, u115open.API_HOST + path,
            data=data, params=params,
            headers={"Authorization": "Bearer " + token},
            timeout=u115open.TIMEOUT,
        )
        payload = resp.json()
    except (requests.RequestException, ValueError) as err:
        raise OpenFSError(f"115 官方接口请求失败：{err}") from err
    if not isinstance(payload, dict):
        raise OpenFSError("115 官方接口返回格式异常")
    if not payload.get("state"):
        msg = str(payload.get("message") or payload.get("error") or payload.get("code") or "未知错误")
        if retry and u115open._is_token_error(payload):  # noqa: SLF001
            u115open._refresh(cfg, force=True)  # noqa: SLF001
            return _call(cfg, method, path, data=data, params=params, retry=False)
        raise OpenFSError(msg)
    return payload.get("data")


def _as_dict(data: Any) -> Dict[str, Any]:
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def get_by_path(cfg: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    """按绝对路径查文件/目录；不存在返回 None（不抛异常）。"""
    clean = "/" + str(path or "").strip("/")
    try:
        data = _call(cfg, "POST", "/open/folder/get_info", data={"path": clean})
    except OpenFSError as err:
        if any(word in str(err) for word in ("不存在", "未找到", "not found", "20004", "990")):
            return None
        raise
    info = _as_dict(data)
    fid = str(info.get("file_id") or info.get("fid") or "")
    return {"id": fid, "name": str(info.get("file_name") or ""),
            "pick_code": str(info.get("pick_code") or ""),
            "sha1": str(info.get("sha1") or ""), "raw": info} if fid else None


def list_dir(cfg: Dict[str, Any], cid: str) -> List[Dict[str, Any]]:
    """列出一个目录下的全部条目（自动分页），统一字段。"""
    items: List[Dict[str, Any]] = []
    offset, limit = 0, 1000
    for _ in range(50):
        data = _call(cfg, "GET", "/open/ufile/files", params={
            "cid": str(cid), "limit": limit, "offset": offset,
            "show_dir": 1, "cur": 1, "asc": 1, "o": "file_name",
        })
        rows = data if isinstance(data, list) else (data.get("data") or data.get("list") or []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("fn") or row.get("n") or row.get("file_name") or "")
            fid = str(row.get("fid") or row.get("file_id") or row.get("id") or "")
            if not (name and fid):
                continue
            items.append({
                "id": fid,
                "name": name,
                "is_dir": str(row.get("fc") or "") == "0" or bool(row.get("is_dir")),
                "size": int(row.get("fs") or row.get("s") or row.get("file_size") or 0),
                "sha1": str(row.get("sha1") or ""),
                "ext": str(row.get("ico") or "").lstrip(".").lower(),
            })
        if len(rows) < limit:
            break
        offset += limit
    return items


def mkdir(cfg: Dict[str, Any], pid: str, name: str) -> str:
    data = _call(cfg, "POST", "/open/folder/add", data={"pid": str(pid), "file_name": name})
    info = _as_dict(data)
    fid = str(info.get("file_id") or info.get("fid") or info.get("cid") or "")
    _dir_cache.clear()
    if not fid:
        raise OpenFSError(f"创建目录没有返回 ID：{name}")
    return fid


def ensure_dir(cfg: Dict[str, Any], path: str, create: bool = True) -> str:
    """逐级确保目录存在，返回末级 cid；create=False 时只查不建。"""
    clean = "/" + str(path or "").strip("/")
    hit = _dir_cache.get(clean)
    if hit and time.monotonic() - hit["at"] < _DIR_TTL:
        return str(hit["cid"])
    node = get_by_path(cfg, clean)
    if node:
        _dir_cache[clean] = {"cid": node["id"], "at": time.monotonic()}
        return node["id"]
    if not create:
        return ""
    parent, _, name = clean.rpartition("/")
    pid = ensure_dir(cfg, parent or "/", create=True) if parent else "0"
    cid = mkdir(cfg, pid, name)
    _dir_cache[clean] = {"cid": cid, "at": time.monotonic()}
    return cid


def move(cfg: Dict[str, Any], file_ids: List[str], to_cid: str) -> None:
    ids = ",".join(str(x) for x in file_ids if x)
    if not ids:
        return
    _call(cfg, "POST", "/open/ufile/move", data={"file_ids": ids, "to_cid": str(to_cid)})
    _dir_cache.clear()


def rename(cfg: Dict[str, Any], file_id: str, new_name: str) -> None:
    _call(cfg, "POST", "/open/ufile/update", data={"file_id": str(file_id), "file_name": new_name})


def delete(cfg: Dict[str, Any], file_ids: List[str], parent_id: str) -> None:
    ids = ",".join(str(x) for x in file_ids if x)
    if not ids:
        return
    _call(cfg, "POST", "/open/ufile/delete", data={"file_ids": ids, "parent_id": str(parent_id)})
    _dir_cache.clear()


def walk(cfg: Dict[str, Any], root_cid: str, root_path: str, max_depth: int = 6) -> Dict[str, Any]:
    """递归列出待整理目录：返回 files 和 dirs（自底向上，方便删空目录）。"""
    files: List[Dict[str, Any]] = []
    dirs: List[Dict[str, Any]] = []

    def rec(cid: str, path: str, depth: int) -> None:
        if depth > max_depth:
            return
        for item in list_dir(cfg, cid):
            item_path = path.rstrip("/") + "/" + item["name"]
            item["path"] = item_path
            if item["is_dir"]:
                rec(item["id"], item_path, depth + 1)
                dirs.append(item)
            else:
                files.append(item)

    rec(root_cid, root_path, 0)
    return {"files": files, "dirs": list(reversed(dirs))}


def invalidate() -> None:
    _dir_cache.clear()
    logger.info("[整理] 115 目录缓存已清空")
