"""直链 Provider：115 / CD2 / AList / 自定义 HTTP。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import quote

import requests

from . import cd2, config, logger, u115


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
    if not u115.client.is_authorized():
        raise ProviderError("115 未授权，请到 WebUI 扫码登录")
    url, _name = u115.client.file_url_by_path(cloud_path, _ua(cfg, ctx))
    if cfg["provider"].get("force_https") and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return Link(url=url, provider="115", expires_in=7200)


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
        if ptype == "115":
            return _resolve_115(cfg, cloud_path, ctx)
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
