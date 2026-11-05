"""302 决策核心：请求 -> Plex 文件路径 -> 云盘路径 -> 直链。"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

from . import config, logger, plex, providers, store

PART_RE = re.compile(r"^/library/parts/(\d+)/", re.I)
START_RE = re.compile(r"^/video/:/transcode/universal/start", re.I)


class TTLCache:
    def __init__(self, maxsize: int = 50000):
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self.maxsize = maxsize

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if expires <= now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if len(self._data) > self.maxsize:
                # 简单清理：删掉最早过期的 10%
                items = sorted(self._data.items(), key=lambda kv: kv[1][0])
                for k, _ in items[: max(1, len(items) // 10)]:
                    self._data.pop(k, None)
            self._data[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._data)


class Resolver:
    def __init__(self) -> None:
        self.part_cache = TTLCache(60000)
        self.url_cache = TTLCache(60000)
        self._lan_nets: list[Any] = []
        self._lan_raw: list[str] = []
        self.reload()

    def reload(self) -> None:
        cfg = config.load(force=True)
        nets = []
        for item in cfg["server"].get("lan_cidrs", []):
            try:
                nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
            except ValueError:
                logger.warn(f"[配置] 忽略非法局域网网段：{item}")
        self._lan_nets = nets
        self._lan_raw = list(cfg["server"].get("lan_cidrs", []))

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _ip(value: str) -> ipaddress._BaseAddress | None:
        try:
            ip = ipaddress.ip_address(str(value or "").strip())
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                return ip.ipv4_mapped
            return ip
        except ValueError:
            return None

    def is_lan(self, ip_text: str) -> bool:
        ip = self._ip(ip_text)
        if ip is None:
            return False
        for net in self._lan_nets:
            try:
                if ip in net:
                    return True
            except TypeError:
                continue
        return False

    @staticmethod
    def map_path(cfg: Dict[str, Any], plex_path: str) -> Optional[str]:
        path = "/" + unquote(str(plex_path or "")).replace("\\", "/").strip("/")
        for mount in cfg.get("mounts", []):
            prefix = mount["plex_prefix"]
            if path == prefix or path.startswith(prefix + "/"):
                rel = path[len(prefix):].lstrip("/")
                cloud_prefix = mount.get("cloud_prefix") or "/"
                cloud = cloud_prefix.rstrip("/") + "/" + rel if rel else (cloud_prefix or "/")
                cloud = "/" + cloud.strip("/")
                return cloud or "/"
        return None

    @staticmethod
    def map_cd2_path(cfg: Dict[str, Any], plex_path: str) -> Optional[str]:
        path = "/" + unquote(str(plex_path or "")).replace("\\", "/").strip("/")
        for mount in cfg.get("mounts", []):
            prefix = mount["plex_prefix"]
            if not mount.get("cd2_prefix"):
                continue
            if path == prefix or path.startswith(prefix + "/"):
                rel = path[len(prefix):].lstrip("/")
                base = mount["cd2_prefix"].rstrip("/")
                return base + ("/" + rel if rel else "")
        return None

    @staticmethod
    def map_plex_path(cfg: Dict[str, Any], cloud_path: str) -> Optional[str]:
        """云盘路径 -> Plex 看到的路径（map_path 的反向，取最长前缀）。"""
        path = "/" + unquote(str(cloud_path or "")).replace("\\", "/").strip("/")
        best_len = -1
        best: Optional[str] = None
        for mount in cfg.get("mounts", []):
            base = (mount.get("cloud_prefix") or "/").rstrip("/")
            if base and base != "/" and not (path == base or path.startswith(base + "/")):
                continue
            rel = path[len(base):].lstrip("/") if base and base != "/" else path.lstrip("/")
            plex_prefix = mount["plex_prefix"].rstrip("/")
            candidate = plex_prefix + ("/" + rel if rel else "")
            score = len(base)
            if score > best_len:
                best_len = score
                best = candidate
        return best

    @staticmethod
    def _arg(args: Dict[str, Any], name: str) -> str:
        value = args.get(name)
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "")

    @staticmethod
    def _is_video(path: str, cfg: Dict[str, Any]) -> bool:
        lower = path.lower()
        return any(lower.endswith(ext) for ext in cfg["scan"]["video_exts"])

    def _cached_part(self, part_id: str, token: str, uri: str = "") -> str:
        cached = self.part_cache.get(part_id)
        if cached:
            return str(cached)
        try:
            path = plex.part_file(part_id, token=token or None)
        except plex.PlexError:
            # 少数 Plex 版本不返回 /library/parts/{id} 的 file，用文件名兜底搜索
            name = ""
            m = re.match(r"^/library/parts/\d+/\d+/(.+)$", uri)
            if m:
                name = unquote(m.group(1)).rsplit("/", 1)[-1]
            path = plex.search_file(name, token=token or None) if name else None
            if not path:
                raise
        ttl = config.load()["advanced"]["part_cache_ttl"]
        self.part_cache.set(part_id, path, ttl)
        return path

    def _resolve_start_path(self, args: Dict[str, Any], token: str) -> str:
        raw_path = self._arg(args, "path")
        if raw_path and raw_path.startswith("/") and not raw_path.startswith("/library/"):
            return raw_path
        rating_key = ""
        m = re.search(r"/library/metadata/(\d+)", raw_path or "")
        if m:
            rating_key = m.group(1)
        if not rating_key:
            rating_key = self._arg(args, "ratingKey") or self._arg(args, "ratingKey")
        if not rating_key:
            raise plex.PlexError("转码请求里没有 ratingKey")
        media_index = int(self._arg(args, "mediaIndex") or 0)
        part_index = int(self._arg(args, "partIndex") or 0)
        return plex.item_file(rating_key, media_index, part_index, token=token or None)

    def explain(self, plex_path: str, ip: str = "", ua: str = "") -> Dict[str, Any]:
        cfg = config.load()
        cloud = self.map_path(cfg, plex_path)
        out: Dict[str, Any] = {"plex_path": plex_path, "cloud_path": cloud, "provider": cfg["provider"]["type"]}
        if not cloud:
            out.update({"ok": False, "error": "路径不在任何挂载映射里"})
            return out
        try:
            link = providers.resolve(cfg, cloud, providers.ResolveContext(ip=ip, ua=ua))
            out.update({"ok": True, "url": link.url, "link_provider": link.provider})
        except providers.ProviderError as err:
            out.update({"ok": False, "error": str(err)})
        return out

    # ---------------------------------------------------------------- 主流程
    async def resolve(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cfg = config.load()
        uri = str(payload.get("uri") or "")
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        ip = str(payload.get("ip") or "")
        ua = str(payload.get("ua") or "")
        token = str(payload.get("token") or "")

        if not cfg["mounts"]:
            return self._proxy("没有配置 CD2 挂载路径", count=False)

        part_match = PART_RE.match(uri)
        is_start = bool(START_RE.match(uri))
        if not part_match and not is_start:
            return self._proxy("不是播放请求", count=False)

        if self.is_lan(ip) and not cfg["server"].get("redirect_lan"):
            return self._proxy("局域网客户端，交给 Plex 直接播放")

        try:
            if part_match:
                plex_path = self._cached_part(part_match.group(1), token, uri)
            else:
                if self._arg(args, "directPlay") == "0":
                    return self._proxy("客户端明确要求转码")
                plex_path = self._resolve_start_path(args, token)
        except plex.PlexError as err:
            store.bump_stat("error")
            logger.warn(f"[302] 获取 Plex 路径失败：{err}")
            return self._proxy(f"Plex 路径解析失败：{err}")

        if not plex_path:
            return self._proxy("Plex 没有返回文件路径")
        if not self._is_video(plex_path, cfg):
            return self._proxy("非视频文件，交给 Plex 处理")

        cloud_path = self.map_path(cfg, plex_path)
        if not cloud_path:
            return self._proxy("文件不在已配置的 CD2 挂载映射内")

        ua_for_cache = providers._ua(cfg, providers.ResolveContext(ip=ip, ua=ua))  # noqa: SLF001
        cache_key = f"{cfg['provider']['type']}|{cloud_path}|{hashlib.sha1(ua_for_cache.encode('utf-8', 'ignore')).hexdigest()}"
        cached = self.url_cache.get(cache_key)
        if cached:
            store.bump_stat("redirect")
            return {"action": "redirect", "url": cached, "reason": "cache", "cloud_path": cloud_path}

        try:
            link = providers.resolve(cfg, cloud_path, providers.ResolveContext(ip=ip, ua=ua, plex_token=token))
        except providers.ProviderError as err:
            store.bump_stat("proxied")
            logger.warn(f"[302] 直链获取失败，回源 Plex：{cloud_path} | {err}")
            return self._proxy(f"直链失败：{err}")

        ttl = int(cfg["advanced"]["url_cache_ttl"])
        if link.expires_in:
            ttl = max(30, min(ttl, link.expires_in - 60))
        self.url_cache.set(cache_key, link.url, ttl)
        store.bump_stat("redirect")
        if cfg["advanced"].get("log_redirects"):
            safe = self._safe_url(link.url)
            logger.info(f"[302] {ip or '-'} | {link.provider} | {cloud_path} -> {safe} | {ua[:60]}")
        return {"action": "redirect", "url": link.url, "reason": "ok", "cloud_path": cloud_path}

    @staticmethod
    def _safe_url(url: str) -> str:
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            path = parts.path if len(parts.path) < 48 else parts.path[:48] + "…"
            return f"{parts.scheme}://{parts.netloc}{path}"
        except ValueError:
            return url[:80]

    @staticmethod
    def _proxy(reason: str, count: bool = True) -> Dict[str, Any]:
        if count:
            store.bump_stat("proxied")
        return {"action": "proxy", "reason": reason}


resolver = Resolver()
