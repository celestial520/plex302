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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
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
    if expires_in <= 0:
        # 兜底通道拿到已过期直链时明确回源，别让 resolver 退回默认的
        # url_cache_ttl（1800s）把 403 链接缓存半小时。
        raise ProviderError("115 网页接口返回的直链已过期")
    return Link(url=url, provider=f"115:{info.get('hit') or 'walk'}", expires_in=expires_in)

# ------------------------------------------------------------------ 115 官方 OpenAPI
# 和 plex-demo 完全同一条链路：folder/get_info{path} → ufile/downurl{pick_code}。
# 一次请求按完整路径定位，不需要逐级列目录，也不依赖网页 Cookie 的风控窗口。
_open_cache: Dict[str, Dict[str, Any]] = {}
_open_locks: Dict[str, threading.Lock] = {}
_open_guard = threading.Lock()
_OPEN_TTL_DEFAULT = 3600.0
_OPEN_TTL_MAX = 6 * 3600.0

# 路径 → pick_code。直链会过期（约 30 分钟）、每台设备 UA 又各自一份，
# 但「这个路径对应哪个 pick_code」几乎不变。缓存它之后，换设备、拖动进度
# 条、直链过期重取都只需 1 次 downurl，不再每次都先 folder/get_info ——
# 首播链路从两次 115 往返压到一次，这是外网起播最直观的一段等待。
_pick_cache: Dict[str, Dict[str, Any]] = {}
_PICK_TTL = 6 * 3600.0


def _open_url_ttl(url: str) -> float:
    """从 115 直链的 t=<过期时间戳> 读真实寿命；读不到给 1 小时，已过期给 0。

    以前用 max(300, ...) 把剩余不足 5 分钟的寿命抬到 300 秒，且 t 已过期时
    直接落到默认 1 小时 —— 三层缓存会一起把过期直链继续 302 给播放器，
    表现为播放中途 403 / 反复重试。TTL 绝不能超过直链真实剩余寿命。
    """
    try:
        qs = parse_qs(url.split("?", 1)[1])
        stamp = int((qs.get("t") or ["0"])[0])
        if stamp <= 0:
            return _OPEN_TTL_DEFAULT
        left = stamp - time.time() - 60  # 提前 1 分钟失效
        if left <= 0:
            return 0.0
        return min(_OPEN_TTL_MAX, left)
    except (IndexError, ValueError):
        return _OPEN_TTL_DEFAULT


def _open_lock(key: str) -> threading.Lock:
    with _open_guard:
        lock = _open_locks.get(key)
        if lock is None:
            if len(_open_locks) > 20000:
                # 只淘汰空闲锁。整表 clear() 会把正在被持有的锁对象丢掉，
                # 后来的请求拿到同 key 的新锁，单飞失效、并发重复打 115。
                for k, l in list(_open_locks.items()):
                    if not l.locked():
                        _open_locks.pop(k, None)
            lock = _open_locks.get(key)
            if lock is None:
                lock = _open_locks[key] = threading.Lock()
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


def _pick_cache_get(path: str) -> str:
    with _open_guard:
        item = _pick_cache.get(path)
        if not item:
            return ""
        if float(item.get("expire_at") or 0) <= time.time():
            _pick_cache.pop(path, None)
            return ""
        return str(item.get("value") or "")


def _pick_cache_put(path: str, pick: str) -> None:
    with _open_guard:
        if len(_pick_cache) > 20000:
            _pick_cache.clear()
        _pick_cache[path] = {"value": pick, "expire_at": time.time() + _PICK_TTL}


def _pick_cache_drop(path: str) -> None:
    with _open_guard:
        _pick_cache.pop(path, None)


# 115 偶发长尾：线上实测同样一次 downurl 有 0.4s 和 5.29s 两种结果，
# 而这段开销是串在「首次播放某个 (文件, UA) 组合」里的，播放器只能干等。
# 做请求对冲：超过 HEDGE_AFTER 还没回来就并发再问一次，取先到的结果。
# 只在长尾时触发，正常 0.3~0.5 秒的请求完全不受影响；downurl 是读接口，
# 重复请求幂等。用独立有界线程池，不占用 asyncio.to_thread 的默认池。
_HEDGE_AFTER = 1.2
_HEDGE_MAX_WAIT = 6.5
_HEDGE_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="p115-hedge")


def _download_hedged(cfg: Dict[str, Any], pick: str, ua: str) -> Tuple[str, str]:
    futures = [_HEDGE_POOL.submit(u115open.download_url, cfg, pick, ua)]
    # 必须用 FIRST_COMPLETED：wait 默认 ALL_COMPLETED，会把快结果硬等到慢的
    # 那个也回来，对冲就白做了。
    done, _ = wait(futures, timeout=_HEDGE_AFTER, return_when=FIRST_COMPLETED)
    if not done:
        logger.info(f"[115Open] 直链请求超过 {_HEDGE_AFTER:g}s，并发重试一次以压缩长尾")
        futures.append(_HEDGE_POOL.submit(u115open.download_url, cfg, pick, ua))
    last: Optional[Exception] = None
    deadline = time.monotonic() + _HEDGE_MAX_WAIT
    while futures:
        done, pending = wait(
            futures, timeout=max(0.0, deadline - time.monotonic()),
            return_when=FIRST_COMPLETED,
        )
        if not done:
            break
        for f in done:
            try:
                return f.result()
            except Exception as err:  # noqa: BLE001
                last = err
        # 先回来的那个失败了：继续等备用请求，别急着把错误抛出去
        futures = list(pending)
    if last is not None:
        raise last
    raise u115open.Open115Error("115 直链请求超时")


def _open_link_url(cfg: Dict[str, Any], pick: str, ua: str) -> str:
    """pick_code → 直链，带缓存 + 单飞 + 长尾对冲：并发起播只打一次 115。"""
    key = f"{pick}|{ua}"
    cached = _open_cache_get(key)
    if cached:
        return cached
    lock = _open_lock(key)
    # 以前等 20 秒：并发起播时后到的请求可能干等满 20 秒才报错，
    # 播放器早就断了。后端整体只有 8 秒硬上限，锁不该比它还长。
    if not lock.acquire(timeout=6):
        raise ProviderError("115 直链解析排队超时，稍后重试")
    try:
        cached = _open_cache_get(key)
        if cached:
            return cached
        url, _name = _download_hedged(cfg, pick, ua)
        ttl = _open_url_ttl(url)
        if ttl > 0:
            _open_cache_put(key, url, ttl)
        return url
    finally:
        lock.release()


def _resolve_open115(cfg: Dict[str, Any], cloud_path: str, ctx: ResolveContext) -> Link:
    ua = _ua(cfg, ctx)
    try:
        pick = _pick_cache_get(cloud_path)
        from_cache = bool(pick)
        if not pick:
            pick = u115open.pickcode_by_path(cfg, cloud_path, ua)
            _pick_cache_put(cloud_path, pick)
        try:
            url = _open_link_url(cfg, pick, ua)
        except u115open.Open115Error:
            if not from_cache:
                raise
            # 缓存里的 pick_code 可能因文件被移动/重新入库而失效。丢掉重取一次，
            # 比直接回源 Plex 好；新取的 pick 再失败就算了，不要越拖越久。
            _pick_cache_drop(cloud_path)
            pick = u115open.pickcode_by_path(cfg, cloud_path, ua)
            _pick_cache_put(cloud_path, pick)
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
    if ttl <= 0:
        # 115 偶尔会复用一条已过期的直链。再要一次；仍然过期就明确回源，
        # 绝不能把会 403 的链接 302 给播放器（拖动进度条时尤其明显）。
        try:
            url, _ = u115open.download_url(cfg, pick, ua)
            ttl = _open_url_ttl(url)
            if ttl > 0:
                _open_cache_put(f"{pick}|{ua}", url, ttl)
        except u115open.Open115Error:
            ttl = 0.0
    if ttl <= 0:
        raise ProviderError("115 返回的直链已过期")
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
