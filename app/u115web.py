"""115 网页版（Cookie）接口 —— 直链、目录、最近文件全靠 Cookie。

不再依赖 115 开放平台：
  GET /files/download?pick_code=<pc>   取文件直链（file_url，和请求 UA 绑定）
  GET /files?aid=1&cid=<cid>           列目录 / 返回 path 链路，可拼出完整路径
  GET /files/recent?limit=N&offset=0   最近变化的文件（按时间倒序）

Cookie 从浏览器 F12 → Network → 任意 webapi.115.com 请求里复制，
形如 `UID=xxx; CID=yyy; SEID=zzz; KID=www`。
Cookie 失效时 115 会返回 HTTP 405，这里统一转成可读错误。

两个坑（踩过）：
1. cid 是 64 位整数，全程必须当字符串传，转成 float 会丢精度，接口直接报“服务器开小差”
2. 直链绑定请求 UA，取链接时的 UA 必须和播放器的一致（默认跟随客户端 UA）
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import requests

from . import config, logger, store

BASE = os.getenv("P115_WEBAPI_BASE", "https://webapi.115.com").rstrip("/")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
EXPIRE_ERRNOS = {990001, 40101017, 40101019}

# 115 风控节奏：CMS 默认 API 请求间隔 2.0 秒，这里取 2.05，只慢不快。
# 后台任务（事件、入库、目录操作）全部走这个间隔；直链解析走 fast 通道（0.35 秒）
# ——直链有 5~10 分钟目录缓存 + 30 分钟 pick_code 缓存，实际很少打 115。
API_GAP = 2.05
FAST_GAP = 0.35

_pace_lock = threading.Lock()
_last_call = 0.0


def pace(fast: bool = False) -> None:
    """所有 115 请求的统一节流闸门。"""
    global _last_call
    gap = FAST_GAP if fast else API_GAP
    with _pace_lock:
        wait = _last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


class Web115Error(Exception):
    pass


def _count_call(n: int = 1) -> None:
    """按天累计真正发出去的 115 请求，概览里能直接核对。"""
    try:
        store.bump_115_call(n)
    except Exception:  # noqa: BLE001 统计失败不能影响播放
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


def _get(
    cfg: Dict[str, Any],
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
    base: Optional[str] = None,
    fast: bool = False,
) -> Dict[str, Any]:
    pace(fast)
    _count_call()
    url = (base or BASE) + path
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


def _get_with_ua(
    cfg: Dict[str, Any],
    path: str,
    params: Optional[Dict[str, Any]] = None,
    ua: str = "",
    timeout: int = 20,
) -> Dict[str, Any]:
    """和 _get 一样，但可以指定 UA —— 115 直链和请求 UA 绑定。"""
    pace(True)
    _count_call()
    url = BASE + path
    headers = {
        "Cookie": _cookie(cfg),
        "User-Agent": ua or DEFAULT_UA,
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
    errno = payload.get("errno") or payload.get("errNo") or 0
    if payload.get("state") is False and errno in EXPIRE_ERRNOS:
        raise Web115Error("115 Cookie 已失效，请重新复制")
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


# ------------------------------------------------------------------ 目录 / 直链
DIR_TTL = 600.0  # 目录缓存 10 分钟：少打 115，新文件靠 force 刷新兜底
PICK_TTL = 1800.0
INDEX_TTL = 7 * 86400.0  # 播放索引（路径 -> pick_code）有效期，命中失败会自动重建

_dir_cache: Dict[str, Dict[str, Any]] = {}
_pick_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


def _cache_get(bucket: Dict[str, Dict[str, Any]], key: str, ttl: float) -> Optional[Any]:
    now = time.time()
    with _cache_lock:
        item = bucket.get(key)
        if item and now - item["at"] < ttl:
            return item["value"]
        if item:
            bucket.pop(key, None)
    return None


def _cache_set(bucket: Dict[str, Dict[str, Any]], key: str, value: Any) -> None:
    with _cache_lock:
        if len(bucket) > 20000:
            bucket.clear()
        bucket[key] = {"value": value, "at": time.time()}


def list_dir(cfg: Dict[str, Any], cid: str, force: bool = False, fast: bool = False) -> List[Dict[str, Any]]:
    """列一个目录（cid 必须是字符串）。"""
    cid = str(cid or "0")
    if not force:
        cached = _cache_get(_dir_cache, cid, DIR_TTL)
        if cached is not None:
            return cached
    items: List[Dict[str, Any]] = []
    offset = 0
    while True:
        payload = _get(
            cfg,
            "/files",
            {
                "aid": 1, "cid": cid, "show_dir": 1, "limit": 1000, "offset": offset,
                "o": "user_ptime", "asc": 0, "natsort": 1, "fc_mix": 0, "format": "json",
            },
            fast=fast,
        )
        data = payload.get("data")
        if isinstance(data, dict):
            data = data.get("list") or data.get("data") or []
        if not isinstance(data, list) or not data:
            break
        items.extend(x for x in data if isinstance(x, dict))
        total = int(payload.get("count") or payload.get("total") or 0)
        offset += len(data)
        if len(data) < 1000 or (total and offset >= total):
            break
    _cache_set(_dir_cache, cid, items)
    return items


def _norm_name(text: str) -> str:
    return str(text or "").strip().replace("\u3000", " ").lower()


def _match(items: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    target = _norm_name(name)
    for item in items:
        if _norm_name(_pick(item, "n", "file_name", "name")) == target:
            return item
    return None


def is_dir_item(item: Dict[str, Any]) -> bool:
    """判断 /files 返回项是目录还是文件。

    真实 115 的**文件项也带 cid**（那是父目录 id），只看 cid 会把视频当成
    子目录递归，播放索引一条都写不进去。目录项的特征是：有自身 cid、
    没有 fid、没有 pick_code。
    """
    explicit = _pick(item, "is_dir", "is_folder", default=None)
    if explicit is not None and str(explicit).lower() in ("1", "true", "yes"):
        return True
    fid = str(_pick(item, "fid", "file_id", default="") or "")
    pc = str(_pick(item, "pc", "pick_code", "pickcode", default="") or "")
    cid = str(_pick(item, "cid", "id", default="") or "")
    return bool(cid) and not fid and not pc


def dir_cid(item: Dict[str, Any]) -> str:
    """目录项的自身 cid（文件项的 cid 是父目录，别拿它当子目录）。"""
    return str(_pick(item, "cid", "id", default="") or "")


# 同一路径只让一个线程去解析，避免 Plex 起播时并发请求把同一个目录连打好几次
_resolve_locks: Dict[str, threading.Lock] = {}
_resolve_locks_guard = threading.Lock()


def _resolve_lock(key: str) -> threading.Lock:
    with _resolve_locks_guard:
        if len(_resolve_locks) > 5000:
            _resolve_locks.clear()
        return _resolve_locks.setdefault(key, threading.Lock())


def resolve_file(cfg: Dict[str, Any], cloud_path: str) -> Dict[str, Any]:
    """云盘绝对路径 -> {pick_code, name, size}（同一路径并发只解析一次）。

    查找顺序（越靠前越省 115 请求）：
      1. 进程内缓存
      2. SQLite 播放索引（起播命中 = 0 次列目录，只剩 1 次 /files/download）
      3. 逐级列目录（只有从没播过、也没被事件索引过的新路径才走这里）
    """
    path = "/" + str(cloud_path or "").strip("/")
    if path == "/":
        raise Web115Error("路径为空")
    cached = _cache_get(_pick_cache, path, PICK_TTL)
    if cached is not None:
        return cached
    lock = _resolve_lock(path)
    if not lock.acquire(timeout=20):
        raise Web115Error("115 目录解析排队超时，稍后重试")
    try:
        return _resolve_file_locked(cfg, path)
    finally:
        lock.release()


def _index_lookup(path: str) -> Optional[Dict[str, Any]]:
    try:
        row = store.index_get(path)
    except Exception:  # noqa: BLE001 索引坏了不能挡住播放
        return None
    if not row:
        return None
    if time.time() - float(row.get("updated_at") or 0) > INDEX_TTL:
        return None
    return {
        "pick_code": str(row["pick_code"]),
        "name": str(row.get("name") or ""),
        "size": int(row.get("size") or 0),
        "hit": "index",
    }


def _index_save(path: str, info: Dict[str, Any], cid: str = "") -> None:
    try:
        store.index_put(path, info["pick_code"], info.get("size") or 0, info.get("name") or "", cid)
    except Exception as err:  # noqa: BLE001
        logger.warn(f"[115] 写播放索引失败：{err}")


def _resolve_file_locked(cfg: Dict[str, Any], path: str) -> Dict[str, Any]:
    cached = _cache_get(_pick_cache, path, PICK_TTL)
    if cached is not None:
        return cached

    indexed = _index_lookup(path)
    if indexed is not None:
        _cache_set(_pick_cache, path, indexed)
        return indexed

    parts = [p for p in path.strip("/").split("/") if p]
    cid = "0"
    node: Optional[Dict[str, Any]] = None
    for index, part in enumerate(parts):
        items = list_dir(cfg, cid, fast=True)
        node = _match(items, part)
        if node is None:
            # 目录里东西太多时试一次强制刷新，避免缓存里是旧的
            items = list_dir(cfg, cid, force=True, fast=True)
            node = _match(items, part)
        if node is None:
            raise Web115Error(f"115 里找不到路径：{'/' + '/'.join(parts[: index + 1])}")
        if index < len(parts) - 1:
            if not is_dir_item(node):
                raise Web115Error(f"{part} 不是目录")
            sub = dir_cid(node)
            if not sub:
                raise Web115Error(f"{part} 不是目录")
            cid = sub

    if node is None:
        raise Web115Error("路径为空")
    pc = str(_pick(node, "pc", "pick_code", "pickcode"))
    if not pc:
        raise Web115Error("115 没有返回 pick_code（可能是目录）")
    info = {
        "pick_code": pc,
        "name": str(_pick(node, "n", "file_name", "name")),
        "size": int(_pick(node, "s", "file_size", "size", default=0) or 0),
        "hit": "walk",
    }
    _cache_set(_pick_cache, path, info)
    _index_save(path, info, cid)
    return info


def download_url(cfg: Dict[str, Any], pick_code: str, ua: str = "") -> str:
    """用 pick_code 换直链。返回的 URL 和请求时的 UA 绑定。"""
    if not pick_code:
        raise Web115Error("pick_code 为空")
    params = {"pick_code": pick_code}
    payload = _get_with_ua(cfg, "/files/download", params, ua)
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}
    url = str(data.get("file_url") or data.get("url") or "")
    if not url and "state" in payload and not payload.get("state"):
        # 少数版本参数名是 pickcode（没有下划线）
        payload = _get_with_ua(cfg, "/files/download", {"pickcode": pick_code}, ua)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        url = str(data.get("file_url") or data.get("url") or "")
    if not url:
        raise Web115Error("115 没有返回直链（pick_code 可能已失效）")
    return url


# ------------------------------------------------------------------ 直链缓存
# 115 直链带 t=<过期时间戳>，按它算缓存寿命；读不到就给 6 小时。
# 不能只给 30 分钟：一部电影播到 29 分钟缓存过期，Plex 重新请求就又去打 115，
# 这正是「连续播放 30 分钟出现多次连接」的第二个原因。
# 缓存键 = pick_code + UA 指纹：同一部片在同一次播放里反复请求（拖动、重连、
# Plex 并发探测）全部命中缓存，不会再去打 115。
URL_TTL_DEFAULT = 6 * 3600.0
URL_TTL_MAX = 6 * 3600.0
URL_TTL_MIN = 300.0

_url_cache: Dict[str, Dict[str, Any]] = {}
_url_locks: Dict[str, threading.Lock] = {}
_url_locks_guard = threading.Lock()


def _ua_key(ua: str) -> str:
    return hashlib.sha1((ua or "").encode("utf-8", "ignore")).hexdigest()[:12]


def _url_key(pick_code: str, ua: str) -> str:
    return f"{pick_code}|{_ua_key(ua)}"


def _url_lock(key: str) -> threading.Lock:
    with _url_locks_guard:
        if len(_url_locks) > 20000:
            _url_locks.clear()
        return _url_locks.setdefault(key, threading.Lock())


def _expiry_from_url(url: str) -> float:
    """从直链里读真实过期时间（115 用 t=<秒级时间戳>），读不到返回 0。"""
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return 0.0
    for name in ("t", "expire", "expires", "deadline"):
        values = query.get(name)
        if not values:
            continue
        try:
            ts = float(values[0])
        except (TypeError, ValueError):
            continue
        if ts > 1e12:  # 毫秒时间戳
            ts /= 1000.0
        if ts > time.time():
            return ts
    return 0.0


def _url_ttl(url: str) -> float:
    expire_at = _expiry_from_url(url)
    if not expire_at:
        return URL_TTL_DEFAULT
    ttl = expire_at - time.time() - 60  # 留 1 分钟余量，别把刚好过期的链接发出去
    return max(URL_TTL_MIN, min(URL_TTL_MAX, ttl))


def url_for_pick(cfg: Dict[str, Any], pick_code: str, ua: str = "", force: bool = False) -> str:
    """pick_code -> 直链，带缓存 + 单飞：并发请求只会有一次真的打 115。"""
    key = _url_key(pick_code, ua)
    if not force:
        cached = _cache_get_url(key)
        if cached:
            return cached
    lock = _url_lock(key)
    if not lock.acquire(timeout=20):
        raise Web115Error("115 直链解析排队超时，稍后重试")
    try:
        if not force:
            # 抢到锁后再看一次：可能别的线程刚刚取回来了
            cached = _cache_get_url(key)
            if cached:
                return cached
        url = download_url(cfg, pick_code, ua)
        ttl = _url_ttl(url)
        with _cache_lock:
            if len(_url_cache) > 20000:
                _url_cache.clear()
            _url_cache[key] = {"value": url, "at": time.time(), "expire_at": time.time() + ttl}
        return url
    finally:
        lock.release()


def _cache_get_url(key: str) -> str:
    with _cache_lock:
        item = _url_cache.get(key)
        if not item:
            return ""
        if float(item.get("expire_at") or 0) <= time.time():
            _url_cache.pop(key, None)
            return ""
        return str(item["value"])


def url_ttl_remaining(pick_code: str, ua: str) -> int:
    """这条直链在本进程缓存里还剩多少秒可用。

    给 resolver 的 302 缓存用：两层缓存同生共死，不会一层还热着、另一层
    已经过期导致重复去打 115。
    """
    with _cache_lock:
        item = _url_cache.get(_url_key(pick_code, ua))
        if not item:
            return 0
        return max(0, int(float(item.get("expire_at") or 0) - time.time()))


def clear_url_cache(pick_code: str = "") -> None:
    with _cache_lock:
        if not pick_code:
            _url_cache.clear()
            return
        for key in [k for k in _url_cache if k.split("|", 1)[0] == pick_code]:
            _url_cache.pop(key, None)


def file_url_by_path(cfg: Dict[str, Any], cloud_path: str, ua: str = "") -> Dict[str, Any]:
    """云盘路径 -> 直链（Cookie 模式，替代原来的开放平台接口）。

    命中播放索引时只发 1 个请求（/files/download）；索引里的 pick_code 失效时
    自动丢掉索引、重新解析一次，避免一次改名导致这个文件永远播不了。
    """
    info = resolve_file(cfg, cloud_path)
    try:
        url = url_for_pick(cfg, info["pick_code"], ua)
    except Web115Error:
        if info.get("hit") != "index":
            raise
        path = "/" + str(cloud_path or "").strip("/")
        logger.warn(f"[115] 索引里的 pick_code 已失效，重建：{path}")
        store.index_delete(path)
        with _cache_lock:
            _pick_cache.pop(path, None)
            _url_cache.pop(_url_key(info["pick_code"], ua), None)
        info = resolve_file(cfg, cloud_path)
        url = url_for_pick(cfg, info["pick_code"], ua, force=True)
    return {
        "url": url,
        "name": info["name"],
        "pick_code": info["pick_code"],
        "hit": info.get("hit", ""),
        "expires_in": url_ttl_remaining(info["pick_code"], ua),
    }


def clear_file_cache() -> None:
    with _cache_lock:
        _pick_cache.clear()
        _url_cache.clear()


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


def clear_path_cache() -> None:
    """目录改名/移动后，缓存里的旧路径要立刻失效。"""
    with _path_lock:
        _path_cache.clear()


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
    """测试 Cookie：最近文件 + 操作事件 + 目录反查 + 取一次直链。"""
    items = recent(cfg, limit=5)
    sample = items[0] if items else None
    path = ""
    if sample and sample.get("cid"):
        path = folder_path(cfg, sample["cid"]).rstrip("/") + "/" + sample["name"]
    result: Dict[str, Any] = {"ok": True, "count": len(items), "sample": sample, "path": path}
    # 顺手验证「路径 -> pick_code -> 直链」这条真正的播放链路
    if path and sample and not sample.get("is_dir"):
        try:
            link = file_url_by_path(cfg, path, DEFAULT_UA)
            result["link"] = {"pick_code": link["pick_code"], "url": link["url"][:120] + "…"}
        except Web115Error as err:
            result["link_error"] = str(err)
    return result


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
