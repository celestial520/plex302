"""302 决策核心：请求 -> Plex 文件路径 -> 云盘路径 -> 直链。"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from . import config, logger, partindex, plex, providers, store

PART_RE = re.compile(r"^/library/parts/(\d+)/", re.I)
START_RE = re.compile(r"^/video/:/transcode/universal/start", re.I)


# 拿 Plex 路径（part_file / search_file / item_file）走线程 + 硬超时。
# 这几个函数内部是同步 requests，超时分别是 10s / 12s / 10s；
# 以前直接在 async 函数里调，会把整个事件循环堵死十几秒 —— 表现就是
# 「播放卡住的同时 WebUI 也打不开、日志一片空白」，因为 /login、/health
# 全都排在同一个循环里。现在丢到线程池，并给一个不超过播放器耐心的上限。
PLEX_PATH_TIMEOUT = 2.5
# 失败过的 part 短期不再重试（默认 60 秒）。播放器起播会连发 2~3 个请求，
# 每次失败都去问一遍 Plex 只会把线程池占满，越试越慢。
PART_FAIL_TTL = 60.0


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

    def ttl(self, key: str) -> int:
        """返回缓存剩余秒数（没有/已过期返回 0）。给 njs 的共享内存缓存对齐用。"""
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return 0
            return max(0, int(item[0] - now))

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
        # 同一部片并发起播（Plex 常一次发 2~3 个请求）只让一个去解析，
        # 其余等结果 —— 这是“一次播放只打一次 115”的关键一环。
        self._resolve_locks: Dict[str, asyncio.Lock] = {}
        self._part_failed: Dict[str, float] = {}
        self._part_fail_lock = threading.Lock()
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
    def explain_mapping(cfg: Dict[str, Any], plex_path: str) -> Dict[str, Any]:
        """把「路径 → 映射 → 云盘路径」每一步摊开，专门用来查映射错。

        映射错的时候 map_path 只会返回 None，然后静默回源 Plex —— 用户看到的是
        「播不了、日志也没东西」。这里把 Plex 自己上报的媒体库路径和配置里的前缀
        摆在一起对比，错在哪一层一眼能看出来。
        """
        path = "/" + unquote(str(plex_path or "")).replace("\\", "/").strip("/")
        mounts = cfg.get("mounts", []) or []
        cloud = Resolver.map_path(cfg, path)

        # Plex 自己上报的媒体库根路径：这是「①Plex 前缀」的唯一事实来源
        plex_locations: List[str] = []
        try:
            for sec in plex.sections():
                plex_locations.extend(sec.get("locations") or [])
        except Exception as err:  # noqa: BLE001
            logger.warn(f"[映射] 读取 Plex 媒体库路径失败：{err}")
        plex_locations = sorted({str(x).replace("\\", "/").rstrip("/") for x in plex_locations if x})

        hits = []
        for m in mounts:
            pre = str(m.get("plex_prefix") or "")
            if pre and (path == pre or path.startswith(pre + "/")):
                hits.append(pre)
        echo = next((m for m in mounts if str(m.get("plex_prefix") or "") in hits), None)

        hints: List[str] = []
        if not mounts:
            hints.append("还没有配置任何路径映射，所有播放都会回源 Plex。")
        elif not hits:
            hints.append(f"Plex 给的是 {plex_path}，不在任何①前缀下，所以直接回源了。")
            for loc in plex_locations:
                if loc and (path == loc or path.startswith(loc + "/")):
                    hints.append(f"看起来应该用 ①{loc}（Plex 媒体库里就是这个路径）。")
                    break
        if echo and cloud:
            extra = path[len(str(echo.get("plex_prefix") or "")):].lstrip("/")
            first = extra.split("/", 1)[0] if extra else ""
            cloud_pref = str(echo.get("cloud_prefix") or "/").strip("/")
            if first and not cloud_pref and extra.count("/") > 0:
                hints.append(
                    f"注意：①之后还剩一层「{first}」。如果 {first} 是 CD2 的挂载点名（比如 115open），"
                    f"它并不是网盘里的文件夹 —— 这时应该 ①填 {echo.get('plex_prefix')}/{first}、②填 /，"
                    f"否则云盘路径会多出 /{first} 这一层。"
                )
        return {
            "plex_path": path,
            "cloud_path": cloud,
            "mounts": [
                {"plex_prefix": m.get("plex_prefix"), "cloud_prefix": m.get("cloud_prefix") or "/"}
                for m in mounts
            ],
            "plex_locations": plex_locations,
            "matched_prefix": hits[0] if hits else "",
            "hints": hints,
        }

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
        # 先查后台建好的「part → 路径」索引：它不依赖 Plex 那个会 302 到媒体文件
        # 的接口，命中就是零延迟。没命中就让它在后台重建，本次先回源。
        from_index = partindex.lookup(part_id)
        if from_index:
            ttl = config.load()["advanced"]["part_cache_ttl"]
            self.part_cache.set(part_id, from_index, ttl)
            return from_index
        # 索引没收录这个 part：绝对不要再问 /library/parts/<id>。这版 Plex 带
        # 有效 token 会 302 到媒体文件本身，带无效 token 直接 401，两条路都拿
        # 不到路径，还会把线程拖到超时、连 WebUI 一起卡住 —— 这正是线上「播放
        # 从来没有成功过」的直接原因（日志里反复是「查询 Plex 文件路径超时」）。
        # 正确做法：立刻回源（至少能播），同时强制后台重建索引，下一次起播命中。
        partindex.rebuild()
        failed = self._part_failed.get(part_id)
        if failed and failed > time.monotonic():
            raise plex.PlexError(f"part {part_id} 不在索引里（后台重建中，本次回源 Plex）")
        with self._part_fail_lock:
            if len(self._part_failed) > 5000:
                self._part_failed.clear()
            self._part_failed[part_id] = time.monotonic() + PART_FAIL_TTL
        raise plex.PlexError(
            f"part {part_id} 不在索引里（已触发后台重建，本次回源 Plex）：{uri or '-'}"
        )

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
        diag = self.explain_mapping(cfg, plex_path)
        out: Dict[str, Any] = {
            "plex_path": plex_path,
            "cloud_path": cloud,
            "provider": cfg["provider"]["type"],
            "mapping": diag,
        }
        if not cloud:
            out.update({"ok": False, "error": "路径不在任何挂载映射里", "hints": diag["hints"]})
            return out
        try:
            link = providers.resolve(cfg, cloud, providers.ResolveContext(ip=ip, ua=ua))
            out.update({"ok": True, "url": link.url, "link_provider": link.provider})
        except providers.ProviderError as err:
            out.update({"ok": False, "error": str(err)})
        return out

    # ---------------------------------------------------------------- 主流程
    def _redirect(self, url: str, reason: str, cloud_path: str, cache_key: str) -> Dict[str, Any]:
        # cache_ttl = 直链缓存剩余寿命。njs 拿它设置共享内存缓存，
        # 保证 nginx 缓存和 Python 直链缓存一起过期，不会把过期直链甩给播放器。
        return {
            "action": "redirect",
            "url": url,
            "reason": reason,
            "cloud_path": cloud_path,
            "cache_ttl": self.url_cache.ttl(cache_key),
        }

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

        if not part_match and self._arg(args, "directPlay") == "0":
            return self._proxy("客户端明确要求转码")

        def _work() -> str:
            """同步部分：查 part → 文件路径。必须在线程里跑，不能堵事件循环。"""
            if part_match:
                return self._cached_part(part_match.group(1), token, uri)
            return self._resolve_start_path(args, token)

        try:
            plex_path = await asyncio.wait_for(
                asyncio.to_thread(_work), timeout=PLEX_PATH_TIMEOUT
            )
        except asyncio.TimeoutError:
            store.bump_stat("proxied")
            logger.warn(f"[302] 向 Plex 要文件路径超时（>{PLEX_PATH_TIMEOUT:g} 秒），本次回源：{uri}")
            return self._proxy("查询 Plex 文件路径超时")
        except plex.PlexError as err:
            store.bump_stat("error")
            logger.warn(f"[302] 获取 Plex 路径失败：{err}")
            return self._proxy(f"Plex 路径解析失败：{err}")
        except Exception as err:  # noqa: BLE001
            store.bump_stat("error")
            logger.warn(f"[302] 获取 Plex 路径异常：{err}")
            return self._proxy(f"Plex 路径解析异常：{err}")

        if not plex_path:
            return self._proxy("Plex 没有返回文件路径")
        if not self._is_video(plex_path, cfg):
            return self._proxy("非视频文件，交给 Plex 处理")

        cloud_path = self.map_path(cfg, plex_path)
        if not cloud_path:
            # 以前这里静默回源，用户只看得到「播不了、日志空白」。映射错是最常见
            # 的起播失败原因，必须留痕，并把 Plex 的真实路径一起打出来。
            prefixes = ", ".join(str(m.get("plex_prefix") or "-") for m in cfg["mounts"]) or "无"
            logger.warn(
                f"[302] 路径映射未命中，回源 Plex：{plex_path}（当前①前缀：{prefixes}）"
            )
            return self._proxy("文件不在已配置的 CD2 挂载映射内")

        ua_for_cache = providers._ua(cfg, providers.ResolveContext(ip=ip, ua=ua))  # noqa: SLF001
        cache_key = f"{cfg['provider']['type']}|{cloud_path}|{hashlib.sha1(ua_for_cache.encode('utf-8', 'ignore')).hexdigest()}"
        cached = self.url_cache.get(cache_key)
        if cached:
            # 302 次数统一由 nginx 302.log 统计（含 nginx 缓存命中），这里不再重复计数
            return self._redirect(cached, "cache", cloud_path, cache_key)

        # 单飞：Plex 起播会并发打同一个 part，只让第一个请求真的去取直链
        lock = self._resolve_locks.get(cache_key)
        if lock is None:
            lock = self._resolve_locks[cache_key] = asyncio.Lock()
        if len(self._resolve_locks) > 5000:
            self._resolve_locks.clear()
            lock = self._resolve_locks[cache_key] = asyncio.Lock()

        async with lock:
            cached = self.url_cache.get(cache_key)
            if cached:
                return self._redirect(cached, "cache-wait", cloud_path, cache_key)

            try:
                link = await asyncio.to_thread(
                    providers.resolve, cfg, cloud_path, providers.ResolveContext(ip=ip, ua=ua, plex_token=token)
                )
            except providers.ProviderError as err:
                store.bump_stat("proxied")
                logger.warn(f"[302] 直链获取失败，回源 Plex：{cloud_path} | {err}")
                return self._proxy(f"直链失败：{err}")

            ttl = int(cfg["advanced"]["url_cache_ttl"])
            if link.expires_in:
                # 115 的 expires_in 已经是「直链缓存剩余寿命」，内部已留好余量，
                # 这里不再二次扣减，两层缓存一起过期、一起只刷新一次。
                ttl = max(30, min(ttl, link.expires_in))
            self.url_cache.set(cache_key, link.url, ttl)
            if cfg["advanced"].get("log_redirects"):
                safe = self._safe_url(link.url)
                logger.info(
                    f"[直链] {ip or '-'} | {link.provider} | {cloud_path} -> {safe} | "
                    f"缓存 {ttl}s | {ua[:60]}"
                )
            return self._redirect(link.url, "ok", cloud_path, cache_key)

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
