"""115 网页版扫码登录：扫一次码，直接拿到网页 Cookie（UID/CID/SEID/KID）。

CMS（cloud-media-sync）走的就是这条路——它的「115扫码登录」拿到的不是
开放平台 token，而是网页 Cookie。所以这里照着做，用户不用再开 F12 复制。

两套授权互相独立、各管一段：

    网页扫码  → Cookie（UID/CID/SEID/KID）→ 读「最近操作」事件、局部入库
    设备码扫码 → access_token + refresh_token → 播放按路径取直链（官方接口）

Cookie 只在服务端保存，接口只回显尾号，不把整串吐回浏览器。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict

import requests

from . import logger

QR_HOST = "https://qrcodeapi.115.com"
PASSPORT_HOST = "https://passportapi.115.com"
TIMEOUT = 15
# 扫码状态接口是长轮询：没有变化时会挂 30 秒才返回，超时必须大于它
STATUS_TIMEOUT = 35
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 网页 Cookie 的四个必需字段，115 的 webapi 靠它们识别会话
_COOKIE_KEYS = ("UID", "CID", "SEID", "KID")

_lock = threading.RLock()
# uid -> {time, sign, qrcode, account}
_pending: Dict[str, Dict[str, Any]] = {}


class WebLoginError(Exception):
    pass


def _headers() -> Dict[str, str]:
    return {"User-Agent": UA, "Referer": "https://115.com/", "Origin": "https://115.com"}


def start() -> Dict[str, Any]:
    """申请网页扫码 token。返回 uid 与二维码内容（前端渲染成图）。"""
    try:
        resp = requests.get(f"{QR_HOST}/api/1.0/web/1.0/token/", headers=_headers(), timeout=TIMEOUT)
        payload = resp.json()
    except requests.RequestException as err:
        raise WebLoginError(f"连不上 115：{err}") from err
    except ValueError as err:
        raise WebLoginError("115 返回的不是 JSON（线路被拦截或需要重试）") from err
    data = payload.get("data") or {}
    uid = str(data.get("uid") or "")
    if not uid or not data.get("sign"):
        # 115 偶尔返回空 data，重试一次
        time.sleep(1.2)
        try:
            payload = requests.get(
                f"{QR_HOST}/api/1.0/web/1.0/token/", headers=_headers(), timeout=TIMEOUT
            ).json()
            data = payload.get("data") or {}
            uid = str(data.get("uid") or "")
        except Exception:  # noqa: BLE001
            pass
    if not uid or not data.get("sign"):
        raise WebLoginError(f"115 没有返回二维码：{payload.get('message') or payload.get('code')}")
    with _lock:
        if len(_pending) > 50:
            _pending.clear()
        _pending[uid] = {
            "time": data.get("time"),
            "sign": data.get("sign"),
            "qrcode": str(data.get("qrcode") or ""),
            "account": "",
        }
    return {"uid": uid, "qrcode": str(data.get("qrcode") or ""), "expires_in": 300}


def qr_text(uid: str) -> str:
    with _lock:
        return str((_pending.get(uid) or {}).get("qrcode") or "")


def poll(uid: str) -> Dict[str, Any]:
    """查询扫码状态。

    115 未扫码时不返回 status 字段（data 为空），所以缺省按 0 处理：
      0 = 等待扫码，1 = 已扫码待确认，2 = 已确认（此时 data.uid 是账号 uid）
    """
    with _lock:
        sess = _pending.get(uid)
    if not sess:
        raise WebLoginError("二维码已失效，重新获取")
    try:
        resp = requests.get(
            f"{QR_HOST}/get/status/",
            params={"uid": uid, "time": sess.get("time"), "sign": sess.get("sign")},
            headers=_headers(),
            timeout=STATUS_TIMEOUT,
        )
        payload = resp.json()
    except requests.Timeout:
        # 长轮询到点没变化是正常现象，静默等下一轮
        return {"status": 0, "message": ""}
    except requests.RequestException as err:
        return {"status": 0, "message": f"查询扫码状态失败，重试中：{err}"}
    except ValueError:
        return {"status": 0, "message": "115 返回异常，重试中"}

    data = payload.get("data") or {}
    try:
        status = int(data.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    account = str(data.get("uid") or "")
    if status == 2 and account:
        with _lock:
            item = _pending.get(uid)
            if item is not None:
                item["account"] = account
    return {"status": status, "message": str(data.get("msg") or payload.get("message") or "")}


def _parse_cookies(resp: requests.Response) -> Dict[str, str]:
    found: Dict[str, str] = {}
    try:
        for key, value in resp.cookies.get_dict().items():
            if key in _COOKIE_KEYS and value:
                found[key] = value
    except Exception:  # noqa: BLE001
        pass
    if len(found) < len(_COOKIE_KEYS):
        # 有些线路 Set-Cookie 合并在一行，直接正则兜底
        raw = " ".join(resp.raw.headers.getlist("Set-Cookie")) if hasattr(resp.raw, "headers") else ""
        raw = raw or resp.headers.get("Set-Cookie", "")
        for key in _COOKIE_KEYS:
            if key in found:
                continue
            m = re.search(rf"(?:^|,\s*){key}=([^;,\s]+)", raw)
            if m:
                found[key] = m.group(1)
    return found


def finish(uid: str) -> Dict[str, Any]:
    """扫码确认后，用 account 换网页 Cookie。"""
    with _lock:
        sess = _pending.get(uid)
        if not sess:
            raise WebLoginError("二维码已失效，重新获取")
        account = str(sess.get("account") or "")
    if not account:
        raise WebLoginError("还没确认登录，请先在手机上点「确认」")

    try:
        resp = requests.post(
            f"{PASSPORT_HOST}/app/1.0/web/1.0/check/sso",
            data={"account": account},
            headers=_headers(),
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as err:
        raise WebLoginError(f"换取 Cookie 失败：{err}") from err

    cookies = _parse_cookies(resp)
    missing = [k for k in _COOKIE_KEYS if not cookies.get(k)]
    if missing:
        detail = ""
        try:
            body = resp.json()
            detail = f"{body.get('code')} {body.get('message') or body.get('error')}".strip()
        except ValueError:
            detail = resp.text[:160].replace("\n", " ")
        raise WebLoginError(f"115 没给全 Cookie（缺 {','.join(missing)}）：{detail or resp.status_code}")

    cookie = "; ".join(f"{k}={cookies[k]}" for k in _COOKIE_KEYS)
    with _lock:
        _pending.pop(uid, None)
    logger.info("[115] 网页扫码登录成功，已写入 Cookie（UID/CID/SEID/KID）")
    return {"cookie": cookie, "hint": "…" + cookie[-6:]}


def cancel(uid: str) -> None:
    with _lock:
        _pending.pop(uid, None)


def has_session(uid: str) -> bool:
    with _lock:
        return uid in _pending
