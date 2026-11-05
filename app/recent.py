"""115 最近文件监听：用网页 Cookie 拉“最近变动”，发现新片就局部入库。

为什么还要这一层（已经有 CD2 目录轮询）：
  - CD2 是缓存挂载，目录/文件列表可能滞后；
  - 目录监听有深度和数量上限，深目录、突增文件容易被漏掉；
  - CMS 整理完是“云盘里多了一个文件”，115 服务端最清楚。

所以这里直接问 115：最近 N 条变动里有没有没见过的视频文件，
有就还原出它的云盘路径 -> 映射成 Plex 路径 -> 推导目录 -> 交给扫描队列。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from . import config, logger, plex, resolver, scanner, store, u115web

# 文件刚上传时 CD2 / Plex 可能还看不到，等一小会儿再扫
SETTLE_SECONDS = 15


class RecentWatcher:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_run: float = 0.0
        self.last_summary: Dict[str, Any] = {}
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="recent-115")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(25)
        while self._running:
            cfg = config.load()
            try:
                await self.run_once(cfg)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                logger.error(f"[115最近] 本轮异常：{err}")
            await asyncio.sleep(max(30, int(cfg["u115"]["recent_interval"])))

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warn(message)

    # ------------------------------------------------------------------ 单轮
    async def run_once(self, cfg: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
        cfg = cfg or config.load()
        enabled = cfg["u115"].get("recent_enabled", True)
        if not force and (not enabled or not cfg["scan"]["enabled"]):
            self.last_summary = {"enabled": False, "at": time.time()}
            return self.last_summary
        if not u115web.has_cookie(cfg):
            self._warn_once("cookie", "[115最近] 未配置 115 Cookie，最近文件监听不可用")
            self.last_summary = {"enabled": False, "reason": "no_cookie", "at": time.time()}
            return self.last_summary

        limit = int(cfg["u115"]["recent_limit"])
        started = time.time()
        try:
            items = await asyncio.to_thread(u115web.recent, cfg, limit)
        except u115web.Web115Error as err:
            self._warn_once(f"err:{err}", f"[115最近] 拉取失败：{err}")
            self.last_summary = {"enabled": True, "ok": False, "error": str(err), "at": started}
            return self.last_summary
        self._warned.discard("cookie")

        first_run = store.recent_count() == 0
        exts = tuple(cfg["scan"]["video_exts"])
        min_size = int(cfg["scan"]["min_size_mb"]) * 1024 * 1024
        new = queued = skipped = 0

        for item in sorted(items, key=lambda x: x.get("ctime") or 0):
            key = f"{item['id']}:{item['sha1'] or item['size']}"
            if not item["id"] or store.recent_seen(key):
                continue
            name = item["name"]
            lower = name.lower()
            is_video = lower.endswith(exts) and not item["is_dir"]
            if not is_video or (item["size"] and item["size"] < min_size):
                store.mark_recent(key, name, "", "")
                skipped += 1
                continue

            try:
                folder = await asyncio.to_thread(u115web.folder_path, cfg, item["cid"])
            except u115web.Web115Error as err:
                logger.warn(f"[115最近] 查询目录失败（{name}）：{err}")
                continue  # 不标记，下一轮重试
            cloud_path = (folder.rstrip("/") + "/" + name) if folder != "/" else "/" + name
            plex_path = resolver.resolver.map_plex_path(cfg, cloud_path) or ""

            if first_run and not cfg["u115"].get("recent_first_scan"):
                store.mark_recent(key, name, cloud_path, plex_path)
                skipped += 1
                continue

            if item["ctime"] and time.time() - item["ctime"] < SETTLE_SECONDS:
                continue  # 刚上传，等下一轮

            if not plex_path:
                self._warn_once(
                    "map",
                    f"[115最近] 云盘路径映射不到 Plex 路径：{cloud_path}（检查路径映射表）",
                )
                store.mark_recent(key, name, cloud_path, "")
                skipped += 1
                continue

            try:
                section = await asyncio.to_thread(plex.section_for_path, plex_path)
            except plex.PlexError as err:
                logger.warn(f"[115最近] 获取媒体库失败：{err}")
                continue
            if not section:
                store.mark_recent(key, name, cloud_path, plex_path)
                skipped += 1
                continue

            target = scanner.scanner.derive_scan_path(plex_path, section)
            result = await scanner.scanner.submit(target, section["key"], source="115recent", force=force)
            new += 1
            store.mark_recent(key, name, cloud_path, plex_path, scanned=bool(result.get("queued")))
            if result.get("queued"):
                queued += 1
                logger.info(f"[115最近] 新文件 {cloud_path} -> 扫描 {target}")
            elif result.get("error"):
                logger.warn(f"[115最近] 入队失败 {plex_path}：{result['error']}")

        self.last_run = time.time()
        self.last_summary = {
            "enabled": True,
            "ok": True,
            "at": self.last_run,
            "checked": len(items),
            "first_run": first_run,
            "new": new,
            "queued": queued,
            "skipped": skipped,
            "elapsed": round(self.last_run - started, 2),
        }
        if items:
            note = "（首次运行只记录，不触发扫描）" if first_run and not cfg["u115"].get("recent_first_scan") else ""
            logger.info(
                f"[115最近] 检查 {len(items)} 条 / 新视频 {new} / 入队 {queued}{note}"
            )
        return self.last_summary

    def status(self) -> Dict[str, Any]:
        return {
            "last_run": self.last_run,
            "summary": self.last_summary,
            "seen": store.recent_count(),
        }


recent_watcher = RecentWatcher()
