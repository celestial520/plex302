"""Plex API 封装（只读 + 局部扫描）。"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from . import config, logger

_lock = threading.RLock()
_sections_cache: List[Dict[str, Any]] = []
_sections_at: float = 0.0


class PlexError(Exception):
    pass


def _base(base: str | None = None) -> str:
    return (base or config.load()["plex"]["url"]).rstrip("/")


def _token(token: str | None = None) -> str:
    # 空字符串同样表示「没传」，回退到已保存配置（输入框留空＝不修改）
    return (token or config.load()["plex"]["token"] or "").strip()


def _verify() -> bool:
    return bool(config.load()["plex"]["verify_tls"])


def request(
    method: str,
    path: str,
    *,
    params: Dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 15.0,
    accept: str = "application/json",
    base: str | None = None,
) -> requests.Response:
    url = f"{_base(base)}{path}"
    headers = {"Accept": accept}
    tok = _token(token)
    if tok:
        headers["X-Plex-Token"] = tok
    try:
        resp = requests.request(
            method, url, params=params, headers=headers, timeout=timeout, verify=_verify()
        )
    except requests.RequestException as err:
        raise PlexError(f"连接 Plex 失败：{err}") from err
    if resp.status_code in (401, 403):
        raise PlexError(_auth_error(path, resp.status_code, tok))
    if resp.status_code >= 400:
        raise PlexError(f"Plex 接口 {path} 返回 HTTP {resp.status_code}")
    return resp


def _auth_error(path: str, status: int, tok: str) -> str:
    """把 401/403 拆细，别再用一句「Token 无效」糊弄过去。"""
    where = f"{path} 返回 HTTP {status}"
    if not tok:
        return (
            f"未配置 X-Plex-Token（{where}）：先在「X-Plex-Token」里填入并点保存，"
            "或从 Plex Web 影片页 → 查看页面源码 → 搜索 X-Plex-Token 重新复制整串"
        )
    hint_len = f"（当前 {len(tok)} 字符）"
    if len(tok) < 16 or " " in tok or '"' in tok or "'" in tok:
        return (
            f"X-Plex-Token 看起来不完整{hint_len}（{where}）："
            "Plex token 一般是 19~20 位连续字母数字，不能带空格、引号或 X-Plex-Token= 前缀，请重新复制"
        )
    if status == 403:
        return (
            f"Plex 拒绝访问{hint_len}（{where}）：token 可能有效但账号无权限。"
            "检查①该 token 是否属于这台服务器的所有者账号；②Plex 设置 → 网络 → "
            "「安全连接」若设为「必需」，请把 Plex 地址改成 https 或改回「首选」"
        )
    return (
        f"Plex Token 无效或已失效{hint_len}（{where}）："
        "从 Plex Web 影片页 → 查看页面源码 → 搜索 X-Plex-Token 重新复制；"
        "若近期改过 Plex 密码或勾选过「登出已连接设备」，旧 token 会全部作废"
    )


def _mc(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data.get("MediaContainer") or {}
    return {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def identity(base: str | None = None, token: str | None = None) -> Dict[str, Any]:
    resp = request("GET", "/identity", timeout=8, base=base, token=token)
    try:
        data = resp.json()
    except ValueError as err:
        raise PlexError("Plex 返回的不是 JSON") from err
    return _mc(data)


def sections(
    force: bool = False,
    ttl: float = 300,
    base: str | None = None,
    token: str | None = None,
) -> List[Dict[str, Any]]:
    global _sections_cache, _sections_at
    # 测试用的临时 url/token 不读写缓存，避免把未保存的值污染进正式缓存
    temp = base is not None or token is not None
    with _lock:
        if not temp and not force and _sections_cache and time.time() - _sections_at < ttl:
            return [dict(x) for x in _sections_cache]
    resp = request("GET", "/library/sections", base=base, token=token)
    try:
        data = resp.json()
    except ValueError as err:
        raise PlexError("获取媒体库失败：Plex 返回非 JSON") from err
    result: List[Dict[str, Any]] = []
    for item in _as_list(_mc(data).get("Directory")):
        locations = []
        for loc in _as_list(item.get("Location")):
            if isinstance(loc, dict) and loc.get("path"):
                locations.append(str(loc["path"]).replace("\\", "/").rstrip("/"))
        result.append(
            {
                "key": str(item.get("key") or ""),
                "title": str(item.get("title") or ""),
                "type": str(item.get("type") or ""),
                "agent": str(item.get("agent") or ""),
                "locations": locations,
                "refreshing": bool(item.get("refreshing")),
            }
        )
    if not temp:
        with _lock:
            _sections_cache = result
            _sections_at = time.time()
    return [dict(x) for x in result]


def invalidate_sections() -> None:
    global _sections_cache, _sections_at
    with _lock:
        _sections_cache = []
        _sections_at = 0.0


def test_connection(base: str | None = None, token: str | None = None) -> Dict[str, Any]:
    """用临时覆盖的 url/token 测连接，不落盘、不污染缓存。

    base/token 为 None 时读已保存配置；前端「测试 Plex」会把输入框当前值
    传进来，做到「所见即所测」——不必先点保存。
    """
    info = identity(base=base, token=token)
    secs = sections(force=True, base=base, token=token)
    return {
        "ok": True,
        "machine_identifier": info.get("machineIdentifier", ""),
        "version": info.get("version", ""),
        "friendly_name": info.get("friendlyName", ""),
        "sections": len(secs),
        "using_input": base is not None or token is not None,
    }


def part_file(part_id: str | int, token: str | None = None) -> str:
    resp = request("GET", f"/library/parts/{part_id}", token=token, timeout=10)
    try:
        md = _as_list(_mc(resp.json()).get("Metadata"))
    except ValueError as err:
        raise PlexError("Plex parts 接口返回非 JSON") from err
    for item in md:
        if isinstance(item, dict) and item.get("file"):
            return str(item["file"]).replace("\\", "/")
    raise PlexError(f"Plex 没有返回 part {part_id} 的文件路径")


def item_file(rating_key: str, media_index: int = 0, part_index: int = 0, token: str | None = None) -> str:
    resp = request("GET", f"/library/metadata/{rating_key}", token=token, timeout=10)
    try:
        md = _as_list(_mc(resp.json()).get("Metadata"))
    except ValueError as err:
        raise PlexError("Plex metadata 接口返回非 JSON") from err
    for item in md:
        media_list = _as_list(item.get("Media")) if isinstance(item, dict) else []
        if not media_list:
            continue
        media = media_list[min(media_index, len(media_list) - 1)]
        parts = _as_list(media.get("Part")) if isinstance(media, dict) else []
        if not parts:
            continue
        part = parts[min(part_index, len(parts) - 1)]
        if isinstance(part, dict) and part.get("file"):
            return str(part["file"]).replace("\\", "/")
    raise PlexError(f"Plex 媒体 {rating_key} 没有文件路径")


def search_file(filename: str, token: str | None = None) -> Optional[str]:
    """按文件名兜底搜索完整路径（part 接口异常时用）。"""
    stem = re.sub(r"\.[^.]+$", "", filename)
    query = re.sub(r"[._]", " ", stem).strip()
    if not query:
        return None
    try:
        resp = request("GET", "/search", params={"query": query, "limit": 20}, token=token, timeout=12)
        md = _as_list(_mc(resp.json()).get("Metadata"))
    except (PlexError, ValueError):
        return None
    target = filename.lower()
    for item in md:
        for media in _as_list(item.get("Media") if isinstance(item, dict) else None):
            for part in _as_list(media.get("Part") if isinstance(media, dict) else None):
                path = str(part.get("file") or "") if isinstance(part, dict) else ""
                if path and path.replace("\\", "/").lower().endswith("/" + target):
                    return path.replace("\\", "/")
    return None


def refresh_section(section_id: str, path: Optional[str] = None) -> None:
    """Plex 官方扫描。

    带 path：只扫这个目录（局部扫描）。
    不带 path：整库扫描（全量同步用）。
    """
    params = {"path": path} if path else None
    resp = request(
        "GET",
        f"/library/sections/{section_id}/refresh",
        params=params,
        timeout=20,
    )
    if resp.status_code not in (200, 204):
        raise PlexError(f"Plex 扫描返回 HTTP {resp.status_code}")


def section_for_path(path: str, secs: List[Dict[str, Any]] | None = None) -> Optional[Dict[str, Any]]:
    path = "/" + str(path or "").strip("/")
    best = None
    best_len = -1
    for sec in secs or sections():
        for loc in sec.get("locations", []):
            loc = "/" + str(loc).strip("/")
            if path == loc or path.startswith(loc + "/"):
                if len(loc) > best_len:
                    best = sec
                    best_len = len(loc)
    return best


def library_counts() -> Dict[str, int]:
    """粗略统计影片数量，用于 UI 展示；失败不影响主流程。"""
    counts: Dict[str, int] = {}
    try:
        for sec in sections():
            resp = request("GET", f"/library/sections/{sec['key']}/all", params={"X-Plex-Container-Size": 1}, timeout=10)
            mc = _mc(resp.json())
            counts[sec["key"]] = int(mc.get("totalSize") or mc.get("size") or 0)
    except (PlexError, ValueError) as err:
        logger.warn(f"[Plex] 统计媒体库数量失败：{err}")
    return counts


def url_for_path(path: str) -> str:
    return f"{_base()}{path}"


def quote_path(path: str) -> str:
    return quote(path, safe="/")
