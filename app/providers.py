"""直链 Provider：115 / MoviePilot / CD2 / AList / 自定义 HTTP。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import parse_qs, quote

import requests

from . import cd2, logger, store, u115open, u115web


class ProviderError(Exception):
    pass


@dataclass
class ResolveContext:
    ip: str
    ua: str
    plex_token: str = ""


@dataclass
class Link:
    url: str
    provider: str
    expires_in: int = 0


def _ua(cfg: Dict[str, Any], ctx: ResolveContext) -> str:
    if cfg["provider"].get("ua_mode") == "fixed":
        return cfg["provider"].get("fixed_ua") or "Plex302/1.0"
    return ctx.ua or "Plex302/1.0"


def _resolve_115(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    """115 直链：Cookie 模式（网页版接口），不再依赖开放平台。

    和 TgtoDrive 的 STRM 一样：只要拿到 pick_code，就只剩 1 次 /files/download。
    """
    if not u115web.has_cookie(cfg):
        raise ProviderError("未配置 115 Cookie（设置 → 115）")
    info = u115web.file_url_by_path(cfg, cloud_path, _ua(cfg, ctx))
    url = info["url"]
    if cfg["provider"].get("force_https") and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    # 直接用 u115web 缓存里算好的剩余寿命（已按直链自带过期时间留了 1 分钟余量），
    # 让 nginx 后面的 302 缓存和直链缓存同时过期，避免播放中途再打一次 115。
    expires_in = int(info.get("expires_in") or 0)
    return Link(url=url, provider=f"115:{info.get('hit') or 'walk'}", expires_in=expires_in)

# ------------------------------------------------------------------ MoviePilot
# MP 的 115 存储走开放平台 /open/folder/get_info，一次请求就能按完整路径拿到
# pickcode —— 这正是网页 Cookie 做不到的（Cookie 只能按 cid 逐级列目录，3~4 次）。
# 所以分工：MP 负责「路径 → pickcode」，plex302 负责「pickcode → 直链 + 302」。
# Cookie 仍然要填：换直链走 /files/download，事件监听（局部入库）也用它。
_MP_PICK_TTL = 12 * 3600.0
_mp_pick: Dict[str, Dict[str, Any]] = {}
_mp_lock = threading.Lock()


def _mp_cache_get(path: str) -> str:
    with _mp_lock:
        item = _mp_pick.get(path)
        if item and float(item.get("expire_at") or 0) > time.time():
            return str(item.get("pick_code") or "")
    return ""


def _mp_cache_put(path: str, pick_code: str, size: int = 0, name: str = "") -> None:
    if not pick_code:
        return
    with _mp_lock:
        if len(_mp_pick) > 20000:
            _mp_pick.clear()
        _mp_pick[path] = {"pick_code": pick_code, "expire_at": time.time() + _MP_PICK_TTL}
    try:
        row = store.index_get(path) or {}
        if str(row.get("pick_code") or "") != pick_code:
            store.index_put(path, pick_code, int(size or row.get("size") or 0), name or str(row.get("name") or ""), "")
    except Exception as err:  # noqa: BLE001 索引坏了不能挡住播放
        logger.warn(f"[MP] 写索引失败：{err}")


def _mp_pick_for_path(cfg: Dict[str, Any], path: str) -> str:
    """路径 -> pickcode：内存 → 本地索引（事件监听写进来的）→ 问 MP。"""
    pick = _mp_cache_get(path)
    if pick:
        return pick
    try:
        row = store.index_get(path)
        if row and row.get("pick_code") and time.time() - float(row.get("updated_at") or 0) <= 7 * 86400:
            _mp_cache_put(path, str(row["pick_code"]), int(row.get("size") or 0), str(row.get("name") or ""))
            return str(row["pick_code"])
    except Exception:  # noqa: BLE001
        pass

    base = cfg["provider"].get("mp_base") or ""
    token = cfg["provider"].get("mp_token") or ""
    storage = cfg["provider"].get("mp_storage") or "u115"
    if not base:
        raise ProviderError("未配置 MoviePilot 地址")
    if not token:
        raise ProviderError("未配置 MoviePilot API Token（MP → 设置 → 系统 → API Token）")
    try:
        resp = requests.post(
            f"{base}/api/v1/storage/list",
            params={"token": token, "sort": "name"},
            json={"path": path, "storage": storage, "type": "file"},
            timeout=15,
        )
    except requests.RequestException as err:
        raise ProviderError(f"连不上 MoviePilot：{err}") from err
    if resp.status_code in (401, 403):
        raise ProviderError(f"MoviePilot 拒绝访问（HTTP {resp.status_code}），检查 API Token")
    if resp.status_code >= 400:
        raise ProviderError(f"MoviePilot 返回 HTTP {resp.status_code}：{resp.text[:120]}")
    try:
        items = resp.json()
    except ValueError as err:
        raise ProviderError("MoviePilot 返回的不是 JSON，检查地址是否指向 MP 的 Web 端口") from err
    if isinstance(items, dict):
        items = items.get("data") or []
    if not isinstance(items, list) or not items:
        raise ProviderError(f"MoviePilot 在 {path} 没找到文件：检查云盘前缀是否与 MP 里 115 存储的路径一致")
    item = next((x for x in items if isinstance(x, dict) and str(x.get("pickcode") or "")), None)
    if not item:
        raise ProviderError(f"MoviePilot 返回了 {len(items)} 项，但都没有 pickcode（存储类型填错？）")
    pick = str(item.get("pickcode"))
    _mp_cache_put(path, pick, int(item.get("size") or 0), str(item.get("name") or ""))
    return pick


def _resolve_mp(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    """MP 官方接口拿 pickcode → Cookie 换直链。首次 2 次请求，之后命中缓存 0 次。"""
    if not u115web.has_cookie(cfg):
        raise ProviderError("未配置 115 Cookie（MP 只负责取 pickcode，换直链仍要 Cookie）")
    path = "/" + str(cloud_path or "").strip("/")
    if path == "/":
        raise ProviderError("路径为空")
    pick = ""
    try:
        pick = _mp_pick_for_path(cfg, path)
    except ProviderError as err:
        # MP 挂了/没配好不能让播放挂掉：退回 Cookie 逐级解析（慢一点但能播）
        logger.warn(f"[MP] 取 pickcode 失败，回退 Cookie 逐级解析：{err}")
        return _resolve_115(cfg, cloud_path, ctx)
    ua = _ua(cfg, ctx)
    url = u115web.url_for_pick(cfg, pick, ua)
    if cfg["provider"].get("force_https") and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return Link(url=url, provider="mp", expires_in=u115web.url_ttl_remaining(pick, ua))



# ------------------------------------------------------------------ 115 官方 OpenAPI
# 和 plex-demo 完全同一条链路：folder/get_info{path} → ufile/downurl{pick_code}。
# 一次请求按完整路径定位，不需要逐级列目录，也不依赖网页 Cookie 的风控窗口。
_open_cache: Dict[str, Dict[str, Any]] = {}
_open_locks: Dict[str, threading.Lock] = {}
_open_guard = threading.Lock()
_OPEN_TTL_DEFAULT = 3600.0
_OPEN_TTL_MAX = 6 * 3600.0


def _open_url_ttl(url: str) -> float:
    """从 115 直链的 t=<过期时间戳> 读真实寿命，读不到给 1 小时。"""
    try:
        qs = parse_qs(url.split("?", 1)[1])
        stamp = int((qs.get("t") or ["0"])[0])
        if stamp > time.time():
            return max(300.0, min(_OPEN_TTL_MAX, stamp - time.time() - 60))
    except (IndexError, ValueError):
        pass
    return _OPEN_TTL_DEFAULT


def _open_lock(key: str) -> threading.Lock:
    with _open_guard:
        lock = _open_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _open_locks[key] = lock
        return lock


def _open_cache_get(key: str) -> str:
    with _open_guard:
        item = _open_cache.get(key)
        if not item:
            return ""
        if float(item.get("expire_at") or 0) <= time.time():
            _open_cache.pop(key, None)
            return ""
        return str(item.get("value") or "")


def _open_cache_put(key: str, url: str, ttl: float) -> None:
    with _open_guard:
        if len(_open_cache) > 20000:
            _open_cache.clear()
        _open_cache[key] = {"value": url, "expire_at": time.time() + ttl}


def _open_link_url(cfg: Dict[str, Any], pick: str, ua: str) -> str:
    """pick_code → 直链，带缓存 + 单飞：并发起播只打一次 115。"""
    key = f"{pick}|{ua}"
    cached = _open_cache_get(key)
    if cached:
        return cached
    lock = _open_lock(key)
    if not lock.acquire(timeout=20):
        raise ProviderError("115 直链解析排队超时，稍后重试")
    try:
        cached = _open_cache_get(key)
        if cached:
            return cached
        url, _name = u115open.download_url(cfg, pick, ua)
        _open_cache_put(key, url, _open_url_ttl(url))
        return url
    finally:
        lock.release()


def _resolve_open115(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    ua = _ua(cfg, ctx)
    try:
        pick = u115open.pickcode_by_path(cfg, cloud_path, ua)
        url = _open_link_url(cfg, pick, ua)
    except u115open.Open115Error as err:
        # 没登录/过期/接口抽风：有 Cookie 就退回 Cookie 逐级解析，别让播放挂掉
        if u115web.has_cookie(cfg):
            logger.warn(f"[115Open] {err}，回退 Cookie 解析")
            return _resolve_115(cfg, cloud_path, ctx)
        raise ProviderError(f"115 官方接口失败：{err}") from err
    if cfg["provider"].get("force_https") and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    ttl = _open_url_ttl(url)
    return Link(url=url, provider="115open", expires_in=int(ttl))


def _resolve_cd2(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    info = cd2.download_url(cloud_path)
    direct = (info.get("direct_url") or "").strip()
    if not direct:
        raise ProviderError("CD2 未返回云盘直链（需要 CD2 直链权限，或该网盘不支持直链）")
    return Link(url=direct, provider="cd2", expires_in=int(info.get("expires_in") or 0))


def _resolve_alist(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    base = cfg["provider"].get("alist_base") or ""
    token = cfg["provider"].get("alist_token") or ""
    if not base:
        raise ProviderError("未配置 AList 地址")
    try:
        resp = requests.post(
            f"{base.rstrip('/')}/api/fs/get",
            json={"path": cloud_path, "password": ""},
            headers={"Authorization": token, "User-Agent": _ua(cfg, ctx)},
            timeout=12,
        )
        payload = resp.json()
    except (requests.RequestException, ValueError) as err:
        raise ProviderError(f"AList 请求失败：{err}") from err
    if int(payload.get("code") or 0) != 200:
        raise ProviderError(payload.get("message") or f"AList 返回 code={payload.get('code')}")
    data = payload.get("data") or {}
    url = data.get("raw_url") or data.get("url") or ""
    if not url:
        raise ProviderError("AList 没有返回 raw_url")
    if url.startswith("/"):
        public = cfg["provider"].get("alist_public_base") or base
        url = public.rstrip("/") + url
    return Link(url=url, provider="alist")


def _resolve_custom(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    base = cfg["provider"].get("custom_url") or ""
    if not base:
        raise ProviderError("未配置自定义直链接口")
    headers = {"User-Agent": _ua(cfg, ctx), "X-Plex302-Path": quote(cloud_path, safe=""), "X-Plex302-Ip": ctx.ip}
    token = cfg["provider"].get("custom_token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = base + ("&" if "?" in base else "?") + "path=" + quote(cloud_path, safe="")
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        text = resp.text.strip()
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
            if isinstance(data, str):
                text = data
            elif isinstance(data, dict):
                text = data.get("url") or data.get("raw_url") or ""
            else:
                text = ""
    except (requests.RequestException, ValueError) as err:
        raise ProviderError(f"自定义接口请求失败：{err}") from err
    if not text.startswith("http"):
        raise ProviderError(f"自定义接口没有返回 http 直链：{text[:120]}")
    return Link(url=text, provider="custom")


def resolve(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    ptype = cfg["provider"]["type"]
    try:
        if ptype == "open115":
            return _resolve_open115(cfg, cloud_path, ctx)
        if ptype == "115":
            return _resolve_115(cfg, cloud_path, ctx)
        if ptype == "mp":
            return _resolve_mp(cfg, cloud_path, ctx)
        if ptype == "cd2":
            return _resolve_cd2(cfg, cloud_path, ctx)
        if ptype == "alist":
            return _resolve_alist(cfg, cloud_path, ctx)
        if ptype == "custom":
            return _resolve_custom(cfg, cloud_path, ctx)
        raise ProviderError("直链模式为 none，全部走 Plex 中转")
    except ProviderError:
        raise
    except Exception as err:  # noqa: BLE001
        logger.warn(f"[Provider] {ptype} 解析异常：{err}")
        raise ProviderError(str(err)) from err
