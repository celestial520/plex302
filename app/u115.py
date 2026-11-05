"""115 开放平台客户端：扫码授权、自动续期、按路径取直链。

流程（PKCE 设备码，官方开放平台）：
  1. POST passportapi.115.com/open/authDeviceCode   拿二维码内容
  2. 手机 115 App 扫码确认
  3. GET  qrcodeapi.115.com/get/status/             轮询到 status=2
  4. POST passportapi.115.com/open/deviceCodeToToken 换 access_token / refresh_token
之后所有业务接口带 Authorization: Bearer <access_token>。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

import qrcode
import requests

from . import config, logger

API_BASE = "https://proapi.115.com"
PASSPORT = "https://passportapi.115.com"
QRCODE_API = "https://qrcodeapi.115.com"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DEFAULT_CLIENT_ID = os.getenv("U115_CLIENT_ID", "100197847")


class U115Error(Exception):
    pass


class U115Client:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "application/json"})
        self._lock = threading.RLock()
        self._token: Dict[str, Any] = self._load_token()
        self._qr_state: Dict[str, Any] = {}
        self._qr_created: float = 0.0
        self._qr_lock = threading.Lock()

    # ------------------------------------------------------------------ token
    def _load_token(self) -> Dict[str, Any]:
        try:
            data = json.loads(config.TOKEN_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_token(self, token: Dict[str, Any]) -> None:
        config.ensure_dirs()
        tmp = config.TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, config.TOKEN_FILE)

    def is_authorized(self) -> bool:
        return bool(self._token.get("refresh_token"))

    def token_status(self) -> Dict[str, Any]:
        return {
            "authorized": self.is_authorized(),
            "user_name": self._token.get("user_name", ""),
            "user_id": self._token.get("user_id", ""),
            "expires_at": int(self._token.get("refresh_time", 0) + self._token.get("expires_in", 0)),
        }

    def logout(self) -> None:
        with self._lock:
            self._token = {}
            self._qr_state = {}
            try:
                config.TOKEN_FILE.unlink()
            except OSError:
                pass

    def access_token(self) -> Optional[str]:
        with self._lock:
            token = self._token or {}
            if not token.get("refresh_token"):
                return None
            expires_in = int(token.get("expires_in") or 7200)
            refresh_time = int(token.get("refresh_time") or 0)
            if not token.get("access_token") or refresh_time + expires_in - 300 < int(time.time()):
                if not self._refresh_locked(token["refresh_token"]):
                    return None
                token = self._token
            return token.get("access_token")

    def _refresh_locked(self, refresh_token: str) -> bool:
        try:
            resp = requests.post(
                f"{PASSPORT}/open/refreshToken",
                data={"refresh_token": refresh_token},
                headers={"User-Agent": DEFAULT_UA},
                timeout=20,
            )
            payload = resp.json()
        except Exception as err:  # noqa: BLE001
            logger.warn(f"[115] 刷新 token 异常：{err}")
            return False
        if payload.get("code") not in (0, None) or not payload.get("data"):
            logger.warn(f"[115] 刷新 token 失败：{payload.get('message') or payload}")
            return False
        data = payload["data"]
        self._token.update(
            {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token") or refresh_token,
                "expires_in": int(data.get("expires_in") or 7200),
                "refresh_time": int(time.time()),
            }
        )
        self._save_token(self._token)
        logger.info("[115] access_token 已自动续期")
        return True

    # ------------------------------------------------------------------ 扫码
    def qr_create(self) -> Dict[str, Any]:
        client_id = str(config.load()["provider"].get("u115_client_id") or DEFAULT_CLIENT_ID)
        verifier = secrets.token_urlsafe(96)[:128]
        challenge = base64.b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("utf-8")
        try:
            resp = requests.post(
                f"{PASSPORT}/open/authDeviceCode",
                data={
                    "client_id": client_id,
                    "code_challenge": challenge,
                    "code_challenge_method": "sha256",
                },
                headers={"User-Agent": DEFAULT_UA},
                timeout=20,
            )
            payload = resp.json()
        except Exception as err:  # noqa: BLE001
            raise U115Error(f"连接 115 授权服务失败：{err}") from err
        if payload.get("code") != 0 or not payload.get("data"):
            raise U115Error(payload.get("message") or "获取二维码失败")
        data = payload["data"]
        self._qr_state = {
            "uid": data.get("uid"),
            "time": data.get("time"),
            "sign": data.get("sign"),
            "code_verifier": verifier,
            "client_id": client_id,
        }
        self._qr_created = time.time()
        content = data.get("qrcode") or ""
        if not content:
            raise U115Error("115 未返回二维码内容")
        img = qrcode.make(content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {
            "qrcode": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
            "uid": data.get("uid"),
            "expires_in": 120,
        }

    def qr_status(self) -> Dict[str, Any]:
        if not self._qr_lock.acquire(blocking=False):
            return {"status": 0, "msg": "等待扫码…"}
        try:
            return self._qr_status_locked()
        finally:
            self._qr_lock.release()

    def _qr_status_locked(self) -> Dict[str, Any]:
        state = self._qr_state or {}
        if not state.get("uid"):
            raise U115Error("二维码已失效，请刷新")
        if time.time() - self._qr_created > 180:
            self._qr_state = {}
            raise U115Error("二维码已过期，请刷新")
        try:
            resp = requests.get(
                f"{QRCODE_API}/get/status/",
                params={
                    "uid": state["uid"],
                    "time": state["time"],
                    "sign": state["sign"],
                    "_": int(time.time() * 1000),
                },
                headers={"User-Agent": DEFAULT_UA},
                timeout=(5, 30),
            )
            payload = resp.json()
        except requests.exceptions.Timeout:
            return {"status": 0, "msg": "等待扫码…"}
        except Exception as err:  # noqa: BLE001
            raise U115Error(f"查询扫码状态失败：{err}") from err
        if payload.get("code") != 0 or not payload.get("data"):
            raise U115Error(payload.get("message") or "查询扫码状态失败")
        data = payload["data"]
        status = int(data.get("status") or 0)
        if status != 2:
            return {"status": status, "msg": data.get("msg") or "等待扫码…"}

        # 已确认，换 token
        try:
            resp = requests.post(
                f"{PASSPORT}/open/deviceCodeToToken",
                data={"uid": state["uid"], "code_verifier": state["code_verifier"]},
                headers={"User-Agent": DEFAULT_UA},
                timeout=20,
            )
            payload = resp.json()
        except Exception as err:  # noqa: BLE001
            raise U115Error(f"换取 token 失败：{err}") from err
        if payload.get("code") != 0 or not payload.get("data"):
            raise U115Error(payload.get("message") or "换取 token 失败")
        token = payload["data"]
        self._token = {
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
            "expires_in": int(token.get("expires_in") or 7200),
            "refresh_time": int(time.time()),
        }
        self._save_token(self._token)
        self._qr_state = {}
        user = self.user_info(silent=True) or {}
        self._token["user_name"] = user.get("user_name", "")
        self._token["user_id"] = user.get("user_id", "")
        self._save_token(self._token)
        logger.info(f"[115] 扫码登录成功：{user.get('user_name', '')}")
        return {"status": 2, "msg": "授权成功", "user": user}

    # ------------------------------------------------------------------ 请求
    def request(self, method: str, endpoint: str, *, params=None, data=None, timeout: int = 30,
                _retry: bool = True, user_agent: str | None = None) -> Dict[str, Any]:
        token = self.access_token()
        if not token:
            raise U115Error("115 未授权，请先在网页里扫码登录")
        headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent or DEFAULT_UA}
        try:
            resp = self.session.request(
                method, f"{API_BASE}{endpoint}", params=params, data=data,
                headers=headers, timeout=timeout,
            )
        except requests.RequestException as err:
            raise U115Error(f"115 接口连接失败：{err}") from err
        if resp.status_code == 429:
            raise U115Error("115 接口限流（429），稍后再试")
        if resp.status_code in (401, 403) and _retry:
            with self._lock:
                self._refresh_locked(self._token.get("refresh_token", ""))
            return self.request(method, endpoint, params=params, data=data, timeout=timeout, _retry=False, user_agent=user_agent)
        try:
            payload = resp.json()
        except ValueError as err:
            raise U115Error(f"115 返回非 JSON（HTTP {resp.status_code}）") from err
        code = payload.get("code")
        if code not in (0, 200, None) and payload.get("state") is not True:
            message = payload.get("message") or payload.get("msg") or f"接口 {endpoint} 返回异常"
            if code in (401401, 401402, 401403) and _retry:
                with self._lock:
                    self._refresh_locked(self._token.get("refresh_token", ""))
                return self.request(method, endpoint, params=params, data=data, timeout=timeout, _retry=False, user_agent=user_agent)
            raise U115Error(str(message))
        return payload

    # ------------------------------------------------------------------ 业务
    def user_info(self, silent: bool = False) -> Dict[str, Any]:
        try:
            payload = self.request("GET", "/open/user/info")
        except U115Error:
            if silent:
                return {}
            raise
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    def path_info(self, cloud_path: str) -> Optional[Dict[str, Any]]:
        """文件或目录信息。115 的 folder/get_info 对文件同样有效。"""
        payload = self.request("POST", "/open/folder/get_info", data={"path": cloud_path})
        data = payload.get("data")
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), None)
        if not isinstance(data, dict):
            return None
        pick = data.get("pick_code") or data.get("pickcode") or ""
        file_id = data.get("file_id") or data.get("fileid") or data.get("cid") or ""
        name = data.get("file_name") or data.get("fn") or ""
        if not pick and not file_id:
            return None
        return {"pick_code": str(pick), "file_id": str(file_id), "name": str(name), "raw": data}

    def download_url(self, pick_code: str, user_agent: str) -> Tuple[str, str]:
        ua = user_agent or DEFAULT_UA
        try:
            payload = self.request(
                "POST", "/open/ufile/downurl",
                data={"pick_code": pick_code},
                timeout=30,
                user_agent=ua,
            )
        except U115Error:
            # 部分账号/文件只支持 video/play
            payload = self.request(
                "GET", "/open/video/play",
                params={"pick_code": pick_code},
                timeout=30,
                user_agent=ua,
            )
        data = payload.get("data") or {}
        url, name = self._extract_url(data)
        if not url:
            raise U115Error("115 未返回下载地址（可能触发风控或文件不可用）")
        return url, name or ""

    @staticmethod
    def _extract_url(data: Any) -> Tuple[str, str]:
        if isinstance(data, str) and data.startswith("http"):
            return data, ""
        if isinstance(data, dict):
            url = data.get("url")
            if isinstance(url, str) and url.startswith("http"):
                return url, str(data.get("file_name") or data.get("name") or "")
            if isinstance(url, dict) and isinstance(url.get("url"), str):
                return url["url"], str(data.get("file_name") or "")
            play = data.get("video_url")
            if isinstance(play, list) and play:
                best = sorted(play, key=lambda x: int(x.get("width") or 0), reverse=True)[0]
                if isinstance(best.get("url"), str):
                    return best["url"], str(data.get("file_name") or "")
            for value in data.values():
                found, name = U115Client._extract_url(value)
                if found:
                    return found, name
        if isinstance(data, list):
            for item in data:
                found, name = U115Client._extract_url(item)
                if found:
                    return found, name
        return "", ""

    def file_url_by_path(self, cloud_path: str, user_agent: str) -> Tuple[str, str]:
        info = self.path_info(cloud_path)
        if not info:
            raise U115Error(f"115 上找不到路径：{cloud_path}")
        if not info["pick_code"]:
            raise U115Error(f"115 未返回 pickcode：{cloud_path}")
        url, name = self.download_url(info["pick_code"], user_agent)
        return url, name or info["name"]


client = U115Client()
