"""配置读写：/config/config.json

只做很薄的一层校验，保证坏配置不会把整个服务带崩。
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.json"
RUNTIME_FILE = CONFIG_DIR / "runtime.json"
LOG_DIR = CONFIG_DIR / "logs"
TOKEN_FILE = CONFIG_DIR / "115_token.json"

DEFAULT_MOUNT = {
    "name": "CD2",
    "plex_prefix": "/mnt/cd2",
    "cloud_prefix": "/",
    "cd2_prefix": "",
}

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "plex": {
        "url": "http://127.0.0.1:32400",
        "token": "",
        "verify_tls": False,
    },
    "server": {
        "name": "Plex302",
        "public_url": "",
        "lan_cidrs": [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ],
        "trust_forwarded_for": True,
        "redirect_lan": False,
    },
    "mounts": [dict(DEFAULT_MOUNT)],
    "cd2": {
        "address": "127.0.0.1:19798",
        "token": "",
    },
    "provider": {
        "type": "115",  # 115 | cd2 | alist | custom | none
        "ua_mode": "client",  # client | fixed
        "fixed_ua": "Plex302/1.0",
        "force_https": True,
        "alist_base": "",
        "alist_token": "",
        "alist_public_base": "",
        "custom_url": "",
        "custom_token": "",
    },
    "scan": {
        "enabled": True,
        "watch_enabled": False,
        "watch_sections": [],
        "interval": 1800,
        "settle_seconds": 90,
        "max_depth": 3,
        "min_size_mb": 20,
        "max_entries": 200000,
        "first_run_scan": False,
        # 全量同步：把 Plex 所有媒体库整库扫一遍，作为兜底
        "full_enabled": True,
        "full_time": "04:30",
        "full_on_start": False,
        "video_exts": [
            ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m2ts",
            ".flv", ".webm", ".iso", ".rmvb", ".mpg", ".mpeg", ".m4v",
        ],
    },
    "security": {
        "password_hash": "",
        "secret": "",
        "webhook_token": "",
        # 给 CMS 的 Emby 兼容入口用（CMS 里 EMBY_API_KEY 填这个）
        "emby_token": "",
    },
    # 115 网页版 Cookie：开放平台没有“最近操作”接口，靠它补
    "u115": {
        "cookie": "",
        "recent_enabled": True,
        "recent_interval": 120,
        "recent_limit": 50,
        "recent_first_scan": False,
    },
    # 115 生活事件（App 里的「最近操作」）：增量入库主通道
    "events": {
        "enabled": True,
        "interval": 60,          # 拉取间隔（秒）
        "limit": 100,            # 单页条数
        "max_pages": 5,          # 一轮最多翻几页
        "first_scan": False,     # 首次运行是否补扫历史
        "settle_seconds": 90,    # 目录静置多久再扫（CMS 整理期间不断延长）
        "max_wait_seconds": 600, # 单目录最长等待，避免一直有事件时永远不扫
        "verify_enabled": True,  # 扫完再复核一次，接住“扫的时候还没写完”
        "verify_delay": 180,     # 复核延迟（秒）
        "recent_fallback": True, # 同时检查“最近文件”，兜住事件接口偶发空返回
    },
    "notify": {
        "wecom_enabled": False,
        "wecom_corpid": "",
        "wecom_secret": "",
        "wecom_agentid": "",
        "wecom_touser": "@all",
        "wecom_proxy": "",
        "on_scan": True,
        "on_full": True,
        "on_error": True,
    },
    # 链接入料：把 115 分享 / 磁力 / ed2k 存到待整理目录
    "ingest": {
        "enabled": False,
        "target_dir": "",
        "wecom_token": "",
        "wecom_aes_key": "",
        "allow_users": "",
    },
    "advanced": {
        "force_direct_play": True,
        "part_cache_ttl": 86400,
        "url_cache_ttl": 21600,
        "route_cache": True,   # nginx 共享内存直链缓存（对齐 plex-demo 的 routeDict）
        "log_redirects": True,
    },
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_mtime: float = 0.0


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, num))


def _norm_prefix(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def normalize(cfg: dict) -> dict:
    """把外部输入收敛成安全、可预期的结构。"""
    cfg = _deep_merge(DEFAULTS, cfg if isinstance(cfg, dict) else {})

    plex = cfg["plex"]
    plex["url"] = str(plex.get("url") or DEFAULTS["plex"]["url"]).strip().rstrip("/")
    plex["token"] = str(plex.get("token") or "").strip()
    plex["verify_tls"] = _as_bool(plex.get("verify_tls"), False)

    server = cfg["server"]
    server["name"] = str(server.get("name") or "Plex302")[:60]
    server["public_url"] = str(server.get("public_url") or "").strip().rstrip("/")
    server["trust_forwarded_for"] = _as_bool(server.get("trust_forwarded_for"), True)
    server["redirect_lan"] = _as_bool(server.get("redirect_lan"), False)
    cidrs = server.get("lan_cidrs")
    if isinstance(cidrs, str):
        cidrs = [x.strip() for x in cidrs.replace("\n", ",").split(",") if x.strip()]
    if not isinstance(cidrs, list):
        cidrs = DEFAULTS["server"]["lan_cidrs"]
    server["lan_cidrs"] = [str(x).strip() for x in cidrs if str(x).strip()]

    mounts = []
    raw_mounts = cfg.get("mounts")
    if isinstance(raw_mounts, str):
        raw_mounts = []
    for item in raw_mounts or []:
        if not isinstance(item, dict):
            continue
        plex_prefix = _norm_prefix(item.get("plex_prefix"))
        if not plex_prefix:
            continue
        cloud_prefix = _norm_prefix(item.get("cloud_prefix")) or "/"
        mounts.append(
            {
                "name": str(item.get("name") or "CD2")[:40],
                "plex_prefix": plex_prefix,
                "cloud_prefix": cloud_prefix,
                "cd2_prefix": _norm_prefix(item.get("cd2_prefix")),
            }
        )
    cfg["mounts"] = mounts

    cd2 = cfg["cd2"]
    cd2["address"] = str(cd2.get("address") or "127.0.0.1:19798").strip().replace("http://", "").replace("https://", "")
    cd2["token"] = str(cd2.get("token") or "").strip()

    provider = cfg["provider"]
    ptype = str(provider.get("type") or "115").lower()
    provider["type"] = ptype if ptype in ("115", "cd2", "alist", "custom", "none") else "115"
    provider["ua_mode"] = "fixed" if str(provider.get("ua_mode")) == "fixed" else "client"
    provider["fixed_ua"] = str(provider.get("fixed_ua") or "Plex302/1.0")[:200]
    provider["force_https"] = _as_bool(provider.get("force_https"), True)
    provider["alist_base"] = str(provider.get("alist_base") or "").strip().rstrip("/")
    provider["alist_token"] = str(provider.get("alist_token") or "").strip()
    provider["alist_public_base"] = str(provider.get("alist_public_base") or "").strip().rstrip("/")
    provider["custom_url"] = str(provider.get("custom_url") or "").strip()
    provider["custom_token"] = str(provider.get("custom_token") or "").strip()

    scan = cfg["scan"]
    scan["enabled"] = _as_bool(scan.get("enabled"), True)
    scan["watch_enabled"] = _as_bool(scan.get("watch_enabled"), False)
    scan["first_run_scan"] = _as_bool(scan.get("first_run_scan"), False)
    scan["interval"] = _as_int(scan.get("interval"), 1800, 60, 86400)
    scan["settle_seconds"] = _as_int(scan.get("settle_seconds"), 90, 0, 86400)
    scan["max_depth"] = _as_int(scan.get("max_depth"), 3, 1, 12)
    scan["min_size_mb"] = _as_int(scan.get("min_size_mb"), 20, 0, 1024 * 1024)
    scan["max_entries"] = _as_int(scan.get("max_entries"), 200000, 1000, 5000000)
    scan["full_enabled"] = _as_bool(scan.get("full_enabled"), True)
    scan["full_on_start"] = _as_bool(scan.get("full_on_start"), False)
    full_time = str(scan.get("full_time") or "04:30").strip()
    try:
        hh, mm = full_time.split(":")[:2]
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        scan["full_time"] = f"{hh:02d}:{mm:02d}"
    except (ValueError, TypeError):
        scan["full_time"] = "04:30"
    ws = scan.get("watch_sections")
    if isinstance(ws, str):
        ws = [x.strip() for x in ws.split(",") if x.strip()]
    scan["watch_sections"] = [str(x) for x in (ws or [])]
    exts = scan.get("video_exts")
    if isinstance(exts, str):
        exts = [x.strip() for x in exts.replace("\n", ",").split(",") if x.strip()]
    if not isinstance(exts, list) or not exts:
        exts = DEFAULTS["scan"]["video_exts"]
    norm_exts = []
    for ext in exts:
        ext = str(ext).strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        norm_exts.append(ext)
    scan["video_exts"] = sorted(set(norm_exts))

    security = cfg["security"]
    if not security.get("secret"):
        security["secret"] = secrets.token_hex(32)
    if not security.get("webhook_token"):
        security["webhook_token"] = "p302_" + secrets.token_urlsafe(24)
    if not security.get("emby_token"):
        security["emby_token"] = "emb_" + secrets.token_urlsafe(24)
    security["password_hash"] = str(security.get("password_hash") or "")

    u115 = cfg["u115"]
    cookie = str(u115.get("cookie") or "").replace("\r", " ").replace("\n", "; ").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    u115["cookie"] = "; ".join(p.strip() for p in cookie.split(";") if p.strip() and "=" in p)
    u115["recent_enabled"] = _as_bool(u115.get("recent_enabled"), True)
    u115["recent_first_scan"] = _as_bool(u115.get("recent_first_scan"), False)
    u115["recent_interval"] = _as_int(u115.get("recent_interval"), 120, 30, 3600)
    u115["recent_limit"] = _as_int(u115.get("recent_limit"), 50, 5, 200)

    events = cfg["events"]
    events["enabled"] = _as_bool(events.get("enabled"), True)
    events["first_scan"] = _as_bool(events.get("first_scan"), False)
    events["interval"] = _as_int(events.get("interval"), 60, 15, 3600)
    events["limit"] = _as_int(events.get("limit"), 100, 20, 500)
    events["max_pages"] = _as_int(events.get("max_pages"), 5, 1, 20)
    events["settle_seconds"] = _as_int(events.get("settle_seconds"), 90, 0, 3600)
    events["max_wait_seconds"] = _as_int(events.get("max_wait_seconds"), 600, 60, 7200)
    events["verify_enabled"] = _as_bool(events.get("verify_enabled"), True)
    events["verify_delay"] = _as_int(events.get("verify_delay"), 180, 30, 3600)
    events["recent_fallback"] = _as_bool(events.get("recent_fallback"), True)

    notify = cfg["notify"]
    notify["wecom_enabled"] = _as_bool(notify.get("wecom_enabled"), False)
    notify["wecom_corpid"] = str(notify.get("wecom_corpid") or "").strip()
    notify["wecom_secret"] = str(notify.get("wecom_secret") or "").strip()
    notify["wecom_agentid"] = str(notify.get("wecom_agentid") or "").strip()
    notify["wecom_touser"] = str(notify.get("wecom_touser") or "@all").strip() or "@all"
    notify["wecom_proxy"] = str(notify.get("wecom_proxy") or "").strip().rstrip("/")
    notify["on_scan"] = _as_bool(notify.get("on_scan"), True)
    notify["on_full"] = _as_bool(notify.get("on_full"), True)
    notify["on_error"] = _as_bool(notify.get("on_error"), True)

    ingest = cfg["ingest"]
    ingest["enabled"] = _as_bool(ingest.get("enabled"), False)
    ingest["target_dir"] = _norm_prefix(ingest.get("target_dir"))
    ingest["wecom_token"] = str(ingest.get("wecom_token") or "").strip()
    ingest["wecom_aes_key"] = str(ingest.get("wecom_aes_key") or "").strip()
    users = ingest.get("allow_users")
    if isinstance(users, list):
        users = ",".join(str(x) for x in users)
    ingest["allow_users"] = str(users or "").strip()

    adv = cfg["advanced"]
    adv["force_direct_play"] = _as_bool(adv.get("force_direct_play"), True)
    adv["part_cache_ttl"] = _as_int(adv.get("part_cache_ttl"), 86400, 60, 30 * 86400)
    adv["url_cache_ttl"] = _as_int(adv.get("url_cache_ttl"), 21600, 300, 86400)
    adv["route_cache"] = _as_bool(adv.get("route_cache"), True)
    adv["log_redirects"] = _as_bool(adv.get("log_redirects"), True)

    # 未知字段直接丢弃，避免前端误传导致配置文件越来越大
    known = set(DEFAULTS.keys())
    return {k: v for k, v in cfg.items() if k in known}


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load(force: bool = False) -> dict:
    global _cache, _cache_mtime
    with _lock:
        if not CONFIG_FILE.exists():
            cfg = normalize({})
            save(cfg)
            return copy.deepcopy(cfg)
        try:
            mtime = CONFIG_FILE.stat().st_mtime
        except OSError:
            mtime = 0
        if _cache is not None and not force and mtime == _cache_mtime:
            return copy.deepcopy(_cache)
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        cfg = normalize(raw)
        _cache = cfg
        _cache_mtime = mtime
        return copy.deepcopy(cfg)


def save(cfg: dict) -> dict:
    global _cache, _cache_mtime
    with _lock:
        cfg = normalize(cfg)
        ensure_dirs()
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        _cache = cfg
        try:
            _cache_mtime = CONFIG_FILE.stat().st_mtime
        except OSError:
            _cache_mtime = 0
        write_runtime(cfg)
        return copy.deepcopy(cfg)


def write_runtime(cfg: dict | None = None) -> None:
    """给 njs 读的运行时配置。"""
    cfg = cfg or load()
    payload = {
        "plex": cfg["plex"]["url"],
        "resolver": "http://127.0.0.1:{}".format(os.getenv("WEBUI_PORT", "999")),
        "token": cfg["security"]["secret"][:48],
        "trust_proxy": cfg["server"]["trust_forwarded_for"],
        "force_direct_play": cfg["advanced"]["force_direct_play"],
        "route_cache": cfg["advanced"]["route_cache"],
    }
    ensure_dirs()
    tmp = RUNTIME_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, RUNTIME_FILE)


def masked(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    out["plex"]["token_set"] = bool(out["plex"].get("token"))
    out["plex"]["token"] = ""
    out["provider"]["alist_token_set"] = bool(out["provider"].get("alist_token"))
    out["provider"]["alist_token"] = ""
    out["provider"]["custom_token_set"] = bool(out["provider"].get("custom_token"))
    out["provider"]["custom_token"] = ""
    out["cd2"]["token_set"] = bool(out["cd2"].get("token"))
    out["cd2"]["token"] = ""
    cookie = out["u115"].get("cookie") or ""
    out["u115"]["cookie_set"] = bool(cookie)
    out["u115"]["cookie_hint"] = ("…" + cookie[-6:]) if cookie else ""
    out["u115"]["cookie"] = ""
    out["security"]["password_set"] = bool(out["security"].get("password_hash"))
    out["security"].pop("password_hash", None)
    out["notify"]["wecom_secret_set"] = bool(out["notify"].get("wecom_secret"))
    out["notify"]["wecom_secret"] = ""
    out["ingest"]["wecom_aes_key_set"] = bool(out["ingest"].get("wecom_aes_key"))
    out["ingest"]["wecom_aes_key"] = ""
    out["security"].pop("secret", None)
    return out


def merge_secrets(new_cfg: dict, old_cfg: dict) -> dict:
    """前端留空代表“不修改”，避免每次保存都要重新填 token。"""
    new_cfg = copy.deepcopy(new_cfg)
    if not (new_cfg.get("plex") or {}).get("token"):
        new_cfg.setdefault("plex", {})["token"] = old_cfg["plex"].get("token", "")
    cd2 = new_cfg.setdefault("cd2", {})
    if not cd2.get("token"):
        cd2["token"] = old_cfg.get("cd2", {}).get("token", "")
    provider = new_cfg.setdefault("provider", {})
    old_provider = old_cfg.get("provider", {})
    if not provider.get("alist_token"):
        provider["alist_token"] = old_provider.get("alist_token", "")
    if not provider.get("custom_token"):
        provider["custom_token"] = old_provider.get("custom_token", "")
    u115 = new_cfg.setdefault("u115", {})
    old_u115 = old_cfg.get("u115", {})
    u115.pop("cookie_set", None)
    u115.pop("cookie_hint", None)
    if not u115.get("cookie"):
        u115["cookie"] = old_u115.get("cookie", "")
    security = new_cfg.setdefault("security", {})
    security.pop("password_set", None)
    if not security.get("password_hash"):
        security["password_hash"] = old_cfg["security"].get("password_hash", "")
    security.setdefault("secret", old_cfg["security"].get("secret", ""))
    security.setdefault("webhook_token", old_cfg["security"].get("webhook_token", ""))
    if not security.get("emby_token"):
        security["emby_token"] = old_cfg["security"].get("emby_token", "")
    notify = new_cfg.setdefault("notify", {})
    notify.pop("wecom_secret_set", None)
    if not notify.get("wecom_secret"):
        notify["wecom_secret"] = old_cfg.get("notify", {}).get("wecom_secret", "")
    ingest = new_cfg.setdefault("ingest", {})
    ingest.pop("wecom_aes_key_set", None)
    if not ingest.get("wecom_aes_key"):
        ingest["wecom_aes_key"] = old_cfg.get("ingest", {}).get("wecom_aes_key", "")
    return new_cfg
