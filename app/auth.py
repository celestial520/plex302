"""WebUI 极简认证：PBKDF2 密码 + HMAC 签名 Cookie。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from . import config

COOKIE_NAME = "p302_session"
SESSION_TTL = 7 * 86400
ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, digest = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest())


def make_session(cfg: dict, username: str = "admin") -> str:
    payload = _b64(json.dumps({"u": username, "exp": int(time.time()) + SESSION_TTL}).encode("utf-8"))
    return f"{payload}.{_sign(cfg['security']['secret'], payload)}"


def check_session(cfg: dict, cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload, signature = cookie.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(cfg["security"]["secret"], payload)):
        return False
    try:
        data: dict[str, Any] = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    return int(data.get("exp", 0)) > time.time()


def default_password_active(cfg: dict) -> bool:
    stored = cfg["security"].get("password_hash") or ""
    if not stored:
        return True
    return verify_password("admin", stored)


def ensure_default_password(cfg: dict) -> tuple[dict, bool]:
    """首次启动写入 admin/admin，并在日志里提醒修改。"""
    if cfg["security"].get("password_hash"):
        return cfg, False
    cfg = dict(cfg)
    cfg["security"] = dict(cfg["security"])
    cfg["security"]["password_hash"] = hash_password("admin")
    config.save(cfg)
    return cfg, True
