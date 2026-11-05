"""出站 HTTP 小工具：给 TMDB / AI 用，支持单独走代理。

115 的接口必须直连（代理反而容易被风控），所以这里只服务“墙外”的元数据与 AI。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

_session: Optional[requests.Session] = None


def _session_get() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def proxies(proxy: str) -> Optional[Dict[str, str]]:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def request(
    method: str,
    url: str,
    *,
    proxy: str = "",
    timeout: float = 20.0,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    return _session_get().request(
        method.upper(),
        url,
        headers=headers or {},
        params=params,
        json=json_body,
        proxies=proxies(proxy),
        timeout=timeout,
    )


def get_json(url: str, **kwargs: Any) -> Any:
    resp = request("GET", url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def post_json(url: str, **kwargs: Any) -> Any:
    resp = request("POST", url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()
