"""115 开放平台（官方 OpenAPI）：扫码登录、按路径取 pickcode、换直链。

播放链路只用这套官方接口，和 plex-demo 完全同源：
    POST /open/folder/get_info {path}   → pick_code（一次请求按完整路径定位）
    POST /open/ufile/downurl  {pick_code} → 直链

凭据来自「设备码扫码登录」，access_token 2 小时过期、refresh_token 自动续期，
不需要去 115 开放平台申请应用（client_id 复用 plex-demo 的，见 CLIENT_ID）。

115 网页 Cookie 只留给「操作事件 → 局部入库」（官方接口看不到移动事件），
所以两套凭据互不影响，缺一套只影响对应功能。
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

from . import config as config_mod
from . import logger

# 复用 plex-demo 已备案的开放平台应用，用户不需要自己申请
CLIENT_ID = "100195313"
# 允许用环境变量指到别处（CI 用假服务验证整条 302 链路；也能给代理/镜像用）
AUTH_HOST = (os.getenv("P115_OPEN_AUTH_BASE") or "https://passportapi.115.com").rstrip("/")
API_HOST = (os.getenv("P115_OPEN_API_BASE") or "https://proapi.115.com").rstrip("/")
QR_HOST = "https://qrcodeapi.115.com"

TIMEOUT = 8  # 单次 115 请求上限；后端 /api/resolve 还有 8s 总闸，别让首播干等
UA_FALLBACK = "Plex302/1.0"

# access_token 过期错误码（115 用 4014xxxx 段）
_TOKEN_CODES = {40140103, 40140104, 40140105, 40140106, 40140107, 40101017}


class Open115Error(Exception):
    def __init__(self, message: str, need_login: bool = False) -> None:
        super().__init__(message)
        self.need_login = need_login


_lock = threading.RLock()
_refresh_lock = threading.Lock()
_login_lock = threading.Lock()
# 扫码会话：uid -> {verifier, time, sign}
_pending: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------- 基础工具
def _post(url: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    try:
        resp = requests.post(url, data=data, headers=headers or {}, timeout=TIMEOUT)
        return resp.json()
    except requests.RequestException as err:
        raise Open115Error(f"连不上 115：{err}") from err
    except ValueError as err:
        raise Open115Error("115 返回的不是 JSON") from err


def _get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        return resp.json()
    except requests.RequestException as err:
        raise Open115Error(f"连不上 115：{err}") from err
    except ValueError as err:
        raise Open115Error("115 返回的不是 JSON") from err


def _is_token_error(payload: Dict[str, Any]) -> bool:
    code = payload.get("code")
    try:
        if int(code) in _TOKEN_CODES:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(payload.get("message") or payload.get("msg") or "")
    return "access_token" in msg or "登录" in msg


def _save(cfg: Dict[str, Any]) -> None:
    """只把 u115_open 字段合并进**最新磁盘配置**后落盘。

    cfg 是请求开始时的 deepcopy 快照。以前直接 config.save(cfg) 会把整份旧
    配置写回去：只要 token 刷新和用户「设置页保存」撞在一起，用户的挂载映射、
    Plex Token、密码等就会被回滚。并发刷新时还会各拿一份旧 refresh_token 去刷。
    这里改成以磁盘最新配置为准，只覆盖 token 相关字段。
    """
    try:
        fresh = config_mod.load(force=True)
        src = cfg.get("u115_open") or {}
        if src:
            target = fresh.setdefault("u115_open", {})
            for key in ("access_token", "refresh_token", "expires_at", "user_name", "client_id"):
                if key in src:
                    target[key] = src[key]
        config_mod.save(fresh)
    except Exception as err:  # noqa: BLE001
        logger.warn(f"[115Open] 保存 token 失败：{err}")


# ---------------------------------------------------------------- token 管理
def token_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    o = cfg.get("u115_open") or {}
    expires_at = int(o.get("expires_at") or 0)
    return {
        "logged_in": bool(o.get("access_token") or o.get("refresh_token")),
        "user_name": str(o.get("user_name") or ""),
        "expires_at": expires_at,
        "expires_in": max(0, expires_at - int(time.time())),
        "client_id": str(o.get("client_id") or CLIENT_ID),
    }


def _store_token(cfg: Dict[str, Any], data: Dict[str, Any], user_name: str = "") -> None:
    o = cfg.setdefault("u115_open", {})
    access = str(data.get("access_token") or "")
    refresh = str(data.get("refresh_token") or "")
    expires_in = int(data.get("expires_in") or 7200)
    if access:
        o["access_token"] = access
    if refresh:
        o["refresh_token"] = refresh
    # 只有确实拿到新 token 才推进过期时间。以前即使 115 没返回 access_token
    # 也把 expires_at 推到 2 小时后，于是旧（已失效）token 会被当成有效一直
    # 用到过期，播放全数回源。
    if access or refresh:
        o["expires_at"] = int(time.time() + max(300, expires_in) - 300)
    if user_name:
        o["user_name"] = user_name
    _save(cfg)


_last_refresh_at = 0.0


def _refresh(cfg: Dict[str, Any], force: bool = False) -> str:
    """在锁内以磁盘最新配置为准刷新，避免并发请求各刷一次 / 覆盖用户设置。

    force=True 用于 115 明确返回 token 错误时强制续期；但 5 秒内刚刷过就不再
    刷，否则并发的两个请求会拿已轮换的 refresh_token 连刷两次导致登录态失效。
    """
    global _last_refresh_at
    with _refresh_lock:
        fresh = config_mod.load(force=True)
        o = fresh.get("u115_open") or {}
        access = str(o.get("access_token") or "")
        just_refreshed = time.time() - _last_refresh_at < 5
        if (
            access
            and int(o.get("expires_at") or 0) > time.time() + 60
            and (not force or just_refreshed)
        ):
            return access
        refresh_token = str(o.get("refresh_token") or "")
        if not refresh_token:
            raise Open115Error("还没有登录 115 开放平台，先去设置页扫码", need_login=True)
        payload = _post(f"{AUTH_HOST}/open/refreshToken", {"refresh_token": refresh_token})
        if not payload.get("state"):
            raise Open115Error(
                f"刷新 115 登录态失败（{payload.get('message') or payload.get('code') or '未知原因'}），请重新扫码",
                need_login=True,
            )
        data = payload.get("data") or {}
        _store_token(fresh, data)
        _last_refresh_at = time.time()
        logger.info("[115Open] access_token 已刷新")
        return str(data.get("access_token") or "")


def ensure_token(cfg: Dict[str, Any]) -> str:
    # cfg 是请求开始时的快照；必须读最新配置，否则同一个请求里的多个并发调用
    # 会一直看到旧 token 反复刷新（115 的 refresh_token 每次都会轮换）。
    o = config_mod.load().get("u115_open") or {}
    access = str(o.get("access_token") or "")
    if access and int(o.get("expires_at") or 0) > time.time() + 60:
        return access
    return _refresh(cfg)


def logout(cfg: Dict[str, Any]) -> None:
    o = cfg.setdefault("u115_open", {})
    o["access_token"] = ""
    o["refresh_token"] = ""
    o["expires_at"] = 0
    o["user_name"] = ""
    _save(cfg)
    logger.info("[115Open] 已退出登录")


# ---------------------------------------------------------------- 官方 API
def _api(
    cfg: Dict[str, Any],
    method: str,
    path: str,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    ua: str = "",
    retry: bool = True,
) -> Any:
    token = ensure_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    if ua:
        headers["User-Agent"] = ua
    try:
        resp = requests.request(
            method, f"{API_HOST}{path}", data=data, params=params, headers=headers, timeout=TIMEOUT
        )
        payload = resp.json()
    except requests.RequestException as err:
        raise Open115Error(f"请求 115 失败：{err}") from err
    except ValueError as err:
        raise Open115Error("115 返回的不是 JSON（可能被限流）") from err

    if not payload.get("state"):
        if retry and _is_token_error(payload):
            # 以前只把请求副本里的 expires_at 置 0，刷新逻辑读的却是副本，
            # 于是「别人刚刷新过」的判断会把这次强制刷新直接跳过，重试仍用旧
            # token 再失败一次。改成走真正带 force 的刷新。
            _refresh(cfg, force=True)
            return _api(cfg, method, path, data=data, params=params, ua=ua, retry=False)
        raise Open115Error(str(payload.get("message") or f"115 返回错误码 {payload.get('code')}"))
    return payload.get("data")


def user_info(cfg: Dict[str, Any]) -> Dict[str, Any]:
    data = _api(cfg, "GET", "/open/user/info")
    return data if isinstance(data, dict) else {}


def pickcode_by_path(cfg: Dict[str, Any], path: str, ua: str = "") -> str:
    """按完整路径直接拿 pickcode —— 这正是网页 Cookie 做不到的一步。"""
    clean = "/" + str(path or "").strip("/")
    if clean == "/":
        raise Open115Error("路径为空")
    data = _api(cfg, "POST", "/open/folder/get_info", data={"path": clean}, ua=ua)
    if isinstance(data, list):
        data = data[0] if data else {}
    pick = str((data or {}).get("pick_code") or "")
    if not pick:
        raise Open115Error(f"115 里找不到：{clean}")
    return pick


def download_url(cfg: Dict[str, Any], pick_code: str, ua: str = "") -> Tuple[str, str]:
    """pick_code → (直链, 文件名)。链接会绑定请求时的 UA，所以必须透传客户端 UA。"""
    if not pick_code:
        raise Open115Error("pick_code 为空")
    data = _api(cfg, "POST", "/open/ufile/downurl", data={"pick_code": pick_code}, ua=ua or UA_FALLBACK)
    if isinstance(data, list):  # 部分版本返回单元素数组（folder/get_info 已兼容）
        data = data[0] if data else {}
    if isinstance(data, dict):
        for item in data.values():
            if not isinstance(item, dict):
                continue
            raw = item.get("url")
            # 115 有时给 {"url": {"url": "https://..."}}，有时直接给
            # {"url": "https://..."}。以前在字符串上调用 .get() 会抛
            # AttributeError，扁平结构的兜底永远走不到。
            url = (raw.get("url") if isinstance(raw, dict) else raw) or item.get("file_url")
            if isinstance(url, str) and url.startswith("http"):
                return url, str(item.get("file_name") or "")
    raise Open115Error("115 没有返回直链（pick_code 可能已失效）")


# ---------------------------------------------------------------- 扫码登录
def _b64(raw: bytes, strip_pad: bool = False) -> str:
    text = base64.b64encode(raw).decode()
    return text.rstrip("=") if strip_pad else text


def start_login() -> Dict[str, Any]:
    """申请设备码。返回二维码内容 + uid，前端拿 qrcode 去渲染二维码。"""
    # 和 plex-demo 完全一致的 PKCE 生成方式：标准 base64，verifier 去掉尾部 '='
    verifier = _b64(os.urandom(50), strip_pad=True)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    payload = _post(
        f"{AUTH_HOST}/open/authDeviceCode",
        {"client_id": CLIENT_ID, "code_challenge": challenge, "code_challenge_method": "sha256"},
    )
    if not payload.get("state"):
        raise Open115Error(f"申请二维码失败：{payload.get('message') or payload.get('code')}")
    data = payload.get("data") or {}
    uid = str(data.get("uid") or "")
    if not uid:
        raise Open115Error("115 没有返回 uid")
    with _login_lock:
        if len(_pending) > 50:
            _pending.clear()
        _pending[uid] = {
            "verifier": verifier,
            "time": data.get("time"),
            "sign": data.get("sign"),
            "qrcode": str(data.get("qrcode") or ""),
        }
    return {
        "uid": uid,
        "qrcode": str(data.get("qrcode") or ""),
        "expires_in": 300,
    }


def qr_text(uid: str) -> str:
    with _login_lock:
        return str((_pending.get(uid) or {}).get("qrcode") or "")


def poll_login(uid: str) -> Dict[str, Any]:
    with _login_lock:
        sess = _pending.get(uid)
    if not sess:
        raise Open115Error("二维码已失效，重新获取")
    try:
        payload = _get(
            f"{QR_HOST}/get/status/", {"uid": uid, "time": sess.get("time"), "sign": sess.get("sign")}
        )
    except Open115Error as err:
        # 二维码接口偶发超时（跨境线路尤其明显）：不算失败，让前端继续轮询
        return {"status": 0, "message": f"查询扫码状态超时，正在重试：{err}"}
    data = payload.get("data") or {}
    status = data.get("status")
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    return {"status": status, "message": str(data.get("msg") or data.get("message") or "")}


def finish_login(cfg: Dict[str, Any], uid: str) -> Dict[str, Any]:
    with _login_lock:
        sess = _pending.get(uid)
        if not sess:
            raise Open115Error("二维码已失效，重新获取")
        verifier = str(sess.get("verifier") or "")
    payload = _post(f"{AUTH_HOST}/open/deviceCodeToToken", {"uid": uid, "code_verifier": verifier})
    if not payload.get("state"):
        raise Open115Error(f"换取 token 失败：{payload.get('message') or payload.get('code')}")
    data = payload.get("data") or {}
    _store_token(cfg, data)
    with _login_lock:
        _pending.pop(uid, None)
    name = ""
    try:
        info = user_info(cfg)
        name = str(info.get("user_name") or info.get("user_id") or "")
        if name:
            o = cfg.setdefault("u115_open", {})
            o["user_name"] = name
            _save(cfg)
    except Open115Error as err:
        logger.warn(f"[115Open] 登录成功但读取用户信息失败：{err}")
    logger.info(f"[115Open] 扫码登录成功{('：' + name) if name else ''}")
    return {"user_name": name, **token_state(cfg)}
