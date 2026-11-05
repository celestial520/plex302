"""115 网页版（Cookie）接口。

开放平台（proapi）没有“最近文件”能力，而 CMS 整理完是否入库，
最可靠的信号就是 115 服务端的文件变动。这里用网页版接口补上：

  GET /files/recent?limit=N&offset=0   最近变化的文件（按时间倒序）
  GET /files?aid=1&cid=<cid>           返回 path 链路，可拼出完整目录

Cookie 从浏览器 F12 → Network → 任意 webapi.115.com 请求里复制，
形如 `UID=xxx; CID=yyy; SEID=zzz; KID=www`。
Cookie 失效时 115 会返回 HTTP 405，这里统一转成可读错误。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from . import config, logger

BASE = os.getenv("P115_WEBAPI_BASE", "https://webapi.115.com").rstrip("/")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
EXPIRE_ERRNOS = {990001, 40101017, 40101019}


class Web115Error(Exception):
    pass


_path_cache: Dict[str, Dict[str, Any]] = {}
_path_lock = threading.Lock()
PATH_TTL = 600.0


def normalize_cookie(raw: str) -> str:
    """把用户粘贴的内容收敛成一行 Cookie。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", "; ")
    if text.lower().startswith("cookie:"):
        text = text[7:]
    parts = [p.strip() for p in text.split(";") if p.strip()]
    # 去掉浏览器复制时常见的多余字段，保留 key=value
    keep = [p for p in parts if "=" in p and not p.lower().startswith(("host:", "user-agent:"))]
    return "; ".join(keep)


def has_cookie(cfg: Dict[str, Any]) -> bool:
    return bool((cfg.get("u115") or {}).get("cookie"))


def _cookie(cfg: Dict[str, Any]) -> str:
    cookie = (cfg.get("u115") or {}).get("cookie") or ""
    if not cookie:
        raise Web115Error("未配置 115 Cookie")
    return cookie


def _get(cfg: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    url = BASE + path
    headers = {
        "Cookie": _cookie(cfg),
        "User-Agent": DEFAULT_UA,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        resp = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
    except requests.RequestException as err:
        raise Web115Error(f"请求 115 失败：{err}") from err
    if resp.status_code == 405:
        raise Web115Error("115 Cookie 已失效（405），请重新从浏览器复制")
    if resp.status_code != 200:
        raise Web115Error(f"115 返回 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as err:
        raise Web115Error("115 返回的不是 JSON，Cookie 可能已失效") from err
    if not isinstance(payload, dict):
        raise Web115Error("115 返回格式异常")
    state = payload.get("state")
    errno = payload.get("errno") or payload.get("errNo") or 0
    if state is False or errno in EXPIRE_ERRNOS:
        msg = payload.get("error") or payload.get("message") or f"errno={errno}"
        if errno in EXPIRE_ERRNOS or "登录" in str(msg):
            raise Web115Error(f"115 Cookie 已失效：{msg}")
        raise Web115Error(f"115 接口报错：{msg}")
    return payload


def _pick(item: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def _norm_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """115 网页接口有长短字段两套写法，这里统一。"""
    try:
        size = int(_pick(raw, "file_size", "s", "size", default=0) or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        ctime = float(_pick(raw, "ctime", "t", "ptime", "user_ptime", default=0) or 0)
    except (TypeError, ValueError):
        ctime = 0.0
    return {
        "id": str(_pick(raw, "file_id", "fid", "id")),
        "name": str(_pick(raw, "file_name", "n", "name")),
        "cid": str(_pick(raw, "parent_id", "cid", "pid")),
        "pick_code": str(_pick(raw, "pick_code", "pc")),
        "sha1": str(_pick(raw, "sha1", "sha", "file_sha1")),
        "size": size,
        "ctime": ctime,
        "is_dir": bool(_pick(raw, "is_dir", "is_folder", default=False)),
    }


def recent(cfg: Dict[str, Any], limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """最近变化的文件（新 → 旧）。"""
    params = {"limit": int(limit), "offset": int(offset), "type": 0}
    try:
        payload = _get(cfg, "/files/recent", params)
    except Web115Error as err:
        # 少数账号/版本上 /files/recent 会返回“服务器开小差”，退回最近接收
        if "开小差" in str(err) or "参数" in str(err):
            payload = _get(cfg, "/rb/recent", {"limit": int(limit), "offset": int(offset)})
        else:
            raise
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("list") or data.get("data") or []
    if not isinstance(data, list):
        return []
    out = [_norm_item(x) for x in data if isinstance(x, dict)]
    return [x for x in out if x["name"]]


def folder_path(cfg: Dict[str, Any], cid: str) -> str:
    """把目录 cid 还原成云盘绝对路径，例如 /电影/沙丘2 (2024)。"""
    cid = str(cid or "0")
    if cid in ("", "0"):
        return "/"
    now = time.time()
    with _path_lock:
        cached = _path_cache.get(cid)
        if cached and now - cached["at"] < PATH_TTL:
            return cached["path"]
    payload = _get(cfg, "/files", {"aid": 1, "cid": cid, "show_dir": 1, "limit": 1})
    path = payload.get("path")
    names: List[str] = []
    if isinstance(path, list):
        for node in path:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name") or "").strip()
            if name:
                names.append(name)
    result = "/" + "/".join(names) if names else "/"
    with _path_lock:
        _path_cache[cid] = {"path": result, "at": now}
    return result


def test(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """测试 Cookie 是否可用，返回最近文件条数与示例。"""
    items = recent(cfg, limit=5)
    sample = items[0] if items else None
    return {"ok": True, "count": len(items), "sample": sample}


def self_test() -> Dict[str, Any]:
    """给 WebUI 的“测试 Cookie”用：读当前配置。"""
    cfg = config.load()
    if not has_cookie(cfg):
        return {"ok": False, "error": "未配置 115 Cookie"}
    try:
        result = test(cfg)
    except Web115Error as err:
        return {"ok": False, "error": str(err)}
    logger.info(f"[115] Cookie 测试通过，最近文件 {result['count']} 条")
    return result
