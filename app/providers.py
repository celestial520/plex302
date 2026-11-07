"""直链 Provider：只有 115 官方接口（和 plex-demo 同一条链路）。

为什么只剩一种：官方接口按完整路径一次定位 pickcode、再一次换直链，两个请求
就够；而按 cid 逐级列目录一个文件要 3~4 个请求，既慢又容易撞风控。所以不再
提供 AList / CD2 / 自定义接口 / 纯 Cookie 这些选项，界面上也选不到。

网页 Cookie 只在官方接口临时不可用时作为兜底（token 过期、proapi 抽风），
它不是选项，是一条保命路径。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import parse_qs

from . import logger, u115open, u115web


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


def resolve(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    """固定走 115 官方接口；失败就回源 Plex，不再有「换个来源试试」的分支。"""
    try:
        return _resolve_open115(cfg, cloud_path, ctx)
    except ProviderError:
        raise
    except Exception as err:  # noqa: BLE001
        logger.warn(f"[Provider] 115 官方接口解析异常：{err}")
        raise ProviderError(str(err)) from err
